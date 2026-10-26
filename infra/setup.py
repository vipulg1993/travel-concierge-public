#!/usr/bin/env python3
"""One-shot idempotent setup for the Travel Concierge demo workload.

Creates (or verifies) in order:
  1. SSM toggle parameters at /travel-concierge/toggles/* with safe defaults.
  2. DynamoDB tables for flights + hotels (both: PK=destination [string]).
  3. The weather-provider Lambda + a Function URL for it, saves URL to SSM.
  4. One IAM role per agent, each with:
     - Trust policy allowing bedrock-agentcore.amazonaws.com
     - Managed BedrockAgentCoreRuntimeServicePolicy (for runtime infra)
     - Inline least-privilege policy for its own data + SSM read on its toggles
     - Bedrock InvokeModel for Claude Sonnet 4 inference profile

Idempotent: running twice does nothing destructive. Prints a status summary.

Usage:
    AWS_PROFILE=omni-demo python3 infra/setup.py
"""

import io
import json
import logging
import sys
import time
import zipfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from _shared import (  # noqa: E402
    ACCOUNT,
    FLIGHTS_TABLE,
    HOTELS_TABLE,
    WEATHER_PROVIDER_FN,
    POLICIES,
    POLICY_PREFIX,
    REGION,
    ROLE_BUDGET,
    ROLE_FLIGHTS,
    ROLE_FOR_AGENT,
    ROLE_HOTELS,
    ROLE_ORCHESTRATOR,
    ROLE_WEATHER,
    SESSION,
)

logging.basicConfig(
    level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s"
)
log = logging.getLogger("setup")

ssm = SESSION.client("ssm")
ddb = SESSION.client("dynamodb")
lam = SESSION.client("lambda")
iam = SESSION.client("iam")


# ----------------------------------------------------------- SSM POLICY PARAMS
def ensure_policies() -> None:
    for name, (default, desc) in POLICIES.items():
        full = f"{POLICY_PREFIX}/{name}"
        try:
            cur = ssm.get_parameter(Name=full)["Parameter"]["Value"]
            log.info("policy %s = %r (already present)", name, cur)
        except ssm.exceptions.ParameterNotFound:
            ssm.put_parameter(
                Name=full,
                Value=default,
                Type="String",
                Description=desc[:1024],
                Overwrite=False,
            )
            log.info("policy %s created = %r", name, default)


# ------------------------------------------------------------- DYNAMODB
def _ensure_table(name: str, pk: str) -> None:
    try:
        desc = ddb.describe_table(TableName=name)["Table"]
        log.info("table %s status=%s", name, desc["TableStatus"])
        return
    except ddb.exceptions.ResourceNotFoundException:
        pass
    ddb.create_table(
        TableName=name,
        AttributeDefinitions=[{"AttributeName": pk, "AttributeType": "S"}],
        KeySchema=[{"AttributeName": pk, "KeyType": "HASH"}],
        BillingMode="PAY_PER_REQUEST",
        Tags=[{"Key": "project", "Value": "travel-concierge"}],
    )
    log.info("table %s: creating...", name)
    ddb.get_waiter("table_exists").wait(TableName=name)
    log.info("table %s: ACTIVE", name)


def ensure_tables() -> None:
    _ensure_table(FLIGHTS_TABLE, "destination")
    _ensure_table(HOTELS_TABLE, "destination")


# ---------------------------------------------------------- WEATHER PROVIDER LAMBDA
WEATHER_PROVIDER_SOURCE = '''
import json
import random
import re
import time


# Winter-season (Nov-Feb) forecast templates for US holiday travel.
TEMPLATES_WINTER = {
    "mild_wet":  {"low": 42, "high": 55, "precip_pct": 65, "note": "cool and rainy, pack a waterproof jacket"},
    "warm":      {"low": 60, "high": 76, "precip_pct": 25, "note": "pleasantly warm for the season, light layers"},
    "chilly":    {"low": 35, "high": 50, "precip_pct": 40, "note": "chilly, bring a warm coat"},
    "freezing":  {"low": 15, "high": 32, "precip_pct": 60, "note": "cold with snow likely, pack winter gear"},
    "snowy":     {"low":  5, "high": 28, "precip_pct": 70, "note": "frequent snow, dress for sub-freezing temps"},
    "crisp":     {"low": 38, "high": 55, "precip_pct": 20, "note": "crisp and dry, light coat sufficient"},
}

# Summer / shoulder-season templates kept in for off-season queries.
TEMPLATES_SUMMER = {
    "sunny":   {"low": 65, "high": 85, "precip_pct": 10, "note": "sunny and clear"},
    "mild":    {"low": 55, "high": 72, "precip_pct": 25, "note": "mild with light clouds"},
    "rainy":   {"low": 50, "high": 65, "precip_pct": 70, "note": "steady rain likely"},
    "humid":   {"low": 70, "high": 90, "precip_pct": 55, "note": "humid, thunderstorms possible"},
}

CITY_WINTER = {
    "seattle":        ["mild_wet", "chilly"],
    "portland":       ["mild_wet", "chilly"],
    "austin":         ["warm", "mild_wet"],
    "miami":          ["warm", "warm"],
    "nashville":      ["chilly", "mild_wet"],
    "nyc":            ["chilly", "freezing"],
    "new york":       ["chilly", "freezing"],
    "boston":         ["freezing", "snowy"],
    "chicago":        ["freezing", "snowy"],
    "denver":         ["snowy", "crisp"],
    "san francisco":  ["mild_wet", "crisp"],
    "sf":             ["mild_wet", "crisp"],
}

CITY_SUMMER = {
    "seattle": ["rainy", "mild"],
    "portland": ["rainy", "mild"],
    "austin": ["humid", "sunny"],
    "miami": ["humid", "sunny"],
    "nashville": ["humid", "mild"],
    "nyc": ["mild", "rainy"],
    "boston": ["mild"],
    "chicago": ["mild"],
    "denver": ["sunny"],
    "san francisco": ["mild"],
    "sf": ["mild"],
}


def _is_winter(dates):
    """Heuristic: dates string looks like Nov/Dec/Jan/Feb or has words
    like 'holiday', 'christmas', 'new year', 'thanksgiving'. Default: winter
    (safer in Oct-Mar demo window)."""
    s = (dates or "").lower()
    if re.search(r"\b(nov|dec|jan|feb|holiday|christmas|xmas|new year|thanksgiving|winter)\b", s):
        return True
    if re.search(r"\\b20\\d\\d-(11|12|01|02)\\b", s):
        return True
    if re.search(r"\b(mar|apr|may|jun|jul|aug|sep|summer|spring)\b", s):
        return False
    return True   # default: winter (today is Oct 2026)


def _handler(destination, dates):
    key = (destination or "").strip().lower()
    winter = _is_winter(dates)
    if winter:
        options = CITY_WINTER.get(key, ["chilly", "crisp"])
        pick = random.choice(options)
        tpl = TEMPLATES_WINTER[pick].copy()
    else:
        options = CITY_SUMMER.get(key, ["mild", "sunny"])
        pick = random.choice(options)
        tpl = TEMPLATES_SUMMER[pick].copy()
    tpl["destination"] = destination
    tpl["dates"] = dates
    tpl["profile"] = pick
    tpl["season"] = "winter" if winter else "summer"
    return tpl


def lambda_handler(event, context):
    body = event.get("body")
    if isinstance(body, str):
        try:
            payload = json.loads(body)
        except Exception:
            payload = {}
    elif isinstance(event, dict) and "destination" in event:
        payload = event
    else:
        payload = {}
    destination = payload.get("destination", "unknown")
    dates = payload.get("dates", "unspecified")

    # Small latency to make the span visible (50-150 ms)
    time.sleep(random.uniform(0.05, 0.15))

    result = _handler(destination, dates)
    return {
        "statusCode": 200,
        "headers": {"Content-Type": "application/json"},
        "body": json.dumps(result),
    }
'''


def _zip_source(source: str, filename: str = "index.py") -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zi = zipfile.ZipInfo(filename)
        zi.external_attr = 0o755 << 16
        zf.writestr(zi, source)
    return buf.getvalue()


WEATHER_PROVIDER_ROLE = "travel-concierge-weather-provider-role"


def _ensure_weather_provider_role() -> str:
    try:
        return iam.get_role(RoleName=WEATHER_PROVIDER_ROLE)["Role"]["Arn"]
    except iam.exceptions.NoSuchEntityException:
        pass
    arn = iam.create_role(
        RoleName=WEATHER_PROVIDER_ROLE,
        AssumeRolePolicyDocument=json.dumps(
            {
                "Version": "2012-10-17",
                "Statement": [
                    {
                        "Effect": "Allow",
                        "Principal": {"Service": "lambda.amazonaws.com"},
                        "Action": "sts:AssumeRole",
                    }
                ],
            }
        ),
    )["Role"]["Arn"]
    iam.attach_role_policy(
        RoleName=WEATHER_PROVIDER_ROLE,
        PolicyArn="arn:aws:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole",
    )
    log.info("role %s created", WEATHER_PROVIDER_ROLE)
    # Lambda needs IAM eventual consistency
    time.sleep(10)
    return arn


def ensure_weather_provider() -> str:
    role_arn = _ensure_weather_provider_role()
    code = _zip_source(WEATHER_PROVIDER_SOURCE)
    try:
        lam.get_function(FunctionName=WEATHER_PROVIDER_FN)
        log.info("lambda %s exists, updating code", WEATHER_PROVIDER_FN)
        lam.update_function_code(FunctionName=WEATHER_PROVIDER_FN, ZipFile=code)
    except lam.exceptions.ResourceNotFoundException:
        log.info("creating lambda %s", WEATHER_PROVIDER_FN)
        for attempt in range(6):
            try:
                lam.create_function(
                    FunctionName=WEATHER_PROVIDER_FN,
                    Runtime="python3.12",
                    Role=role_arn,
                    Handler="index.lambda_handler",
                    Code={"ZipFile": code},
                    Timeout=10,
                    MemorySize=128,
                    Tags={"project": "travel-concierge", "env": "prod", "service": "weather-provider"},
                )
                break
            except lam.exceptions.InvalidParameterValueException as e:
                if "role defined for the function cannot be assumed" in str(e):
                    log.info("waiting on IAM role propagation (attempt %d)...", attempt + 1)
                    time.sleep(10)
                    continue
                raise

    # The weather specialist calls this Lambda via `lambda:InvokeFunction`
    # (direct SigV4-authenticated SDK invoke). No public Function URL is
    # provisioned — the weather role is granted `lambda:InvokeFunction` on
    # exactly this function (see IAM policy).
    return None


# ------------------------------------------------------------ IAM ROLES PER AGENT
BEDROCK_AGENTCORE_TRUST = {
    "Version": "2012-10-17",
    "Statement": [
        {
            "Effect": "Allow",
            "Principal": {"Service": "bedrock-agentcore.amazonaws.com"},
            "Action": "sts:AssumeRole",
            "Condition": {
                "StringEquals": {"aws:SourceAccount": ACCOUNT},
                "ArnLike": {
                    "aws:SourceArn": f"arn:aws:bedrock-agentcore:{REGION}:{ACCOUNT}:*"
                },
            },
        }
    ],
}


BEDROCK_INFERENCE_POLICY = {
    "Effect": "Allow",
    "Action": [
        "bedrock:InvokeModel",
        "bedrock:InvokeModelWithResponseStream",
        "bedrock:Converse",
        "bedrock:ConverseStream",
    ],
    "Resource": [
        f"arn:aws:bedrock:us-east-1:{ACCOUNT}:inference-profile/us.anthropic.claude-sonnet-4-5-20250929-v1:0",
        "arn:aws:bedrock:us-east-1::foundation-model/anthropic.claude-sonnet-4-5-20250929-v1:0",
        "arn:aws:bedrock:us-east-2::foundation-model/anthropic.claude-sonnet-4-5-20250929-v1:0",
        "arn:aws:bedrock:us-west-2::foundation-model/anthropic.claude-sonnet-4-5-20250929-v1:0",
    ],
}

# OTel / observability — all agents emit traces to Transaction Search
BASE_OBSERVABILITY = [
    {
        "Effect": "Allow",
        "Action": [
            "logs:CreateLogGroup",
            "logs:CreateLogStream",
            "logs:PutLogEvents",
            "logs:DescribeLogGroups",
            "logs:DescribeLogStreams",
        ],
        "Resource": [
            f"arn:aws:logs:{REGION}:{ACCOUNT}:log-group:/aws/bedrock-agentcore/*"
        ],
    },
    {
        "Effect": "Allow",
        "Action": [
            "xray:PutTraceSegments",
            "xray:PutSpans",
            "xray:PutSpansForIndexing",
            "xray:GetSamplingRules",
            "xray:GetSamplingTargets",
        ],
        "Resource": "*",
    },
    {
        "Effect": "Allow",
        "Action": [
            "cloudwatch:PutMetricData",
        ],
        "Resource": "*",
        "Condition": {
            "StringEquals": {
                "cloudwatch:namespace": [
                    "bedrock-agentcore",
                    "AWS/BedrockAgentCore",
                    "aws-otel",
                    "TravelConcierge",
                ]
            }
        },
    },
    # ECR pull (image lives in the account's bootstrap ECR after `agentcore deploy`)
    {
        "Effect": "Allow",
        "Action": [
            "ecr:BatchGetImage",
            "ecr:GetDownloadUrlForLayer",
            "ecr:GetAuthorizationToken",
        ],
        "Resource": "*",
    },
]

# SSM read for a given toggle sub-prefix (e.g. "flights/*")
def _ssm_policy_read(subprefix: str) -> dict:
    return {
        "Effect": "Allow",
        "Action": ["ssm:GetParameter", "ssm:GetParameters"],
        "Resource": f"arn:aws:ssm:{REGION}:{ACCOUNT}:parameter{POLICY_PREFIX}/{subprefix}",
    }


def _ddb_access(table: str) -> dict:
    return {
        "Effect": "Allow",
        "Action": [
            "dynamodb:GetItem",
            "dynamodb:Query",
            "dynamodb:BatchGetItem",
            "dynamodb:DescribeTable",
        ],
        "Resource": f"arn:aws:dynamodb:{REGION}:{ACCOUNT}:table/{table}",
    }


def _invoke_specialist(agent: str) -> dict:
    return {
        "Effect": "Allow",
        "Action": [
            "bedrock-agentcore:InvokeAgentRuntime",
            # Required when the X-Amzn-Bedrock-AgentCore-Runtime-User-Id header
            # is set — the orchestrator sets it per request to carry user_id
            # into every specialist's trace, which turns on per-user scoping.
            "bedrock-agentcore:InvokeAgentRuntimeForUser",
        ],
        "Resource": f"arn:aws:bedrock-agentcore:{REGION}:{ACCOUNT}:runtime/*",
    }


IAM_POLICIES: dict = {
    ROLE_ORCHESTRATOR: {
        "Version": "2012-10-17",
        "Statement": BASE_OBSERVABILITY
        + [
            BEDROCK_INFERENCE_POLICY,
            _invoke_specialist("*"),
        ],
    },
    ROLE_FLIGHTS: {
        "Version": "2012-10-17",
        "Statement": BASE_OBSERVABILITY
        + [
            BEDROCK_INFERENCE_POLICY,
            _ddb_access(FLIGHTS_TABLE),
        ],
    },
    ROLE_HOTELS: {
        "Version": "2012-10-17",
        "Statement": BASE_OBSERVABILITY
        + [
            BEDROCK_INFERENCE_POLICY,
            _ddb_access(HOTELS_TABLE),
        ],
    },
    ROLE_WEATHER: {
        "Version": "2012-10-17",
        "Statement": BASE_OBSERVABILITY
        + [
            BEDROCK_INFERENCE_POLICY,
            {
                "Effect": "Allow",
                "Action": ["lambda:InvokeFunction"],
                "Resource": f"arn:aws:lambda:{REGION}:{ACCOUNT}:function:{WEATHER_PROVIDER_FN}",
            },
        ],
    },
    ROLE_BUDGET: {
        "Version": "2012-10-17",
        "Statement": BASE_OBSERVABILITY
        + [
            BEDROCK_INFERENCE_POLICY,
            # Keep SSM read for budget/prompt_variant policy (Scenario C in reserve)
            _ssm_policy_read("budget/*"),
        ],
    },
}


def _ensure_role(role_name: str, policy_doc: dict) -> str:
    try:
        existing = iam.get_role(RoleName=role_name)["Role"]
        arn = existing["Arn"]
        # Keep trust policy up to date (we may have changed source arn condition)
        iam.update_assume_role_policy(
            RoleName=role_name,
            PolicyDocument=json.dumps(BEDROCK_AGENTCORE_TRUST),
        )
        log.info("role %s exists (%s)", role_name, arn)
    except iam.exceptions.NoSuchEntityException:
        resp = iam.create_role(
            RoleName=role_name,
            AssumeRolePolicyDocument=json.dumps(BEDROCK_AGENTCORE_TRUST),
            Description=f"Travel Concierge demo role: {role_name}",
            Tags=[{"Key": "project", "Value": "travel-concierge"}],
        )
        arn = resp["Role"]["Arn"]
        log.info("role %s created (%s)", role_name, arn)

    iam.put_role_policy(
        RoleName=role_name,
        PolicyName="InlinePolicy",
        PolicyDocument=json.dumps(policy_doc),
    )
    return arn


def ensure_roles() -> dict:
    arns = {}
    for role_name, doc in IAM_POLICIES.items():
        arns[role_name] = _ensure_role(role_name, doc)
    return arns


# ---------------------------------------------------------- MAIN
def main() -> None:
    log.info("Account: %s  Region: %s", ACCOUNT, REGION)
    log.info("=== SSM policies ===")
    ensure_policies()
    log.info("=== DynamoDB tables ===")
    ensure_tables()
    log.info("=== Weather provider Lambda ===")
    url = ensure_weather_provider()
    log.info("=== IAM roles ===")
    arns = ensure_roles()

    log.info("\n==== SUMMARY ====")
    log.info("Flights table : %s", FLIGHTS_TABLE)
    log.info("Hotels table  : %s", HOTELS_TABLE)
    log.info("Weather Lambda: %s", WEATHER_PROVIDER_FN)
    log.info("Weather URL   : %s", url)
    log.info("Policy prefix : %s", POLICY_PREFIX)
    for name, arn in arns.items():
        log.info("Role %-45s -> %s", name, arn)
    log.info("Next: python3 infra/seed_data.py")


if __name__ == "__main__":
    main()
