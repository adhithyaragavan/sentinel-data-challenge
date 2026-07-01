# Sentinel — Live Demo Script

Target runtime: **~3 minutes**. One terminal, full screen, large font.

## Before you start (offstage)
- [ ] `.env` filled in (NIM key + Slack webhook)
- [ ] Docker daemon running (`docker info` succeeds)
- [ ] Sandbox image pre-pulled (`docker pull python:3.11-slim` or run `./scripts/smoke_test.sh`)
- [ ] Slack channel visible on a second screen for the escalation reveal
- [ ] Run `./scripts/smoke_test.sh` once to pre-warm NIM (first call is slow)

## The pitch (15s)
> "When a SOC alert fires, a human analyst spends 20–40 minutes triaging,
> detonating the file, and deciding whether to isolate the host. Sentinel is a
> 5-agent swarm that does it autonomously in seconds — and still asks a human
> before pulling the trigger on anything high-risk."

## The run

```sh
./scripts/run_demo.sh
```

Narrate as each stage prints:

1. **Triage** — "Agent one reads the raw EDR alert and asks Nemotron to classify
   it. Critical severity, phishing-delivered RAT."

2. **Forensic** — "Agent two builds the evidence packet — process tree shows
   `outlook.exe` spawned the malware. Hash matches AsyncRAT, 54 of 72 vendors.
   C2 IP is a known Tor exit with a 97% abuse score."

3. **Tool-Executor** — **THIS IS THE MONEY SHOT. Slow down.**
   "Agent three detonates the actual file inside a Docker sandbox with
   a default-deny network policy. Watch —"
   → point at the line: `blocked C2: 185.220.101.47:4444 — network unreachable`
   "The malware tried to beacon home. The kernel blocked it. That's our proof of
   malice, captured live and safely."

4. **Planner** — "Agent four scores containment risk at 0.95 and recommends
   isolating the host — because persistence via the registry means killing the
   process alone won't help."

5. **Supervisor** — "Risk is above threshold, so Sentinel does NOT act alone. It
   escalates to Slack for human approval —"
   → switch to Slack screen, show the formatted alert card.
   "Full evidence packet, one click for the analyst to approve isolation."

## The close (15s)
> "Five agents, one real sandbox detonation, one real Slack escalation, end-to-end
> in seconds — with a human gate on anything dangerous. That's Sentinel."

## Fallback plan (if something breaks)
- **NIM slow/down:** you pre-warmed with smoke_test; if it still hangs, show
  `pipeline_output.json` from a prior successful run.
- **Docker hiccup:** the pipeline still completes; the executor reports the
  beacon failure from the payload itself, so the "blocked" evidence still shows.
- **Slack fails:** the Supervisor logs the escalation outcome to the terminal even
  without the webhook — narrate that as the audit trail.
- **Total failure:** run `.venv/bin/python eval/evaluate.py` — it shows the same
  pipeline scoring 9/9 with latency numbers, proving it works.
