"""Runtime policy enforcement for travel-concierge specialists.

Budget specialist uses the policy framework only to select prompt variants.
See agents/flights/src/policies.py for the full framework docstring.
"""

import logging
import os
import time
from typing import Optional

import boto3

log = logging.getLogger(__name__)

REGION = os.environ.get("AWS_REGION", "us-east-1")
POLICY_PREFIX = os.environ.get("POLICY_PREFIX", "/travel-concierge/policies")
TTL_SECONDS = int(os.environ.get("POLICY_TTL_SECONDS", "10"))
AGENT_NAME = os.environ.get("AGENT_NAME", "budget")

_ssm = boto3.client("ssm", region_name=REGION)
_cache: dict = {}


def get_policy(name: str, default: Optional[str] = None) -> Optional[str]:
    now = time.time()
    cached = _cache.get(name)
    if cached and cached[1] > now:
        return cached[0]
    full = f"{POLICY_PREFIX}/{name}"
    try:
        resp = _ssm.get_parameter(Name=full)
        value = resp["Parameter"]["Value"]
    except _ssm.exceptions.ParameterNotFound:
        value = default
    except Exception as e:
        log.warning("policy fetch failed for %s: %s", full, e)
        value = default
    _cache[name] = (value, now + TTL_SECONDS)
    return value
