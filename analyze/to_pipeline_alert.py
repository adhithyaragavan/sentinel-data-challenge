"""
to_pipeline_alert.py — adapt a dataset row to the existing swarm's alert schema.

The generated superset row carries a nested `process_tree` plus the challenge fields
(host_id, file_hash, dest_ip, ...). The existing swarm (`pipeline.run`, pipeline.py:55)
expects the richer `mock_data/edr_alert.json` shape. This maps one -> the other so the
top-ranked alert flows into the UNCHANGED agent files. Nested fields may arrive as dicts
(from generate_alerts) or JSON strings (from a parquet/BigQuery read) — both handled.
"""
from __future__ import annotations

import datetime as dt
import json
from typing import Any


def _as_obj(v: Any):
    return json.loads(v) if isinstance(v, str) else v


def _iso(ts: str) -> str:
    """Normalize any of the generator's timestamp formats to ISO-8601 Z."""
    s = str(ts)
    if s.isdigit():
        return dt.datetime.fromtimestamp(int(s), tz=dt.timezone.utc).strftime(
            "%Y-%m-%dT%H:%M:%SZ")
    try:
        d = dt.datetime.fromisoformat(s.replace("Z", "+00:00"))
        return d.astimezone(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    except ValueError:
        return s


def to_pipeline_alert(row: dict) -> dict:
    """Dataset row (dict) -> edr_alert.json-shaped dict for pipeline.run()."""
    pt = _as_obj(row.get("process_tree")) or {}
    parent = pt.get("parent", {}) or {}
    child = pt.get("child", {}) or {}

    return {
        "alert_id": row["alert_id"],
        "timestamp": _iso(row.get("timestamp", "")),
        "hostname": row.get("host_id"),
        "username": row.get("username"),
        "os": row.get("os"),
        "process": {
            "name": child.get("name"),
            "pid": child.get("pid"),
            "parent_name": parent.get("name"),
            "parent_pid": parent.get("pid"),
            "path": child.get("path"),
            "command_line": child.get("command_line"),
            "sha256": child.get("sha256", row.get("file_hash")),
            "signed": child.get("signed"),
            "signer": child.get("signer"),
        },
        "network": {
            "outbound_connections": [{
                "dst_ip": row.get("dest_ip"),
                "dst_port": row.get("dst_port"),
                "protocol": row.get("protocol"),
                "bytes_sent": row.get("bytes_sent"),
                "country": row.get("country"),
            }],
        },
        "file_events": _as_obj(row.get("file_events")) or [],
        "registry_events": _as_obj(row.get("registry_events")) or [],
        "rule_triggered": row.get("rule_triggered"),
        "mitre_techniques": _as_obj(row.get("mitre_techniques")) or [],
    }
