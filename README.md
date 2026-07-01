# Sentinel — Autonomous SOC Incident Response Swarm

A 5-agent swarm that autonomously triages a SOC alert, detonates a suspicious file
in an isolated sandbox, scores containment risk, and either auto-remediates or
escalates to a human via Slack.

Built for the **NVIDIA / gnani.ai / OpenACC Agentic AI Open Hackathon — Track A
(Agentic Workflows)**.

---

## Architecture

A linear pipeline where each agent hands a structured JSON packet to the next:

```
  EDR alert JSON
       │
       ▼
  ┌─────────────────┐   classify severity, dedupe
  │ 1. Triage       │──────────────────────────────► NVIDIA NIM (Nemotron)
  └─────────────────┘
       │
       ▼
  ┌─────────────────┐   IOCs, process tree, threat-intel enrichment
  │ 2. Forensic     │──────────────────────────────► NVIDIA NIM (Nemotron)
  └─────────────────┘
       │
       ▼
  ┌─────────────────┐   detonate file, capture blocked C2 beacon
  │ 3. Tool-Executor│──────────────────────────────► Docker sandbox
  └─────────────────┘      (default-deny network)
       │
       ▼
  ┌─────────────────┐   action + risk_score + rationale
  │ 4. Planner      │──────────────────────────────► NVIDIA NIM (Nemotron)
  └─────────────────┘
       │
       ▼
  ┌─────────────────┐   risk < threshold → auto-remediate
  │ 5. Supervisor   │   risk ≥ threshold → escalate ─► Slack webhook
  └─────────────────┘
```

| # | Agent | Responsibility | Real integration |
|---|-------|----------------|------------------|
| 1 | Triage | Classify severity, dedupe alerts | NIM inference |
| 2 | Forensic Examiner | Build evidence packet, enrich IOCs | NIM inference + threat-intel |
| 3 | Tool-Executor | Detonate file in isolated sandbox | **Docker** (real tool call) |
| 4 | Remediation Planner | Decide action + risk score | NIM inference |
| 5 | Supervisor | Auto-remediate or escalate | **Slack webhook** |

---

## Demo scenario

Phishing email → malicious attachment opened → malware drops and beacons to a C2 IP
→ EDR alert fires → the swarm runs end-to-end → host-isolation decision made within
seconds, with a full evidence trail. The money shot is step 3: the malware's
outbound C2 connection is **blocked live** inside the Docker sandbox.

---

## Stack

- **NVIDIA NIM** — cloud inference at `https://integrate.api.nvidia.com/v1`.
  Default model `nvidia/nemotron-3-nano-30b-a3b`.
- **Docker** — sandbox isolation for the detonation step (`--network none` blocks
  all outbound connections, `--read-only`, `--cap-drop ALL`).
- **Python 3** — agent logic and orchestration. Inference only, no training.

---

## Setup

### 1. Prerequisites
- Python 3.10+
- Docker Desktop running (`docker info` should succeed)

### 2. Install Python dependencies
```sh
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
```

### 3. Configure secrets
```sh
cp .env.example .env
# edit .env and fill in:
#   NVIDIA_NIM_API_KEY   — from build.nvidia.com
#   SLACK_WEBHOOK_URL    — your Slack incoming webhook
#   RISK_SCORE_THRESHOLD — default 0.7
```

### 4. Smoke-test NIM connectivity
```sh
./scripts/smoke_test.sh
```

---

## Running the demo

```sh
./scripts/run_demo.sh
```

This runs the full 5-agent pipeline against the synthetic EDR alert in
`mock_data/edr_alert.json` and writes the complete JSON trace to
`pipeline_output.json`.

---

## Evaluation

A small evaluation loop measures per-agent latency and decision correctness
against known ground truth:

```sh
.venv/bin/python eval/evaluate.py
```

Results are written to `eval/results.json`. See [`eval/`](eval/) for the metrics
and ground-truth fixtures.

---

## Project structure

```
agents/          one file per agent (triage, forensic, executor, planner, supervisor)
mock_data/       synthetic EDR alert + stubbed threat-intel
sandbox_policy/  Docker sandbox policy (default-deny network)
eval/            evaluation harness + ground truth
docs/            architecture + demo script
scripts/         run_demo.sh, smoke_test.sh
nemoclaw.py      NIM Privacy Router wrapper
pipeline.py      end-to-end orchestration
```

---

## Known limitations

- Docker sandbox is single-container — no concurrent multi-incident handling.
- Demo data is synthetic, not a live SIEM feed.

## License

MIT — see [LICENSE](LICENSE).
