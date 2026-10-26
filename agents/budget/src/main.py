"""Budget specialist — FastAPI entrypoint, backed by the CrewAI crew in agent.py.

Parses the orchestrator's natural-language budget prompt (which embeds flight
and hotel JSON), runs the Analyst → Reviewer crew, and streams the result back
as SSE events for consistency with the other specialists' /invocations contract.
"""

import json
import logging
import re
import sys
from pathlib import Path
from typing import AsyncGenerator, Optional

import uvicorn
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from opentelemetry import context as otel_context
from opentelemetry import trace as otel_trace
from opentelemetry.trace.propagation.tracecontext import TraceContextTextMapPropagator

SRC = Path(__file__).parent
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from agent import invoke_budget  # noqa: E402

_tracer = otel_trace.get_tracer(__name__)
_w3c = TraceContextTextMapPropagator()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
)
log = logging.getLogger(__name__)
for noisy in ("opentelemetry", "LiteLLM"):
    logging.getLogger(noisy).setLevel(logging.WARNING)


class InvocationInput(BaseModel):
    message: str
    session_id: Optional[str] = None
    user_id: Optional[str] = None


def _sse(event: dict) -> str:
    return f"data: {json.dumps(event, ensure_ascii=False, default=str)}\n\n"


def _extract_crew_inputs(message: str) -> tuple[str, str, int]:
    """Pull the flights JSON, hotels JSON, and day count out of the natural-
    language message the orchestrator sends.

    Orchestrator sends something like:
        "Given flight options: {...flights json...}
         and hotel options: {...hotels json...}
         over 7 days, estimate the total trip cost..."
    """
    days = 7
    m = re.search(r"over\s+(\d+)\s+days", message, re.I)
    if m:
        try:
            days = int(m.group(1))
        except Exception:
            pass

    # Pull the two JSON blobs out — greedy to allow nested braces, then
    # trim to the first balanced JSON object we can find.
    flights_json = _grab_json_after(message, "flight options:") or "{}"
    hotels_json = _grab_json_after(message, "hotel options:") or "{}"
    return flights_json, hotels_json, days


def _grab_json_after(text: str, marker: str) -> Optional[str]:
    """Return the first balanced {...} JSON object found after `marker`."""
    idx = text.lower().find(marker.lower())
    if idx == -1:
        return None
    start = text.find("{", idx)
    if start == -1:
        return None
    depth = 0
    for i in range(start, len(text)):
        c = text[i]
        if c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0:
                return text[start : i + 1]
    return text[start:]


async def _stream(message: str, parent_ctx=None, session_id: Optional[str] = None) -> AsyncGenerator[str, None]:
    token = otel_context.attach(parent_ctx) if parent_ctx is not None else None
    try:
        with _tracer.start_as_current_span("sub_agent.invocation") as span:
            span.set_attribute("sub_agent.name", "budget")
            if session_id:
                span.set_attribute("session.id", session_id)
            async for item in _stream_impl(message):
                yield item
    finally:
        if token is not None:
            otel_context.detach(token)


async def _stream_impl(message: str) -> AsyncGenerator[str, None]:
    try:
        flights_json, hotels_json, days = _extract_crew_inputs(message)
        result_text = await invoke_budget(flights_json, hotels_json, days)
        yield _sse({"type": "text", "content": result_text})
        yield _sse({"type": "done"})
    except Exception as e:
        log.exception("Budget specialist crew failed")
        yield _sse({"type": "error", "content": f"{type(e).__name__}: {e}"})
        yield _sse({"type": "done"})


app = FastAPI(title="Travel Concierge — Budget Specialist (CrewAI)")


@app.get("/ping")
async def ping():
    return {"status": "ok", "framework": "crewai", "agents": ["analyst", "reviewer"]}


@app.post("/invocations")
async def invocations(request: Request):
    body = await request.json()
    data = InvocationInput(**(body.get("input", body)))
    if not data.message.strip():
        raise HTTPException(status_code=400, detail="message cannot be empty")
    tc = (body.get("_trace_context") or {}) if isinstance(body, dict) else {}
    parent_ctx = _w3c.extract(tc) if tc.get("traceparent") else None
    return StreamingResponse(
        _stream(data.message, parent_ctx=parent_ctx, session_id=data.session_id),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=8080, log_level="info")  # nosec B104 - containerized FastAPI listens on all container-internal interfaces; network isolation is enforced by the host (AgentCore / EKS pod / Cloud Run)
