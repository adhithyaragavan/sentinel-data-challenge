# Sentinel — Architecture & Safety Design

## Design goal

Compress the human SOC triage-to-containment loop — normally 20–40 minutes of
analyst work — into a few autonomous seconds, while keeping a human in the loop
for high-risk actions. The system is a linear agent pipeline: each stage consumes
a structured JSON packet and emits one, so every agent is independently testable
and the full run produces an auditable evidence trail.

## Pipeline

```
EDR alert ─► Triage ─► Forensic ─► Tool-Executor ─► Planner ─► Supervisor ─► action
             (NIM)      (NIM)       (Docker sandbox)      (NIM)       (Slack / auto)
```

| Stage | Input | Output | External call |
|-------|-------|--------|---------------|
| Triage | EDR alert | severity, classification, dedupe flag | NIM |
| Forensic | alert + triage | IOCs, process tree, threat-intel | NIM + threat-intel |
| Tool-Executor | alert + forensic | verdict, blocked connections | **Docker sandbox sandbox** |
| Planner | all prior | action, risk_score, rationale | NIM |
| Supervisor | all prior | decision, Slack notification | **Slack webhook** |

## Why a linear pipeline (not a graph)

For a SOC containment workflow the stages have a strict data dependency: you cannot
plan remediation before you have a sandbox verdict, and you cannot get a verdict
before you have the file hash from forensics. A linear chain makes the evidence
trail deterministic and the demo legible. Concurrency would only matter for
multi-incident handling, which is out of scope (see limitations).

## Safety design (Docker sandbox)

The Tool-Executor is the only stage that runs untrusted code — the suspected
malware. Everything dangerous is confined inside a Docker container with the
following four safety boundaries:

1. **Network egress control.** `--network none` removes the container's network
   interface entirely at the kernel level. When the detonated sample tries to
   beacon to its C2 (`185.220.101.47:4444`) it gets `ENETUNREACH` — the connection
   never leaves the host. This is verifiable live and is the core evidence that the
   file is malicious.

2. **Filesystem scope.** `--read-only` makes the container root filesystem
   immutable. `--tmpfs /tmp:noexec,nosuid,size=64m` provides a capped, writable,
   non-executable scratch space that is destroyed when the container exits. Malware
   persistence attempts land in `/tmp` and disappear with the container.

3. **Credentials isolation.** Nothing from the host environment is mounted into the
   container — no volumes except the read-only payload file, no env vars, no
   secrets. The `.env` (NIM key, Slack webhook) stays in the orchestrator process.

4. **Human control / approval gates.** The Supervisor enforces the final gate:
   any incident scoring at or above `RISK_SCORE_THRESHOLD` (default 0.7) is not
   auto-actioned — it is escalated to a human via Slack with the full evidence
   packet. The agent recommends; the human approves.

## Trust boundary

```
┌─────────────────────────────────────────────┐
│ Orchestrator (trusted)                        │
│  - holds NIM key + Slack webhook              │
│  - runs Triage, Forensic, Planner, Supervisor │
│                                               │
│   ┌───────────────────────────────────────┐  │
│   │ Docker sandbox sandbox (untrusted)          │  │
│   │  - runs the suspected malware          │  │
│   │  - default-deny network                │  │
│   │  - no host credentials                 │  │
│   │  - ephemeral filesystem                │  │
│   └───────────────────────────────────────┘  │
└─────────────────────────────────────────────┘
```

## Reliability / evaluation

`eval/evaluate.py` runs the full pipeline against a fixed alert and scores nine
correctness checks (severity, IOC count, malware family, sandbox verdict, blocked
connection, planner action, risk score, supervisor decision) plus per-agent
latency. This is the "small but believable evaluation loop" — it catches
regressions in any stage and gives a single accuracy number.

## Known limitations

- Docker sandbox is single-container; no concurrent multi-incident handling.
- Demo data is synthetic, not a live SIEM feed.
- Threat-intel is stubbed (VirusTotal / AbuseIPDB-shaped fixtures), not live APIs.
