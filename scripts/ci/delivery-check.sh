#!/usr/bin/env bash
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$ROOT"
./scripts/test.sh tests/ci tests/setup/test_docker_base_images.py
if command -v kubectl >/dev/null 2>&1; then
  kubectl kustomize k8s-deploy/lightrag-kustomize/overlays/test >/tmp/lightrag-test-kustomize-render.yaml
fi
if command -v woodpecker-cli >/dev/null 2>&1; then
  woodpecker-cli lint .woodpecker/*.yml
fi
