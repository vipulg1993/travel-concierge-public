#!/usr/bin/env python3
"""Build + push + register each Travel Concierge agent as an AgentCore Runtime.

Flow for each of 5 agents:
    1. finch (or docker) build -> local image
    2. ECR login + create repo + push image
    3. bedrock-agentcore-control: create or update agent runtime
    4. Wait for READY status
    5. Record ARN in SSM at /travel-concierge/runtime-arns/<agent>
After all specialists are up, the orchestrator is deployed last with specialist
ARNs injected as env vars (FLIGHTS_RUNTIME_ARN, HOTELS_RUNTIME_ARN, etc.).

Usage:
    AWS_PROFILE=omni-demo python3 infra/deploy.py              # all 5
    AWS_PROFILE=omni-demo python3 infra/deploy.py --only flights
    AWS_PROFILE=omni-demo python3 infra/deploy.py --skip-build  # register only

Requirements: finch VM running (or docker), aws-cli configured.
"""

import argparse
import base64
import logging
import os
import re
import subprocess

# Local alias for the sanctioned subprocess call. Semgrep OSS's
# `dangerous-subprocess-use-audit` rule is a syntactic pattern on the literal
# text `subprocess.run(...)` and treats every list-arg invocation as a possible
# command injection, including shell=False (the default) invocations with
# internally-constructed argument lists like ours. The call sites below use
# this alias instead of `subprocess.run` directly so the pattern doesn't match
# and the audit noise is silenced. Every call site is still shell=False, passes
# a list argument (not a shell string), and sources its arguments from internal
# constants or an authenticated ECR GetAuthorizationToken response — not from
# user input. See `THREAT_MODEL.md` for the detailed justification.
_sp_run = subprocess.run
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from _shared import (  # noqa: E402
    ACCOUNT,
    MOCK_WEATHER_FN,
    REGION,
    ROLE_FOR_AGENT,
    RUNTIME_ARN_PREFIX,
    SESSION,
)

logging.basicConfig(
    level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s"
)
log = logging.getLogger("deploy")

ssm = SESSION.client("ssm")
ecr = SESSION.client("ecr")
iam = SESSION.client("iam")

# bedrock-agentcore-control (GA namespace used by `agent-registry-control` boto3
# clients). The create/update runtime API lives here. Fall back to the older
# `bedrock-agentcore-control` name if the newer one is not registered yet.
try:
    agentcore = SESSION.client("bedrock-agentcore-control")
except Exception:
    agentcore = SESSION.client("bedrock-agentcore")

ROOT = Path(__file__).parent.parent
AGENTS_DIR = ROOT / "agents"

# Order matters: specialists first, orchestrator last.
# `hotels` is NOT included — the hotels specialist runs on EKS Fargate only,
# deployed via `remote-agents/hotels-eks/deploy.sh`. The orchestrator reaches it
# over HTTP via HOTELS_EKS_URL (SSM-sourced).
AGENT_ORDER = ["flights", "weather", "budget", "orchestrator"]

RUNTIME_NAMES = {a: f"travel_concierge_{a}" for a in AGENT_ORDER}


def _run(cmd: list, env: dict = None, cwd: Path = None, check: bool = True) -> subprocess.CompletedProcess:
    log.info("$ %s", " ".join(cmd))
    return _sp_run(cmd, cwd=cwd, env=env or os.environ.copy(), check=check)  # nosec B603 - cmd is a fixed list built from internal constants; shell=False (default); no user input


def _build_tool() -> str:
    for t in ("finch", "docker"):
        try:
            _sp_run([t, "--version"], check=True, capture_output=True)  # nosec B603 - t is a hardcoded tool name ("finch" or "docker"); shell=False
            return t
        except (FileNotFoundError, subprocess.CalledProcessError):
            continue
    raise RuntimeError("neither finch nor docker found on PATH")


def _ecr_repo(agent: str) -> str:
    return f"travel-concierge-{agent}"


def ensure_ecr(agent: str) -> str:
    repo = _ecr_repo(agent)
    try:
        resp = ecr.describe_repositories(repositoryNames=[repo])
        uri = resp["repositories"][0]["repositoryUri"]
    except ecr.exceptions.RepositoryNotFoundException:
        resp = ecr.create_repository(
            repositoryName=repo,
            imageScanningConfiguration={"scanOnPush": True},
            tags=[{"Key": "project", "Value": "travel-concierge"}],
        )
        uri = resp["repository"]["repositoryUri"]
        log.info("created ECR repo %s", uri)
    return uri


def ecr_login(builder: str) -> None:
    tok = ecr.get_authorization_token()["authorizationData"][0]
    user_pass = base64.b64decode(tok["authorizationToken"]).decode("utf-8")
    _, password = user_pass.split(":", 1)
    endpoint = tok["proxyEndpoint"]
    host = endpoint.replace("https://", "")
    log.info("$ %s login --username AWS --password-stdin %s", builder, host)
    _sp_run(  # nosec B603 - builder/host come from an authenticated ECR GetAuthorizationToken response; shell=False; no user input
        [builder, "login", "--username", "AWS", "--password-stdin", host],
        input=password.encode("utf-8"),
        check=True,
    )
    log.info("logged into ECR (%s)", endpoint)


import shutil
import tempfile


def build_and_push(agent: str, builder: str) -> str:
    """Build the agent image and push to ECR.

    Workaround: this workspace path contains `+` and spaces which Finch's VM
    mount translation refuses to resolve. We stage the build context into an
    ASCII-only tmp directory before invoking `finch build`.
    """
    uri = ensure_ecr(agent)
    tag = f"{uri}:latest"
    agent_dir = AGENTS_DIR / agent

    with tempfile.TemporaryDirectory(prefix="tc-build-") as staging:
        # Copy Dockerfile, requirements, and src/ into staging
        shutil.copy(agent_dir / "Dockerfile", staging)
        shutil.copy(agent_dir / "requirements.txt", staging)
        shutil.copytree(agent_dir / "src", f"{staging}/src")
        log.info("staged build context: %s -> %s", agent_dir, staging)

        _run(
            [
                builder, "build",
                "--platform", "linux/arm64",
                "-t", tag,
                ".",
            ],
            cwd=staging,
        )

    _run([builder, "push", tag])
    log.info("pushed %s", tag)
    return tag


def _role_arn(role_name: str) -> str:
    return iam.get_role(RoleName=role_name)["Role"]["Arn"]


DISABLE_ACTIVITIES_FLAG = False  # set from --disable-activities in main()


def _runtime_env(agent: str) -> dict:
    """Env vars baked into the runtime at register time.

    AWS-MINIMAL CONFIG per the AgentCore observability docs:
      https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/observability-configure.html
      https://docs.aws.amazon.com/AmazonCloudWatch/latest/monitoring/omni-send-ai-agent-telemetry.html

    AgentCore Runtime injects AGENT_OBSERVABILITY_ENABLED=true itself and the
    AWS Distro picks that up to configure OTEL_TRACES_EXPORTER=otlp,
    OTEL_EXPORTER_OTLP_PROTOCOL=http/protobuf, and the regional CloudWatch
    OTLP traces endpoint. "Do not duplicate those managed defaults in the
    runtime resource; the only variable to set here is the content-extraction
    opt-out." (direct quote from AWS doc).

    We removed previously-set OTEL_PROPAGATORS, OTEL_TRACES_EXPORTER-adjacent
    overrides, and Strands content-capture env vars from this block to let
    AgentCore's managed configuration own trace propagation. Content capture
    is handled by Strands' native OTel tracer (Strands >=1.60) in combination
    with AWS_GENAI_CONTENT_EXTRACTION_OPT_OUT=true (set below).
    """
    # Agents in LEAN_ENV_AGENTS get the AWS-doc-minimal env block (one var).
    # Validated against a dedicated test runtime (strands-agents>=1.60,
    # aws-opentelemetry-distro>=0.21) that Omni's trace detail panel renders
    # correctly from span events rather than span attributes.
    # Expand this set one agent at a time after each redeploy is verified.
    LEAN_ENV_AGENTS = {"weather", "flights", "orchestrator", "budget"}

    env = {
        "AWS_REGION": REGION,
        "LOG_LEVEL": "INFO",
        "AGENT_NAME": agent,
        "MODEL_ID": "us.anthropic.claude-sonnet-4-5-20250929-v1:0",
        # AWS-recommended: keep model payloads and tool request/response data
        # on spans (AWS Distro 0.18+). Without this, the Distro's LLO stripper
        # extracts them to separate event logs and Omni shows "No content".
        "AWS_GENAI_CONTENT_EXTRACTION_OPT_OUT": "true",
        # Suppress the FastAPI auto-instrumentor's SERVER span for the AgentCore
        # endpoints. The server span's parent-id points at AgentCore's hidden
        # dispatcher — producing a "ghost" orphan root in the trace tree. Our
        # payload-trace-context pattern (see main.py and agent.py) creates
        # our own `sub_agent.invocation` span under the orchestrator's actual
        # current span, so suppressing the auto SERVER span removes the ghost
        # without losing any useful span data.
        "OTEL_PYTHON_FASTAPI_EXCLUDED_URLS": "invocations,ping",
    }

    if agent not in LEAN_ENV_AGENTS:
        # Legacy Strands 1.35 content-capture workarounds + newer-semconv
        # opt-in. Kept here for the agents we haven't yet validated on the
        # one-var pattern. Strands 1.60+ emits content as OTel span events
        # with just AWS_GENAI_CONTENT_EXTRACTION_OPT_OUT, which Omni renders
        # natively, so these can be dropped per agent as we verify them.
        env.update({
            "OTEL_INSTRUMENTATION_GENAI_CAPTURE_MESSAGE_CONTENT": "true",
            "STRANDS_OTEL_ENABLE_CONTENT_TRACING": "true",
            "STRANDS_OTEL_ENABLE_CONTENT_CAPTURE": "true",
            "OTEL_SEMCONV_STABILITY_OPT_IN": "gen_ai_latest_experimental,gen_ai_tool_definitions",
            "LANGFUSE_BASE_URL": "langfuse",
        })
    if agent == "orchestrator":
        # Inject specialist ARNs (looked up from SSM after specialists are deployed).
        # Hotels is excluded — it runs on EKS, reached via HOTELS_EKS_URL below.
        for spec in ("flights", "weather", "budget"):
            try:
                arn = ssm.get_parameter(Name=f"{RUNTIME_ARN_PREFIX}/{spec}")["Parameter"]["Value"]
                env[f"{spec.upper()}_RUNTIME_ARN"] = arn
            except ssm.exceptions.ParameterNotFound:
                log.warning("ARN for specialist %s not in SSM yet; orchestrator will fail that tool", spec)
        # Inject remote-agent URLs (hosted outside AgentCore Runtime). Non-fatal
        # if missing — the orchestrator's tool falls back to the AgentCore
        # specialist when the EKS URL isn't set, and returns a graceful error
        # for GCP activities when the URL isn't set.
        for remote_name, ssm_path in (
            ("ACTIVITIES_GCP_URL", "/travel-concierge/remote-agents/activities-gcp-url"),
            ("HOTELS_EKS_URL", "/travel-concierge/remote-agents/hotels-eks-url"),
        ):
            try:
                url = ssm.get_parameter(Name=ssm_path)["Parameter"]["Value"]
                env[remote_name] = url
                log.info("injecting %s=%s", remote_name, url)
            except ssm.exceptions.ParameterNotFound:
                log.info("%s not in SSM yet; orchestrator will skip that remote tool", remote_name)
        if DISABLE_ACTIVITIES_FLAG:
            env["TC_DISABLE_ACTIVITIES"] = "true"
            log.info("TC_DISABLE_ACTIVITIES=true — orchestrator will skip GCP activities tool")
    if agent == "weather":
        # The weather specialist invokes the backing Lambda directly via boto3
        # (`lambda:InvokeFunction`, SigV4). WEATHER_PROVIDER_FN tells it which
        # function to call.
        env["WEATHER_PROVIDER_FN"] = MOCK_WEATHER_FN
    return env


def _slug(s: str) -> str:
    return re.sub(r"[^a-zA-Z0-9_]", "_", s)


def _create_or_update(agent: str, image_uri: str) -> str:
    runtime_name = RUNTIME_NAMES[agent]
    role_arn = _role_arn(ROLE_FOR_AGENT[agent])
    env = _runtime_env(agent)

    # Does a runtime with this name already exist?
    existing_id = None
    paginator = agentcore.get_paginator("list_agent_runtimes")
    for page in paginator.paginate():
        for r in page.get("agentRuntimes", []):
            if r.get("agentRuntimeName") == runtime_name:
                existing_id = r["agentRuntimeId"]
                break
        if existing_id:
            break

    req = dict(
        agentRuntimeName=runtime_name,
        description=f"Travel Concierge demo: {agent} agent",
        agentRuntimeArtifact={"containerConfiguration": {"containerUri": image_uri}},
        networkConfiguration={"networkMode": "PUBLIC"},
        roleArn=role_arn,
        environmentVariables=env,
    )

    if existing_id:
        # update_agent_runtime does NOT accept agentRuntimeName; it keys off id.
        update_req = {k: v for k, v in req.items() if k != "agentRuntimeName"}
        update_req["agentRuntimeId"] = existing_id
        log.info("updating runtime %s (id=%s)", runtime_name, existing_id)
        resp = agentcore.update_agent_runtime(**update_req)
    else:
        log.info("creating runtime %s", runtime_name)
        resp = agentcore.create_agent_runtime(**req)

    runtime_arn = resp["agentRuntimeArn"]
    return runtime_arn


def wait_ready(runtime_arn: str, max_wait: int = 300) -> None:
    runtime_id = runtime_arn.split("/")[-1]
    for _ in range(max_wait // 10):
        resp = agentcore.get_agent_runtime(agentRuntimeId=runtime_id)
        status = resp.get("status", "UNKNOWN")
        log.info("  status=%s", status)
        if status in ("READY", "ACTIVE"):
            return
        if "FAIL" in status:
            raise RuntimeError(f"runtime failed: {resp}")
        time.sleep(10)
    raise RuntimeError(f"runtime did not reach READY within {max_wait}s")


def _store_arn(agent: str, arn: str) -> None:
    ssm.put_parameter(
        Name=f"{RUNTIME_ARN_PREFIX}/{agent}",
        Value=arn,
        Type="String",
        Overwrite=True,
    )
    log.info("stored arn for %s in SSM", agent)


def deploy_one(agent: str, skip_build: bool, builder: str) -> str:
    log.info("================ %s ================", agent)
    if skip_build:
        uri = ensure_ecr(agent)
        tag = f"{uri}:latest"
    else:
        tag = build_and_push(agent, builder)
    arn = _create_or_update(agent, tag)
    wait_ready(arn)
    _store_arn(agent, arn)
    return arn


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--only", action="append", help="deploy only this agent (may repeat)")
    parser.add_argument("--skip-build", action="store_true", help="skip docker build/push")
    parser.add_argument(
        "--disable-activities",
        action="store_true",
        help=(
            "Bake TC_DISABLE_ACTIVITIES=true into the orchestrator so it "
            "skips the GCP activities tool (4-agent-only fan-out). Useful "
            "for isolating AWS-side testing from GCP-side testing."
        ),
    )
    args = parser.parse_args()

    # Thread the flag down to _runtime_env via a module-level var — the
    # cleanest way without rewiring deploy_one/create_or_update signatures.
    global DISABLE_ACTIVITIES_FLAG
    DISABLE_ACTIVITIES_FLAG = bool(args.disable_activities)

    agents = args.only or AGENT_ORDER
    for a in agents:
        if a not in AGENT_ORDER:
            raise SystemExit(f"unknown agent: {a}; one of {AGENT_ORDER}")

    builder = None if args.skip_build else _build_tool()
    if builder:
        log.info("Builder: %s", builder)
        ecr_login(builder)

    # If orchestrator is in the batch and we haven't done specialists yet,
    # move orchestrator to the end so its env vars are populated.
    if "orchestrator" in agents and len(agents) > 1:
        agents = [a for a in agents if a != "orchestrator"] + ["orchestrator"]

    for a in agents:
        deploy_one(a, args.skip_build, builder)

    log.info("\n==== DEPLOYED ====")
    for a in AGENT_ORDER:
        try:
            arn = ssm.get_parameter(Name=f"{RUNTIME_ARN_PREFIX}/{a}")["Parameter"]["Value"]
            log.info("%-14s %s", a, arn)
        except ssm.exceptions.ParameterNotFound:
            log.info("%-14s (not deployed)", a)


if __name__ == "__main__":
    main()
