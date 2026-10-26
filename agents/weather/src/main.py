"""Weather specialist — FastAPI entrypoint. Identical shape to flights/main.py."""

import json
import logging
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

from agent import create_weather_agent  # noqa: E402

_tracer = otel_trace.get_tracer(__name__)
_w3c = TraceContextTextMapPropagator()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
)
log = logging.getLogger(__name__)
for noisy in ("strands.agent.agent", "strands.tools", "opentelemetry"):
    logging.getLogger(noisy).setLevel(logging.WARNING)


class InvocationInput(BaseModel):
    message: str
    session_id: Optional[str] = None
    user_id: Optional[str] = None


def _sse(event: dict) -> str:
    return f"data: {json.dumps(event, ensure_ascii=False, default=str)}\n\n"


async def _stream(message: str, parent_ctx=None, session_id: Optional[str] = None) -> AsyncGenerator[str, None]:
    token = otel_context.attach(parent_ctx) if parent_ctx is not None else None
    try:
        with _tracer.start_as_current_span("sub_agent.invocation") as span:
            span.set_attribute("sub_agent.name", "weather")
            if session_id:
                span.set_attribute("session.id", session_id)
            async for item in _stream_impl(message):
                yield item
    finally:
        if token is not None:
            otel_context.detach(token)


async def _stream_impl(message: str) -> AsyncGenerator[str, None]:
    agent = create_weather_agent()
    tool_id_to_name: dict = {}
    seen: set = set()
    tool_error: Optional[str] = None
    try:
        async for event in agent.stream_async(message):
            if "message" in event:
                msg = event["message"]
                content = (
                    msg.content if hasattr(msg, "content")
                    else msg.get("content", []) if isinstance(msg, dict)
                    else []
                ) or []
                for item in content:
                    if not isinstance(item, dict):
                        continue
                    if "toolUse" in item:
                        tu = item["toolUse"]
                        tid, name = tu.get("toolUseId", ""), tu.get("name", "")
                        if tid and name and tid not in seen:
                            seen.add(tid)
                            tool_id_to_name[tid] = name
                            yield _sse({"type": "tool_use", "name": name, "input": tu.get("input", {})})
                    if "toolResult" in item:
                        tr = item["toolResult"]
                        name = tool_id_to_name.get(tr.get("toolUseId", ""), "")
                        parts = []
                        for c in tr.get("content", []) or []:
                            if isinstance(c, dict) and "text" in c:
                                parts.append(c["text"])
                        text = "\n".join(parts)
                        yield _sse({"type": "tool_result", "name": name, "content": text})
                        if tr.get("status") == "error":
                            tool_error = f"tool {name!r} failed: {text[:200]}"
            elif "data" in event and not event.get("reasoning"):
                yield _sse({"type": "text", "content": event["data"]})
        yield _sse({"type": "done"})
    except Exception as e:
        log.exception("Weather specialist stream failed")
        yield _sse({"type": "error", "content": f"{type(e).__name__}: {e}"})
        yield _sse({"type": "done"})
        raise

    if tool_error:
        raise RuntimeError(tool_error)


app = FastAPI(title="Travel Concierge — Weather Specialist")


@app.get("/ping")
async def ping():
    return {"status": "ok"}


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
