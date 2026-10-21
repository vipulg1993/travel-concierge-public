"""Activities Specialist — hosted on GCP (Cloud Shell), framework = LangGraph.

LangGraph node graph:

    START ─► extract_params ─► generate_activities ─► END

extract_params:
    Parse destination / dates / interests from the user message.
    (The orchestrator passes a natural-language query.)

generate_activities:
    Call Bedrock Claude via boto3 (cross-cloud).
    Return 3 holiday-season activity suggestions matched to destination
    and interests.

The trace shape for this agent in Omni:

    POST /invocations                       ← FastAPIInstrumentor
    └── langgraph.compiled_graph.invoke     ← LangGraph auto span (if any)
        ├── langgraph.node extract_params
        └── langgraph.node generate_activities
            └── aws.bedrock InvokeModel     ← BotocoreInstrumentor
                └── (Bedrock returns text)

The orchestrator (in AWS) sees this whole subtree under its own
`execute_tool recommend_activities` span, stitched by W3C traceparent.
"""

import json
import logging
import os
from typing import Any, Optional, TypedDict

import boto3
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from langgraph.graph import StateGraph, END

# Must be configured BEFORE FastAPI app is created so the instrumentor
# attaches to all routes.
from otel_setup import setup_tracing
setup_tracing()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
)
log = logging.getLogger(__name__)

REGION = os.environ.get("AWS_REGION", "us-east-1")
MODEL_ID = os.environ.get(
    "MODEL_ID", "us.anthropic.claude-sonnet-4-5-20250929-v1:0"
)

# Bedrock client — uses AWS_ACCESS_KEY_ID / AWS_SECRET_ACCESS_KEY from env
# (set via `source aws-creds.env` in Cloud Shell).
_bedrock = boto3.client("bedrock-runtime", region_name=REGION)


# ------------------------------------------------------------------ STATE
class ActivitiesState(TypedDict, total=False):
    user_message: str
    destination: str
    dates: str
    interests: str
    activities: str
    error: Optional[str]


# ------------------------------------------------------------------ NODES
def extract_params(state: ActivitiesState) -> ActivitiesState:
    """Light extraction — pull destination/dates/interests from the user message
    using a quick prompt, so the second node can concentrate on recommendations.
    """
    msg = state["user_message"]

    # The orchestrator passes a structured enough message that we don't need
    # an LLM round-trip here. We parse by convention: 'Suggest activities for
    # <destination> during <dates> interests: <interests>'.
    import re
    dest_match = re.search(r"activities?(?: in| for)?\s+([A-Za-z ]+?)(?:\s+(?:during|for|on|\d))", msg, re.I)
    dates_match = re.search(r"(?:during|for|on)\s+([^.]+)", msg, re.I)
    interests_match = re.search(r"interests?:\s*(.+?)(?:\.|$)", msg, re.I)
    return {
        **state,
        "destination": (dest_match.group(1).strip() if dest_match else "the destination"),
        "dates": (dates_match.group(1).strip() if dates_match else "the holiday season"),
        "interests": (interests_match.group(1).strip() if interests_match else "sightseeing"),
    }


def generate_activities(state: ActivitiesState) -> ActivitiesState:
    """Call Bedrock Claude to produce 3 activity suggestions."""
    prompt = (
        f"You are a travel activities expert. The user is visiting "
        f"{state['destination']} during {state['dates']} and is interested in "
        f"{state['interests']}. "
        f"Recommend exactly 3 activities as plain text, one per line in this format:\n"
        f"  Activity: <name> — <1-sentence description>. Suggested time: <morning/afternoon/evening>.\n"
        f"No markdown, no headers, no bullets, no asterisks, no dollar signs."
    )
    try:
        resp = _bedrock.converse(
            modelId=MODEL_ID,
            messages=[{"role": "user", "content": [{"text": prompt}]}],
            inferenceConfig={"maxTokens": 500, "temperature": 0.7},
        )
        text = resp["output"]["message"]["content"][0]["text"].strip()
        return {**state, "activities": text}
    except Exception as e:
        log.exception("Bedrock invocation failed")
        return {**state, "error": f"{type(e).__name__}: {e}", "activities": ""}


# ------------------------------------------------------------------ GRAPH
def _build_graph():
    g = StateGraph(ActivitiesState)
    g.add_node("extract_params", extract_params)
    g.add_node("generate_activities", generate_activities)
    g.set_entry_point("extract_params")
    g.add_edge("extract_params", "generate_activities")
    g.add_edge("generate_activities", END)
    return g.compile()


_graph = _build_graph()


# ------------------------------------------------------------------ API
app = FastAPI(title="Travel Concierge — Activities Specialist (GCP / LangGraph)")


@app.get("/ping")
async def ping():
    return {"status": "ok", "service": "activities", "cloud": "gcp", "framework": "langgraph"}


@app.post("/invocations")
async def invocations(request: Request):
    """AgentCore-compatible invocation shape.

    Accepts {"input": {"message": "...", "session_id": "...", "user_id": "..."}}
    and returns a plain JSON body (not SSE) with the activities text.
    """
    body = await request.json()
    data = body.get("input", body)
    message = data.get("message", "").strip()
    if not message:
        return JSONResponse({"error": "message cannot be empty"}, status_code=400)

    final: dict[str, Any] = _graph.invoke({"user_message": message})

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
    uvicorn.run(app, host="0.0.0.0", port=8080, log_level="info")
