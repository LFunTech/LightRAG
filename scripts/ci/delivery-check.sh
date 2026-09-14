#!/usr/bin/env bash
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$ROOT"

# Woodpecker delivery validation intentionally does not run repository test
# suites. Backend/frontend tests remain local or external-CI checks; this step
# only catches malformed delivery scripts/manifests before release side effects.
PYTHON="${PYTHON:-}"
if [ -z "$PYTHON" ]; then
  if [ -x "$ROOT/.venv/bin/python" ]; then
    PYTHON="$ROOT/.venv/bin/python"
  elif command -v python3 >/dev/null 2>&1; then
    PYTHON=python3
  else
    PYTHON=python
  fi
fi
"$PYTHON" -m py_compile scripts/ci/__init__.py scripts/ci/delivery.py scripts/ci/k8s_deploy.py
"$PYTHON" -m json.tool scripts/ci/base-images.lock.json >/dev/null
"$PYTHON" -m json.tool scripts/ci/tool-images.lock.json >/dev/null
bash -n scripts/ci/delivery-check.sh
bash -n scripts/ci/build-image.sh
bash -n scripts/ci/deploy-test.sh
sh -n scripts/ci/build-image.sh
sh -n scripts/ci/deploy-test.sh

if command -v kubectl >/dev/null 2>&1; then
  kubectl kustomize k8s-deploy/lightrag-kustomize/overlays/test >/tmp/lightrag-test-kustomize-render.yaml
else
  echo "kubectl not available; skipping Kustomize render in this image" >&2
fi
if command -v woodpecker-cli >/dev/null 2>&1; then
  woodpecker-cli lint --strict .woodpecker/*.yml
else
  echo "woodpecker-cli not available; skipping Woodpecker lint in this image" >&2
fi
if command -v openspec >/dev/null 2>&1; then
  openspec validate add-woodpecker-test-delivery --strict
else
  echo "openspec not available; skipping OpenSpec validation in this image" >&2
fi
