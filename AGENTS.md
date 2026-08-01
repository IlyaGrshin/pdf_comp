<!-- BEGIN:nextjs-agent-rules -->
# This is NOT the Next.js you know

This version has breaking changes — APIs, conventions, and file structure may all differ from your training data. Read the relevant guide in `node_modules/next/dist/docs/` before writing any code. Heed deprecation notices.
<!-- END:nextjs-agent-rules -->

# pdf-comp

PDF compression service. Designed for Figma exports but works on any PDF.
The whole value proposition is preserving vectors, transparency groups, blend
modes, soft-mask structure, and fonts — only **image stream content** is
touched. Mounted at `/pdf_comp` on the host.

## Pipeline

```
upload (streamed to disk, never buffered fully in memory)
   ↓
validate-pdf  (magic bytes + qpdf --requires-password)
   ↓
compress()  →  spawn .venv/bin/python scripts/recompress.py
                ├─ pikepdf walks every Image XObject
                ├─ ThreadPoolExecutor:  resize (LANCZOS) + encode (mozjpeg)
                ├─ write new bytes to objects     ← main thread only
                └─ cross-page dedup of identical streams
   ↓
qpdf is no longer in the runtime path; pikepdf.save(linearize=True) handles it
   ↓
final.pdf      (or input.pdf if compressed >= 95% of original — no-benefit)
   ↓
streamed download  →  job dir deleted on close
```

## Critical invariants — read before touching anything

- **Never touch non-image content.** Vectors, transparency groups, soft-mask
  structure, blend modes, fonts, OCGs all pass through 1:1. Anything that
  rewrites whole content streams (gs `pdfwrite`, pdftk, mutool convert) is
  off-limits — they flatten Figma effects.
- **`/Width` and `/Height` MUST be updated after resize.** PDF viewers read the
  dict, not the JPEG SOF markers; mismatch → viewer stretches a shrunken
  bitmap. See the comment in `scripts/recompress.py` near the `obj.write`
  call.
- **No `/sRGB` ColorConversionStrategy** if reintroducing any gs path. Empirical
  on Walt-style decks: ~85% of vector content silently dropped on slides whose
  ICC profile isn't plain sRGB. Inherit `/LeaveColorUnchanged`.
- **Smask DPI must match color image DPI.** Lower alpha DPI quantizes at a
  different grid → visible block tearing on transparency edges at zoom.
- **pikepdf is NOT thread-safe for writes.** Only the pure-CPU phase
  (decode → resize → encode) runs in workers. Object writes stay on main.
- **Don't add `/sRGB` even "for testing".** It's a known footgun.
- **The upload is a raw body, never `multipart/form-data`.** `req.formData()`
  materialises the whole part in memory before returning a `File`: measured
  at 4.3x-4.9x the upload size in RSS (a 173 MB PDF grew the server by
  740 MB), which alone exceeds `MemoryMax` on a 2 GB host before compression
  even starts. The client sends the file as the request body and the route
  pipes `req.body` to disk — the same 173 MB upload now costs 26 MB. If you
  ever need a second form field, add a header, not a multipart part.
- **The sweeper must skip in-flight jobs.** A directory's mtime is stamped
  when an entry is *created* inside it, so a job dir is dated from the moment
  `input.pdf` is opened — the start of the upload, not the end. A slow upload
  plus a long compression crosses `JOB_TTL_MS` while Python is still writing.
  `holdJob()` pins the dir for the request's lifetime; the 10-minute promise
  still applies from the moment the job finishes.
- **`BASE_PATH` lives in three places — keep in sync.** `lib/config.ts`,
  `basePath` in `next.config.ts`, and the `location` matcher in the host's
  reverse-proxy config. Set to `""` to mount at the apex.

## Conventions

- **English everywhere.** UI copy, comments, commit messages — no Cyrillic.
- **No emojis.** Anywhere. Code, UI, commits, docs.
- **Single Maximum preset.** Quality/Balance and the Ghostscript codepath were
  removed; do not reintroduce unless explicitly requested.
- **Honest claims in user copy.** Compression varies 1.2×–30× depending on
  input — never promise specific multiples on the landing page.
- **Comments explain WHY, not WHAT.** Especially flags/thresholds with
  empirical justification (a past failure mode, a measurement). Anything that
  reads like a session diary or a pull-request description should be deleted.
- **oxlint, not ESLint.** `eslint-config-next` drags in typescript-eslint,
  which refuses to run under TypeScript 7 (upstream closed that request as
  not planned); oxlint has no dependency on the TS compiler API, which is
  what makes TS 7 usable here at all. Rule parity with the old config was
  verified against planted violations, React Compiler rules included — those
  live in oxlint's `nursery` category and are switched on explicitly as
  `react/react-compiler` in `.oxlintrc.json`. That rule is experimental
  upstream and its diagnostics may shift; if it ever regresses, oxlint can
  load `eslint-plugin-react-hooks` directly through its `jsPlugins` option.

## Auto-tuning

`lib/runtime-limits.ts` reads `os.totalmem()` and `os.availableParallelism()`
at startup and computes:

| host | concurrency | maxBytes |
|------|-------------|----------|
| M-series Mac, 16 GB | 4 | 1 GB |
| Alwyzon XS, 2 GB | 1 | 512 MB |
| Cheap 1 GB VPS | 1 | 256 MB |

`POST /api/compress` also reads `/proc/meminfo` `MemAvailable` per request and
returns `503 BUSY` if the host is memory-tight (Linux only; macOS skips the
check because `os.freemem()` under-reports by an order of magnitude — it
omits reclaimable cache).

`MAX_RAM_BYTES` env var overrides the autotune (useful when Node's container
memory detection misreports).

## Critical files

| File | Why it matters |
|------|----------------|
| `lib/compress.ts` | spawns Python, no-benefit guard, 10-min subprocess timeout |
| `scripts/recompress.py` | the actual compression — pikepdf + mozjpeg + parallel encode + dedup. Falls back to Pillow's libjpeg if mozjpeg's `cjpeg` isn't on PATH |
| `lib/runtime-limits.ts` | host-aware autotune of concurrency and file size cap; per-request memory pressure probe |
| `lib/config.ts` | `BASE_PATH` — keep in sync with `next.config.ts` and the host's reverse proxy |
| `systemd/pdf-comp.service` | service unit — `MemoryMax=1700M`, hardening, `Restart=always`. Installed by `scripts/deploy.sh` |
| `lib/errors.ts` | shared error-code union (server emits + client maps to copy) |
| `app/page.tsx` | server shell — reads LIMITS, hands `maxBytes` to the client to avoid first-paint flash |
| `app/home.tsx` | client state machine: idle → uploading → processing → done/error |
| `app/api/compress/route.ts` | streams `req.body` straight to disk — see the raw-body invariant below |

## Local dev

```bash
brew install qpdf mozjpeg node@24 gnu-time   # gnu-time only for bench/
corepack enable                              # pnpm version comes from package.json
python3 -m venv .venv && .venv/bin/pip install -r scripts/requirements.txt
pnpm install
pnpm dev    # → http://localhost:3000/pdf_comp/
```

**Toolchain versions live in exactly one place each** — `.nvmrc` for the Node
major, `packageManager` in `package.json` for pnpm. `scripts/deploy.sh` and
`scripts/setup.sh` read `.nvmrc` rather than hardcoding a major, and corepack
reads `packageManager`. Bumping either means editing one file; nothing else
needs to be kept in sync.

Direct script use (handy when tuning compression parameters without the web
layer):

```bash
.venv/bin/python scripts/recompress.py input.pdf out.pdf [color_q] [gray_q] [max_long] [workers]
```

## Deploy

```bash
./scripts/deploy.sh root@vps.example.com
```

Native (no Docker) — Node + Python run directly under systemd. Chose this
over the previous containerized layout because `dockerd + containerd` cost
~70 MB idle for zero functional benefit on a single-service host.

What it does (idempotent — safe to re-run as the update flow):
1. Refuses to run unless your local Node major matches what the host runs —
   the build happens here, the artifact runs there, and nothing downstream
   would notice the mismatch. Override with `PDF_COMP_ALLOW_NODE_MISMATCH=1`.
2. Builds Next.js standalone **locally**. The host needs neither pnpm nor
   Next's build toolchain.
3. Installs on the host via apt: nodejs from NodeSource at the `.nvmrc`
   major, qpdf, libjpeg-turbo-progs, python3-venv, rsync.
4. Creates the `pdf-comp` service user + `/opt/pdf-comp/current/`.
5. rsyncs `.next/standalone` + `.next/static` + `public` + `scripts` to
   the host; builds `.venv` there from `scripts/requirements.txt`.
6. Installs `/etc/systemd/system/pdf-comp.service` + `/etc/pdf-comp.env`,
   enables and restarts the service (listens on `127.0.0.1:3127`,
   override with `PDF_COMP_PORT`).
7. Creates a 2 GB swap file if the host has none.
8. Prints a ready-to-paste nginx and Caddy snippet for the host's reverse
   proxy.

We do **not** run a reverse proxy ourselves — the production hosts already
have nginx/Caddy serving the apex. Adding our own would conflict on 80/443.

Requires **cgroup v2** on the host (default on Debian 12+, Ubuntu 22.04+).
`MemoryHigh` is a v2-only feature; on v1 the unit would still boot but
memory throttling would degrade to `MemoryMax` alone.

## Self-healing memory pressure

Four layers, no monitoring required:

1. Pre-check in API: `/proc/meminfo` `MemAvailable` < 500 MB → 503 BUSY.
2. systemd cgroup limits (`MemoryHigh=1400M` throttles/reclaims before
   `MemoryMax=1700M` OOM-kills). Python is usually the target; Node
   sees `SubprocessError`, returns 500 to the user, service stays up.
3. `Restart=always` in the unit — covers the rare case where the OOM
   target is Node itself.
4. journald default rotation (`SystemMaxUse` on the host, typically
   10% of `/var/log`) — prevents log growth from filling disk.

The 1400/1700 pair pins Node (`--max-old-space-size=768`) + Python worst-
case (~800 MB) with a small envelope, and leaves ~300 MB on a 2 GB host
for the OS + reverse proxy. `MALLOC_ARENA_MAX=2` in `/etc/pdf-comp.env`
prevents glibc from handing each libuv/Python worker its own 64 MB arena.

## Privacy guarantees

The user copy on the landing page promises three things; honor them:
- **Private:** no content is read or persisted; tmp dir is wiped on download
  or after 10 min via the sweeper in `lib/job-fs.ts`.
- **Secure:** no analytics, no content logging, no third-party network calls.
  No `console.log` of user content. `Referrer-Policy: no-referrer` set in
  `next.config.ts`.
- **Not indexed:** `app/robots.ts` (Disallow /), `<meta name="robots"
  content="noindex, nofollow, nocache">` via metadata in layout, and
  `X-Robots-Tag: noindex, nofollow, noarchive` HTTP header. Three independent
  layers — keep all three.
