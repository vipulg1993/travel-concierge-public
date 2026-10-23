"""Travel Concierge — Streamlit frontend.

Pulls the orchestrator ARN from SSM and lets a human run queries against the
multi-agent system. Shows the SSE event stream with a tool-call timeline on the
right so the demo audience can see the orchestrator fanning out.

Run:
    AWS_PROFILE=omni-demo streamlit run frontend/app.py
"""

import json
import os
import time
import uuid

import boto3
import requests
import streamlit as st

REGION = os.environ.get("AWS_REGION", "us-east-1")
PROFILE = os.environ.get("AWS_PROFILE") or (
    None if os.environ.get("AWS_ACCESS_KEY_ID") else "omni-demo"
)

if PROFILE:
    _session = boto3.Session(profile_name=PROFILE, region_name=REGION)
else:
    _session = boto3.Session(region_name=REGION)

# When set, Streamlit POSTs directly to the local orchestrator over HTTP and
# skips the SSM lookup / `bedrock-agentcore:InvokeAgentRuntime` SDK path. This
# is the F2 local topology — one process per agent, same SSE wire shape.
ORCHESTRATOR_LOCAL_URL = os.environ.get("ORCHESTRATOR_LOCAL_URL", "")

st.set_page_config(page_title="Travel Concierge", page_icon="✈️", layout="wide")


import re


def _safe_md(text: str) -> str:
    """Normalize the LLM's streamed markdown so Streamlit renders it cleanly.

    1. Escape `$` so Streamlit does not treat `$...$` as inline LaTeX math.
       Our responses carry many prices (`$418`, `$2,500`) and without this
       everything between successive dollars renders in math italic with
       collapsed spaces.
    2. Insert a blank line (two newlines) before each known section label
       (`Flight:`, `Hotel:`, `Weather:`, `Activities:`, `Total:`, bold or
       plain) so the structured summary renders as separate paragraphs
       instead of collapsing into one wall of text.
    3. Collapse 3+ consecutive newlines to 2.
    """
    if not text:
        return ""
    s = text.replace("$", "\\$")

    # Markdown headings
    s = re.sub(r"(?<!\n)(##+ )", r"\n\n\1", s)

    # Section labels — bold OR plain. Match both the first occurrence (which
    # may be after other prose) and subsequent ones (separated by only one \n).
    labels = (
        "Flight:", "Hotel:", "Weather:", "Activities:", "Activity:",
        "Total:", "Total Cost:",
        "Budget:", "Pick:", "Recommendation",
    )
    for label in labels:
        esc = re.escape(label)
        # Case 1: label glued to preceding text with no newline at all.
        s = re.sub(r"(?<=\S)([ \t]*)(\*\*)?(" + esc + r")", r"\n\n\2\3", s)
        # Case 2: label on its own line but only single-newline separated;
        # upgrade to a blank-line separator so markdown treats it as a new paragraph.
        s = re.sub(r"(?<!\n)\n(\*\*)?(" + esc + r")", r"\n\n\1\2", s)

    # Collapse runs of blank lines
    s = re.sub(r"\n{3,}", "\n\n", s)
    return s.strip()


@st.cache_resource
def _clients():
    return {
        "ac": _session.client("bedrock-agentcore"),
        "ssm": _session.client("ssm"),
    }


def _orchestrator_arn() -> str | None:
    try:
        return _clients()["ssm"].get_parameter(
            Name="/travel-concierge/runtime-arns/orchestrator"
        )["Parameter"]["Value"]
    except Exception:
        return None


def _resolve_target() -> tuple[str, str] | None:
    """Return (kind, address) describing where /invocations lives.

      ("local",     "http://127.0.0.1:8080")  — Phase B dev stack
      ("agentcore", "<orchestrator-arn>")     — prod default (SSM)

    Local mode wins when `ORCHESTRATOR_LOCAL_URL` is set; otherwise falls back
    to the SSM-resolved AgentCore ARN. Returns None if neither is available
    (so the caller can show a helpful error).
    """
    if ORCHESTRATOR_LOCAL_URL:
        return ("local", ORCHESTRATOR_LOCAL_URL.rstrip("/"))
    arn = _orchestrator_arn()
    if arn:
        return ("agentcore", arn)
    return None


def _invoke(target: tuple[str, str], message: str, session_id: str, user_id: str):
    """Yield SSE events from the orchestrator, either locally (HTTP) or via AgentCore (SDK)."""
    kind, addr = target
    payload = {"input": {"message": message, "session_id": session_id, "user_id": user_id}}
    if kind == "local":
        # POST to the local orchestrator uvicorn process. The orchestrator
        # streams the same SSE "data: {json}" frames as the AgentCore path,
        # so the frame-parsing loop downstream is shared.
        resp = requests.post(
            f"{addr}/invocations",
            json=payload,
            headers={"Content-Type": "application/json",
                     "Accept": "text/event-stream, application/json"},
            stream=True,
            timeout=180,
        )
        if resp.status_code >= 400:
            yield {"type": "error",
                   "content": f"local orchestrator HTTP {resp.status_code}: {resp.text[:200]}"}
            return
        buf = b""
        for chunk in resp.iter_content(chunk_size=None):
            if not chunk:
                continue
            buf += chunk
            while b"\n\n" in buf:
                frame, buf = buf.split(b"\n\n", 1)
                if frame.startswith(b"data: "):
                    try:
                        yield json.loads(frame[6:])
                    except Exception:
                        continue
        return

    ac = _clients()["ac"]
    resp = ac.invoke_agent_runtime(
        agentRuntimeArn=addr,
        runtimeSessionId=session_id,
        runtimeUserId=user_id,
        payload=json.dumps(payload).encode("utf-8"),
        qualifier="DEFAULT",
    )
    buf = b""
    for chunk in resp["response"]:
        if not chunk:
            continue
        buf += chunk
        while b"\n\n" in buf:
            frame, buf = buf.split(b"\n\n", 1)
            if frame.startswith(b"data: "):
                try:
                    yield json.loads(frame[6:])
                except Exception:
                    continue


# ---------- SIDEBAR ----------
with st.sidebar:
    st.title("Travel Concierge")
    st.caption("Multi-agent demo on AgentCore Runtime")

    target = _resolve_target()
    if target is None:
        st.error(
            "Orchestrator address not resolved. Either run `infra/deploy.py` "
            "(prod) or set `ORCHESTRATOR_LOCAL_URL` and launch `scripts/dev-local.sh`."
        )
        st.stop()

    if target[0] == "local":
        st.markdown("**Orchestrator (local)**")
        st.code(target[1], language=None)
    else:
        st.markdown("**Orchestrator ARN**")
        st.code(target[1].split("/")[-1], language=None)

    if "user_id" not in st.session_state:
        st.session_state.user_id = "demo_user"
    st.session_state.user_id = st.selectbox(
        "User", ["alice", "bob", "carol", "dan", "eve", "demo_user"],
        index=5,
    )

    if "session_id" not in st.session_state:
        st.session_state.session_id = f"demo-{uuid.uuid4().hex[:16]}" + "x" * 16

    if st.button("New session"):
        st.session_state.session_id = f"demo-{uuid.uuid4().hex[:16]}" + "x" * 16
        st.session_state.messages = []
        st.rerun()

    st.markdown("**Session ID**")
    st.code(st.session_state.session_id[:24] + "...", language=None)

    st.markdown("---")
    st.markdown("**Try these**")
    quick_prompts = [
        "Ski trip to Denver Dec 26-30 2026, 4-star lodge, budget $2500.",
        "Deep dish weekend in Chicago Dec 23-27 2026 under $2000.",
        "Country music getaway to Nashville Dec 25-29 2026, mid-range hotel.",
        "Historic walking tour of Boston Dec 22-27 2026, boutique hotel, budget $2800.",
    ]
    for p in quick_prompts:
        if st.button(p, use_container_width=True):
            st.session_state._pending = p
            st.rerun()


# ---------- MAIN ----------
if "messages" not in st.session_state:
    st.session_state.messages = []

st.title("✈️ Travel Concierge")
st.caption(
    "A multi-agent travel-planning demo on AgentCore Runtime. "
    "Each request fans out to 5 specialist agents (Flights, Hotels, Weather, Budget, Activities). "
    "Watch the trace in CloudWatch Omni."
)

# Replay history
for m in st.session_state.messages:
    with st.chat_message(m["role"]):
        if m.get("text"):
            st.markdown(_safe_md(m["text"]))
        if m.get("tools"):
            with st.expander(f"🔧 {len(m['tools'])} tool call(s)", expanded=False):
                for t in m["tools"]:
                    st.markdown(f"**{t['name']}**")
                    st.code(json.dumps(t.get("input", {}), indent=2), language="json")
                    if t.get("result"):
                        st.text(t["result"][:500] + ("…" if len(t.get("result", "")) > 500 else ""))

# Chat input
pending = st.session_state.pop("_pending", None) if "_pending" in st.session_state else None
user_msg = pending or st.chat_input("Where do you want to travel?")

if user_msg:
    st.session_state.messages.append({"role": "user", "text": user_msg})
    with st.chat_message("user"):
        st.markdown(user_msg)

    with st.chat_message("assistant"):
        text_slot = st.empty()
        tool_slot = st.container()
        collected_text = []
        tool_calls = []
        tool_last_by_name = {}
        t0 = time.time()
        try:
            for evt in _invoke(target, user_msg, st.session_state.session_id, st.session_state.user_id):
                t = evt.get("type")
                if t == "text":
                    collected_text.append(evt.get("content", ""))
                    text_slot.markdown(_safe_md("".join(collected_text)))
                elif t == "tool_use":
                    name = evt.get("name", "?")
                    tool = {"name": name, "input": evt.get("input", {}), "result": None}
                    tool_calls.append(tool)
                    tool_last_by_name[name] = tool
                    with tool_slot:
                        st.info(f"🔧 calling **{name}** — {json.dumps(tool['input'])[:80]}")
                elif t == "tool_result":
                    name = evt.get("name", "?")
                    tgt = tool_last_by_name.get(name)
                    if tgt is not None:
                        tgt["result"] = evt.get("content", "")
                elif t == "error":
                    with tool_slot:
                        st.error(f"❌ {evt.get('content', '')}")
            dur = time.time() - t0
            with tool_slot:
                st.caption(f"⏱  completed in {dur:.1f}s  •  {len(tool_calls)} tool call(s)")
        except Exception as e:
            text_slot.error(f"Request failed: {type(e).__name__}: {e}")

        st.session_state.messages.append({
            "role": "assistant",
            "text": "".join(collected_text),
            "tools": tool_calls,
        })
