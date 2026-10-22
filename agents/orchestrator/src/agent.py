"""Travel Concierge Orchestrator.

A Strands agent that fans out to 4 specialist AgentCore Runtimes in parallel:
Flights, Hotels, Weather, and Budget. Each specialist has its own OTel trace
subtree because each is a separate AgentCore Runtime.

Trace shape (one request):

    invoke_agent (this agent)
    ├── gen_ai.chat (initial plan)
    ├── tool_call: search_flights → bedrock-agentcore:InvokeAgentRuntime → Flights
    ├── tool_call: search_hotels  → HTTP → EKS Fargate pod (Hotels)
    ├── tool_call: check_weather  → bedrock-agentcore:InvokeAgentRuntime → Weather
    ├── tool_call: estimate_budget → bedrock-agentcore:InvokeAgentRuntime → Budget
    └── gen_ai.chat (final synthesis)
"""

import json
import logging
import os

# ADOT (via `opentelemetry-instrument`) owns global tracer provider + propagator
# configuration. Per the AWS doc we must NOT create another tracer provider,
# override the global TextMapPropagator, or duplicate ADOT's managed env vars.
#
# EXCEPTION: ADOT's auto-detect doesn't always pick up `requests` in this
# runtime image (observed empirically — outbound HTTP spans for the two
# HTTP-hosted sub-agents were missing after we stripped overrides). Call it
# explicitly here; the call is idempotent so if auto-detect does run too,
# there's no conflict. Without this, the orchestrator's HTTP calls to the
# EKS hotels pod and the Cloud Run activities container produce no client
# span and no trace context propagates to them.
from opentelemetry.instrumentation.requests import RequestsInstrumentor
RequestsInstrumentor().instrument()

import boto3
import requests
from strands import Agent, tool
from strands.models.bedrock import BedrockModel

log = logging.getLogger(__name__)

REGION = os.environ.get("AWS_REGION", "us-east-1")
MODEL_ID = os.environ.get(
    "MODEL_ID", "us.anthropic.claude-sonnet-4-5-20250929-v1:0"
)

# Specialist ARNs are injected at deploy time via SSM lookups in main.py.
# Env var fallback keeps local development working without SSM.
FLIGHTS_ARN = os.environ.get("FLIGHTS_RUNTIME_ARN", "")
WEATHER_ARN = os.environ.get("WEATHER_RUNTIME_ARN", "")
BUDGET_ARN = os.environ.get("BUDGET_RUNTIME_ARN", "")
# Hotels has no AgentCore ARN — the hotels specialist is EKS-only. HOTELS_EKS_URL
# (set below) is required.

# Remote agent URLs (hosted outside AgentCore Runtime — on EKS, Cloud Run, etc.).
# Populated at deploy-time via SSM. The orchestrator calls these over plain
# HTTP(S); OTel auto-instrumentation of `requests` injects W3C traceparent so
# trace context stitches cleanly into the parent trace.
ACTIVITIES_GCP_URL = os.environ.get("ACTIVITIES_GCP_URL", "")
# When set, hotels is called over HTTP against this URL (EKS Fargate pod)
# instead of via bedrock-agentcore:InvokeAgentRuntime. Lets us run the same
# Strands agent on either compute without any agent-code change — just a
# different plumbing path in the orchestrator.
HOTELS_EKS_URL = os.environ.get("HOTELS_EKS_URL", "")

# ─── Local-dev-mode fallbacks (Phase B) ─────────────────────────────────────
# When set, the orchestrator calls the matching specialist over plain HTTP at
# this URL instead of via bedrock-agentcore:InvokeAgentRuntime. This is the
# F2 local topology: 5 uvicorn processes on 127.0.0.1:808{1..5}. The HTTP call
# goes through `_invoke_remote_agent`, which `requests` auto-instruments —
# W3C traceparent is injected automatically so the orchestrator → specialist
# hop stays part of one unified trace tree.
#
# Precedence per tool is additive: *_LOCAL_URL beats *_EKS_URL/*_GCP_URL,
# which both beat the AgentCore SDK (`_invoke_specialist` → *_RUNTIME_ARN).
# All vars default to empty, so the AgentCore prod path is unchanged when
# nothing opts in.
FLIGHTS_LOCAL_URL    = os.environ.get("FLIGHTS_LOCAL_URL", "")
HOTELS_LOCAL_URL     = os.environ.get("HOTELS_LOCAL_URL", "")
WEATHER_LOCAL_URL    = os.environ.get("WEATHER_LOCAL_URL", "")
BUDGET_LOCAL_URL     = os.environ.get("BUDGET_LOCAL_URL", "")
ACTIVITIES_LOCAL_URL = os.environ.get("ACTIVITIES_LOCAL_URL", "")

# When truthy, the orchestrator skips registering `recommend_activities` so
# fan-out is 4-agent-only (AWS specialists). Useful for isolating AWS-side
# issues from GCP-side issues during smoke tests. Flip via a one-liner env
# var update: `aws bedrock-agentcore-control update-agent-runtime ...`
DISABLE_ACTIVITIES = os.environ.get("TC_DISABLE_ACTIVITIES", "").lower() in (
    "1", "true", "yes", "on"
)

_ac = boto3.client("bedrock-agentcore", region_name=REGION)


from opentelemetry import trace as _otel_trace


def _trace_context_payload() -> dict:
    """Build a trace-context dict to embed in the invocation payload.

    Workaround for AgentCore control-plane dispatcher: the SDK's
    `invoke_agent_runtime` HTTP headers (traceparent / X-Amzn-Trace-Id) get
    REWRITTEN by AWS's internal dispatch hop before reaching the sub-runtime
    container, so the inbound traceparent on the sub-agent's SERVER span
    always points to a hidden dispatcher span — producing a disconnected
    sub-root in the trace tree.

    Embedding the orchestrator's current W3C traceparent inside the
    application-level JSON body bypasses that rewrite. The sub-agent
    extracts it from the body and manually sets it as the parent for a span
    that wraps all the real agent work. Result: the sub-agent's work
    correctly nests under the orchestrator's execute_tool span.
    """
    span = _otel_trace.get_current_span()
    if not span:
        return {}
    sc = span.get_span_context()
    if not sc or not sc.is_valid:
        return {}
    traceparent = f"00-{sc.trace_id:032x}-{sc.span_id:016x}-{sc.trace_flags:02x}"
    return {"traceparent": traceparent}


def _invoke_specialist(arn: str, message: str, session_id: str, user_id: str) -> str:
    """Call a specialist AgentCore Runtime and drain its SSE stream to a single string.

    Trace propagation here uses TWO paths:
      1. ADOT's botocore instrumentor auto-injects HTTP headers (traceparent,
         X-Amzn-Trace-Id) on the outbound call — these are what AgentCore
         sees but subsequently rewrites.
      2. We embed `_trace_context.traceparent` in the application payload,
         which AgentCore passes through verbatim. The sub-agent extracts
         this and creates a span under our ACTUAL current span, bypassing
         the dispatcher rewrite.
    """
    if not arn:
        return json.dumps({"error": f"specialist runtime not configured"})
    payload = {
        "input": {"message": message, "session_id": session_id, "user_id": user_id},
        "_trace_context": _trace_context_payload(),
    }
    resp = _ac.invoke_agent_runtime(
        agentRuntimeArn=arn,
        runtimeSessionId=session_id,
        runtimeUserId=user_id,
        payload=json.dumps(payload).encode("utf-8"),
        qualifier="DEFAULT",
    )
    # The specialist streams SSE "data: {...}" events. We only need the final text
    # from the specialist's agent for the orchestrator's LLM to reason about.
    text_parts = []
    error_parts = []
    buf = b""
    for chunk in resp["response"]:
        if not chunk:
            continue
        buf += chunk
        while b"\n\n" in buf:
            frame, buf = buf.split(b"\n\n", 1)
            if frame.startswith(b"data: "):
                try:
                    evt = json.loads(frame[6:])
                except Exception:
                    continue
                t = evt.get("type")
                if t == "text":
                    text_parts.append(evt.get("content", ""))
                elif t == "tool_result":
                    text_parts.append(evt.get("content", ""))
                elif t == "error":
                    error_parts.append(evt.get("content", ""))
    if error_parts and not text_parts:
        return json.dumps({"error": " ".join(error_parts)})
    return "".join(text_parts) or json.dumps({"result": "(empty response)"})


def _invoke_remote_agent(url: str, message: str, session_id: str, user_id: str, timeout: int = 60) -> str:
    """Call a remote (non-AgentCore) agent over plain HTTP(S).

    Works for two kinds of remote agents:
      1. GCP Cloud Run activities — returns a single JSON body.
      2. EKS Fargate hotels — same Strands/FastAPI shell as the AgentCore
         version, so it streams SSE "data: {json}" events.

    The `requests` library is auto-instrumented, so trace context is injected
    as a W3C `traceparent` header on the outbound call. The remote agent's
    OTel SDK reads that header and continues the trace locally, producing one
    unified trace tree in Omni.
    """
    if not url:
        return json.dumps({"error": "remote agent not configured"})
    # Payload includes the same `_trace_context` as the SDK path — the sub-agent
    # treats SDK and HTTP paths uniformly. On the HTTP path, the W3C traceparent
    # header is also auto-injected by requests instrumentation (direct chain),
    # but having the payload copy keeps one code path in the sub-agent.
    payload = {
        "input": {"message": message, "session_id": session_id, "user_id": user_id},
        "_trace_context": _trace_context_payload(),
    }
    try:
        resp = requests.post(
            f"{url.rstrip('/')}/invocations",
            json=payload,
            timeout=timeout,
            headers={"Content-Type": "application/json", "Accept": "text/event-stream, application/json"},
            stream=True,
        )
        if resp.status_code >= 500:
            return json.dumps({"error": f"remote agent HTTP {resp.status_code}: {resp.text[:200]}"})

        ctype = resp.headers.get("content-type", "")
        if "text/event-stream" in ctype:
            # Drain the SSE stream like we do for AgentCore specialists —
            # concatenate `text` and `tool_result` events; surface errors.
            text_parts, error_parts = [], []
            buf = b""
            for chunk in resp.iter_content(chunk_size=None):
                if not chunk:
                    continue
                buf += chunk
                while b"\n\n" in buf:
                    frame, buf = buf.split(b"\n\n", 1)
                    if frame.startswith(b"data: "):
                        try:
                            evt = json.loads(frame[6:])
                        except Exception:
                            continue
                        t = evt.get("type")
                        if t in ("text", "tool_result"):
                            text_parts.append(evt.get("content", ""))
                        elif t == "error":
                            error_parts.append(evt.get("content", ""))
            if error_parts and not text_parts:
                return json.dumps({"error": " ".join(error_parts)})
            return "".join(text_parts) or json.dumps({"result": "(empty response)"})

        # Non-streaming remote agent (JSON body)
        return resp.text
    except requests.exceptions.Timeout:
        return json.dumps({"error": "remote agent timed out"})
    except requests.exceptions.RequestException as e:
        return json.dumps({"error": f"remote agent error: {e}"})


def build_tools(session_id: str, user_id: str):
    """Build the orchestrator's tool-set with session context baked in."""

    @tool
    def search_flights(destination: str, dates: str, max_price: int = 2000) -> str:
        """Search for flights to a destination for a given date range.

        Args:
            destination: City name (e.g., "Seattle", "Austin").
            dates: Human-readable date range (e.g., "Dec 22-28 2026",
                   "Christmas week 2026", "2026-12-22 to 2026-12-28").
            max_price: Maximum total price in USD. Defaults to 2000.
        """
        q = (
            f"Find flights to {destination} departing in the window {dates} "
            f"under ${max_price}. Only recommend flights whose departure date "
            f"falls within that window; if none, say so explicitly."
        )
        if FLIGHTS_LOCAL_URL:
            return _invoke_remote_agent(FLIGHTS_LOCAL_URL, q, session_id, user_id)
        return _invoke_specialist(FLIGHTS_ARN, q, session_id, user_id)

    @tool
    def search_hotels(destination: str, dates: str, max_price_per_night: int = 300) -> str:
        """Search for hotels at a destination for a given date range.

        Args:
            destination: City name.
            dates: Human-readable date range.
            max_price_per_night: Max per-night price in USD. Defaults to 300.
        """
        q = f"Find hotels in {destination} for {dates} under ${max_price_per_night} per night."
        if HOTELS_LOCAL_URL:
            return _invoke_remote_agent(HOTELS_LOCAL_URL, q, session_id, user_id)
        if not HOTELS_EKS_URL:
            # Hotels is EKS-only — no AgentCore fallback. If the EKS URL isn't
            # set at deploy time (SSM param missing or empty), the orchestrator
            # fails this tool loudly instead of silently routing elsewhere.
            raise RuntimeError(
                "hotels specialist unreachable: HOTELS_EKS_URL env var not set"
            )
        # Route to the EKS Fargate pod. Produces an outbound HTTP span in the
        # trace, visibly showing the EKS hop in Omni.
        return _invoke_remote_agent(HOTELS_EKS_URL, q, session_id, user_id)

    @tool
    def check_weather(destination: str, dates: str) -> str:
        """Check the weather forecast for a destination over a date range.

        Args:
            destination: City name.
            dates: Human-readable date range.
        """
        q = f"What is the weather forecast for {destination} during {dates}?"
        if WEATHER_LOCAL_URL:
            return _invoke_remote_agent(WEATHER_LOCAL_URL, q, session_id, user_id)
        return _invoke_specialist(WEATHER_ARN, q, session_id, user_id)

    @tool
    def estimate_budget(flights_json: str, hotels_json: str, days: int = 7) -> str:
        """Estimate total trip cost given flight and hotel options.

        Args:
            flights_json: JSON string from search_flights.
            hotels_json: JSON string from search_hotels.
            days: Trip duration in days. Defaults to 7.
        """
        q = (
            f"Given flight options: {flights_json[:500]}\n"
            f"and hotel options: {hotels_json[:500]}\n"
            f"over {days} days, estimate the total trip cost and recommend the best value combo."
        )
        if BUDGET_LOCAL_URL:
            return _invoke_remote_agent(BUDGET_LOCAL_URL, q, session_id, user_id)
        return _invoke_specialist(BUDGET_ARN, q, session_id, user_id)

    @tool
    def recommend_activities(destination: str, dates: str, interests: str = "sightseeing") -> str:
        """Recommend 3 activities at the destination for the trip dates.

        This specialist is hosted outside AWS — it runs on Google Cloud Run
        as a LangGraph agent and emits OTel traces back to the CloudWatch
        OTLP traces endpoint via the same unified trace tree.

        Args:
            destination: City name.
            dates: Human-readable date range.
            interests: Comma-separated hints (e.g., "museums, food, hiking").
                Defaults to "sightseeing".
        """
        q = (
            f"Suggest activities in {destination} during {dates} "
            f"interests: {interests}."
        )
        if ACTIVITIES_LOCAL_URL:
            return _invoke_remote_agent(ACTIVITIES_LOCAL_URL, q, session_id, user_id)
        return _invoke_remote_agent(ACTIVITIES_GCP_URL, q, session_id, user_id)

    tools = [search_flights, search_hotels, check_weather, estimate_budget]
    if not DISABLE_ACTIVITIES:
        tools.append(recommend_activities)
    return tools


def _system_prompt() -> str:
    if DISABLE_ACTIVITIES:
        tools_list = (
            "  - search_flights(destination, dates, max_price)\n"
            "  - search_hotels(destination, dates, max_price_per_night)\n"
            "  - check_weather(destination, dates)\n"
            "  - estimate_budget(flights_json, hotels_json, days)\n"
        )
        output_lines = (
            "  Flight: <airline> <flight_id>, <origin> → <destination>, departs <date>, <price> USD.\n"
            "  Hotel: <hotel_name>, <star_rating>-star, <nightly_rate> USD per night, <nights> nights = <hotel_total> USD.\n"
            "  Weather: <2-sentence summary with temp range and one packing hint>.\n"
            "  Total: <flight_price> + <hotel_total> = <total> USD (<within/over> your <budget> USD budget)."
        )
        num = "four"
        parallel = "the first three"
    else:
        tools_list = (
            "  - search_flights(destination, dates, max_price)\n"
            "  - search_hotels(destination, dates, max_price_per_night)\n"
            "  - check_weather(destination, dates)\n"
            "  - recommend_activities(destination, dates, interests)\n"
            "  - estimate_budget(flights_json, hotels_json, days)\n"
            "\n"
            "If the user did not state interests, pass \"sightseeing\" to recommend_activities.\n"
        )
        output_lines = (
            "  Flight: <airline> <flight_id>, <origin> → <destination>, departs <date>, <price> USD.\n"
            "  Hotel: <hotel_name>, <star_rating>-star, <nightly_rate> USD per night, <nights> nights = <hotel_total> USD.\n"
            "  Weather: <2-sentence summary with temp range and one packing hint>.\n"
            "  Activities: <first activity name>; <second activity name>; <third activity name>.\n"
            "  Total: <flight_price> + <hotel_total> = <total> USD (<within/over> your <budget> USD budget)."
        )
        num = "five"
        parallel = "the first four"
    return (
        f"You are the Travel Concierge. Plan a trip in {num} tool calls.\n\n"
        f"For every trip-planning request you MUST call all {num} tools before "
        f"answering. Do NOT ask the user for clarification — the backend tools "
        f"accept vague inputs.\n"
        f"Default dates: \"Christmas week, December 2026\". Default budget: $2500.\n\n"
        f"Tools (call {parallel} in parallel, then estimate_budget):\n"
        f"{tools_list}\n"
        f"After gathering results, write the final answer in this format EXACTLY "
        f"— {num} short lines, plain prose, no markdown headers, no bold, no "
        f"asterisks, no emoji, no bullet lists. Use real newlines between lines. "
        f"Keep each line under 30 words.\n\n"
        f"{output_lines}\n\n"
        f"Write prices as plain text with the word \"USD\" (e.g. \"418 USD\"), "
        f"never with a \"$\".\n"
        f"If a tool returned an error, replace the affected line with: "
        f"\"<Field>: unavailable (<brief reason>)\".\n"
        f"Never fabricate options the tools did not return.\n"
    )


SYSTEM_PROMPT = _system_prompt()


def create_orchestrator_agent(session_id: str, user_id: str) -> Agent:
    model = BedrockModel(model_id=MODEL_ID, region_name=REGION)
    # session.id must appear on span attributes for the AgentCore online-eval
    # pipeline to cluster the trace into a scored session. Strands propagates
    # trace_attributes onto every span it emits.
    return Agent(
        model=model,
        tools=build_tools(session_id, user_id),
        system_prompt=SYSTEM_PROMPT,
        trace_attributes={
            "gen_ai.capture_message_content": True,
            "session.id": session_id,
        },
    )
