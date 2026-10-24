#!/usr/bin/env sh
set -eu
if (set -o pipefail) 2>/dev/null; then set -o pipefail; fi
umask 077
: "${LIGHTRAG_PROD_KUBECONFIG:?LIGHTRAG_PROD_KUBECONFIG is required}"
: "${CI_COMMIT_TAG:?CI_COMMIT_TAG is required}"
: "${LIGHTRAG_IMAGE_REF:?LIGHTRAG_IMAGE_REF is required}"

# Follow test's Kustomize apply/rollout path; provisioned backends stay untouched.
OVERLAY=k8s-deploy/lightrag-kustomize/overlays/prod/kubernetes
KUBECONFIG_FILE="$(mktemp /tmp/lightrag-prod-kubeconfig.XXXXXX)"
trap 'rm -f "$KUBECONFIG_FILE"' EXIT
printf '%s' "$LIGHTRAG_PROD_KUBECONFIG" > "$KUBECONFIG_FILE"
export KUBECONFIG="$KUBECONFIG_FILE"

kubectl --namespace prod apply -k "$OVERLAY"
kubectl --namespace prod rollout status deployment/lightrag-blue --timeout=900s
kubectl --namespace prod get deployment/lightrag-blue
kubectl --namespace prod get pods -l lightrag.io/deployment=lightrag_prod -o wide
kubectl --namespace prod get service,ingress lightrag -o wide
printf 'Production configuration release: %s; application image: %s\n' "$CI_COMMIT_TAG" "$LIGHTRAG_IMAGE_REF"
