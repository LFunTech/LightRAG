# Woodpecker test delivery runbook

This Kustomize runbook covers the LightRAG fork delivery path for protected `vX.Y.Z-test` tags. It targets the test cluster namespace `lightrag-test` only.

## Boundaries

- `main` remains the upstream mirror and is not a release source.
- `master` push and PR events run quality checks without release or deployment secrets.
- Source archives, image records and deployment state use LightRAG-owned COS/registry paths only.
- Initial environment preparation is **not part of every tag deployment**. Automatic deployment refuses missing schemas, runtime Secrets, PVCs, profile registration or unknown storage state.
- Do not claim YAML lint or Kustomize render as deployment success. Static validation, environment initialization and remote acceptance are separate evidence classes.

## Required CI onboarding

1. Enable the repository in Woodpecker with protected tags matching `v*`, `v*-pre` and `v*-test`.
2. Configure secrets only for tag events where the workflow needs them:
   - `lightrag_cos_access_key` / `lightrag_cos_secret_key` for LightRAG source archive and release records.
   - `registry_username` / `registry_password` scoped to `docker-hub.f123.pub/lfun/lightrag`.
   - `lightrag_test_kubeconfig` for the `lightrag-test-deployer` ServiceAccount in `lightrag-test`.
3. Do not expose runtime model, database or graph credentials to build steps. Runtime credentials live in the Kubernetes Secret `lightrag-runtime`.

## First-time test environment initialization

An operator must explicitly prepare the environment before expecting automatic `-test` deployment to pass:

1. Apply the Kustomize overlay `k8s-deploy/lightrag-kustomize/overlays/test` or its reviewed RBAC subset to create `lightrag-test`, ServiceAccount, Role and RoleBinding.
2. Create `lightrag-test-inputs-rwx` and `lightrag-test-working-rwx` PVCs with the approved RWX StorageClass.
3. Verify cross-node filesystem semantics as UID/GID 1000: create/read, atomic rename and exclusive create on both PVCs.
4. Provision LightRAG-only PostgreSQL/pgvector schemas and HugeGraph graph/domain. Do not share another application's data stores.
5. Create `lightrag-runtime` with API key, token secret when account auth is enabled, PG password, HugeGraph credentials and Bailian-compatible model keys for `qwen-plus` and `text-embedding-v4` (1024 dimensions).
6. Stop all writers, wait for in-flight requests to finish, and run the existing explicit migrate/bootstrap maintenance procedure. CI must not auto-bootstrap or auto-recover.

## Release flow

1. Push/PR to `master`: run backend, frontend and delivery static checks. Backend CI installs required system libraries such as `libcairo2`, but does not dynamically download spaCy language models; smart-heading tests that require missing models use the test suite skip report instead of turning the quality gate into a proxy-dependent model download. It also probes the Faiss wheel before pytest; if the locked wheel traps on the current runner CPU, CI falls back to a compatible `faiss-cpu` wheel within the repository dependency range and still runs the Faiss tests. Before pytest starts, the script clears inherited proxy variables so offline tests do not depend on Woodpecker agent proxy adapters.
2. Protected tag `vX.Y.Z-test`: verify repository/tag/commit/master ancestry, archive source, build and verify `docker-hub.f123.pub/lfun/lightrag` image, then deploy by digest to `lightrag-test` with Kustomize and `kubectl apply -k`, keeping Service routing paused.
3. Verify both Pods' `imageID`, health, authentication, distributed status and cross-Pod ingestion/query with a unique test document.
4. Restore Service routing only after acceptance succeeds.
5. Tags `vX.Y.Z-pre` and `vX.Y.Z` publish and verify images but do not deploy.

## Failure and rollback

If a release lock, old writer shutdown, storage fence, pending operation, image identity or acceptance check is uncertain, leave routing closed and preserve evidence. Do not force delete Pods, clear claims, bootstrap, recover or automatically start old writers. A rollback is a manual maintenance operation after operators verify no writes are active and the storage profile is compatible.

## Evidence status

Remote acceptance not yet run until the approved test cluster resources, scoped secrets and tag-trigger authorization are configured and a real Woodpecker pipeline records pipeline ID, commit, tag, digest, per-Pod image IDs and business acceptance results.
