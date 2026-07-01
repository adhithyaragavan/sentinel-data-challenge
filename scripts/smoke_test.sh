#!/usr/bin/env bash
# Smoke test: verify NIM connectivity and Docker daemon before the demo.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

PY=".venv/bin/python"
if [ ! -x "$PY" ]; then
  echo "ERROR: virtualenv not found. Run: python3 -m venv .venv && .venv/bin/pip install -r requirements.txt"
  exit 1
fi

echo "==> Checking Docker daemon..."
if docker info >/dev/null 2>&1; then
  echo "Docker daemon reachable"
  # Pre-pull the sandbox image so the first detonation is fast
  docker pull python:3.11-slim >/dev/null 2>&1 && echo "Sandbox image python:3.11-slim ready"
else
  echo "WARNING: Docker daemon not running — Tool-Executor step will fail"
fi

echo ""
echo "==> Checking NIM connectivity..."
"$PY" nemoclaw.py

echo ""
echo "Smoke test complete."
