#!/usr/bin/env sh
set -eu
if (set -o pipefail) 2>/dev/null; then
  set -o pipefail
fi

: "${CI_COMMIT_TAG:?CI_COMMIT_TAG is required}"
: "${CI_COMMIT_SHA:?CI_COMMIT_SHA is required}"
: "${LIGHTRAG_IMAGE_DIGEST:?LIGHTRAG_IMAGE_DIGEST is required}"
: "${LIGHTRAG_IMAGE_REF:?LIGHTRAG_IMAGE_REF is required}"
: "${LIGHTRAG_TEST_KUBECONFIG:?LIGHTRAG_TEST_KUBECONFIG is required}"

case "$LIGHTRAG_IMAGE_DIGEST" in
  sha256:????????????????????????????????????????????????????????????????) ;;
  *) echo "invalid image digest: $LIGHTRAG_IMAGE_DIGEST" >&2; exit 1 ;;
esac

DEPLOYMENT="${LIGHTRAG_TEST_DEPLOYMENT:-lightrag}"
SERVICE="${LIGHTRAG_TEST_SERVICE:-lightrag}"
CONTAINER="${LIGHTRAG_TEST_CONTAINER:-lightrag}"
OVERLAY="${LIGHTRAG_KUSTOMIZE_OVERLAY:-k8s-deploy/lightrag-kustomize/overlays/test}"
BASE_NETWORK_POLICY="${LIGHTRAG_BASE_NETWORK_POLICY:-k8s-deploy/lightrag-kustomize/base/networkpolicy.yaml}"
LIGHTRAG_TEST_INSTANCES="${LIGHTRAG_TEST_INSTANCES:-01,02,03,04,05}"
LIGHTRAG_TEST_DOMAIN="${LIGHTRAG_TEST_DOMAIN:-f123.pub}"
LIGHTRAG_TEST_INGRESS_CLASS="${LIGHTRAG_TEST_INGRESS_CLASS:-nginx}"
LIGHTRAG_TEST_PUBLIC_SCHEME="${LIGHTRAG_TEST_PUBLIC_SCHEME:-http}"
LABEL_SELECTOR="app.kubernetes.io/name=lightrag,app.kubernetes.io/instance=lightrag"
KUBECONFIG_FILE="${KUBECONFIG_FILE:-/tmp/lightrag-test-kubeconfig}"
ROUTE_PAUSE_PATCH='{"spec":{"selector":{"lightrag.openai.com/routing-paused":"true"}}}'
ROUTE_RESTORE_PATCH='{"spec":{"selector":{"app.kubernetes.io/name":"lightrag","app.kubernetes.io/instance":"lightrag","lightrag.openai.com/routing-paused":null}}}'
INSTANCE_ID=""
NAMESPACE=""
LIGHTRAG_INSTANCE_WORKSPACE=""
LIGHTRAG_INSTANCE_POSTGRES_WORKSPACE=""
LIGHTRAG_INSTANCE_DEPLOYMENT_ID=""
LIGHTRAG_INSTANCE_S3_OBJECT_PREFIX=""
LIGHTRAG_INSTANCE_API_KEY=""
LIGHTRAG_TEST_PUBLIC_HOST=""
LIGHTRAG_TEST_PUBLIC_BASE_URL=""
INSTANCE_RELEASE_DIR=""

require_env() {
  name="$1"
  eval "value=\${$name:-}"
  if [ -z "$value" ]; then
    echo "required environment variable is not set: $name" >&2
    exit 1
  fi
}

validate_instance_id() {
  case "$1" in
    [0-9][0-9]) ;;
    *)
      echo "invalid LightRAG test instance id: $1" >&2
      exit 1
      ;;
  esac
}

configure_instance() {
  INSTANCE_ID="$1"
  validate_instance_id "$INSTANCE_ID"
  NAMESPACE="lightrag-test-$INSTANCE_ID"
  LIGHTRAG_INSTANCE_WORKSPACE="lightrag_test_$INSTANCE_ID"
  LIGHTRAG_INSTANCE_POSTGRES_WORKSPACE="$LIGHTRAG_INSTANCE_WORKSPACE"
  LIGHTRAG_INSTANCE_DEPLOYMENT_ID="$LIGHTRAG_INSTANCE_WORKSPACE"
  LIGHTRAG_INSTANCE_S3_OBJECT_PREFIX="lightrag/test-$INSTANCE_ID/object-ingestion"
  LIGHTRAG_TEST_PUBLIC_HOST="rag-test-$INSTANCE_ID.$LIGHTRAG_TEST_DOMAIN"
  public_host_var="LIGHTRAG_TEST_${INSTANCE_ID}_PUBLIC_HOST"
  public_base_url_var="LIGHTRAG_TEST_${INSTANCE_ID}_PUBLIC_BASE_URL"
  api_key_var="LIGHTRAG_TEST_${INSTANCE_ID}_API_KEY"
  eval "configured_host=\${$public_host_var:-}"
  eval "configured_base_url=\${$public_base_url_var:-}"
  eval "LIGHTRAG_INSTANCE_API_KEY=\${$api_key_var:-}"
  if [ -n "$configured_host" ]; then
    LIGHTRAG_TEST_PUBLIC_HOST="$configured_host"
  fi
  LIGHTRAG_TEST_PUBLIC_BASE_URL="${LIGHTRAG_TEST_PUBLIC_SCHEME}://$LIGHTRAG_TEST_PUBLIC_HOST"
  if [ -n "$configured_base_url" ]; then
    LIGHTRAG_TEST_PUBLIC_BASE_URL="$configured_base_url"
  fi
  if [ -z "$LIGHTRAG_INSTANCE_API_KEY" ]; then
    echo "required environment variable is not set: $api_key_var" >&2
    exit 1
  fi
  echo "Configured LightRAG test instance $INSTANCE_ID: namespace=$NAMESPACE workspace=$LIGHTRAG_INSTANCE_WORKSPACE host=$LIGHTRAG_TEST_PUBLIC_HOST"
}

instance_list() {
  printf '%s\n' "$LIGHTRAG_TEST_INSTANCES" | tr ',;' '  '
}

show_kubectl_status() {
  label="$1"
  shift

  echo "${label}:"
  if ! "$@"; then
    echo "Unable to read ${label}; keeping deployment command status unchanged." >&2
  fi
}

show_job_failure_context() {
  job="$1"
  component="$2"
  label="$3"
  reason="$4"

  echo "${label} ${reason}; recent non-secret logs follow:" >&2
  kubectl -n "$NAMESPACE" logs "job/$job" --tail=120 >&2 || true
  kubectl -n "$NAMESPACE" get pods \
    -l "app.kubernetes.io/component=$component" \
    -o wide >&2 || true
}

wait_for_job_terminal() {
  job="$1"
  timeout_seconds="$2"
  component="$3"
  label="$4"
  deadline="$(($(date +%s) + timeout_seconds))"

  while :; do
    complete_status="$(kubectl -n "$NAMESPACE" get "job/$job" -o jsonpath='{.status.conditions[?(@.type=="Complete")].status}' 2>/dev/null || true)"
    failed_status="$(kubectl -n "$NAMESPACE" get "job/$job" -o jsonpath='{.status.conditions[?(@.type=="Failed")].status}' 2>/dev/null || true)"
    if [ "$complete_status" = "True" ]; then
      echo "$job completed"
      return 0
    fi
    if [ "$failed_status" = "True" ]; then
      show_job_failure_context "$job" "$component" "$label" "failed"
      return 1
    fi
    if [ "$(date +%s)" -ge "$deadline" ]; then
      show_job_failure_context "$job" "$component" "$label" "timed out after ${timeout_seconds}s"
      return 1
    fi
    sleep 5
  done
}

ensure_namespace() {
  kubectl create namespace "$NAMESPACE" --dry-run=client -o yaml | kubectl apply -f -
}

apply_registry_pull_secret() {
  require_env DOCKER_USERNAME
  require_env DOCKER_PASSWORD

  kubectl -n "$NAMESPACE" create secret docker-registry lightrag-registry-pull \
    --docker-server=docker-hub.f123.pub \
    --docker-username="$DOCKER_USERNAME" \
    --docker-password="$DOCKER_PASSWORD" \
    --dry-run=client -o yaml | kubectl apply -f -
}

make_coordination_dsn() {
  python3 - \
    "$LIGHTRAG_TEST_POSTGRES_USER" \
    "$LIGHTRAG_TEST_POSTGRES_PASSWORD" \
    "$LIGHTRAG_TEST_POSTGRES_HOST" \
    "$LIGHTRAG_TEST_POSTGRES_PORT" \
    "$LIGHTRAG_TEST_POSTGRES_DATABASE" <<'PY'
from urllib.parse import quote
import sys

user, password, host, port, database = sys.argv[1:]
if ":" in host and not host.startswith("["):
    host = f"[{host}]"
print(
    "postgresql://"
    + quote(user, safe="")
    + ":"
    + quote(password, safe="")
    + "@"
    + host
    + ":"
    + port
    + "/"
    + quote(database, safe="")
)
PY
}

normalize_http_url_secret() {
  name="$1"
  default_path="${LIGHTRAG_TEST_OPENAI_COMPATIBLE_PATH:-/compatible-mode/v1}"
  case "$default_path" in
    /*) ;;
    *) default_path="/$default_path" ;;
  esac
  eval "value=\${$name:-}"
  case "$value" in
    http://* | https://*)
      url="$value"
      ;;
    *://*)
      echo "required environment variable must be an http(s) URL: $name" >&2
      return 64
      ;;
    *)
      url="https://$value"
      ;;
  esac

  canonical_url="$url"
  while [ "${canonical_url%/}" != "$canonical_url" ]; do
    canonical_url="${canonical_url%/}"
  done
  case "$canonical_url" in
    http://*/* | https://*/*)
      printf '%s\n' "$url"
      ;;
    *)
      printf '%s%s\n' "$canonical_url" "$default_path"
      ;;
  esac
}

storage_profile_env_yaml() {
  cat <<YAML
          env:
            - name: HOST
              value: 0.0.0.0
            - name: PORT
              value: "9621"
            - name: LIGHTRAG_DISTRIBUTED_WRITES
              value: "true"
            - name: LIGHTRAG_SHARED_STORAGE
              value: "false"
            - name: LIGHTRAG_OBJECT_STORAGE
              value: s3
            - name: ENABLE_LOCAL_FILE_INGESTION
              value: "false"
            - name: LIGHTRAG_DEPLOYMENT_ID
              value: ${LIGHTRAG_INSTANCE_DEPLOYMENT_ID}
            - name: LIGHTRAG_COORDINATION_POOL_MODE
              value: direct
            - name: LIGHTRAG_DISTRIBUTED_POLL_INTERVAL
              value: "1"
            - name: WORKSPACE
              value: ${LIGHTRAG_INSTANCE_WORKSPACE}
            - name: POSTGRES_WORKSPACE
              value: ${LIGHTRAG_INSTANCE_POSTGRES_WORKSPACE}
            - name: INPUT_DIR
              value: /app/data/inputs
            - name: WORKING_DIR
              value: /app/data/rag_storage
            - name: S3_OBJECT_PREFIX
              value: ${LIGHTRAG_INSTANCE_S3_OBJECT_PREFIX}
            - name: S3_FORCE_PATH_STYLE
              value: "true"
            - name: S3_PRESIGN_TTL_SECONDS
              value: "900"
            - name: S3_UPLOAD_SESSION_TTL_SECONDS
              value: "3600"
            - name: S3_SCRATCH_DIR
              value: /app/data/object-scratch
            - name: WORKERS
              value: "1"
            - name: LIGHTRAG_KV_STORAGE
              value: PGKVStorage
            - name: LIGHTRAG_DOC_STATUS_STORAGE
              value: PGDocStatusStorage
            - name: LIGHTRAG_VECTOR_STORAGE
              value: PGVectorStorage
            - name: LIGHTRAG_GRAPH_STORAGE
              value: HugeGraphStorage
            - name: HUGEGRAPH_AUTO_CREATE_SCHEMA
              value: "true"
            - name: LLM_BINDING
              value: openai
            - name: LLM_MODEL
              value: qwen-plus
            - name: EMBEDDING_BINDING
              value: openai
            - name: EMBEDDING_MODEL
              value: text-embedding-v4
            - name: EMBEDDING_DIM
              value: "1024"
YAML
}

apply_runtime_secret() {
  require_env LIGHTRAG_INSTANCE_API_KEY
  require_env LIGHTRAG_TEST_LLM_API_KEY
  require_env LIGHTRAG_TEST_EMBEDDING_API_KEY
  require_env LIGHTRAG_TEST_LLM_BINDING_HOST
  require_env LIGHTRAG_TEST_EMBEDDING_BINDING_HOST
  require_env LIGHTRAG_TEST_POSTGRES_HOST
  require_env LIGHTRAG_TEST_POSTGRES_PORT
  require_env LIGHTRAG_TEST_POSTGRES_USER
  require_env LIGHTRAG_TEST_POSTGRES_DATABASE
  require_env LIGHTRAG_TEST_POSTGRES_PASSWORD
  require_env LIGHTRAG_TEST_HUGEGRAPH_URI
  require_env LIGHTRAG_TEST_HUGEGRAPH_GREMLIN
  require_env LIGHTRAG_TEST_HUGEGRAPH_GRAPH
  require_env LIGHTRAG_TEST_HUGEGRAPH_GRAPHSPACE
  require_env LIGHTRAG_TEST_HUGEGRAPH_USERNAME
  require_env LIGHTRAG_TEST_HUGEGRAPH_PASSWORD
  require_env LIGHTRAG_TEST_S3_ENDPOINT_URL
  require_env LIGHTRAG_TEST_S3_BUCKET
  require_env LIGHTRAG_TEST_S3_ACCESS_KEY_ID
  require_env LIGHTRAG_TEST_S3_SECRET_ACCESS_KEY

  LIGHTRAG_COORDINATION_DSN="$(make_coordination_dsn)"
  LIGHTRAG_TEST_LLM_BINDING_HOST="$(normalize_http_url_secret LIGHTRAG_TEST_LLM_BINDING_HOST)"
  LIGHTRAG_TEST_EMBEDDING_BINDING_HOST="$(normalize_http_url_secret LIGHTRAG_TEST_EMBEDDING_BINDING_HOST)"
  HUGEGRAPH_AUTH_METHOD="${LIGHTRAG_TEST_HUGEGRAPH_AUTH_METHOD:-basic}"

  kubectl -n "$NAMESPACE" create secret generic lightrag-runtime \
    --from-literal=LIGHTRAG_API_KEY="$LIGHTRAG_INSTANCE_API_KEY" \
    --from-literal=LIGHTRAG_COORDINATION_DSN="$LIGHTRAG_COORDINATION_DSN" \
    --from-literal=LLM_BINDING_API_KEY="$LIGHTRAG_TEST_LLM_API_KEY" \
    --from-literal=EMBEDDING_BINDING_API_KEY="$LIGHTRAG_TEST_EMBEDDING_API_KEY" \
    --from-literal=DASHSCOPE_API_KEY="$LIGHTRAG_TEST_LLM_API_KEY" \
    --from-literal=DASHSCOPE_WORKSPACE_ID="${LIGHTRAG_TEST_DASHSCOPE_WORKSPACE_ID:-}" \
    --from-literal=DASHSCOPE_REGION="${LIGHTRAG_TEST_BAILIAN_REGION:-}" \
    --from-literal=LLM_BINDING_HOST="$LIGHTRAG_TEST_LLM_BINDING_HOST" \
    --from-literal=EMBEDDING_BINDING_HOST="$LIGHTRAG_TEST_EMBEDDING_BINDING_HOST" \
    --from-literal=POSTGRES_HOST="$LIGHTRAG_TEST_POSTGRES_HOST" \
    --from-literal=POSTGRES_PORT="$LIGHTRAG_TEST_POSTGRES_PORT" \
    --from-literal=POSTGRES_USER="$LIGHTRAG_TEST_POSTGRES_USER" \
    --from-literal=POSTGRES_DATABASE="$LIGHTRAG_TEST_POSTGRES_DATABASE" \
    --from-literal=POSTGRES_PASSWORD="$LIGHTRAG_TEST_POSTGRES_PASSWORD" \
    --from-literal=HUGEGRAPH_URI="$LIGHTRAG_TEST_HUGEGRAPH_URI" \
    --from-literal=HUGEGRAPH_GREMLIN="$LIGHTRAG_TEST_HUGEGRAPH_GREMLIN" \
    --from-literal=HUGEGRAPH_GRAPH="$LIGHTRAG_TEST_HUGEGRAPH_GRAPH" \
    --from-literal=HUGEGRAPH_GRAPHSPACE="$LIGHTRAG_TEST_HUGEGRAPH_GRAPHSPACE" \
    --from-literal=HUGEGRAPH_USERNAME="$LIGHTRAG_TEST_HUGEGRAPH_USERNAME" \
    --from-literal=HUGEGRAPH_PASSWORD="$LIGHTRAG_TEST_HUGEGRAPH_PASSWORD" \
    --from-literal=HUGEGRAPH_AUTH_METHOD="$HUGEGRAPH_AUTH_METHOD" \
    --from-literal=S3_ENDPOINT_URL="$LIGHTRAG_TEST_S3_ENDPOINT_URL" \
    --from-literal=S3_BUCKET="$LIGHTRAG_TEST_S3_BUCKET" \
    --from-literal=S3_REGION="${LIGHTRAG_TEST_S3_REGION:-}" \
    --from-literal=S3_ACCESS_KEY_ID="$LIGHTRAG_TEST_S3_ACCESS_KEY_ID" \
    --from-literal=S3_SECRET_ACCESS_KEY="$LIGHTRAG_TEST_S3_SECRET_ACCESS_KEY" \
    --from-literal=S3_SESSION_TOKEN="${LIGHTRAG_TEST_S3_SESSION_TOKEN:-}" \
    --dry-run=client -o yaml | kubectl apply -f -
}

preflight_storage_profile() {
  echo "Preflighting LightRAG test storage profile with verified image ${LIGHTRAG_IMAGE_DIGEST}"
  kubectl -n "$NAMESPACE" delete job lightrag-storage-preflight --ignore-not-found --wait=true
  {
    cat <<YAML
apiVersion: batch/v1
kind: Job
metadata:
  name: lightrag-storage-preflight
  labels:
    app.kubernetes.io/name: lightrag
    app.kubernetes.io/instance: lightrag
    app.kubernetes.io/component: storage-preflight
spec:
  backoffLimit: 0
  activeDeadlineSeconds: 900
  template:
    metadata:
      labels:
        app.kubernetes.io/name: lightrag
        app.kubernetes.io/instance: lightrag
        app.kubernetes.io/component: storage-preflight
    spec:
      restartPolicy: Never
      imagePullSecrets:
        - name: lightrag-registry-pull
      securityContext:
        runAsNonRoot: true
        runAsUser: 1000
        runAsGroup: 1000
        fsGroup: 1000
        fsGroupChangePolicy: OnRootMismatch
      containers:
        - name: preflight
          image: ${LIGHTRAG_IMAGE_REF}
          imagePullPolicy: IfNotPresent
          command: ["sh", "-c", "python -m lightrag.distributed preflight"]
          envFrom:
            - secretRef:
                name: lightrag-runtime
YAML
    storage_profile_env_yaml
    cat <<'YAML'
          securityContext:
            allowPrivilegeEscalation: false
            capabilities:
              drop: ["ALL"]
YAML
  } | kubectl -n "$NAMESPACE" apply -f -

  if ! wait_for_job_terminal lightrag-storage-preflight 600 storage-preflight "Storage preflight"; then
    exit 1
  fi

  kubectl -n "$NAMESPACE" logs job/lightrag-storage-preflight --tail=120 || true
  kubectl -n "$NAMESPACE" delete job lightrag-storage-preflight --wait=true
}

migrate_coordination_schema() {
  echo "Migrating LightRAG coordination schema with verified image ${LIGHTRAG_IMAGE_DIGEST}"
  kubectl -n "$NAMESPACE" delete job lightrag-coordination-migrate --ignore-not-found --wait=true
  cat <<YAML | kubectl -n "$NAMESPACE" apply -f -
apiVersion: batch/v1
kind: Job
metadata:
  name: lightrag-coordination-migrate
  labels:
    app.kubernetes.io/name: lightrag
    app.kubernetes.io/instance: lightrag
    app.kubernetes.io/component: coordination-migrate
spec:
  backoffLimit: 0
  activeDeadlineSeconds: 900
  template:
    metadata:
      labels:
        app.kubernetes.io/name: lightrag
        app.kubernetes.io/instance: lightrag
        app.kubernetes.io/component: coordination-migrate
    spec:
      restartPolicy: Never
      imagePullSecrets:
        - name: lightrag-registry-pull
      securityContext:
        runAsNonRoot: true
        runAsUser: 1000
        runAsGroup: 1000
        fsGroup: 1000
        fsGroupChangePolicy: OnRootMismatch
      containers:
        - name: migrate
          image: ${LIGHTRAG_IMAGE_REF}
          imagePullPolicy: IfNotPresent
          command: ["sh", "-c", "python -m lightrag.distributed migrate"]
          envFrom:
            - secretRef:
                name: lightrag-runtime
          securityContext:
            allowPrivilegeEscalation: false
            capabilities:
              drop: ["ALL"]
YAML

  if ! wait_for_job_terminal lightrag-coordination-migrate 600 coordination-migrate "Coordination migration"; then
    exit 1
  fi

  kubectl -n "$NAMESPACE" logs job/lightrag-coordination-migrate --tail=120 || true
  kubectl -n "$NAMESPACE" delete job lightrag-coordination-migrate --wait=true
}

bootstrap_storage_profile() {
  echo "Bootstrapping LightRAG test storage profile with verified image ${LIGHTRAG_IMAGE_DIGEST}"
  kubectl -n "$NAMESPACE" delete job lightrag-storage-bootstrap --ignore-not-found --wait=true
  cat <<YAML | kubectl -n "$NAMESPACE" apply -f -
apiVersion: batch/v1
kind: Job
metadata:
  name: lightrag-storage-bootstrap
  labels:
    app.kubernetes.io/name: lightrag
    app.kubernetes.io/instance: lightrag
    app.kubernetes.io/component: storage-bootstrap
spec:
  backoffLimit: 0
  activeDeadlineSeconds: 900
  template:
    metadata:
      labels:
        app.kubernetes.io/name: lightrag
        app.kubernetes.io/instance: lightrag
        app.kubernetes.io/component: storage-bootstrap
    spec:
      restartPolicy: Never
      imagePullSecrets:
        - name: lightrag-registry-pull
      securityContext:
        runAsNonRoot: true
        runAsUser: 1000
        runAsGroup: 1000
        fsGroup: 1000
        fsGroupChangePolicy: OnRootMismatch
      containers:
        - name: bootstrap
          image: ${LIGHTRAG_IMAGE_REF}
          imagePullPolicy: IfNotPresent
          command:
            - sh
            - -c
            - python -m lightrag.distributed bootstrap --actor woodpecker --confirm-writers-stopped --confirm-inflight-finished
          envFrom:
            - secretRef:
                name: lightrag-runtime
$(storage_profile_env_yaml)
          securityContext:
            allowPrivilegeEscalation: false
            capabilities:
              drop: ["ALL"]
YAML

  if ! wait_for_job_terminal lightrag-storage-bootstrap 600 storage-bootstrap "Storage bootstrap"; then
    exit 1
  fi

  kubectl -n "$NAMESPACE" logs job/lightrag-storage-bootstrap --tail=120 || true
  kubectl -n "$NAMESPACE" delete job lightrag-storage-bootstrap --wait=true
}

apply_environment_snapshot() {
  cat > "$INSTANCE_RELEASE_DIR/environment-snapshot.json" <<JSON
{"cluster":"test","namespace":"$NAMESPACE","instance":"$INSTANCE_ID","initialized":true,"profile":{"replicas":2,"workers":1,"kv_storage":"PGKVStorage","doc_status_storage":"PGDocStatusStorage","vector_storage":"PGVectorStorage","graph_storage":"HugeGraphStorage","workspace":"$LIGHTRAG_INSTANCE_WORKSPACE","postgres_workspace":"$LIGHTRAG_INSTANCE_POSTGRES_WORKSPACE","object_storage":"s3","s3_object_prefix":"$LIGHTRAG_INSTANCE_S3_OBJECT_PREFIX","shared_filesystem_required":false},"storage_state":{"fenced":false,"active_operations":0,"pending_mutations":0,"orphaned_claims":0}}
JSON
  kubectl -n "$NAMESPACE" create configmap lightrag-test-environment \
    --from-file=snapshot="$INSTANCE_RELEASE_DIR/environment-snapshot.json" \
    --dry-run=client -o yaml | kubectl apply -f -
}

wait_for_pvc_bound() {
  pvc="$1"
  kubectl -n "$NAMESPACE" wait \
    --for=jsonpath='{.status.phase}'=Bound \
    "pvc/$pvc" \
    --timeout=600s
}

validate_public_entrypoint() {
  case "$LIGHTRAG_TEST_PUBLIC_HOST" in
    ""|*"://"*|*/*|*" "*)
      echo "invalid LIGHTRAG_TEST_PUBLIC_HOST: $LIGHTRAG_TEST_PUBLIC_HOST" >&2
      exit 1
      ;;
  esac
  case "$LIGHTRAG_TEST_INGRESS_CLASS" in
    ""|*"://"*|*/*|*" "*)
      echo "invalid LIGHTRAG_TEST_INGRESS_CLASS: $LIGHTRAG_TEST_INGRESS_CLASS" >&2
      exit 1
      ;;
  esac
  case "$LIGHTRAG_TEST_PUBLIC_BASE_URL" in
    http://*|https://*) ;;
    *)
      echo "invalid LIGHTRAG_TEST_PUBLIC_BASE_URL: $LIGHTRAG_TEST_PUBLIC_BASE_URL" >&2
      exit 1
      ;;
  esac
}

apply_public_entrypoint_config() {
  validate_public_entrypoint
  cat > "$OVERLAY/public-entrypoint.yaml" <<YAML
apiVersion: v1
kind: ConfigMap
metadata:
  name: lightrag-test-public-entrypoint
data:
  host: $LIGHTRAG_TEST_PUBLIC_HOST
  ingressClassName: $LIGHTRAG_TEST_INGRESS_CLASS
YAML
}

write_instance_kustomize_overlay() {
  cat > "$OVERLAY/release-image.yaml" <<YAML
apiVersion: v1
kind: ConfigMap
metadata:
  name: lightrag-release-image
data:
  digest: $LIGHTRAG_IMAGE_DIGEST
YAML
  cat > "$OVERLAY/workspace-profile.yaml" <<YAML
apiVersion: v1
kind: ConfigMap
metadata:
  name: lightrag-test-workspace-profile
data:
  deploymentId: $LIGHTRAG_INSTANCE_DEPLOYMENT_ID
  workspace: $LIGHTRAG_INSTANCE_WORKSPACE
  postgresWorkspace: $LIGHTRAG_INSTANCE_POSTGRES_WORKSPACE
  s3ObjectPrefix: $LIGHTRAG_INSTANCE_S3_OBJECT_PREFIX
YAML
  cat > "$OVERLAY/deployer-rbac.yaml" <<YAML
apiVersion: v1
kind: Namespace
metadata:
  name: $NAMESPACE
---
apiVersion: v1
kind: ServiceAccount
metadata:
  name: lightrag-test-deployer
---
apiVersion: rbac.authorization.k8s.io/v1
kind: Role
metadata:
  name: lightrag-test-deployer
rules:
  - apiGroups: [""]
    resources: ["pods", "pods/log", "services", "endpoints", "persistentvolumeclaims", "configmaps", "secrets"]
    verbs: ["get", "list", "watch", "create", "update", "patch"]
  - apiGroups: [""]
    resources: ["pods/exec"]
    verbs: ["create"]
  - apiGroups: ["apps"]
    resources: ["deployments", "replicasets"]
    verbs: ["get", "list", "watch", "create", "update", "patch"]
  - apiGroups: ["networking.k8s.io"]
    resources: ["ingresses"]
    verbs: ["get", "list", "watch", "create", "update", "patch"]
  - apiGroups: ["batch"]
    resources: ["jobs"]
    verbs: ["get", "list", "watch", "create", "update", "patch", "delete"]
  - apiGroups: ["coordination.k8s.io"]
    resources: ["leases"]
    verbs: ["get", "list", "watch", "create", "update", "patch"]
---
apiVersion: rbac.authorization.k8s.io/v1
kind: RoleBinding
metadata:
  name: lightrag-test-deployer
subjects:
  - kind: ServiceAccount
    name: lightrag-test-deployer
roleRef:
  apiGroup: rbac.authorization.k8s.io
  kind: Role
  name: lightrag-test-deployer
YAML
  cat > "$OVERLAY/kustomization.yaml" <<YAML
apiVersion: kustomize.config.k8s.io/v1beta1
kind: Kustomization
namespace: $NAMESPACE
resources:
  - ../../base
  - release-image.yaml
  - public-entrypoint.yaml
  - workspace-profile.yaml
  - deployer-rbac.yaml
replacements:
  - source:
      kind: ConfigMap
      name: lightrag-release-image
      fieldPath: data.digest
    targets:
      - select:
          kind: Deployment
          name: lightrag
        fieldPaths:
          - spec.template.spec.containers.[name=lightrag].image
        options:
          delimiter: "@"
          index: 1
  - source:
      kind: ConfigMap
      name: lightrag-test-public-entrypoint
      fieldPath: data.host
    targets:
      - select:
          kind: Ingress
          name: lightrag
        fieldPaths:
          - spec.rules.0.host
  - source:
      kind: ConfigMap
      name: lightrag-test-public-entrypoint
      fieldPath: data.ingressClassName
    targets:
      - select:
          kind: Ingress
          name: lightrag
        fieldPaths:
          - spec.ingressClassName
  - source:
      kind: ConfigMap
      name: lightrag-test-workspace-profile
      fieldPath: data.deploymentId
    targets:
      - select:
          kind: Deployment
          name: lightrag
        fieldPaths:
          - spec.template.spec.containers.[name=lightrag].env.[name=LIGHTRAG_DEPLOYMENT_ID].value
  - source:
      kind: ConfigMap
      name: lightrag-test-workspace-profile
      fieldPath: data.workspace
    targets:
      - select:
          kind: Deployment
          name: lightrag
        fieldPaths:
          - spec.template.spec.containers.[name=lightrag].env.[name=WORKSPACE].value
  - source:
      kind: ConfigMap
      name: lightrag-test-workspace-profile
      fieldPath: data.postgresWorkspace
    targets:
      - select:
          kind: Deployment
          name: lightrag
        fieldPaths:
          - spec.template.spec.containers.[name=lightrag].env.[name=POSTGRES_WORKSPACE].value
  - source:
      kind: ConfigMap
      name: lightrag-test-workspace-profile
      fieldPath: data.s3ObjectPrefix
    targets:
      - select:
          kind: Deployment
          name: lightrag
        fieldPaths:
          - spec.template.spec.containers.[name=lightrag].env.[name=S3_OBJECT_PREFIX].value
YAML
}

verify_public_ingress() {
  expected_host="$LIGHTRAG_TEST_PUBLIC_HOST"
  expected_class="$LIGHTRAG_TEST_INGRESS_CLASS"
  base_url="${LIGHTRAG_TEST_PUBLIC_BASE_URL%/}"

  kubectl -n "$NAMESPACE" get ingress "$SERVICE" -o json > "$INSTANCE_RELEASE_DIR/ingress.json"
  actual_host="$(kubectl -n "$NAMESPACE" get ingress "$SERVICE" -o jsonpath='{.spec.rules[0].host}')"
  actual_class="$(kubectl -n "$NAMESPACE" get ingress "$SERVICE" -o jsonpath='{.spec.ingressClassName}')"
  if [ "$actual_host" != "$expected_host" ]; then
    echo "ingress host mismatch: expected $expected_host got $actual_host" >&2
    exit 1
  fi
  if [ "$actual_class" != "$expected_class" ]; then
    echo "ingress class mismatch: expected $expected_class got $actual_class" >&2
    exit 1
  fi

  echo "Verifying public LightRAG test health at $base_url"
  python3 - "$base_url" <<'PY'
import sys
import time
import urllib.error
import urllib.request

base_url = sys.argv[1].rstrip("/")


def status_for(path: str) -> int:
    request = urllib.request.Request(base_url + path, method="GET")
    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            response.read(1024)
            return response.status
    except urllib.error.HTTPError as exc:
        exc.read(1024)
        return exc.code


for _ in range(30):
    status = status_for("/health")
    if status == 200:
        print("public health verified: 200")
        break
    time.sleep(5)
else:
    raise SystemExit(f"public health verification failed: {status}")
PY
}

deploy_one_instance() {
  configure_instance "$1"
  INSTANCE_RELEASE_DIR="build/release/instances/$INSTANCE_ID"
  mkdir -p "$INSTANCE_RELEASE_DIR"
  echo "Starting LightRAG test deployment for instance $INSTANCE_ID"
ensure_namespace
apply_registry_pull_secret
apply_runtime_secret
kubectl -n "$NAMESPACE" delete job \
  lightrag-storage-preflight \
  lightrag-coordination-migrate \
  lightrag-storage-bootstrap \
  --ignore-not-found \
  --wait=true

preflight_storage_profile

if kubectl -n "$NAMESPACE" get service "$SERVICE" >/dev/null 2>&1; then
  kubectl -n "$NAMESPACE" patch service "$SERVICE" --type=merge -p "$ROUTE_PAUSE_PATCH"
fi

if kubectl -n "$NAMESPACE" get deployment "$DEPLOYMENT" >/dev/null 2>&1; then
  kubectl -n "$NAMESPACE" scale "deployment/$DEPLOYMENT" --replicas=0
  if kubectl -n "$NAMESPACE" get pod -l "$LABEL_SELECTOR" -o name | grep -q .; then
    kubectl -n "$NAMESPACE" wait --for=delete pod -l "$LABEL_SELECTOR" --timeout=600s
  fi
fi

kubectl -n "$NAMESPACE" apply -f "$BASE_NETWORK_POLICY"
migrate_coordination_schema
bootstrap_storage_profile
apply_environment_snapshot

SNAPSHOT="$(kubectl -n "$NAMESPACE" get configmap lightrag-test-environment -o jsonpath='{.data.snapshot}')"
printf '%s\n' "$SNAPSHOT" > "$INSTANCE_RELEASE_DIR/environment-snapshot.json"
printf '%s' "$SNAPSHOT" | grep -Eq '"cluster"[[:space:]]*:[[:space:]]*"test"'
printf '%s' "$SNAPSHOT" | grep -Eq '"namespace"[[:space:]]*:[[:space:]]*"'"$NAMESPACE"'"'
printf '%s' "$SNAPSHOT" | grep -Eq '"workspace"[[:space:]]*:[[:space:]]*"'"$LIGHTRAG_INSTANCE_WORKSPACE"'"'
printf '%s' "$SNAPSHOT" | grep -Eq '"initialized"[[:space:]]*:[[:space:]]*true'
printf '%s' "$SNAPSHOT" | grep -Eq '"graph_storage"[[:space:]]*:[[:space:]]*"HugeGraphStorage"'
printf '%s' "$SNAPSHOT" | grep -Eq '"vector_storage"[[:space:]]*:[[:space:]]*"PGVectorStorage"'
printf '%s' "$SNAPSHOT" | grep -Eq '"object_storage"[[:space:]]*:[[:space:]]*"s3"'
printf '%s' "$SNAPSHOT" | grep -Eq '"shared_filesystem_required"[[:space:]]*:[[:space:]]*false'

apply_public_entrypoint_config
write_instance_kustomize_overlay

kubectl -n "$NAMESPACE" apply -k "$OVERLAY"
wait_for_pvc_bound lightrag-test-working-rwx
kubectl -n "$NAMESPACE" rollout status "deployment/$DEPLOYMENT" --timeout=600s
kubectl -n "$NAMESPACE" wait --for=condition=Ready pod -l "$LABEL_SELECTOR" --timeout=600s
kubectl -n "$NAMESPACE" get pods -l "$LABEL_SELECTOR" -o json > "$INSTANCE_RELEASE_DIR/pods.json"

POD_NAMES="$(kubectl -n "$NAMESPACE" get pods -l "$LABEL_SELECTOR" -o jsonpath='{range .items[*]}{.metadata.name}{"\n"}{end}')"
POD_COUNT="$(printf '%s\n' "$POD_NAMES" | sed '/^$/d' | wc -l | tr -d ' ')"
if [ "$POD_COUNT" != "2" ]; then
  echo "expected exactly two LightRAG pods, got $POD_COUNT" >&2
  exit 1
fi

POD_IMAGES="$(kubectl -n "$NAMESPACE" get pods -l "$LABEL_SELECTOR" -o jsonpath='{range .items[*]}{.metadata.name}{"\t"}{range .status.containerStatuses[*]}{.imageID}{"\n"}{end}{end}')"
printf '%s\n' "$POD_IMAGES" > "$INSTANCE_RELEASE_DIR/pod-images.txt"
IMAGE_MATCHES="$(printf '%s\n' "$POD_IMAGES" | grep -c "$LIGHTRAG_IMAGE_DIGEST" || true)"
if [ "$IMAGE_MATCHES" != "2" ]; then
  echo "not all pods are running verified digest $LIGHTRAG_IMAGE_DIGEST" >&2
  cat "$INSTANCE_RELEASE_DIR/pod-images.txt" >&2
  exit 1
fi

kubectl -n "$NAMESPACE" patch service "$SERVICE" --type=merge -p "$ROUTE_RESTORE_PATCH"
ENDPOINTS="$(kubectl -n "$NAMESPACE" get endpoints "$SERVICE" -o jsonpath='{.subsets[*].addresses[*].ip}')"
if [ -z "$ENDPOINTS" ]; then
  echo "service $SERVICE has no endpoints after routing restore" >&2
  exit 1
fi
verify_public_ingress

show_kubectl_status "Deployment status" \
  kubectl -n "$NAMESPACE" get deployment "$DEPLOYMENT" -o wide
show_kubectl_status "Pod status" \
  kubectl -n "$NAMESPACE" get pods -l "$LABEL_SELECTOR" -o wide
show_kubectl_status "Service status" \
  kubectl -n "$NAMESPACE" get service "$SERVICE" -o wide
show_kubectl_status "Ingress status" \
  kubectl -n "$NAMESPACE" get ingress "$SERVICE" -o wide

cat > "$INSTANCE_RELEASE_DIR/deployment-result.json" <<JSON
{"instance":"$INSTANCE_ID","namespace":"$NAMESPACE","workspace":"$LIGHTRAG_INSTANCE_WORKSPACE","tag":"$CI_COMMIT_TAG","commit":"$CI_COMMIT_SHA","digest":"$LIGHTRAG_IMAGE_DIGEST","pods":2,"service_endpoints":"present","public_base_url":"$LIGHTRAG_TEST_PUBLIC_BASE_URL","ingress_host":"$LIGHTRAG_TEST_PUBLIC_HOST"}
JSON
cat "$INSTANCE_RELEASE_DIR/deployment-result.json"
cat "$INSTANCE_RELEASE_DIR/deployment-result.json" >> build/release/deployment-results.jsonl
}

deploy_all_instances() {
  rm -f build/release/deployment-results.jsonl
  seen=" "
  for instance in $(instance_list); do
    validate_instance_id "$instance"
    case "$seen" in
      *" $instance "*)
        echo "duplicate LightRAG test instance id: $instance" >&2
        exit 1
        ;;
    esac
    seen="$seen$instance "
    deploy_one_instance "$instance"
  done
}

mkdir -p "$(dirname "$KUBECONFIG_FILE")" build/release
cleanup() {
  rm -f "$KUBECONFIG_FILE"
}
trap cleanup EXIT INT TERM

if printf '%s' "$LIGHTRAG_TEST_KUBECONFIG" | grep -q '^apiVersion:'; then
  printf '%s' "$LIGHTRAG_TEST_KUBECONFIG" > "$KUBECONFIG_FILE"
else
  printf '%s' "$LIGHTRAG_TEST_KUBECONFIG" | base64 -d > "$KUBECONFIG_FILE"
fi
chmod 600 "$KUBECONFIG_FILE"
export KUBECONFIG="$KUBECONFIG_FILE"

kubectl cluster-info >/dev/null
deploy_all_instances
