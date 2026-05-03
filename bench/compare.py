#!/usr/bin/env python3
"""Compare two bench/results.json files; print a markdown delta table.

Exit codes:
    0  no regression (or no baseline supplied)
    1  ratio or SSIM regressed past threshold

Time and CPU regressions are surfaced in the table but never fail the build —
shared CI runners produce ±20-40% timing noise, so a strict gate would mostly
flag noise. Ratio and SSIM are deterministic, so we gate on them.
"""

import argparse
import json
import sys
from pathlib import Path

# Output > 5% larger than baseline = compression regressed.
RATIO_FAIL_DELTA = 0.05
# SSIM dropped > 0.005 = visible quality regression (designer's eye).
SSIM_FAIL_DELTA = 0.005


def fmt_bytes(n: float) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024:
            return f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} TB"


def fmt_pct(new: float, old: float) -> str:
    if old == 0:
        return ""
    return f"{(new - old) / old * 100:+.1f}%"


def fmt_abs(new: float, old: float, precision: int) -> str:
    return f"{new - old:+.{precision}f}"


def index_by(results: list[dict], key: str = "fixture") -> dict[str, dict]:
    return {r[key]: r for r in results}


def render_table(rows: list[list[str]], headers: list[str]) -> str:
    widths = [max(len(str(r[i])) for r in rows + [headers]) for i in range(len(headers))]
    lines = []
    lines.append("| " + " | ".join(headers[i].ljust(widths[i]) for i in range(len(headers))) + " |")
    lines.append("|" + "|".join("-" * (w + 2) for w in widths) + "|")
    for r in rows:
        lines.append("| " + " | ".join(str(r[i]).ljust(widths[i]) for i in range(len(r))) + " |")
    return "\n".join(lines)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("new", help="results.json from the candidate branch")
    parser.add_argument("base", nargs="?", help="results.json from the baseline branch (optional)")
    args = parser.parse_args()

    new = json.loads(Path(args.new).read_text())
    base = None
    if args.base:
        base_path = Path(args.base)
        if base_path.exists() and base_path.stat().st_size > 0:
            try:
                loaded = json.loads(base_path.read_text())
                if loaded.get("results"):
                    base = loaded
            except json.JSONDecodeError:
                pass

    new_idx = index_by(new["results"])
    base_idx = index_by(base["results"]) if base else {}

    failures: list[str] = []
    rows: list[list[str]] = []
    totals = {"in": 0, "out": 0, "wall": 0.0, "cpu": 0.0}
    base_totals = {"in": 0, "out": 0, "wall": 0.0, "cpu": 0.0}

    for name in sorted(new_idx):
        n = new_idx[name]
        b = base_idx.get(name)

        ratio_cell = f"{n['ratio']:.3f}"
        ssim_cell = f"{n['ssim']:.4f}"
        wall_cell = f"{n['wall_s']:.2f}s"
        cpu_cell = f"{n['cpu_s']:.2f}s"

        if b:
            ratio_cell += f" ({fmt_abs(n['ratio'], b['ratio'], 3)})"
            ssim_cell += f" ({fmt_abs(n['ssim'], b['ssim'], 4)})"
            wall_cell += f" ({fmt_pct(n['wall_s'], b['wall_s'])})"
            cpu_cell += f" ({fmt_pct(n['cpu_s'], b['cpu_s'])})"
            if (n["ratio"] - b["ratio"]) > RATIO_FAIL_DELTA:
                failures.append(f"{name}: ratio {b['ratio']:.3f} -> {n['ratio']:.3f}")
            if (b["ssim"] - n["ssim"]) > SSIM_FAIL_DELTA:
                failures.append(f"{name}: SSIM {b['ssim']:.4f} -> {n['ssim']:.4f}")
            base_totals["in"] += b["in_bytes"]
            base_totals["out"] += b["out_bytes"]
            base_totals["wall"] += b["wall_s"]
            base_totals["cpu"] += b["cpu_s"]

        totals["in"] += n["in_bytes"]
        totals["out"] += n["out_bytes"]
        totals["wall"] += n["wall_s"]
        totals["cpu"] += n["cpu_s"]

        rows.append([
            name,
            f"{fmt_bytes(n['in_bytes'])} -> {fmt_bytes(n['out_bytes'])}",
            ratio_cell,
            ssim_cell,
            wall_cell,
            cpu_cell,
            f"{n['max_rss_kb'] / 1024:.0f} MB",
        ])

    headers = ["fixture", "size", "ratio", "ssim", "wall", "cpu", "rss"]
    print(render_table(rows, headers))

    overall_ratio = totals["out"] / totals["in"] if totals["in"] else 1.0
    print()
    print(f"**Overall:** {fmt_bytes(totals['in'])} -> {fmt_bytes(totals['out'])} "
          f"(ratio {overall_ratio:.3f}), wall {totals['wall']:.1f}s, cpu {totals['cpu']:.1f}s")
    if base:
        base_ratio = base_totals["out"] / base_totals["in"] if base_totals["in"] else 1.0
        print(f"**Baseline:** ratio {base_ratio:.3f}, wall {base_totals['wall']:.1f}s, "
              f"cpu {base_totals['cpu']:.1f}s")

    if failures:
        print("\n**Regressions (gating):**")
        for f in failures:
            print(f"- {f}")
        sys.exit(1)


if __name__ == "__main__":
    main()
