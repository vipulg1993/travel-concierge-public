"""Weather specialist agent.

Backed by the `travel-concierge-weather-provider` Lambda. The specialist calls
the Lambda via the AWS SDK (boto3), producing an `aws.lambda.invoke` child span
in the OTel trace. The Lambda's own `AWS/Lambda.Duration` and `Errors` metrics
are the native alarm sources (no custom instrumentation needed).
"""

import json
import logging
import os

import boto3
from strands import Agent, tool
from strands.models.bedrock import BedrockModel

log = logging.getLogger(__name__)

REGION = os.environ.get("AWS_REGION", "us-east-1")
MODEL_ID = os.environ.get(
    "MODEL_ID", "us.anthropic.claude-sonnet-4-5-20250929-v1:0"
)
WEATHER_PROVIDER_FN = os.environ.get(
    "WEATHER_PROVIDER_FN", "travel-concierge-weather-provider"
)

_lambda = boto3.client("lambda", region_name=REGION)


def _call_weather_provider(destination: str, dates: str) -> dict:
    """Fetch a forecast from the weather-provider Lambda via direct SDK invoke.

    Produces an `aws.lambda.invoke` span on the trace. Requires the weather
    role to have `lambda:InvokeFunction` on this function (granted in
    `infra/setup.py`).
    """
    payload = {"destination": destination, "dates": dates}
    body = json.dumps(payload).encode("utf-8")

    resp = _lambda.invoke(
        FunctionName=WEATHER_PROVIDER_FN,
        InvocationType="RequestResponse",
        Payload=body,
    )
    data = json.loads(resp["Payload"].read().decode("utf-8"))
    if "errorMessage" in data:
        raise RuntimeError(
            f"weather-provider lambda error: {data['errorMessage']}"
        )
    if isinstance(data, dict) and "body" in data and "statusCode" in data:
        try:
            return json.loads(data["body"])
        except Exception:
            return {"raw": data["body"]}
    return data


@tool
def get_forecast(destination: str, dates: str) -> str:
    """Return a forecast summary for a destination over a date range.

    Args:
        destination: City name.
        dates: Human-readable date range (e.g., "Dec 22-28 2026").
    """
    forecast = _call_weather_provider(destination, dates)
    return json.dumps(
        {
            "destination": destination,
            "dates": dates,
            "forecast": forecast,
        },
        default=str,
    )


SYSTEM_PROMPT = """You are the Weather Specialist. When given a destination and date range:

1. Call `get_forecast` with those parameters.
2. Return a brief 2-sentence summary covering typical temperature range, chance
   of rain, and one packing hint (e.g., "bring a rain jacket" or "sunglasses").

Keep responses short. Never invent weather data the tool did not return.
"""


def create_weather_agent() -> Agent:
    return Agent(
        model=BedrockModel(model_id=MODEL_ID, region_name=REGION),
        tools=[get_forecast],
        system_prompt=SYSTEM_PROMPT,
        trace_attributes={"gen_ai.capture_message_content": True},
    )
