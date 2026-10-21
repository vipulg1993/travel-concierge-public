# Activities Specialist — GCP Cloud Run deploy

A LangGraph agent packaged in one container,
deployed to Google Cloud Run from a fresh GCP account. The agent calls AWS
Bedrock cross-cloud for the LLM and ships OTel traces back to AWS Transaction
Search so Omni shows one unified trace spanning AWS + GCP.

Permanent URL, scales to zero when idle, no tunnels.

---

## Prerequisites

- GCP account with billing enabled (free tier credits apply) — you have this
- `gcloud` CLI installed:
  ```bash
  brew install --cask google-cloud-sdk
  ```
- `omni-demo` AWS profile working on your laptop

---

## Step 1 — AWS: create a scoped IAM user for the remote agent

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
        "Action":["bedrock:InvokeModel","bedrock:Converse","bedrock:ConverseStream"],
        "Resource":[
          "arn:aws:bedrock:us-east-1:<AWS_ACCOUNT_ID>:inference-profile/us.anthropic.claude-sonnet-4-5-20250929-v1:0",
          "arn:aws:bedrock:*::foundation-model/anthropic.claude-sonnet-4-5-20250929-v1:0"
        ]
      },
      {
        "Effect":"Allow",
        "Action":["xray:PutTraceSegments","xray:PutSpans","xray:PutSpansForIndexing","logs:PutLogEvents","logs:CreateLogStream","logs:CreateLogGroup","logs:DescribeLogStreams","logs:DescribeLogGroups"],
        "Resource":"*"
      }
    ]
  }'

aws iam create-access-key --user-name travel-concierge-remote-gcp
```

Copy the printed `AccessKeyId` and `SecretAccessKey`.

Store them in a local file the deploy script reads:

```bash
cat > ~/.travel-concierge-remote-aws-creds <<'EOF'
AWS_ACCESS_KEY_ID=AKIA...REPLACE
AWS_SECRET_ACCESS_KEY=...REPLACE
AWS_REGION=us-east-1
EOF
chmod 600 ~/.travel-concierge-remote-aws-creds
```

---

## Step 2 — GCP: authenticate gcloud

**On your laptop:**

```bash
gcloud auth login
gcloud config set project omni-demo-511113
```

(Replace `omni-demo-511113` with your actual project ID — the one shown in the
GCP console sidebar.)

---

## Step 3 — Deploy

```bash
cd travel-concierge/remote-agents/activities-cloudrun/
./deploy.sh
```

The script will:
1. Verify your AWS creds + gcloud auth
2. Enable Cloud Build, Cloud Run, Artifact Registry APIs (idempotent)
3. Submit a source build (Cloud Build packages the Dockerfile for you — no local Docker needed)
4. Deploy the result as a Cloud Run service
5. Print the permanent URL

First deploy takes ~3-5 min (Cloud Build download + Python layer). Subsequent
deploys are much faster.

**Expected final output:**

```
═══════════════════════════════════════════════════════════
  DEPLOYED
═══════════════════════════════════════════════════════════

  URL: https://activities-agent-xxxxxxxx-uc.a.run.app

  Smoke test:
    curl -sS https://activities-agent-xxxxxxxx-uc.a.run.app/ping

  Register with the orchestrator (uses omni-demo profile):
    AWS_PROFILE=omni-demo aws ssm put-parameter ...
```

---

## Step 4 — Smoke test

```bash
URL=https://activities-agent-xxxxxxxx-uc.a.run.app

curl -sS "$URL/ping"
# → {"status":"ok","service":"activities","cloud":"gcp","framework":"langgraph"}

curl -sS -X POST "$URL/invocations" \
  -H 'Content-Type: application/json' \
  -d '{"input":{"message":"Suggest activities in Seattle during Christmas week interests: museums coffee hiking"}}'
```

The POST takes ~5-10 s (Bedrock inference). Returns JSON with the activities.

Within ~30 s the trace appears in CloudWatch Transaction Search with
`service.name = travel_concierge_activities.gcp` and `cloud.provider = gcp`.

---

## Step 5 — Register the agent with the orchestrator

```bash
AWS_PROFILE=omni-demo aws ssm put-parameter \
  --name /travel-concierge/remote-agents/activities-gcp-url \
  --value "$URL" \
  --type String --overwrite --region us-east-1
```

---

## Step 6 — (Next) Wire the orchestrator to call this agent

Once Step 5 is done, I'll update the orchestrator to add a `recommend_activities`
tool that invokes this URL. See the parent `travel-concierge/README.md`.

---

## Costs

- **Cloud Run**: scales to zero; idle = $0. 2M requests/mo and 180k vCPU-sec free. Our demo volume stays within free tier.
- **Cloud Build**: 120 build-minutes/day free. Our build takes ~3 min.
- **Artifact Registry**: 0.5 GB free. Our image is ~250 MB.
- **AWS Bedrock + CloudWatch**: unchanged (same bill as today).
- **Egress GCP→AWS**: < 10 MB for a full demo day, pennies.

Total out-of-pocket for a 2-month demo run: realistically **$0-5**.

---

## Teardown

```bash
# Delete the Cloud Run service (stops all charges)
gcloud run services delete activities-agent --region us-central1 --quiet

# Delete the AWS user when done
aws iam list-access-keys --user-name travel-concierge-remote-gcp \
  --profile omni-demo --query 'AccessKeyMetadata[].AccessKeyId' --output text \
  | xargs -n1 aws iam delete-access-key --profile omni-demo \
    --user-name travel-concierge-remote-gcp --access-key-id
aws iam delete-user-policy --profile omni-demo \
  --user-name travel-concierge-remote-gcp --policy-name RemoteAgentAccess
aws iam delete-user --profile omni-demo --user-name travel-concierge-remote-gcp
```
