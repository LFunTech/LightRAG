#!/usr/bin/env bash
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$ROOT/lightrag_webui"
bun install --frozen-lockfile
bun test
bunx tsc --noEmit
bun run lint
bun run build
