"""
generate_alerts.py — synthesize a realistic SOC EDR alert dataset at scale.

Produces N alerts with a planted escalation signal (~8-12% positive), messy
timestamps (3 formats), ~5% duplicate alert_ids, and nested process_tree /
file_events / registry_events JSON. Writes local NDJSON (+ parquet for fast
downstream/benchmark reads); optionally uploads NDJSON to Cloud Storage and
loads it into BigQuery.

Usage:
    python ingest/generate_alerts.py --rows 10000
    python ingest/generate_alerts.py --rows 1000000 --seed 7
    python ingest/generate_alerts.py --rows 100000 --upload   # -> GCS + BigQuery

Local-only by default (no GCP creds needed) so the whole CPU pipeline runs
offline on the Mac; `--upload` exercises the Cloud Storage -> BigQuery warehouse
path on the VM (or anywhere ADC is configured).
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time

import numpy as np

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
sys.path.insert(0, _ROOT)

from ingest import schema as S  # noqa: E402

_DATA_DIR = os.path.join(_ROOT, "data")

# suspicious vs benign child process name pools
_MAL_CHILD = ["invoice_june.pdf.exe", "svchost32.exe", "update_helper.exe",
              "rundll32_x.exe", "wscript_host.exe", "mshta_loader.exe"]
_BENIGN_CHILD = ["chrome_update.exe", "OneDrive.exe", "Teams.exe", "python.exe",
                 "MsMpEng.exe", "gupdate.exe"]
_USERS = ["jsmith", "adoe", "mchen", "rpatel", "svc_backup", "administrator",
          "kjohnson", "lgarcia"]
_SIGNERS = ["Microsoft Windows", "Google LLC", "Adobe Inc.", "Mozilla Corporation"]


def _sigmoid(x):
    return 1.0 / (1.0 + np.exp(-x))


def generate(n: int, seed: int = 42) -> tuple[list[dict], dict]:
    """Return (rows, stats). rows are plain dicts matching the BigQuery schema."""
    rng = np.random.default_rng(seed)

    types = list(S.ALERT_TYPES.keys())
    tw = np.array([S.ALERT_TYPE_WEIGHTS[t] for t in types], dtype=float)
    tw /= tw.sum()
    tidx = rng.choice(len(types), size=n, p=tw)

    # Per-row feature-likelihood profiles pulled from the chosen alert_type.
    def prof(field):
        arr = np.array([S.ALERT_TYPES[t][field] for t in types])
        return arr[tidx]

    p_hash, p_country = prof("p_hash"), prof("p_country")
    p_port, p_unsigned, p_persist = prof("p_port"), prof("p_unsigned"), prof("p_persist")
    alert_logit = np.array([S.ALERT_TYPES[t]["logit"] for t in types])[tidx]

    # Feature fires (booleans), correlated with alert_type via the profiles.
    is_bad_hash = rng.random(n) < p_hash
    is_bad_country = rng.random(n) < p_country
    is_beacon_port = rng.random(n) < p_port
    is_unsigned = rng.random(n) < p_unsigned
    is_persist = rng.random(n) < p_persist

    # Severity propensity -> level 0..3, correlated with risk + noise.
    sev_prop = _sigmoid(0.8 * alert_logit
                        + 0.6 * (is_bad_hash + is_beacon_port + is_bad_country)
                        + rng.normal(0, 0.7, n))
    sev_level = np.clip((sev_prop * 4).astype(int), 0, 3)   # 0=low..3=critical
    sev_norm = sev_level / 3.0

    # Planted escalation label from a latent logit (+ Gaussian noise).
    W = S.SIGNAL_WEIGHTS
    latent = (W["intercept"]
              + W["hash"] * is_bad_hash
              + W["country"] * is_bad_country
              + W["port"] * is_beacon_port
              + W["unsigned"] * is_unsigned
              + W["persist"] * is_persist
              + W["severity"] * sev_norm
              + 0.5 * alert_logit
              + rng.normal(0, W["noise_sd"], n))
    p_esc = _sigmoid(latent)
    label = (rng.random(n) < p_esc).astype(int)

    # Scalar column arrays.
    dst_port = np.where(is_beacon_port,
                        rng.choice(S.BEACON_PORTS, n),
                        rng.choice(S.BENIGN_PORTS, n))
    protocol = np.where(rng.random(n) < 0.85, "TCP", "UDP")
    bytes_sent = rng.lognormal(7.0, 1.3, n).astype(int)
    country = np.where(is_bad_country,
                       rng.choice(S.HIGH_RISK_COUNTRIES, n),
                       rng.choice(S.LOW_RISK_COUNTRIES, n))
    os_arr = rng.choice(S.OS_CHOICES, n)
    users = rng.choice(_USERS, n)
    host_kind = np.where(rng.random(n) < 0.7, "WORKSTATION", "SERVER")
    host_num = rng.integers(1, 250, n)
    pid = rng.integers(1000, 9000, n)
    ppid = rng.integers(200, 999, n)

    # file_hash: blocklist member if bad, else random hex.
    bad_hash_pick = rng.choice(S.KNOWN_BAD_HASHES, n)
    rand_hash = np.array(["%064x" % v for v in rng.integers(0, 2**63, n)])
    file_hash = np.where(is_bad_hash, bad_hash_pick, rand_hash)

    # dest_ip: bad prefix when high-risk country, else random public-ish.
    bad_prefix = rng.choice(S.KNOWN_BAD_IP_PREFIXES, n)
    bad_ip = np.array([f"{pfx}{o}" for pfx, o in zip(bad_prefix, rng.integers(1, 254, n))])
    benign_ip = np.array([f"{a}.{b}.{c}.{d}" for a, b, c, d in
                          zip(rng.integers(11, 223, n), rng.integers(0, 255, n),
                              rng.integers(0, 255, n), rng.integers(1, 254, n))])
    dest_ip = np.where(is_bad_country, bad_ip, benign_ip)

    # severity_raw string (numeric or word form, occasionally uppercased).
    words = np.array(["low", "medium", "high", "critical"])
    nums = np.array(["1", "2", "4", "5"])
    use_word = rng.random(n) < 0.5
    sev_raw = np.where(use_word, words[sev_level], nums[sev_level])
    upper = rng.random(n) < 0.15
    sev_raw = np.array([s.upper() if u and s.isalpha() else s for s, u in zip(sev_raw, upper)])

    # child/parent names.
    parent_name = np.where(is_unsigned,
                           rng.choice(S.OFFICE_BROWSER_PARENTS, n),
                           rng.choice(S.BENIGN_PARENTS, n))
    child_name = np.where(rng.random(n) < np.where(label == 1, 0.8, 0.3),
                          rng.choice(_MAL_CHILD, n), rng.choice(_BENIGN_CHILD, n))
    signed = ~is_unsigned & (rng.random(n) < 0.9)   # unsigned mostly, some benign noise
    signer_pick = rng.choice(_SIGNERS, n)

    # timestamps: base spread over ~30 days, rendered in mixed formats.
    base = 1719705600  # 2024-06-30T00:00:00Z (epoch secs)
    ts_epoch = base + rng.integers(0, 30 * 86400, n)
    ts_fmt = rng.choice(S.TS_FORMATS, n)
    tz_off = rng.choice([-8, -5, -4, 0, 1, 5], n)   # for offset format

    # unique alert_ids, then inject ~5% duplicates.
    alert_ids = np.array([f"EDR-2024-{i:07d}" for i in range(n)])
    n_dup = int(n * S.DUPLICATE_RATE)
    if n_dup > 0 and n > 1:
        dup_targets = rng.integers(0, n, n_dup)         # rows to overwrite
        dup_sources = rng.integers(0, n, n_dup)         # ids to copy
        alert_ids[dup_targets] = alert_ids[dup_sources]

    types_arr = np.array(types)[tidx]

    rows: list[dict] = []
    for i in range(n):
        ts = _render_ts(int(ts_epoch[i]), ts_fmt[i], int(tz_off[i]))
        atype = types_arr[i]
        child = {
            "name": str(child_name[i]),
            "pid": int(pid[i]),
            "path": f"C:\\Users\\{users[i]}\\AppData\\Local\\Temp\\{child_name[i]}",
            "command_line": f"C:\\Users\\{users[i]}\\AppData\\Local\\Temp\\{child_name[i]} --silent",
            "sha256": str(file_hash[i]),
            "signed": bool(signed[i]),
            "signer": (str(signer_pick[i]) if signed[i] else None),
        }
        parent = {"name": str(parent_name[i]), "pid": int(ppid[i])}
        reg_events = []
        if is_persist[i]:
            reg_events.append({
                "action": "SET",
                "key": "HKCU\\Software\\Microsoft\\Windows\\CurrentVersion\\Run",
                "value": child_name[i].split(".")[0],
                "data": child["path"],
            })
        file_events = [{"action": "CREATE", "path": child["path"]}]
        if is_persist[i]:
            file_events.append({
                "action": "CREATE",
                "path": f"C:\\Users\\{users[i]}\\AppData\\Roaming\\Microsoft\\Windows"
                        f"\\Start Menu\\Programs\\Startup\\{child_name[i]}",
            })

        rows.append({
            "alert_id": str(alert_ids[i]),
            "timestamp": ts,
            "host_id": f"{host_kind[i]}-{int(host_num[i]):03d}",
            "username": str(users[i]),
            "os": str(os_arr[i]),
            "alert_type": str(atype),
            "severity_raw": str(sev_raw[i]),
            "file_hash": str(file_hash[i]),
            "dest_ip": str(dest_ip[i]),
            "dst_port": int(dst_port[i]),
            "protocol": str(protocol[i]),
            "bytes_sent": int(bytes_sent[i]),
            "country": str(country[i]),
            "rule_triggered": _rule_for(atype),
            "process_tree": {"parent": parent, "child": child},
            "file_events": file_events,
            "registry_events": reg_events,
            "mitre_techniques": S.MITRE_BY_TYPE.get(atype, []),
            "label_escalate": int(label[i]),
        })

    stats = {
        "rows": n,
        "positive_rate": round(float(label.mean()), 4),
        "n_positive": int(label.sum()),
        "n_duplicate_ids": int(n - len(set(alert_ids))),
        "alert_type_counts": {t: int((types_arr == t).sum()) for t in types},
        "severity_level_counts": {int(k): int(v) for k, v in
                                  zip(*np.unique(sev_level, return_counts=True))},
    }
    return rows, stats


def _render_ts(epoch: int, fmt: str, tz_off_hours: int) -> str:
    import datetime as dt
    if fmt == S.TS_FORMAT_EPOCH:
        return str(epoch)
    utc = dt.datetime.fromtimestamp(epoch, tz=dt.timezone.utc)
    if fmt == S.TS_FORMAT_ISO_Z:
        return utc.strftime("%Y-%m-%dT%H:%M:%SZ")
    # iso with offset
    tz = dt.timezone(dt.timedelta(hours=tz_off_hours))
    local = utc.astimezone(tz)
    return local.strftime("%Y-%m-%dT%H:%M:%S%z")[:-2] + ":" + local.strftime("%z")[-2:]


def _rule_for(atype: str) -> str:
    return {
        "malware_beacon": "C2_BEACON_DETECTED",
        "cred_dump": "LSASS_MEMORY_ACCESS",
        "lateral_movement": "REMOTE_SERVICE_CREATION",
        "phishing_payload": "SUSPICIOUS_CHILD_PROCESS_OUTLOOK",
        "suspicious_powershell": "ENCODED_POWERSHELL_COMMAND",
        "policy_violation": "UNAPPROVED_SOFTWARE",
        "benign_admin": "ADMIN_TOOL_USAGE",
        "benign_update": "SCHEDULED_UPDATE",
    }.get(atype, "GENERIC_ALERT")


# ---------------------------------------------------------------------------
# Output: NDJSON + parquet, optional GCS upload + BigQuery load
# ---------------------------------------------------------------------------

def write_ndjson(rows: list[dict], path: str) -> None:
    with open(path, "w") as f:
        for r in rows:
            f.write(json.dumps(r) + "\n")


def write_parquet(rows: list[dict], path: str) -> None:
    """Scalar columns kept typed; JSON columns stored as JSON strings for fast reads."""
    import pandas as pd
    flat = []
    for r in rows:
        rr = dict(r)
        for c in S.JSON_COLUMNS:
            rr[c] = json.dumps(rr[c])
        flat.append(rr)
    pd.DataFrame(flat).to_parquet(path, index=False)


def upload_and_load(ndjson_path: str, project: str, bucket: str,
                    dataset: str, table: str) -> str:
    """Upload NDJSON to GCS and load it into BigQuery. Returns the table id."""
    from google.cloud import storage, bigquery

    blob_name = f"raw/{os.path.basename(ndjson_path)}"
    gcs = storage.Client(project=project)
    gcs.bucket(bucket).blob(blob_name).upload_from_filename(ndjson_path)
    uri = f"gs://{bucket}/{blob_name}"
    print(f"  uploaded -> {uri}")

    bq = bigquery.Client(project=project)
    table_id = f"{project}.{dataset}.{table}"
    job_config = bigquery.LoadJobConfig(
        schema=S.bigquery_schema(),
        source_format=bigquery.SourceFormat.NEWLINE_DELIMITED_JSON,
        write_disposition=bigquery.WriteDisposition.WRITE_TRUNCATE,
    )
    bq.load_table_from_uri(uri, table_id, job_config=job_config).result()
    print(f"  loaded -> {table_id}")
    return table_id


def main(argv=None):
    ap = argparse.ArgumentParser(description="Generate synthetic SOC alert dataset")
    ap.add_argument("--rows", type=int, default=10_000)
    ap.add_argument("--seed", type=int, default=42)
    ap.add_argument("--out-dir", default=_DATA_DIR)
    ap.add_argument("--no-parquet", action="store_true", help="skip parquet output")
    ap.add_argument("--upload", action="store_true",
                    help="upload NDJSON to GCS and load BigQuery (needs ADC)")
    args = ap.parse_args(argv)

    os.makedirs(args.out_dir, exist_ok=True)
    t0 = time.time()
    rows, stats = generate(args.rows, seed=args.seed)
    gen_s = round(time.time() - t0, 2)

    tag = f"alerts_{args.rows}"
    ndjson_path = os.path.join(args.out_dir, f"{tag}.ndjson")
    write_ndjson(rows, ndjson_path)
    parquet_path = os.path.join(args.out_dir, f"{tag}.parquet")
    if not args.no_parquet:
        write_parquet(rows, parquet_path)

    _print_summary(stats, gen_s, ndjson_path,
                   None if args.no_parquet else parquet_path)

    if args.upload:
        project = os.environ["GOOGLE_CLOUD_PROJECT"]
        bucket = os.environ["GCS_BUCKET"]
        dataset = os.environ.get("BQ_DATASET", "sentinel")
        table = os.environ.get("BQ_TABLE", "alerts_raw")
        print("\nUploading to Cloud Storage + BigQuery...")
        upload_and_load(ndjson_path, project, bucket, dataset, table)

    return stats


def _print_summary(stats, gen_s, ndjson_path, parquet_path):
    bar = "=" * 60
    print(bar)
    print(f" SYNTHETIC ALERT DATASET  ({stats['rows']:,} rows, gen {gen_s}s)")
    print(bar)
    pr = stats["positive_rate"]
    flag = "OK" if S.TARGET_POSITIVE_RATE * 0.6 <= pr <= S.TARGET_POSITIVE_RATE * 1.6 else "CHECK"
    print(f" escalate positive rate : {pr:.3f}  ({stats['n_positive']:,})  [{flag}]")
    print(f" duplicate alert_ids     : {stats['n_duplicate_ids']:,}")
    print(f" NDJSON                  : {ndjson_path}")
    if parquet_path:
        print(f" parquet                 : {parquet_path}")
    print(" alert_type distribution :")
    for t, c in sorted(stats["alert_type_counts"].items(), key=lambda kv: -kv[1]):
        print(f"    {t:22s} {c:>10,}")
    print(bar)


if __name__ == "__main__":
    main()
