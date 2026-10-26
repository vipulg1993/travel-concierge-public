"""Activities Specialist — GCP Cloud Run, framework = LangGraph.

LangGraph node graph:
    START ─► extract_params ─► generate_activities ─► END

Trace shape (visible in Omni):

    POST /invocations                       (FastAPIInstrumentor)
    └── invoke activities_graph             (LangGraph auto-span if present)
        ├── node extract_params             (manual span wrapper)
        └── node generate_activities
            └── aws.bedrock InvokeModel     (BotocoreInstrumentor)
"""

import json
import logging
import os
import re
from typing import Any, Optional, TypedDict

# Must configure OTel BEFORE FastAPI app is created so auto-instrumentors
# attach to the right objects.
from otel_setup import setup_tracing
setup_tracing()

import boto3
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from langgraph.graph import StateGraph, END
from opentelemetry import context as otel_context
from opentelemetry import trace
from opentelemetry.trace.propagation.tracecontext import TraceContextTextMapPropagator

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
)
log = logging.getLogger(__name__)
tracer = trace.get_tracer(__name__)
_w3c = TraceContextTextMapPropagator()

REGION = os.environ.get("AWS_BEDROCK_REGION", "us-east-1")
MODEL_ID = os.environ.get(
    "MODEL_ID", "us.anthropic.claude-sonnet-4-5-20250929-v1:0"
)

# Bedrock client. AWS creds are injected as env vars (set as Cloud Run secrets).
_bedrock = boto3.client("bedrock-runtime", region_name=REGION)


# -------------------------------------------------- STATE
class ActivitiesState(TypedDict, total=False):
    user_message: str
    destination: str
    dates: str
    interests: str
    activities: str
    error: Optional[str]


# -------------------------------------------------- NODES
def extract_params(state: ActivitiesState) -> ActivitiesState:
    """Parse destination/dates/interests from the user's query (regex only, no LLM)."""
    with tracer.start_as_current_span("node.extract_params") as span:
        msg = state["user_message"]
        dest_match = re.search(
            r"activit(?:y|ies)?(?:\s+in|\s+for)?\s+([A-Za-z][A-Za-z ]{0,30}?)(?:\s+(?:during|for|on|in)|\s*[,.])",
            msg, re.I,
        )
        dates_match = re.search(r"(?:during|for|on)\s+([^.]+?)(?:\s+interests?|\s*$|\.)", msg, re.I)
        interests_match = re.search(r"interests?:\s*(.+?)(?:\.|$)", msg, re.I)
        dest = (dest_match.group(1).strip() if dest_match else "the destination")
        dates = (dates_match.group(1).strip() if dates_match else "the holiday season")
        interests = (interests_match.group(1).strip() if interests_match else "sightseeing")
        span.set_attribute("tc.destination", dest)
        span.set_attribute("tc.dates", dates)
        span.set_attribute("tc.interests", interests)
        return {**state, "destination": dest, "dates": dates, "interests": interests}


def generate_activities(state: ActivitiesState) -> ActivitiesState:
    """Call Bedrock Claude for 3 activity suggestions."""
    with tracer.start_as_current_span("node.generate_activities") as span:
        prompt = (
            f"You are a travel activities expert. The user is visiting "
            f"{state['destination']} during {state['dates']} and is interested in "
            f"{state['interests']}. "
            f"Recommend exactly 3 activities as plain text, one per line in this format:\n"
            f"  Activity: <name> — <1-sentence description>. Suggested time: <morning/afternoon/evening>.\n"
            f"No markdown, no headers, no bullets, no asterisks, no dollar signs."
        )
        span.set_attribute("gen_ai.operation.name", "chat")
        span.set_attribute("gen_ai.request.model", MODEL_ID)
        # Use the NEW OTel GenAI semantic convention (`gen_ai.input.messages` /
        # `gen_ai.output.messages`) that Omni's trace detail panel displays as
        # "Input/output". The older `gen_ai.prompt.{index}.content` naming is
        # recognised as an LLO (Large Language Object) pattern and gets stripped
        # from spans by the CloudWatch OTLP traces ingest before it reaches the UI.
        # The new convention is NOT in the LLO strip list (and even if it were,
        # the AWS Distro honours AWS_GENAI_CONTENT_EXTRACTION_OPT_OUT).
        #
        # SIZE WARNING: X-Ray caps total span attribute payload at roughly
        # 256 KB per span. Our current prompt is well under 1 KB and the
        # Claude response for a 3-activity list is a few hundred bytes, so we
        # have ~250 KB headroom. If a future caller injects a long RAG context
        # or a chatty tool schema into the user_message, this payload could
        # bump against the cap and X-Ray will silently drop over-cap spans.
        # If that happens, truncate the JSON payload here before setting the
        # attribute (e.g. keep the last N chars) rather than relying on the
        # collector to do it downstream.
        span.set_attribute(
            "gen_ai.input.messages",
            json.dumps(
                [{"role": "user", "parts": [{"type": "text", "content": prompt}]}]
            ),
        )
        try:
            resp = _bedrock.converse(
                modelId=MODEL_ID,
                messages=[{"role": "user", "content": [{"text": prompt}]}],
                inferenceConfig={"maxTokens": 500, "temperature": 0.7},
            )
            text = resp["output"]["message"]["content"][0]["text"].strip()
            usage = resp.get("usage", {})
            span.set_attribute("gen_ai.usage.input_tokens", usage.get("inputTokens", 0))
            span.set_attribute("gen_ai.usage.output_tokens", usage.get("outputTokens", 0))
            span.set_attribute(
                "gen_ai.output.messages",
                json.dumps(
                    [
                        {
                            "role": "assistant",
                            "parts": [{"type": "text", "content": text}],
                            "finish_reason": resp.get("stopReason", "stop"),
                        }
                    ]
                ),
            )
            return {**state, "activities": text}
        except Exception as e:
            log.exception("Bedrock invocation failed")
            span.record_exception(e)
            span.set_attribute("otel.status_code", "ERROR")
            return {**state, "error": f"{type(e).__name__}: {e}", "activities": ""}


# -------------------------------------------------- GRAPH
def _build_graph():
    g = StateGraph(ActivitiesState)
    g.add_node("extract_params", extract_params)
    g.add_node("generate_activities", generate_activities)
    g.set_entry_point("extract_params")
    g.add_edge("extract_params", "generate_activities")
    g.add_edge("generate_activities", END)
    return g.compile()


_graph = _build_graph()


# -------------------------------------------------- API
app = FastAPI(title="Travel Concierge — Activities Specialist (GCP / LangGraph)")


@app.get("/ping")
async def ping():
    return {"status": "ok", "service": "activities", "cloud": "gcp", "framework": "langgraph"}


@app.post("/invocations")
async def invocations(request: Request):
    body = await request.json()
    data = body.get("input", body)
    message = data.get("message", "").strip()
    session_id = data.get("session_id") or ""
    if not message:
        return JSONResponse({"error": "message cannot be empty"}, status_code=400)

    # Extract orchestrator's actual trace context from the application payload
    # (not the HTTP header, which can be rewritten by intermediaries). Attach
    # it so our spans nest under the orchestrator's current span.
    tc = (body.get("_trace_context") or {}) if isinstance(body, dict) else {}
    parent_ctx = _w3c.extract(tc) if tc.get("traceparent") else None
    token = otel_context.attach(parent_ctx) if parent_ctx is not None else None

    try:
        with tracer.start_as_current_span("sub_agent.invocation") as root_span:
            root_span.set_attribute("sub_agent.name", "activities")
            if session_id:
                # Required by the AgentCore online-eval pipeline to cluster
                # this trace into a scored session.
                root_span.set_attribute("session.id", session_id)
            with tracer.start_as_current_span("activities_graph.invoke") as span:
                span.set_attribute("gen_ai.agent.name", "activities")
                span.set_attribute("gen_ai.operation.name", "invoke_agent")
                if session_id:
                    span.set_attribute("session.id", session_id)
                final: dict[str, Any] = _graph.invoke({"user_message": message})
    finally:
        if token is not None:
            otel_context.detach(token)

    if final.get("error"):
        return JSONResponse(
            {"error": final["error"], "activities": ""},
            status_code=500,
        )
    return {
        "destination": final.get("destination"),
        "dates": final.get("dates"),
        "interests": final.get("interests"),
        "activities": final.get("activities"),
    }


if __name__ == "__main__":
    import uvicorn
    port = int(os.environ.get("PORT", "8080"))
    uvicorn.run(app, host="0.0.0.0", port=port, log_level="info")  # nosec B104 - containerized FastAPI listens on all container-internal interfaces; network isolation is enforced by the host (Cloud Run)
