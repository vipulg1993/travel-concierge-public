# Activities Specialist — GCP Cloud Shell deploy

A LangGraph agent running in Google Cloud Shell that proves one trace spans
AWS and GCP. The agent calls Bedrock Claude cross-cloud, exports OTel traces
back to AWS via an ADOT Collector, and gets reached by the orchestrator over a
free Cloudflare tunnel.

## Prerequisites

**On your laptop:**
- The travel-concierge system deployed and working (orchestrator + 4 specialists)
- AWS CLI with `omni-demo` profile that works (`aws sts get-caller-identity`)
- A Google account

## Step 1 — Create AWS credentials for the GCP agent

The GCP agent needs AWS credentials to (a) call Bedrock cross-cloud and (b) sign OTLP exports to CloudWatch. Create a dedicated IAM user with just those permissions so the credentials you paste into Cloud Shell are scoped.

**On your laptop:**

```bash
export AWS_PROFILE=omni-demo

aws iam create-user --user-name travel-concierge-remote-gcp 2>/dev/null || true

aws iam put-user-policy --user-name travel-concierge-remote-gcp \
  --policy-name RemoteAgentAccess \
  --policy-document '{
    "Version":"2012-10-17",
    "Statement":[
      {
        "Effect":"Allow",
        "Action":[
          "bedrock:InvokeModel",
          "bedrock:Converse",
          "bedrock:ConverseStream"
        ],
        "Resource":[
          "arn:aws:bedrock:us-east-1:<AWS_ACCOUNT_ID>:inference-profile/us.anthropic.claude-sonnet-4-5-20250929-v1:0",
          "arn:aws:bedrock:*::foundation-model/anthropic.claude-sonnet-4-5-20250929-v1:0"
        ]
      },
      {
        "Effect":"Allow",
        "Action":[
          "xray:PutTraceSegments",
          "xray:PutSpans",
          "xray:PutSpansForIndexing",
          "logs:PutLogEvents",
          "logs:CreateLogStream",
          "logs:CreateLogGroup",
          "logs:DescribeLogStreams",
          "logs:DescribeLogGroups"
        ],
        "Resource":"*"
      }
    ]
  }'

aws iam create-access-key --user-name travel-concierge-remote-gcp
```

Copy the `AccessKeyId` and `SecretAccessKey` from the last command's output —
you'll paste these into Cloud Shell in Step 3.

## Step 2 — Open GCP Cloud Shell

1. Go to https://shell.cloud.google.com (sign in with any Google account).
2. Wait for the terminal to initialize (~15 seconds first time).

## Step 3 — Set credentials and copy the agent code

In the Cloud Shell terminal:

```bash
# Paste your AWS credentials from Step 1
cat > ~/aws-creds.env <<'EOF'
export AWS_ACCESS_KEY_ID=AKIA...   # ← REPLACE
export AWS_SECRET_ACCESS_KEY=...   # ← REPLACE
export AWS_REGION=us-east-1
EOF
chmod 600 ~/aws-creds.env
source ~/aws-creds.env

# Verify they work (Cloud Shell has no aws CLI by default — use boto3)
pip install --quiet boto3
python3 -c "import boto3; print(boto3.client('sts').get_caller_identity())"
# Should print the ARN: arn:aws:iam::<AWS_ACCOUNT_ID>:user/travel-concierge-remote-gcp
```

Copy the agent code into Cloud Shell. Easiest way: upload the folder via the
"⋮ → Upload" menu in Cloud Shell. Or paste each file inline.

```bash
mkdir -p ~/activities-gcp
cd ~/activities-gcp
# <upload the 5 files: app.py, otel_setup.py, requirements.txt,
#  collector-config.yaml, bootstrap.sh>
chmod +x bootstrap.sh
```

## Step 4 — Run the bootstrap

```bash
cd ~/activities-gcp
./bootstrap.sh
```

The script will:
1. Create a Python venv and install LangGraph + OTel packages (~1 min)
2. Download the ADOT Collector binary (~15 s)
3. Download `cloudflared` (~5 s)
4. Start the Collector on `:4318`
5. Start the FastAPI agent on `:8080`
6. Start a Cloudflare tunnel and print the public URL

Watch for the line like:
```
Your quick Tunnel has been created! Visit it at:
https://xyz-abc-123.trycloudflare.com
```

Copy that URL. Leave this terminal running — closing it kills the agent.

## Step 5 — Register the agent URL in AWS SSM

**On your laptop (NOT Cloud Shell):**

```bash
export AWS_PROFILE=omni-demo

aws ssm put-parameter \
  --name /travel-concierge/remote-agents/activities-gcp-url \
  --value "https://xyz-abc-123.trycloudflare.com" \
  --type String --overwrite --region us-east-1
```

## Step 6 — Smoke test the GCP agent directly

**On your laptop:**

```bash
URL=$(aws ssm get-parameter --name /travel-concierge/remote-agents/activities-gcp-url --region us-east-1 --query 'Parameter.Value' --output text)
curl -sS "$URL/ping"
# → {"status":"ok","service":"activities","cloud":"gcp","framework":"langgraph"}

curl -sS -X POST "$URL/invocations" \
  -H 'Content-Type: application/json' \
  -d '{"input":{"message":"Suggest activities in Seattle during Christmas week interests: museums coffee hiking"}}' | jq
```

You should get a JSON body with `destination`, `dates`, `interests`, and a
multi-line `activities` field.

Within ~30 seconds of this smoke test, a new trace appears in CloudWatch
Transaction Search with `service.name = travel_concierge_activities.gcp`
and `cloud.provider = gcp` resource attributes.

## Step 7 — Wire the orchestrator to call the GCP agent

See the parent `travel-concierge/README.md` for the orchestrator update that
adds a `recommend_activities` tool pointing at the SSM-registered URL.

Once wired up and the orchestrator is redeployed, firing a trip query in the
Streamlit UI will produce a trace that fans out to **4 AWS AgentCore specialists
plus the GCP-hosted Activities agent**, all stitched into one trace in Omni.

## Logs during the demo

- `collector.log` — ADOT Collector export status (look for "sent" messages)
- `agent.log` — FastAPI + LangGraph output
- `cloudflared.log` — tunnel status

## Teardown

```bash
# In Cloud Shell:
pkill -f otelcol-contrib
pkill -f "python3 app.py"
pkill -f cloudflared

# On your laptop (remove the dedicated AWS user when done):
aws iam list-access-keys --user-name travel-concierge-remote-gcp \
  --query 'AccessKeyMetadata[].AccessKeyId' --output text \
  | xargs -n1 aws iam delete-access-key --user-name travel-concierge-remote-gcp --access-key-id
aws iam delete-user-policy --user-name travel-concierge-remote-gcp --policy-name RemoteAgentAccess
aws iam delete-user --user-name travel-concierge-remote-gcp
```

## What's not persistent

Cloud Shell sessions time out after ~20 minutes idle. When the terminal
closes, the agent dies and the tunnel URL expires. For a persistent demo,
redeploy the same code to **Cloud Run** (within GCP's always-free tier). The
code is unchanged; only the deploy target differs.
