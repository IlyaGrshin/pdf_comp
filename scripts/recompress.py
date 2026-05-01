#!/usr/bin/env python3
"""PDF image-only recompression — preserves vectors, transparency, blend modes.

Pipeline (OCRmyPDF-style, what iLovePDF/Pdftools SDK do):
  1. pikepdf walks every Image XObject in the PDF.
  2. For each image: decode (Pillow) → optional downsample (LANCZOS) →
     re-encode (mozjpeg's cjpeg if available, else Pillow JPEG).
  3. Replace stream IF new bytes < original. Update /Width /Height to match.
  4. Cross-page deduplication: hash final image streams, redirect every
     reference to identical streams onto a single survivor object (5× copies
     of the same template image → 1 shared XObject).

NEVER touches: vectors, transparency groups, soft masks (their pixel content
is processed via the Image XObject path, but blend modes / opacity / group
structure stays 1:1), fonts, document structure, annotations, OCGs.

CLI: recompress.py <input> <output> [color_q=80] [gray_q=92] [max_long=2400]
"""
import hashlib
import io
import json
import os
import shutil
import subprocess
import sys
import time
from collections import defaultdict

import pikepdf
from pikepdf import Pdf, PdfImage, Name, Stream
from PIL import Image

# Find mozjpeg's cjpeg (or any cjpeg in PATH from libjpeg-turbo) — falls back
# to Pillow's JPEG encoder if neither is available.
MOZJPEG_CANDIDATES = [
    "/opt/homebrew/opt/mozjpeg/bin/cjpeg",
    "/usr/local/opt/mozjpeg/bin/cjpeg",
    "/usr/bin/mozcjpeg",
    shutil.which("cjpeg") or "",
]
CJPEG = next((p for p in MOZJPEG_CANDIDATES if p and os.path.isfile(p)), None)


def encode_jpeg(pil, quality):
    """Encode PIL image as JPEG; prefer cjpeg, fall back to Pillow."""
    if CJPEG:
        if pil.mode == "L":
            marker = b"P5"
        else:
            marker = b"P6"
            if pil.mode != "RGB":
                pil = pil.convert("RGB")
        w, h = pil.size
        pnm = b"%s\n%d %d\n255\n" % (marker, w, h) + pil.tobytes()
        return subprocess.run(
            [CJPEG, "-quality", str(quality), "-optimize", "-progressive"],
            input=pnm, capture_output=True, check=True,
        ).stdout
    if pil.mode not in ("RGB", "L"):
        pil = pil.convert("RGB")
    buf = io.BytesIO()
    pil.save(buf, format="JPEG", quality=quality, optimize=True, progressive=True)
    return buf.getvalue()


def recompress_pdf(input_path, output_path, color_q=80, gray_q=92, max_long=2400):
    t0 = time.time()
    pdf = Pdf.open(input_path)

    untouched = re_encoded = downsampled = errored = 0
    by_hash: "defaultdict[str, list[Stream]]" = defaultdict(list)

    # Single pass: per-image recompress + collect content hashes for dedup.
    for obj in pdf.objects:
        if not isinstance(obj, Stream):
            continue
        if obj.get("/Subtype") != Name.Image:
            continue
        try:
            current_bytes = obj.read_raw_bytes()
            pdfimg = PdfImage(obj)
            pil = pdfimg.as_pil_image()

            if pil.width * pil.height < 10000:
                # Tiny icons — keep original, but still hash for dedup.
                by_hash[hashlib.sha256(current_bytes).hexdigest()].append(obj)
                untouched += 1
                continue

            mode = pil.mode
            is_gray = mode in ("L", "1")
            quality = gray_q if is_gray else color_q

            if mode == "1":
                pil = pil.convert("L")
                mode = "L"
            elif mode in ("RGBA", "P", "LA"):
                pil = pil.convert("RGB")
                mode = "RGB"

            longest = max(pil.width, pil.height)
            did_downsample = False
            if longest > max_long:
                ratio = max_long / longest
                pil = pil.resize(
                    (int(pil.width * ratio), int(pil.height * ratio)),
                    Image.LANCZOS,
                )
                did_downsample = True

            jb = encode_jpeg(pil, quality)

            if len(jb) >= len(current_bytes):
                by_hash[hashlib.sha256(current_bytes).hexdigest()].append(obj)
                untouched += 1
                continue

            obj.write(jb, filter=Name.DCTDecode)
            obj["/ColorSpace"] = Name.DeviceGray if mode == "L" else Name.DeviceRGB
            obj["/BitsPerComponent"] = 8
            # PDF viewers read /Width and /Height from the dict, not from the
            # JPEG SOF markers. After resize, both must be updated — otherwise
            # the viewer stretches a shrunken bitmap.
            obj["/Width"] = pil.width
            obj["/Height"] = pil.height
            if "/DecodeParms" in obj:
                del obj["/DecodeParms"]

            by_hash[hashlib.sha256(jb).hexdigest()].append(obj)
            if did_downsample:
                downsampled += 1
            else:
                re_encoded += 1
        except Exception:
            errored += 1
            continue

    # Index every page's XObject references once, then collapse duplicates.
    # O(pages × xobjects + duplicates) instead of nested O(victims × pages × xobjects).
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
        "encoder": "cjpeg" if CJPEG else "pillow",
        "elapsed_s": round(time.time() - t0, 2),
    }


if __name__ == "__main__":
    if len(sys.argv) < 3:
        print(
            "usage: recompress.py <input> <output> [color_q=80] [gray_q=92] [max_long=2400]",
            file=sys.stderr,
        )
        sys.exit(2)
    cq = int(sys.argv[3]) if len(sys.argv) > 3 else 80
    gq = int(sys.argv[4]) if len(sys.argv) > 4 else 92
    ml = int(sys.argv[5]) if len(sys.argv) > 5 else 2400
    print(json.dumps(recompress_pdf(sys.argv[1], sys.argv[2], cq, gq, ml)))
