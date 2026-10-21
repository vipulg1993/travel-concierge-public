# Travel Concierge

A multi-agent travel planner that demonstrates **AWS Bedrock AgentCore Runtime**
alongside **CloudWatch Omni** and the **AWS DevOps Agent**. One Streamlit UI
drives an orchestrator that fans out to five specialist agents (flights,
hotels, weather, budget, activities) — each independently deployable — and
emits a unified OpenTelemetry trace tree that lands in CloudWatch
Transaction Search.

![Overview](docs/architecture-overview.png)

## What's in here

- **5 Strands/CrewAI agents** on AgentCore Runtime (`agents/`), orchestrated
  by a 6th. All share the same OTel instrumentation (`otel_patch.py`) and
  FastAPI shell shape.
- **2 remote specialists** off AgentCore to prove cross-platform
  interoperability (`remote-agents/`): hotels on EKS Fargate behind a
  Cloudflare tunnel, and activities on Google Cloud Run (two variants).
- **Infra scripts** (`infra/`) that stand up / tear down the whole
  workload: 5 IAM roles, 2 DynamoDB tables, 1 Lambda weather provider,
  5 AgentCore Runtimes, 5 ECR repos.
- **Streamlit UI** (`frontend/app.py`) that invokes the orchestrator via
  `bedrock-agentcore:InvokeAgentRuntime`.
- **Docs** (`docs/`) covering architecture and telemetry ingestion.

See `docs/architecture.md` for the full system picture and
`docs/telemetry-ingestion.md` for how spans flow from agents into Omni.

## Required environment

Every infra command and the EKS deploy script read these:

| Variable          | Required | Example                 | Notes                                                      |
|-------------------|----------|-------------------------|------------------------------------------------------------|
| `AWS_PROFILE`     | yes      | `omni-demo`             | Any profile with admin on the demo account.                |
| `AWS_ACCOUNT_ID`  | yes      | `123456789012`          | Your 12-digit account id. Scripts fail loudly if unset.    |
| `AWS_REGION`      | no       | `us-east-1` (default)   | Primary region for every AWS resource.                     |

Export both before any `infra/*.py` call. The infra Python code resolves the
account id via `infra/_config.py`, which prefers `AWS_ACCOUNT_ID` and falls
back to `sts:GetCallerIdentity` on the active profile.

```bash
export AWS_PROFILE=omni-demo
export AWS_ACCOUNT_ID=$(aws sts get-caller-identity --query Account --output text)
export AWS_REGION=us-east-1
```

## Prerequisites

- `python >= 3.12`
- `aws` CLI configured with your profile
- `finch` or `docker` (ARM64 build support, for `agents/*/Dockerfile`)
- `gh` CLI (only if you fork this repo)
- `brew install gitleaks` if you plan to re-run the secret scan locally
- For `remote-agents/hotels-eks/`: `eksctl`, `kubectl`
- For `remote-agents/activities-{gcp,cloudrun}/`: `gcloud` + a GCP project
  with Cloud Run enabled

Install Python deps once per agent (each has its own `requirements.txt`).

## Deploy to prod (AgentCore)

The infra scripts are idempotent. Run them in order:

```bash
# 1. SSM policies, DynamoDB tables, weather Lambda, 5 IAM roles.
AWS_PROFILE=omni-demo AWS_ACCOUNT_ID=$AWS_ACCOUNT_ID python3 infra/setup.py

# 2. Seed the DDB tables with mock flights/hotels inventory.
AWS_PROFILE=omni-demo AWS_ACCOUNT_ID=$AWS_ACCOUNT_ID python3 infra/seed_data.py

# 3. Build + push each agent's container, register as an AgentCore Runtime.
AWS_PROFILE=omni-demo AWS_ACCOUNT_ID=$AWS_ACCOUNT_ID python3 infra/deploy.py

# 4. Launch the Streamlit UI.
cd frontend && streamlit run app.py
# → http://localhost:8501
```

Optional remote specialists (replace the AgentCore flights pod with a
Kubernetes pod and an off-AWS activities pod):

```bash
AWS_PROFILE=omni-demo AWS_ACCOUNT_ID=$AWS_ACCOUNT_ID remote-agents/hotels-eks/deploy.sh
# For activities: see remote-agents/activities-gcp/README.md
```

## Run the prod UI

Streamlit always points at prod AgentCore (via SSM/ARN). Use this to drive
the deployed system from a browser.

```bash
AWS_PROFILE=omni-demo ./scripts/streamlit-prod.sh
# → http://localhost:8501
#   custom port: ./scripts/streamlit-prod.sh --port 8502
```

The script explicitly unsets `ORCHESTRATOR_LOCAL_URL` so the frontend
takes the SSM/ARN path even if that env var is set elsewhere.

## Docs

- [`docs/architecture.md`](docs/architecture.md) — full system walk-through,
  trace shape, AWS resource inventory
- [`docs/telemetry-ingestion.md`](docs/telemetry-ingestion.md) — per-agent
  OTel configuration, trace stitching, and GenAI content rendering

## License

MIT — see [LICENSE](LICENSE).
