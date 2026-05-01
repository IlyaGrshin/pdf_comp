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

On a fresh Linux VPS that already runs nginx (or another reverse proxy):

```bash
git clone https://github.com/IlyaGrshin/pdf-comp.git
cd pdf-comp
sudo ./scripts/setup.sh
```

The script sets up a 2 GB swap file, installs Docker if missing, builds the
image, and starts the app on `127.0.0.1:3127`. It prints an nginx
`location /pdf_comp/ { ... }` block at the end — paste it into the
appropriate `server { ... }` block on the host and `systemctl reload nginx`.

Updates later:

```bash
git pull && sudo docker compose up -d --build
```

### Alternative: rsync from local (no GitHub)

```bash
./scripts/deploy.sh root@your-vps
```

This rsyncs the project from your laptop to `/opt/pdf-comp` on the VPS
and runs the same bootstrap remotely. Useful if you'd rather not publish
the code or set up a deploy key on the VPS.

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
