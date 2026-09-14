#!/usr/bin/env bash
set -euo pipefail
# Local-only helper. Woodpecker must not invoke this script; repository
# test suites are verified locally or by a separate CI service, not by the
# Woodpecker delivery pipeline.
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$ROOT/lightrag_webui"
bun install --frozen-lockfile
bun test
bunx tsc --noEmit
bun run lint
bun run build
