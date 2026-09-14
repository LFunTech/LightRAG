#!/usr/bin/env bash
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$ROOT"
export LIGHTRAG_TEST_ISOLATED=1
export DOCX_SMART_HEADING=false
if command -v apt-get >/dev/null 2>&1; then
  apt-get update
  apt-get install -y --no-install-recommends libcairo2
  rm -rf /var/lib/apt/lists/*
fi
uv sync --extra api --extra test --extra offline-storage --extra offline-llm
export FAISS_OPT_LEVEL="${FAISS_OPT_LEVEL:-generic}"

faiss_smoke() {
  timeout 30s "$ROOT/.venv/bin/python" - <<'PY'
import faiss
print(f"faiss import ok: {getattr(faiss, '__version__', 'unknown')}")
PY
}

ensure_faiss_importable() {
  local status
  set +e
  faiss_smoke
  status=$?
  set -e
  if [ "$status" -eq 0 ]; then
    return 0
  fi

  echo "faiss-cpu from uv.lock failed to import on this CI runner (exit $status); trying compatible wheels." >&2
  for version in 1.13.0 1.12.0 1.11.0; do
    echo "Trying faiss-cpu==$version" >&2
    uv pip install --python "$ROOT/.venv/bin/python" --no-deps "faiss-cpu==$version"
    set +e
    faiss_smoke
    status=$?
    set -e
    if [ "$status" -eq 0 ]; then
      echo "Using faiss-cpu==$version for this CI runner." >&2
      return 0
    fi
  done

  echo "No faiss-cpu fallback version could import on this CI runner." >&2
  return 1
}

ensure_faiss_importable
unset HTTP_PROXY HTTPS_PROXY ALL_PROXY http_proxy https_proxy all_proxy
mkdir -p build/ci-reports
PYTHON="$ROOT/.venv/bin/python" ./scripts/test.sh tests -m "not integration" --junitxml build/ci-reports/backend-pytest.xml
