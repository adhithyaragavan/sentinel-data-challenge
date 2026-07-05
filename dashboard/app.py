"""
app.py — Sentinel SOC triage dashboard (Streamlit).

Two tabs:
  1. Alert Queue    — the GPU-ranked risk queue (highest risk first). Click any
                      alert to see its evidence; if it was escalated and deep-dived
                      by the agent swarm, the rationale + IOC/sandbox evidence trail
                      is shown from the cached pipeline output.
  2. GPU Benchmark  — the cached CPU-vs-GPU acceleration chart + results table, with
                      an optional "re-run live" button.

Run:  streamlit run dashboard/app.py
"""
from __future__ import annotations

import json
import os
import subprocess
import sys

import pandas as pd
import streamlit as st

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
_DATA = os.path.join(_ROOT, "data")
_CACHE = os.path.join(_ROOT, "deep_dive_cache")
_BENCH = os.path.join(_ROOT, "benchmarks")
_QUEUE = os.path.join(_DATA, "ranked_queue.parquet")

BAND_COLOR = {"critical": "#e34948", "high": "#eb6834",
              "medium": "#eda100", "low": "#1baf7a"}

st.set_page_config(page_title="Sentinel — SOC Triage", page_icon="🛡️", layout="wide")


@st.cache_data
def load_queue() -> pd.DataFrame | None:
    if not os.path.exists(_QUEUE):
        return None
    return pd.read_parquet(_QUEUE)


def load_deep_dive(alert_id: str) -> dict | None:
    path = os.path.join(_CACHE, f"{alert_id}.json")
    if os.path.exists(path):
        with open(path) as f:
            return json.load(f)
    return None


def _badge(band: str) -> str:
    c = BAND_COLOR.get(band, "#52514e")
    return (f"<span style='background:{c};color:white;padding:2px 10px;"
            f"border-radius:10px;font-size:0.85em;font-weight:600'>{band.upper()}</span>")


# ============================ Alert Queue tab ============================
def queue_tab():
    q = load_queue()
    st.subheader("Ranked alert queue")
    st.caption("A SOC analyst can't read every alert. cudf.pandas cleans and cuML "
               "scores the whole stream on GPU; this queue puts the alerts that "
               "matter now at the top.")
    if q is None:
        st.warning("No ranked queue found. Generate it first:\n\n"
                   "```\npython ingest/generate_alerts.py --rows 10000\n"
                   "python clean/normalize.py --rows 10000\n"
                   "python analyze/score_model.py --rows 10000\n```")
        return

    n_esc = int(q["escalate"].sum()) if "escalate" in q else 0
    c1, c2, c3 = st.columns(3)
    c1.metric("Alerts scored", f"{len(q):,}")
    c2.metric("Escalated (risk ≥ 0.7)", f"{n_esc:,}")
    c3.metric("Top risk score", f"{q['risk_score'].max():.3f}")

    show_cols = ["rank", "risk_score", "band", "alert_type", "host_id",
                 "dest_ip", "severity_raw", "escalate", "alert_id"]
    show_cols = [c for c in show_cols if c in q.columns]
    view = q[show_cols].head(200)

    def _row_style(row):
        c = BAND_COLOR.get(row["band"], "")
        return [f"background-color:{c}22"] * len(row)  # faint band tint

    st.dataframe(
        view.style.apply(_row_style, axis=1)
             .format({"risk_score": "{:.3f}"}),
        width="stretch", hide_index=True, height=380,
    )

    st.divider()
    st.subheader("Inspect an alert")
    top = q.head(200)
    options = top["alert_id"].tolist()
    rank_by_id = dict(zip(top["alert_id"], top["rank"]))
    sel = st.selectbox("Select an alert (ranked order)", options,
                       format_func=lambda a: f"#{int(rank_by_id.get(a, 0))}  {a}")
    if sel:
        _detail(q[q.alert_id == sel].iloc[0])


def _detail(row):
    left, right = st.columns([1, 2])
    with left:
        st.markdown(f"**{row['alert_id']}**", unsafe_allow_html=True)
        st.markdown(_badge(row["band"]) +
                    f" &nbsp; risk **{row['risk_score']:.3f}** &nbsp; rank **#{int(row['rank'])}**",
                    unsafe_allow_html=True)
        for k in ("alert_type", "host_id", "dest_ip", "severity_raw", "file_hash", "timestamp"):
            if k in row and pd.notna(row[k]):
                st.write(f"**{k}**: {row[k]}")

    with right:
        dd = load_deep_dive(row["alert_id"])
        escalated = int(row.get("escalate", 0)) == 1
        if dd:
            _render_deep_dive(dd)
        elif escalated:
            st.info("This alert was escalated but has no cached agent-swarm deep-dive "
                    "yet. Run it on the demo box (needs NVIDIA NIM + Docker):\n\n"
                    f"```\npython analyze/run_deep_dive.py --top 5\n```")
        else:
            st.caption("Below the escalation threshold — no deep-dive. The GPU model "
                       "auto-triaged this as low priority.")


def _render_deep_dive(dd: dict):
    st.markdown("#### Agent swarm deep-dive")
    planner = dd.get("planner", {})
    supervisor = dd.get("supervisor", {})
    forensic = dd.get("forensic", {})
    executor = dd.get("executor", {})

    if supervisor:
        st.markdown(f"**Decision:** `{supervisor.get('decision','?')}` → "
                    f"action `{supervisor.get('action','?')}` on "
                    f"`{supervisor.get('target','?')}`")
    if planner.get("rationale"):
        st.markdown("**Rationale**")
        st.write(planner["rationale"])

    cols = st.columns(2)
    with cols[0]:
        iocs = forensic.get("iocs")
        if iocs:
            st.markdown("**IOCs**")
            st.json(iocs, expanded=False)
    with cols[1]:
        blocked = executor.get("blocked_connections")
        if blocked:
            st.markdown("**Sandbox — blocked C2 connections**")
            st.json(blocked, expanded=False)
        if executor.get("verdict"):
            st.markdown(f"**Sandbox verdict:** `{executor['verdict']}`")

    with st.expander("Full swarm output (JSON)"):
        st.json(dd, expanded=False)


# ============================ Benchmark tab ============================
def benchmark_tab():
    st.subheader("GPU acceleration proof")
    st.caption("The cleaning and model steps at 10K / 100K / 1M rows, CPU vs NVIDIA "
               "T4 GPU. Chart + numbers are cached to disk so this doesn't depend on "
               "live GPU access.")
    png = os.path.join(_BENCH, "cpu_vs_gpu.png")
    csv = os.path.join(_BENCH, "results.csv")

    if os.path.exists(png):
        st.image(png, width="stretch")
    else:
        st.warning("No cached chart yet. Run `python benchmarks/cpu_vs_gpu.py`.")

    if os.path.exists(csv):
        df = pd.read_csv(csv)
        gpu = df[df.backend == "gpu"]
        if not gpu.empty and gpu["speedup"].notna().any():
            st.metric("Best speedup", f"{gpu['speedup'].max():.1f}×")
        st.dataframe(df, width="stretch", hide_index=True)

    st.divider()
    if st.button("↻ Re-run benchmark live", help="Runs benchmarks/cpu_vs_gpu.py "
                 "(needs the T4 VM for GPU bars)"):
        with st.status("Running benchmark…", expanded=True) as status:
            proc = subprocess.run([sys.executable,
                                   os.path.join(_BENCH, "cpu_vs_gpu.py")],
                                  cwd=_ROOT, capture_output=True, text=True)
            st.code(proc.stdout[-3000:] or proc.stderr[-3000:])
            status.update(label="Done" if proc.returncode == 0 else "Failed",
                          state="complete" if proc.returncode == 0 else "error")
        st.cache_data.clear()
        st.rerun()


# ============================ Layout ============================
st.title("🛡️ Sentinel — GPU-accelerated SOC triage")
st.caption("Ingest → BigQuery → cudf.pandas clean → cuML rank → agent-swarm deep-dive")

tab1, tab2 = st.tabs(["Alert Queue", "GPU Benchmark"])
with tab1:
    queue_tab()
with tab2:
    benchmark_tab()
