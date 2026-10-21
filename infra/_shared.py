"""Shared constants + boto3 session for travel-concierge/infra scripts."""

import os

import boto3

REGION = os.environ.get("AWS_REGION", "us-east-1")
# Default to the demo profile when the caller hasn't exported AWS_PROFILE
# and isn't using static access keys.
PROFILE = os.environ.get("AWS_PROFILE") or (
    None if os.environ.get("AWS_ACCESS_KEY_ID") else "omni-demo"
)

if PROFILE:
    SESSION = boto3.Session(profile_name=PROFILE, region_name=REGION)
else:
    SESSION = boto3.Session(region_name=REGION)

# Account id: prefer the AWS_ACCOUNT_ID env var (so a fresh-clone run with the
# env var set does not need to hit STS at import time); fall back to STS
# caller identity using the session above. _config.AWS_ACCOUNT_ID is the
# single source of truth; this module re-exports it as `ACCOUNT` so existing
# callers (deploy.py, setup.py, teardown.py) keep working unchanged.
if os.environ.get("AWS_ACCOUNT_ID"):
    from _config import AWS_ACCOUNT_ID as ACCOUNT
else:
    ACCOUNT = SESSION.client("sts").get_caller_identity()["Account"]

# Resources
FLIGHTS_TABLE = "travel-concierge-flights"
HOTELS_TABLE = "travel-concierge-hotels"
WEATHER_PROVIDER_FN = "travel-concierge-weather-provider"
# Backwards-compat alias retained during migration from the previous name.
MOCK_WEATHER_FN = WEATHER_PROVIDER_FN

# SSM
POLICY_PREFIX = "/travel-concierge/policies"
RUNTIME_ARN_PREFIX = "/travel-concierge/runtime-arns"
WEATHER_PROVIDER_URL_PARAM = "/travel-concierge/weather-provider-url"
# Backwards-compat alias retained during migration from the previous name.
MOCK_WEATHER_URL_PARAM = WEATHER_PROVIDER_URL_PARAM

# Backwards-compat alias so older callers keep working during the rename.
TOGGLE_PREFIX = POLICY_PREFIX

# Only policies still in use after the Level-2 migration. Failure scenarios A,
# B, D now come from real AWS resource changes (IAM / Lambda config / DDB
# capacity) rather than SSM-driven synthetic faults. The budget prompt-variant
# policy remains for optional Scenario C (prompt regression) and model-as-judge
# evaluators.
POLICIES = {
    "budget/prompt_variant": ("stable", "Prompt variant for budget specialist: 'stable' or 'regressed'"),
}
TOGGLES = POLICIES  # backwards-compat alias

# IAM role names (one per agent, so each specialist has least-privilege)
ROLE_ORCHESTRATOR = "travel-concierge-orchestrator-role"
ROLE_FLIGHTS = "travel-concierge-flights-role"
ROLE_HOTELS = "travel-concierge-hotels-role"
ROLE_WEATHER = "travel-concierge-weather-role"
ROLE_BUDGET = "travel-concierge-budget-role"

ROLE_FOR_AGENT = {
    "orchestrator": ROLE_ORCHESTRATOR,
    "flights": ROLE_FLIGHTS,
    "hotels": ROLE_HOTELS,
    "weather": ROLE_WEATHER,
    "budget": ROLE_BUDGET,
}
