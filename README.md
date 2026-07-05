# Sentinel — GPU-Accelerated SOC Alert Triage

**NVIDIA Accelerated Data Intelligence Challenge submission.**

### 🔗 Live demo: **https://sentinel-dashboard-260796599985.us-central1.run.app**

The hosted dashboard (Google Cloud Run) serves a committed demo bundle: the top of a
**953,498-alert** GPU-ranked queue and **real** agent-swarm deep-dives, plus the real
CPU-vs-NVIDIA-T4 acceleration chart. (Scales to zero — the first visit may cold-start
for ~5–10s.)

A data-intelligence pipeline that helps a Security Operations Center (SOC) analyst
survive alert volume: it ingests a large stream of EDR alerts, cleans and scores the
**entire stream on GPU** to produce a ranked risk queue, and hands only the top alerts
to a 5-agent LLM swarm for autonomous deep-dive — surfacing everything in a dashboard.

> The 5-agent swarm ([`agents/`](agents/), [`pipeline.py`](pipeline.py)) was built for
> an earlier agentic-AI hackathon and is **reused unchanged** here as the deep-dive
> stage. This submission adds the data pipeline and the GPU acceleration around it.

---

## 1. The user and the problem

A **SOC analyst** faces tens of thousands of EDR alerts a day. The vast majority are
benign; a handful are a live intrusion. Reading them in arrival order means the one
that matters waits in a queue behind a thousand that don't. Sentinel answers one
question at scale: **which alerts matter right now?** It cleans and risk-scores the
whole stream, ranks it, and auto-triages the top of the queue with an agent swarm so
the analyst starts at rank #1 with an evidence trail already assembled.

---

## 2. Pipeline — ingest to visualize

```
  ingest/generate_alerts.py        synthetic EDR alerts @ 10K / 100K / 1M+ rows
        │                          (nested process_tree, messy timestamps, dup ids)
        ▼
  Cloud Storage  ───►  BigQuery    landing zone → warehouse (schema, joins, aggs)
        │
        ▼
  clean/normalize.py               GPU-accelerated cleaning via cudf.pandas
        │                          (flatten JSON, dedupe, normalize ts, features)
        │                          ── SAME code runs on pandas (CPU) for baseline ──
        ▼
  analyze/score_model.py           GPU-accelerated escalation model via cuML
        │                          (scikit-learn RandomForest = CPU baseline)
        ▼
  Ranked risk queue  ──►  analyze/run_deep_dive.py  ──►  pipeline.run()  [UNCHANGED swarm]
        │                          top-ranked alert → triage → forensic → sandbox
        │                          → planner → supervisor  (risk_score + rationale)
        ▼
  dashboard/app.py                 Streamlit: ranked queue + click-into rationale
                                   + the CPU-vs-GPU acceleration chart
```

| Stage | File | What it does |
|-------|------|--------------|
| Ingest | `ingest/generate_alerts.py`, `ingest/schema.py` | Generate synthetic alerts at scale with a planted ~10% escalation signal; upload to Cloud Storage; load BigQuery. |
| Clean | `clean/normalize.py` | Flatten nested `process_tree`, dedupe on `alert_id`, normalize mixed-format timestamps, engineer IOC/severity features. One `clean()` function, run on **CPU (pandas)** or **GPU (cudf.pandas)** via a one-line import swap; `--verify` diffs the two. |
| Model | `analyze/score_model.py` | Train an escalation classifier (**scikit-learn** CPU / **cuML** GPU), quote held-out AUC/F1/precision/recall, emit `risk_score ∈ [0,1]` and the ranked queue. |
| Deep-dive | `analyze/to_pipeline_alert.py`, `analyze/run_deep_dive.py` | Adapt the top-ranked alert to the swarm's schema and run the **unchanged** `pipeline.run()`; cache the rationale/evidence. |
| Visualize | `dashboard/app.py` | Streamlit ranked queue, per-alert drill-in with the swarm's rationale, and the benchmark chart. |
| Prove | `benchmarks/cpu_vs_gpu.py` | Time cleaning + model at 10K/100K/1M, CPU vs GPU; cache CSV + chart. |

The reused deep-dive swarm (each agent hands a JSON packet to the next):

| # | Agent | Responsibility | Real integration |
|---|-------|----------------|------------------|
| 1 | Triage | Classify severity, dedupe | NVIDIA NIM inference |
| 2 | Forensic Examiner | Evidence packet, enrich IOCs | NIM + threat-intel |
| 3 | Tool-Executor | Detonate file in isolated sandbox | **Docker** (`--network none`) |
| 4 | Remediation Planner | Action + `risk_score` + rationale | NIM inference |
| 5 | Supervisor | Auto-remediate or escalate | **Slack webhook** |

The GPU model's `risk_score` and the swarm's `risk_score` share one contract: `≥ 0.7 →
escalate`, with bands (`≥0.9 critical / ≥0.7 high / ≥0.5 medium / <0.5 low`) reused from
[`agents/planner.py`](agents/planner.py).

---

## 3. Acceleration proof

`benchmarks/cpu_vs_gpu.py` times the **cleaning** and **model** steps at 10K / 100K /
1M rows on CPU vs an NVIDIA T4, writes [`benchmarks/results.csv`](benchmarks/results.csv),
and renders [`benchmarks/cpu_vs_gpu.png`](benchmarks/cpu_vs_gpu.png). Results are **cached
to disk** so a demo never depends on live GPU access; the dashboard shows the cached
chart with a "re-run live" button.

Honesty guarantees baked in:
- **Same logic, both backends.** The CPU and GPU paths call the identical `clean()` and
  the same RandomForest hyperparameters — only the compute backend differs. `python
  clean/normalize.py --verify` diffs the two feature tables and asserts they're identical.
- **Quality, not just speed.** The model reports AUC/F1/precision/recall on a held-out
  stratified test split; CPU and GPU quality match within tolerance (same algorithm),
  so the ranking is demonstrably *useful*, not merely fast.
- **Cost-safe.** `python benchmarks/cpu_vs_gpu.py --smoke` runs the full path at ~1K rows
  first to catch bugs before any billed 10K/100K/1M sweep on the GPU.

```sh
python benchmarks/cpu_vs_gpu.py --smoke     # ~1K sanity check
python benchmarks/cpu_vs_gpu.py             # 10K/100K/1M sweep → cache CSV + chart
```

---

## 4. Tools used

- **Google Cloud:** Cloud Storage (landing zone) + BigQuery (warehouse) — *only these two*.
- **NVIDIA:** `cudf.pandas` (GPU cleaning) + cuML (GPU model), running on an **NVIDIA T4
  GPU on Google Compute Engine**.
- **NVIDIA NIM** cloud inference powers the reused deep-dive swarm (not counted as
  "acceleration").

See [`infra/setup_gcp.md`](infra/setup_gcp.md) for from-scratch GCP setup (project,
budget alert, bucket, dataset) and [`infra/provision_vm.sh`](infra/provision_vm.sh) for
the T4 VM.

---

## 5. Setup & running

### Local (CPU baseline + dashboard, no GPU)
```sh
python3.11 -m venv .venv
.venv/bin/pip install -r requirements.txt         # cross-platform stack (no RAPIDS)
cp .env.example .env                              # fill NIM key + GCP config

# generate → clean → score → dashboard, all on CPU
.venv/bin/python ingest/generate_alerts.py --rows 10000
.venv/bin/python clean/normalize.py --rows 10000
.venv/bin/python analyze/score_model.py --rows 10000
.venv/bin/streamlit run dashboard/app.py
```

### On the T4 VM (adds the GPU path)
```sh
bash infra/provision_vm.sh                         # prints the T4 create command (review)
bash infra/provision_vm.sh --yes                   # provision after approval
# on the VM:
pip install -r requirements.txt -r requirements-gpu.txt   # + cudf-cu12, cuml-cu12
python clean/normalize.py --rows 100000 --verify   # CPU vs GPU parity → PASS
python benchmarks/cpu_vs_gpu.py                    # real CPU-vs-GPU chart
python analyze/run_deep_dive.py --top 3            # swarm deep-dive (needs NIM + Docker)
```

### The reused single-alert swarm demo (unchanged)
```sh
./scripts/run_demo.sh          # runs pipeline.py on mock_data/edr_alert.json
.venv/bin/python eval/evaluate.py
```

---

## 6. Rubric mapping

| Deliverable | Where |
|-------------|-------|
| Real user + problem statement | §1 above |
| Data pipeline (ingest → visualize) | `ingest/` → Cloud Storage → BigQuery → `clean/` → `analyze/` → `dashboard/` |
| GPU cleaning (cudf.pandas, one-line swap + correctness diff) | `clean/normalize.py` (`--verify`) |
| GPU model (cuML vs scikit-learn, same benchmark) | `analyze/score_model.py` |
| Ranked queue → existing agents reused | `analyze/run_deep_dive.py` → `pipeline.run()` |
| Dashboard | `dashboard/app.py` |
| **Acceleration proof (CPU vs GPU, cached CSV + chart)** | `benchmarks/cpu_vs_gpu.py` |
| Tools: Cloud Storage + BigQuery; cudf.pandas + cuML | `ingest/`, `clean/`, `analyze/`, `infra/` |
| Unchanged: `agents/`, `eval/`, `mock_data/edr_alert.json`, `sandbox_policy/` | — |

---

## Project structure

```
ingest/          generate_alerts.py, schema.py     — synth dataset → GCS → BigQuery
clean/           normalize.py                        — CPU/GPU cleaning + parity check
analyze/         score_model.py, to_pipeline_alert.py, run_deep_dive.py
benchmarks/      cpu_vs_gpu.py, results.csv, cpu_vs_gpu.png   — acceleration proof (cached)
dashboard/       app.py                              — Streamlit queue + rationale + chart
infra/           setup_gcp.md, provision_vm.sh       — GCP + T4 VM
agents/          triage/forensic/executor/planner/supervisor   — UNCHANGED swarm
pipeline.py      end-to-end swarm orchestration      — UNCHANGED (reuse entry point)
eval/            latency + correctness harness        — UNCHANGED
mock_data/       edr_alert.json (swarm fallback), threat_intel.json   — UNCHANGED
sandbox_policy/  Docker default-deny network policy   — UNCHANGED
requirements.txt / requirements-gpu.txt              — CPU stack / RAPIDS (VM only)
```

## License

MIT — see [LICENSE](LICENSE).
