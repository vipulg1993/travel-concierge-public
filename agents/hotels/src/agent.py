"""Hotels specialist agent — DynamoDB-backed hotel search.

Trace shape per request:

    invoke_agent (this specialist)
    ├── gen_ai.chat
    └── tool_call: search_hotels_db
        └── dynamodb.get_item  (TABLE=travel-concierge-hotels)

Failure scenarios driven by real AWS resource changes (DynamoDB table
capacity change on `travel-concierge-hotels`), surfaced natively through
`ProvisionedThroughputExceededException` on the DynamoDB span.
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
TABLE_NAME = os.environ.get("HOTELS_TABLE", "travel-concierge-hotels")

_ddb = boto3.resource("dynamodb", region_name=REGION)
_table = _ddb.Table(TABLE_NAME)


def _decimal_safe(obj):
    if isinstance(obj, list):
        return [_decimal_safe(x) for x in obj]
    if isinstance(obj, dict):
        return {k: _decimal_safe(v) for k, v in obj.items()}
    if isinstance(obj, Decimal):
        if obj == obj.to_integral_value():
            return int(obj)
        return float(obj)
    return obj


@tool
def search_hotels_db(destination: str, max_price_per_night: int = 300) -> str:
    """Search the hotel database for lodging options in a destination city.

    Args:
        destination: Destination city (case-insensitive, matches 'city' attribute).
        max_price_per_night: Maximum per-night price in USD. Defaults to 300.
    """
    # Schema: one Item per destination (PK), with embedded "hotels" list.
    resp = _table.get_item(Key={"destination": destination.lower()})
    item = _decimal_safe(resp.get("Item") or {})
    all_hotels = item.get("hotels", [])

    affordable = [
        x for x in all_hotels if x.get("price_per_night", 10 ** 9) <= max_price_per_night
    ]
    affordable.sort(key=lambda x: x.get("price_per_night", 10 ** 9))

    return json.dumps(
        {
            "destination": destination,
            "max_price_per_night": max_price_per_night,
            "count_total": len(all_hotels),
            "count_affordable": len(affordable),
            "hotels": affordable[:5],
        },
        default=str,
    )


SYSTEM_PROMPT = """You are the Hotels Specialist. When given a hotel search query:

1. Extract the destination and (optional) nightly budget from the user's query.
2. Call `search_hotels_db` with those parameters.
3. Return a brief 2–3 sentence summary of the top 2 affordable options with
   hotel name, star rating, and nightly rate. If no results, say so clearly.

Keep responses short and factual. Never fabricate hotel names, prices, or ratings.
"""


def create_hotels_agent() -> Agent:
    return Agent(
        model=BedrockModel(model_id=MODEL_ID, region_name=REGION),
        tools=[search_hotels_db],
        system_prompt=SYSTEM_PROMPT,
        trace_attributes={"gen_ai.capture_message_content": True},
    )
