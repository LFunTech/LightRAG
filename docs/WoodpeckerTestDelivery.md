# Woodpecker test delivery runbook

This Kustomize runbook covers the LightRAG fork delivery path for protected `vX.Y.Z-test` tags. It targets the test cluster namespace `lightrag-test` only.

## Boundaries

- `main` remains the upstream mirror and is not a release source.
- Woodpecker is tag-only for this repository; `master` pushes and PRs do not start LightRAG Woodpecker pipelines.
- Source archives, image records and deployment state use LightRAG-owned COS/registry paths only. All Woodpecker workflows declare `skip_clone: true`; the release-source workflow performs the only manual single-commit clone inside container-local `/tmp`, uploads the source package through MinIO/S3-compatible `mc`, and downstream workflows download that package instead of cloning again.
- Test environment preparation is part of the `deploy-test` pipeline: it creates or updates the `lightrag-test` namespace, registry pull Secret, Kubernetes runtime Secrets, environment snapshot ConfigMap, and Kustomize-managed PVCs before rollout. It still refuses missing Woodpecker secrets or unsafe storage state without printing secret values.
- Do not claim YAML lint, Kustomize render, or local test results as deployment success. Static validation, local/separate-CI testing, environment initialization and remote acceptance are separate evidence classes.

## Required CI onboarding

1. Enable the repository in Woodpecker with protected tags matching `v*`, `v*-pre` and `v*-test`; do not enable `push` or `pull_request` workflows for this repository.
2. Reuse existing Woodpecker global/organization secrets where their scope matches the LightRAG release boundary:
   - Organization COS secrets: `cos_storage_endpoint`, `cos_storage_bucket`, `cos_storage_secret_id`, `cos_storage_secret_key`.
   - Global registry secrets: `DOCKER_USERNAME`, `DOCKER_PASSWORD`.
   - Global test-cluster kubeconfig: `kubeconfig_test`, injected into the workflow as `LIGHTRAG_TEST_KUBECONFIG`.
   - Repository runtime secrets populated from `.secrets/test.secrets`: `lightrag_test_bailian_api_key`, `lightrag_test_bailian_api_host`, `lightrag_test_bailian_region`, `lightrag_test_dashscope_workspace_id`, `lightrag_test_postgres_host`, `lightrag_test_postgres_port`, `lightrag_test_postgres_user`, `lightrag_test_postgres_database`, `lightrag_test_postgres_password`, `lightrag_test_hugegraph_uri`, `lightrag_test_hugegraph_graph`, `lightrag_test_hugegraph_graphspace`, `lightrag_test_hugegraph_username`, `lightrag_test_hugegraph_password`.
   - Repository application API secret: `lightrag_test_api_key`.
   - 2026-09-14 check: these names exist on `https://woodpecker.f123.pub`; runtime connection values were loaded from `.secrets/test.secrets`, while the application API key was generated as a repository secret because it is not a provider/database credential.
3. Add the missing repository release controls before declaring the pipeline ready: the GitHub repository currently has no rulesets, so protected release tags still need explicit configuration.
4. Do not expose runtime model, database or graph credentials to build steps. They are injected only into `deploy-test`, which creates or updates the Kubernetes Secret `lightrag-runtime` with `kubectl create secret ... --dry-run=client -o yaml | kubectl apply -f -`.

## Pipeline-managed test environment initialization

The `deploy-test` workflow creates or updates Kubernetes runtime Secrets and prepares the Kubernetes-side test environment on every `vX.Y.Z-test` deployment attempt while keeping the operation idempotent:

1. It writes the test kubeconfig from global `kubeconfig_test` and creates the `lightrag-test` namespace if missing.
2. It creates or updates `lightrag-registry-pull` from global `DOCKER_USERNAME` / `DOCKER_PASSWORD`.
3. It creates or updates `lightrag-runtime` from the Woodpecker repo secrets loaded from `.secrets/test.secrets`, including Bailian/OpenAI-compatible model API key and host, DashScope workspace header, PostgreSQL connection fields, HugeGraph REST endpoint, graphspace, graph and credentials.
4. It writes `lightrag-test-environment` with the expected test profile: two replicas, one worker per Pod, `PGKVStorage`, `PGDocStatusStorage`, `PGVectorStorage`, `HugeGraphStorage`, and matching `WORKSPACE` / `POSTGRES_WORKSPACE` set to the valid workspace identifier `lightrag_test` while the Kubernetes namespace remains `lightrag-test`.
5. It applies `k8s-deploy/lightrag-kustomize/overlays/test`, creating the ServiceAccount/RBAC, private Service, NetworkPolicy and the two `syno-nfs` RWX PVCs.
6. It waits for both PVCs to bind, rolls out the two application Pods, verifies image identity, health/authentication and cross-Pod ingestion/query before restoring Service routing.

The pipeline does not run repository tests and does not perform destructive data recovery or rollback. Existing storage fences, unknown writers, incompatible profiles or failed acceptance still stop the deployment for operator investigation.

## Release flow

1. Push/PR to `master`: no LightRAG Woodpecker pipeline is selected. Keep backend pytest, frontend Bun tests, frontend typecheck/lint/build scripts and external integration tests as local or separate-CI evidence.
2. Protected tag `vX.Y.Z-test`: the source workflow performs the only clone into container-local `/tmp`, runs delivery static checks, verifies repository/tag/commit/master ancestry, archives the cloned source, uploads the archive/checksum/record to the LightRAG COS path with `mc`, then downstream workflows download the archive from MinIO/COS to build and verify `docker-hub.f123.pub/lfun/lightrag` and deploy by digest to `lightrag-test` with pipeline-managed Secret/namespace/PVC preparation, Kustomize and `kubectl apply -k`, keeping Service routing paused.
   - The release-source step follows the working `../haier-demo` pattern: internal CI tools image, `git init`, no tag/submodule/LFS fetch, `--depth=1` for `CI_COMMIT_SHA`, and retry logging. It is implemented as a normal step rather than top-level `clone:` because strict Woodpecker lint only accepts server-allowlisted clone images. The Git worktree stays under `/tmp/lightrag-release-source`, not the Woodpecker NFS workspace, so `git reset --hard` does not materialize the repository through the shared workspace mount.
   - Image building is `scripts/ci/build-image.sh` inside the digest-pinned internal Kaniko debug image. It follows the working `../haier-demo` shape: registry auth is written only inside the step, cache layers go to `docker-hub.f123.pub/lfun/cache-lightrag`, Kaniko logs stream to Woodpecker stdout, `--snapshot-mode=redo` avoids full content hashing on large Python venv layers, and the produced image is labeled with the source commit. The Dockerfiles default Debian apt sources to Tsinghua before `apt-get update`, default Python dependency installs to the internal f123 PyPI mirror, keep `uv.lock` registry/artifact URLs on that mirror so frozen `uv sync` does not use `files.pythonhosted.org`, and default Bun/npm installs to `registry.npmmirror.com`. The Woodpecker release build does not install Rust/Cargo toolchains and does not download tiktoken or spaCy offline model caches during image build.
   - Image verification is `python -m scripts.ci.delivery resolve-image`, which resolves the registry digest and checks the image config revision label against the release commit.
   - Deployment is `scripts/ci/deploy-test.sh` inside the Kubernetes-capable CI tools image; it follows the haier-style shell-script deployment/status pattern, consumes `build/release/image.env` produced from the verified image record, writes kubeconfig from the global `kubeconfig_test` secret, applies the Kustomize overlay, waits for rollout, and prints Deployment/Pod/Service status.
3. Verify both Pods' `imageID`, per-Pod health and authentication behavior, plus cross-Pod ingestion/query with a unique test document.
4. Restore Service routing only after acceptance succeeds.
5. Tags `vX.Y.Z-pre` and `vX.Y.Z` publish and verify images but do not deploy.

A plain `master` push does not start Woodpecker by design. To exercise the build and deploy chain, create a protected `vX.Y.Z-test` tag after the scoped registry, COS, kubeconfig and `.secrets/test.secrets`-derived repository secrets are in place.

## Failure and rollback

If a release lock, old writer shutdown, storage fence, pending operation, image identity or acceptance check is uncertain, leave routing closed and preserve evidence. Do not force delete Pods, clear claims, bootstrap, recover or automatically start old writers. A rollback is a manual maintenance operation after operators verify no writes are active and the storage profile is compatible.

## Evidence status

Remote acceptance not yet run until tag-trigger authorization is configured and a real Woodpecker pipeline records pipeline ID, commit, tag, digest, pipeline-managed environment preparation, per-Pod image IDs and business acceptance results.

2026-09-14 diagnostic note: tag `v1.5.23-test` / Woodpecker pipeline #31 confirmed the source archive upload/download path, rootless BuildKit startup, and Tsinghua Debian apt source rewrite. The Woodpecker UI log for the build step stopped at `#23 ... Fetched 80.6 MB in 45s`, but Kubernetes-side observation showed that step had continued through package installation and finished later; the visible line was stale/incomplete progress, not the final command state. The same run also exposed two Dockerfile issues: Rust used `https://sh.rustup.rs` and its `curl | sh` pipeline could hide a download/DNS failure, while `uv sync` still used public PyPI/Fastly by default. The Dockerfiles now default uv/pip to `https://mirror.f123.pub/repository/pypi/simple`, keep the other package sources explicit, and fail fast on rustup download errors, but a new tag pipeline is still required for end-to-end remote proof.

2026-09-14 diagnostic note: tag `v1.5.25-test` / Woodpecker pipeline #33 stalled before build in the old `clone-source` step. The Git fetch returned, but Kubernetes showed `git reset --hard` blocked in uninterruptible NFS wait while checking out into `/woodpecker/src`. The source workflow now combines clone, static validation, archive verification and upload into one `release-source` step that uses `/tmp/lightrag-release-source` for the worktree and leaves `/woodpecker` out of Git checkout.

2026-09-14 diagnostic note: tag `v1.5.26-test` / Woodpecker pipeline #34 verified the `/tmp` release-source checkout and COS source upload. The next build step then showed `uv sync` still connecting to public Fastly because `uv.lock` retained `https://files.pythonhosted.org/` artifact URLs; `UV_DEFAULT_INDEX` alone does not rewrite locked distribution URLs during frozen sync. The lock file now uses `https://mirror.f123.pub/repository/pypi/simple` and `https://mirror.f123.pub/repository/pypi/packages/` URLs.

2026-09-14 diagnostic note: tag `v1.5.28-test` / Woodpecker pipeline #36 proved the clone/NFS fix remotely: `release-source` cloned into `/tmp/lightrag-release-source`, uploaded the source archive, and finished in about 69 seconds; `build-image > download-source` finished in 13 seconds. The Kaniko build then exposed two unnecessary build inputs: Dockerfile explicitly installed `rustup`/Cargo although the repository has no Rust sources and the locked amd64 dependency set installed from wheels, and the main image build ran `lightrag-download-cache`, which attempted OpenAI blob/GitHub spaCy downloads from inside the build. The pipeline was stopped after this root cause was confirmed; the release Dockerfiles now remove those build-time downloads/toolchains.

2026-09-14 diagnostic note: tag `v1.5.29-test` / Woodpecker pipeline #38 confirmed dependency installation proceeds without Cargo/GCC and without model-cache downloads, but then spent several minutes in Kaniko's default full filesystem snapshot of the large `.venv` layer. The build script now passes `--snapshot-mode=redo` to avoid full content hashing while keeping Kaniko cache behavior.

2026-09-14 diagnostic note: tag `v1.5.30-test` / Woodpecker pipeline #39 confirmed `--snapshot-mode=redo` was present, but the old Python builder stage still forced Kaniko to save `/root/.local`, `/app/lightrag` and the large `/app/.venv` for later stages. The release Dockerfiles now install locked Python dependencies directly in the final image stage and no longer copy `.venv` from a Python builder stage.

2026-09-14 diagnostic note: tag `v1.5.31-test` / Woodpecker pipeline #41 passed source archive download, complete Kaniko image build and pre-deploy image verification. The build pushed digest `sha256:42df257a4bb8a244633bceb6b96fd7495a8ab262f43a89efa9ab0bbea927e8a9`. Its deploy step then exposed an environment-contract bug: `resolve-image --env-output` wrote plain shell assignments, but the workflow sourced that file before launching `deploy-test.sh` as a child shell, so `LIGHTRAG_IMAGE_DIGEST` was not exported. The env output now writes exported variables.

2026-09-14 diagnostic note: tag `v1.5.32-test` / Woodpecker pipeline #42 passed source archive, image build, pre-deploy image verification and deploy image-env propagation. The build pushed digest `sha256:575beb3cf5c93be229b94cc00d0f7cbf581a33d96fe73cbfe58f187242da296f`. The deploy step then failed at the old first-time environment gate because the test cluster did not yet have namespace `lightrag-test`. The deploy workflow now consumes `.secrets/test.secrets`-derived Woodpecker repo secrets and prepares namespace, registry/runtime Secrets, profile snapshot and PVCs inside the pipeline before rollout.
