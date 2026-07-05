"""
score_model.py — train an escalation-risk classifier and produce the ranked queue.

Same model, two backends: scikit-learn RandomForest (CPU) vs cuML RandomForest
(GPU), selected by --gpu / SENTINEL_GPU=1. Because escalation label is a planted
signal, we quote quality on a HELD-OUT stratified test split (AUC / F1 / precision /
recall), not on training data — so the ranking is demonstrably *useful*, not just fast.
CPU and GPU quality should match within tolerance; only wall-clock differs.

The model emits P(escalate) in [0,1] as `risk_score`, which plugs straight into the
existing swarm's contract: `risk_score >= RISK_SCORE_THRESHOLD (0.7) -> escalate`, with
bands reused from agents/planner.py (>=0.9 critical / >=0.7 high / >=0.5 medium / <0.5 low).

Usage:
    python analyze/score_model.py --rows 10000                 # CPU
    python analyze/score_model.py --rows 10000 --gpu           # GPU (VM only)
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
sys.path.insert(0, _ROOT)

from ingest import schema as S  # noqa: E402
from clean.normalize import FEATURE_COLUMNS  # noqa: E402

_USE_GPU = ("--gpu" in sys.argv) or os.environ.get("SENTINEL_GPU") == "1"
BACKEND = "gpu" if _USE_GPU else "cpu"
_DATA_DIR = os.path.join(_ROOT, "data")

# Shared hyperparameters — identical across backends for a fair comparison.
RF_PARAMS = {"n_estimators": 100, "max_depth": 12, "random_state": 42}
TEST_SIZE = 0.20

# Risk bands reused from agents/planner.py (planner._SYSTEM_PROMPT:49-53).
RISK_BANDS = [(0.9, "critical"), (0.7, "high"), (0.5, "medium"), (0.0, "low")]


def _threshold() -> float:
    try:
        return float(os.environ.get("RISK_SCORE_THRESHOLD", "0.7"))
    except ValueError:
        return 0.7


def _band(score: float) -> str:
    for lo, name in RISK_BANDS:
        if score >= lo:
            return name
    return "low"


def _make_model():
    """RandomForest classifier for the active backend."""
    if _USE_GPU:
        from cuml.ensemble import RandomForestClassifier as cuRF
        # cuML uses n_bins/max_depth; map the shared params.
        return cuRF(n_estimators=RF_PARAMS["n_estimators"],
                    max_depth=RF_PARAMS["max_depth"],
                    random_state=RF_PARAMS["random_state"])
    from sklearn.ensemble import RandomForestClassifier
    return RandomForestClassifier(n_jobs=-1, **RF_PARAMS)


def _to_host(a):
    """cupy/cudf/numpy -> numpy array (for sklearn.metrics)."""
    import numpy as np
    if hasattr(a, "to_numpy"):
        return a.to_numpy()
    if hasattr(a, "get"):          # cupy
        return a.get()
    return np.asarray(a)


def run(features_path: str, raw_path: str, queue_path: str) -> dict:
    import numpy as np
    import pandas as pd
    from sklearn.model_selection import train_test_split
    from sklearn.metrics import (roc_auc_score, f1_score,
                                  precision_score, recall_score)

    feat = pd.read_parquet(features_path)
    X = feat[FEATURE_COLUMNS].astype("float32")
    y = feat["label_escalate"].astype("int32")

    X_tr, X_te, y_tr, y_te = train_test_split(
        X, y, test_size=TEST_SIZE, stratify=y, random_state=42)

    model = _make_model()
    t0 = time.time()
    model.fit(X_tr.to_numpy() if _USE_GPU else X_tr, y_tr.to_numpy() if _USE_GPU else y_tr)
    fit_s = round(time.time() - t0, 3)

    # Held-out quality
    proba_te = model.predict_proba(X_te.to_numpy() if _USE_GPU else X_te)
    proba_te = _to_host(proba_te)[:, 1]
    y_te_h = _to_host(y_te)
    pred_te = (proba_te >= 0.5).astype(int)
    metrics = {
        "backend": BACKEND,
        "fit_seconds": fit_s,
        "test_rows": int(len(y_te_h)),
        "auc": round(float(roc_auc_score(y_te_h, proba_te)), 4),
        "f1": round(float(f1_score(y_te_h, pred_te, zero_division=0)), 4),
        "precision": round(float(precision_score(y_te_h, pred_te, zero_division=0)), 4),
        "recall": round(float(recall_score(y_te_h, pred_te, zero_division=0)), 4),
    }

    # Operational scoring: risk_score for EVERY alert -> ranked queue.
    t1 = time.time()
    proba_all = _to_host(model.predict_proba(X.to_numpy() if _USE_GPU else X))[:, 1]
    metrics["score_all_seconds"] = round(time.time() - t1, 3)

    queue = pd.DataFrame({
        "alert_id": feat["alert_id"].values,
        "risk_score": np.round(proba_all, 4),
        "label_escalate": _to_host(y),
    })
    queue["band"] = [_band(s) for s in queue["risk_score"]]
    thr = _threshold()
    queue["escalate"] = (queue["risk_score"] >= thr).astype(int)

    # attach display fields from the raw alerts (dedup on alert_id first)
    raw = pd.read_parquet(raw_path).drop_duplicates("alert_id", keep="first")
    disp_cols = ["alert_id", "host_id", "alert_type", "severity_raw",
                 "dest_ip", "file_hash", "timestamp"]
    queue = queue.merge(raw[disp_cols], on="alert_id", how="left")
    queue = queue.sort_values("risk_score", ascending=False).reset_index(drop=True)
    queue.insert(0, "rank", queue.index + 1)
    queue.to_parquet(queue_path, index=False)

    metrics["threshold"] = thr
    metrics["queue_rows"] = int(len(queue))
    metrics["n_escalate"] = int(queue["escalate"].sum())
    metrics["queue_path"] = queue_path
    return metrics


def _print_summary(m: dict):
    bar = "=" * 60
    print(bar)
    print(f" ESCALATION MODEL  [{m['backend'].upper()}]  (RandomForest)")
    print(bar)
    print(f" fit time         : {m['fit_seconds']:.3f}s   (train) ")
    print(f" score-all time   : {m['score_all_seconds']:.3f}s   ({m['queue_rows']:,} alerts)")
    print(" held-out quality (test set):")
    print(f"    AUC       {m['auc']:.4f}")
    print(f"    F1        {m['f1']:.4f}")
    print(f"    precision {m['precision']:.4f}")
    print(f"    recall    {m['recall']:.4f}")
    print(f" escalated (>= {m['threshold']}): {m['n_escalate']:,} / {m['queue_rows']:,}")
    print(f" ranked queue     : {m['queue_path']}")
    print(bar)


def main(argv=None):
    ap = argparse.ArgumentParser(description="Train + score escalation model")
    ap.add_argument("--rows", type=int, default=10_000)
    ap.add_argument("--features", default=None)
    ap.add_argument("--raw", default=None)
    ap.add_argument("--queue", default=None)
    ap.add_argument("--gpu", action="store_true")
    args = ap.parse_args(argv)

    features_path = args.features or os.path.join(_DATA_DIR, f"features_{BACKEND}.parquet")
    if not os.path.exists(features_path):
        # fall back to whichever backend's features exist
        alt = os.path.join(_DATA_DIR, "features_cpu.parquet")
        features_path = alt if os.path.exists(alt) else features_path
    raw_path = args.raw or os.path.join(_DATA_DIR, f"alerts_{args.rows}.parquet")
    queue_path = args.queue or os.path.join(_DATA_DIR, "ranked_queue.parquet")

    m = run(features_path, raw_path, queue_path)
    with open(os.path.join(_DATA_DIR, f"model_metrics_{BACKEND}.json"), "w") as f:
        json.dump(m, f, indent=2)
    _print_summary(m)
    return 0


if __name__ == "__main__":
    sys.exit(main())
