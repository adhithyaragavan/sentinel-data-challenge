"""
Sentinel pipeline — orchestrates the 5-agent swarm end-to-end.

Flow:
  EDR alert JSON
    → Triage Agent          (classify severity, dedupe)
    → Forensic Examiner     (IOCs, threat-intel enrichment)
    → Tool-Executor         (Docker sandbox detonation)
    → Remediation Planner   (action + risk_score via NIM)
    → Supervisor            (auto-remediate or Slack escalation)
"""

import json
import os
import sys
import time

from dotenv import load_dotenv

load_dotenv(os.path.join(os.path.dirname(__file__), ".env"))

from agents import triage, forensic, executor, planner, supervisor

_ALERT_PATH = os.path.join(os.path.dirname(__file__), "mock_data", "edr_alert.json")

RESET  = "\033[0m"
BOLD   = "\033[1m"
CYAN   = "\033[36m"
GREEN  = "\033[32m"
YELLOW = "\033[33m"
RED    = "\033[31m"


def _banner(step: int, name: str) -> None:
    print(f"\n{BOLD}{CYAN}[{step}/5] {name}{RESET}")
    print("─" * 50)


def _ok(label: str, value: str) -> None:
    print(f"  {GREEN}✓{RESET} {label}: {BOLD}{value}{RESET}")


def _warn(label: str, value: str) -> None:
    print(f"  {YELLOW}!{RESET} {label}: {value}")


def _run_step(name: str, fn, *args, **kwargs):
    start = time.time()
    result = fn(*args, **kwargs)
    elapsed = time.time() - start
    print(f"  {GREEN}completed in {elapsed:.1f}s{RESET}")
    return result


def run(alert: dict) -> dict:
    print(f"\n{BOLD}{'═'*50}")
    print("  SENTINEL — Autonomous SOC Incident Response")
    print(f"{'═'*50}{RESET}")
    print(f"  Alert: {alert.get('alert_id')}  |  Host: {alert.get('hostname')}  |  User: {alert.get('username')}")

    # ── Step 1: Triage ──────────────────────────────────────────────────────
    _banner(1, "Triage Agent")
    triage_out = _run_step("triage", triage.run, alert)
    if triage_out["duplicate"]:
        _warn("duplicate", "alert already processed — stopping pipeline")
        return {"triage": triage_out}
    _ok("severity",       triage_out["severity"])
    _ok("classification", triage_out["classification"])
    _ok("confidence",     f"{triage_out['confidence']:.0%}")

    # ── Step 2: Forensic Examiner ────────────────────────────────────────────
    _banner(2, "Forensic Examiner Agent")
    forensic_out = _run_step("forensic", forensic.run, {"alert": alert, "triage": triage_out})
    _ok("IOCs found",    str(len(forensic_out["iocs"])))
    _ok("hash verdict",  f"{(forensic_out.get('hash_intel') or {}).get('malware_family', 'unknown')} — {(forensic_out.get('hash_intel') or {}).get('vendor_detections', '?')}/{(forensic_out.get('hash_intel') or {}).get('total_vendors', '?')} vendors")
    _ok("C2 IP abuse",   f"{(forensic_out.get('ip_intel') or {}).get('abuse_confidence_score', '?')}% confidence")
    print(f"\n  {forensic_out['evidence_summary']}")

    # ── Step 3: Tool-Executor ────────────────────────────────────────────────
    _banner(3, "Tool-Executor Agent  (Docker sandbox)")
    print(f"  {YELLOW}detonating sample — this may take ~60s...{RESET}")
    executor_out = _run_step("executor", executor.run, {"alert": alert, "forensic": forensic_out})
    _ok("detonation",    executor_out["detonation_status"])
    _ok("verdict",       executor_out["verdict"])
    blocked = executor_out.get("blocked_connections", [])
    if blocked:
        for b in blocked:
            _ok("blocked C2",f"{b.get('dst_host')}:{b.get('dst_port')} — {b.get('deny_reason', 'denied')}")
    else:
        _warn("blocked",     "no outbound connections detected")

    # ── Step 4: Remediation Planner ──────────────────────────────────────────
    _banner(4, "Remediation Planner Agent")
    planner_out = _run_step("planner", planner.run, {
        "alert": alert, "triage": triage_out, "forensic": forensic_out, "executor": executor_out,
    })
    _ok("action",        planner_out["action"])
    _ok("target",        planner_out["target"])
    _ok("risk_score",    f"{planner_out['risk_score']:.2f}")
    _ok("confidence",    f"{planner_out['confidence']:.0%}")
    print(f"\n  {planner_out['rationale']}")

    # ── Step 5: Supervisor ───────────────────────────────────────────────────
    _banner(5, "Supervisor Agent")
    supervisor_out = _run_step("supervisor", supervisor.run, {
        "alert": alert, "triage": triage_out, "forensic": forensic_out,
        "executor": executor_out, "planner": planner_out,
    })

    decision = supervisor_out["decision"]
    if decision == "auto_remediated":
        _ok("decision", f"{GREEN}AUTO-REMEDIATED{RESET}")
    else:
        color = RED if supervisor_out["slack_notified"] else YELLOW
        _ok("decision", f"{color}ESCALATED TO HUMAN{RESET}")
        if supervisor_out["slack_notified"]:
            _ok("slack", "alert posted — awaiting human approval")
        else:
            _warn("slack", "SLACK_WEBHOOK_URL not set — escalation logged only")

    print(f"\n  {supervisor_out['outcome']}")

    # ── Summary ──────────────────────────────────────────────────────────────
    print(f"\n{BOLD}{'═'*50}")
    print("  PIPELINE COMPLETE")
    print(f"{'═'*50}{RESET}\n")

    return {
        "triage":     triage_out,
        "forensic":   forensic_out,
        "executor":   executor_out,
        "planner":    planner_out,
        "supervisor": supervisor_out,
    }


if __name__ == "__main__":
    with open(_ALERT_PATH) as f:
        alert = json.load(f)

    results = run(alert)

    out_path = os.path.join(os.path.dirname(__file__), "pipeline_output.json")
    with open(out_path, "w") as f:
        json.dump(results, f, indent=2)
    print(f"Full output written to {out_path}\n")
