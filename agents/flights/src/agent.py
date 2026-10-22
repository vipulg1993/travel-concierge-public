"""Flights specialist agent.

Trace shape per request:

    invoke_agent (this specialist)
    ├── gen_ai.chat          (model picks the tool and reasons about query)
    └── tool_call: search_flights_db
        └── dynamodb.get_item  (TABLE=travel-concierge-flights)

No per-request SSM reads. Failure scenarios driven by real AWS resource
changes (IAM policy on `travel-concierge-flights-role`), surfaced natively
through `AccessDeniedException` on the DynamoDB span.
"""

import json


import logging
import os
from decimal import Decimal

import boto3
from strands import Agent, tool
from strands.models.bedrock import BedrockModel
log = logging.getLogger(__name__)

REGION = os.environ.get("AWS_REGION", "us-east-1")
MODEL_ID = os.environ.get(
    "MODEL_ID", "us.anthropic.claude-sonnet-4-5-20250929-v1:0"
)
TABLE_NAME = os.environ.get("FLIGHTS_TABLE", "travel-concierge-flights")
# Per-service region override. AgentCore pins AWS_REGION to the runtime's
# region, so a workload that wants a configurable DynamoDB region (e.g. for
# a multi-region deployment) reads a dedicated env var and falls back to
# AWS_REGION. In this demo, Scenario D flips DDB_REGION to simulate config
# drift pointing the flights specialist at the wrong region.
DDB_REGION = os.environ.get("DDB_REGION") or REGION

_ddb = boto3.resource("dynamodb", region_name=DDB_REGION)
_table = _ddb.Table(TABLE_NAME)
log.info("flights specialist ready: ddb_region=%s table=%s model=%s",
         DDB_REGION, TABLE_NAME, MODEL_ID)


def _decimal_safe(obj):
    """Convert DynamoDB Decimal values to int/float for JSON serialization."""
    if isinstance(obj, list):
        return [_decimal_safe(x) for x in obj]
    if isinstance(obj, dict):
        return {k: _decimal_safe(v) for k, v in obj.items()}
    if isinstance(obj, Decimal):
        # Preserve ints as ints; floats otherwise.
        if obj == obj.to_integral_value():
            return int(obj)
        return float(obj)
    return obj


@tool
def search_flights_db(
    destination: str,
    max_price: int = 2000,
    departure_start: str = "",
    departure_end: str = "",
) -> str:
    """Search the flights database for routes to a destination within a price cap.

    Args:
        destination: Destination city (case-insensitive, matches 'city' attribute).
        max_price: Maximum ticket price in USD; defaults to 2000.
        departure_start: Earliest acceptable departure date in ISO form, e.g.
            '2026-12-22'. If omitted, no lower bound is applied.
        departure_end: Latest acceptable departure date in ISO form, e.g.
            '2026-12-28'. If omitted, no upper bound is applied.
    """
    # DynamoDB get_item — produces an aws.dynamodb span automatically via OTel.
    # Schema: one Item per destination (PK), with embedded "flights" list.
    resp = _table.get_item(Key={"destination": destination.lower()})
    item = _decimal_safe(resp.get("Item") or {})
    all_flights = item.get("flights", [])

    # Filter by max price
    affordable = [x for x in all_flights if x.get("price", 10 ** 9) <= max_price]

    # Filter by departure window (string prefix compare on YYYY-MM-DD works fine)
    start = departure_start.strip() or "0000-00-00"
    end = departure_end.strip() or "9999-99-99"
    in_window = [
        f for f in affordable
        if start <= str(f.get("departure", ""))[:10] <= end
    ]

    # Sort by departure date (earliest first — this IS the outbound leg) and
    # then by price so the top-2 cheapest flights on the earliest date come first.
    in_window.sort(key=lambda x: (
        str(x.get("departure", ""))[:10],
        x.get("price", 10 ** 9),
        x.get("duration_min", 10 ** 9),
    ))

    return json.dumps(
        {
            "destination": destination,
            "max_price": max_price,
            "departure_start": departure_start or "any",
            "departure_end": departure_end or "any",
            "count_total": len(all_flights),
            "count_in_price": len(affordable),
            "count_in_window": len(in_window),
            "flights": in_window[:5],
        },
        default=str,
    )


SYSTEM_PROMPT = """You are the Flights Specialist. When given a travel query:

1. Extract the destination, (optional) budget, and departure date range.
2. Normalize the date range to ISO dates (YYYY-MM-DD). Examples:
     "Dec 22-28 2026"          -> departure_start=2026-12-22  departure_end=2026-12-28
     "Christmas week 2026"     -> departure_start=2026-12-22  departure_end=2026-12-28
     "New Year Dec 30 2026 - Jan 3 2027" -> 2026-12-30 to 2027-01-03
     "May 2026"                -> 2026-05-01 to 2026-05-31
   If no date hint is given, pass empty strings so no date filter is applied.
3. Call `search_flights_db` with destination, max_price (default 2000),
   departure_start, and departure_end.
4. If `count_in_window` is 0 but `count_in_price` is non-zero, tell the user
   "no flights available in that date range" and show one or two of the
   closest-date alternatives for context — do NOT silently recommend an
   out-of-window flight.
5. If `count_in_window` is non-zero, prefer flights with the EARLIEST
   departure date in the window (the outbound leg of the trip is at the
   start of the window, not the end). Among flights sharing the earliest
   date, pick the cheapest. Return the top 2 with airline, departure date,
   price, and duration.

Keep responses short and factual. Never fabricate airline names, times, or prices.
"""


def create_flights_agent() -> Agent:
    return Agent(
        model=BedrockModel(model_id=MODEL_ID, region_name=REGION),
        tools=[search_flights_db],
        system_prompt=SYSTEM_PROMPT,
        trace_attributes={"gen_ai.capture_message_content": True},
    )
