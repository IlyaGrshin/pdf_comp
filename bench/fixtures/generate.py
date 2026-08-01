#!/usr/bin/env python3
"""Deterministic synthetic PDFs for bench/run.py.

Each fixture targets a specific compression archetype. Output goes to
bench/fixtures/out/ — regeneration is idempotent; existing files are skipped
unless --force is passed.

Archetypes:
  vector_only   text + paths only; the no-benefit guard should hold.
  single_image  one 4000x3000 photo on one page; biggest savings.
  multi_image   8 pages with distinct photos; stresses parallel encode.
  dedup_heavy   16 pages all referencing the same photo; tests cross-page dedup.
  softmask      RGBA images on white pages; exercises the soft-mask path.
  tiny_images   many <100x100 thumbnails; tests the skip-small short-circuit.
  mixed         text + vectors + medium photos; closest to real-world decks.
"""

import argparse
import io
import zlib
from pathlib import Path

import numpy as np
import pikepdf
from PIL import Image
from reportlab.lib.pagesizes import A4
from reportlab.lib.utils import ImageReader
from reportlab.pdfgen import canvas


def photo_like(w: int, h: int, seed: int) -> Image.Image:
    """Multi-octave noise — JPEG-friendly entropy without a real photo.

    Pure random noise compresses badly under JPEG (no spatial correlation),
    so layered low-frequency noise stands in for photo statistics: smooth
    regions, gradients, and edges roughly in the band JPEG handles well.
    """
    rng = np.random.default_rng(seed)
    out = np.zeros((h, w, 3), dtype=np.float32)
    for octave in range(6):
        scale = max(1, 2 ** (5 - octave))
        small_h = max(2, h // scale)
        small_w = max(2, w // scale)
        small = (rng.random((small_h, small_w, 3), dtype=np.float32) * 255).astype(np.uint8)
        big = Image.fromarray(small).resize((w, h), Image.BILINEAR)
        out += np.asarray(big, dtype=np.float32) / (octave + 1)
    out = np.clip(out / out.max() * 255, 0, 255).astype(np.uint8)
    return Image.fromarray(out)


def jpeg_reader(img: Image.Image, quality: int = 92) -> ImageReader:
    buf = io.BytesIO()
    if img.mode != "RGB":
        img = img.convert("RGB")
    img.save(buf, "JPEG", quality=quality)
    buf.seek(0)
    return ImageReader(buf)


def png_reader(img: Image.Image) -> ImageReader:
    buf = io.BytesIO()
    img.save(buf, "PNG")
    buf.seek(0)
    return ImageReader(buf)


def make_vector_only(out_path: Path) -> None:
    c = canvas.Canvas(str(out_path), pagesize=A4)
    w, h = A4
    for page in range(3):
        c.setFont("Helvetica", 12)
        for line in range(50):
            c.drawString(36, h - 60 - line * 14, f"Page {page} line {line:02d} — lorem ipsum dolor sit amet")
        c.setStrokeColorRGB(0.2, 0.4, 0.8)
        for i in range(80):
            x = 36 + (i * 6) % (w - 72)
            c.line(x, 200, x + 30, 400)
        c.setFillColorRGB(0.9, 0.3, 0.2)
        for i in range(40):
            c.circle(60 + i * 12, 120, 5, fill=1)
        c.showPage()
    c.save()


def make_single_image(out_path: Path) -> None:
    img = photo_like(4000, 3000, seed=1)
    reader = jpeg_reader(img, quality=95)
    c = canvas.Canvas(str(out_path), pagesize=A4)
    w, h = A4
    c.drawImage(reader, 0, 0, width=w, height=h)
    c.save()


def make_multi_image(out_path: Path, n: int = 8) -> None:
    c = canvas.Canvas(str(out_path), pagesize=A4)
    w, h = A4
    for i in range(n):
        img = photo_like(2400, 1800, seed=100 + i)
        c.drawImage(jpeg_reader(img, quality=95), 0, 0, width=w, height=h)
        c.showPage()
    c.save()


def make_dedup_heavy(out_path: Path, n: int = 16) -> None:
    # ReportLab dedupes ImageReader instances, so naively calling drawImage in
    # a loop produces one shared XObject — the recompressor would have nothing
    # to dedup. We want N distinct XObjects with identical raw bytes to
    # exercise the alias-mirror code path (the regression that took Walt.pdf
    # from 5 MB to 75 MB; see scripts/recompress.py).
    img = photo_like(2400, 1800, seed=7)
    buf = io.BytesIO()
    img.convert("RGB").save(buf, "JPEG", quality=95)
    img_bytes = buf.getvalue()

    pdf = pikepdf.Pdf.new()
    w_pt, h_pt = A4
    content = f"q\n{w_pt} 0 0 {h_pt} 0 0 cm\n/Img Do\nQ\n".encode()

    for _ in range(n):
        img_obj = pdf.make_stream(img_bytes)
        img_obj["/Type"] = pikepdf.Name.XObject
        img_obj["/Subtype"] = pikepdf.Name.Image
        img_obj["/Width"] = img.width
        img_obj["/Height"] = img.height
        img_obj["/ColorSpace"] = pikepdf.Name.DeviceRGB
        img_obj["/BitsPerComponent"] = 8
        img_obj["/Filter"] = pikepdf.Name.DCTDecode

        page = pdf.add_blank_page(page_size=A4)
        page.Resources = pikepdf.Dictionary(
            XObject=pikepdf.Dictionary(Img=img_obj),
        )
        page.Contents = pdf.make_stream(content)

    pdf.save(str(out_path))


def make_jpx(out_path: Path, n: int = 6) -> None:
    """JPEG 2000 plus a soft mask — what real Figma exports are actually made of.

    Every other fixture here is DCTDecode or FlateDecode without an /SMask,
    and for those pikepdf hands back a lazy PIL handle: the pixels get decoded
    later, inside the worker pool, so the cost is already parallel. This shape
    behaves differently. pikepdf has to merge the mask into an alpha channel
    to produce one RGBA image, which forces both the JPX codestream and the
    Flate mask to be decoded eagerly — on the main thread, in phase 1a.

    That is the dominant cost of the workload this service exists for: on
    figma-1.pdf it is 148 such images and ~7.4s of a ~10s run, while its 150
    plain FlateDecode images cost 0.16s between them. Nothing in bench/
    exercised it, so the gate was blind to both regressions and wins on that
    path.

    The /SMask is the load-bearing part, not the filter: strip it and
    as_pil_image() goes back to returning a lazy handle for JPX as well,
    0.03s for six images against 4.57s with the mask attached. A fixture
    without one measures nothing.

    Built through pikepdf rather than reportlab: reportlab re-encodes whatever
    it is handed, and the point is to get a genuine JPX codestream into the
    file untouched.
    """
    pdf = pikepdf.Pdf.new()
    w_pt, h_pt = A4
    content = f"q\n{w_pt} 0 0 {h_pt} 0 0 cm\n/Img Do\nQ\n".encode()
    w, h = 2400, 1800

    yy, xx = np.mgrid[0:h, 0:w].astype(np.float32)

    for i in range(n):
        img = photo_like(w, h, seed=400 + i)
        # Radial falloff, same idea as make_softmask, but the centre moves per
        # page. Identical masks would collapse into one under cross-page dedup
        # and this fixture would stop measuring what it is here to measure.
        cx = w * (0.3 + 0.4 * i / max(1, n - 1))
        cy = h * (0.7 - 0.4 * i / max(1, n - 1))
        d = np.sqrt((xx - cx) ** 2 + (yy - cy) ** 2)
        alpha = np.clip(255.0 - (d / d.max()) * 255.0 * 1.4, 0, 255).astype(np.uint8)

        buf = io.BytesIO()
        # codec="jp2" writes the JP2 container. A bare "j2k" codestream is also
        # legal under /JPXDecode, but the bench needs pdftoppm to rasterise the
        # page for SSIM and poppler is happier with the box format.
        #
        # 5:1 rather than something aggressive: at 20:1 the JPX source came out
        # smaller than the JPEG the recompressor produces, so every image hit
        # the "re-encode didn't help" branch and landed as untouched. The
        # fixture still exercised the decode, but `ratio` could never move and
        # a compression regression would have been invisible in it.
        img.convert("RGB").save(
            buf, "JPEG2000", codec="jp2", quality_mode="rates", quality_layers=[5]
        )

        img_obj = pdf.make_stream(buf.getvalue())
        img_obj["/Type"] = pikepdf.Name.XObject
        img_obj["/Subtype"] = pikepdf.Name.Image
        img_obj["/Width"] = w
        img_obj["/Height"] = h
        img_obj["/ColorSpace"] = pikepdf.Name.DeviceRGB
        img_obj["/BitsPerComponent"] = 8
        img_obj["/Filter"] = pikepdf.Name.JPXDecode

        # Mask at the same dimensions as the colour image. AGENTS.md is
        # explicit that a lower-resolution alpha quantises on a different grid
        # and tears at zoom; matching here keeps the fixture honest about what
        # production data looks like.
        smask = pdf.make_stream(zlib.compress(alpha.tobytes()))
        smask["/Type"] = pikepdf.Name.XObject
        smask["/Subtype"] = pikepdf.Name.Image
        smask["/Width"] = w
        smask["/Height"] = h
        smask["/ColorSpace"] = pikepdf.Name.DeviceGray
        smask["/BitsPerComponent"] = 8
        smask["/Filter"] = pikepdf.Name.FlateDecode
        img_obj["/SMask"] = smask

        page = pdf.add_blank_page(page_size=A4)
        page.Resources = pikepdf.Dictionary(
            XObject=pikepdf.Dictionary(Img=img_obj),
        )
        page.Contents = pdf.make_stream(content)

    pdf.save(str(out_path))


def make_softmask(out_path: Path, n: int = 4) -> None:
    c = canvas.Canvas(str(out_path), pagesize=A4)
    w, h = A4
    for i in range(n):
        rgb = np.asarray(photo_like(2000, 1500, seed=200 + i))
        yy, xx = np.mgrid[0:1500, 0:2000].astype(np.float32)
        cx, cy = 1000.0, 750.0
        d = np.sqrt((xx - cx) ** 2 + (yy - cy) ** 2)
        alpha = np.clip(255.0 - (d / d.max()) * 255.0 * 1.4, 0, 255).astype(np.uint8)
        rgba = np.dstack([rgb, alpha])
        img = Image.fromarray(rgba, "RGBA")
        c.drawImage(png_reader(img), 0, 0, width=w, height=h, mask="auto")
        c.showPage()
    c.save()


def make_tiny_images(out_path: Path, n_per_page: int = 40, n_pages: int = 3) -> None:
    c = canvas.Canvas(str(out_path), pagesize=A4)
    w, h = A4
    cell = 70
    for page in range(n_pages):
        for i in range(n_per_page):
            img = photo_like(64, 64, seed=300 + page * 100 + i)
            x = 36 + (i % 8) * (cell + 4)
            y = h - 72 - (i // 8) * (cell + 4)
            c.drawImage(jpeg_reader(img, quality=85), x, y - cell, width=cell, height=cell)
        c.showPage()
    c.save()


def make_mixed(out_path: Path) -> None:
    c = canvas.Canvas(str(out_path), pagesize=A4)
    w, h = A4
    for page in range(4):
        c.setFont("Helvetica-Bold", 24)
        c.drawString(36, h - 60, f"Mixed content page {page + 1}")
        c.setFont("Helvetica", 11)
        for line in range(20):
            c.drawString(36, h - 100 - line * 14, f"Body line {line} on page {page}")
        c.setFillColorRGB(0.9, 0.2, 0.3)
        for i in range(20):
            c.circle(80 + i * 22, 200, 8, fill=1)
        img = photo_like(1600, 1200, seed=400 + page)
        c.drawImage(jpeg_reader(img, quality=90), 36, 300, width=w - 72, height=300)
        c.showPage()
    c.save()


FIXTURES = {
    "vector_only": make_vector_only,
    "single_image": make_single_image,
    "multi_image": make_multi_image,
    "dedup_heavy": make_dedup_heavy,
    "jpx": make_jpx,
    "softmask": make_softmask,
    "tiny_images": make_tiny_images,
    "mixed": make_mixed,
}


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("out_dir", nargs="?", default=str(Path(__file__).parent / "out"))
    parser.add_argument("--force", action="store_true", help="regenerate even if file exists")
    args = parser.parse_args()

    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    for name, builder in FIXTURES.items():
        out = out_dir / f"{name}.pdf"
        if out.exists() and not args.force:
            print(f"skip {name} (exists)")
            continue
        builder(out)
        print(f"made {name}: {out.stat().st_size / 1024:.1f} KB")


if __name__ == "__main__":
    main()
