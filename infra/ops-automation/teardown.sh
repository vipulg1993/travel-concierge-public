#!/usr/bin/env bash
# Reverses deploy.sh — removes the CloudWatch alarm, SNS topic, Lambda,
# and IAM role created by the ops-automation integration.

set -euo pipefail

AWS_REGION="${AWS_REGION:-us-east-1}"
ACCOUNT_ID=$(aws sts get-caller-identity --query 'Account' --output text)

ROLE_NAME="TravelConcierge-OpsAutomation-Role"
FUNCTION_NAME="tc-ops-alert-dispatcher"
TOPIC_NAME="tc-production-alerts"
ALARM_NAME="TC-WeatherProvider-Duration-SLO"
LOG_GROUP="/aws/lambda/${FUNCTION_NAME}"

echo "=== Teardown ops-automation integration ==="

echo "[1/5] Delete CloudWatch alarm"
aws cloudwatch delete-alarms --alarm-names "$ALARM_NAME" --region "$AWS_REGION" 2>/dev/null \
  && echo "  deleted" || echo "  not found"

echo "[2/5] Delete SNS topic"
TOPIC_ARN="arn:aws:sns:${AWS_REGION}:${ACCOUNT_ID}:${TOPIC_NAME}"
aws sns delete-topic --topic-arn "$TOPIC_ARN" --region "$AWS_REGION" 2>/dev/null \
  && echo "  deleted" || echo "  not found"

echo "[3/5] Delete Lambda function"
aws lambda delete-function --function-name "$FUNCTION_NAME" --region "$AWS_REGION" 2>/dev/null \
  && echo "  deleted" || echo "  not found"

echo "[4/5] Delete Lambda log group"
aws logs delete-log-group --log-group-name "$LOG_GROUP" --region "$AWS_REGION" 2>/dev/null \
  && echo "  deleted" || echo "  not found"

echo "[5/5] Delete IAM role"
# Detach managed policies, delete inline policies, then delete the role
aws iam detach-role-policy --role-name "$ROLE_NAME" \
  --policy-arn arn:aws:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole 2>/dev/null || true
aws iam delete-role-policy --role-name "$ROLE_NAME" --policy-name "DevOpsAgentBacklogWrite" 2>/dev/null || true
aws iam delete-role --role-name "$ROLE_NAME" 2>/dev/null \
  && echo "  deleted" || echo "  not found"

echo
echo "Done."
