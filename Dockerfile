# syntax=docker/dockerfile:1.7
ARG NODE_VERSION=22

# --- Node deps ---
FROM node:${NODE_VERSION}-bookworm-slim AS deps
WORKDIR /app
RUN corepack enable && corepack prepare pnpm@latest --activate
COPY package.json pnpm-lock.yaml pnpm-workspace.yaml ./
RUN --mount=type=cache,id=pnpm,target=/root/.local/share/pnpm/store \
    pnpm install --frozen-lockfile

# --- Next.js build ---
FROM node:${NODE_VERSION}-bookworm-slim AS build
WORKDIR /app
RUN corepack enable && corepack prepare pnpm@latest --activate
COPY --from=deps /app/node_modules ./node_modules
COPY . .
ENV NEXT_TELEMETRY_DISABLED=1
RUN pnpm build

# --- Runtime ---
FROM node:${NODE_VERSION}-bookworm-slim AS runtime
WORKDIR /app
ENV NODE_ENV=production
ENV NEXT_TELEMETRY_DISABLED=1
ENV PORT=3000
ENV HOSTNAME=0.0.0.0

# System packages:
#  ghostscript, qpdf — kept as fallbacks even though Maximum uses pikepdf path
#  python3 + venv     — for scripts/recompress.py (pikepdf+Pillow pipeline)
#  libjpeg-turbo-progs — provides /usr/bin/cjpeg used by recompress.py for
#                        better JPEG encoding than Pillow's default
#  tini, ca-certificates — process supervisor + TLS roots
RUN apt-get update \
 && apt-get install -y --no-install-recommends \
      ghostscript qpdf tini ca-certificates \
      python3 python3-venv \
      libjpeg-turbo-progs \
 && rm -rf /var/lib/apt/lists/*

RUN groupadd -r app && useradd -r -g app -d /app app

# Copy Next standalone output + scripts dir.
COPY --from=build --chown=app:app /app/.next/standalone ./
COPY --from=build --chown=app:app /app/.next/static ./.next/static
COPY --from=build --chown=app:app /app/public ./public
COPY --from=build --chown=app:app /app/scripts ./scripts

# Python venv with pikepdf + Pillow (pinned via scripts/requirements.txt).
# Done as root, then chowned so non-root `app` user can execute it.
RUN python3 -m venv /app/.venv \
 && /app/.venv/bin/pip install --no-cache-dir --upgrade pip \
 && /app/.venv/bin/pip install --no-cache-dir -r /app/scripts/requirements.txt \
 && chown -R app:app /app/.venv

# Job tmp dir (writable by `app`).
RUN mkdir -p /app/tmp && chown -R app:app /app/tmp

USER app
EXPOSE 3000
HEALTHCHECK --interval=30s --timeout=5s --retries=3 \
  CMD node -e "fetch('http://127.0.0.1:3000/pdf_comp/api/health').then(r=>process.exit(r.ok?0:1)).catch(()=>process.exit(1))"
ENTRYPOINT ["/usr/bin/tini","--"]
CMD ["node","server.js"]
