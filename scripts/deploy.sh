#!/usr/bin/env bash
# One-shot deployment to a fresh Linux VPS.
#
# Usage from your local machine:
#   scripts/deploy.sh root@vps.example.com pdf.example.com
#
# What it does (idempotent):
#   1. rsyncs the project to /opt/pdf-comp on the host
#   2. installs Docker if missing
#   3. creates a 2 GB swap file if missing
#   4. writes .env with DOMAIN
#   5. starts the app + Caddy reverse proxy (auto-HTTPS via Let's Encrypt)
#
# Requirements:
#   - SSH access to the host (root or a user with passwordless sudo)
#   - DNS A record for the domain pointing at the VPS IP (Caddy needs it
#     to obtain the certificate)
#
# Re-run any time to update — only changed Docker layers rebuild.

set -euo pipefail

if [ $# -ne 2 ]; then
    echo "usage: $0 <user@host> <domain>" >&2
    echo "example: $0 root@1.2.3.4 pdf.example.com" >&2
    exit 1
fi

REMOTE="$1"
DOMAIN="$2"
PROJECT_DIR="/opt/pdf-comp"

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
ssh "$REMOTE" DOMAIN="$DOMAIN" PROJECT_DIR="$PROJECT_DIR" bash <<'REMOTE_BOOTSTRAP'
set -euo pipefail
cd "$PROJECT_DIR"

# 2 GB swap if absent — safety net for occasional big-PDF memory spikes.
if ! swapon --show | grep -q swap; then
    echo "  creating 2 GB swap file"
    fallocate -l 2G /swapfile
    chmod 600 /swapfile
    mkswap /swapfile >/dev/null
    swapon /swapfile
    grep -q '/swapfile' /etc/fstab || echo '/swapfile none swap sw 0 0' >> /etc/fstab
fi

# Docker if absent.
if ! command -v docker >/dev/null 2>&1; then
    echo "  installing Docker"
    curl -fsSL https://get.docker.com | sh >/dev/null
    systemctl enable --now docker
fi

# Write .env so docker-compose can interpolate the domain into Caddy config.
printf "DOMAIN=%s\n" "$DOMAIN" > .env
chmod 600 .env

echo "  building and starting (this can take a few minutes on the first run)"
docker compose pull caddy >/dev/null
docker compose up -d --build
REMOTE_BOOTSTRAP

step "Done"
echo "  https://$DOMAIN"
echo "  curl https://$DOMAIN/api/health   # should return: ok"
echo
echo "  Caddy may take ~30s to obtain the TLS cert on first run."
echo "  Logs:    ssh $REMOTE 'cd $PROJECT_DIR && docker compose logs -f'"
echo "  Update:  re-run this script"
