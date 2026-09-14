# Woodpecker test delivery runbook

This Kustomize runbook covers the LightRAG fork delivery path for protected `vX.Y.Z-test` tags. It targets the test cluster namespace `lightrag-test` only.

## Boundaries

- `main` remains the upstream mirror and is not a release source.
- Woodpecker is tag-only for this repository; `master` pushes and PRs do not start LightRAG Woodpecker pipelines.
- Source archives, image records and deployment state use LightRAG-owned COS/registry paths only. All Woodpecker workflows declare `skip_clone: true`; the release-source workflow performs the only manual single-commit clone as its first step, uploads the source package through MinIO/S3-compatible `mc`, and downstream workflows download that package instead of cloning again.
- Initial environment preparation is **not part of every tag deployment**. Automatic deployment refuses missing schemas, runtime Secrets, PVCs, profile registration or unknown storage state.
- Do not claim YAML lint, Kustomize render, or local test results as deployment success. Static validation, local/separate-CI testing, environment initialization and remote acceptance are separate evidence classes.

## Required CI onboarding

1. Enable the repository in Woodpecker with protected tags matching `v*`, `v*-pre` and `v*-test`; do not enable `push` or `pull_request` workflows for this repository.
2. Reuse existing Woodpecker global/organization secrets where their scope matches the LightRAG release boundary:
   - Organization COS secrets: `cos_storage_endpoint`, `cos_storage_bucket`, `cos_storage_secret_id`, `cos_storage_secret_key`.
   - Global registry secrets: `DOCKER_USERNAME`, `DOCKER_PASSWORD`.
   - Global test-cluster kubeconfig: `kubeconfig_test`, injected into the workflow as `LIGHTRAG_TEST_KUBECONFIG`.
   - 2026-09-14 check: these names exist as global/organization secrets on `https://woodpecker.f123.pub`; `LFunTech/LightRAG` has no repository-local secrets.
3. Add the missing repository release controls before declaring the pipeline ready: the GitHub repository currently has no rulesets, so protected release tags still need explicit configuration.
4. Do not expose runtime model, database or graph credentials to build steps. Runtime credentials live in the Kubernetes Secret `lightrag-runtime`.

## First-time test environment initialization

An operator must explicitly prepare the environment before expecting automatic `-test` deployment to pass:

1. Apply the Kustomize overlay `k8s-deploy/lightrag-kustomize/overlays/test` or its reviewed RBAC subset to create `lightrag-test`, ServiceAccount, Role and RoleBinding. As of 2026-09-14, `lightrag-test` does not exist on the test cluster.
2. Create `lightrag-test-inputs-rwx` and `lightrag-test-working-rwx` PVCs with the approved RWX StorageClass.
3. Verify cross-node filesystem semantics as UID/GID 1000: create/read, atomic rename and exclusive create on both PVCs.
4. Provision LightRAG-only PostgreSQL/pgvector schemas and HugeGraph graph/domain. Do not share another application's data stores.
5. Create `lightrag-runtime` with API key, token secret when account auth is enabled, PG password, HugeGraph credentials and Bailian-compatible model keys for `qwen-plus` and `text-embedding-v4` (1024 dimensions).
6. Stop all writers, wait for in-flight requests to finish, and run the existing explicit migrate/bootstrap maintenance procedure. CI must not auto-bootstrap or auto-recover.

## Release flow

1. Push/PR to `master`: no LightRAG Woodpecker pipeline is selected. Keep backend pytest, frontend Bun tests, frontend typecheck/lint/build scripts and external integration tests as local or separate-CI evidence.
2. Protected tag `vX.Y.Z-test`: the source workflow performs the only clone, runs delivery static checks, verifies repository/tag/commit/master ancestry, archives the cloned source, uploads the archive/checksum/record to the LightRAG COS path with `mc`, then downstream workflows download the archive from MinIO/COS to build and verify `docker-hub.f123.pub/lfun/lightrag` and deploy by digest to `lightrag-test` with Kustomize and `kubectl apply -k`, keeping Service routing paused.
   - The clone step follows the working `../haier-demo` pattern: internal CI tools image, `git init`, no tag/submodule/LFS fetch, `--depth=1` for `CI_COMMIT_SHA`, and retry logging. It is implemented as a normal first step rather than top-level `clone:` because strict Woodpecker lint only accepts server-allowlisted clone images.
   - BuildKit execution is `scripts/ci/build-image.sh` inside the rootless BuildKit image; it keeps the haier-style registry auth/cache/progress/apt-mirror shape but uses BuildKit rather than Kaniko because the LightRAG Dockerfile depends on `RUN --mount` cache/bind features. The Dockerfiles default Debian apt sources to the Tsinghua mirrors before `apt-get update`.
   - Image verification is `python -m scripts.ci.delivery resolve-image`, which resolves the registry digest and checks the image config revision label against the release commit.
   - Deployment is `scripts/ci/deploy-test.sh` inside the Kubernetes-capable CI tools image; it follows the haier-style shell-script deployment/status pattern, consumes `build/release/image.env` produced from the verified image record, writes kubeconfig from the global `kubeconfig_test` secret, applies the Kustomize overlay, waits for rollout, and prints Deployment/Pod/Service status.
3. Verify both Pods' `imageID`, per-Pod health and authentication behavior, plus cross-Pod ingestion/query with a unique test document.
4. Restore Service routing only after acceptance succeeds.
5. Tags `vX.Y.Z-pre` and `vX.Y.Z` publish and verify images but do not deploy.

A plain `master` push does not start Woodpecker by design. To exercise the build and deploy chain, create a protected `vX.Y.Z-test` tag after the scoped registry, COS and kubeconfig secrets and the initialized `lightrag-test` namespace are in place.

## Failure and rollback

If a release lock, old writer shutdown, storage fence, pending operation, image identity or acceptance check is uncertain, leave routing closed and preserve evidence. Do not force delete Pods, clear claims, bootstrap, recover or automatically start old writers. A rollback is a manual maintenance operation after operators verify no writes are active and the storage profile is compatible.

## Evidence status

Remote acceptance not yet run until the approved test cluster resources, scoped secrets and tag-trigger authorization are configured and a real Woodpecker pipeline records pipeline ID, commit, tag, digest, per-Pod image IDs and business acceptance results.
