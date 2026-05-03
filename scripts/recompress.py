#!/usr/bin/env python3
"""PDF image-only recompression — preserves vectors, transparency, blend modes.

Pipeline (OCRmyPDF-style, what iLovePDF/Pdftools SDK do):
  1. pikepdf walks every Image XObject in the PDF.
  2. For each image: decode (pikepdf → Pillow bridge → pyvips) → optional
     downsample (lanczos3) → re-encode (mozjpeg's cjpeg if available, else
     libvips' jpegsave).
  3. Replace stream IF new bytes < original. Update /Width /Height to match.
  4. Cross-page deduplication: hash final image streams, redirect every
     reference to identical streams onto a single survivor object.

Concurrency: resize + encode run in a ThreadPoolExecutor. libvips releases
the GIL during pixel ops and the cjpeg subprocess does too, so threads
scale well. Decode and pikepdf object writes stay on the main thread
(pikepdf is not thread-safe for mutation, and PdfImage decode reads
object state).

Pillow is a thin decode bridge only — pikepdf's PdfImage.as_pil_image()
is the sole supported decode API; alternatives (extract_to) raise on
indexed palettes / predictor FlateDecode / custom /Decode arrays, which
Figma exports use heavily. Every operation downstream of the bridge runs
on pyvips.

NEVER touches: vectors, transparency groups, soft masks (their pixel content
is processed via the Image XObject path, but blend modes / opacity / group
structure stays 1:1), fonts, document structure, annotations, OCGs.

CLI: recompress.py <input> <output>
     [color_q=80] [gray_q=92] [max_long=2400] [workers=auto]
"""
import hashlib
import json
import os
import shutil
import subprocess
import sys
import time
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor

import pikepdf
import pyvips
from pikepdf import Pdf, PdfImage, Name, Stream
from PIL import Image  # Decode bridge ONLY — pikepdf hands us a PIL Image.

# libvips caches operation results across calls; in a long batch (every
# Image XObject in a 200-page deck) this holds decoded tiles in RAM. Disable.
pyvips.cache_set_max(0)

# Find mozjpeg's cjpeg (or libjpeg-turbo's cjpeg in PATH) — falls back to
# libvips' JPEG encoder if neither is available.
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


def _pil_to_vips(pil):
    """Bridge pikepdf's Pillow output to pyvips, normalizing mode in transit.

    Returns (vips_image, output_mode) where output_mode is "L" or "RGB".
    Collapses every PIL mode we expect to see from PDF Image XObjects into
    one of those two — alpha channels and palettes flatten to RGB (the
    pre-migration code did the same just before encode).
    """
    if pil.mode == "1":
        pil = pil.convert("L")
    elif pil.mode in ("RGBA", "P", "LA", "CMYK"):
        # CMYK previously collapsed to RGB inside encode_jpeg's cjpeg branch;
        # doing it here unifies the path for both encoder branches.
        pil = pil.convert("RGB")
    bands = 1 if pil.mode == "L" else 3
    interp = "b-w" if bands == 1 else "srgb"
    img = pyvips.Image.new_from_memory(
        pil.tobytes(), pil.width, pil.height, bands, "uchar"
    )
    return img.copy(interpretation=interp), ("L" if bands == 1 else "RGB")


def encode_jpeg(img, mode, quality):
    """Encode vips Image as JPEG; prefer cjpeg, fall back to libvips' jpegsave."""
    if CJPEG:
        marker = b"P5" if mode == "L" else b"P6"
        # write_to_memory returns tightly packed band-interleaved uchar bytes,
        # exactly the layout PNM expects. No stride padding for 8-bit.
        pnm = b"%s\n%d %d\n255\n" % (marker, img.width, img.height) + img.write_to_memory()
        return subprocess.run(
            [CJPEG, "-quality", str(quality), "-optimize", "-progressive"],
            input=pnm, capture_output=True, check=True,
        ).stdout
    return img.jpegsave_buffer(
        Q=quality, optimize_coding=True, interlace=True, strip=True
    )


def _resize_and_encode(work):
    """Worker: resize if oversized, encode JPEG. Pure CPU.

    libvips releases the GIL during pixel ops and the cjpeg subprocess
    does too, so this scales near-linearly in a ThreadPoolExecutor.
    """
    try:
        img = work["img"]
        mode = work["mode"]

        longest = max(img.width, img.height)
        did_downsample = False
        if longest > work["max_long"]:
            img = img.resize(work["max_long"] / longest, kernel="lanczos3")
            did_downsample = True

        return {
            "new_bytes": encode_jpeg(img, mode, work["quality"]),
            "new_w": img.width,
            "new_h": img.height,
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

    untouched = re_encoded = downsampled = errored = 0
    by_hash: "defaultdict[str, list[Stream]]" = defaultdict(list)

    # ---- Phase 1a: walk + decode (sequential, main thread) ----
    # Two early-exits before the expensive decode:
    #   (a) raw-byte hash already seen → this object is a byte-identical
    #       duplicate of one we'll process; route it through dedup directly,
    #       skipping decode/resize/encode. Pays off on multi-page decks where
    #       the same logo/header appears N times.
    #   (b) /Width × /Height from the object dict shows the image is too small
    #       to be worth touching → skip decode entirely.
    pending: list[tuple[Stream, bytes, dict]] = []
    seen_raw: dict[str, Stream] = {}
    aliases: "defaultdict[tuple, list[Stream]]" = defaultdict(list)
    # Untouched-first objects are deferred until aliases are fully collected:
    # an object marked untouched on iteration 5 may pick up aliases on
    # iteration 30, so we record (obj, raw_hash) and finalize after the loop.
    untouched_first: list[tuple[Stream, str]] = []
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
            img, out_mode = _pil_to_vips(pil)
        except Exception:
            errored += 1
            continue

        if img.width * img.height < MIN_PIXELS_TO_RECOMPRESS:
            untouched_first.append((obj, raw_hash))
            untouched += 1
            continue

        is_gray = (out_mode == "L")
        pending.append((obj, current_bytes, {
            "img": img,
            "mode": out_mode,
            "max_long": max_long,
            "quality": gray_q if is_gray else color_q,
        }))

    for obj, raw_hash in untouched_first:
        group = by_hash[raw_hash]
        group.append(obj)
        group.extend(aliases.get(obj.objgen, []))

    # ---- Phase 1b: parallel encode ----
    if pending:
        with ThreadPoolExecutor(max_workers=workers) as pool:
            results = list(pool.map(_resize_and_encode, [w[2] for w in pending]))
    else:
        results = []

    # ---- Phase 1c: apply results (sequential, main thread — pikepdf writes) ----
    for (obj, current_bytes, _), result in zip(pending, results):
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
        "re_encoded": re_encoded,
        "downsampled": downsampled,
        "errored": errored,
        "duplicates_collapsed": duplicates_collapsed,
        "bytes_saved_dedup": bytes_saved_dedup,
        "encoder": "cjpeg" if CJPEG else "vips",
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
