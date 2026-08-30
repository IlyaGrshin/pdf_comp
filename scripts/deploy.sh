#!/usr/bin/env bash
# One-shot deployment to a systemd Linux VPS. No Docker involved —
# saves ~70 MB idle RAM (dockerd + containerd) compared to the old
# containerized layout and shaves the container overhead off Node.
#
# Usage from your local machine:
#   scripts/deploy.sh root@vps.example.com
#
# What it does (idempotent — safe to re-run as the update flow):
#   1. Builds Next.js standalone locally (needs pnpm here, on the same
#      Node major the host runs — see the version check below).
#   2. Installs apt packages on the host: nodejs, qpdf,
#      libjpeg-turbo-progs, python3-venv.
#   3. Creates the `pdf-comp` service user + /opt/pdf-comp/current.
#   4. rsyncs .next/standalone + .next/static + public + scripts to
#      the host, then builds a Python venv there.
#   5. Installs /etc/systemd/system/pdf-comp.service and /etc/pdf-comp.env,
#      enables + restarts the service (listens on 127.0.0.1:$PORT).
#   6. Creates a 2 GB swap file if the host has none.
#
# After it finishes, add the two nginx blocks it prints (a rate-limit zone
# and the location) to your existing config. The script does not touch your
# reverse proxy or HTTPS.

set -euo pipefail

if [ $# -ne 1 ]; then
    echo "usage: $0 <user@host>" >&2
    echo "example: $0 root@1.2.3.4" >&2
    exit 1
fi

REMOTE="$1"
INSTALL_DIR="/opt/pdf-comp/current"
PORT="${PDF_COMP_PORT:-3127}"

# .nvmrc is the single source of truth for the Node major: it feeds the
# NodeSource install, the host's upgrade threshold, and the local build
# check below. Bumping Node means editing one file.
NODE_MAJOR=$(tr -dc '0-9' < .nvmrc 2>/dev/null || true)
if [ -z "$NODE_MAJOR" ]; then
    echo "cannot read a Node major from .nvmrc — run this from the repo root" >&2
    exit 1
fi

step() { printf "\n→ %s\n" "$1"; }

# The build runs here but the artifact runs there, and nothing else in this
# script would notice a mismatch: `.next/standalone` ships traced
# node_modules and a server.js emitted for whichever Node built them, so a
# newer local major can bake in syntax the host's Node rejects — at request
# time, not build time. Probing before the build also fails fast when the
# host is unreachable.
step "Checking Node major (local build vs host runtime)"
HOST_NODE=$(ssh "$REMOTE" '/usr/bin/node -v 2>/dev/null || true' | cut -c2- | cut -d. -f1)
# Absent, unparseable, or too old: provisioning below installs $NODE_MAJOR.
case "$HOST_NODE" in
    ''|*[!0-9]*) HOST_NODE="$NODE_MAJOR" ;;
esac
if [ "$HOST_NODE" -lt "$NODE_MAJOR" ]; then
    HOST_NODE="$NODE_MAJOR"
elif [ "$HOST_NODE" -gt "$NODE_MAJOR" ]; then
    # Deliberately not resolved by either silent option. Following the host
    # would make .nvmrc decorative — the declared major would be built with
    # nowhere and deployed nowhere. Forcing the host down to it would mean
    # apt-downgrading a system-wide package on a box that also runs the
    # reverse proxy and whatever else lives there, which is not this script's
    # call to make. So: stop, and let a human pick.
    if [ "${PDF_COMP_ALLOW_NODE_MISMATCH:-}" = "1" ]; then
        echo "warning: host runs Node $HOST_NODE, .nvmrc declares $NODE_MAJOR (override set)" >&2
    else
        echo "Host runs Node $HOST_NODE but .nvmrc declares $NODE_MAJOR." >&2
        echo "Provisioning never downgrades the host's system Node, so this deploy" >&2
        echo "would build and run on $HOST_NODE while the repo claims $NODE_MAJOR." >&2
        echo "Bump .nvmrc to $HOST_NODE, downgrade the host deliberately, or set" >&2
        echo "PDF_COMP_ALLOW_NODE_MISMATCH=1 to deploy on $HOST_NODE anyway." >&2
        exit 1
    fi
fi
LOCAL_NODE=$(node -v 2>/dev/null | cut -c2- | cut -d. -f1 || true)
if [ -z "$LOCAL_NODE" ]; then
    echo "node not found on PATH — the Next.js build runs locally, not on the host" >&2
    exit 1
fi
if [ "$LOCAL_NODE" != "$HOST_NODE" ]; then
    if [ "${PDF_COMP_ALLOW_NODE_MISMATCH:-}" = "1" ]; then
        echo "warning: building on Node $LOCAL_NODE for a host running $HOST_NODE (override set)" >&2
    else
        echo "Node major mismatch: building on $LOCAL_NODE, host runs $HOST_NODE." >&2
        echo "Put Node $HOST_NODE first on PATH so the build matches the runtime" >&2
        echo "(nvm use $HOST_NODE, or brew's node@$HOST_NODE/bin), or set" >&2
        echo "PDF_COMP_ALLOW_NODE_MISMATCH=1 to deploy anyway." >&2
        exit 1
    fi
fi
echo "local $LOCAL_NODE, host $HOST_NODE"

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
# NODE_MAJOR comes from .nvmrc via the assignment prefix; the heredoc stays
# quoted so nothing else here is expanded by the local shell.
ssh "$REMOTE" NODE_MAJOR="$NODE_MAJOR" bash <<'REMOTE_PROVISION'
set -euo pipefail
export DEBIAN_FRONTEND=noninteractive

# Node from NodeSource — Debian bookworm ships 18, which is too old for
# Next.js 16. Always install: the systemd unit hard-codes `/usr/bin/node`,
# and skipping install just because an nvm/asdf node is on PATH would leave
# the service pointing at a missing binary.
if ! dpkg -l nodejs 2>/dev/null | grep -q '^ii  nodejs' \
   || [ "$(/usr/bin/node -v 2>/dev/null | cut -c2- | cut -d. -f1)" -lt "$NODE_MAJOR" ]; then
    curl -fsSL "https://deb.nodesource.com/setup_${NODE_MAJOR}.x" | bash - >/dev/null
    apt-get install -y --no-install-recommends nodejs >/dev/null
fi

apt-get update >/dev/null
apt-get install -y --no-install-recommends \
    python3 python3-venv qpdf libjpeg-turbo-progs rsync >/dev/null

# Service user. --system: no login, no home, no aging. Home points at
# the install dir so relative paths still work if anyone su's in.
# Named `pdf-comp` (not the generic `app`) to avoid inheriting an
# existing account belonging to another service on the host.
if ! id pdf-comp >/dev/null 2>&1; then
    useradd --system --home /opt/pdf-comp --shell /usr/sbin/nologin pdf-comp
fi

mkdir -p /opt/pdf-comp/current/tmp
chown -R pdf-comp:pdf-comp /opt/pdf-comp
REMOTE_PROVISION

step "Stopping service before mutating install tree"
# Without this, rsync --delete replaces files that node/python may be
# reading, and clients hitting the service during the swap can see 500s
# or half-updated scripts. Downtime here is a few seconds on a small
# host. Ignore failure — first-time installs have no unit yet.
ssh "$REMOTE" "systemctl stop pdf-comp.service 2>/dev/null || true"

step "Rsyncing build to $REMOTE:$INSTALL_DIR"
# --delete on the top level would wipe the on-host .venv every deploy;
# exclude .venv and tmp explicitly so the venv survives updates.
rsync -az --delete \
    --exclude=tmp \
    --exclude=.venv \
    "$STAGE/" "$REMOTE:$INSTALL_DIR/"
ssh "$REMOTE" "chown -R pdf-comp:pdf-comp $INSTALL_DIR"

step "Refreshing Python venv on host"
ssh "$REMOTE" bash <<REMOTE_VENV
set -euo pipefail
cd "$INSTALL_DIR"
if [ ! -x .venv/bin/python ]; then
    python3 -m venv .venv
fi
.venv/bin/pip install --no-cache-dir --disable-pip-version-check --upgrade pip >/dev/null
.venv/bin/pip install --no-cache-dir --disable-pip-version-check -r scripts/requirements.txt >/dev/null
chown -R pdf-comp:pdf-comp .venv
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
systemctl start pdf-comp.service
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

Add BOTH blocks to your existing nginx config for the host, then reload it.
The rate limit is not decoration: the app caps concurrency and disk, but the
cheapest way to keep a single client from occupying every slot is to stop it
at the proxy.

──── nginx (OUTSIDE any server { … } block — e.g. /etc/nginx/conf.d/) ────
limit_req_zone \$binary_remote_addr zone=pdf_comp:10m rate=20r/m;

──── nginx (inside the matching server { … } block) ────
location /pdf_comp/ {
    proxy_pass http://127.0.0.1:$PORT;
    proxy_http_version 1.1;
    proxy_set_header Host \$host;
    proxy_set_header X-Forwarded-For \$proxy_add_x_forwarded_for;
    proxy_set_header X-Forwarded-Proto \$scheme;

    # ~20 compressions/min per client IP, bursting to 10.
    limit_req zone=pdf_comp burst=10 nodelay;

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
