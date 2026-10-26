# Threat Model — sample-agent-observability-in-omni

STRIDE-based threat model for the Travel Concierge multi-agent sample
application, prepared for AWS PCSR (Public Content Security Review).

> **Scope:** This threat model covers the sample application as published
> under MIT-0 to `github.com/aws-samples/sample-agent-observability-in-omni`.
> The sample is explicitly non-production — it exists to demonstrate
> CloudWatch Omni + AWS DevOps Agent observability across a multi-cloud,
> multi-framework agentic workload. Customers deploying this sample are
> expected to add production-grade security controls (authentication,
> input validation, secret rotation, etc.) before any customer-facing use.

## 1. System overview

An orchestrator agent on AWS Bedrock AgentCore Runtime fans out to five
specialists:

| Specialist | Hosting | Framework | Reached via |
|---|---|---|---|
| Flights | AgentCore Runtime | Strands | `bedrock-agentcore:InvokeAgentRuntime` (SigV4) |
| Hotels | AWS EKS Fargate pod | Strands | HTTPS to a Cloudflare quick-tunnel URL (unauthenticated) |
| Weather | AgentCore Runtime | Strands | `bedrock-agentcore:InvokeAgentRuntime` (SigV4) |
| Budget | AgentCore Runtime | CrewAI | `bedrock-agentcore:InvokeAgentRuntime` (SigV4) |
| Activities | GCP Cloud Run | LangGraph | HTTPS to a Cloud Run URL (unauthenticated) |

Backend data sources: 2 DynamoDB tables (flights, hotels, with fake
fixture data for 10 cities), 1 Lambda (season-aware weather forecast
generator). No customer data, no PII, no PHI anywhere in the system.

Observability: every agent ships OTel spans to the CloudWatch OTLP traces
endpoint (`xray.us-east-1.amazonaws.com/v1/traces`), SigV4-signed with
either the runtime's own role (AgentCore, Lambda), IRSA (EKS), or a static
IAM user (Cloud Run).

## 2. Trust boundaries

| # | Boundary | Mechanism | Auth |
|---|---|---|---|
| T1 | User laptop → Streamlit frontend | Local HTTP (`localhost:8501`) | None (local) |
| T2 | Streamlit → Orchestrator | `bedrock-agentcore:InvokeAgentRuntime` | AWS SigV4 (user profile) |
| T3 | Orchestrator → AgentCore specialists (flights, weather, budget) | `bedrock-agentcore:InvokeAgentRuntime` | AWS SigV4 (orchestrator runtime role) |
| T4 | Orchestrator → EKS hotels pod | HTTPS over Cloudflare quick tunnel | **None** (public URL) |
| T5 | Orchestrator → GCP Cloud Run activities | HTTPS to `*.run.app` | **None** (public URL) |
| T6 | Agents → AWS Bedrock Converse | HTTPS | SigV4 (runtime role / IRSA / Cloud Run static keys) |
| T7 | Agents → AWS DynamoDB / Lambda | HTTPS | SigV4 (runtime role / IRSA) |
| T8 | Cloud Run (GCP) → AWS Bedrock | HTTPS cross-cloud | SigV4 (static IAM user keys stored as Cloud Run secrets) |
| T9 | Hotels pod → Cloudflare edge | Outbound TLS (QUIC) | Cloudflare-side identity (quick tunnel registration) |
| T10 | All agents → CloudWatch OTLP ingest | HTTPS SigV4 | Runtime role / IRSA / Cloud Run static keys |

## 3. STRIDE analysis per boundary

### T4 — Orchestrator → EKS hotels pod (public Cloudflare tunnel URL)

| Threat | Risk | Mitigation (current) | Residual risk |
|---|---|---|---|
| **S**poofing: anyone on the internet can reach `/invocations` on the hotels pod | High in production, Low in sample | Sample demo only; fixture DynamoDB data; prompt-only interface; no stored secrets reachable from the hotels container | Documented in README; customers must add auth before any production use |
| **T**ampering: trace-context payload field (`_trace_context.traceparent`) can be forged | Low | Trace context affects observability tree only, not data; invalid traceparent is dropped by OTel SDK | Accepted — observability-only impact |
| **R**epudiation: no per-invocation audit trail from unauthenticated clients | Low | OTel traces + CloudWatch Logs capture every invocation with span IDs | Accepted for a sample |
| **I**nformation disclosure: prompt + response in OTel spans | Low | Spans route through SigV4-signed OTLP to CloudWatch in the account owner's region; `AWS_GENAI_CONTENT_EXTRACTION_OPT_OUT=true` keeps content on the span (private per account) | Documented; customers should review `OTEL_RESOURCE_ATTRIBUTES` and `AWS_GENAI_CONTENT_EXTRACTION_OPT_OUT` before deploying |
| **D**oS: public tunnel + public pod | Medium | Cloudflare quick tunnel has implicit DoS protection at the edge; pod has `max 1 replica` + CPU/memory limits; Fargate scales under load | Residual DoS risk is accepted for sample; customers should put an authenticated Ingress + rate limiting in production |
| **E**oP: compromised pod → broader AWS access | Low | IRSA role scoped to DynamoDB `GetItem` on `travel-concierge-hotels` + Bedrock `InvokeModel` + X-Ray write — no S3, no IAM, no cross-service writes | Documented minimal IRSA policy in `remote-agents/hotels-eks/iam-policy.json` |

**Hard mitigation offered in the sample's README**: swap the Cloudflare
quick tunnel for a Cloudflare named tunnel (DNS CNAME + stable URL) or an
authenticated ALB Ingress — see `remote-agents/hotels-eks/README.md`.

### T5 — Orchestrator → GCP Cloud Run activities (public URL)

| Threat | Risk | Mitigation (current) | Residual risk |
|---|---|---|---|
| **S**poofing: `--allow-unauthenticated` on the Cloud Run service | High in production, Low in sample | Cloud Run endpoint is public; no shared secret; prompt-only interface | Documented in README; customers should use Cloud Run IAM invoker auth + Workload Identity Federation in production |
| **T**ampering: payload-embedded trace context | Low | Observability-only, same as T4 | Accepted |
| **I**nformation disclosure: Bedrock prompts/responses via cross-cloud HTTPS | Low | HTTPS enforces encryption in transit; AWS static-key credentials are Cloud Run secrets (not in container image); content captured on spans goes to the owner's CloudWatch account only | Documented |
| **D**oS | Medium | Cloud Run concurrency: 10, max instances: 2 — natural cap | Accepted for sample |
| **E**oP: compromised Cloud Run container → Bedrock + CloudWatch across accounts | Medium — this is the real one | Static IAM user `travel-concierge-remote-gcp` is scoped to `bedrock:InvokeModel` on Claude Sonnet 4 + `xray:PutTraceSegments / PutSpans` only. No DynamoDB, no Lambda, no S3. Even a full container compromise cannot reach beyond those two APIs on the demo account. | Static keys are the real residual risk — documented in README with explicit mitigation: use GCP Workload Identity Federation + `AssumeRoleWithWebIdentity` in production. Static keys rotated manually. |

### T8 — Static IAM keys as Cloud Run secrets

Elaborated separately because it's the thing a Guardian will zero in on.

| Risk | Current state | Mitigation |
|---|---|---|
| Static long-lived IAM keys embedded in Cloud Run env | Yes — stored as Cloud Run secrets (not image), encrypted at rest by GCP Secret Manager | **Scoped to only `bedrock:InvokeModel` on one model + `xray:PutTraceSegments/PutSpans`.** No other API access. If leaked, blast radius is limited to additional Bedrock inference charges + spurious X-Ray spans. No data loss, no cross-service escalation. |
| Rotation | Manual | Documented in README. Customers should automate rotation or (preferred) replace with GCP Workload Identity Federation + `AssumeRoleWithWebIdentity`. |
| Audit | CloudTrail captures every API call attributed to the IAM user | Standard CloudTrail detection applies |

### T6 — Agents → AWS Bedrock Converse

| Threat | Risk | Mitigation |
|---|---|---|
| **I**nformation disclosure: prompts contain PII | None in sample | No customer data is ever placed into prompts. Sample uses fixture flight/hotel data from DynamoDB. |
| **E**oP: compromised agent → wider Bedrock access | Low | Each runtime role scoped to `InvokeModel` on exactly `us.anthropic.claude-sonnet-4-5-20250929-v1:0`. No access to custom models, provisioned throughput, or model customization. |

### T7 — Agents → DynamoDB / Lambda

| Threat | Risk | Mitigation |
|---|---|---|
| **E**oP: compromised flights agent → `travel-concierge-flights` table | Low | Role allows `GetItem` only (no `PutItem`, `DeleteItem`, `Scan`). Table contains fixture data — nothing sensitive. |
| **E**oP: compromised weather agent → Lambda | Low | Role allows `lambda:InvokeFunction` on exactly `travel-concierge-weather-provider`. Lambda returns computed forecast strings, no state. |

### T10 — Agents → CloudWatch OTLP ingest

| Threat | Risk | Mitigation |
|---|---|---|
| **I**nformation disclosure: span content contains sensitive data | None in sample | Content capture explicitly enabled (`AWS_GENAI_CONTENT_EXTRACTION_OPT_OUT=true`) and documented. Customers should flip this to `false` if they want LLO extraction to separate log records, or disable content capture if their prompts contain regulated data. |
| **T**ampering: compromised agent emits forged spans | Low | SigV4 attributes every span to its emitting role / IRSA. Downstream analysis in Omni / DevOps Agent treats trace data as reportive, not authoritative. |
| **D**oS: span volume cost explosion | Low | 100% sampling is default (Omni requires it for agent evaluation); customers can set `OTEL_TRACES_SAMPLER=parentbased_traceidratio` to control cost — documented. |

## 4. Non-STRIDE concerns

### Prompt injection

Any public endpoint that forwards user input to Bedrock is a prompt-injection
surface. In this sample:
- Hotels EKS (T4) and Activities Cloud Run (T5) accept arbitrary prompts.
- An attacker who knows the public URLs can submit prompts that attempt
  to exfiltrate information, trigger excessive token usage, or produce
  misleading output.
- Impact is bounded: no system prompts contain secrets; tool-call outputs
  are strictly structured (DynamoDB fixtures, Bedrock forecast strings).
- Guardrails are not applied (sample simplicity). Customers deploying this
  sample should attach Bedrock Guardrails + input length limits before
  production use.

Documented in README as a known limitation.

### Dependency supply chain

- `aws-opentelemetry-distro>=0.21.0`, `strands-agents>=1.22`, `crewai>=1.10`,
  `langgraph>=0.6` — all permissive-licensed (Apache 2.0 or MIT) and
  actively maintained.
- No pinned-to-exact versions in `requirements.txt`; sample users will
  get latest-compatible on each build. Documented; customers running in
  production should pin and audit.
- Container base images: `python:3.12-slim`, `python:3.13-slim`,
  `cloudflare/cloudflared:latest`. The `:latest` tag on `cloudflared`
  is a weak-point for reproducibility; documented in the EKS manifest's
  comments.

### Secrets in Git

- No AWS keys, GCP service account JSON, or API tokens are committed.
- `.env.omni` is gitignored (contains the Omni local collector token).
- Repository was scanned with git-secrets / gitleaks locally before the
  first public push (recommend re-running before each release).

### Multi-tenant / shared-cloud concerns

Not applicable — this is a self-contained sample intended to be deployed
once per demo account. No multi-tenant resource sharing.

## 5. Appendix C escalation check

The Appendix C clauses that would escalate from PCSR to full AWS AppSec
review:

| Clause | Applies to this sample? |
|---|---|
| Prohibited GenAI use cases (medical diagnosis, legal advice, financial advice, etc.) | No — the sample recommends travel activities and summarizes flight/hotel options |
| Third-party generative AI service as content producer | No — only AWS Bedrock Claude is used for inference |
| Customer PII / PHI handling | No — no customer data in the system |
| Regulated-industry framing | No — travel-planning demo content |
| Direct customer production integration | No — explicitly non-production per README disclaimer |
| Processing non-public Amazon data | No — fixture data only |

Conclusion: **PCSR is the correct review path.** Full AWS AppSec review
(via Talos) is not required.

## 6. Review summary

Residual risks accepted for sample code:
1. Public, unauthenticated hotels EKS endpoint (T4) — mitigation documented
2. Public, unauthenticated Cloud Run activities endpoint (T5) — mitigation documented
3. Static IAM keys for GCP → AWS (T8) — scoped minimally, mitigation documented
4. Prompt-injection surface on T4 + T5 — mitigation documented (Bedrock Guardrails)
5. `cloudflared:latest` tag — documented reproducibility weakness

All five are explicitly non-production limitations surfaced in the README
sample-code disclaimer and inline in the relevant deploy scripts / Dockerfiles.
No high/critical security findings expected from automated scanners
(Slingshot / Probe / semgrep / gitleaks) against this sample.
