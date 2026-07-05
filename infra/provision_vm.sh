#!/usr/bin/env bash
# =====================================================================
# provision_vm.sh — create the NVIDIA T4 GPU VM that runs the whole
# Sentinel data-intelligence pipeline (RAPIDS + Docker swarm + Streamlit).
#
# APPROVAL GATE: this is the ONE line item with real (trial-credit-covered)
# cost (~$0.35/hr for the T4). By default this script only PRINTS the
# create command and exits — it does NOT provision. Re-run with --yes to
# actually create the VM after you've reviewed the command.
#
# Prereqs: infra/setup_gcp.md steps 1-6 done (project, billing, budget
# alert, APIs, bucket, dataset). Run on your Mac.
# =====================================================================
set -euo pipefail

PROJECT_ID="${GOOGLE_CLOUD_PROJECT:-sentinel-data-challenge}"
ZONE="${ZONE:-us-east1-c}"             # T4 capacity is volatile; us-central1-a/b/c/f
                                        # were ZONE_RESOURCE_POOL_EXHAUSTED at build time.
                                        # If this zone is also exhausted, retry with e.g.
                                        # ZONE=us-west1-a, europe-west4-a, asia-southeast1-b.
INSTANCE="${INSTANCE:-sentinel-t4}"
MACHINE_TYPE="${MACHINE_TYPE:-n1-standard-8}"   # 8 vCPU / 30GB — fair CPU baseline vs T4
BOOT_DISK_GB="${BOOT_DISK_GB:-150}"

# Deep Learning VM image: CUDA 12.x + NVIDIA driver preinstalled, so we only
# add RAPIDS + Docker on top. (Debian-11 cu123 families were retired; current
# images are Ubuntu 22.04/24.04. List available families with:
#   gcloud compute images list --project=deeplearning-platform-release \
#     --format="value(family)" | sort -u | grep common)
IMAGE_FAMILY="${IMAGE_FAMILY:-common-cu129-ubuntu-2204-nvidia-580}"
IMAGE_PROJECT="deeplearning-platform-release"

read -r -d '' CREATE_CMD <<CMD || true
gcloud compute instances create ${INSTANCE} \\
  --project=${PROJECT_ID} \\
  --zone=${ZONE} \\
  --machine-type=${MACHINE_TYPE} \\
  --accelerator=type=nvidia-tesla-t4,count=1 \\
  --image-family=${IMAGE_FAMILY} \\
  --image-project=${IMAGE_PROJECT} \\
  --boot-disk-size=${BOOT_DISK_GB}GB \\
  --maintenance-policy=TERMINATE \\
  --metadata=install-nvidia-driver=True \\
  --scopes=cloud-platform
CMD

echo "======================================================================"
echo " T4 VM provisioning command (APPROVAL GATE — review before running):"
echo "======================================================================"
echo "$CREATE_CMD"
echo "======================================================================"
echo " Cost: ~\$0.35/hr while RUNNING. Stop when idle:"
echo "   gcloud compute instances stop ${INSTANCE} --zone=${ZONE}"
echo "======================================================================"

if [[ "${1:-}" != "--yes" ]]; then
  echo
  echo "Dry run. Re-run with '--yes' to actually create the VM:"
  echo "   bash infra/provision_vm.sh --yes"
  exit 0
fi

echo "Creating VM..."
eval "$CREATE_CMD"

cat <<'POST'

======================================================================
 VM created. Next, on the VM (SSH in):
   gcloud compute ssh sentinel-t4 --zone=us-central1-a

 Then set it up:
   # 1. Clone the repo
   git clone <this-repo-url> sentinel && cd sentinel

   # 2. Python deps — CPU stack + RAPIDS GPU stack
   #    (this image needs python3-venv installed first; not preinstalled)
   sudo apt-get update -qq && sudo apt-get install -y -qq python3.10-venv docker.io
   python3 -m venv .venv && source .venv/bin/activate
   pip install -r requirements.txt
   pip install -r requirements-gpu.txt   # cudf-cu12 + cuml-cu12

   # 3. Verify GPU stack imports
   python -c "import cudf, cuml; print('RAPIDS OK', cudf.__version__, cuml.__version__)"

   # 4. Docker is NOT preinstalled on this image family — enable it, add
   #    yourself to the docker group (needs a new shell/session to take
   #    effect), then pre-pull the sandbox base image:
   sudo systemctl enable --now docker
   sudo usermod -aG docker "$USER"     # log out/in (or new SSH session) after this
   docker info >/dev/null && docker pull python:3.11-slim

   # 5. Copy .env (fill NIM key, GCS_BUCKET, BQ_* — see infra/setup_gcp.md step 7)
   cp .env.example .env && nano .env

 The VM's attached service account (--scopes=cloud-platform) provides ADC,
 so leave GOOGLE_APPLICATION_CREDENTIALS unset — the GCS/BigQuery clients
 authenticate automatically.
======================================================================
POST
