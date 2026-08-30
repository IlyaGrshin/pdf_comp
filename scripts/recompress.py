#!/usr/bin/env python3
"""PDF image-only recompression — preserves vectors, transparency, blend modes.

Pipeline (OCRmyPDF-style, what iLovePDF/Pdftools SDK do):
  1. pikepdf walks every Image XObject in the PDF.
  2. For each image: decode (Pillow) → optional downsample (LANCZOS) →
     re-encode (mozjpeg's cjpeg if available, else Pillow JPEG).
  3. Replace stream IF new bytes < original. Update /Width /Height to match.
  4. Cross-page deduplication: hash final image streams, redirect every
     reference to identical streams onto a single survivor object.

Concurrency: resize + encode run in a ThreadPoolExecutor. Both Pillow's C
extensions and the cjpeg subprocess release the GIL, so threads scale well.
Decode and pikepdf object writes stay on the main thread (pikepdf is not
thread-safe for mutation, and PdfImage decode reads object state).

NEVER touches: vectors, transparency groups, soft masks (their pixel content
is processed via the Image XObject path, but blend modes / opacity / group
structure stays 1:1), fonts, document structure, annotations, OCGs.

CLI: recompress.py <input> <output>
     [color_q=80] [gray_q=92] [max_long=2400] [workers=auto]
"""
import hashlib
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from collections import defaultdict, deque
from concurrent.futures import Future, ThreadPoolExecutor

import pikepdf
from pikepdf import Pdf, PdfImage, Name, Stream
from PIL import Image

# Find mozjpeg's cjpeg (or libjpeg-turbo's cjpeg in PATH) — falls back to
# Pillow's JPEG encoder if neither is available.
MOZJPEG_CANDIDATES = [
    "/opt/homebrew/opt/mozjpeg/bin/cjpeg",
    "/usr/local/opt/mozjpeg/bin/cjpeg",
    "/opt/mozjpeg/bin/cjpeg",
    "/usr/bin/mozcjpeg",
    shutil.which("cjpeg") or "",
]
CJPEG = next((p for p in MOZJPEG_CANDIDATES if p and os.path.isfile(p)), None)

# Pixel-area threshold below which JPEG header overhead + dict bloat exceed
# any savings from re-encoding. ~100×100 — covers logos, icons, sprite tiles.
MIN_PIXELS_TO_RECOMPRESS = 10_000

# Ceiling on the decoded bitmaps allowed in flight at once. Everything below
# is expressed against it so the two bounds cannot drift apart.
INFLIGHT_BUDGET_BYTES = 256 * 1024 * 1024

# Upper pixel bound, the mirror of MIN_PIXELS_TO_RECOMPRESS: above this an
# image is left untouched instead of decoded. The bound exists because the
# decode allocates from the *declared* dimensions before anything can measure
# it — Pillow's own MAX_IMAGE_PIXELS guard does not fire on the path pikepdf
# uses for non-JPEG images (`Image.frombuffer`), so a 1.1 MB PDF declaring one
# 20000x20000 Flate image drove peak RSS to 2701 MB with every other bound in
# this file still holding. Worst case a work item costs 7 bytes per pixel
# (RGBA source plus the RGB copy — see _bitmap_bytes), so this keeps a single
# image inside the whole in-flight budget. ~38 Mpx also clears real content
# with room to spare: 600 DPI A4 is 35 Mpx, a 4x full-bleed 1920x1080 Figma
# slide is 33 Mpx.
MAX_DECODE_PIXELS = INFLIGHT_BUDGET_BYTES // 7


def encode_jpeg(pil, quality):
    """Encode PIL image as JPEG; prefer cjpeg, fall back to Pillow."""
    if CJPEG:
        if pil.mode != "L" and pil.mode != "RGB":
            pil = pil.convert("RGB")
        # Hand cjpeg a file rather than piping the PNM to its stdin.
        # subprocess.run(input=...) goes through communicate(), which feeds the
        # pipe in select.PIPE_BUF-sized slices under a selector loop — 512 B on
        # macOS, 4096 B on Linux. A 2400x1800 RGB frame is ~12 MB, i.e. tens of
        # thousands of write+poll syscalls per image, and the profile showed
        # that plumbing costing more than pikepdf's whole save step. Output
        # stays on the pipe: the encoded JPEG is small enough not to matter.
        # TemporaryFile, not NamedTemporaryFile: on POSIX it is unlinked the
        # instant it is created (or opened with O_TMPFILE), so the decoded
        # bitmap has no directory entry for anything to find. That matters
        # because this is user content — a named temp under /tmp would sit
        # outside the job dir, i.e. outside both deleteJob() and the sweeper,
        # and a SIGKILL from the compress.ts timeout or the cgroup OOM killer
        # skips any cleanup we could write. Here the kernel reclaims the
        # blocks when the fd dies with the process, kill signal or not.
        #
        # Pillow writes the PNM straight into the fd rather than us building
        # it: `header + pil.tobytes()` had the bitmap, the bytes object and
        # the concatenation result all live at once — about 9 bytes per pixel
        # where the caller's budget reserved 6, and 9 against 4 for a palette
        # image. Sixty 2400x1800 images at eight workers peaked at 389 MB
        # (494 MB palette) against a 256 MB budget. Pillow's writer chunks
        # through a 64 KB buffer, and its output is byte-identical to the
        # hand-built header plus tobytes() for both P5 and P6.
        with tempfile.TemporaryFile() as tf:
            pil.save(tf, format="PPM")
            tf.seek(0)
            return subprocess.run(
                [CJPEG, "-quality", str(quality), "-optimize", "-progressive"],
                stdin=tf, capture_output=True, check=True,
            ).stdout
    if pil.mode not in ("RGB", "L"):
        pil = pil.convert("RGB")
    buf = io.BytesIO()
    pil.save(buf, format="JPEG", quality=quality, optimize=True, progressive=True)
    return buf.getvalue()


def _bitmap_bytes(pil):
    """Upper bound on the bytes one work item holds at its peak.

    The source bitmap plus the converted copy that lives alongside it while
    convert() runs. Counting the source alone under-budgets palette images
    threefold — mode "P" is one byte per pixel until the worker turns it into
    three-byte RGB. Nothing downstream exceeds this: both encoders stream into
    their destination rather than materialising another full-size buffer.
    Reads only .mode/.width/.height, so a lazily-opened JPEG stays lazy here.
    """
    src = len(pil.getbands())
    dst = 1 if pil.mode in ("1", "L") else 3
    return pil.width * pil.height * (src + dst)


def _resize_and_encode(work):
    """Worker: convert mode, resize if oversized, encode JPEG. Pure CPU.

    Pillow's C extensions and the cjpeg subprocess both release the GIL,
    so this scales near-linearly in a ThreadPoolExecutor.
    """
    try:
        # pop, not [] — the dict is the last reference to the source bitmap,
        # and holding it here would keep the source alive alongside every
        # copy convert()/resize() makes, doubling what the caller budgeted.
        pil = work.pop("pil")
        mode = pil.mode
        if mode == "1":
            pil = pil.convert("L")
            mode = "L"
        elif mode in ("RGBA", "P", "LA"):
            pil = pil.convert("RGB")
            mode = "RGB"

        longest = max(pil.width, pil.height)
        did_downsample = False
        if longest > work["max_long"]:
            ratio = work["max_long"] / longest
            pil = pil.resize(
                (int(pil.width * ratio), int(pil.height * ratio)),
                Image.LANCZOS,
            )
            did_downsample = True

        return {
            "new_bytes": encode_jpeg(pil, work["quality"]),
            "new_w": pil.width,
            "new_h": pil.height,
            "mode": mode,
            "did_downsample": did_downsample,
        }
    except Exception:
        return {"error": True}


def _auto_workers():
    return min(os.cpu_count() or 2, 8)


def recompress_pdf(input_path, output_path,
                   color_q=80, gray_q=92, max_long=2400, workers=None):
    if workers is None or workers <= 0:
        workers = _auto_workers()

    t0 = time.time()
    pdf = Pdf.open(input_path)

    untouched = re_encoded = downsampled = errored = skipped_oversized = 0
    by_hash: "defaultdict[str, list[Stream]]" = defaultdict(list)

    # ---- Phase 1a: walk, decode, and encode within a bounded window ----
    # Two early-exits before the expensive decode:
    #   (a) raw-byte hash already seen → this object is a byte-identical
    #       duplicate of one we'll process; route it through dedup directly,
    #       skipping decode/resize/encode. Pays off on multi-page decks where
    #       the same logo/header appears N times.
    #   (b) /Width × /Height from the object dict shows the image is too small
    #       to be worth touching → skip decode entirely.
    #
    # Each decoded image is handed to the pool immediately and the bitmap is
    # dropped as soon as its encode returns, so only `window` of them exist at
    # once. Accumulating every bitmap first and encoding afterwards peaked at
    # 957 MB of RSS on a 9.3 MB Figma export (298 images, 477 MB of raw
    # bitmaps) — the ceiling that keeps concurrency at 1 on a 2 GB host, since
    # MemoryMax there is 1700 MB. What survives the window is the *encoded*
    # bytes, which are two orders of magnitude smaller.
    #
    # The results cannot be applied here: an object decoded early can still
    # pick up aliases late in the walk, and phase 1c needs the complete alias
    # list. Only the bitmaps are freed early, not the bookkeeping.
    done: list[tuple[Stream, bytes, dict]] = []
    inflight: "deque[tuple[Stream, bytes, Future, int]]" = deque()
    inflight_bytes = 0
    # Two per worker keeps the pool fed while the main thread decodes the next
    # image. A count alone is not a memory bound, though: figma-1.pdf contains
    # 3884x2590 RGBA images at ~40 MB each, so sixteen of those in flight is
    # 640 MB. The byte budget is what holds the ceiling; the count is only
    # there to stop thousands of thumbnails from queueing up.
    window = max(2, workers * 2)
    seen_raw: dict[str, Stream] = {}
    aliases: "defaultdict[tuple, list[Stream]]" = defaultdict(list)
    # Untouched-first objects are deferred until aliases are fully collected:
    # an object marked untouched on iteration 5 may pick up aliases on
    # iteration 30, so we record (obj, raw_hash) and finalize after the loop.
    untouched_first: list[tuple[Stream, str]] = []

    def drain(max_items, max_bytes):
        """Collect finished encodes until the window fits both bounds."""
        nonlocal inflight_bytes
        while inflight and (len(inflight) > max_items or inflight_bytes > max_bytes):
            o, cb, fut, nbytes = inflight.popleft()
            done.append((o, cb, fut.result()))
            inflight_bytes -= nbytes

    # Map each soft mask back to the image carrying it. The oversize cap has
    # to apply to the pair, not to each stream on its own: /SMask is a
    # separate Image XObject, so skipping an over-cap colour image while its
    # mask still goes through the resize path (or the reverse) would leave the
    # two sampled on different grids — exactly the tearing the smask-DPI
    # invariant exists to prevent.
    def _declared_pixels(obj):
        try:
            return int(obj.get("/Width", 0) or 0) * int(obj.get("/Height", 0) or 0)
        except Exception:
            return 0

    # Largest colour image each soft mask is attached to. One /SMask stream may
    # be shared by several XObjects, so this keeps the biggest rather than
    # whichever came last: the mask has to be skipped if *any* image it masks
    # is over the cap, or that image keeps full resolution while its alpha is
    # resampled underneath it.
    smask_parent_pixels: dict[tuple, int] = {}
    for obj in pdf.objects:
        if not isinstance(obj, Stream) or obj.get("/Subtype") != Name.Image:
            continue
        smask = obj.get("/SMask")
        if isinstance(smask, Stream):
            px = _declared_pixels(obj)
            if px > smask_parent_pixels.get(smask.objgen, 0):
                smask_parent_pixels[smask.objgen] = px

    def _pair_oversized(obj):
        """True if this image, or any stream it is paired with, is over the cap."""
        if _declared_pixels(obj) > MAX_DECODE_PIXELS:
            return True
        smask = obj.get("/SMask")
        if isinstance(smask, Stream) and _declared_pixels(smask) > MAX_DECODE_PIXELS:
            return True
        return smask_parent_pixels.get(obj.objgen, 0) > MAX_DECODE_PIXELS

    pool = ThreadPoolExecutor(max_workers=workers)
    try:
        for obj in pdf.objects:
            if not isinstance(obj, Stream):
                continue
            if obj.get("/Subtype") != Name.Image:
                continue
            try:
                current_bytes = obj.read_raw_bytes()
            except Exception:
                errored += 1
                continue

            raw_hash = hashlib.sha256(current_bytes).hexdigest()

            # Ahead of the alias fast-path, not after it: aliasing merges two
            # streams by their bytes alone, and two byte-identical soft masks
            # can hang off differently sized colour images. Deciding later
            # would let the second mask inherit the first one's resized bytes
            # while its own oversized image stayed untouched — the grid
            # mismatch this cap exists to avoid, rebuilt through the alias.
            # Keeping oversized streams out of `seen_raw` entirely means an
            # alias group is always uniformly processable. They still dedup:
            # untouched_first groups them by raw hash for phase 2.
            #
            # Before the decode, too, because the decode is what allocates:
            # pikepdf builds non-JPEG images with Image.frombuffer, which sizes
            # its buffer from the declared dimensions and never consults
            # MAX_IMAGE_PIXELS.
            if _pair_oversized(obj):
                untouched_first.append((obj, raw_hash))
                skipped_oversized += 1
                continue

            first = seen_raw.get(raw_hash)
            if first is not None:
                aliases[first.objgen].append(obj)
                continue
            seen_raw[raw_hash] = obj

            try:
                w = int(obj.get("/Width", 0) or 0)
                h = int(obj.get("/Height", 0) or 0)
            except Exception:
                w = h = 0
            if w > 0 and h > 0 and w * h < MIN_PIXELS_TO_RECOMPRESS:
                untouched_first.append((obj, raw_hash))
                untouched += 1
                continue

            try:
                pil = PdfImage(obj).as_pil_image()
            except Exception:
                errored += 1
                continue

            if pil.width * pil.height < MIN_PIXELS_TO_RECOMPRESS:
                untouched_first.append((obj, raw_hash))
                untouched += 1
                continue
            # Again, now against the decoder's own idea of the size. A JPEG
            # carries its dimensions in the SOF marker, so /Width and /Height can
            # understate it; Image.open() is lazy, so nothing is allocated yet and
            # the bitmap only appears once a worker touches it. This backstop is
            # about memory alone and cannot pair with a soft mask the way the
            # check above does — establishing the partner's true size would mean
            # decoding it, which is the allocation being avoided. A file whose
            # dict understates its own images is malformed either way; the pair
            # rule holds for every PDF that declares its sizes honestly.
            if pil.width * pil.height > MAX_DECODE_PIXELS:
                untouched_first.append((obj, raw_hash))
                skipped_oversized += 1
                del pil
                continue

            is_gray = pil.mode in ("L", "1")
            bitmap_bytes = _bitmap_bytes(pil)
            # Block here rather than before the decode: `pil` already exists, so
            # waiting first would just hold one extra bitmap while we wait. Leaving
            # room for it means draining to one under each bound.
            drain(window - 1, max(0, INFLIGHT_BUDGET_BYTES - bitmap_bytes))
            inflight.append((obj, current_bytes, pool.submit(_resize_and_encode, {
                "pil": pil,
                "max_long": max_long,
                "quality": gray_q if is_gray else color_q,
            }), bitmap_bytes))
            inflight_bytes += bitmap_bytes
            # Drop the main thread's handle; the worker's dict is the last
            # reference and it dies with the task.
            del pil

        drain(0, 0)
    finally:
        # cancel_futures so an exception escaping the walk does not leave
        # the interpreter's atexit hook joining a queue of encodes nobody
        # will read.
        pool.shutdown(wait=True, cancel_futures=True)

    for obj, raw_hash in untouched_first:
        group = by_hash[raw_hash]
        group.append(obj)
        group.extend(aliases.get(obj.objgen, []))

    # ---- Phase 1c: apply results (sequential, main thread — pikepdf writes) ----
    for obj, current_bytes, result in done:
        obj_aliases = aliases.get(obj.objgen, [])
        if result.get("error"):
            errored += 1
            continue
        new_bytes = result["new_bytes"]
        if len(new_bytes) >= len(current_bytes):
            group = by_hash[hashlib.sha256(current_bytes).hexdigest()]
            group.append(obj)
            group.extend(obj_aliases)
            untouched += 1
            continue

        color_space = Name.DeviceGray if result["mode"] == "L" else Name.DeviceRGB
        new_w = result["new_w"]
        new_h = result["new_h"]

        def _apply(stream):
            stream.write(new_bytes, filter=Name.DCTDecode)
            stream["/ColorSpace"] = color_space
            stream["/BitsPerComponent"] = 8
            # PDF viewers read /Width and /Height from the dict, not from the
            # JPEG SOF markers. After resize, both must be updated — otherwise
            # the viewer stretches a shrunken bitmap.
            stream["/Width"] = new_w
            stream["/Height"] = new_h
            if "/DecodeParms" in stream:
                del stream["/DecodeParms"]

        _apply(obj)
        # Mirror the recompressed bytes onto aliases. Phase 2 dedup will
        # redirect references to the survivor and orphan the aliases — but
        # pikepdf still serializes orphan streams. Without this mirror they
        # would carry their ORIGINAL raw bytes (the very thing pre-decode
        # dedup tried to skip recompressing), and the output bloats by
        # `n_aliases × original_size`. Walt.pdf went 5 MB → 75 MB on this
        # path before the fix.
        for alias in obj_aliases:
            _apply(alias)

        group = by_hash[hashlib.sha256(new_bytes).hexdigest()]
        group.append(obj)
        group.extend(obj_aliases)
        if result["did_downsample"]:
            downsampled += 1
        else:
            re_encoded += 1

    # ---- Phase 2: cross-page dedup ----
    # Index each page's XObject references once, then collapse duplicates.
    # O(pages × xobjects + duplicates) instead of O(victims × pages × xobjects).
    refs_by_objgen: "defaultdict[tuple, list[tuple]]" = defaultdict(list)
    for page in pdf.pages:
        if Name.Resources not in page:
            continue
        resources = page[Name.Resources]
        if Name.XObject not in resources:
            continue
        xobjects = resources[Name.XObject]
        for key, val in xobjects.items():
            refs_by_objgen[val.objgen].append((xobjects, key))

    duplicates_collapsed = 0
    bytes_saved_dedup = 0
    for group in by_hash.values():
        if len(group) < 2:
            continue
        survivor = group[0]
        for victim in group[1:]:
            try:
                bytes_saved_dedup += len(victim.read_raw_bytes())
                for xobjects, key in refs_by_objgen.get(victim.objgen, []):
                    xobjects[key] = survivor
                duplicates_collapsed += 1
            except Exception:
                continue

    pdf.save(
        output_path,
        compress_streams=True,
        object_stream_mode=pikepdf.ObjectStreamMode.generate,
        linearize=True,
    )

    return {
        "untouched": untouched,
        "skipped_oversized": skipped_oversized,
        "re_encoded": re_encoded,
        "downsampled": downsampled,
        "errored": errored,
        "duplicates_collapsed": duplicates_collapsed,
        "bytes_saved_dedup": bytes_saved_dedup,
        "encoder": "cjpeg" if CJPEG else "pillow",
        "workers": workers,
        "elapsed_s": round(time.time() - t0, 2),
    }


if __name__ == "__main__":
    if len(sys.argv) < 3:
        print(
            "usage: recompress.py <input> <output> "
            "[color_q=80] [gray_q=92] [max_long=2400] [workers=auto]",
            file=sys.stderr,
        )
        sys.exit(2)
    cq = int(sys.argv[3]) if len(sys.argv) > 3 else 80
    gq = int(sys.argv[4]) if len(sys.argv) > 4 else 92
    ml = int(sys.argv[5]) if len(sys.argv) > 5 else 2400
    wk = int(sys.argv[6]) if len(sys.argv) > 6 else 0
    print(json.dumps(recompress_pdf(sys.argv[1], sys.argv[2], cq, gq, ml, wk)))
