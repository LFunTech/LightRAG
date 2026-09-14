#!/usr/bin/env bash
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$ROOT"
export LIGHTRAG_TEST_ISOLATED=1
if command -v apt-get >/dev/null 2>&1; then
  apt-get update
  apt-get install -y --no-install-recommends libcairo2
  rm -rf /var/lib/apt/lists/*
fi
uv sync --extra api --extra test --extra offline-storage --extra offline-llm
uv pip install pip PySocks
uv run lightrag-download-cache --spacy-install
mkdir -p build/ci-reports
./scripts/test.sh tests -m "not integration" --junitxml build/ci-reports/backend-pytest.xml
