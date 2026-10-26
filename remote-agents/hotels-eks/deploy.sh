#!/usr/bin/env bash
# One-shot deploy of the Hotels specialist to EKS Fargate.
#
# Prereqs:
#   - EKS cluster already created: `eksctl create cluster -f cluster.yaml`
#   - A dedicated IAM identity (role or user) for this one-time bootstrap
#     scoped to EXACTLY these permissions (grant-least-privilege per the
#     AWS IAM User Guide,
#     https://docs.aws.amazon.com/IAM/latest/UserGuide/best-practices.html):
#       - EKS cluster admin on the travel-concierge cluster (granted via the
#         cluster's aws-auth ConfigMap, not an IAM policy)
#       - iam:CreateRole, iam:AttachRolePolicy, iam:CreatePolicy on
#         travel-concierge-hotels-eks-role and the policy ARN this script
#         creates (see iam-policy.json)
#       - ecr:DescribeRepositories, ecr:CreateRepository,
#         ecr:GetAuthorizationToken, ecr:PushImage on
#         arn:aws:ecr:<region>:<account>:repository/travel-concierge-hotels-eks
#       - ssm:PutParameter on /travel-concierge/remote-agents/hotels-eks-url
#       - sts:GetCallerIdentity
#     The DEPLOYED HOTELS POD runs as the IRSA role
#     `travel-concierge-hotels-eks-role` (minimum policy in iam-policy.json:
#     DynamoDB GetItem on travel-concierge-hotels, Bedrock InvokeModel on the
#     Claude Sonnet 4 inference profile, X-Ray PutTraceSegments/PutSpans) —
#     the bootstrap identity above is never used at runtime.
#   - eksctl, kubectl, aws, finch (or docker) on PATH
#
# What this does, in order:
#   1. Writes a kubeconfig entry for the cluster.
#   2. Creates/updates the hotels IAM policy + IRSA role bound to the
#      `hotels/hotels` ServiceAccount (least-privilege: DDB GetItem on the
#      hotels table, Bedrock InvokeModel, X-Ray write).
#   3. Builds + pushes the hotels container image to ECR (reuses the exact
#      same source as the AgentCore version — zero code drift).
#   4. Applies the K8s manifests (namespace, SA, Deployment).
#   5. Waits for the cloudflared sidecar to publish its public tunnel URL,
#      then writes that URL to SSM so the orchestrator picks it up on its
#      next deploy.
#
# Public exposure: Cloudflare Tunnel (quick tunnel, ephemeral URL). The
# EKS ALB/NLB path in this account hits a 100.48.0.0/12 IP routing issue
# with many ISPs/corp networks (see README). The tunnel sidesteps that.
#
# NOTE: the quick tunnel URL changes every time the pod restarts. For a
# stable demo over multiple days, swap the sidecar to a named Cloudflare
# tunnel + Cloudflare API token (see README "Stable URL" section).

set -euo pipefail

ROOT="$(cd "$(dirname "$0")" && pwd)"
cd "$ROOT"

AWS_PROFILE="${AWS_PROFILE:-omni-demo}"
AWS_REGION="${AWS_REGION:-us-east-1}"
# Prefer an explicit AWS_ACCOUNT_ID env var if provided (keeps this script in
# lockstep with the rest of the repo's parameterization). Fall back to STS so
# a one-shot run with just an AWS_PROFILE still works.
if [ -n "${AWS_ACCOUNT_ID:-}" ]; then
  ACCOUNT_ID="$AWS_ACCOUNT_ID"
else
  ACCOUNT_ID="$(aws sts get-caller-identity --profile "$AWS_PROFILE" --region "$AWS_REGION" --query Account --output text)"
fi
export AWS_ACCOUNT_ID="$ACCOUNT_ID"
CLUSTER_NAME="travel-concierge"
NAMESPACE="hotels"
SA_NAME="hotels"
IAM_POLICY_NAME="travel-concierge-hotels-eks-policy"
IAM_ROLE_NAME="travel-concierge-hotels-eks-role"
ECR_REPO="travel-concierge-hotels"
IMAGE_TAG="latest"
IMAGE_URI="${ACCOUNT_ID}.dkr.ecr.${AWS_REGION}.amazonaws.com/${ECR_REPO}:${IMAGE_TAG}"
SSM_URL_PARAM="/travel-concierge/remote-agents/hotels-eks-url"

echo "═══════════════════════════════════════════════════════════"
echo "  Deploying Hotels specialist to EKS Fargate"
echo "  Cluster:   ${CLUSTER_NAME}  (${AWS_REGION})"
echo "  Account:   ${ACCOUNT_ID}"
echo "  Namespace: ${NAMESPACE}"
echo "  Image:     ${IMAGE_URI}"
echo "  Exposure:  Cloudflare quick tunnel (sidecar)"
echo "═══════════════════════════════════════════════════════════"

export AWS_PROFILE AWS_REGION

# ── 1. kubeconfig ─────────────────────────────────────────────────
echo
echo "[1/5] Updating local kubeconfig..."
aws eks update-kubeconfig --name "$CLUSTER_NAME" --region "$AWS_REGION" >/dev/null
kubectl get svc --no-headers 2>&1 | head -1

# ── 2. IRSA for the hotels pod ────────────────────────────────────
echo
echo "[2/5] Creating/updating hotels IAM policy + IRSA..."
HOTELS_POLICY_ARN="arn:aws:iam::${ACCOUNT_ID}:policy/${IAM_POLICY_NAME}"
if aws iam get-policy --policy-arn "$HOTELS_POLICY_ARN" >/dev/null 2>&1; then
  echo "  Policy exists — ensuring latest doc is attached as new version..."
  # Clean up old non-default versions (IAM caps at 5 versions per policy)
  OLD_VERSIONS=$(aws iam list-policy-versions --policy-arn "$HOTELS_POLICY_ARN" \
                 --query 'Versions[?IsDefaultVersion==`false`].VersionId' --output text)
  for V in $OLD_VERSIONS; do
    aws iam delete-policy-version --policy-arn "$HOTELS_POLICY_ARN" --version-id "$V" || true
  done
  # Render ${AWS_ACCOUNT_ID} placeholder into the policy document before upload.
  RENDERED_POLICY="$(mktemp /tmp/hotels-eks-iam-policy.XXXXXX.json)"
  sed "s|\${AWS_ACCOUNT_ID}|${ACCOUNT_ID}|g" iam-policy.json > "$RENDERED_POLICY"
  aws iam create-policy-version \
    --policy-arn "$HOTELS_POLICY_ARN" \
    --policy-document "file://$RENDERED_POLICY" \
    --set-as-default >/dev/null
  rm -f "$RENDERED_POLICY"
else
  RENDERED_POLICY="$(mktemp /tmp/hotels-eks-iam-policy.XXXXXX.json)"
  sed "s|\${AWS_ACCOUNT_ID}|${ACCOUNT_ID}|g" iam-policy.json > "$RENDERED_POLICY"
  aws iam create-policy \
    --policy-name "$IAM_POLICY_NAME" \
    --policy-document "file://$RENDERED_POLICY" \
    --description "Hotels specialist on EKS — DDB read + Bedrock + X-Ray" >/dev/null
  rm -f "$RENDERED_POLICY"
fi

kubectl create namespace "$NAMESPACE" --dry-run=client -o yaml | kubectl apply -f -

if ! eksctl get iamserviceaccount --cluster "$CLUSTER_NAME" --namespace "$NAMESPACE" --name "$SA_NAME" 2>/dev/null | grep -q "$SA_NAME"; then
  eksctl create iamserviceaccount \
    --cluster "$CLUSTER_NAME" \
    --namespace "$NAMESPACE" \
    --name "$SA_NAME" \
    --role-name "$IAM_ROLE_NAME" \
    --attach-policy-arn "$HOTELS_POLICY_ARN" \
    --override-existing-serviceaccounts \
    --approve
else
  echo "  Hotels ServiceAccount already exists; re-attaching policy."
  eksctl update iamserviceaccount \
    --cluster "$CLUSTER_NAME" \
    --namespace "$NAMESPACE" \
    --name "$SA_NAME" \
    --attach-policy-arn "$HOTELS_POLICY_ARN" \
    --approve || true
fi

# ── 3. Build + push the container ────────────────────────────────
echo
echo "[3/5] Build + push container image..."
HOTELS_SRC="$ROOT/../../agents/hotels"
if [ ! -d "$HOTELS_SRC/src" ]; then
  echo "ERROR: cannot find hotels source at $HOTELS_SRC" >&2
  exit 1
fi

aws ecr describe-repositories --repository-names "$ECR_REPO" >/dev/null 2>&1 || \
  aws ecr create-repository --repository-name "$ECR_REPO" >/dev/null

aws ecr get-login-password --region "$AWS_REGION" | \
  "${BUILDER:-finch}" login --username AWS --password-stdin "${ACCOUNT_ID}.dkr.ecr.${AWS_REGION}.amazonaws.com"

# Stage into a non-whitespace tmp dir because Finch's VM mount cannot handle
# the workspace path containing spaces + '+'. Mirrors infra/deploy.py.
python3 - <<PYSTAGE
import shutil, tempfile, subprocess, sys, os
src = "${HOTELS_SRC}"
tag = "${IMAGE_URI}"
builder = "${BUILDER:-finch}"
with tempfile.TemporaryDirectory(prefix="hotels-eks-build-") as staging:
    shutil.copy(f"{src}/Dockerfile", staging)
    shutil.copy(f"{src}/requirements.txt", staging)
    shutil.copytree(f"{src}/src", f"{staging}/src")
    print(f"staged: {src} -> {staging}", file=sys.stderr)
    subprocess.run(
        [builder, "build", "--platform", "linux/amd64", "-t", tag, "."],
        cwd=staging, check=True,
    )
subprocess.run([builder, "push", tag], check=True)
print(f"pushed {tag}", file=sys.stderr)
PYSTAGE

# ── 4. Apply K8s manifests ──────────────────────────────────────
echo
echo "[4/5] Applying K8s manifests..."
RENDERED="$(mktemp /tmp/hotels-eks-manifest.XXXXXX.yaml)"
sed "s|PLACEHOLDER_IMAGE_URI|${IMAGE_URI}|g" manifests.yaml > "$RENDERED"

kubectl apply -f "$RENDERED"
# NOTE: previous version loaded a collector-config.yaml ConfigMap for the
# ADOT Collector sidecar. The sidecar has been removed — hotels now uses
# the ADOT SDK directly to ship spans to the CloudWatch OTLP traces
# endpoint. Clean up any leftover ConfigMap from a prior deploy.
kubectl -n "$NAMESPACE" delete configmap otel-collector-config --ignore-not-found >/dev/null

ROLE_ARN="arn:aws:iam::${ACCOUNT_ID}:role/${IAM_ROLE_NAME}"
kubectl -n "$NAMESPACE" annotate sa "$SA_NAME" \
  "eks.amazonaws.com/role-arn=${ROLE_ARN}" --overwrite >/dev/null

# Restart pods to pick up any image/annotation changes + fresh tunnel URL
# + any config map changes.
kubectl -n "$NAMESPACE" rollout restart deployment/hotels
kubectl -n "$NAMESPACE" rollout status deployment/hotels --timeout=240s
rm -f "$RENDERED"

# ── 5. Extract cloudflared URL + register in SSM ────────────────
echo
echo "[5/5] Waiting for cloudflared tunnel URL..."
POD=""
for i in $(seq 1 24); do
  POD=$(kubectl -n "$NAMESPACE" get pods -l app=hotels \
    --field-selector=status.phase=Running \
    -o jsonpath='{.items[0].metadata.name}' 2>/dev/null || true)
  [ -n "$POD" ] && break
  sleep 5
done
[ -z "$POD" ] && { echo "ERROR: no Running hotels pod" >&2; exit 1; }

URL=""
for i in $(seq 1 24); do
  URL=$(kubectl -n "$NAMESPACE" logs "$POD" -c cloudflared --tail=200 2>/dev/null \
          | grep -oE 'https://[a-z0-9-]+\.trycloudflare\.com' | head -1)
  [ -n "$URL" ] && break
  sleep 5
done

if [ -z "$URL" ]; then
  echo "ERROR: did not find trycloudflare URL in cloudflared logs" >&2
  kubectl -n "$NAMESPACE" logs "$POD" -c cloudflared --tail=50 >&2
  exit 1
fi

echo "  Tunnel URL: $URL"
echo "  Writing to SSM: $SSM_URL_PARAM"
aws ssm put-parameter --name "$SSM_URL_PARAM" --value "$URL" \
  --type String --overwrite --region "$AWS_REGION" >/dev/null

echo
echo "═══════════════════════════════════════════════════════════"
echo "  DEPLOYED"
echo "═══════════════════════════════════════════════════════════"
echo
echo "  URL:   $URL"
echo "  Smoke test (from AWS, not corp network):"
echo "    curl -sS $URL/ping"
echo
echo "  Rewire orchestrator to use the EKS pod:"
echo "    AWS_PROFILE=omni-demo python3 ../../infra/deploy.py --only orchestrator"
echo
echo "  Note: the quick tunnel URL CHANGES on every pod restart. Re-run"
echo "  this script to update SSM, then redeploy the orchestrator."
echo
