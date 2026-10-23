#!/usr/bin/env sh
set -eu
if (set -o pipefail) 2>/dev/null; then set -o pipefail; fi
umask 077
: "${LIGHTRAG_PROD_KUBECONFIG:?explicit prod kubeconfig is required}"
: "${LIGHTRAG_PROD_ONBOARDING_JSON:?approved prod onboarding is required}"
: "${CI_COMMIT_TAG:?formal release tag is required}"
: "${CI_COMMIT_SHA:?commit is required}"
# No startup/bootstrap/migrate/recover, no automatic undo in the trap.
TMP_DIR="$(mktemp -d)"
trap 'rm -rf "$TMP_DIR"' EXIT
printf '%s' "$LIGHTRAG_PROD_KUBECONFIG" > "$TMP_DIR/kubeconfig"
printf '%s' "$LIGHTRAG_PROD_ONBOARDING_JSON" > "$TMP_DIR/config.json"
python3 -m scripts.ci.prod_deploy \
  --config "$TMP_DIR/config.json" \
  --kubeconfig "$TMP_DIR/kubeconfig" \
  --image-record /woodpecker/src/build/release/image-record.json \
  --source-archive /woodpecker/src/build/source-artifact/source.tar.gz \
  --source-record /woodpecker/src/build/source-artifact/source-record.json \
  --execute
