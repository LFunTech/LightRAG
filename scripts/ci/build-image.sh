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
IMAGE_REPOSITORY="${LIGHTRAG_IMAGE_REPOSITORY:-lfun/lightrag}"
IMAGE="${REGISTRY%/}/${IMAGE_REPOSITORY}"
CACHE_REF="${LIGHTRAG_IMAGE_CACHE_REF:-${IMAGE}:buildcache}"
SOURCE_URL="https://github.com/${CI_REPO:-LFunTech/LightRAG}"
DOCKER_CONFIG_DIR="${DOCKER_CONFIG:-${HOME:-/tmp}/.docker}"
METADATA_FILE="${METADATA_FILE:-/tmp/lightrag-build-metadata.json}"

mkdir -p "$DOCKER_CONFIG_DIR" "$(dirname "$METADATA_FILE")"
cleanup() {
  rm -f "$DOCKER_CONFIG_DIR/config.json"
}
trap cleanup EXIT INT TERM

AUTH_TOKEN="$(printf '%s:%s' "$REGISTRY_USERNAME" "$REGISTRY_PASSWORD" | base64 | tr -d '\n')"
printf '{"auths":{"%s":{"auth":"%s"}}}\n' "$REGISTRY" "$AUTH_TOKEN" > "$DOCKER_CONFIG_DIR/config.json"
chmod 600 "$DOCKER_CONFIG_DIR/config.json"
export DOCKER_CONFIG="$DOCKER_CONFIG_DIR"

# Woodpecker agents may inject proxy values unsupported by registry clients in
# minimal builder images. The private registry and mirrored base images are
# reachable directly from the runner network.
unset HTTP_PROXY HTTPS_PROXY ALL_PROXY http_proxy https_proxy all_proxy

export BUILDKIT_PROGRESS=plain
echo "starting rootless BuildKit image build"
echo "image tag: ${IMAGE}:${CI_COMMIT_TAG}"
echo "commit: ${CI_COMMIT_SHA}"
echo "cache ref: ${CACHE_REF}"
echo "metadata file: ${METADATA_FILE}"

buildctl-daemonless.sh build \
  --progress=plain \
  --frontend dockerfile.v0 \
  --local context=. \
  --local dockerfile=. \
  --opt filename=Dockerfile \
  --opt platform=linux/amd64 \
  --opt "build-arg:LIGHTRAG_IMAGE_SOURCE=$SOURCE_URL" \
  --opt "build-arg:LIGHTRAG_IMAGE_REVISION=$CI_COMMIT_SHA" \
  --opt "build-arg:LIGHTRAG_IMAGE_VERSION=$CI_COMMIT_TAG" \
  --import-cache "type=registry,ref=$CACHE_REF" \
  --export-cache "type=registry,ref=$CACHE_REF,mode=max" \
  --metadata-file "$METADATA_FILE" \
  --output "type=image,name=${IMAGE}:${CI_COMMIT_TAG},push=true"

test -s "$METADATA_FILE"
echo "image tag pushed: ${IMAGE}:${CI_COMMIT_TAG}"
