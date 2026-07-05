# GCP Setup — from scratch

Sets up the **Cloud Storage + BigQuery** warehouse and the **T4 GPU VM** that runs
the whole Sentinel data-intelligence pipeline (RAPIDS + Docker swarm + Streamlit).
Only two GCP products are used — Cloud Storage and BigQuery — plus one Compute Engine
GPU VM. Nothing else (no GKE, Spark, Looker, Vertex).

> Everything below fits inside the GCP free-trial credit. The **only** real line item
> is the T4 VM ($0.35/hr) — stop it when idle. A **$50 budget alert is set before the
> VM is ever created** (step 4) so runaway spend is caught early.

Run these on your Mac (needs the `gcloud` CLI: https://cloud.google.com/sdk/docs/install).
Interactive auth steps must be run by you — in this Claude session, prefix them with `!`
so the output lands in the conversation (e.g. `! gcloud auth login`).

---

## 1. Install + authenticate the gcloud CLI

```bash
gcloud auth login                     # opens a browser; interactive — run yourself
gcloud auth application-default login # ADC for the Python client libs on this Mac
```

## 2. Create the project + enable billing / free trial

```bash
export PROJECT_ID="sentinel-data-challenge"        # must be globally unique; add a suffix if taken
gcloud projects create "$PROJECT_ID" --name="Sentinel Data Challenge"
gcloud config set project "$PROJECT_ID"

# Link a billing account (free-trial credits live here). List, then link:
gcloud billing accounts list
export BILLING_ACCOUNT_ID="XXXXXX-XXXXXX-XXXXXX"    # from the list above
gcloud billing projects link "$PROJECT_ID" --billing-account="$BILLING_ACCOUNT_ID"
```

## 3. Enable the (only) required APIs

```bash
gcloud services enable \
  storage.googleapis.com \
  bigquery.googleapis.com \
  compute.googleapis.com \
  cloudbilling.googleapis.com \
  billingbudgets.googleapis.com
```

## 4. Set a budget alert BEFORE provisioning anything billable

```bash
# ~$50 threshold with alerts at 50/90/100%. Catches runaway GPU spend early.
gcloud billing budgets create \
  --billing-account="$BILLING_ACCOUNT_ID" \
  --display-name="sentinel-budget-50" \
  --budget-amount=50USD \
  --threshold-rule=percent=0.5 \
  --threshold-rule=percent=0.9 \
  --threshold-rule=percent=1.0
```

## 5. Cloud Storage bucket (landing zone for raw synthetic alerts)

```bash
export REGION="us-central1"                         # best T4 availability + trial credit
export GCS_BUCKET="sentinel-alerts-${PROJECT_ID}"   # bucket names are global; keep unique
gcloud storage buckets create "gs://${GCS_BUCKET}" \
  --location="$REGION" --uniform-bucket-level-access
```

## 6. BigQuery dataset (raw alert warehouse)

```bash
export BQ_DATASET="sentinel"
bq --location="$REGION" mk --dataset "${PROJECT_ID}:${BQ_DATASET}"
# The alerts_raw table is created + loaded by ingest/generate_alerts.py.
```

## 7. Record config in .env

Copy `.env.example` → `.env` and fill:

```
GOOGLE_CLOUD_PROJECT=sentinel-data-challenge
GCS_BUCKET=sentinel-alerts-sentinel-data-challenge
BQ_DATASET=sentinel
BQ_TABLE=alerts_raw
```

## 8. Provision the T4 VM

The VM is where the GPU steps and the demo run. Provisioning is an **approval gate** —
see `infra/provision_vm.sh`. It carries the one real (trial-covered) cost, so the exact
`gcloud compute instances create ...` command is reviewed before it runs.

---

## Cost hygiene

- **Stop the VM when idle:** `gcloud compute instances stop sentinel-t4 --zone="$ZONE"`
  (you pay for the GPU only while it runs; the disk is a few cents/day).
- **Delete when done:** `gcloud compute instances delete sentinel-t4 --zone="$ZONE"`.
- The benchmark chart + CSV are cached in `benchmarks/` and committed, so the demo does
  **not** need the VM running. Re-provision only to re-run live.
