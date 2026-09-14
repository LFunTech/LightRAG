#!/usr/bin/env sh
set -eu
if (set -o pipefail) 2>/dev/null; then
  set -o pipefail
fi

: "${CI_COMMIT_TAG:?CI_COMMIT_TAG is required}"
: "${CI_COMMIT_SHA:?CI_COMMIT_SHA is required}"
REGISTRY_USERNAME="${REGISTRY_USERNAME:-${DOCKER_USERNAME:-}}"
REGISTRY_PASSWORD="${REGISTRY_PASSWORD:-${DOCKER_PASSWORD:-}}"
: "${REGISTRY_USERNAME:?REGISTRY_USERNAME or DOCKER_USERNAME is required}"
: "${REGISTRY_PASSWORD:?REGISTRY_PASSWORD or DOCKER_PASSWORD is required}"

case "$CI_COMMIT_TAG" in
  v[0-9]*.[0-9]*.[0-9]*|v[0-9]*.[0-9]*.[0-9]*-pre|v[0-9]*.[0-9]*.[0-9]*-test) ;;
  *)
    echo "unsupported release tag: $CI_COMMIT_TAG" >&2
    exit 1
    ;;
esac

REGISTRY="${REGISTRY:-docker-hub.f123.pub}"
REGISTRY_HOST="${REGISTRY#http://}"
REGISTRY_HOST="${REGISTRY_HOST#https://}"
REGISTRY_HOST="${REGISTRY_HOST%%/}"
IMAGE_REPOSITORY="${LIGHTRAG_IMAGE_REPOSITORY:-lfun/lightrag}"
CACHE_REPOSITORY="${LIGHTRAG_IMAGE_CACHE_REPOSITORY:-lfun/cache-lightrag}"
IMAGE="${REGISTRY_HOST}/${IMAGE_REPOSITORY}"
CACHE_REPO="${REGISTRY_HOST}/${CACHE_REPOSITORY}"
SOURCE_URL="https://github.com/${CI_REPO:-LFunTech/LightRAG}"
CONTEXT_DIR="${LIGHTRAG_BUILD_CONTEXT_DIR:-${CI_WORKSPACE:-/woodpecker/src}}"
KANIKO_EXECUTOR="${KANIKO_EXECUTOR:-/kaniko/executor}"
DIGEST_FILE="${DIGEST_FILE:-/tmp/lightrag-image-digest.txt}"
IMAGE_NAME_WITH_DIGEST_FILE="${IMAGE_NAME_WITH_DIGEST_FILE:-/tmp/lightrag-image-ref.txt}"
DOCKER_CONFIG_DIR="${DOCKER_CONFIG:-${HOME:-/kaniko}/.docker}"

if [ -z "$REGISTRY_HOST" ]; then
  echo "registry host is empty" >&2
  exit 64
fi
if [ ! -x "$KANIKO_EXECUTOR" ]; then
  echo "Kaniko executor not found at $KANIKO_EXECUTOR; use the Kaniko Woodpecker step image" >&2
  exit 127
fi
if [ ! -d "$CONTEXT_DIR" ] || [ ! -f "$CONTEXT_DIR/Dockerfile" ]; then
  echo "build context is missing Dockerfile: $CONTEXT_DIR" >&2
  exit 66
fi

mkdir -p "$DOCKER_CONFIG_DIR" "$(dirname "$DIGEST_FILE")" "$(dirname "$IMAGE_NAME_WITH_DIGEST_FILE")"
cleanup() {
  rm -f "$DOCKER_CONFIG_DIR/config.json"
}
trap cleanup EXIT INT TERM

AUTH_TOKEN="$(printf '%s:%s' "$REGISTRY_USERNAME" "$REGISTRY_PASSWORD" | base64 | tr -d '\n')"
printf '{"auths":{"%s":{"auth":"%s"}}}\n' \
  "$REGISTRY_HOST" "$AUTH_TOKEN" > "$DOCKER_CONFIG_DIR/config.json"
chmod 600 "$DOCKER_CONFIG_DIR/config.json"
export DOCKER_CONFIG="$DOCKER_CONFIG_DIR"

# Woodpecker agents may inject proxy values unsupported by registry clients in
# minimal builder images. The private registry and mirrored package sources are
# reachable directly from the runner network.
unset HTTP_PROXY HTTPS_PROXY ALL_PROXY http_proxy https_proxy all_proxy

cd "$CONTEXT_DIR"
echo "starting Kaniko image build"
echo "image tag: ${IMAGE}:${CI_COMMIT_TAG}"
echo "commit: ${CI_COMMIT_SHA}"
echo "cache repo: ${CACHE_REPO}"
echo "context dir: ${CONTEXT_DIR}"
echo "digest file: ${DIGEST_FILE}"
echo "kaniko version: $($KANIKO_EXECUTOR version 2>/dev/null || true)"

"$KANIKO_EXECUTOR" \
  --context="dir://${CONTEXT_DIR}" \
  --dockerfile=Dockerfile \
  --destination="${IMAGE}:${CI_COMMIT_TAG}" \
  --build-arg="LIGHTRAG_IMAGE_SOURCE=${SOURCE_URL}" \
  --build-arg="LIGHTRAG_IMAGE_REVISION=${CI_COMMIT_SHA}" \
  --build-arg="LIGHTRAG_IMAGE_VERSION=${CI_COMMIT_TAG}" \
  --cache=true \
  --cache-copy-layers \
  --cache-repo="${CACHE_REPO}" \
  --custom-platform=linux/amd64 \
  --snapshot-mode=redo \
  --digest-file="${DIGEST_FILE}" \
  --image-name-with-digest-file="${IMAGE_NAME_WITH_DIGEST_FILE}"

test -s "$DIGEST_FILE"
test -s "$IMAGE_NAME_WITH_DIGEST_FILE"
echo "image digest: $(cat "$DIGEST_FILE")"
echo "image tag pushed: ${IMAGE}:${CI_COMMIT_TAG}"
