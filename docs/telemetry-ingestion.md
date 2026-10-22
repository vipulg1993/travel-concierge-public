# Telemetry Ingestion — Reference



```
┌─────────────────┐    ┌─────────────────┐    ┌─────────────────┐
│  5 agents       │ →  │ CloudWatch OTLP │ →  │  CloudWatch     │
│  3 compute      │    │ traces endpoint │    │  Omni           │
│  2 OTel SDKs    │    │ (xray.*/v1/…)   │    │  Transaction    │
└─────────────────┘    └─────────────────┘    │  Search         │
                                              │  DevOps Agent   │
                                              └─────────────────┘
```

Every agent — regardless of SDK, cloud, or compute — ships OpenTelemetry
Protocol (OTLP) spans over HTTP/protobuf to `xray.<region>.amazonaws.com/v1/traces`,
SigV4-signed. CloudWatch Logs persists them in `aws/spans` (or a per-runtime
log group), Transaction Search indexes them, and Omni / Agent Observability
renders them in trace trees, service maps, and dashboards.

---

## 1. Prerequisite — one-time per account

| Setting | Command | Why |
|---|---|---|
| CloudWatch Transaction Search | `aws xray update-trace-segment-destination --destination CloudWatchLogs --region us-east-1` | Required for the trace list, trace detail panel, service map, and the DevOps Agent investigation flow. Without it the OTLP endpoint still accepts traces but Omni cannot query them. |
| Verify | `aws xray get-trace-segment-destination` → `{"Destination":"CloudWatchLogs","Status":"ACTIVE"}` | Confirms the data path is open. |

---

## 2. Agent-by-agent configuration

### 2.1 AgentCore Runtime (orchestrator, flights, weather, budget)

The simplest path. **AgentCore wires 95% of OTel for you**; one env var
turns content capture on. Works identically for Strands (orchestrator,
flights, weather) and CrewAI (budget) through ADOT's bundled framework
instrumentors.

**SDK:** [`aws-opentelemetry-distro>=0.21.0`](https://github.com/aws-observability/aws-otel-python-instrumentation) (ADOT) — a vendored OpenTelemetry Python distribution with AWS extensions

**Launch:** `CMD ["opentelemetry-instrument", "uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8080"]`
— the `opentelemetry-instrument` wrapper auto-detects FastAPI, requests,
botocore, logging and attaches ADOT's TracerProvider as the global one.

**Env vars that YOU set** (`infra/deploy.py :: _runtime_env`):

```bash
AWS_GENAI_CONTENT_EXTRACTION_OPT_OUT=true
```

`AWS_GENAI_CONTENT_EXTRACTION_OPT_OUT=true` keeps prompt and response text
on the spans — the only telemetry env var AWS asks you to set on
AgentCore + ADOT with Strands.

**Env vars AgentCore injects for you:**

```bash
AGENT_OBSERVABILITY_ENABLED=true
OTEL_PYTHON_DISTRO=aws_distro
OTEL_PYTHON_CONFIGURATOR=aws_configurator
OTEL_TRACES_EXPORTER=otlp
OTEL_EXPORTER_OTLP_PROTOCOL=http/protobuf
OTEL_EXPORTER_OTLP_ENDPOINT=https://xray.us-east-1.amazonaws.com
OTEL_RESOURCE_ATTRIBUTES=service.name=travel_concierge_<agent>.DEFAULT,
                          aws.log.group.names=/aws/bedrock-agentcore/runtimes/<runtime-id>,
                          cloud.resource_id=<AgentEndpointArn:AgentEndpointName>
OTEL_PROPAGATORS=<AgentCore defaults — includes tracecontext + xray>
```


**Auth:** AgentCore's internal pipeline SigV4-signs OTLP uploads using the
runtime's own execution role; no credential wiring in agent code.

**Trace storage:** By default, the shared `aws/spans` log group. Opt in to
per-runtime log groups with `UNIFIED_TRACES_DESTINATION_ENABLED=true`.

**IAM policy on the runtime role** needs:
- `xray:PutTraceSegments`, `xray:PutSpans`, `xray:PutSpansForIndexing`
- `logs:CreateLogGroup`, `CreateLogStream`, `PutLogEvents`
- `cloudwatch:PutMetricData` (scoped by namespace)

---

### 2.2 EKS Fargate (hotels — primary HTTP path)

Same SDK as AgentCore, but **you wire every env var and IAM yourself**.
Equivalent to running the AgentCore config "by hand".

**SDK:** `aws-opentelemetry-distro>=0.21.0` (ADOT)

**Launch:** `CMD ["opentelemetry-instrument", "uvicorn", "main:app", ...]`
(identical to the AgentCore agents — same source image, in fact)

**Full env block** (`remote-agents/hotels-eks/manifests.yaml`):

```yaml
env:
  - { name: AGENT_OBSERVABILITY_ENABLED,                  value: "true" }
  - { name: AWS_GENAI_CONTENT_EXTRACTION_OPT_OUT,          value: "true" }
  - { name: OTEL_PYTHON_DISTRO,                            value: "aws_distro" }
  - { name: OTEL_PYTHON_CONFIGURATOR,                      value: "aws_configurator" }
  - { name: OTEL_EXPORTER_OTLP_PROTOCOL,                   value: "http/protobuf" }
  - { name: OTEL_TRACES_EXPORTER,                          value: "otlp" }
  - { name: OTEL_EXPORTER_OTLP_TRACES_HEADERS,             value: "x-aws-log-group=/aws/bedrock-agentcore/runtimes/travel_concierge_hotels-eks,x-aws-log-stream=spans" }
  - { name: OTEL_EXPORTER_OTLP_LOGS_HEADERS,               value: "x-aws-log-group=/aws/bedrock-agentcore/runtimes/travel_concierge_hotels-eks,x-aws-log-stream=runtime-logs,x-aws-metric-namespace=bedrock-agentcore" }
  - { name: OTEL_SERVICE_NAME,                             value: "travel_concierge_hotels.eks" }
  - { name: OTEL_RESOURCE_ATTRIBUTES,                      value: "service.name=travel_concierge_hotels.eks,aws.log.group.names=/aws/bedrock-agentcore/runtimes/travel_concierge_hotels-eks,cloud.platform=aws_eks,service.framework=strands,deployment.environment=eks-fargate" }
```

**Auth:** IRSA (IAM Role for Service Account) — EKS Pod Identity Webhook
injects `AWS_ROLE_ARN` + `AWS_WEB_IDENTITY_TOKEN_FILE` into the pod. The
ADOT SDK's botocore layer picks these up to SigV4-sign the OTLP POST.

**One-time setup** (per account/region):
1. Create log group `/aws/bedrock-agentcore/runtimes/travel_concierge_hotels-eks`
   with 7-day retention.
2. Attach a CloudWatch Logs resource policy allowing `xray.amazonaws.com`
   to call `logs:PutLogEvents` on that log group — this lets Transaction
   Search deliver spans to it.
3. Grant the IRSA role `xray:PutTraceSegments`, `PutSpans`, `PutSpansForIndexing`.

---

### 2.3 GCP Cloud Run (activities — cross-cloud)

Pure OpenTelemetry SDK (no ADOT), with a custom SigV4 wrapper so the OTel
exporter can call the AWS OTLP endpoint. This path demonstrates that any
OTel-instrumented agent can push to the CloudWatch OTLP endpoint — the
AWS-specific distro is a convenience, not a requirement.

**SDK:**
```
opentelemetry-api>=1.30.0
opentelemetry-sdk>=1.30.0
opentelemetry-exporter-otlp-proto-http>=1.30.0
opentelemetry-instrumentation-{fastapi,botocore,requests,logging}
opentelemetry-propagator-aws-xray>=1.0.1   # for cross-propagator support
```

**Launch:** `CMD ["python", "app.py"]` — NO `opentelemetry-instrument` wrapper;
`otel_setup.py` is explicitly imported and configured before FastAPI starts.

**Env vars** (`remote-agents/activities-cloudrun/deploy.sh --set-env-vars`):

App config (consumed by `app.py`, not by OTel):

```bash
# AWS credentials — static IAM user keys stored as Cloud Run secrets. The
# user needs xray:PutTraceSegments / PutSpans / PutSpansForIndexing to ship
# traces to the CloudWatch OTLP ingest, plus bedrock:InvokeModel for the
# cross-cloud model call.
AWS_ACCESS_KEY_ID=<static key>
AWS_SECRET_ACCESS_KEY=<static secret>

# Where the agent calls Bedrock from GCP.
AWS_BEDROCK_REGION=us-east-1

# GCP region, read into the span resource as `cloud.region`.
GOOGLE_CLOUD_REGION=us-central1
```

OTel configuration:

```bash
# Region used by the SigV4 signer and the default OTLP endpoint.
AWS_REGION=us-east-1

OTEL_SERVICE_NAME=travel_concierge_activities.gcp
OTEL_EXPORTER_OTLP_TRACES_ENDPOINT=https://xray.us-east-1.amazonaws.com/v1/traces

# OTel GenAI auto-instrumentor toggles. Inert for this service because we
# hand-roll gen_ai.* attributes on manual spans in app.py (see below), but
# kept set so any future OTel GenAI instrumentor added to this container
# would honor the content-capture + newer-semconv behavior out of the box.
OTEL_INSTRUMENTATION_GENAI_CAPTURE_MESSAGE_CONTENT=true
OTEL_SEMCONV_STABILITY_OPT_IN=gen_ai_latest_experimental

# Suppress FastAPI auto-SERVER span for /invocations and /ping (same
# rationale as the AgentCore agents — our sub_agent.invocation span, created
# in app.py under the orchestrator's trace context, replaces it).
OTEL_PYTHON_FASTAPI_EXCLUDED_URLS=invocations,ping
```

**In-app OTel setup** (`otel_setup.py`):

```python
# 1. Resource attributes (one-time, baked into every span)
resource = Resource.create({
    "service.name":   "travel_concierge_activities.gcp",
    "cloud.provider": "gcp",
    "cloud.platform": "gcp_cloud_run",
    "cloud.region":   os.environ.get("GOOGLE_CLOUD_REGION", "us-central1"),
})

# 2. Tracer provider with X-Ray-compatible trace-id (first 8 hex = epoch seconds)
tracer_provider = TracerProvider(
    resource=resource,
    id_generator=XRayCompatibleIdGenerator(),
)

# 3. OTLP exporter with a SigV4-signing requests.Session underneath
exporter = OTLPSpanExporter(
    endpoint="https://xray.us-east-1.amazonaws.com/v1/traces",
    session=SigV4Session(region="us-east-1", service="xray"),
)
tracer_provider.add_span_processor(BatchSpanProcessor(exporter))
trace.set_tracer_provider(tracer_provider)

# 4. Composite propagator — accept W3C and X-Ray formats
set_global_textmap(CompositePropagator([
    TraceContextTextMapPropagator(),   # W3C traceparent
    AwsXRayPropagator(),               # X-Amzn-Trace-Id
]))

# 5. Explicit instrumentors (no auto-instrument wrapper on Cloud Run)
BotocoreInstrumentor().instrument()
FastAPIInstrumentor.instrument_app(app)
RequestsInstrumentor().instrument()
```

The GenAI span attributes (`gen_ai.operation.name`, `gen_ai.request.model`,
`gen_ai.input.messages`, `gen_ai.output.messages`, `gen_ai.usage.*`,
`gen_ai.agent.name`) are set directly in `app.py` on hand-crafted spans
around the LangGraph nodes — no OpenInference or auto-instrumentor needed.
This keeps the demo's "pure OTel, no AWS-specific distro" thesis intact.

**SigV4 wrapper (`SigV4Session`):** 30 lines using `botocore.auth.SigV4Auth`
and `botocore.awsrequest.AWSRequest` to sign every outbound OTLP POST with
`service=xray`. ADOT bundles the equivalent of this; implementing it
directly on top of the standard OTel exporter shows the signing contract
explicitly and keeps the Cloud Run container free of AWS-specific
distributions.

---

## 3. Trace stitching and content rendering

- **Traceparent propagation.** Each outbound call carries a W3C `traceparent` so every sub-agent span nests under the caller in one unified trace tree.
- **Content rendering.** Omni's trace detail panel reads the OpenTelemetry GenAI semantic conventions (`gen_ai.*`) to render prompts, responses, system instructions, token usage, and model identity inline.
