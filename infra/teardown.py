#!/usr/bin/env python3
"""Tear down everything travel-concierge created.

Does NOT touch AgentCore Runtimes — those are managed by `agentcore destroy`.
Run this after deleting runtimes.

Removes:
  - IAM roles + inline policies  (travel-concierge-* and mock-weather role)
  - mock-weather Lambda + function URL
  - DynamoDB tables flights + hotels
  - SSM toggle parameters at /travel-concierge/*

Usage:
    AWS_PROFILE=omni-demo python3 infra/teardown.py --yes
"""

import argparse
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from _shared import (  # noqa: E402
    FLIGHTS_TABLE,
    HOTELS_TABLE,
    MOCK_WEATHER_FN,
    REGION,
    ROLE_FOR_AGENT,
    SESSION,
    POLICY_PREFIX,
)

logging.basicConfig(
    level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s"
)
log = logging.getLogger("teardown")

iam = SESSION.client("iam")
lam = SESSION.client("lambda")
ddb = SESSION.client("dynamodb")
ssm = SESSION.client("ssm")

MOCK_WEATHER_ROLE = "travel-concierge-mock-weather-role"


def delete_role(role_name: str) -> None:
    try:
        policies = iam.list_role_policies(RoleName=role_name).get("PolicyNames", [])
        for p in policies:
            iam.delete_role_policy(RoleName=role_name, PolicyName=p)
        attached = iam.list_attached_role_policies(RoleName=role_name).get(
            "AttachedPolicies", []
        )
        for p in attached:
            iam.detach_role_policy(RoleName=role_name, PolicyArn=p["PolicyArn"])
        iam.delete_role(RoleName=role_name)
        log.info("deleted role %s", role_name)
    except iam.exceptions.NoSuchEntityException:
        log.info("role %s not found", role_name)


def delete_lambda() -> None:
    try:
        try:
            lam.delete_function_url_config(FunctionName=MOCK_WEATHER_FN)
        except lam.exceptions.ResourceNotFoundException:
            pass
        lam.delete_function(FunctionName=MOCK_WEATHER_FN)
        log.info("deleted lambda %s", MOCK_WEATHER_FN)
    except lam.exceptions.ResourceNotFoundException:
        log.info("lambda %s not found", MOCK_WEATHER_FN)


def delete_table(name: str) -> None:
    try:
        ddb.delete_table(TableName=name)
        log.info("deleting table %s...", name)
        ddb.get_waiter("table_not_exists").wait(TableName=name)
        log.info("table %s deleted", name)
    except ddb.exceptions.ResourceNotFoundException:
        log.info("table %s not found", name)


def delete_ssm() -> None:
    paginator = ssm.get_paginator("describe_parameters")
    names = []
    for page in paginator.paginate(
        ParameterFilters=[{"Key": "Name", "Option": "BeginsWith", "Values": [POLICY_PREFIX]}]
    ):
        for p in page["Parameters"]:
            names.append(p["Name"])
    # also the /travel-concierge/mock-weather-url and runtime-arns
    for extra_prefix in ("/travel-concierge/mock-weather-url", "/travel-concierge/runtime-arns"):
        for page in paginator.paginate(
            ParameterFilters=[
                {"Key": "Name", "Option": "BeginsWith", "Values": [extra_prefix]}
            ]
        ):
            for p in page["Parameters"]:
                names.append(p["Name"])
    # Batch delete (max 10 per call)
    for i in range(0, len(names), 10):
        batch = names[i : i + 10]
        if batch:
            ssm.delete_parameters(Names=batch)
    log.info("deleted %d SSM parameters", len(names))


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--yes", action="store_true", help="skip confirmation")
    args = parser.parse_args()
    if not args.yes:
        resp = input(
            "This will delete the DynamoDB tables, mock-weather Lambda, "
            "IAM roles, and SSM parameters for the Travel Concierge demo. "
            "Continue? [y/N] "
        )
        if resp.strip().lower() != "y":
            print("aborted")
            return

    delete_lambda()
    delete_table(FLIGHTS_TABLE)
    delete_table(HOTELS_TABLE)
    for role in list(ROLE_FOR_AGENT.values()) + [MOCK_WEATHER_ROLE]:
        delete_role(role)
    delete_ssm()
    log.info("Teardown complete. (AgentCore runtimes NOT touched.)")


if __name__ == "__main__":
    main()
