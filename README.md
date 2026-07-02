# pdf-comp

PDF compression service. Designed for Figma exports but works on any PDF —
the pipeline preserves vectors, transparency groups, blend modes and fonts;
only image streams are recompressed and identical streams are deduplicated
across pages. Compressed files are delivered through your existing reverse
proxy without the service ever reading content into application logs or
persisting beyond the download window.

Built around [pikepdf](https://pikepdf.readthedocs.io/) (the same library
[OCRmyPDF](https://ocrmypdf.readthedocs.io/) uses internally) plus
[mozjpeg](https://github.com/mozilla/mozjpeg). Mounts at `/pdf_comp` by
default so it can sit alongside an existing site at the apex of the same
domain.

## Deploy

Native (no Docker). Node + Python run directly under systemd on the host —
that saves ~70 MB idle RAM compared to the containerized layout, which on
a 2 GB VPS is meaningful headroom.

On a fresh Linux VPS (Debian 12+ / Ubuntu 22.04+, cgroup v2) that already
runs nginx or another reverse proxy:

```bash
git clone https://github.com/IlyaGrshin/pdf_comp.git
cd pdf_comp
sudo ./scripts/setup.sh
```

The script sets up a 2 GB swap file, installs Node 22 + qpdf +
libjpeg-turbo, builds Next.js standalone in place, drops
`pdf-comp.service` into systemd, and starts the app on `127.0.0.1:3127`.
It prints an nginx `location /pdf_comp/ { ... }` block at the end —
paste it into the appropriate `server { ... }` block on the host and
`systemctl reload nginx`.

Updates later:

```bash
git pull && sudo ./scripts/setup.sh
```

### Alternative: rsync from local (no GitHub)

```bash
./scripts/deploy.sh root@your-vps
```

Builds locally, rsyncs `.next/standalone` + `scripts` to `/opt/pdf-comp/`
on the VPS, and installs the same systemd unit remotely. Useful if you'd
rather not put the source or a build toolchain on the VPS.

## Local dev

```bash
brew install ghostscript qpdf mozjpeg pnpm
python3 -m venv --copies .venv
.venv/bin/pip install -r scripts/requirements.txt
pnpm install
pnpm dev   # http://localhost:3000/pdf_comp/
```

The `--copies` flag on `python3 -m venv` is important — Turbopack rejects
symlinks that point outside the project root, and the default venv layout
on macOS uses a symlink to the Homebrew Python install.

## Architecture, conventions, gotchas

See [AGENTS.md](./AGENTS.md) for the pipeline diagram, critical invariants
(four things that took dozens of iterations to nail down), the auto-tuning
table, and the privacy/security guarantees that the landing copy promises.

## License

Personal project, no license declared. Don't redistribute the binary or
copy the brand. Ideas, code patterns and the pikepdf integration are free
for inspiration.
