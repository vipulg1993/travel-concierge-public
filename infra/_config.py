"""Shared account-id loader. Imported by every infra script that needs the
account id in a literal string (e.g. ECR URIs, ARN construction).

Prefer the AWS_ACCOUNT_ID env var; fall back to STS caller identity so a
one-shot local run with AWS_PROFILE still works.
"""
import os
from functools import lru_cache


@lru_cache(maxsize=1)
def account_id() -> str:
    env = os.environ.get("AWS_ACCOUNT_ID")
    if env:
        return env.strip()
    try:
        import boto3

        return boto3.client("sts").get_caller_identity()["Account"]
    except Exception as e:  # pragma: no cover — surfaced at import time
        raise RuntimeError(
            "AWS_ACCOUNT_ID is not set and STS caller-identity lookup "
            "failed. Export AWS_ACCOUNT_ID or run with a valid "
            "AWS_PROFILE."
        ) from e


AWS_ACCOUNT_ID = account_id()
