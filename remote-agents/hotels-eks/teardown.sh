#!/usr/bin/env bash
# Full teardown: deletes the EKS cluster + supporting IAM. Idempotent.

set -euo pipefail

AWS_PROFILE="${AWS_PROFILE:-omni-demo}"
AWS_REGION="${AWS_REGION:-us-east-1}"
ACCOUNT_ID="$(aws sts get-caller-identity --profile "$AWS_PROFILE" --region "$AWS_REGION" --query Account --output text)"
CLUSTER_NAME="travel-concierge"
NAMESPACE="hotels"
IAM_POLICY_NAME="travel-concierge-hotels-eks-policy"
IAM_ROLE_NAME="travel-concierge-hotels-eks-role"
HOTELS_POLICY_ARN="arn:aws:iam::${ACCOUNT_ID}:policy/${IAM_POLICY_NAME}"
LBC_POLICY_ARN="arn:aws:iam::${ACCOUNT_ID}:policy/AWSLoadBalancerControllerIAMPolicy"
SSM_URL_PARAM="/travel-concierge/remote-agents/hotels-eks-url"

export AWS_PROFILE AWS_REGION

echo "═══════════════════════════════════════════════════════════"
echo "  Tearing down Hotels on EKS"
echo "  Cluster: $CLUSTER_NAME"
echo "═══════════════════════════════════════════════════════════"

echo
echo "[1/4] Unregister SSM URL so the orchestrator falls back to AgentCore hotels..."
aws ssm delete-parameter --name "$SSM_URL_PARAM" --region "$AWS_REGION" 2>/dev/null && \
  echo "  SSM parameter deleted." || echo "  (SSM parameter not present)"

echo
echo "[2/4] Deleting hotels IRSA + IAM policy..."
if aws eks describe-cluster --name "$CLUSTER_NAME" >/dev/null 2>&1; then
  aws eks update-kubeconfig --name "$CLUSTER_NAME" --region "$AWS_REGION" >/dev/null
  # Delete hotels workload cleanly first (sidecar needs to flush tunnel on exit)
  kubectl -n "$NAMESPACE" delete deployment hotels --ignore-not-found --timeout=120s || true
  eksctl delete iamserviceaccount --cluster "$CLUSTER_NAME" \
    --namespace "$NAMESPACE" --name hotels --region "$AWS_REGION" 2>/dev/null || true
  # Also delete the LBC's IRSA if present (installed by earlier deploy; harmless if left)
  eksctl delete iamserviceaccount --cluster "$CLUSTER_NAME" \
    --namespace kube-system --name aws-load-balancer-controller --region "$AWS_REGION" 2>/dev/null || true
fi

if aws iam get-policy --policy-arn "$HOTELS_POLICY_ARN" >/dev/null 2>&1; then
  for V in $(aws iam list-policy-versions --policy-arn "$HOTELS_POLICY_ARN" \
             --query 'Versions[?IsDefaultVersion==`false`].VersionId' --output text); do
    aws iam delete-policy-version --policy-arn "$HOTELS_POLICY_ARN" --version-id "$V" || true
  done
  aws iam delete-policy --policy-arn "$HOTELS_POLICY_ARN" 2>/dev/null && \
    echo "  Deleted hotels policy." || echo "  (hotels policy delete failed — may still be attached to a role)"
fi

echo
echo "[3/4] Deleting the EKS cluster (10-15 min)..."
if aws eks describe-cluster --name "$CLUSTER_NAME" >/dev/null 2>&1; then
  eksctl delete cluster --name "$CLUSTER_NAME" --region "$AWS_REGION" --wait
else
  echo "  Cluster already deleted."
fi

echo
echo "[4/4] Optional cleanup. The following are left in place because they're"
echo "  cheap/useful for a redeploy — delete manually if desired:"
echo
echo "  * AWS Load Balancer Controller IAM policy:"
echo "    aws iam delete-policy --policy-arn $LBC_POLICY_ARN"
echo
echo "  * ECR repo (holds the hotels container image — shared with the AgentCore"
echo "    hotels deployment, so DON'T delete unless you're also tearing down AgentCore):"
echo "    aws ecr delete-repository --repository-name travel-concierge-hotels --force"
echo

echo
echo "═══════════════════════════════════════════════════════════"
echo "  TEARDOWN COMPLETE"
echo "═══════════════════════════════════════════════════════════"
echo
echo "Verify nothing is left:"
echo "  aws eks list-clusters --region $AWS_REGION"
echo "  aws elbv2 describe-load-balancers --region $AWS_REGION --query 'LoadBalancers[].LoadBalancerName'"
