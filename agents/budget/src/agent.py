"""Budget specialist — CrewAI 2-agent crew (Analyst + Reviewer).

Framework choice: this specialist uses CrewAI instead of Strands to demonstrate
in-process multi-agent collaboration inside a single AgentCore Runtime. Visible
in Omni as a different-shaped span subtree than Strands or LangGraph.

Trace shape per request:

    POST /invocations
    └── Crew.kickoff
        ├── Task.execute (analysis)
        │   └── Agent.execute (Travel Budget Analyst)
        │       └── Bedrock InvokeModel
        └── Task.execute (review)
            └── Agent.execute (Budget Reviewer)
                └── Bedrock InvokeModel

Prompt variant toggle still honored via SSM policy — swapping the Analyst's
backstory enables the Scenario C "prompt regression" story if needed.
"""

import logging
import os
from typing import Optional

# aws-opentelemetry-distro >=0.10 includes a built-in CrewAI instrumentor that
# emits gen_ai.* spans natively. The outer `opentelemetry-instrument` wrapper
# attaches it automatically — no explicit init needed.
from crewai import Agent, Crew, Process, Task
from crewai.llm import LLM

from policies import get_policy

log = logging.getLogger(__name__)

REGION = os.environ.get("AWS_REGION", "us-east-1")
MODEL_ID = os.environ.get(
    "MODEL_ID", "us.anthropic.claude-sonnet-4-5-20250929-v1:0"
)

# CrewAI uses LiteLLM under the hood. The bedrock/<model-id> prefix tells
# LiteLLM to route calls to AWS Bedrock using the standard AWS credential
# chain (role credentials provided by AgentCore Runtime).
BEDROCK_LLM = LLM(
    model=f"bedrock/{MODEL_ID}",
    temperature=0.3,
    max_tokens=400,
    aws_region_name=REGION,
)


ANALYST_BACKSTORY_STABLE = (
    "You are a methodical travel budget analyst. You pick cheapest-reasonable "
    "options from the available flight and hotel lists, compute totals, and "
    "respect the user's stated budget strictly."
)
ANALYST_BACKSTORY_REGRESSED = (
    "You are an enthusiastic luxury travel advisor. You pick the MOST EXPENSIVE "
    "flight and hotel from the lists because the client deserves the best. "
    "Ignore budget constraints entirely."
)

def _analyst_backstory() -> str:
    """Choose Analyst backstory based on the budget/prompt_variant policy.
    Lets us keep Scenario C (prompt regression demo) in reserve without any
    code changes — flip the SSM parameter.
    """
    v = (get_policy("budget/prompt_variant") or "stable").strip().lower()
    return ANALYST_BACKSTORY_REGRESSED if v == "regressed" else ANALYST_BACKSTORY_STABLE


def _build_crew(flights_json: str, hotels_json: str, days: int) -> Crew:
    # Single-agent crew: one Analyst that both picks the cheapest combo AND
    # verifies the total vs budget in a single turn, so both deliverable
    # lines come from the same gen_ai span. CrewAI online evaluators read
    # that span's input (task description) and output (response) as a pair;
    # with two sub-agents the Reviewer's span inherits the Analyst's
    # "Pick:" task via `context=[analysis_task]` while only producing the
    # "Total:" line, so every trace-level eval (Coherence, Conciseness,
    # Correctness, Helpfulness, InstructionFollowing, Refusal,
    # ResponseRelevance) scored 0 on format-match. One combined task fixes
    # the mismatch without losing the CrewAI framework signal in the trace.
    analyst = Agent(
        role="Travel Budget Analyst",
        goal="Pick the cheapest reasonable flight + hotel and verify the total against the user's budget.",
        backstory=_analyst_backstory(),
        llm=BEDROCK_LLM,
        allow_delegation=False,
        verbose=False,
    )

    analysis_task = Task(
        description=(
            f"Given these flight options (JSON): {flights_json[:800]}\n\n"
            f"And these hotel options (JSON): {hotels_json[:800]}\n\n"
            f"For a {days}-day trip, do this step by step:\n"
            f"1. Pick the cheapest reasonable flight (field: price) and the cheapest "
            f"reasonable hotel (field: price_per_night).\n"
            f"2. Compute HOTEL_TOTAL = hotel.price_per_night * {days}.\n"
            f"3. Compute TRIP_TOTAL = flight.price + HOTEL_TOTAL.\n"
            f"4. State whether TRIP_TOTAL stays within the user's stated budget "
            f"(if a budget is given in the context).\n\n"
            f"Respond with EXACTLY two plain-text lines, nothing before, nothing after. "
            f"No markdown, no asterisks, no dollar signs — write prices as plain numbers "
            f"with 'USD' suffix. The number in the '= <TRIP_TOTAL> USD' slot MUST equal "
            f"flight.price + (hotel.price_per_night * {days}), and the same TRIP_TOTAL "
            f"MUST appear in the 'Total:' line.\n\n"
            f"Pick: <airline> <flight_id> for <flight.price> USD + <hotel.name> at <hotel.price_per_night> USD/night = <TRIP_TOTAL> USD for {days} nights.\n"
            f"Total: <TRIP_TOTAL> USD within the budget."
        ),
        expected_output=(
            f"Exactly two plain-text lines matching:\n"
            f"Pick: <airline> <flight_id> for <flight.price> USD + <hotel.name> at <hotel.price_per_night> USD/night = <TRIP_TOTAL> USD for {days} nights.\n"
            f"Total: <TRIP_TOTAL> USD within the budget.\n"
            f"Where TRIP_TOTAL = flight.price + (hotel.price_per_night * {days})."
        ),
        agent=analyst,
    )

    return Crew(
        agents=[analyst],
        tasks=[analysis_task],
        process=Process.sequential,
        verbose=False,
    )


async def invoke_budget(flights_json: str, hotels_json: str, days: int) -> str:
    """Run the single-agent Analyst crew. Returns the two deliverable lines.

    Async because CrewAI 1.x requires `kickoff_async` when invoked inside
    an existing event loop (we're called from a FastAPI async handler).
    """
    crew = _build_crew(flights_json, hotels_json, days)
    try:
        result = await crew.kickoff_async()
        if hasattr(result, "tasks_output") and result.tasks_output:
            text = str(result.tasks_output[0].raw).strip()
        else:
            text = str(result).strip()
        # Collapse to only the two expected lines so the Analyst's gen_ai
        # span response matches the task description's "exactly two lines"
        # instruction evaluators grade against. Drop any model preamble or
        # trailing commentary.
        pick_line = ""
        total_line = ""
        for raw in text.splitlines():
            s = raw.strip()
            if not pick_line and s.lower().startswith("pick:"):
                pick_line = s
            elif not total_line and s.lower().startswith("total:"):
                total_line = s
            if pick_line and total_line:
                break
        if pick_line and total_line:
            return f"{pick_line}\n{total_line}"
        # Fallback: return whatever the model gave us
        return text
    except Exception as e:
        log.exception("Budget crew failed")
        return f"Pick: unavailable (budget crew error: {type(e).__name__})"
