"""OpenTelemetry setup for the GCP-hosted Activities specialist.

Design:
  * Export spans via OTLP/HTTP to the local ADOT Collector (localhost:4318).
    The Collector handles AWS SigV4 and ships to Transaction Search in us-east-1.
  * Install AWS X-Ray trace ID propagator so trace IDs flowing in from the
    AWS orchestrator keep their format and stitch cleanly into the same trace
    in Omni.
  * Mark this service as `cloud.provider=gcp`, `service.framework=langgraph`
    so Omni's topology view groups it correctly.
  * Enable auto-instrumentation for FastAPI, botocore, requests, and logging.

The actual Bedrock call downstream uses botocore, which the botocore
instrumentor turns into an aws.bedrock span under the current context — so
the orchestrator → GCP agent → Bedrock chain shows up as one trace tree.
"""

import logging
import os

from opentelemetry import trace
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.instrumentation.botocore import BotocoreInstrumentor
from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
from opentelemetry.instrumentation.logging import LoggingInstrumentor
from opentelemetry.instrumentation.requests import RequestsInstrumentor
from opentelemetry.propagate import set_global_textmap
from opentelemetry.propagators.composite import CompositePropagator
from opentelemetry.propagators.aws import AwsXRayPropagator
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor
from opentelemetry.sdk.trace.id_generator import IdGenerator
from opentelemetry.trace.propagation.tracecontext import TraceContextTextMapPropagator

log = logging.getLogger(__name__)

SERVICE_NAME = os.environ.get("OTEL_SERVICE_NAME", "travel_concierge_activities.gcp")
COLLECTOR_URL = os.environ.get(
    "OTEL_EXPORTER_OTLP_ENDPOINT", "http://localhost:4318"
).rstrip("/")


class _XRayCompatibleIdGenerator(IdGenerator):
    """Generate trace IDs in X-Ray's time-prefixed format.

    X-Ray requires the first 8 hex chars of the trace ID to encode the epoch
    seconds of the trace start. OTel's default is pure-random, which Transaction
    Search accepts but X-Ray's native viewers reject. Using an X-Ray-compatible
    generator keeps the two paths interchangeable.
    """

    def __init__(self):
        import secrets
        import time
        self._random = secrets.randbits
        self._time = time.time

    def generate_span_id(self) -> int:
        return self._random(64)

    def generate_trace_id(self) -> int:
        # 32 bits of epoch time + 96 bits of random — the X-Ray scheme
        epoch_hex = int(self._time())
        rand_hex = self._random(96)
        return (epoch_hex << 96) | rand_hex


def setup_tracing() -> None:
    """Initialize the tracer provider, exporter, propagators, and instrumentors."""
    resource = Resource.create(
        {
            "service.name": SERVICE_NAME,
            "service.namespace": "travel-concierge",
            "service.framework": "langgraph",
            "cloud.provider": "gcp",
            "cloud.region": os.environ.get("GOOGLE_CLOUD_REGION", "unknown"),
            "deployment.environment": os.environ.get("DEPLOYMENT_ENV", "cloudshell"),
            "aws.service.type": "gen_ai_agent",
        }
    )

    provider = TracerProvider(
        resource=resource, id_generator=_XRayCompatibleIdGenerator()
    )
    span_exporter = OTLPSpanExporter(
        endpoint=f"{COLLECTOR_URL}/v1/traces"
    )
    provider.add_span_processor(BatchSpanProcessor(span_exporter))
    trace.set_tracer_provider(provider)

    # Accept BOTH W3C traceparent (standard) and AWS X-Ray trace header
    # from the inbound HTTP request so stitching works no matter which
    # format the orchestrator chose.
    set_global_textmap(
        CompositePropagator(
            [TraceContextTextMapPropagator(), AwsXRayPropagator()]
        )
    )

    # Auto-instrument the standard libraries so FastAPI requests, boto3
    # Bedrock calls, outbound HTTP, and logs all get spans and trace stamps.
    FastAPIInstrumentor().instrument()
    BotocoreInstrumentor().instrument()
    RequestsInstrumentor().instrument()
    LoggingInstrumentor().instrument(set_logging_format=True)

    log.info(
        "OTel initialized: service=%s, exporter=%s, propagators=[tracecontext, aws-xray]",
        SERVICE_NAME, COLLECTOR_URL,
    )
