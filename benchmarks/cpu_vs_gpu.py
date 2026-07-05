"""
cpu_vs_gpu.py — the acceleration proof.

Times the CLEANING step and the MODEL step at multiple data sizes, CPU vs GPU,
logs wall-clock time, and writes a results CSV + a matplotlib chart. Results are
CACHED to disk (benchmarks/results.csv, benchmarks/cpu_vs_gpu.png) so the demo
never depends on live GPU access — re-running this script overwrites the cache
("re-run live").

Each (size, backend, step) runs in a FRESH subprocess (worker mode): the GPU
backend requires `cudf.pandas.install()` before `import pandas`, which can't be
toggled mid-process, so isolating each run in its own process is the only honest
way to time both backends. The worker sets the backend via SENTINEL_GPU and reuses
the real clean/normalize.py and analyze/score_model.py code paths — no duplicated
logic. On a machine without RAPIDS (e.g. the Mac dev box) the GPU workers are
skipped and CPU-only results are produced; run on the T4 VM for the full chart.

Cost-safe: `--smoke` runs the full path at ~1K rows first to shake out
install/logic bugs before any billed 10K/100K/1M sweep.

Usage:
    python benchmarks/cpu_vs_gpu.py --smoke          # ~1K sanity, then stop
    python benchmarks/cpu_vs_gpu.py                  # 10K/100K/1M sweep (hero)
    python benchmarks/cpu_vs_gpu.py --sizes 10000 100000
"""
from __future__ import annotations

import argparse
import csv
import json
import os
import subprocess
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
sys.path.insert(0, _ROOT)

_DATA_DIR = os.path.join(_ROOT, "data")
_RESULTS_CSV = os.path.join(_HERE, "results.csv")
_CHART_PNG = os.path.join(_HERE, "cpu_vs_gpu.png")

DEFAULT_SIZES = [10_000, 100_000, 1_000_000]
SMOKE_SIZE = 1_000

# CVD-validated categorical pair (dataviz skill: blue / aqua, ΔE 73.6).
COLOR_CPU = "#2a78d6"
COLOR_GPU = "#1baf7a"


# ---------------------------------------------------------------------------
# Worker: one timed run of one step for the backend named by SENTINEL_GPU.
# ---------------------------------------------------------------------------
def _worker(step: str, size: int) -> int:
    raw = os.path.join(_DATA_DIR, f"alerts_{size}.parquet")
    backend = "gpu" if os.environ.get("SENTINEL_GPU") == "1" else "cpu"
    if step == "clean":
        from clean import normalize   # module bootstrap reads SENTINEL_GPU
        feat = os.path.join(_DATA_DIR, f"features_{backend}.parquet")
        stats = normalize.run(raw, feat)
        print(json.dumps({"step": "clean", "backend": backend,
                          "seconds": stats["seconds"], "rows": size}))
    elif step == "model":
        from analyze import score_model
        feat = os.path.join(_DATA_DIR, f"features_{backend}.parquet")
        tmp_queue = os.path.join(_DATA_DIR, f"queue_{backend}_bench.parquet")
        m = score_model.run(feat, raw, tmp_queue)
        print(json.dumps({"step": "model", "backend": backend,
                          "seconds": round(m["fit_seconds"] + m["score_all_seconds"], 3),
                          "fit_seconds": m["fit_seconds"],
                          "auc": m["auc"], "f1": m["f1"],
                          "precision": m["precision"], "recall": m["recall"],
                          "rows": size}))
    else:
        print(json.dumps({"error": f"unknown step {step}"}))
        return 1
    return 0


def _run_worker(step: str, size: int, gpu: bool) -> dict | None:
    """Invoke a worker subprocess; return its JSON result, or None on failure."""
    env = {**os.environ, "SENTINEL_GPU": "1" if gpu else "0"}
    proc = subprocess.run(
        [sys.executable, os.path.abspath(__file__), "--worker", step, "--size", str(size)],
        env=env, capture_output=True, text=True)
    if proc.returncode != 0:
        tail = (proc.stderr or "").strip().splitlines()[-1:] or ["(no stderr)"]
        print(f"    [{'gpu' if gpu else 'cpu'}/{step}] FAILED: {tail[0]}")
        return None
    line = [ln for ln in proc.stdout.strip().splitlines() if ln.startswith("{")]
    return json.loads(line[-1]) if line else None


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------
def _ensure_dataset(size: int) -> None:
    path = os.path.join(_DATA_DIR, f"alerts_{size}.parquet")
    if os.path.exists(path):
        return
    print(f"  generating dataset ({size:,} rows)...")
    from ingest.generate_alerts import generate, write_parquet, write_ndjson
    os.makedirs(_DATA_DIR, exist_ok=True)
    rows, _ = generate(size, seed=42)
    write_parquet(rows, path)
    write_ndjson(rows, os.path.join(_DATA_DIR, f"alerts_{size}.ndjson"))


def sweep(sizes: list[int], cpu_only: bool) -> list[dict]:
    results: list[dict] = []
    for size in sizes:
        _ensure_dataset(size)
        print(f"\n[{size:,} rows]")
        # CPU first (its cleaning also produces features_cpu for the CPU model).
        backends = [("cpu", False)] + ([] if cpu_only else [("gpu", True)])
        per = {}
        for bname, is_gpu in backends:
            for step in ("clean", "model"):
                r = _run_worker(step, size, gpu=is_gpu)
                if r is None:
                    continue
                per[(bname, step)] = r
                extra = f"  AUC={r['auc']}" if step == "model" else ""
                print(f"    {bname}/{step:5s}: {r['seconds']:.3f}s{extra}")
        # assemble rows with speedup
        for step in ("clean", "model"):
            cpu_r = per.get(("cpu", step))
            gpu_r = per.get(("gpu", step))
            if cpu_r:
                results.append(_row(size, step, "cpu", cpu_r, None))
            if gpu_r:
                spd = round(cpu_r["seconds"] / gpu_r["seconds"], 2) if cpu_r and gpu_r["seconds"] else None
                results.append(_row(size, step, "gpu", gpu_r, spd))
    return results


def _row(size, step, backend, r, speedup) -> dict:
    return {
        "size": size, "step": step, "backend": backend,
        "seconds": r["seconds"], "speedup": speedup,
        "auc": r.get("auc", ""), "f1": r.get("f1", ""),
        "precision": r.get("precision", ""), "recall": r.get("recall", ""),
    }


def write_csv(results: list[dict], path: str = _RESULTS_CSV) -> None:
    cols = ["size", "step", "backend", "seconds", "speedup", "auc", "f1", "precision", "recall"]
    with open(path, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=cols)
        w.writeheader()
        w.writerows(results)


# ---------------------------------------------------------------------------
# Chart
# ---------------------------------------------------------------------------
def plot(results: list[dict], path: str = _CHART_PNG) -> None:
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    import numpy as np

    sizes = sorted({r["size"] for r in results})
    steps = ["clean", "model"]
    titles = {"clean": "Cleaning  (cudf.pandas)", "model": "Model train+score  (cuML)"}
    idx = {(r["size"], r["step"], r["backend"]): r for r in results}
    has_gpu = any(r["backend"] == "gpu" for r in results)

    fig, axes = plt.subplots(1, 2, figsize=(11, 4.6))
    x = np.arange(len(sizes))
    w = 0.38
    for ax, step in zip(axes, steps):
        cpu = [idx.get((s, step, "cpu"), {}).get("seconds", np.nan) for s in sizes]
        gpu = [idx.get((s, step, "gpu"), {}).get("seconds", np.nan) for s in sizes]
        b1 = ax.bar(x - (w/2 if has_gpu else 0), cpu, w, label="CPU", color=COLOR_CPU, zorder=3)
        _labels(ax, b1, cpu)
        if has_gpu:
            b2 = ax.bar(x + w/2, gpu, w, label="GPU", color=COLOR_GPU, zorder=3)
            _labels(ax, b2, gpu)
            for i, s in enumerate(sizes):   # speedup annotation over GPU bar
                r = idx.get((s, step, "gpu"))
                if r and r.get("speedup"):
                    ax.annotate(f"{r['speedup']:.1f}×", (x[i] + w/2, gpu[i]),
                                textcoords="offset points", xytext=(0, 12),
                                ha="center", fontsize=9, fontweight="bold", color="#0b0b0b")
        ax.set_yscale("log")
        ax.set_title(titles[step], fontsize=11, color="#0b0b0b")
        ax.set_xticks(x)
        ax.set_xticklabels([f"{s//1000}K" if s < 1_000_000 else f"{s//1_000_000}M" for s in sizes])
        ax.set_ylabel("wall-clock seconds (log)", fontsize=9, color="#52514e")
        ax.set_xlabel("dataset rows", fontsize=9, color="#52514e")
        ax.grid(axis="y", color="#e5e5e2", linewidth=0.8, zorder=0)
        for sp in ("top", "right"):
            ax.spines[sp].set_visible(False)
        ax.tick_params(colors="#52514e")

    fig.tight_layout(rect=(0, 0, 1, 0.82))
    handles = [plt.Rectangle((0, 0), 1, 1, color=COLOR_CPU),
               plt.Rectangle((0, 0), 1, 1, color=COLOR_GPU)]
    fig.legend(handles, ["CPU  (pandas / scikit-learn)", "GPU  (cudf.pandas / cuML)"],
               loc="upper center", ncol=2, frameon=False, fontsize=9,
               bbox_to_anchor=(0.5, 0.90))
    sub = "CPU vs NVIDIA T4 GPU" if has_gpu else "CPU only — run on the T4 VM for GPU bars"
    fig.text(0.5, 0.975, "Sentinel pipeline acceleration", ha="center",
             fontsize=13, fontweight="bold", color="#0b0b0b")
    fig.text(0.5, 0.93, sub, ha="center", fontsize=9, color="#52514e")
    fig.savefig(path, dpi=130)
    plt.close(fig)


def _labels(ax, bars, vals):
    import numpy as np
    for b, v in zip(bars, vals):
        if v is None or (isinstance(v, float) and np.isnan(v)):
            continue
        txt = f"{v:.2f}s" if v >= 0.1 else f"{v*1000:.0f}ms"
        ax.annotate(txt, (b.get_x() + b.get_width()/2, v),
                    textcoords="offset points", xytext=(0, 2),
                    ha="center", va="bottom", fontsize=7.5, color="#52514e")


def _print_summary(results: list[dict]) -> None:
    bar = "=" * 68
    print(bar)
    print(" CPU vs GPU BENCHMARK")
    print(bar)
    print(f" {'rows':>9} {'step':6} {'CPU s':>9} {'GPU s':>9} {'speedup':>8} {'AUC':>7}")
    sizes = sorted({r["size"] for r in results})
    idx = {(r["size"], r["step"], r["backend"]): r for r in results}
    for s in sizes:
        for step in ("clean", "model"):
            c = idx.get((s, step, "cpu"))
            g = idx.get((s, step, "gpu"))
            cs = f"{c['seconds']:.3f}" if c else "-"
            gs = f"{g['seconds']:.3f}" if g else "-"
            sp = f"{g['speedup']:.1f}x" if g and g.get("speedup") else "-"
            auc = (g or c or {}).get("auc", "") or "-"
            print(f" {s:>9,} {step:6} {cs:>9} {gs:>9} {sp:>8} {str(auc):>7}")
    print(bar)


def main(argv=None):
    ap = argparse.ArgumentParser(description="CPU vs GPU acceleration benchmark")
    ap.add_argument("--worker", choices=["clean", "model"], help="(internal) run one step")
    ap.add_argument("--size", type=int, help="(internal) worker size")
    ap.add_argument("--smoke", action="store_true", help="~1K full-path sanity then stop")
    ap.add_argument("--sizes", type=int, nargs="+", default=None)
    ap.add_argument("--cpu-only", action="store_true", help="skip GPU (no RAPIDS present)")
    ap.add_argument("--no-plot", action="store_true")
    args = ap.parse_args(argv)

    if args.worker:
        return _worker(args.worker, args.size)

    if args.smoke:
        print("SMOKE TEST (~1K rows) — validating full path before any billed sweep\n")
        results = sweep([SMOKE_SIZE], cpu_only=args.cpu_only)
        _print_summary(results)
        print("\nSmoke passed. Now run the real sweep:  python benchmarks/cpu_vs_gpu.py")
        return 0

    sizes = args.sizes or DEFAULT_SIZES
    results = sweep(sizes, cpu_only=args.cpu_only)
    write_csv(results)
    if not args.no_plot:
        plot(results)
    _print_summary(results)
    print(f"\n cached -> {_RESULTS_CSV}")
    print(f" cached -> {_CHART_PNG}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
