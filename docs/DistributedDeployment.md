# Distributed workspace writers: deployment and operator recovery

This fork offers **opt-in concurrent writers, not automatic HA**. Default local
mode still permits only one ingesting instance per workspace. Read
[the runtime contract](design/DistributedRuntimeContract.md),
[the pipeline contract](design/DistributedPipelineContract.md), and
[the purge contract](design/PurgeRecoveryContract.md) before rollout or recovery.

## Supported profile and trust boundary

Use `PGKVStorage`, `PGDocStatusStorage`, `PGVectorStorage`, `HugeGraphStorage`,
a nonempty matching `WORKSPACE`/`POSTGRES_WORKSPACE`, one stable
`LIGHTRAG_DEPLOYMENT_ID`, and the **same coordination database** for every writer.
The business PG schema is `public`. Provision pgvector and compatible business
schemas under DBA control. HugeGraph must have the LightRAG schema; see
[HugeGraphStorage.md](HugeGraphStorage.md). Normal replicas verify, never migrate.
The explicit bootstrap may create missing current tables/indexes/schema, but
cannot convert incompatible shared tables or switch embedding models for you.

`LIGHTRAG_COORDINATION_POOL_MODE=direct` is recommended; `session` is supported
only after verifying the actual pooler configuration. Transaction/statement
pooling is incompatible with session witness lifetime.
[PgBouncer pool modes](https://www.pgbouncer.org/config.html#pool_mode) and
[PostgreSQL advisory locks](https://www.postgresql.org/docs/current/explicit-locking.html#ADVISORY-LOCKS)
explain those connection lifetimes. Durable ownership/pending rows—not the
session lock—retain abandoned work. No heartbeat or timeout grants takeover.

All replicas and maintenance jobs mount the same persistent filesystem at
`/app/data/inputs` and `/app/data/rag_storage`. The runtime fingerprints resolved
paths, backend targets and embedding model/dimension. `LIGHTRAG_SHARED_STORAGE=true`
is an operator assertion, **not a filesystem probe**. PVC access modes also do
not implement application write coordination; verify the actual volume provider
supports simultaneous multi-node mounts and coherent file operations.
[Kubernetes access modes](https://kubernetes.io/docs/concepts/storage/persistent-volumes/#access-modes)
describe that distinction. EmptyDir, independent per-Pod claims, and file-backed
business databases do not satisfy this profile.

Use UTC/NTP on application nodes and PG: one-shot retry selection compares
`doc_status.updated_at` with the request's database timestamp. Future/newer
versions are conservatively skipped, not granted another attempt.

## Build, secrets, permissions, and network

Build and publish **this reviewed feature commit**, then use an immutable tag
(enforce tag immutability in your registry). The upstream `latest` image lacks
this feature; the sample repository/tag is deliberately not a published image.

```sh
FEATURE_COMMIT=$(git rev-parse HEAD)
docker build -t "$REGISTRY/lightrag:$FEATURE_COMMIT" .
docker push "$REGISTRY/lightrag:$FEATURE_COMMIT"
```

The Dockerfile installs locked API/offline extras including asyncpg/pgvector.
Image build/cache downloads need build-time network access. Runtime cloud model
calls separately need controlled outbound access; no deterministic model fixture
is shipped in production.

Use `k8s-deploy/lightrag/values-distributed.yaml` as your operator values base.
Supply an externally managed `lightrag-distributed-credentials` Secret through
`envFrom.secrets`, containing `LIGHTRAG_COORDINATION_DSN`, `POSTGRES_PASSWORD`,
HugeGraph credentials if required, `LLM_BINDING_API_KEY`,
`EMBEDDING_BINDING_API_KEY`, and API authentication (`LIGHTRAG_API_KEY` and/or
`AUTH_ACCOUNTS`). Do not commit credentials or put them on command lines. The
mounted chart `.env` uses dotenv `override=False`: process Secret env values
win even when mounted values are blank. Render validation checks chart-declared
settings; an external Secret must not override workspace/backend/path settings.
Runtime profile/manifest checks are the final guard, not a replacement for review.

Pre-provision both RWX roots writable by UID/GID 1000. The sample sets non-root
UID/GID 1000, fsGroup 1000 and no privilege escalation. Verify create/read/rename/
exclusive-create from Pods on different nodes, including the workspace
subdirectories; do not trust `chown` failure suppression in the entrypoint.
Non-root avoids every Pod recursively changing ownership on the shared volume.

Service defaults to ClusterIP. It is an internal routing choice, **not an ACL or
NetworkPolicy**. This chart creates neither Ingress nor NetworkPolicy. Separately
configure private ingress/auth and namespace/network controls; permit application
outbound traffic to PG/HugeGraph, DNS and configured model endpoints. Cloud LLM
egress does not require a public inbound API. SDK/recovery tools use operator
backend credentials; this feature adds no workspace user ACL.

The distributed chart requires one Uvicorn process per Pod (`WORKERS=1`); scale
Pods for this tested profile. `python -m lightrag.api.lightrag_server` is single-process, while the
separate Gunicorn launcher can use multiple workers. Its preloaded constructor
must remain IO-free: pools/witnesses are initialized in each worker's lifespan,
never in a pre-fork initialized rag. Independent-process acceptance is not a
Gunicorn preload or Kubernetes scheduling test.

## Explicit bootstrap and rollout

1. Back up PG business and coordination databases, HugeGraph, and shared files.
   Stop **all** old writers: Deployments, old ReplicaSets, jobs, SDK clients,
   query processes that write cache, external importers and repair tools. Do not
   mix local/distributed versions on a workspace. Stop ingress first, then drain.
2. Confirm all backend requests actually finished (PG transaction/activity
   inspection plus HugeGraph request/task telemetry and client shutdown evidence).
   Absence of Pods or a fixed sleep is not proof that remote requests ended.
3. Migrate incompatible legacy schemas/data offline, preserving full documents,
   chunks/cache references, curated tracking and full entity/relation anchors.
   Moving old RWO data requires **new RWX claims and an offline data copy**;
   changing immutable PVC accessModes in Helm does not migrate existing storage.
4. Review operator values, external Secrets, identical mount paths and physical
   targets. Verify the RWX provider on the actual cluster. This repository's
   Helm tests only render manifests; they do not establish that cluster fact.
5. Run coordinator migration and business bootstrap exactly once as an explicit
   maintenance action. CLI configuration comes from the same server environment
   (including `.env`); no server CLI flags are accepted by `bootstrap`:

```sh
python -m lightrag.distributed migrate
python -m lightrag.distributed bootstrap --actor platform-operator \
  --confirm-writers-stopped --confirm-inflight-finished
```

The bootstrap constructs `create_app(args).state.rag` without entering lifespan,
then enters `distributed_maintenance()` **before** storage initialization and
`check_and_migrate_data()`. Regular API startup verifies only. A failure does not
clear pending state or pretend finalization confirmed uncertain work.

Alternatively use the chart's explicit, non-hook Job, with the same image/env/
mounts/security context. First scale the old workload to zero and **wait for all
old Pods/requests to finish**; the Job's flags declare those actual checks, not
perform them. Keep `replicaCount=0` while it runs. Example operator commands
(`operator-values.yaml` must contain your pinned image and reviewed configuration):

```sh
helm lint k8s-deploy/lightrag -f operator-values.yaml
helm template lightrag k8s-deploy/lightrag -f operator-values.yaml > rendered.yaml
# After old writers and backend requests have actually stopped:
helm upgrade --install lightrag k8s-deploy/lightrag -f operator-values.yaml \
  --set replicaCount=0 --set maintenance.enabled=true \
  --set maintenance.writersStopped=true --set maintenance.inflightFinished=true
kubectl wait --for=condition=complete job/lightrag-bootstrap --timeout=30m
kubectl logs job/lightrag-bootstrap
# Only after positive success and review:
helm upgrade lightrag k8s-deploy/lightrag -f operator-values.yaml \
  --set maintenance.enabled=false --set replicaCount=2
```

The Job has `backoffLimit: 0`, no automatic migration hook/retry. If it failed,
inspect/audit before a new attempt. Remove the old completed/failed Job explicitly
only after saving logs before rerunning (Job pod specs are immutable). The chart
rejects distributed StatefulSet, missing/RWO storage, unsafe backends, mismatched
workspace, unsupported pooling, and upstream/unpinned mutable image choices.
Existing shared claims render no new PVC; to provision new claims, clear each
`existingClaim` and select a real RWX `storageClassName` (empty class selects no
dynamic provisioner). New claims keep the Helm retention annotation. Default local Deployment/RWO/one replica is unchanged.

`/health` remains useful for diagnostics even when writes are fenced. Use
`/documents/pipeline_status` for `distributed.fenced`, `recovery_required`,
`polling_error`, pause and claims. Do not create restart loops treating a durable
fence as recoverable by restart. Graceful shutdown drains admitted work; the
sample grace period is 600 seconds, which operators must size to real backend/
LLM latency. Deadline SIGKILL deliberately requires the recovery procedure.

## Upgrade and rollback

Use a stopped-writer maintenance window for feature/schema/manifest changes,
not mixed-version rolling writes. Recreate alone does not stop external clients
or prove backend quiescence. Preserve backups and coordinator history, explicitly
migrate/verify using the new pinned image, then start new replicas.

Rollback is also stopped-writer work: stop **all distributed writers**, confirm
all requests ended, inspect for pending/active/orphaned operations, audit and
recover if needed, and only then start **one** compatible local writer with
`LIGHTRAG_DISTRIBUTED_WRITES=false`. Never drop coordination tables to make a
rollback start. Restore a coherent backup only under an explicit data-loss/
reconciliation decision, not as an automatic compensation.

## Fence recovery: inspect, offline audit/repair, then recover

A generation is not a HugeGraph server fence. Already sent requests can still
commit; time, TTL, witness disappearance or scaling to zero does not prove their
completion. Stop every writer and confirm backend quiescence first. If that
cannot be established, **do not recover**.

```sh
umask 077
python -m lightrag.distributed inspect \
  --deployment-id "$LIGHTRAG_DEPLOYMENT_ID" --workspace "$WORKSPACE" > before.json
```

Save this snapshot, backend logs/exports and an operator audit record outside the
mutable workspace. Inspect `operations.metadata.tracking_recovery`,
`custom_kg_targets`, document claims, mutation pending/ACK and phases. Unknown ACK
means “may have committed”, not “absent”. Recovery snapshots/history grow; retain
and archive under an operator policy, never delete active evidence to save space.

### Executable offline audit and repair path

For document ingestion failure with intact anchors, inspect actual graph/chunks/
tracking and choose the existing **post-recovery** retry or purge path. Do not
call raw repair tools in a running distributed Pod: they correctly refuse ungated
writes. For rename/edit/merge residues requiring repair **before** fence release:

1. Using a protected PG service/password file (`PGSERVICE`/`PGPASSFILE`, not a
   password-bearing command argument), export the exact workspace rows. Example:

```sh
psql -X -v ON_ERROR_STOP=1 -v workspace="$WORKSPACE" <<'SQL'
BEGIN READ ONLY;
SELECT * FROM lightrag_entity_chunks WHERE workspace=:'workspace';
SELECT * FROM lightrag_relation_chunks WHERE workspace=:'workspace';
SELECT * FROM lightrag_full_entities WHERE workspace=:'workspace';
SELECT * FROM lightrag_full_relations WHERE workspace=:'workspace';
SELECT * FROM lightrag_doc_status WHERE workspace=:'workspace';
COMMIT;
SQL
```

2. Compare those exact keys against actual HugeGraph objects (read API or graph
   console using this workspace's scope) and the recorded edit/merge intent.
   Preserve live-object curated tracking, including deliberately empty rows.
   Staged supersets may be reduced only after their final object/anchor ownership
   has been positively established. Never reconstruct authoritative tracking from
   graph `source_id`, infer `kg_write_state`, or delete anchors/chunks to hide a
   partial write. If intent cannot be reconstructed, retain the fence for manual
   investigation instead of guessing.
3. For a **positively absent object and its recorded obsolete key only**, a
   scoped, reviewed SQL transaction can retire the tracking row. For a live
   object, write the audited chunk-ID set, not a graph-derived replacement:

```sh
# Supply the exact recorded/audited key and JSON list as operator variables.
psql -X -v ON_ERROR_STOP=1 -v workspace="$WORKSPACE" \
  -v tracking_key="$AUDITED_TRACKING_KEY" -v chunks="$AUDITED_CHUNKS_JSON" <<'SQL'
BEGIN;
SELECT * FROM lightrag_entity_chunks
 WHERE workspace=:'workspace' AND id=:'tracking_key' FOR UPDATE;
UPDATE lightrag_entity_chunks
 SET chunk_ids=:'chunks'::jsonb, count=jsonb_array_length(:'chunks'::jsonb)
 WHERE workspace=:'workspace' AND id=:'tracking_key';
COMMIT;
SQL
```

   Use `lightrag_relation_chunks` for a recorded relation tracking key. To retire
   a positively absent object's row use `DELETE ... WHERE workspace=... AND
   id=...` instead, never a workspace-wide drop. Store before/after rows and the
   reason. These are intentional operator bypasses under stopped-writer and
   quiescent-backend guarantees, not application fallback paths.
4. Rebuild vectors from the audited **final graph and retained text chunks**.
   The existing interactive tool provides a real offline path:

```sh
# Dedicated operator shell/container, exact SAME profile, workspace, model and paths.
# Explicitly disabling the guard is allowed ONLY while every writer/request is stopped.
LIGHTRAG_DISTRIBUTED_WRITES=false HUGEGRAPH_AUTO_CREATE_SCHEMA=false \
  python -m lightrag.tools.rebuild_vdb
```

   Confirm stopped servers, choose `[1]` consistency check, then `[2]` graph
   vectors or `[4]` all vectors as the audit requires; confirm the stated targets,
   then `[1]` again and `[0]`. Keep the exit status and full report; partial errors
   are failure. This tool drops/rebuilds only selected workspace vector records,
   invokes the configured embedding provider (possible cost/egress), and uses raw
   backend initialization. Therefore **first verify current compatible PG schema**
   under DBA control, and stop other users of shared tables if initialization
   needs schema work. Do not use this as an unreviewed schema-migration shortcut.
   Do not run `chunk_tracking_repair` to overwrite curated authoritative tracking.
   The bypass neither changes nor clears coordinator history/generation/fence.
5. Compare final graph/vector/PG state and retained anchors again. Only after the
   audit/necessary repair is complete, with all old requests finished, recover:

```sh
python -m lightrag.distributed recover \
  --deployment-id "$LIGHTRAG_DEPLOYMENT_ID" --workspace "$WORKSPACE" \
  --expected-generation "$AUDITED_GENERATION" --actor platform-operator \
  --reason 'Change record: stopped writers, verified backend quiescence, audited/repaired state' \
  --confirm-writers-stopped --confirm-inflight-finished --confirm-state-audited
```

All three confirmations and the inspected generation are mandatory. Recovery
advances generation and retains an audit snapshot; it does not repair business
data or restart workers. Start one controlled writer first, then perform the
selected explicit retry (`/documents/reprocess_failed`, SDK
`apipeline_request_retry()` plus processing) or document purge
(`adelete_by_doc_id`) and verify convergence before scaling out. Interrupted
non-FAILED documents can resume via the strict scheduler; FAILED never retries
without explicit intent. Reusing a completed retry ID does not grant extra tries.
`/documents/recovery/force_reset` cannot clear this durable fence.

## What has and has not been validated

`tests/setup/test_distributed_chart.py` executes real Helm rendering and parses
Kubernetes YAML. `tests/distributed/test_bootstrap_cli.py` executes the production
bootstrap CLI against isolated real PG/HugeGraph without model calls.
`tests/distributed/test_process_acceptance.py` uses independent spawned OS
processes, independent Managers and pools, production parser/extractor/merge/
purge, deterministic test-only models, overlapping real graph requests and
post-commit ACK-loss/SIGKILL faults. It does not kill backend services.

These tests **are not a kubectl multi-Pod test**. Cluster scheduling, RWX provider
semantics, Secrets delivery, real egress policy, node failure, Gunicorn preload,
and production load/capacity must be validated by the deployment operator.
