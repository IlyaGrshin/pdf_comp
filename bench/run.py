#!/usr/bin/env python3
"""Run scripts/recompress.py against every fixture and capture metrics.

Each fixture is recompressed N times (median of 3 by default). Wall, user,
and system time plus peak RSS come from /usr/bin/time -v wrapping the child
process. SSIM is computed once per fixture by rasterizing input and output
with pdftoppm and comparing pages with skimage.metrics.structural_similarity.

Why median+max-RSS rather than mean: shared CI runners produce long-tailed
timing outliers (a single noisy neighbor can double a run); the median
ignores them. RSS we want the worst case across runs since that's what'd
trip the cgroup limit in production.

Output: bench/results.json (or first CLI arg). Schema:
    {
      "results": [
        {fixture, in_bytes, out_bytes, ratio, ssim, wall_s, cpu_s,
         max_rss_kb, recompress: {...recompress.py stdout JSON}, raw_runs: [...]},
        ...
      ],
      "elapsed_s": float,
      "meta": {"python": "...", "host": "..."},
    }
"""

import argparse
import json
import os
import platform
import re
import shutil
import statistics
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import numpy as np
from PIL import Image
from skimage.metrics import structural_similarity as ssim_fn

REPO_ROOT = Path(__file__).resolve().parents[1]
RECOMPRESS = REPO_ROOT / "scripts" / "recompress.py"

VENV_PY = REPO_ROOT / ".venv" / "bin" / "python"
PYTHON = str(VENV_PY) if VENV_PY.exists() else sys.executable

TIME_BIN = "/usr/bin/time"

TIME_PATTERNS = {
    "wall_s": re.compile(r"Elapsed \(wall clock\) time .*?: (.+)$", re.MULTILINE),
    "user_s": re.compile(r"User time \(seconds\): ([0-9.]+)"),
    "sys_s": re.compile(r"System time \(seconds\): ([0-9.]+)"),
    "max_rss_kb": re.compile(r"Maximum resident set size \(kbytes\): (\d+)"),
}


def parse_wall_clock(s: str) -> float:
    parts = [float(p) for p in s.strip().split(":")]
    if len(parts) == 2:
        return parts[0] * 60 + parts[1]
    if len(parts) == 3:
        return parts[0] * 3600 + parts[1] * 60 + parts[2]
    return parts[0]


def run_once(input_pdf: Path, output_pdf: Path) -> dict:
    if not Path(TIME_BIN).exists():
        raise RuntimeError(f"{TIME_BIN} not found — install GNU time (apt install time)")
    with tempfile.NamedTemporaryFile("w+", suffix=".time", delete=False) as tf:
        tf_path = Path(tf.name)
    try:
        proc = subprocess.run(
            [TIME_BIN, "-v", "-o", str(tf_path),
             PYTHON, str(RECOMPRESS), str(input_pdf), str(output_pdf)],
            check=True,
            capture_output=True,
        )
        text = tf_path.read_text()
    finally:
        tf_path.unlink(missing_ok=True)

    metrics: dict = {}
    for key, pat in TIME_PATTERNS.items():
        m = pat.search(text)
        if not m:
            continue
        if key == "wall_s":
            metrics[key] = parse_wall_clock(m.group(1))
        elif key == "max_rss_kb":
            metrics[key] = int(m.group(1))
        else:
            metrics[key] = float(m.group(1))
    metrics["cpu_s"] = metrics.get("user_s", 0.0) + metrics.get("sys_s", 0.0)

    try:
        metrics["recompress"] = json.loads(proc.stdout.decode())
    except (json.JSONDecodeError, UnicodeDecodeError):
        metrics["recompress"] = {}
    return metrics


def render_pdf(pdf_path: Path, out_dir: Path, dpi: int) -> list[Path]:
    out_dir.mkdir(parents=True, exist_ok=True)
    prefix = out_dir / "page"
    subprocess.run(
        ["pdftoppm", "-r", str(dpi), "-png", str(pdf_path), str(prefix)],
        check=True,
        capture_output=True,
    )
    return sorted(out_dir.glob("page-*.png"))


def compute_ssim(in_pdf: Path, out_pdf: Path, dpi: int) -> float:
    with tempfile.TemporaryDirectory() as td:
        td_path = Path(td)
        a = render_pdf(in_pdf, td_path / "a", dpi)
        b = render_pdf(out_pdf, td_path / "b", dpi)
        if not a or not b:
            return float("nan")
        n = min(len(a), len(b))
        scores: list[float] = []
        for ap, bp in zip(a[:n], b[:n]):
            ai = np.asarray(Image.open(ap).convert("RGB"))
            bi = np.asarray(Image.open(bp).convert("RGB"))
            # pdftoppm output can drift by 1px between runs — clip to common.
            h = min(ai.shape[0], bi.shape[0])
            w = min(ai.shape[1], bi.shape[1])
            scores.append(float(ssim_fn(ai[:h, :w], bi[:h, :w], channel_axis=2, data_range=255)))
        return statistics.mean(scores) if scores else float("nan")


def median_run(input_pdf: Path, work_dir: Path, repeats: int) -> dict:
    runs: list[dict] = []
    last_output: Path | None = None
    for i in range(repeats):
        out = work_dir / f"{input_pdf.stem}.run{i}.pdf"
        runs.append(run_once(input_pdf, out))
        last_output = out
    return {
        "wall_s": statistics.median(r["wall_s"] for r in runs),
        "cpu_s": statistics.median(r["cpu_s"] for r in runs),
        "max_rss_kb": max(r["max_rss_kb"] for r in runs),
        "output_pdf": str(last_output),
        "recompress": runs[0].get("recompress", {}),
        "raw_runs": runs,
    }


def bench_fixture(fixture: Path, work_dir: Path, repeats: int, ssim_dpi: int) -> dict:
    print(f"-- {fixture.name}", flush=True)
    timings = median_run(fixture, work_dir, repeats=repeats)
    out = Path(timings.pop("output_pdf"))
    in_size = fixture.stat().st_size
    out_size = out.stat().st_size
    return {
        "fixture": fixture.name,
        "in_bytes": in_size,
        "out_bytes": out_size,
        "ratio": out_size / in_size if in_size else 1.0,
        "ssim": compute_ssim(fixture, out, dpi=ssim_dpi),
        **timings,
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("output", nargs="?", default=str(REPO_ROOT / "bench" / "results.json"))
    parser.add_argument("--fixtures", default=str(REPO_ROOT / "bench" / "fixtures" / "out"))
    parser.add_argument("--repeats", type=int, default=3)
    parser.add_argument("--ssim-dpi", type=int, default=150)
    args = parser.parse_args()

    fixtures_dir = Path(args.fixtures)
    work_dir = REPO_ROOT / "bench" / "work"
    if work_dir.exists():
        shutil.rmtree(work_dir)
    work_dir.mkdir(parents=True)

    pdfs = sorted(fixtures_dir.glob("*.pdf"))
    if not pdfs:
        print(f"No fixtures in {fixtures_dir}; run generate.py first.", file=sys.stderr)
        sys.exit(2)

    t0 = time.time()
    results = [bench_fixture(p, work_dir, args.repeats, args.ssim_dpi) for p in pdfs]
    elapsed = time.time() - t0

    out = {
        "results": results,
        "elapsed_s": round(elapsed, 1),
        "meta": {
            "python": platform.python_version(),
            "host": platform.platform(),
            "cpu_count": os.cpu_count(),
            "repeats": args.repeats,
            "ssim_dpi": args.ssim_dpi,
        },
    }
    Path(args.output).write_text(json.dumps(out, indent=2))
    print(f"wrote {args.output} ({elapsed:.1f}s)")


if __name__ == "__main__":
    main()
