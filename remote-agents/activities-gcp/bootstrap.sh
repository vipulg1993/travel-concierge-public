#!/usr/bin/env bash
# Cloud Shell bootstrap for the Activities Specialist.
# Sets up a Python venv, downloads ADOT Collector, starts Collector + FastAPI
# + cloudflared tunnel. Prints the public URL at the end.
#
# Prereq (one-time, user action):
#   1. Copy your AWS credentials into ~/aws-creds.env with:
#        export AWS_ACCESS_KEY_ID=AKIA...
#        export AWS_SECRET_ACCESS_KEY=...
#        export AWS_REGION=us-east-1
#   2. source ~/aws-creds.env
#   3. ./bootstrap.sh

set -euo pipefail

ROOT="$(cd "$(dirname "$0")" && pwd)"
cd "$ROOT"

echo "═══════════════════════════════════════════════════════════"
echo "  Activities Specialist — GCP Cloud Shell bootstrap"
echo "═══════════════════════════════════════════════════════════"

# ------------------------------------------------- preflight
# Auto-source ~/aws-creds.env if present so a fresh Cloud Shell shell works
# without the user remembering to re-source.
if [ -z "${AWS_ACCESS_KEY_ID:-}" ] && [ -f "$HOME/aws-creds.env" ]; then
  echo "Loading credentials from ~/aws-creds.env ..."
  # shellcheck disable=SC1090
  source "$HOME/aws-creds.env"
fi

if [ -z "${AWS_ACCESS_KEY_ID:-}" ] || [ -z "${AWS_SECRET_ACCESS_KEY:-}" ]; then
  echo "ERROR: AWS credentials not set. Create ~/aws-creds.env with:"
  echo "    export AWS_ACCESS_KEY_ID=AKIA..."
  echo "    export AWS_SECRET_ACCESS_KEY=..."
  echo "    export AWS_REGION=us-east-1"
  echo "Then re-run this script."
  exit 1
fi
export AWS_REGION="${AWS_REGION:-us-east-1}"

# ------------------------------------------------- python deps
if [ ! -d venv ]; then
  echo
  echo "[1/5] Creating Python venv..."
  python3 -m venv venv
fi
source venv/bin/activate
echo "[1/5] Installing agent dependencies..."
pip install --quiet -r requirements.txt

# ------------------------------------------------- ADOT collector
if [ ! -f ./otelcol-contrib ]; then
  echo
  echo "[2/5] Downloading ADOT Collector (contrib build)..."
  ARCH=$(uname -m)
  case "$ARCH" in
    x86_64)   OTELCOL_ARCH=amd64 ;;
    aarch64)  OTELCOL_ARCH=arm64 ;;
    *)        echo "unsupported arch: $ARCH"; exit 1 ;;
  esac
  OTELCOL_VERSION="0.108.0"
  curl -sSL -o otelcol-contrib.tar.gz \
    "https://github.com/open-telemetry/opentelemetry-collector-releases/releases/download/v${OTELCOL_VERSION}/otelcol-contrib_${OTELCOL_VERSION}_linux_${OTELCOL_ARCH}.tar.gz"
  tar xzf otelcol-contrib.tar.gz otelcol-contrib
  rm -f otelcol-contrib.tar.gz
  chmod +x otelcol-contrib
  echo "    installed otelcol-contrib $(./otelcol-contrib --version 2>&1 | head -1)"
fi

# ------------------------------------------------- cloudflared tunnel
# Kill any orphans FIRST so we can overwrite the binary if needed.
pkill -9 -f cloudflared || true
sleep 1

if [ ! -x ./cloudflared ]; then
  echo
  echo "[3/5] Installing cloudflared (free public tunnel)..."
  # Remove any stale/truncated file left from a failed download
  rm -f ./cloudflared
  curl -sSL --fail -o ./cloudflared \
    "https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-linux-amd64" || {
      echo "ERROR: cloudflared download failed. Retry: rm -f ./cloudflared && ./bootstrap.sh"
      exit 1
    }
  chmod +x ./cloudflared
fi
export PATH="$ROOT:$PATH"
echo "[3/5] cloudflared ready: $(./cloudflared --version 2>&1 | head -1)"

# ------------------------------------------------- start collector
echo
echo "[4/5] Starting ADOT Collector on :4318..."
pkill -f otelcol-contrib || true
sleep 1
./otelcol-contrib --config collector-config.yaml > collector.log 2>&1 &
COLLECTOR_PID=$!
sleep 2
if ! kill -0 "$COLLECTOR_PID" 2>/dev/null; then
  echo "ERROR: Collector failed to start. tail collector.log:"
  tail -20 collector.log
  exit 1
fi
echo "    collector pid=$COLLECTOR_PID (logs → collector.log)"

# ------------------------------------------------- start agent
echo
echo "[5/5] Starting Activities Specialist on :8080..."
pkill -f "uvicorn app:app" || true
pkill -f "python3? -m uvicorn" || true
pkill -f "python3? app.py" || true
sleep 1
nohup python3 app.py > agent.log 2>&1 &
AGENT_PID=$!
sleep 3
if ! kill -0 "$AGENT_PID" 2>/dev/null; then
  echo "ERROR: Agent failed to start. tail agent.log:"
  tail -20 agent.log
  exit 1
fi
echo "    agent pid=$AGENT_PID (logs → agent.log)"
echo
curl -sf http://localhost:8080/ping && echo || echo "    (ping did not respond yet)"

# ------------------------------------------------- cloudflared tunnel
echo
echo "─────────────────────────────────────────────"
echo "  Starting cloudflared tunnel..."
echo "  Public URL will appear below in ~5 seconds."
echo "─────────────────────────────────────────────"
pkill -f "cloudflared" || true
sleep 1
./cloudflared tunnel --url http://localhost:8080 --no-autoupdate 2>&1 | tee cloudflared.log &
CF_PID=$!

echo
echo "═══════════════════════════════════════════════════════════"
echo "  Collector:    pid=$COLLECTOR_PID  log=collector.log"
echo "  Agent:        pid=$AGENT_PID     log=agent.log"
echo "  Cloudflared:  pid=$CF_PID        log=cloudflared.log"
echo
echo "  Watch for a line like:"
echo "    Your quick Tunnel has been created! Visit it at:"
echo "    https://<something>.trycloudflare.com"
echo
echo "  Copy that URL and run on your AWS laptop:"
echo "    aws ssm put-parameter \\"
echo "      --name /travel-concierge/remote-agents/activities-gcp-url \\"
echo "      --value <URL> --type String --overwrite --region us-east-1"
echo "═══════════════════════════════════════════════════════════"

wait "$CF_PID"
