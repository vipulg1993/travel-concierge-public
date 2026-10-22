#!/usr/bin/env bash
# Deploy the CloudWatch → operations backlog integration for Travel Concierge.
#
# Creates:
#   - IAM role     TravelConcierge-OpsAutomation-Role
#   - Lambda       tc-ops-alert-dispatcher
#   - SNS topic    tc-production-alerts
#   - SNS → Lambda subscription + Lambda invoke permission
#   - CloudWatch   TC-WeatherProvider-Duration-SLO alarm (publishes to the SNS topic)
#
# All resources are idempotent — re-running updates in place.
#
# Usage: AWS_PROFILE=omni-demo bash deploy.sh

set -euo pipefail

AWS_REGION="${AWS_REGION:-us-east-1}"
ACCOUNT_ID=$(aws sts get-caller-identity --query 'Account' --output text)

ROLE_NAME="TravelConcierge-OpsAutomation-Role"
FUNCTION_NAME="tc-ops-alert-dispatcher"
TOPIC_NAME="tc-production-alerts"
ALARM_NAME="TC-WeatherProvider-Duration-SLO"

AGENT_SPACE_ID="745987a0-1671-4eb4-b224-358c01e22383"
ASSOCIATION_ID="5c1dd9a9-2d9a-46ba-8997-96052de4e3e6"

# Service being alarmed on
TARGET_FUNCTION="travel-concierge-weather-provider"
DURATION_THRESHOLD_MS=500   # average over 60s window; baseline ~100-150ms

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"

echo "=== Account: $ACCOUNT_ID  Region: $AWS_REGION ==="
echo

# ── 1. IAM role ──────────────────────────────────────────────────────────────
echo "[1/6] IAM role: $ROLE_NAME"
TRUST=$(cat <<EOF
{"Version":"2012-10-17","Statement":[{"Effect":"Allow","Principal":{"Service":"lambda.amazonaws.com"},"Action":"sts:AssumeRole"}]}
EOF
)

if aws iam get-role --role-name "$ROLE_NAME" --region "$AWS_REGION" >/dev/null 2>&1; then
  echo "  role exists"
else
  aws iam create-role \
    --role-name "$ROLE_NAME" \
    --assume-role-policy-document "$TRUST" \
    --description "Routes CloudWatch alarms to the operations backlog" \
    --query 'Role.Arn' --output text
  echo "  role created, waiting for IAM propagation..."
  sleep 10
fi
ROLE_ARN="arn:aws:iam::${ACCOUNT_ID}:role/${ROLE_NAME}"

# Attach the Lambda-basic-execution managed policy
aws iam attach-role-policy \
  --role-name "$ROLE_NAME" \
  --policy-arn arn:aws:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole \
  >/dev/null 2>&1 || true

# Inline policy for devops-agent:CreateBacklogTask
INLINE_POLICY=$(cat <<EOF
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Effect": "Allow",
      "Action": ["aidevops:CreateBacklogTask"],
      "Resource": "*"
    }
  ]
}
EOF
)
aws iam put-role-policy \
  --role-name "$ROLE_NAME" \
  --policy-name "DevOpsAgentBacklogWrite" \
  --policy-document "$INLINE_POLICY" >/dev/null
echo "  role policies attached"
echo

# ── 2. SNS topic ─────────────────────────────────────────────────────────────
echo "[2/6] SNS topic: $TOPIC_NAME"
TOPIC_ARN=$(aws sns create-topic \
  --name "$TOPIC_NAME" \
  --region "$AWS_REGION" \
  --query 'TopicArn' --output text)
echo "  $TOPIC_ARN"
echo

# ── 3. Package & upload Lambda ───────────────────────────────────────────────
echo "[3/6] Package Lambda source"
BUILD_DIR=$(mktemp -d)
cp "$SCRIPT_DIR/lambda_handler.py" "$BUILD_DIR/"
(cd "$BUILD_DIR" && zip -q lambda.zip lambda_handler.py)
ZIP_BYTES=$(wc -c < "$BUILD_DIR/lambda.zip" | tr -d ' ')
echo "  package: ${ZIP_BYTES} bytes"
echo

echo "[4/6] Lambda function: $FUNCTION_NAME"
if aws lambda get-function --function-name "$FUNCTION_NAME" --region "$AWS_REGION" >/dev/null 2>&1; then
  echo "  function exists — updating code + config"
  aws lambda update-function-code \
    --function-name "$FUNCTION_NAME" \
    --zip-file "fileb://$BUILD_DIR/lambda.zip" \
    --region "$AWS_REGION" \
    --query 'LastModified' --output text >/dev/null
  # Wait for the update to complete before trying to update configuration
  aws lambda wait function-updated --function-name "$FUNCTION_NAME" --region "$AWS_REGION"
  aws lambda update-function-configuration \
    --function-name "$FUNCTION_NAME" \
    --role "$ROLE_ARN" \
    --handler lambda_handler.lambda_handler \
    --runtime python3.12 \
    --timeout 30 \
    --environment "Variables={AGENT_SPACE_ID=$AGENT_SPACE_ID,ASSOCIATION_ID=$ASSOCIATION_ID}" \
    --region "$AWS_REGION" \
    --query 'LastModified' --output text >/dev/null
else
  aws lambda create-function \
    --function-name "$FUNCTION_NAME" \
    --runtime python3.12 \
    --role "$ROLE_ARN" \
    --handler lambda_handler.lambda_handler \
    --timeout 30 \
    --memory-size 256 \
    --description "Routes CloudWatch alarm transitions to the operations backlog" \
    --environment "Variables={AGENT_SPACE_ID=$AGENT_SPACE_ID,ASSOCIATION_ID=$ASSOCIATION_ID}" \
    --zip-file "fileb://$BUILD_DIR/lambda.zip" \
    --region "$AWS_REGION" \
    --query 'FunctionArn' --output text >/dev/null
fi
FUNCTION_ARN="arn:aws:lambda:${AWS_REGION}:${ACCOUNT_ID}:function:${FUNCTION_NAME}"
echo "  $FUNCTION_ARN"
echo

# ── 5. SNS → Lambda subscription + invoke permission ────────────────────────
echo "[5/6] Wire SNS → Lambda"
# Permission for SNS to invoke this Lambda
aws lambda add-permission \
  --function-name "$FUNCTION_NAME" \
  --statement-id "AllowSNS-$TOPIC_NAME" \
  --action lambda:InvokeFunction \
  --principal sns.amazonaws.com \
  --source-arn "$TOPIC_ARN" \
  --region "$AWS_REGION" >/dev/null 2>&1 && echo "  invoke permission added" \
  || echo "  invoke permission exists"

# Subscription (idempotent — list first, create if missing)
EXISTING_SUB=$(aws sns list-subscriptions-by-topic \
  --topic-arn "$TOPIC_ARN" \
  --region "$AWS_REGION" \
  --query "Subscriptions[?Endpoint=='$FUNCTION_ARN'].SubscriptionArn | [0]" \
  --output text)
if [ "$EXISTING_SUB" = "None" ] || [ -z "$EXISTING_SUB" ]; then
  aws sns subscribe \
    --topic-arn "$TOPIC_ARN" \
    --protocol lambda \
    --notification-endpoint "$FUNCTION_ARN" \
    --region "$AWS_REGION" \
    --query 'SubscriptionArn' --output text >/dev/null
  echo "  subscription created"
else
  echo "  subscription exists: $EXISTING_SUB"
fi
echo

# ── 6. CloudWatch alarm ──────────────────────────────────────────────────────
echo "[6/6] CloudWatch alarm: $ALARM_NAME"
aws cloudwatch put-metric-alarm \
  --alarm-name "$ALARM_NAME" \
  --alarm-description "Weather forecast Lambda average response time SLO. Baseline is 100-150ms; alarm trips if the 1-minute average exceeds ${DURATION_THRESHOLD_MS}ms for 2 consecutive periods, indicating a performance degradation that will cascade into travel-planning request latency." \
  --metric-name Duration \
  --namespace AWS/Lambda \
  --statistic Average \
  --dimensions "Name=FunctionName,Value=$TARGET_FUNCTION" \
  --period 60 \
  --evaluation-periods 2 \
  --threshold "$DURATION_THRESHOLD_MS" \
  --comparison-operator GreaterThanThreshold \
  --treat-missing-data notBreaching \
  --alarm-actions "$TOPIC_ARN" \
  --ok-actions "$TOPIC_ARN" \
  --region "$AWS_REGION"
echo "  alarm configured"
echo

# ── Cleanup build artifacts ──────────────────────────────────────────────────
rm -rf "$BUILD_DIR"

echo "═══════════════════════════════════════════════════════════"
echo " Deployed."
echo
echo " Role:      $ROLE_ARN"
echo " Lambda:    $FUNCTION_ARN"
echo " SNS:       $TOPIC_ARN"
echo " Alarm:     $ALARM_NAME  (threshold ${DURATION_THRESHOLD_MS}ms Avg, 2x60s)"
echo
echo " To trigger an investigation end-to-end, elevate the weather"
echo " provider's response time (e.g., via scripts/scenario.sh B on)."
echo " The alarm will transition to ALARM within ~2 minutes; a backlog"
echo " task should appear in the DevOps Agent space shortly after."
echo "═══════════════════════════════════════════════════════════"
