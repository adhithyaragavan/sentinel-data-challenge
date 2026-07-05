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
# The Budgets API rejects an amount whose currency doesn't match the billing
# account's own currency (INVALID_ARGUMENT, no useful message) — check first:
gcloud billing accounts describe "$BILLING_ACCOUNT_ID" --format="value(currencyCode)"

# ~$50 threshold (converted to the billing account's currency) at 50/90/100%.
gcloud billing budgets create \
  --billing-account="$BILLING_ACCOUNT_ID" \
  --display-name="sentinel-budget-50" \
  --budget-amount=50USD \
  --threshold-rule=percent=0.5 \
  --threshold-rule=percent=0.9 \
  --threshold-rule=percent=1.0
# ^ if currencyCode above isn't USD, replace 50USD with the equivalent amount
#   in that currency, e.g. --budget-amount=4150INR for an INR-billed account.
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

Two gotchas hit while building this, in order — expect to hit the first, maybe the second:

1. **`GPUS_ALL_REGIONS` project quota is 0 by default**, even though per-region quotas
   (e.g. `NVIDIA_T4_GPUS` in `us-central1`) show `limit=1`. That per-region number is a
   template ceiling, not a grant — the global 0 blocks provisioning everywhere until
   raised. Fix (often auto-approved in seconds for a request this small):
   ```bash
   gcloud services enable cloudquotas.googleapis.com --project="$PROJECT_ID"
   gcloud components install alpha
   gcloud alpha quotas preferences create \
     --service=compute.googleapis.com --project="$PROJECT_ID" \
     --quota-id=GPUS-ALL-REGIONS-per-project --preferred-value=1 \
     --justification="Single T4 for a GPU-acceleration benchmark, stopped when idle." \
     --preference-id="${PROJECT_ID}-gpus-all-regions"
   gcloud alpha quotas preferences describe "${PROJECT_ID}-gpus-all-regions" \
     --project="$PROJECT_ID" --format="value(quotaConfig.grantedValue)"
   ```
   If instead this comes back ineligible or rejected, the fallback is upgrading the
   billing account from free-trial to a paid account (Console → Billing → Overview →
   "Upgrade") — trial accounts are frequently blocked from GPU quota by policy.

2. **T4 capacity is genuinely exhausted in specific zones** (`ZONE_RESOURCE_POOL_EXHAUSTED`)
   — a real, transient shortage, independent of quota. `infra/provision_vm.sh` defaults
   to `us-east1-c` (worked at build time); if that zone is also exhausted, retry with
   `ZONE=<zone> bash infra/provision_vm.sh --yes` against another candidate
   (`us-central1-a/b/c/f`, `us-west1-a/b`, `europe-west4-a/b`, `asia-southeast1-a/b`).

---

## Cost hygiene

- **Stop the VM when idle:** `gcloud compute instances stop sentinel-t4 --zone="$ZONE"`
  (you pay for the GPU only while it runs; the disk is a few cents/day).
- **Delete when done:** `gcloud compute instances delete sentinel-t4 --zone="$ZONE"`.
- The benchmark chart + CSV are cached in `benchmarks/` and committed, so the demo does
  **not** need the VM running. Re-provision only to re-run live.
