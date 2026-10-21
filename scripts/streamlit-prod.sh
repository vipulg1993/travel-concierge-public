#!/usr/bin/env bash
# streamlit-prod.sh — Streamlit UI pointed at prod AgentCore.
#
# Unsets ORCHESTRATOR_LOCAL_URL so Streamlit's frontend/app.py takes the
# SSM/ARN path and talks to the real AgentCore orchestrator runtime.
#
# For the local flow (6 agents on your laptop), do NOT use Streamlit at all —
# invoke the local orchestrator from the Omni Kiro extension's Test Agent /
# Playground view against http://127.0.0.1:8080 instead.
#
# Usage:
#   AWS_PROFILE=omni-demo ./scripts/streamlit-prod.sh [--port 8501]

set -euo pipefail

: "${AWS_PROFILE:?set AWS_PROFILE (e.g. omni-demo)}"

PORT="${PORT:-8501}"
if [ "${1:-}" = "--port" ] && [ -n "${2:-}" ]; then
  PORT="$2"
fi

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$REPO_ROOT"

if lsof -nP -iTCP:"$PORT" -sTCP:LISTEN >/dev/null 2>&1; then
  echo "port $PORT is already in use."
  lsof -nP -iTCP:"$PORT" -sTCP:LISTEN | head -3
  echo
  echo "Either stop that process or pass --port <N> to use a different port."
  exit 1
fi

# Force prod path by making sure ORCHESTRATOR_LOCAL_URL is NOT set.
unset ORCHESTRATOR_LOCAL_URL

echo "starting Streamlit (prod) on :$PORT ..."
echo "  AWS_PROFILE=$AWS_PROFILE"
echo "  URL: http://localhost:$PORT"
echo

exec streamlit run frontend/app.py \
  --server.port "$PORT" \
  --server.headless true \
  --browser.gatherUsageStats false
