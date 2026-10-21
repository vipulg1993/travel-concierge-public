# Hotels specialist on EKS Fargate

A second deployment of the Hotels agent — same Strands code as the AgentCore
version — hosted on EKS Fargate instead. Demonstrates that the Omni
observability pipeline, agent code, and trace shape are compute-platform
agnostic: the AgentCore Runtime version, the Cloud Run GCP activities, the
Lambda weather provider, and this EKS Fargate pod all land in the same
unified Omni trace tree per request.

## Why EKS Fargate (not EC2 nodes)

- **No node management**: no AMIs, no autoscaler, no node patching.
- **Smaller cost floor**: Fargate charges per vCPU-hour of running pods.
  One 0.5 vCPU / 1 GB pod ≈ \$5-7/month, versus a t3.medium node at ~\$30/month
  even when idle.
- **Shared ergonomics**: same IRSA, same ADOT auto-instrumentation, same
  trace emission as any other container on AWS.

**Cost caveats**: EKS control plane is ~\$73/month regardless of pod count.
Expect ~\$80/month total if left on. Run `teardown.sh` when done to stop
the meter.

## Why Cloudflare Tunnel for external exposure (not ALB/NLB)

Standard AWS Load Balancers in this account/region (us-east-1) are being
assigned public IPs from `100.48.0.0/12`, which sits adjacent to the
carrier-grade NAT (CGN) reserved space `100.64.0.0/10`. Many ISPs
(Verizon in particular) and most corporate perimeters aggressively filter
`100.x.x.x` as "non-routable", so SYN packets from clients on those
networks never reach the LB.

VPC Flow Logs confirm the drops happen upstream — not in our VPC. We
can't influence IP allocation. Options were:

1. Delete + recreate the LB hoping for different IPs — tried, still got 100.x.
2. Rebuild as EKS Auto Mode (AWS's canonical pattern for agentic AI on
   EKS, per the [FSI blog](https://aws.amazon.com/blogs/industries/multi-agent-systems-for-financial-services-on-amazon-eks-and-agentcore/)) — would work but is ~30 min of rebuild.
3. **Cloudflare Tunnel sidecar — picked.** The pod runs a `cloudflared`
   sidecar that connects outbound to Cloudflare's edge and gets back a
   public `*.trycloudflare.com` URL. Clients hit Cloudflare's anycast IPs
   (which no one filters). No AWS LB involved at all.

The tunnel is the same pattern we use for the GCP Cloud Run activities
agent, so the architecture stays consistent across all remote agents.

**Known limitation**: the quick-tunnel URL is EPHEMERAL — it changes on
every pod restart. Re-run `deploy.sh` to republish the new URL to SSM,
then redeploy the orchestrator to pick it up. For a stable multi-day
URL, swap to a named Cloudflare tunnel + Cloudflare API token (see
"Stable URL" below).

## Architecture

```
   Streamlit frontend
         │
         ▼ bedrock-agentcore:InvokeAgentRuntime (SigV4, SSE)
    Orchestrator  ──── (3 other specialists via AgentCore) ──── ...
   (AgentCore Runtime)
         │
         ▼ HTTPS POST /invocations (W3C traceparent)
   ┌─────────────────────────────────────────────────────────┐
   │  https://<random>.trycloudflare.com                     │
   │  (Cloudflare anycast — no AWS LB in the data path)      │
   └─────────────────────────────────────────────────────────┘
                              │
                              ▼ outbound QUIC/HTTP2 tunnel
   ┌─────────────────────────────────────────────────────────┐
   │  EKS Fargate pod — hotels/hotels                        │
   │                                                         │
   │  ┌─ cloudflared sidecar (public tunnel edge) ─────────┐ │
   │  │  cloudflared tunnel --url http://localhost:8080    │ │
   │  └────────────────────────────────────────────────────┘ │
   │           │ localhost:8080                              │
   │  ┌────────▼───────────────────────────────────────────┐ │
   │  │  hotels container — Strands agent on FastAPI       │ │
   │  │  Image: <AWS_ACCOUNT_ID>.dkr.ecr.us-east-1.amazonaws   │ │
   │  │         .com/travel-concierge-hotels:latest        │ │
   │  │  Entry: opentelemetry-instrument uvicorn main:app  │ │
   │  │     └─ aws-opentelemetry-distro 0.21               │ │
   │  │        └─ OTLPAwsSpanExporter → CloudWatch OTLP    │ │
   │  │                                   traces (IRSA)    │ │
   │  │  Env: identical to AgentCore runtime env + a few   │ │
   │  │       OTEL_SERVICE_NAME overrides for topology.    │ │
   │  └────────────────────────────────────────────────────┘ │
   │  ServiceAccount: hotels  (IRSA → hotels-eks-role)       │
   │  Namespace: hotels   Fargate profile: fp-default        │
   └─────────────────────────────────────────────────────────┘
                 │                 │
                 ▼                 ▼
          DynamoDB            Amazon Bedrock
   (travel-concierge-     (us.anthropic.claude-
       hotels, GetItem)    sonnet-4, Converse)
```

## Files

| File | Purpose |
|---|---|
| `cluster.yaml` | eksctl spec that creates the EKS cluster (1.31, Fargate-only, OIDC enabled). |
| `iam-policy.json` | IRSA policy granting DDB read on the hotels table + Bedrock InvokeModel + X-Ray write. |
| `manifests.yaml` | Namespace + ServiceAccount + Deployment (hotels + cloudflared sidecar). No Service/Ingress — Cloudflare Tunnel handles exposure. |
| `deploy.sh` | One-shot deploy: kubeconfig, IRSA, container build+push, kubectl apply, extract tunnel URL, write to SSM. |
| `teardown.sh` | Full cleanup: deletes the cluster, IRSA role, Fargate profiles, VPC. |

## One-time setup

```bash
# Create the cluster (takes ~15 min)
export AWS_PROFILE=omni-demo AWS_REGION=us-east-1
eksctl create cluster -f cluster.yaml
```

## Deploy / redeploy the hotels agent

```bash
./deploy.sh
# Prints the current tunnel URL + writes it to SSM at
#   /travel-concierge/remote-agents/hotels-eks-url
```

## Rewire the orchestrator to call EKS instead of AgentCore

```bash
# From the project root:
AWS_PROFILE=omni-demo python3 infra/deploy.py --only orchestrator
# Picks up HOTELS_EKS_URL from SSM. The orchestrator's search_hotels tool
# now calls the Cloudflare tunnel → EKS pod; the AgentCore hotels runtime
# becomes an unused fallback.
```

To revert (orchestrator uses AgentCore hotels again):

```bash
AWS_PROFILE=omni-demo aws ssm delete-parameter \
  --name /travel-concierge/remote-agents/hotels-eks-url --region us-east-1
AWS_PROFILE=omni-demo python3 infra/deploy.py --only orchestrator
```

## Smoke test

```bash
URL=$(aws ssm get-parameter --profile omni-demo --region us-east-1 \
      --name /travel-concierge/remote-agents/hotels-eks-url \
      --query 'Parameter.Value' --output text)

# IMPORTANT: trycloudflare.com DNS is blocked on many corporate networks
# (including Amazon's). If `/ping` times out locally, test from AWS
# directly (e.g. CloudShell) or trust the orchestrator trace — AgentCore
# resolves it fine.
curl -sS "$URL/ping"
```

Alternatively, fire a full orchestrator query and look at Omni:

```bash
AWS_PROFILE=omni-demo python3 scripts/load_gen.py --count 1 --parallel 1 \
  --query "Trip to Boston Dec 22-28 2026 under \$2500."
```

The resulting trace's `execute_tool search_hotels` subsegment should be
an HTTP span to `*.trycloudflare.com` (instead of `BedrockAgentCore.
InvokeAgentRuntime` as before).

## Stable URL (multi-day demo)

The quick tunnel URL changes on every pod restart. For a stable URL:

1. Create a free Cloudflare account.
2. Create a named tunnel: `cloudflared tunnel create travel-concierge-hotels`
3. Save the tunnel credentials JSON file.
4. Create a K8s Secret with the credentials.
5. Update the cloudflared sidecar args from `tunnel --url ...` to
   `tunnel run --token <TUNNEL_TOKEN>` (or mount the credentials and
   use `tunnel run travel-concierge-hotels`).
6. Point the DNS at the tunnel: `cloudflared tunnel route dns travel-concierge-hotels hotels.<yourdomain>.com`.

The URL `https://hotels.<yourdomain>.com` is then stable across pod
restarts.

## Teardown (stop paying)

```bash
./teardown.sh
```

Removes: Deployment → ServiceAccount → IRSA role → namespace → cluster.
Also unregisters the SSM URL so the orchestrator falls back to AgentCore
hotels on its next deploy.
