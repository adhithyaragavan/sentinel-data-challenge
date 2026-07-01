"""
Sentinel evaluation harness — the "small but believable evaluation loop"
required by the hackathon Track A rubric.

Runs the full pipeline against the mock EDR alert, then scores each agent's
output against eval/ground_truth.json and records per-agent latency.

Metrics:
  - correctness: pass/fail per checked assertion (accuracy = passed / total)
  - latency:     wall-clock seconds per agent stage

Writes eval/results.json and prints a summary table.
"""

import json
import os
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from dotenv import load_dotenv
load_dotenv(os.path.join(os.path.dirname(__file__), "..", ".env"))

from agents import triage, forensic, executor, planner, supervisor

_HERE = os.path.dirname(__file__)
_GT_PATH = os.path.join(_HERE, "ground_truth.json")
_ALERT_PATH = os.path.join(_HERE, "..", "mock_data", "edr_alert.json")
_RESULTS_PATH = os.path.join(_HERE, "results.json")


def _timed(fn, *args, **kwargs):
    start = time.time()
    result = fn(*args, **kwargs)
    return result, round(time.time() - start, 3)


def _check(checks: list, name: str, passed: bool, detail: str = "") -> None:
    checks.append({"check": name, "passed": bool(passed), "detail": detail})


def run() -> dict:
    with open(_GT_PATH) as f:
        gt = json.load(f)["expected"]
    with open(_ALERT_PATH) as f:
        alert = json.load(f)

    latency = {}
    checks = []

    # Stage 1: Triage
    triage_out, latency["triage"] = _timed(triage.run, alert)
    _check(checks, f"triage.severity in {gt['triage_severity_acceptable']}",
           triage_out["severity"] in gt["triage_severity_acceptable"],
           f"got {triage_out['severity']!r}")
    _check(checks, "triage.duplicate == false",
           triage_out["duplicate"] == gt["triage_duplicate"],
           f"got {triage_out['duplicate']}")

    # Stage 2: Forensic
    forensic_out, latency["forensic"] = _timed(
        forensic.run, {"alert": alert, "triage": triage_out})
    _check(checks, f"forensic.iocs >= {gt['forensic_min_iocs']}",
           len(forensic_out["iocs"]) >= gt["forensic_min_iocs"],
           f"got {len(forensic_out['iocs'])}")
    fam = (forensic_out.get("hash_intel") or {}).get("malware_family")
    _check(checks, f"forensic.malware_family == {gt['forensic_malware_family']}",
           fam == gt["forensic_malware_family"], f"got {fam!r}")

    # Stage 3: Tool-Executor (Docker sandbox)
    executor_out, latency["executor"] = _timed(
        executor.run, {"alert": alert, "forensic": forensic_out})
    _check(checks, "executor.verdict == malicious",
           executor_out["verdict"] == gt["executor_verdict"],
           f"got {executor_out['verdict']!r}")
    has_blocked = len(executor_out.get("blocked_connections", [])) > 0
    _check(checks, "executor blocked >=1 C2 connection",
           has_blocked == gt["executor_expect_blocked_connection"],
           f"blocked={len(executor_out.get('blocked_connections', []))}")

    # Stage 4: Remediation Planner
    planner_out, latency["planner"] = _timed(planner.run, {
        "alert": alert, "triage": triage_out,
        "forensic": forensic_out, "executor": executor_out})
    _check(checks, "planner.action in expected set",
           planner_out["action"] in gt["planner_expected_actions"],
           f"got {planner_out['action']!r}")
    _check(checks, f"planner.risk_score >= {gt['planner_min_risk_score']}",
           planner_out["risk_score"] >= gt["planner_min_risk_score"],
           f"got {planner_out['risk_score']}")

    # Stage 5: Supervisor
    supervisor_out, latency["supervisor"] = _timed(supervisor.run, {
        "alert": alert, "triage": triage_out, "forensic": forensic_out,
        "executor": executor_out, "planner": planner_out})
    _check(checks, "supervisor.decision == escalated",
           supervisor_out["decision"] == gt["supervisor_decision"],
           f"got {supervisor_out['decision']!r}")

    passed = sum(1 for c in checks if c["passed"])
    total = len(checks)
    accuracy = round(passed / total, 3) if total else 0.0
    total_latency = round(sum(latency.values()), 3)

    results = {
        "alert_id": alert.get("alert_id"),
        "accuracy": accuracy,
        "passed": passed,
        "total": total,
        "latency_seconds": latency,
        "total_latency_seconds": total_latency,
        "checks": checks,
    }
    return results


def _print_summary(r: dict) -> None:
    print("\n" + "=" * 56)
    print("  SENTINEL EVALUATION RESULTS")
    print("=" * 56)
    print(f"  Accuracy: {r['passed']}/{r['total']} = {r['accuracy']:.0%}\n")
    print("  Per-check:")
    for c in r["checks"]:
        mark = "PASS" if c["passed"] else "FAIL"
        print(f"    [{mark}] {c['check']}  ({c['detail']})")
    print("\n  Per-agent latency (s):")
    for agent, secs in r["latency_seconds"].items():
        print(f"    {agent:12s} {secs:>7.3f}")
    print(f"    {'TOTAL':12s} {r['total_latency_seconds']:>7.3f}")
    print("=" * 56 + "\n")


if __name__ == "__main__":
    results = run()
    with open(_RESULTS_PATH, "w") as f:
        json.dump(results, f, indent=2)
    _print_summary(results)
    print(f"Results written to {_RESULTS_PATH}")
    sys.exit(0 if results["accuracy"] == 1.0 else 1)
