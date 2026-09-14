## Purpose

为 LightRAG fork 提供可追溯且不运行仓库测试套件的 Woodpecker 交付静态校验、可信源码与镜像交付，以及遵守 HugeGraph 分布式写入和显式维护合同的 Kubernetes 内网测试环境自动部署，防止错误分支发布、凭据泄漏、未验证产物上线和不安全混合版本写入。

## ADDED Requirements

### Requirement: All Dockerfile image inputs are locally mirrored before use

Every repository Dockerfile MUST reference external image inputs only under docker-hub.f123.pub/base/, including its frontend and external copy sources. The operator SHALL copy and verify those images from the local machine before updating the Dockerfiles, preserving the source digest and full platform set without overwriting unrelated shared tags. CI MUST NOT silently fetch missing base images from an upstream registry.

#### Scenario: Base image is updated
- **WHEN** an external base image or frontend is introduced or refreshed
- **THEN** its complete content is first mirrored and verified locally, its source and destination identity is recorded, and only then is the internal digest-pinned reference committed

#### Scenario: Local machine and CI use different architectures
- **WHEN** the operator mirrors on an arm64 machine for an amd64 build agent
- **THEN** the destination retains both source platforms and their unchanged manifests instead of containing only the local platform

#### Scenario: Internal image is unavailable
- **WHEN** an internal base image cannot be read or fails digest verification
- **THEN** the build fails without an automatic upstream-registry fallback

### Requirement: Release events are scoped to the fork and protected source

The delivery system SHALL run only for supported release tag events and MUST NOT trigger on master pushes or pull requests. The release-source workflow SHALL perform the only repository clone, run static delivery checks without executing repository test suites in Woodpecker, upload the source archive/checksum/record to the LightRAG MinIO/COS path, and require later workflows to download that package instead of cloning again. It MUST publish only supported version tags whose resolved commit matches the event and belongs to the fork master history, without changing main. Only test-suffixed version tags SHALL automatically deploy to the confirmed test namespace.

#### Scenario: Plain master update
- **WHEN** `master` receives a push or a pull request targets `master`
- **THEN** no LightRAG Woodpecker workflow is selected and no release, registry, object-storage or deployment credential is exposed by Woodpecker

#### Scenario: Test release from master
- **WHEN** a protected `vX.Y.Z-test` tag resolves to the event commit in fork master history
- **THEN** the release is eligible for source upload, quality checks, image publishing, and deployment to `lightrag-test` after every preceding gate succeeds

#### Scenario: Downstream release workflows consume uploaded source
- **WHEN** build, image verification or test deployment workflows run after source validation
- **THEN** they skip repository clone, download the source archive from the LightRAG MinIO/COS path, verify its checksum, extract it, and use that extracted source for their scripts and build context

#### Scenario: Unsupported or unprovable source
- **WHEN** a tag is invalid, moved, from an unapproved repository, or its master ancestry cannot be established
- **THEN** no releasable image is published and no environment is deployed

#### Scenario: Pre or stable release
- **WHEN** a valid `vX.Y.Z-pre` or `vX.Y.Z` tag passes quality and artifact validation
- **THEN** the image is published and verified without deploying any environment

### Requirement: Static delivery failures block all release side effects

The delivery system MUST require static delivery-script checks, workflow validation, source identity validation, image validation, and Kustomize manifest validation for the exact release commit. Woodpecker MUST NOT execute repository test suites such as backend pytest, frontend Bun tests, frontend typecheck/lint/build check scripts, or external-service integration tests. Local or separate-CI test results MAY be recorded as separate evidence but MUST NOT be represented as Woodpecker gates.

#### Scenario: Static delivery validation fails
- **WHEN** any mandatory static delivery check, workflow validation, source identity check, image validation or manifest check fails
- **THEN** publishing and deployment do not run and the failure is reported with the failing gate

#### Scenario: Repository tests are not a Woodpecker gate
- **WHEN** a release tag runs through Woodpecker
- **THEN** the workflow does not invoke repository test commands and the delivery report identifies local or separate-CI tests as outside the Woodpecker evidence set

### Requirement: Build and deployment credentials are separated

The delivery system MUST keep runtime credentials, repository-local secret files and private data out of source artifacts, image layers and logs. Non-tag events MUST NOT receive publishing or deployment credentials because they do not select Woodpecker workflows for this repository. Test deployment credentials SHALL be limited to the dedicated test namespace and MUST NOT authorize changes to other applications or production environments.

#### Scenario: Non-tag validation
- **WHEN** code is updated without a supported release tag event
- **THEN** no LightRAG Woodpecker step runs and no COS publishing credential, registry push credential, runtime credential or deployment credential is made available by Woodpecker

#### Scenario: Missing or malformed credential
- **WHEN** a required release credential is missing or malformed
- **THEN** its owning step fails without printing its value or proceeding anonymously

### Requirement: Source and image identity remain traceable

The delivery system SHALL bind source archives and release records to repository, commit, version tag and pipeline identity. It MUST verify archive integrity and safe extraction, validate image platform and source revision, and deploy the verified image by immutable digest rather than by a mutable tag lookup.

#### Scenario: Archive or image identity mismatch
- **WHEN** an archive checksum, identity record, image revision, digest or target platform differs from the expected release
- **THEN** the consumer rejects the artifact before building or deploying it

#### Scenario: Version tag already exists
- **WHEN** a requested version is already published
- **THEN** retries verify and reuse the matching recorded artifact, while conflicting source or digest information is rejected without overwriting the version

#### Scenario: Digest deployment
- **WHEN** a verified image is promoted
- **THEN** all application replicas use that digest and acceptance verifies the running image identities against the validated artifact

### Requirement: The test environment preserves the supported distributed profile

The delivery system SHALL target the confirmed test cluster and lightrag-test namespace, run two application replicas with one process per replica, and preserve shared PostgreSQL/vector/status, HugeGraph, workspace and filesystem configuration. It MUST reject missing prerequisites, incompatible profiles and unverified shared storage rather than fall back to local storage or an unauthenticated single replica.

#### Scenario: Test environment not initialized
- **WHEN** required runtime credentials, backend schemas, registered workspace or shared persistent storage are absent
- **THEN** automatic deployment fails with actionable prerequisites and does not execute bootstrap, migration or recovery

#### Scenario: Real shared storage acceptance
- **WHEN** the test environment is first enabled for automatic deployment
- **THEN** shared filesystem operations by the application UID on different cluster nodes are verified and recorded rather than inferred from PVC access modes

### Requirement: Test services are private and authenticated

The delivery system MUST NOT create public ingress, NodePort or LoadBalancer exposure for the test application. The test environment SHALL require API authentication and declared internal access controls while allowing the explicitly configured backend, DNS and model-service outbound connections.

#### Scenario: Private test deployment
- **WHEN** a test release is deployed
- **THEN** the service remains internally routed, protected endpoints reject unauthenticated clients, and no public entry point is added

### Requirement: Automatic upgrades cannot mix writers or erase uncertainty

The delivery system MUST serialize environment changes, prevent older releases overwriting newer successful releases, stop new routed traffic, gracefully stop old replicas, and establish completed coordinated writes before starting new writers. It MUST NOT infer backend quiescence solely from elapsed time or absent Pods, and MUST NOT clear fences, unknown writes, claims or history to make an upgrade pass.

#### Scenario: Concurrent releases
- **WHEN** two pipelines attempt to change the same test deployment
- **THEN** only one owns the deployment transition and the competing pipeline cannot overwrite its changes

#### Scenario: Interrupted release lock
- **WHEN** an earlier release leaves ownership or completion uncertain
- **THEN** a later pipeline refuses takeover until an operator verifies and resolves the prior deployment state

#### Scenario: Unsafe storage state or shutdown
- **WHEN** an old writer is forcibly terminated, coordinated operations remain active or orphaned, writes are unconfirmed, or backend completion cannot be established
- **THEN** promotion fails, business routing stays closed, and evidence is retained for explicit operator investigation without automatic recovery or rollback

#### Scenario: Incompatible new version
- **WHEN** the candidate image requires a schema, manifest or storage-profile migration
- **THEN** ordinary test deployment stops and requires the existing explicit maintenance procedure

### Requirement: Successful deployment requires functional acceptance

The delivery system MUST verify both replicas, running image identity, health, authentication, distributed state, cross-Pod ingestion and query behavior before declaring a release successful. It SHALL use real configured backends and models and record identifiers and outcomes without leaking secrets or unrelated document contents.

#### Scenario: Healthy compatible upgrade
- **WHEN** both new replicas pass per-Pod and cross-Pod acceptance
- **THEN** business routing is restored, the internal service path is verified, and a successful release record links commit, tag, pipeline, digest and acceptance results

#### Scenario: Acceptance failure
- **WHEN** image identity, API behavior, authentication, storage state or cross-Pod behavior fails validation
- **THEN** the pipeline reports failure, preserves diagnostic evidence and does not report test availability or automatically restore old writers

### Requirement: Initial setup and operational evidence are explicit

The delivery SHALL include reproducible instructions for CI onboarding, scoped credentials, test prerequisites, explicit initialization, release, failure investigation and controlled rollback. It MUST distinguish proposal or static validation from actual remote build and deployment verification.

#### Scenario: Remote acceptance has not run
- **WHEN** credentials, prerequisites or release-trigger authorization prevent remote verification
- **THEN** delivery reports those exact incomplete checks rather than claiming the automated test deployment is usable
