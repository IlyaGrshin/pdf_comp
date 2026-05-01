#!/usr/bin/env bash
# Run on the VPS, inside a freshly-cloned project directory.
#
# Usage:
#   sudo ./scripts/setup.sh
#
# Override the host-side port if 3127 is taken by something else:
#   sudo PDF_COMP_PORT=3300 ./scripts/setup.sh
#
# What it does (idempotent — re-running updates the deployment):
#   1. creates a 2 GB swap file if missing  (safety net for big-PDF spikes)
#   2. installs Docker if missing
#   3. writes .env with PDF_COMP_PORT
#   4. docker compose up -d --build  (app on 127.0.0.1:$PDF_COMP_PORT)
#
# After this finishes, paste the printed nginx snippet into your existing
# server { ... } block for ilyagrshn.com and reload nginx.

set -euo pipefail

if [ "$(id -u)" -ne 0 ]; then
    echo "must run as root (try: sudo $0)" >&2
    exit 1
fi

PORT="${PDF_COMP_PORT:-3127}"

step() { printf "\n→ %s\n" "$1"; }

step "Swap (2 GB if absent)"
if ! swapon --show | grep -q swap; then
    fallocate -l 2G /swapfile
    chmod 600 /swapfile
    mkswap /swapfile >/dev/null
    swapon /swapfile
    grep -q '/swapfile' /etc/fstab || echo '/swapfile none swap sw 0 0' >> /etc/fstab
    echo "  done"
else
    echo "  already configured"
fi

step "Docker"
if ! command -v docker >/dev/null 2>&1; then
    curl -fsSL https://get.docker.com | sh >/dev/null
    systemctl enable --now docker
    echo "  installed"
else
    echo "  already installed"
fi

step "Building and starting (first build takes ~5-10 minutes on a small VPS)"
printf "PDF_COMP_PORT=%s\n" "$PORT" > .env
chmod 600 .env
docker compose up -d --build

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

Optional but recommended — per-IP rate limit. Add this OUTSIDE any server { … }
block (e.g. in /etc/nginx/conf.d/pdf_comp-ratelimit.conf or near the top of
the existing site config):

    limit_req_zone \$binary_remote_addr zone=pdf_comp:10m rate=20r/m;

…then inside the location block above:

    limit_req zone=pdf_comp burst=10 nodelay;

This caps each client IP at ~20 compressions/min with a burst of 10.
Without it, a single misbehaving client can saturate your VPS RAM.

Reload:    nginx -t && systemctl reload nginx
Open:      https://<your-domain>/pdf_comp/

To update later:
    git pull && sudo docker compose up -d --build

Logs:
    docker compose logs -f
EOF
