#!/usr/bin/env bash
# One-shot deployment to a systemd Linux VPS. No Docker involved —
# saves ~70 MB idle RAM (dockerd + containerd) compared to the old
# containerized layout and shaves the container overhead off Node.
#
# Usage from your local machine:
#   scripts/deploy.sh root@vps.example.com
#
# What it does (idempotent — safe to re-run as the update flow):
#   1. Builds Next.js standalone locally (needs Node 22 + pnpm here).
#   2. Installs apt packages on the host: nodejs 22, qpdf,
#      libjpeg-turbo-progs, python3-venv.
#   3. Creates the `app` service user + /opt/pdf-comp/current.
#   4. rsyncs .next/standalone + .next/static + public + scripts to
#      the host, then builds a Python venv there.
#   5. Installs /etc/systemd/system/pdf-comp.service and /etc/pdf-comp.env,
#      enables + restarts the service (listens on 127.0.0.1:$PORT).
#   6. Creates a 2 GB swap file if the host has none.
#
# After it finishes, add ONE block to your existing nginx (snippets
# printed at the end). The script does not touch your reverse proxy
# or HTTPS.

set -euo pipefail

if [ $# -ne 1 ]; then
    echo "usage: $0 <user@host>" >&2
    echo "example: $0 root@1.2.3.4" >&2
    exit 1
fi

REMOTE="$1"
INSTALL_DIR="/opt/pdf-comp/current"
PORT="${PDF_COMP_PORT:-3127}"

step() { printf "\n→ %s\n" "$1"; }

step "Building Next.js standalone locally"
pnpm install --frozen-lockfile
pnpm build

step "Preparing staging tree"
STAGE=$(mktemp -d)
trap 'rm -rf "$STAGE"' EXIT
# The standalone bundle is a self-contained mini node_modules + server.js.
cp -R .next/standalone/. "$STAGE/"
# `.next/static` and `public` are NOT in standalone — ship alongside.
mkdir -p "$STAGE/.next"
cp -R .next/static "$STAGE/.next/static"
[ -d public ] && cp -R public "$STAGE/public"
cp -R scripts "$STAGE/scripts"
mkdir -p "$STAGE/tmp"

step "Provisioning host packages + user (idempotent)"
ssh "$REMOTE" bash <<'REMOTE_PROVISION'
set -euo pipefail
export DEBIAN_FRONTEND=noninteractive

# Node 22 from NodeSource — Debian bookworm ships 18, which is too old
# for Next.js 16.
if ! command -v node >/dev/null 2>&1 || [ "$(node -v | cut -c2- | cut -d. -f1)" -lt 22 ]; then
    curl -fsSL https://deb.nodesource.com/setup_22.x | bash - >/dev/null
    apt-get install -y --no-install-recommends nodejs >/dev/null
fi

apt-get update >/dev/null
apt-get install -y --no-install-recommends \
    python3 python3-venv qpdf libjpeg-turbo-progs rsync >/dev/null

# Service user. --system: no login, no home, no aging. Home points at
# the install dir so relative paths still work if anyone su's in.
if ! id app >/dev/null 2>&1; then
    useradd --system --home /opt/pdf-comp --shell /usr/sbin/nologin app
fi

mkdir -p /opt/pdf-comp/current/tmp
chown -R app:app /opt/pdf-comp
REMOTE_PROVISION

step "Rsyncing build to $REMOTE:$INSTALL_DIR"
# --delete on the top level would wipe the on-host .venv every deploy;
# exclude .venv and tmp explicitly so the venv survives updates.
rsync -az --delete \
    --exclude=tmp \
    --exclude=.venv \
    "$STAGE/" "$REMOTE:$INSTALL_DIR/"
ssh "$REMOTE" "chown -R app:app $INSTALL_DIR"

step "Refreshing Python venv on host"
ssh "$REMOTE" bash <<REMOTE_VENV
set -euo pipefail
cd "$INSTALL_DIR"
if [ ! -x .venv/bin/python ]; then
    python3 -m venv .venv
fi
.venv/bin/pip install --no-cache-dir --disable-pip-version-check --upgrade pip >/dev/null
.venv/bin/pip install --no-cache-dir --disable-pip-version-check -r scripts/requirements.txt >/dev/null
chown -R app:app .venv
REMOTE_VENV

step "Installing systemd unit"
scp -q systemd/pdf-comp.service "$REMOTE:/etc/systemd/system/pdf-comp.service"

# Outer heredoc is unquoted so $PORT expands locally; the inner one
# stays unquoted for the same reason. Nothing else in the block uses
# $VAR syntax that could be misread by the remote shell.
ssh "$REMOTE" bash <<REMOTE_SYSTEMD
set -euo pipefail
cat >/etc/pdf-comp.env <<ENV_EOF
NODE_ENV=production
NEXT_TELEMETRY_DISABLED=1
HOSTNAME=127.0.0.1
PORT=$PORT
PATH=/usr/local/bin:/usr/bin:/bin
# See AGENTS.md → "Self-healing memory pressure" for the reasoning
# behind each of these — they are what makes 45 MB idle possible.
NODE_OPTIONS=--max-old-space-size=768
MALLOC_ARENA_MAX=2
PYTHONDONTWRITEBYTECODE=1
PYTHONUNBUFFERED=1
ENV_EOF
chmod 600 /etc/pdf-comp.env

systemctl daemon-reload
systemctl enable pdf-comp.service >/dev/null
systemctl restart pdf-comp.service
sleep 1
systemctl --no-pager --lines=0 status pdf-comp.service | head -8
REMOTE_SYSTEMD

step "Ensuring 2 GB swap (idempotent)"
ssh "$REMOTE" bash <<'REMOTE_SWAP'
set -euo pipefail
if ! swapon --show | grep -q swap; then
    fallocate -l 2G /swapfile
    chmod 600 /swapfile
    mkswap /swapfile >/dev/null
    swapon /swapfile
    grep -q '/swapfile' /etc/fstab || echo '/swapfile none swap sw 0 0' >> /etc/fstab
fi
REMOTE_SWAP

step "Done — app is listening on 127.0.0.1:$PORT"
cat <<EOF

Add this block to your existing nginx server { … } for the host,
then reload nginx:

──── nginx (inside the matching server { … } block) ────
location /pdf_comp/ {
    proxy_pass http://127.0.0.1:$PORT;
    proxy_http_version 1.1;
    proxy_set_header Host \$host;
    proxy_set_header X-Forwarded-For \$proxy_add_x_forwarded_for;
    proxy_set_header X-Forwarded-Proto \$scheme;

    # Streaming uploads + downloads — never buffer (1 GB-class files).
    client_max_body_size 1100M;
    proxy_request_buffering off;
    proxy_buffering off;

    # Compression on big PDFs can run a few minutes; default 60s is too short.
    proxy_read_timeout 600s;
    proxy_send_timeout 600s;
}

Caddy equivalent:
    handle /pdf_comp* {
        reverse_proxy 127.0.0.1:$PORT
        request_body { max_size 1100MB }
    }

Reload:    nginx -t && systemctl reload nginx
Open:      https://ilyagrshn.com/pdf_comp/
Logs:      ssh $REMOTE 'journalctl -u pdf-comp -f'
Status:    ssh $REMOTE 'systemctl status pdf-comp'
Update:    re-run this script
EOF
