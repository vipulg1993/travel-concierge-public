#!/usr/bin/env bash
# dev-local.sh — one-command F2 local dev stack for Travel Concierge.
#
# Topology this starts (6 python processes):
#   orchestrator      :8080   (POST /invocations streams SSE)
#   flights (Strands) :8081
#   hotels  (Strands) :8082
#   weather (Strands) :8083
#   budget  (CrewAI)  :8084
#   activities(GCP)   :8085   (LangGraph; same source as remote-agents/activities-cloudrun)
#
# All six processes export OTLP spans to the Omni local collector
# (OTEL_EXPORTER_OTLP_ENDPOINT — set by the Omni Studio IDE extension).
# The orchestrator calls each specialist over plain HTTP; `requests` is
# auto-instrumented so W3C traceparent propagates and the whole run is one
# unified trace tree.
#
# Invoke the local orchestrator from the Omni Kiro extension's Test Agent
# or Playground view. Streamlit is NOT part of the local stack — it only
# runs against prod. Start the prod Streamlit separately with
# `./scripts/streamlit-prod.sh`.
#
# Usage:
#   AWS_PROFILE=omni-demo AWS_ACCOUNT_ID=<id> ./scripts/dev-local.sh
#   AWS_PROFILE=omni-demo AWS_ACCOUNT_ID=<id> ./scripts/dev-local.sh --detach
#
# Stop with Ctrl-C (foreground mode traps SIGINT) or `scripts/dev-local-stop.sh`.

set -euo pipefail

# ── repo root (script is in <repo>/scripts/) ───────────────────────────────
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"

LOG_DIR="$REPO_ROOT/.local-logs"
mkdir -p "$LOG_DIR"

# ── required operator env ──────────────────────────────────────────────────
: "${AWS_PROFILE:?set AWS_PROFILE (e.g. omni-demo)}"
: "${AWS_ACCOUNT_ID:?set AWS_ACCOUNT_ID (your 12-digit account id)}"
export AWS_PROFILE AWS_ACCOUNT_ID
export AWS_REGION="${AWS_REGION:-us-east-1}"

# Omni's local collector issues a fresh x-omni-collector-token every time
# it restarts (which happens on IDE reload, often invisibly during a long
# session). The current token lives in .env.omni; the shell may still hold
# a stale copy from an earlier source. ALWAYS re-source if the file exists
# so the agents never start with a stale token — the clearest symptom of
# that bug is `OTLPExporter: 401 Unauthorized` in .local-logs/*.log.
# We never commit .env.omni — it carries the dynamic collector port + token.
for candidate in \
  "$REPO_ROOT/.env.omni" \
  "$REPO_ROOT/../Omni + DevOps Agent/.env.omni" \
  "$REPO_ROOT/../.env.omni"
do
  if [ -f "$candidate" ]; then
    # shellcheck disable=SC1090
    set -a; source "$candidate"; set +a
    echo "sourced OTel env from: $candidate"
    break
  fi
done

: "${OTEL_EXPORTER_OTLP_ENDPOINT:?Omni local collector not set.
  In Kiro: Cmd+Shift+P → \"Omni: Start Dev Server\". Then re-run.
  Or source your .env.omni manually and re-run.}"

# ── OTel env — mirror of prod _runtime_env in infra/deploy.py ──────────────
# Shared across every agent (same for every agent in prod). Resource attrs
# and OTEL_SERVICE_NAME are per-agent and set in start_agent() below.
export AGENT_OBSERVABILITY_ENABLED="true"
export AWS_GENAI_CONTENT_EXTRACTION_OPT_OUT="true"
export OTEL_INSTRUMENTATION_GENAI_CAPTURE_MESSAGE_CONTENT="true"
export STRANDS_OTEL_ENABLE_CONTENT_TRACING="true"
export STRANDS_OTEL_ENABLE_CONTENT_CAPTURE="true"
export OTEL_SEMCONV_STABILITY_OPT_IN="gen_ai_latest_experimental,gen_ai_tool_definitions"
export LANGFUSE_BASE_URL="langfuse"
export OTEL_PROPAGATORS="tracecontext,baggage,xray"
export OTEL_EXPORTER_OTLP_PROTOCOL="http/protobuf"
export OTEL_TRACES_EXPORTER="otlp"
export OTEL_LOGS_EXPORTER="none"
export OTEL_METRICS_EXPORTER="none"
# AWS Distro: X-Ray-compatible trace IDs + auto-detect Boto/FastAPI/requests.
export OTEL_PYTHON_DISTRO="aws_distro"
export OTEL_PYTHON_CONFIGURATOR="aws_configurator"

# ── fetch prod AgentCore runtime ARNs so local resource attrs mirror prod ──
# Each AgentCore-hosted service's prod span resource carries:
#   cloud.resource_id      = arn:aws:bedrock-agentcore:...:runtime/<name>-<id>/runtime-endpoint/DEFAULT:DEFAULT
#   aws.log.group.names    = /aws/bedrock-agentcore/runtimes/<name>-<id>-DEFAULT
# <id> is the AgentCore-assigned random suffix, stable per deployment. We
# pull the live ARN once from SSM so local and prod stay 1:1 after a redeploy
# without hand-editing this script. A missing param (fresh workspace,
# nothing deployed yet) is non-fatal: we fall back to the service.name only.
echo "fetching prod runtime ARNs for cloud.resource_id parity ..."
AC_ARN_ORCHESTRATOR="$(aws ssm get-parameter --name /travel-concierge/runtime-arns/orchestrator --query Parameter.Value --output text 2>/dev/null || true)"
AC_ARN_FLIGHTS="$(aws ssm get-parameter --name /travel-concierge/runtime-arns/flights --query Parameter.Value --output text 2>/dev/null || true)"
AC_ARN_WEATHER="$(aws ssm get-parameter --name /travel-concierge/runtime-arns/weather --query Parameter.Value --output text 2>/dev/null || true)"
AC_ARN_BUDGET="$(aws ssm get-parameter --name /travel-concierge/runtime-arns/budget --query Parameter.Value --output text 2>/dev/null || true)"
# Derive the log-group suffix "<name>-<id>-DEFAULT" from the ARN's runtime
# path (second-to-last / third segments). The ARN shape is
#   arn:aws:bedrock-agentcore:...:runtime/<name>-<id>/runtime-endpoint/DEFAULT:DEFAULT
_lg_from_arn() {
  local arn="$1" name="$2"
  if [ -n "$arn" ]; then
    local runtime_id
    runtime_id="$(echo "$arn" | sed -nE 's#.*:runtime/([^/]+)/.*#\1#p')"
    [ -n "$runtime_id" ] && echo "/aws/bedrock-agentcore/runtimes/${runtime_id}-DEFAULT" && return
  fi
  # Fallback — placeholder shape, no runtime id available
  echo "/aws/bedrock-agentcore/runtimes/travel_concierge_${name}-local-DEFAULT"
}
export AC_ARN_ORCHESTRATOR AC_ARN_FLIGHTS AC_ARN_WEATHER AC_ARN_BUDGET

# ── collector health check ─────────────────────────────────────────────────
# OTel HTTP endpoints accept POST only; a GET returns 405 on a live collector
# and 'connection refused' on a dead one. Both 405 and 200 count as healthy.
echo "checking Omni collector at $OTEL_EXPORTER_OTLP_ENDPOINT ..."
code=$(curl -sS -o /dev/null -w '%{http_code}' -m 3 "$OTEL_EXPORTER_OTLP_ENDPOINT" || echo 000)
case "$code" in
  200|204|400|401|403|404|405)
    # 2xx: GET answered. 4xx: a server is listening — Omni's collector
    # answers 401/405 to anonymous GETs because the token header is missing;
    # that still proves reachability. Any other code (000, 5xx) is a dead port.
    echo "  OK (HTTP $code — collector reachable)"
    ;;
  *)
    cat <<EOF
Omni local collector not reachable at $OTEL_EXPORTER_OTLP_ENDPOINT (got $code).
  In Kiro: Cmd+Shift+P → "Omni: Start Dev Server", then re-run this script.
  Or source your .env.omni manually (collector port is dynamic across restarts).
EOF
    exit 1
    ;;
esac

# ── port collision check ───────────────────────────────────────────────────
# If anything is already listening on our target ports, abort rather than
# silently colliding — the operator probably has a stale run.
for port in 8080 8081 8082 8083 8084 8085; do
  if lsof -nP -iTCP:"$port" -sTCP:LISTEN >/dev/null 2>&1; then
    echo "port $port is already in use. Run scripts/dev-local-stop.sh first."
    lsof -nP -iTCP:"$port" -sTCP:LISTEN | head -3
    exit 1
  fi
done

# ── per-agent launch ───────────────────────────────────────────────────────
# `opentelemetry-instrument` wraps uvicorn so every incoming HTTP request on
# /invocations gets a FastAPI SERVER span (same as what AgentCore surfaces in
# prod), and every outbound requests/boto3 call auto-produces a client span.
rm -f "$LOG_DIR"/*.pid

PIDS=()

start_agent() {
  local name="$1" port="$2" app_dir="$3" app_target="${4:-main:app}"
  # Per-agent OTel service identity. Mirror prod resource attrs exactly so
  # Omni Trace Explorer renders each hop with the right cloud.platform —
  # orchestrator/flights/weather/budget on AgentCore, hotels on EKS Fargate,
  # activities on GCP Cloud Run. Prod ground truth: trace
  # 6ad81140fa284bb3dd1c84713e3ff072 in the main account.
  local service resource
  case "$name" in
    hotels)
      service="travel_concierge_hotels.eks"
      # Prod hotels has NO cloud.provider and NO cloud.resource_id, but DOES
      # carry an aws.log.group.names pointing at the EKS-equivalent CW path.
      resource="service.name=${service},cloud.platform=aws_eks,deployment.environment=eks-fargate,aws.local.service=${service},aws.service.type=gen_ai_agent,service.framework=strands,aws.log.group.names=/aws/bedrock-agentcore/runtimes/travel_concierge_hotels-eks"
      ;;
    activities)
      service="travel_concierge_activities.gcp"
      # Prod activities carries service.namespace and does NOT carry
      # aws.local.service — match exactly.
      resource="service.name=${service},cloud.provider=gcp,cloud.platform=gcp_cloud_run,cloud.region=us-central1,deployment.environment=cloudrun,aws.service.type=gen_ai_agent,service.framework=langgraph,service.namespace=travel-concierge"
      ;;
    *)
      # AgentCore-hosted services (orchestrator, flights, weather, budget).
      # Prod uses OTel 1.27+ deployment.environment.name and the .DEFAULT
      # endpoint suffix. aws.log.group.names and cloud.resource_id are
      # derived from the live AgentCore runtime ARN fetched from SSM at
      # script start, so local matches prod 1:1. service.framework is NOT
      # present on these services in prod (ADOT doesn't inject it on
      # AgentCore), but we add it locally so the Omni UI shows per-service
      # framework identity instead of leaving the column empty.
      local framework="strands"
      case "$name" in
        budget) framework="crewai" ;;
      esac
      service="travel_concierge_${name}.DEFAULT"
      local arn_var="AC_ARN_$(echo "$name" | tr '[:lower:]' '[:upper:]')"
      local prod_arn="${!arn_var:-}"
      local log_group
      log_group="$(_lg_from_arn "$prod_arn" "$name")"
      resource="service.name=${service},cloud.provider=aws,cloud.platform=aws_bedrock_agentcore,cloud.region=us-east-1,deployment.environment.name=bedrock-agentcore:default,aws.local.service=${service},aws.service.type=gen_ai_agent,service.framework=${framework},aws.log.group.names=${log_group},aws.log.stream.names=otel-rt-logs"
      if [ -n "$prod_arn" ]; then
        resource="${resource},cloud.resource_id=${prod_arn}"
      fi
      ;;
  esac

  # Local-only orchestrator wiring: point every specialist tool at its
  # 127.0.0.1 sibling. Leaving *_RUNTIME_ARN unset means the AgentCore
  # SDK fallback can't accidentally fire — misconfiguration surfaces as
  # an obvious HTTP connect error, not a silent SigV4 fail.
  local extra_env=""
  if [ "$name" = "orchestrator" ]; then
    extra_env="\
FLIGHTS_LOCAL_URL=http://127.0.0.1:8081 \
HOTELS_LOCAL_URL=http://127.0.0.1:8082 \
WEATHER_LOCAL_URL=http://127.0.0.1:8083 \
BUDGET_LOCAL_URL=http://127.0.0.1:8084 \
ACTIVITIES_LOCAL_URL=http://127.0.0.1:8085"
  fi

  # shellcheck disable=SC2086
  env OTEL_SERVICE_NAME="$service" \
      OTEL_RESOURCE_ATTRIBUTES="$resource" \
      $extra_env \
      opentelemetry-instrument \
        uvicorn "$app_target" --host 127.0.0.1 --port "$port" --app-dir "$app_dir" \
        > "$LOG_DIR/${name}.log" 2>&1 &
  local pid=$!
  echo "$pid" > "$LOG_DIR/${name}.pid"
  PIDS+=("$pid")
  printf "  started %-13s pid=%-6s port=%s  log=%s\n" \
    "$name" "$pid" "$port" ".local-logs/${name}.log"
}

echo "starting specialists ..."
start_agent flights    8081 "agents/flights/src"
start_agent hotels     8082 "agents/hotels/src"
start_agent weather    8083 "agents/weather/src"
start_agent budget     8084 "agents/budget/src"
start_agent activities 8085 "remote-agents/activities-cloudrun" "app:app"

# Orchestrator must come last so its tool fan-out has live /invocations peers.
echo "starting orchestrator ..."
start_agent orchestrator 8080 "agents/orchestrator/src"

# Streamlit is intentionally NOT started here — it runs against prod only.
# Start it separately when you need the prod UI: ./scripts/streamlit-prod.sh

# ── wait for /ping on every agent ──────────────────────────────────────────
echo "waiting for /ping on 8080..8085 ..."
all_green=0
for _ in $(seq 1 60); do
  all_green=1
  for port in 8080 8081 8082 8083 8084 8085; do
    code=$(curl -sS -o /dev/null -w '%{http_code}' "http://127.0.0.1:${port}/ping" -m 1 || echo 000)
    if [ "$code" != "200" ]; then
      all_green=0
      break
    fi
  done
  if [ "$all_green" = "1" ]; then
    break
  fi
  sleep 1
done

echo
if [ "$all_green" = "1" ]; then
  echo "all 6 agents are reachable:"
  for port in 8080 8081 8082 8083 8084 8085; do
    printf "  http://127.0.0.1:%s/ping => %s\n" "$port" \
      "$(curl -sS -w ' HTTP %{http_code}' http://127.0.0.1:${port}/ping -m 2 2>/dev/null | head -c 120)"
  done
  echo
  echo "Invoke the local orchestrator from the Omni Kiro extension's"
  echo "Test Agent / Playground view (target: http://127.0.0.1:8080)."
  echo "For prod UI, start Streamlit separately: ./scripts/streamlit-prod.sh"
else
  echo "WARNING: not every agent came up — check .local-logs/*.log"
  for port in 8080 8081 8082 8083 8084 8085; do
    code=$(curl -sS -o /dev/null -w '%{http_code}' "http://127.0.0.1:${port}/ping" -m 1 || echo 000)
    printf "  :%s %s\n" "$port" "$code"
  done
fi

# ── Ctrl-C → clean shutdown ────────────────────────────────────────────────
cleanup() {
  echo
  echo "shutting down (SIGTERM to $(ls "$LOG_DIR"/*.pid 2>/dev/null | wc -l | tr -d ' ') children) ..."
  "$REPO_ROOT/scripts/dev-local-stop.sh" || true
}

if [ "${1:-}" = "--detach" ]; then
  cat > "$LOG_DIR/README" <<EOF
Dev-local processes are detached. Stop with: scripts/dev-local-stop.sh
Logs: $LOG_DIR/*.log
EOF
  echo
  echo "detached mode — use scripts/dev-local-stop.sh to stop."
  exit 0
fi

trap cleanup INT TERM EXIT
echo
echo "foreground mode — Ctrl-C to stop all children."
wait
