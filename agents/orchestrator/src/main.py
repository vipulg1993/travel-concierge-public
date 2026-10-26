"""Travel Concierge Orchestrator — FastAPI entrypoint.

AgentCore Runtime requires:
  POST /invocations — accepts user input, streams SSE event tree
  GET  /ping        — health check (returns 200)

Streams the following SSE event types to the client:
  tool_use    — {"type":"tool_use",   "name": "...", "input": {...}}
  tool_result — {"type":"tool_result","name": "...", "content": "..."}
  text        — {"type":"text",       "content": "..."}
  error       — {"type":"error",      "content": "..."}
  done        — {"type":"done"}
"""

import asyncio
import json
import logging
import os
import sys
from pathlib import Path
from typing import AsyncGenerator, Optional

import uvicorn
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

# Make `agent.py` importable as sibling module.
SRC = Path(__file__).parent
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from agent import create_orchestrator_agent  # noqa: E402

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
)
log = logging.getLogger(__name__)

# Quiet opinionated libraries
for noisy in ("strands.agent.agent", "strands.tools.mcp", "opentelemetry"):
    logging.getLogger(noisy).setLevel(logging.WARNING)


class InvocationInput(BaseModel):
    message: str
    session_id: Optional[str] = None
    user_id: Optional[str] = None


HEADER_SESSION = "X-Amzn-Bedrock-AgentCore-Runtime-Session-Id"
HEADER_USER = "X-Amzn-Bedrock-AgentCore-Runtime-User-Id"


def _sse(event: dict) -> str:
    return f"data: {json.dumps(event, ensure_ascii=False, default=str)}\n\n"


async def _stream_response(
    session_id: str, user_id: str, message: str
) -> AsyncGenerator[str, None]:
    agent = create_orchestrator_agent(session_id=session_id, user_id=user_id)
    seen_tools: set = set()
    tool_id_to_name: dict = {}

    try:
        async for event in agent.stream_async(message):
            # Capture tool_use + tool_result events from Strands' message stream
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
                        if tid and name and tid not in seen_tools:
                            seen_tools.add(tid)
                            tool_id_to_name[tid] = name
                            yield _sse({"type": "tool_use", "name": name, "input": tu.get("input", {})})
                    if "toolResult" in item:
                        tr = item["toolResult"]
                        tid = tr.get("toolUseId", "")
                        name = tool_id_to_name.get(tid, "")
                        parts = []
                        for c in tr.get("content", []) or []:
                            if isinstance(c, dict) and "text" in c:
                                parts.append(c["text"])
                        yield _sse({"type": "tool_result", "name": name, "content": "\n".join(parts)})
            elif "data" in event and not event.get("reasoning"):
                yield _sse({"type": "text", "content": event["data"]})

        yield _sse({"type": "done"})
    except Exception as e:
        log.exception("Orchestrator stream failed")
        yield _sse({"type": "error", "content": f"{type(e).__name__}: {e}"})
        yield _sse({"type": "done"})


app = FastAPI(title="Travel Concierge Orchestrator")


@app.get("/ping")
async def ping():
    return {"status": "ok"}


@app.post("/invocations")
async def invocations(request: Request):
    body = await request.json()
    data = InvocationInput(**(body.get("input", body)))
    session_id = (
        data.session_id
        or request.headers.get(HEADER_SESSION)
        or "default_session"
    )
    user_id = data.user_id or request.headers.get(HEADER_USER) or "default_user"
    if not data.message.strip():
        raise HTTPException(status_code=400, detail="message cannot be empty")

    return StreamingResponse(
        _stream_response(session_id, user_id, data.message),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=8080, log_level="info")  # nosec B104 - containerized FastAPI listens on all container-internal interfaces; network isolation is enforced by the host (AgentCore / EKS pod / Cloud Run)
