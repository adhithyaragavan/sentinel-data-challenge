"""
Shared schema + signal constants for the synthetic SOC alert dataset.

One row = one EDR alert. The generator (generate_alerts.py) produces a SUPERSET
schema: it carries the challenge-required columns (host_id, file_hash, dest_ip,
severity_raw, alert_type, nested process_tree) AND enough structure to reconstruct
a valid `mock_data/edr_alert.json`-shaped dict for the existing agent swarm
(see analyze/to_pipeline_alert.py). It also carries a planted ground-truth label
`label_escalate` so cuML/scikit-learn have real signal to learn.

Nothing here imports pandas/cudf — this is pure constants + BigQuery schema so it
loads on Mac and VM identically.
"""
from __future__ import annotations

# ---------------------------------------------------------------------------
# Categorical vocab
# ---------------------------------------------------------------------------

# alert_type -> (base escalation logit contribution, feature-likelihood profile).
# The profile drives how often malicious features co-occur with each type, so the
# flattened features are genuinely predictive rather than random.  Fields in the
# profile are P(known-bad hash), P(high-risk country), P(beacon port),
# P(unsigned child of office/browser), P(persistence run-key).
ALERT_TYPES: dict[str, dict] = {
    "malware_beacon":       {"logit": 2.4, "p_hash": 0.75, "p_country": 0.65, "p_port": 0.85, "p_unsigned": 0.70, "p_persist": 0.55},
    "cred_dump":            {"logit": 2.1, "p_hash": 0.65, "p_country": 0.35, "p_port": 0.30, "p_unsigned": 0.60, "p_persist": 0.45},
    "lateral_movement":     {"logit": 1.6, "p_hash": 0.45, "p_country": 0.40, "p_port": 0.50, "p_unsigned": 0.45, "p_persist": 0.35},
    "phishing_payload":     {"logit": 1.2, "p_hash": 0.55, "p_country": 0.45, "p_port": 0.40, "p_unsigned": 0.75, "p_persist": 0.40},
    "suspicious_powershell":{"logit": 0.8, "p_hash": 0.30, "p_country": 0.25, "p_port": 0.30, "p_unsigned": 0.35, "p_persist": 0.30},
    "policy_violation":     {"logit": -0.6,"p_hash": 0.08, "p_country": 0.10, "p_port": 0.08, "p_unsigned": 0.15, "p_persist": 0.10},
    "benign_admin":         {"logit": -2.2,"p_hash": 0.02, "p_country": 0.05, "p_port": 0.05, "p_unsigned": 0.08, "p_persist": 0.06},
    "benign_update":        {"logit": -2.6,"p_hash": 0.01, "p_country": 0.03, "p_port": 0.03, "p_unsigned": 0.04, "p_persist": 0.03},
}

# Ordinal encoding of alert_type by descending risk (higher = riskier). Used as a
# model feature by clean/normalize.py.
ALERT_TYPE_CODE: dict[str, int] = {
    t: i for i, t in enumerate(
        sorted(ALERT_TYPES, key=lambda k: ALERT_TYPES[k]["logit"], reverse=True))
}

# Relative frequency of each alert_type in the stream (benign dominates, like reality).
ALERT_TYPE_WEIGHTS: dict[str, float] = {
    "malware_beacon": 0.06, "cred_dump": 0.05, "lateral_movement": 0.06,
    "phishing_payload": 0.08, "suspicious_powershell": 0.10, "policy_violation": 0.15,
    "benign_admin": 0.24, "benign_update": 0.20,
}

# Latent-logit weights for the planted escalation signal.
SIGNAL_WEIGHTS = {
    "intercept": -6.0,     # tuned so mean(label_escalate) lands ~8-12%
    "hash": 1.6,
    "country": 1.1,
    "port": 1.0,
    "unsigned": 0.9,
    "persist": 1.2,
    "severity": 1.4,       # multiplies severity_norm in [0,1]
    "noise_sd": 0.6,       # Gaussian logit noise -> classes not perfectly separable
}
TARGET_POSITIVE_RATE = 0.10   # sanity target for label distribution

# Feature vocab used when a feature "fires".
HIGH_RISK_COUNTRIES = ["RU", "KP", "IR", "CN", "BY"]
LOW_RISK_COUNTRIES = ["US", "GB", "DE", "CA", "AU", "NL", "JP", "SE"]
BEACON_PORTS = [4444, 8080, 1337, 8443, 9001, 6667, 53]      # common C2 / covert ports
BENIGN_PORTS = [443, 80, 22, 3389, 445, 123]
OFFICE_BROWSER_PARENTS = ["outlook.exe", "winword.exe", "excel.exe", "chrome.exe", "firefox.exe", "msedge.exe"]
BENIGN_PARENTS = ["explorer.exe", "services.exe", "svchost.exe", "cmd.exe", "bash"]
OS_CHOICES = ["Windows 10 22H2", "Windows 11 23H2", "Windows Server 2019", "Ubuntu 22.04"]

# Known-malicious IOC pools. A "bad" hash/IP is drawn from these; benign ones are
# random. clean/normalize.py flags membership as is_known_bad_hash / is_bad_dest_ip.
KNOWN_BAD_HASHES = [f"bad{ i :060d}"[:64] for i in range(256)]          # 256-entry blocklist
KNOWN_BAD_IP_PREFIXES = ["185.220.101.", "45.153.160.", "193.42.33.", "91.219.236."]  # tor/bulletproof ranges

# MITRE techniques by alert_type (for the reconstructed pipeline alert).
MITRE_BY_TYPE = {
    "malware_beacon": ["T1071.001", "T1059", "T1547.001"],
    "cred_dump": ["T1003.001", "T1059"],
    "lateral_movement": ["T1021.001", "T1570"],
    "phishing_payload": ["T1566.001", "T1204.002"],
    "suspicious_powershell": ["T1059.001"],
    "policy_violation": ["T1078"],
    "benign_admin": [],
    "benign_update": [],
}

# ---------------------------------------------------------------------------
# Timestamp formats (mixed on purpose so cleaning has real normalization work)
# ---------------------------------------------------------------------------
TS_FORMAT_ISO_Z = "iso_z"          # 2024-06-30T14:22:31Z
TS_FORMAT_ISO_OFFSET = "iso_offset"  # 2024-06-30T10:22:31-04:00
TS_FORMAT_EPOCH = "epoch"          # 1719757351  (seconds)
TS_FORMATS = [TS_FORMAT_ISO_Z, TS_FORMAT_ISO_OFFSET, TS_FORMAT_EPOCH]

DUPLICATE_RATE = 0.05   # ~5% of rows reuse an earlier alert_id (dedup test)

# ---------------------------------------------------------------------------
# BigQuery schema. Scalar columns are typed for joins/aggregations on the raw
# warehouse; nested structures are JSON columns for fidelity. The cleaning step
# flattens the JSON columns into feature columns downstream.
# ---------------------------------------------------------------------------
def bigquery_schema():
    """Return a list of google.cloud.bigquery.SchemaField (imported lazily)."""
    from google.cloud import bigquery
    J = bigquery.SchemaField
    return [
        J("alert_id", "STRING", mode="REQUIRED"),
        J("timestamp", "STRING"),          # raw messy string, normalized in cleaning
        J("host_id", "STRING"),
        J("username", "STRING"),
        J("os", "STRING"),
        J("alert_type", "STRING"),
        J("severity_raw", "STRING"),       # mixed numeric/word, normalized in cleaning
        J("file_hash", "STRING"),
        J("dest_ip", "STRING"),
        J("dst_port", "INTEGER"),
        J("protocol", "STRING"),
        J("bytes_sent", "INTEGER"),
        J("country", "STRING"),
        J("rule_triggered", "STRING"),
        J("process_tree", "JSON"),         # nested {parent, child}
        J("file_events", "JSON"),          # list of {action, path}
        J("registry_events", "JSON"),      # list of {action, key, value, data}
        J("mitre_techniques", "JSON"),     # list[str]
        J("label_escalate", "INTEGER"),    # planted ground-truth training label
    ]

# Columns that are JSON-encoded structures (everything else is scalar).
JSON_COLUMNS = ["process_tree", "file_events", "registry_events", "mitre_techniques"]
