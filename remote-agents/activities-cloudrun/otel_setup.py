"""OTel setup for the Cloud Run activities agent — pure OpenTelemetry SDK,
no ADOT distro. Demonstrates that any OTel-instrumented agent, even running
cross-cloud on GCP, can ship spans directly into AWS CloudWatch Omni without
AWS-specific SDKs or a Collector sidecar.

How it works
------------
1. Standard OpenTelemetry SDK generates spans with `gen_ai.*` conventions.
2. OTLPSpanExporter targets the CloudWatch OTLP traces ingest endpoint
   `https://xray.us-east-1.amazonaws.com/v1/traces` (the hostname is
   historical; data lands in CloudWatch Logs via Transaction Search).
3. The endpoint requires AWS SigV4 auth (service=xray), so we hand-roll a
   `requests.Session` subclass that intercepts outbound requests and
   SigV4-signs them using the standard botocore credential chain (which
   reads AWS_ACCESS_KEY_ID / AWS_SECRET_ACCESS_KEY env vars on Cloud Run).

Contrast with the other agents in this demo:
  * AgentCore agents (orchestrator, flights, weather, budget) → ADOT SDK
    (`aws-opentelemetry-distro`), which bundles this same SigV4 wrapper.
  * Hotels (EKS) → also ADOT SDK (uses IRSA for credentials).
  * Activities (Cloud Run) → **pure OTel SDK + 30 lines of SigV4 wrapper**
    (this file), to prove nothing AWS-specific is required.
"""

import logging
import os
import secrets
import time

import requests
from botocore.auth import SigV4Auth
from botocore.awsrequest import AWSRequest
from botocore.session import Session as BotoSession
from opentelemetry import trace
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.instrumentation.botocore import BotocoreInstrumentor
from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
from opentelemetry.instrumentation.requests import RequestsInstrumentor
from opentelemetry.propagate import set_global_textmap
from opentelemetry.propagators.aws import AwsXRayPropagator
from opentelemetry.propagators.composite import CompositePropagator
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor
from opentelemetry.sdk.trace.id_generator import IdGenerator
from opentelemetry.trace.propagation.tracecontext import TraceContextTextMapPropagator

log = logging.getLogger(__name__)

SERVICE_NAME = os.environ.get("OTEL_SERVICE_NAME", "travel_concierge_activities.gcp")
AWS_REGION = os.environ.get("AWS_REGION", "us-east-1")
XRAY_OTLP_ENDPOINT = os.environ.get(
    "OTEL_EXPORTER_OTLP_TRACES_ENDPOINT",
    f"https://xray.{AWS_REGION}.amazonaws.com/v1/traces",
)


# ────────────────────────────────────────────────────────────────────
# SigV4-signing requests.Session — the only AWS-specific piece.
# ────────────────────────────────────────────────────────────────────
class SigV4Session(requests.Session):
    """A ``requests.Session`` that SigV4-signs every outbound request.

    Used as the HTTP session underlying the OTLP exporter so our pure-OTel
    exporter can hit the CloudWatch OTLP traces endpoint (which requires
    AWS SigV4 auth, service=xray).
    """

    def __init__(self, region: str, service: str) -> None:
        super().__init__()
        self._region = region
        self._service = service
        self._boto_session = BotoSession()

    def send(self, request, **kwargs):  # type: ignore[override]
        creds = self._boto_session.get_credentials()
        if creds is None:
            # No AWS credentials — let the vanilla send happen and the
            # endpoint will reject with 403, which surfaces the issue
            # clearly in logs rather than silently dropping spans.
            return super().send(request, **kwargs)
        frozen = creds.get_frozen_credentials()
        aws_req = AWSRequest(
            method=request.method,
            url=request.url,
            data=request.body,
            headers=dict(request.headers),
        )
        SigV4Auth(frozen, self._service, self._region).add_auth(aws_req)
        # Copy signed headers back onto the outbound request
        for k, v in aws_req.headers.items():
            request.headers[k] = v
        return super().send(request, **kwargs)


# ────────────────────────────────────────────────────────────────────
# X-Ray-compatible trace ID generator (first 8 hex = epoch seconds).
# OTel's default is pure-random; X-Ray's native viewers reject that.
# ────────────────────────────────────────────────────────────────────
class XRayCompatibleIdGenerator(IdGenerator):
    def generate_span_id(self) -> int:
        return secrets.randbits(64)

    def generate_trace_id(self) -> int:
        epoch = int(time.time())
        return (epoch << 96) | secrets.randbits(96)


def setup_tracing() -> None:
    """Initialize OTel: tracer provider, SigV4-signing OTLP exporter, propagators,
    auto-instrumentors. All pure OpenTelemetry — no ADOT in requirements.txt."""

    # Resource attrs: these end up on every span and let Omni group this
    # service in topology as a distinct GCP / LangGraph node.
    resource = Resource.create(
        {
            "service.name": SERVICE_NAME,
            "service.namespace": "travel-concierge",
            "service.framework": "langgraph",
            "cloud.provider": "gcp",
            "cloud.platform": "gcp_cloud_run",
            "cloud.region": os.environ.get("GOOGLE_CLOUD_REGION", "us-central1"),
            "deployment.environment": os.environ.get("DEPLOYMENT_ENV", "cloudrun"),
            "aws.service.type": "gen_ai_agent",
        }
    )

    provider = TracerProvider(resource=resource, id_generator=XRayCompatibleIdGenerator())

    # OTLPSpanExporter talks protobuf over HTTP. We swap its session for the
    # SigV4-signing one so the CloudWatch OTLP traces endpoint accepts the
    # request (SigV4 service=xray — historical IAM/signing namespace).
    exporter = OTLPSpanExporter(
        endpoint=XRAY_OTLP_ENDPOINT,
        session=SigV4Session(region=AWS_REGION, service="xray"),
    )
    provider.add_span_processor(BatchSpanProcessor(exporter))
    trace.set_tracer_provider(provider)

    # Accept BOTH W3C traceparent (standard) and AWS X-Ray trace header on
    # inbound HTTP, so stitching works no matter which header the AWS
    # orchestrator sends.
    set_global_textmap(
        CompositePropagator([TraceContextTextMapPropagator(), AwsXRayPropagator()])
    )

    # Auto-instrument standard libraries. Bedrock boto3 calls become
    # aws.bedrock spans under the LangGraph node span.
    FastAPIInstrumentor().instrument()
    BotocoreInstrumentor().instrument()
    RequestsInstrumentor().instrument()

    log.info(
        "OTel initialized (pure SDK): service=%s, endpoint=%s",
        SERVICE_NAME,
        XRAY_OTLP_ENDPOINT,
    )
