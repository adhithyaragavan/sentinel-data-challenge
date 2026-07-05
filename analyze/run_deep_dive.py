"""
run_deep_dive.py — hand top-ranked alerts to the EXISTING agent swarm.

Reads the ranked queue (score_model.py), selects the top-K escalated alerts, adapts
each to the swarm's schema (to_pipeline_alert), and runs the UNCHANGED 5-agent
pipeline (`pipeline.run`, pipeline.py:55). Each result is cached to
deep_dive_cache/<alert_id>.json so the dashboard can show the rationale/evidence
trail without paying NIM latency live.

Requires the swarm's runtime (NVIDIA_NIM_API_KEY + Docker) — run on the VM / demo box.
--dry-run adapts + validates the alert dicts WITHOUT calling the swarm (safe anywhere).

Usage:
    python analyze/run_deep_dive.py --top 3
    python analyze/run_deep_dive.py --top 1 --dry-run
"""
from __future__ import annotations

import argparse
import json
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
sys.path.insert(0, _ROOT)

from analyze.to_pipeline_alert import to_pipeline_alert  # noqa: E402

_DATA_DIR = os.path.join(_ROOT, "data")
_CACHE_DIR = os.path.join(_ROOT, "deep_dive_cache")
_REQUIRED_KEYS = {"alert_id", "timestamp", "hostname", "process", "network",
                  "file_events", "registry_events", "rule_triggered", "mitre_techniques"}


def _select(queue_path: str, top: int, escalated_only: bool):
    import pandas as pd
    q = pd.read_parquet(queue_path)
    if escalated_only and "escalate" in q.columns:
        q = q[q["escalate"] == 1]
    return q.sort_values("risk_score", ascending=False).head(top)


def _raw_lookup(raw_path: str):
    import pandas as pd
    raw = pd.read_parquet(raw_path).drop_duplicates("alert_id", keep="first")
    return {r["alert_id"]: r.to_dict() for _, r in raw.iterrows()}


def main(argv=None):
    ap = argparse.ArgumentParser(description="Run swarm deep-dive on top alerts")
    ap.add_argument("--rows", type=int, default=10_000)
    ap.add_argument("--top", type=int, default=3)
    ap.add_argument("--queue", default=os.path.join(_DATA_DIR, "ranked_queue.parquet"))
    ap.add_argument("--raw", default=None)
    ap.add_argument("--all", action="store_true",
                    help="include non-escalated alerts in selection")
    ap.add_argument("--dry-run", action="store_true",
                    help="adapt + validate alert dicts without calling the swarm")
    args = ap.parse_args(argv)

    raw_path = args.raw or os.path.join(_DATA_DIR, f"alerts_{args.rows}.parquet")
    selection = _select(args.queue, args.top, escalated_only=not args.all)
    raw_by_id = _raw_lookup(raw_path)
    os.makedirs(_CACHE_DIR, exist_ok=True)

    print(f"Selected top {len(selection)} alert(s) for deep-dive:")
    for _, r in selection.iterrows():
        print(f"  rank {int(r['rank']):>3}  risk={r['risk_score']:.3f}  "
              f"{r['alert_id']}  {r.get('alert_type','?')}")

    if args.dry_run:
        print("\n[dry-run] adapting to swarm schema (not calling pipeline.run)...")
        ok = True
        for _, r in selection.iterrows():
            alert = to_pipeline_alert(raw_by_id[r["alert_id"]])
            missing = _REQUIRED_KEYS - set(alert)
            status = "OK" if not missing else f"MISSING {missing}"
            if missing:
                ok = False
            print(f"  {alert['alert_id']}: {status}  "
                  f"host={alert['hostname']} proc={alert['process']['name']} "
                  f"c2={alert['network']['outbound_connections'][0]['dst_ip']}")
        print("[dry-run] all alerts valid" if ok else "[dry-run] SCHEMA ISSUES")
        return 0 if ok else 1

    # Live: import the unchanged swarm and run it.
    import pipeline  # noqa: E402  (pipeline.py at repo root)
    for _, r in selection.iterrows():
        alert = to_pipeline_alert(raw_by_id[r["alert_id"]])
        print(f"\n>>> deep-dive {alert['alert_id']} (risk {r['risk_score']:.3f}) ...")
        result = pipeline.run(alert)
        result["_queue"] = {"rank": int(r["rank"]), "risk_score": float(r["risk_score"]),
                            "band": r.get("band")}
        out = os.path.join(_CACHE_DIR, f"{alert['alert_id']}.json")
        with open(out, "w") as f:
            json.dump(result, f, indent=2)
        print(f"    cached -> {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
