#!/usr/bin/env bash
# One-shot deployment to a Linux VPS that already has a reverse proxy.
#
# Usage from your local machine:
#   scripts/deploy.sh root@vps.example.com
#
# What it does (idempotent):
#   1. rsyncs the project to /opt/pdf-comp on the host
#   2. installs Docker if missing
#   3. creates a 2 GB swap file if missing
#   4. starts the app container — bound to 127.0.0.1:3127
#
# After it finishes, you add ONE block to your existing reverse proxy
# config to forward /pdf_comp/* to localhost:3127. Snippets are printed
# at the end. The script does NOT touch your existing proxy or HTTPS.

set -euo pipefail

if [ $# -ne 1 ]; then
    echo "usage: $0 <user@host>" >&2
    echo "example: $0 root@1.2.3.4" >&2
    exit 1
fi

REMOTE="$1"
PROJECT_DIR="/opt/pdf-comp"
PORT="${PDF_COMP_PORT:-3127}"

step() { printf "\n→ %s\n" "$1"; }

step "Syncing project to $REMOTE:$PROJECT_DIR"
ssh "$REMOTE" "mkdir -p $PROJECT_DIR"
rsync -az --delete \
    --exclude=node_modules \
    --exclude=.next \
    --exclude=.venv \
    --exclude=tmp \
    --exclude=test-pdfs \
    --exclude=.git \
    --exclude=.DS_Store \
    ./ "$REMOTE:$PROJECT_DIR/"

step "Provisioning host (idempotent)"
ssh "$REMOTE" PROJECT_DIR="$PROJECT_DIR" PDF_COMP_PORT="$PORT" bash <<'REMOTE_BOOTSTRAP'
set -euo pipefail
cd "$PROJECT_DIR"

if ! swapon --show | grep -q swap; then
    echo "  creating 2 GB swap file"
    fallocate -l 2G /swapfile
    chmod 600 /swapfile
    mkswap /swapfile >/dev/null
    swapon /swapfile
    grep -q '/swapfile' /etc/fstab || echo '/swapfile none swap sw 0 0' >> /etc/fstab
fi

if ! command -v docker >/dev/null 2>&1; then
    echo "  installing Docker"
    curl -fsSL https://get.docker.com | sh >/dev/null
    systemctl enable --now docker
fi

printf "PDF_COMP_PORT=%s\n" "$PDF_COMP_PORT" > .env
chmod 600 .env

echo "  building and starting (this can take a few minutes on the first run)"
docker compose up -d --build
REMOTE_BOOTSTRAP

step "Done — app is running on 127.0.0.1:$PORT (the host)"
cat <<EOF

Add this block to your existing nginx server { … } for ilyagrshn.com,
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

If you also use Caddy (e.g. for some other host), the equivalent is:
    handle /pdf_comp* {
        reverse_proxy 127.0.0.1:$PORT
        request_body { max_size 1100MB }
    }

Notes:
  • Compression: brotli/gzip at the server level applies automatically to
    HTML/JS/CSS/JSON. PDF downloads (Content-Type: application/pdf) stream
    through uncompressed — JPEG inside is already compressed, recompressing
    wastes CPU.
  • The "/pdf_comp/" prefix is preserved end-to-end (Next.js basePath).
    Don't add a trailing slash to proxy_pass — that would strip the prefix
    and break Next's routing.

Reload:    nginx -t && systemctl reload nginx
Open:      https://ilyagrshn.com/pdf_comp/
Logs:      ssh $REMOTE 'cd $PROJECT_DIR && docker compose logs -f'
Update:    re-run this script
EOF
