"""
normalize.py — clean + feature-engineer the raw alert dataset.

The SAME cleaning logic runs on CPU and GPU. The ONLY difference is whether
`cudf.pandas.install()` executes before `import pandas as pd` (a one-line swap
selected by --gpu / SENTINEL_GPU=1). Because both backends call the identical
`clean()`, the logic cannot drift; cudf.pandas accelerates the ops it supports
and transparently falls back to pandas for the rest, so output is identical.

Cleaning steps (all vectorized so cudf can accelerate them):
  1. dedupe on alert_id (mirrors the swarm's triage dedup)
  2. normalize mixed-format timestamps (ISO-Z / ISO-offset / epoch) -> UTC epoch + hour
  3. flatten nested process_tree / registry_events / file_events via vectorized
     string ops -> is_unsigned_child, is_office_parent, has_persistence, num_file_events
  4. engineer IOC + severity features mirroring the signals the agent swarm scores on
     (planner._build_summary): bad hash, bad dest IP, high-risk country, beacon port,
     severity, bytes, alert_type

Usage:
    python clean/normalize.py --rows 10000                 # CPU
    python clean/normalize.py --rows 10000 --gpu           # GPU (VM only)
    python clean/normalize.py --rows 10000 --verify        # run BOTH, diff outputs
"""
from __future__ import annotations

import argparse
import os
import re
import subprocess
import sys
import time

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
sys.path.insert(0, _ROOT)

from ingest import schema as S  # noqa: E402

# --- GPU bootstrap: MUST run before `import pandas` ------------------------
_USE_GPU = ("--gpu" in sys.argv) or os.environ.get("SENTINEL_GPU") == "1"
if _USE_GPU:
    import cudf.pandas
    cudf.pandas.install()

import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402  (accelerated iff cudf.pandas.install() ran above)

_DATA_DIR = os.path.join(_ROOT, "data")
BACKEND = "gpu" if _USE_GPU else "cpu"

# Precompiled regex fragments for vectorized flattening.
_OFFICE_RE = "|".join(re.escape(p) for p in S.OFFICE_BROWSER_PARENTS)
_BADIP_RE = "^(?:" + "|".join(re.escape(p) for p in S.KNOWN_BAD_IP_PREFIXES) + ")"
_SEV_MAP = {"low": 0, "1": 0, "medium": 1, "2": 1, "3": 1,
            "high": 2, "4": 2, "critical": 3, "5": 3}

# Feature columns produced by clean() (excludes alert_id + label).
FEATURE_COLUMNS = [
    "hour_of_day", "severity_norm", "is_unsigned_child", "is_office_parent",
    "has_persistence", "num_file_events", "is_known_bad_hash", "is_bad_dest_ip",
    "is_high_risk_country", "is_beacon_port", "protocol_tcp", "bytes_sent_log",
    "alert_type_code", "dst_port",
]


def clean(df):
    """Raw alert DataFrame -> clean feature table. Backend-agnostic (pandas/cudf)."""
    df = df.drop_duplicates(subset="alert_id", keep="first").reset_index(drop=True)

    # 2. timestamps: epoch strings vs ISO (with/without offset) -> UTC epoch seconds.
    ts = df["timestamp"].astype(str)
    is_epoch = ts.str.match(r"^\d+$")
    epoch_dt = pd.to_datetime(pd.to_numeric(ts.where(is_epoch), errors="coerce"),
                              unit="s", utc=True)
    iso_dt = pd.to_datetime(ts.where(~is_epoch), utc=True, errors="coerce",
                            format="mixed")
    parsed = epoch_dt.fillna(iso_dt)

    out = pd.DataFrame({"alert_id": df["alert_id"].values})
    # store epoch seconds (int) — avoids tz-representation diffs across backends
    _epoch0 = pd.Timestamp("1970-01-01", tz="UTC")
    out["event_time_epoch"] = ((parsed - _epoch0) // pd.Timedelta(seconds=1)).astype("int64")
    out["hour_of_day"] = parsed.dt.hour.astype("int64")

    # severity_raw (mixed numeric/word, any case) -> 0..1
    sev = df["severity_raw"].astype(str).str.lower()
    out["severity_norm"] = sev.map(_SEV_MAP).fillna(0).astype("float64") / 3.0

    # 3. flatten nested JSON string columns via vectorized string ops
    pt = df["process_tree"].astype(str)
    out["is_unsigned_child"] = pt.str.contains(r'"signed":\s*false').astype("int64")
    out["is_office_parent"] = pt.str.contains(_OFFICE_RE).astype("int64")
    reg = df["registry_events"].astype(str)
    out["has_persistence"] = reg.str.contains(r"CurrentVersion\\\\Run").astype("int64")
    fe = df["file_events"].astype(str)
    out["num_file_events"] = fe.str.count(r'"action"').astype("int64")

    # 4. IOC + numeric features (mirror planner._build_summary signals)
    out["is_known_bad_hash"] = df["file_hash"].isin(S.KNOWN_BAD_HASHES).astype("int64")
    out["is_bad_dest_ip"] = df["dest_ip"].astype(str).str.contains(_BADIP_RE).astype("int64")
    out["is_high_risk_country"] = df["country"].isin(S.HIGH_RISK_COUNTRIES).astype("int64")
    out["is_beacon_port"] = df["dst_port"].isin(S.BEACON_PORTS).astype("int64")
    out["protocol_tcp"] = (df["protocol"] == "TCP").astype("int64")
    out["bytes_sent_log"] = np.log1p(df["bytes_sent"].astype("float64"))
    out["alert_type_code"] = df["alert_type"].map(S.ALERT_TYPE_CODE).fillna(0).astype("int64")
    out["dst_port"] = df["dst_port"].astype("int64")
    out["label_escalate"] = df["label_escalate"].astype("int64")
    return out


def run(input_path: str, output_path: str) -> dict:
    """Read parquet -> clean -> write parquet. Returns timing/stats dict."""
    df = pd.read_parquet(input_path)
    raw_rows = len(df)
    t0 = time.time()
    feat = clean(df)
    secs = round(time.time() - t0, 3)
    feat.to_parquet(output_path, index=False)
    return {
        "backend": BACKEND,
        "input": input_path,
        "output": output_path,
        "raw_rows": int(raw_rows),
        "clean_rows": int(len(feat)),
        "deduped": int(raw_rows - len(feat)),
        "seconds": secs,
    }


# ---------------------------------------------------------------------------
# Correctness: diff two feature tables produced by the two backends
# ---------------------------------------------------------------------------

def diff_feature_tables(path_a: str, path_b: str, atol: float = 1e-9) -> dict:
    """Compare two feature parquets (order-independent). Returns a report dict."""
    import pandas as _pd  # plain pandas for the comparison itself
    a = _pd.read_parquet(path_a).sort_values("alert_id").reset_index(drop=True)
    b = _pd.read_parquet(path_b).sort_values("alert_id").reset_index(drop=True)

    report = {"shape_a": a.shape, "shape_b": b.shape, "mismatches": {}}
    if list(a.columns) != list(b.columns):
        report["column_mismatch"] = (list(a.columns), list(b.columns))
        report["passed"] = False
        return report
    if a.shape != b.shape:
        report["passed"] = False
        return report

    for col in a.columns:
        if _pd.api.types.is_float_dtype(a[col]):
            bad = int((~np.isclose(a[col].values, b[col].values,
                                   atol=atol, equal_nan=True)).sum())
        else:
            bad = int((a[col].values != b[col].values).sum())
        if bad:
            report["mismatches"][col] = bad
    report["passed"] = len(report["mismatches"]) == 0
    return report


def _verify(input_path: str) -> int:
    """Run BOTH backends in fresh subprocesses, then diff. Returns exit code."""
    cpu_out = os.path.join(_DATA_DIR, "features_cpu.parquet")
    gpu_out = os.path.join(_DATA_DIR, "features_gpu.parquet")
    print("[verify] running CPU backend...")
    r_cpu = subprocess.run([sys.executable, os.path.abspath(__file__),
                            "--input", input_path, "--output", cpu_out],
                           env={**os.environ, "SENTINEL_GPU": "0"})
    print("[verify] running GPU backend...")
    r_gpu = subprocess.run([sys.executable, os.path.abspath(__file__), "--gpu",
                            "--input", input_path, "--output", gpu_out],
                           env={**os.environ, "SENTINEL_GPU": "1"})
    if r_cpu.returncode or r_gpu.returncode:
        print("[verify] a backend failed to run. On the Mac the GPU path needs the "
              "T4 VM (cudf/cuml). Run --verify there.")
        return 2

    rep = diff_feature_tables(cpu_out, gpu_out)
    bar = "=" * 60
    print(bar)
    print(" CPU vs GPU CLEANING PARITY")
    print(bar)
    print(f" CPU output : {cpu_out}  {rep['shape_a']}")
    print(f" GPU output : {gpu_out}  {rep['shape_b']}")
    if rep["passed"]:
        print(" RESULT     : PASS  (identical feature tables)")
    else:
        print(" RESULT     : FAIL")
        for col, n in rep.get("mismatches", {}).items():
            print(f"   mismatch {col}: {n} rows")
        if "column_mismatch" in rep:
            print(f"   columns differ: {rep['column_mismatch']}")
    print(bar)
    return 0 if rep["passed"] else 1


def _default_input(rows: int) -> str:
    return os.path.join(_DATA_DIR, f"alerts_{rows}.parquet")


def main(argv=None):
    ap = argparse.ArgumentParser(description="Clean + feature-engineer alerts")
    ap.add_argument("--rows", type=int, default=10_000,
                    help="pick data/alerts_<rows>.parquet when --input omitted")
    ap.add_argument("--input", default=None)
    ap.add_argument("--output", default=None)
    ap.add_argument("--gpu", action="store_true", help="use cudf.pandas (VM only)")
    ap.add_argument("--verify", action="store_true",
                    help="run BOTH backends and diff outputs (correctness check)")
    args = ap.parse_args(argv)

    input_path = args.input or _default_input(args.rows)

    if args.verify:
        return _verify(input_path)

    output_path = args.output or os.path.join(_DATA_DIR, f"features_{BACKEND}.parquet")
    stats = run(input_path, output_path)
    bar = "=" * 60
    print(bar)
    print(f" CLEANING  [{stats['backend'].upper()}]")
    print(bar)
    print(f" input       : {stats['input']}")
    print(f" raw rows    : {stats['raw_rows']:,}")
    print(f" deduped     : {stats['deduped']:,}  -> clean rows {stats['clean_rows']:,}")
    print(f" clean time  : {stats['seconds']:.3f}s")
    print(f" output      : {stats['output']}")
    print(bar)
    return 0


if __name__ == "__main__":
    sys.exit(main())
