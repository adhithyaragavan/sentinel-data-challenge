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

Presentation note: the visual layer (hero stats, sidebar, WCAG-safe risk badges,
CSS) is display-only. It does not touch the pipeline logic, data, or GPU story —
all numbers come from the same cached artifacts (meta.json, results.csv, the queue).
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

# Demo bundle: a small committed queue + real swarm deep-dives, used when the live
# pipeline outputs aren't present (e.g. the Cloud Run deployment). SENTINEL_DEMO=1
# also hides GPU-only controls that can't run without the T4.
_DEMO = os.environ.get("SENTINEL_DEMO") == "1"
_DEMO_DIR = os.path.join(_HERE, "demo_data")
_DEMO_QUEUE = os.path.join(_DEMO_DIR, "ranked_queue.parquet")
_DEMO_CACHE = os.path.join(_DEMO_DIR, "deep_dive")
_DEMO_META = os.path.join(_DEMO_DIR, "meta.json")

# Risk-band palette. Fills + text colours are WCAG-AA (>=4.5:1) as a badge:
#   critical white-on-#d03b3b 4.80 | high #0b0b0b-on-#ec835a 7.46
#   medium   #0b0b0b-on-#fab219 10.7 | low  #0b0b0b-on-#0ca30c 5.87
BAND_COLOR = {"critical": "#d03b3b", "high": "#ec835a",
              "medium": "#fab219", "low": "#0ca30c"}
BAND_TEXT = {"critical": "#ffffff", "high": "#0b0b0b",
             "medium": "#0b0b0b", "low": "#0b0b0b"}
BAND_ORDER = ["critical", "high", "medium", "low"]

st.set_page_config(page_title="Sentinel — SOC Triage", page_icon="🛡️",
                   layout="wide", initial_sidebar_state="auto")


def _inject_css():
    """Presentation-only styling. Selectors target Streamlit 1.58 test-ids (pinned)."""
    st.markdown("""
    <style>
      /* reclaim above-the-fold space */
      .block-container { padding-top: 2.2rem; padding-bottom: 2rem; }
      /* hero stat tiles */
      [data-testid="stMetricValue"] {
        font-size: 2.3rem; font-weight: 700; font-variant-numeric: tabular-nums;
        line-height: 1.1;
      }
      [data-testid="stMetricLabel"] p { font-size: 0.82rem; color: #52514e; }
      /* tabs: bold + pill on the active tab, not colour alone */
      .stTabs [data-baseweb="tab"] { font-size: 1.02rem; padding: 0.35rem 0.9rem; }
      .stTabs [aria-selected="true"] {
        font-weight: 700; background: rgba(42,120,214,0.10); border-radius: 8px 8px 0 0;
      }
      /* risk badge / legend pill */
      .sentinel-badge {
        display: inline-block; padding: 2px 12px; border-radius: 999px;
        font-size: 0.82em; font-weight: 700; letter-spacing: 0.02em; white-space: nowrap;
      }
      /* narrow window / tablet: stack columns instead of cramping */
      @media (max-width: 640px) {
        [data-testid="column"] { flex: 1 1 100% !important; min-width: 100% !important; }
      }
    </style>
    """, unsafe_allow_html=True)


def _badge(band: str) -> str:
    bg = BAND_COLOR.get(band, "#52514e")
    fg = BAND_TEXT.get(band, "#ffffff")
    return (f"<span class='sentinel-badge' style='background:{bg};color:{fg}'>"
            f"{band.upper()}</span>")


def _using_demo() -> bool:
    """True when we're serving the committed demo bundle (no live queue present)."""
    return not os.path.exists(_QUEUE) and os.path.exists(_DEMO_QUEUE)


def _load_meta() -> dict:
    if os.path.exists(_DEMO_META):
        with open(_DEMO_META) as f:
            return json.load(f)
    return {}


@st.cache_data
def load_queue() -> pd.DataFrame | None:
    path = _QUEUE if os.path.exists(_QUEUE) else _DEMO_QUEUE
    if not os.path.exists(path):
        return None
    return pd.read_parquet(path)


@st.cache_data
def load_results() -> pd.DataFrame | None:
    csv = os.path.join(_BENCH, "results.csv")
    return pd.read_csv(csv) if os.path.exists(csv) else None


def _best_speedup() -> float | None:
    """Headline GPU speedup from the cached benchmark (max over all steps/sizes)."""
    df = load_results()
    if df is None or "speedup" not in df.columns:
        return None
    gpu = df[df.get("backend") == "gpu"] if "backend" in df.columns else df
    s = gpu["speedup"].dropna()
    return float(s.max()) if not s.empty else None


def load_deep_dive(alert_id: str) -> dict | None:
    for base in (_CACHE, _DEMO_CACHE):
        path = os.path.join(base, f"{alert_id}.json")
        if os.path.exists(path):
            with open(path) as f:
                return json.load(f)
    return None


# ============================ Sidebar (persistent context) ============================
def sidebar():
    with st.sidebar:
        st.markdown("## 🛡️ Sentinel")
        st.caption("GPU-accelerated SOC alert triage")
        st.markdown(
            "**Pipeline**  \nIngest → BigQuery → `cudf.pandas` clean → "
            "`cuML` rank → agent-swarm deep-dive")
        st.divider()
        st.markdown("**Risk bands**")
        legend = "".join(
            f"<div style='margin:3px 0'>{_badge(b)}</div>" for b in BAND_ORDER)
        st.markdown(legend, unsafe_allow_html=True)
        st.caption("Bands from the model's risk score (≥0.9 / ≥0.7 / ≥0.5 / <0.5). "
                   "risk ≥ 0.7 auto-escalates to the swarm.")
        st.divider()
        st.caption("Reading this: the queue ranks every alert by GPU-model risk; "
                   "click one to see the agent swarm's rationale and evidence.")


# ============================ Alert Queue tab ============================
def queue_tab():
    with st.spinner("Loading GPU-ranked queue…"):
        q = load_queue()
    if q is None:
        st.warning("No ranked queue found. Generate it first:\n\n"
                   "```\npython ingest/generate_alerts.py --rows 10000\n"
                   "python clean/normalize.py --rows 10000\n"
                   "python analyze/score_model.py --rows 10000\n```")
        return

    # Headline stats first (above the fold). Demo bundle shows the true full-run
    # totals from meta.json even though only the top slice ships.
    meta = _load_meta() if _using_demo() else {}
    total_scored = meta.get("total_scored", len(q))
    total_esc = meta.get("total_escalated",
                         int(q["escalate"].sum()) if "escalate" in q else 0)
    top_risk = meta.get("top_risk_score", float(q["risk_score"].max()))
    speedup = _best_speedup()

    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Alerts scored", f"{total_scored:,}")
    c2.metric("Escalated (risk ≥ 0.7)", f"{total_esc:,}")
    c3.metric("Top risk score", f"{top_risk:.3f}")
    c4.metric("GPU speedup", f"{speedup:.1f}×" if speedup else "—",
              help="Best CPU→GPU wall-clock speedup across the pipeline (GPU Benchmark tab).")

    st.markdown("#### Ranked alert queue")
    st.caption("A SOC analyst can't read every alert. `cudf.pandas` cleans and `cuML` "
               "scores the whole stream on GPU; this queue puts the alerts that matter "
               "now at the top.")
    if _using_demo():
        st.caption(f"Live demo bundle — top {len(q):,} of {total_scored:,} GPU-ranked "
                   "alerts. The full stream was scored on an NVIDIA T4 (see the GPU "
                   "Benchmark tab).")

    show_cols = ["rank", "risk_score", "band", "alert_type", "host_id",
                 "dest_ip", "severity_raw", "escalate", "alert_id"]
    show_cols = [c for c in show_cols if c in q.columns]
    view = q[show_cols].head(200).copy()
    if "escalate" in view.columns:
        view["escalate"] = view["escalate"].map({1: "⬆ escalate", 0: ""})

    def _row_style(row):
        c = BAND_COLOR.get(row["band"], "")
        return [f"background-color:{c}22"] * len(row)  # faint band tint

    st.dataframe(
        view.style.apply(_row_style, axis=1).format({"risk_score": "{:.3f}"}),
        width="stretch", hide_index=True, height=380,
        column_config={"severity_raw": st.column_config.TextColumn("severity (as-reported)")},
    )
    st.caption("Raw vendor severity is intentionally inconsistent "
               "(`critical` / `CRITICAL` / `5` / `high`) — the pipeline normalizes it "
               "during GPU cleaning. The **risk score** is the model's, not the vendor's.")

    st.divider()
    st.markdown("#### Inspect an alert")
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
                label = "severity (as-reported)" if k == "severity_raw" else k
                st.write(f"**{label}**: {row[k]}")

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
def _speedup_at(df, size, step):
    row = df[(df.get("backend") == "gpu") & (df["size"] == size) & (df["step"] == step)]
    s = row["speedup"].dropna() if not row.empty else pd.Series(dtype=float)
    return float(s.iloc[0]) if not s.empty else None


def benchmark_tab():
    st.markdown("#### GPU acceleration proof")
    st.caption("Cleaning and model steps at 10K / 100K / 1M rows, CPU vs NVIDIA T4 GPU. "
               "Chart + numbers are cached to disk so this doesn't depend on live GPU access.")

    df = load_results()

    # Hero speedup callout ABOVE the chart — the single most important evidence.
    if df is not None:
        best = _best_speedup()
        clean_1m = _speedup_at(df, 1_000_000, "clean")
        model_1m = _speedup_at(df, 1_000_000, "model")
        h1, h2, h3 = st.columns(3)
        h1.metric("Best speedup", f"{best:.1f}×" if best else "—")
        h2.metric("Cleaning @ 1M rows", f"{clean_1m:.1f}×" if clean_1m else "—")
        h3.metric("Model @ 1M rows", f"{model_1m:.1f}×" if model_1m else "—")
        st.caption("Same code, same model — only the compute backend differs. GPU pulls "
                   "ahead as the data scales; held-out AUC matches CPU (quality preserved).")

    png = os.path.join(_BENCH, "cpu_vs_gpu.png")
    if os.path.exists(png):
        st.image(png, width="stretch")
    else:
        st.warning("No cached chart yet. Run `python benchmarks/cpu_vs_gpu.py`.")

    if df is not None:
        with st.expander("Full results table"):
            st.dataframe(df, width="stretch", hide_index=True)

    if _DEMO:
        st.caption("Cached results from a real NVIDIA T4 run. The live re-run control is "
                   "disabled in the hosted demo (no GPU attached).")
        return

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
_inject_css()
sidebar()

st.title("🛡️ Sentinel — GPU-accelerated SOC triage")
_meta = _load_meta() if _using_demo() else {}
_scored = _meta.get("total_scored")
_spd = _best_speedup()
_bits = []
if _scored:
    _bits.append(f"**{_scored:,}** EDR alerts triaged")
if _spd:
    _bits.append(f"up to **{_spd:.1f}× faster** on GPU")
_bits.append("top threats auto-escalated to the agent swarm")
st.markdown(" • ".join(_bits))

tab1, tab2 = st.tabs(["📋 Alert Queue", "⚡ GPU Benchmark"])
with tab1:
    queue_tab()
with tab2:
    benchmark_tab()
