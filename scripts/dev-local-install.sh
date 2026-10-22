#!/usr/bin/env bash
# dev-local-install.sh — install every agent's dependencies into the active
# Python environment, so scripts/dev-local.sh can launch all 6 processes in
# one venv. Run this once (or after a dep bump).
#
# Usage:
#   source .venv/bin/activate   # recommended — install into a venv, not system python
#   scripts/dev-local-install.sh

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"

if [ -z "${VIRTUAL_ENV:-}" ]; then
  echo "WARNING: no VIRTUAL_ENV is active. Installing into $(python3 -c 'import sys; print(sys.prefix)')"
  echo "Prefer: python3 -m venv .venv && source .venv/bin/activate && scripts/dev-local-install.sh"
  sleep 2
fi

python3 -m pip install -U pip

# aws-opentelemetry-distro provides `opentelemetry-instrument` (the wrapper
# dev-local.sh exec's) and the AWS auto-config for FastAPI/boto/requests.
python3 -m pip install -U \
  -r agents/orchestrator/requirements.txt \
  -r agents/flights/requirements.txt \
  -r agents/hotels/requirements.txt \
  -r agents/weather/requirements.txt \
  -r agents/budget/requirements.txt \
  -r remote-agents/activities-cloudrun/requirements.txt \
  -r frontend/requirements.txt

# Explicit check — dev-local.sh invokes `opentelemetry-instrument` by name.
if ! command -v opentelemetry-instrument >/dev/null 2>&1; then
  echo "ERROR: opentelemetry-instrument not on PATH after install."
  echo "Make sure aws-opentelemetry-distro installed its console-scripts into the active venv."
  exit 1
fi

echo
echo "install OK."
echo "opentelemetry-instrument -> $(command -v opentelemetry-instrument)"
echo "python                   -> $(command -v python3)"
echo
echo "Next: AWS_PROFILE=omni-demo AWS_ACCOUNT_ID=<id> scripts/dev-local.sh"
