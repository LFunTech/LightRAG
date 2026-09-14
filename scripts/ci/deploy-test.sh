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

NAMESPACE="${LIGHTRAG_TEST_NAMESPACE:-lightrag-test}"
DEPLOYMENT="${LIGHTRAG_TEST_DEPLOYMENT:-lightrag}"
SERVICE="${LIGHTRAG_TEST_SERVICE:-lightrag}"
CONTAINER="${LIGHTRAG_TEST_CONTAINER:-lightrag}"
OVERLAY="${LIGHTRAG_KUSTOMIZE_OVERLAY:-k8s-deploy/lightrag-kustomize/overlays/test}"
BASE_NETWORK_POLICY="${LIGHTRAG_BASE_NETWORK_POLICY:-k8s-deploy/lightrag-kustomize/base/networkpolicy.yaml}"
LABEL_SELECTOR="app.kubernetes.io/name=lightrag,app.kubernetes.io/instance=lightrag"
KUBECONFIG_FILE="${KUBECONFIG_FILE:-/tmp/lightrag-test-kubeconfig}"
ROUTE_PAUSE_PATCH='{"spec":{"selector":{"lightrag.openai.com/routing-paused":"true"}}}'
ROUTE_RESTORE_PATCH='{"spec":{"selector":{"app.kubernetes.io/name":"lightrag","app.kubernetes.io/instance":"lightrag"}}}'

require_env() {
  name="$1"
  eval "value=\${$name:-}"
  if [ -z "$value" ]; then
    echo "required environment variable is not set: $name" >&2
    exit 1
  fi
}

show_kubectl_status() {
  label="$1"
  shift

  echo "${label}:"
  if ! "$@"; then
    echo "Unable to read ${label}; keeping deployment command status unchanged." >&2
  fi
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

apply_runtime_secret() {
  require_env LIGHTRAG_TEST_API_KEY
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
  require_env LIGHTRAG_TEST_HUGEGRAPH_AUTH_METHOD

  LIGHTRAG_COORDINATION_DSN="$(make_coordination_dsn)"

  kubectl -n "$NAMESPACE" create secret generic lightrag-runtime \
    --from-literal=LIGHTRAG_API_KEY="$LIGHTRAG_TEST_API_KEY" \
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
    --from-literal=HUGEGRAPH_AUTH_METHOD="$LIGHTRAG_TEST_HUGEGRAPH_AUTH_METHOD" \
    --dry-run=client -o yaml | kubectl apply -f -
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
  activeDeadlineSeconds: 180
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

  if ! kubectl -n "$NAMESPACE" wait \
    --for=condition=Complete \
    job/lightrag-coordination-migrate \
    --timeout=180s; then
    echo "Coordination migration failed or timed out; recent non-secret logs follow:" >&2
    kubectl -n "$NAMESPACE" logs job/lightrag-coordination-migrate --tail=120 >&2 || true
    kubectl -n "$NAMESPACE" get pods \
      -l app.kubernetes.io/component=coordination-migrate \
      -o wide >&2 || true
    exit 1
  fi

  kubectl -n "$NAMESPACE" logs job/lightrag-coordination-migrate --tail=120 || true
  kubectl -n "$NAMESPACE" delete job lightrag-coordination-migrate --wait=true
}

apply_environment_snapshot() {
  cat > build/release/environment-snapshot.json <<JSON
{"cluster":"test","namespace":"$NAMESPACE","initialized":true,"profile":{"replicas":2,"workers":1,"kv_storage":"PGKVStorage","doc_status_storage":"PGDocStatusStorage","vector_storage":"PGVectorStorage","graph_storage":"HugeGraphStorage","workspace":"lightrag_test","postgres_workspace":"lightrag_test"},"storage_state":{"fenced":false,"active_operations":0,"pending_mutations":0,"orphaned_claims":0}}
JSON
  kubectl -n "$NAMESPACE" create configmap lightrag-test-environment \
    --from-file=snapshot=build/release/environment-snapshot.json \
    --dry-run=client -o yaml | kubectl apply -f -
}

wait_for_pvc_bound() {
  pvc="$1"
  kubectl -n "$NAMESPACE" wait \
    --for=jsonpath='{.status.phase}'=Bound \
    "pvc/$pvc" \
    --timeout=600s
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
ensure_namespace
apply_registry_pull_secret
apply_runtime_secret
kubectl -n "$NAMESPACE" delete job lightrag-coordination-migrate --ignore-not-found --wait=true

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
apply_environment_snapshot

SNAPSHOT="$(kubectl -n "$NAMESPACE" get configmap lightrag-test-environment -o jsonpath='{.data.snapshot}')"
printf '%s\n' "$SNAPSHOT" > build/release/environment-snapshot.json
printf '%s' "$SNAPSHOT" | grep -Eq '"cluster"[[:space:]]*:[[:space:]]*"test"'
printf '%s' "$SNAPSHOT" | grep -Eq '"namespace"[[:space:]]*:[[:space:]]*"lightrag-test"'
printf '%s' "$SNAPSHOT" | grep -Eq '"initialized"[[:space:]]*:[[:space:]]*true'
printf '%s' "$SNAPSHOT" | grep -Eq '"graph_storage"[[:space:]]*:[[:space:]]*"HugeGraphStorage"'
printf '%s' "$SNAPSHOT" | grep -Eq '"vector_storage"[[:space:]]*:[[:space:]]*"PGVectorStorage"'

cat > "$OVERLAY/release-image.yaml" <<YAML
apiVersion: v1
kind: ConfigMap
metadata:
  name: lightrag-release-image
data:
  digest: $LIGHTRAG_IMAGE_DIGEST
YAML

kubectl apply -k "$OVERLAY"
wait_for_pvc_bound lightrag-test-working-rwx
wait_for_pvc_bound lightrag-test-inputs-rwx
kubectl -n "$NAMESPACE" rollout status "deployment/$DEPLOYMENT" --timeout=600s
kubectl -n "$NAMESPACE" wait --for=condition=Ready pod -l "$LABEL_SELECTOR" --timeout=600s
kubectl -n "$NAMESPACE" get pods -l "$LABEL_SELECTOR" -o json > build/release/pods.json

POD_NAMES="$(kubectl -n "$NAMESPACE" get pods -l "$LABEL_SELECTOR" -o jsonpath='{range .items[*]}{.metadata.name}{"\n"}{end}')"
POD_COUNT="$(printf '%s\n' "$POD_NAMES" | sed '/^$/d' | wc -l | tr -d ' ')"
if [ "$POD_COUNT" != "2" ]; then
  echo "expected exactly two LightRAG pods, got $POD_COUNT" >&2
  exit 1
fi

POD_IMAGES="$(kubectl -n "$NAMESPACE" get pods -l "$LABEL_SELECTOR" -o jsonpath='{range .items[*]}{.metadata.name}{"\t"}{range .status.containerStatuses[*]}{.imageID}{"\n"}{end}{end}')"
printf '%s\n' "$POD_IMAGES" > build/release/pod-images.txt
IMAGE_MATCHES="$(printf '%s\n' "$POD_IMAGES" | grep -c "$LIGHTRAG_IMAGE_DIGEST" || true)"
if [ "$IMAGE_MATCHES" != "2" ]; then
  echo "not all pods are running verified digest $LIGHTRAG_IMAGE_DIGEST" >&2
  cat build/release/pod-images.txt >&2
  exit 1
fi

FIRST_POD="$(printf '%s\n' "$POD_NAMES" | sed -n '1p')"
SECOND_POD="$(printf '%s\n' "$POD_NAMES" | sed -n '2p')"
MARKER="woodpecker-${CI_COMMIT_TAG}-${CI_COMMIT_SHA}-${CI_PIPELINE_NUMBER:-manual}-$(date +%s)"

kubectl -n "$NAMESPACE" exec "$FIRST_POD" -c "$CONTAINER" -- python - "$MARKER" <<'PY' > build/release/acceptance-insert.json
import json
import os
import sys
import time
import urllib.error
import urllib.request

base = "http://127.0.0.1:9621"
marker = sys.argv[1]
api_key = os.environ.get("LIGHTRAG_API_KEY")
if not api_key:
    raise SystemExit("LIGHTRAG_API_KEY is not available in pod environment")


def request(method, path, payload=None, auth=True):
    data = None if payload is None else json.dumps(payload).encode()
    headers = {"Content-Type": "application/json"}
    if auth:
        headers["X-API-Key"] = api_key
    req = urllib.request.Request(base + path, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=30) as response:
            return response.status, response.read().decode()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode()

health_status, _ = request("GET", "/health", auth=False)
if health_status != 200:
    raise SystemExit(f"health returned {health_status}")
unauth_status, _ = request("GET", "/documents/pipeline_status", auth=False)
if unauth_status not in (401, 403):
    raise SystemExit(f"protected endpoint unauthenticated status was {unauth_status}")
insert_status, insert_body = request(
    "POST",
    "/documents/text",
    {
        "text": f"LightRAG Woodpecker delivery acceptance marker {marker}. The answer marker is {marker}.",
        "file_source": f"woodpecker-acceptance/{marker}.txt",
    },
)
if insert_status not in (200, 202):
    raise SystemExit(f"insert returned {insert_status}: {insert_body[:200]}")
track_id = json.loads(insert_body).get("track_id")
if not track_id:
    raise SystemExit("insert response did not include track_id")
summary = {}
for _ in range(120):
    time.sleep(2)
    status, body = request("GET", f"/documents/track_status/{track_id}")
    if status == 404:
        continue
    if status != 200:
        raise SystemExit(f"track status returned {status}: {body[:200]}")
    data = json.loads(body)
    summary = {str(k).upper(): v for k, v in data.get("status_summary", {}).items()}
    if summary.get("FAILED") or summary.get("ERROR"):
        raise SystemExit(f"document processing failed: {summary}")
    total = int(data.get("total_count") or 1)
    if int(summary.get("PROCESSED") or summary.get("processed") or 0) >= total:
        break
else:
    raise SystemExit(f"document did not finish processing: {summary}")
print(json.dumps({"marker": marker, "track_id": track_id, "status_summary": summary}, sort_keys=True))
PY

kubectl -n "$NAMESPACE" exec "$SECOND_POD" -c "$CONTAINER" -- python - "$MARKER" <<'PY' > build/release/acceptance-query.json
import json
import os
import sys
import time
import urllib.error
import urllib.request

base = "http://127.0.0.1:9621"
marker = sys.argv[1]
api_key = os.environ.get("LIGHTRAG_API_KEY")
if not api_key:
    raise SystemExit("LIGHTRAG_API_KEY is not available in pod environment")


def request(method, path, payload=None):
    data = None if payload is None else json.dumps(payload).encode()
    req = urllib.request.Request(
        base + path,
        data=data,
        headers={"Content-Type": "application/json", "X-API-Key": api_key},
        method=method,
    )
    try:
        with urllib.request.urlopen(req, timeout=60) as response:
            return response.status, response.read().decode()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode()

health_status, _ = request("GET", "/health")
if health_status != 200:
    raise SystemExit(f"health returned {health_status}")
unauth_req = urllib.request.Request(base + "/documents/pipeline_status", method="GET")
try:
    with urllib.request.urlopen(unauth_req, timeout=30) as response:
        unauth_status = response.status
except urllib.error.HTTPError as exc:
    unauth_status = exc.code
if unauth_status not in (401, 403):
    raise SystemExit(f"protected endpoint unauthenticated status was {unauth_status}")

payload = {
    "query": f"Find the Woodpecker delivery acceptance marker {marker}.",
    "mode": "naive",
    "only_need_context": True,
    "include_references": True,
    "include_chunk_content": True,
    "top_k": 5,
    "chunk_top_k": 5,
}
last_status = None
last_body = ""
for _ in range(60):
    status, body = request("POST", "/query", payload)
    last_status = status
    last_body = body
    if status == 200 and marker in body:
        print(json.dumps({"cross_pod_query_ok": True, "marker": marker}, sort_keys=True))
        break
    time.sleep(3)
else:
    raise SystemExit(f"cross-pod query failed: status={last_status} body={last_body[:300]}")
PY

kubectl -n "$NAMESPACE" patch service "$SERVICE" --type=merge -p "$ROUTE_RESTORE_PATCH"
ENDPOINTS="$(kubectl -n "$NAMESPACE" get endpoints "$SERVICE" -o jsonpath='{.subsets[*].addresses[*].ip}')"
if [ -z "$ENDPOINTS" ]; then
  echo "service $SERVICE has no endpoints after routing restore" >&2
  exit 1
fi

show_kubectl_status "Deployment status" \
  kubectl -n "$NAMESPACE" get deployment "$DEPLOYMENT" -o wide
show_kubectl_status "Pod status" \
  kubectl -n "$NAMESPACE" get pods -l "$LABEL_SELECTOR" -o wide
show_kubectl_status "Service status" \
  kubectl -n "$NAMESPACE" get service "$SERVICE" -o wide

cat > build/release/deployment-result.json <<JSON
{"tag":"$CI_COMMIT_TAG","commit":"$CI_COMMIT_SHA","digest":"$LIGHTRAG_IMAGE_DIGEST","pods":2,"service_endpoints":"present"}
JSON
cat build/release/deployment-result.json
