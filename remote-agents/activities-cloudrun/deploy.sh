#!/usr/bin/env bash
# Build + deploy the Activities Specialist to GCP Cloud Run.
# One-shot: Cloud Build builds the container, Cloud Run deploys it, URL printed.
# Needs AWS creds for the in-app SigV4 signer (CloudWatch OTLP traces) + Bedrock.

set -euo pipefail

ROOT="$(cd "$(dirname "$0")" && pwd)"
cd "$ROOT"

GCP_PROJECT="${GCP_PROJECT:-omni-demo-511116}"
GCP_REGION="${GCP_REGION:-us-central1}"
SERVICE_NAME="${SERVICE_NAME:-activities-agent}"

AWS_SECRETS_FILE="$HOME/.travel-concierge-remote-aws-creds"

echo "═══════════════════════════════════════════════════════════"
echo "  Deploying Activities Specialist to Cloud Run"
echo "  Project: $GCP_PROJECT   Region: $GCP_REGION   Service: $SERVICE_NAME"
echo "═══════════════════════════════════════════════════════════"

# Preflight: AWS credentials for the Collector + Bedrock
if [ ! -f "$AWS_SECRETS_FILE" ]; then
  echo
  echo "ERROR: Missing $AWS_SECRETS_FILE"
  echo
  echo "Create it (one-time) with the dedicated travel-concierge-remote-gcp"
  echo "IAM user's keys (see README.md Step 1):"
  echo
  echo "  cat > $AWS_SECRETS_FILE <<'EOF'"
  echo "  AWS_ACCESS_KEY_ID=AKIA..."
  echo "  AWS_SECRET_ACCESS_KEY=..."
  echo "  AWS_REGION=us-east-1"
  echo "  EOF"
  echo "  chmod 600 $AWS_SECRETS_FILE"
  exit 1
fi

# shellcheck disable=SC1090
set -a; source "$AWS_SECRETS_FILE"; set +a

# Preflight: gcloud auth + project
if ! gcloud auth list --filter=status:ACTIVE --format='value(account)' | grep -q .; then
  echo "ERROR: no active gcloud account. Run: gcloud auth login"
  exit 1
fi
gcloud config set project "$GCP_PROJECT" >/dev/null

# Enable required APIs (idempotent — a no-op if already enabled)
echo
echo "[1/3] Enabling required APIs (Cloud Build, Cloud Run, Artifact Registry)..."
gcloud services enable \
  run.googleapis.com \
  cloudbuild.googleapis.com \
  artifactregistry.googleapis.com \
  --quiet

# Deploy
echo
echo "[2/3] Submitting source build + Cloud Run deploy (3-5 min)..."
gcloud run deploy "$SERVICE_NAME" \
  --source . \
  --region "$GCP_REGION" \
  --allow-unauthenticated \
  --port 8080 \
  --timeout 60 \
  --concurrency 10 \
  --max-instances 2 \
  --memory 512Mi \
  --cpu 1 \
  --set-env-vars "^|^AWS_ACCESS_KEY_ID=${AWS_ACCESS_KEY_ID}|AWS_SECRET_ACCESS_KEY=${AWS_SECRET_ACCESS_KEY}|AWS_REGION=${AWS_REGION:-us-east-1}|AWS_BEDROCK_REGION=${AWS_REGION:-us-east-1}|GOOGLE_CLOUD_REGION=${GCP_REGION}|OTEL_SERVICE_NAME=travel_concierge_activities.gcp|OTEL_EXPORTER_OTLP_TRACES_ENDPOINT=https://xray.us-east-1.amazonaws.com/v1/traces|OTEL_INSTRUMENTATION_GENAI_CAPTURE_MESSAGE_CONTENT=true|OTEL_SEMCONV_STABILITY_OPT_IN=gen_ai_latest_experimental|OTEL_PYTHON_FASTAPI_EXCLUDED_URLS=invocations,ping" \
  --quiet

echo
echo "[3/3] Fetching service URL..."
URL=$(gcloud run services describe "$SERVICE_NAME" \
  --region "$GCP_REGION" --format='value(status.url)')

echo
echo "═══════════════════════════════════════════════════════════"
echo "  DEPLOYED"
echo "═══════════════════════════════════════════════════════════"
echo
echo "  URL: $URL"
echo
echo "  Smoke test:"
echo "    curl -sS $URL/ping"
echo
echo "  Register with the orchestrator (uses omni-demo profile):"
echo "    AWS_PROFILE=omni-demo aws ssm put-parameter \\"
echo "      --name /travel-concierge/remote-agents/activities-gcp-url \\"
echo "      --value $URL \\"
echo "      --type String --overwrite --region us-east-1"
echo
