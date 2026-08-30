#!/usr/bin/env bash
# Run on the VPS itself, inside a freshly-cloned project directory.
# Use scripts/deploy.sh instead if you're pushing from your laptop.
#
# Usage:
#   sudo ./scripts/setup.sh
#
# Override the port if 3127 is taken:
#   sudo PDF_COMP_PORT=3300 ./scripts/setup.sh
#
# What it does (idempotent — re-running updates the deployment):
#   1. Creates a 2 GB swap file if missing (spike headroom for big PDFs).
#   2. Installs apt packages: nodejs from NodeSource (major read from
#      .nvmrc), python3-venv, qpdf, libjpeg-turbo-progs, pnpm.
#   3. `pnpm install --frozen-lockfile && pnpm build` — builds Next.js
#      standalone on the host itself.
#   4. Stages standalone + static + public + scripts under
#      /opt/pdf-comp/current/, creates .venv from scripts/requirements.txt.
#   5. Installs the pdf-comp systemd unit + /etc/pdf-comp.env, starts it.
#   6. Prints an nginx snippet.

set -euo pipefail

if [ "$(id -u)" -ne 0 ]; then
    echo "must run as root (try: sudo $0)" >&2
    exit 1
fi

PORT="${PDF_COMP_PORT:-3127}"
INSTALL_DIR="/opt/pdf-comp/current"
SRC_DIR="$(cd "$(dirname "$0")/.." && pwd)"

# Same single source of truth as scripts/deploy.sh — see the note there.
# Read via $SRC_DIR: the apt step below runs before we cd into the repo.
NODE_MAJOR=$(tr -dc '0-9' < "$SRC_DIR/.nvmrc" 2>/dev/null || true)
if [ -z "$NODE_MAJOR" ]; then
    echo "cannot read a Node major from $SRC_DIR/.nvmrc" >&2
    exit 1
fi

step() { printf "\n→ %s\n" "$1"; }

step "Swap (2 GB if absent)"
if ! swapon --show | grep -q swap; then
    fallocate -l 2G /swapfile
    chmod 600 /swapfile
    mkswap /swapfile >/dev/null
    swapon /swapfile
    grep -q '/swapfile' /etc/fstab || echo '/swapfile none swap sw 0 0' >> /etc/fstab
fi

step "apt packages"
export DEBIAN_FRONTEND=noninteractive
# Always resolve Node from the apt package — the systemd unit hard-codes
# `/usr/bin/node`, so we can't rely on a nvm/asdf install on PATH.
HOST_NODE=$(/usr/bin/node -v 2>/dev/null | cut -c2- | cut -d. -f1)
case "$HOST_NODE" in
    ''|*[!0-9]*) HOST_NODE=0 ;;
esac
if [ "$HOST_NODE" -gt "$NODE_MAJOR" ]; then
    # Same reasoning as scripts/deploy.sh: building against a newer major than
    # the repo declares makes .nvmrc decorative, and apt-downgrading a
    # system-wide package on a host that also serves other things is not this
    # script's decision.
    echo "Host runs Node $HOST_NODE but .nvmrc declares $NODE_MAJOR." >&2
    echo "This script will not downgrade the host's system Node." >&2
    echo "Either bump .nvmrc to $HOST_NODE, or downgrade the host deliberately." >&2
    exit 1
fi
if ! dpkg -l nodejs 2>/dev/null | grep -q '^ii  nodejs' \
   || [ "$HOST_NODE" -lt "$NODE_MAJOR" ]; then
    curl -fsSL "https://deb.nodesource.com/setup_${NODE_MAJOR}.x" | bash - >/dev/null
    apt-get install -y --no-install-recommends nodejs >/dev/null
fi
apt-get update >/dev/null
apt-get install -y --no-install-recommends \
    python3 python3-venv qpdf libjpeg-turbo-progs rsync >/dev/null
# corepack ships with the NodeSource nodejs. Which pnpm it installs comes
# from package.json's `packageManager` field — the same single-source rule
# .nvmrc gives Node, so the version is pinned in exactly one place and CI,
# the host and a laptop cannot drift apart. Enable unconditionally: an
# unrelated global pnpm already on PATH would otherwise shadow the pin.
export COREPACK_ENABLE_DOWNLOAD_PROMPT=0
corepack enable

step "Service user + install dir"
# Dedicated user name (not the generic `app`) so a co-hosted service
# can't accidentally inherit our filesystem/process privileges by
# reusing an existing account.
if ! id pdf-comp >/dev/null 2>&1; then
    useradd --system --home /opt/pdf-comp --shell /usr/sbin/nologin pdf-comp
fi
mkdir -p "$INSTALL_DIR/tmp"

step "Build (this can take a few minutes on a small VPS)"
cd "$SRC_DIR"
pnpm install --frozen-lockfile
pnpm build

step "Stopping service before mutating install tree"
# Prevents mid-request file swap on the update flow. First-time
# installs have no unit yet — swallow the error.
systemctl stop pdf-comp.service 2>/dev/null || true

step "Staging to $INSTALL_DIR"
# Keep .venv and tmp across re-runs (they live inside INSTALL_DIR).
rsync -a --delete \
    --exclude=tmp \
    --exclude=.venv \
    .next/standalone/. "$INSTALL_DIR/"
mkdir -p "$INSTALL_DIR/.next"
rsync -a --delete .next/static/. "$INSTALL_DIR/.next/static/"
[ -d public ] && rsync -a --delete public/. "$INSTALL_DIR/public/"
rsync -a --delete scripts/. "$INSTALL_DIR/scripts/"

step "Python venv"
cd "$INSTALL_DIR"
if [ ! -x .venv/bin/python ]; then
    python3 -m venv .venv
fi
.venv/bin/pip install --no-cache-dir --disable-pip-version-check --upgrade pip >/dev/null
.venv/bin/pip install --no-cache-dir --disable-pip-version-check -r scripts/requirements.txt >/dev/null
chown -R pdf-comp:pdf-comp /opt/pdf-comp

step "systemd unit"
install -m 0644 "$SRC_DIR/systemd/pdf-comp.service" /etc/systemd/system/pdf-comp.service
cat >/etc/pdf-comp.env <<ENV_EOF
NODE_ENV=production
NEXT_TELEMETRY_DISABLED=1
HOSTNAME=127.0.0.1
PORT=$PORT
PATH=/usr/local/bin:/usr/bin:/bin
NODE_OPTIONS=--max-old-space-size=768
MALLOC_ARENA_MAX=2
PYTHONDONTWRITEBYTECODE=1
PYTHONUNBUFFERED=1
ENV_EOF
chmod 600 /etc/pdf-comp.env
systemctl daemon-reload
systemctl enable pdf-comp.service >/dev/null
systemctl start pdf-comp.service

cat <<EOF


✓ App running on 127.0.0.1:$PORT

Add this block to your existing nginx server { … } config for the domain,
then reload nginx:

──── nginx (inside server { … }) ────
location /pdf_comp/ {
    proxy_pass http://127.0.0.1:$PORT;
    proxy_http_version 1.1;
    proxy_set_header Host \$host;
    proxy_set_header X-Forwarded-For \$proxy_add_x_forwarded_for;
    proxy_set_header X-Forwarded-Proto \$scheme;

    client_max_body_size 1100M;
    proxy_request_buffering off;
    proxy_buffering off;

    proxy_read_timeout 600s;
    proxy_send_timeout 600s;
}

Per-IP rate limit — add it. The app caps concurrency and disk, but stopping a
single noisy client at the proxy is what keeps it from holding every slot.
The zone goes OUTSIDE any server { … } block (e.g. in
/etc/nginx/conf.d/pdf_comp-ratelimit.conf, or near the top of the site config):

    limit_req_zone \$binary_remote_addr zone=pdf_comp:10m rate=20r/m;

…and the limit itself on the upload endpoint alone, NOT on the prefix location
above — there it would also meter the landing page, every _next asset, the
health check and the downloads, so one cold page load could spend the whole
burst and 503 a real user. An exact-match location inherits nothing, so the
proxy lines are repeated:

location = /pdf_comp/api/compress {
    limit_req zone=pdf_comp burst=10 nodelay;

    proxy_pass http://127.0.0.1:$PORT;
    proxy_http_version 1.1;
    proxy_set_header Host \$host;
    proxy_set_header X-Forwarded-For \$proxy_add_x_forwarded_for;
    proxy_set_header X-Forwarded-Proto \$scheme;

    client_max_body_size 1100M;
    proxy_request_buffering off;
    proxy_buffering off;
    proxy_read_timeout 600s;
    proxy_send_timeout 600s;
}

This caps each client IP at ~20 compressions/min with a burst of 10.

Reload:    nginx -t && systemctl reload nginx
Open:      https://<your-domain>/pdf_comp/

To update later, re-run this script (git pull first).

Logs:
    journalctl -u pdf-comp -f
Status:
    systemctl status pdf-comp
EOF
