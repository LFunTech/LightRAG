# Distributed runtime and physical writes

This contract applies only when `distributed_writes=True`. The default local
contracts, including cancellation deferral, remain unchanged.

## Ownership and actual commits

A shared/exclusive operation is a durable permit, not an expiring lease. Core
GraphDB locks cover the complete read/modify/write, including actual PG vector
persistence. Each distributed vector upsert/delete uses a private per-call batch;
the default process-wide deferred buffer is never published on its behalf.
Every PG transaction/SQL mutation and HugeGraph acknowledged batch has its own
pending/ACK journal. A failed response is not proof of rollback and must not be
replayed by a hidden connection retry. Reads retain their connection retries.

Storage mutation methods reject calls without a live runtime operation. Child
tasks may inherit a live permit, but may not borrow their parent's keyed-lock
ownership. They must finish before the operation exits; late inherited permits
are rejected. Native parser threads and LLM queues carry the caller's context.

## Coordinator diagnostics and cold SDK setup

Coordinator quarantine is permanent for that object, with or without the
optional release protocol. Before hiding driver details from public errors,
record the first failure's exception class, validated SQLSTATE (or `unknown`),
connection purpose, Pod/workspace/deployment/owner, actual connection PID (or
unknown), separate witness PID, command timeout and UTC quarantine time. Do not
log exception text, SQL, DSNs, credentials or business content. Bound and sanitize
labels; do not emit a new log for each rejected request. A missing transaction
PID must not be replaced with an unrelated witness PID. Normal close is not a
fault. Diagnostic-handler failure cannot prevent marking broken and terminating
both transports; it is not permission to resume or release durable evidence.

Distributed polling logs its stop time and safe scope/reason once when a
coordination error ends the polling task, even when release is disabled. This
stop time is distinct from quarantine time. Busy admission remains a retryable
scheduling condition, not a permanent stop. Neither diagnostic reinstates a
failed coordinator, retries an unknown write, or changes claims/generation.

OpenAI-compatible embedding and chat bindings prepare their per-call SDK client
and lazy resource properties in a worker, not on the coordinator's event loop.
SDK cold imports/model construction can otherwise block unrelated driver timers
and provoke fail-closed quarantine even with an in-memory HTTP transport.
Actual async HTTP/stream operations and closure stay on the caller's loop;
ContextVars propagate into setup, and clients are never shared across calls.
Cancellation joins setup and closes any constructed client before returning,
including repeated cancellation during setup/close. It does not abandon a worker
or release its resource owner early. A setup failure is propagated after cleanup.

This is not global SDK caching, a new retry policy or a timeout increase. Worker
queueing/GIL contention and other synchronous work still require measurement.
The enclosing model-role stage includes setup wait; `provider_open` now measures
SDK dispatch without lazy resource setup, so do not compare that stage alone to
old cold-start runs. Local regressions establish a possible blocking/timeout
mechanism, not the lost cause of a historical database exception or a production
latency/capacity guarantee. Remote failures require separately authorized tests;
already-accepted work remains subject to the recovery rules below.

## Maintenance, cancellation, and recovery

Normal initialization verifies PG columns, primary keys and vector dimensions,
and HugeGraph schema, without DDL or data migration. With the release protocol
**disabled**, provision new tables/schema inside the SDK's explicit `distributed_maintenance()` context, then call
`initialize_storages()` and `check_and_migrate_data()`. Existing incompatible PG
business schemas must be migrated offline before opt-in; maintenance does not
silently alter them or copy data from a different embedding model's tables.
Maintenance checks existing vector schemas before any table/index DDL; compatible
schemas allow only additive index creation, never type conversion or old-index
removal. A workspace permit is not authority to migrate a shared table.
Finalization rejected or cancelled before exclusive admission leaves the runtime,
business connections and coordinator witness open for existing operations.
Normal startup refuses legacy tracking/anchor migration needs. Coordination
schema itself is provisioned separately by the versioned migration CLI.

With the production release protocol enabled, maintenance is also verify-only:
it never provisions extensions, business/vector tables or indexes, or graph
schema. Use the separate versioned `lightrag.provisioning` operator phases in
[ProdDelivery](../ProdDelivery.md#独立版本化初始化工具已实现真实执行另行授权).
The business PG connection verifies the migration ledger, current structure,
runtime privileges and a pre-registered compatible workspace before coordinator
initialization. Frozen migration resources are shipped in both wheel and sdist.
A graph POST has durable per-definition intent outside any PG transaction;
unknown results are not replayed or removed. PG can remain committed while the
graph is incomplete: fail closed and audit, never synthesize cross-store rollback.
The operator's maintenance assertion must exclude external writers as well as
registered ones. Read-only metadata cannot prove this exclusion by itself.

The graph edit/delete/merge cancellation-deferring region runs in the owning
asyncio Task in distributed mode. Forking a shielded child there would make it
wait for its own parent's durable entity lock. Cancellation instead propagates,
retains the pending/lock/operation evidence, and durably fences the workspace.
This never licenses deletion of attribution before confirmed graph removal.

Accepted residue: graph removal/shrink may have committed while tracking rows
still describe the removed objects or wider evidence. New/target attribution is
staged first, so this residue contains surplus provenance rather than losing it.
Before the first immediate graph removal/shrinking upsert (not merely before an
`index_done_callback`), `operation.metadata.tracking_recovery` records
the exact namespace and key of every row to retire/reconcile. Durable resource
locks retain the affected entity names too. This evidence survives process death;
logs and retrying rename are not a recovery mechanism (the target may exist).

Recovery requires all writers stopped and all backend requests quiescent. Use
coordinator inspect and the recorded tracking keys to compare authoritative PG
tracking against actual HugeGraph objects and the document write-ahead anchors.
For each absent object, retire only its recorded obsolete tracking row; for live
objects preserve authoritative curated attribution (including empty rows), and
reconcile documented staged supersets against the confirmed edit/merge and
anchors. Rebuild affected vector records from the audited final graph, never from
an old pending buffer. Do not blindly retry rename, seed tracking from graph
`source_id`, delete attribution carriers to hide a failure, or run an ungated
repair while any writer lives. Only after this operator audit/repair completes
may the explicit recovery CLI advance generation and release the fence; retain
its snapshot and recovery audit. No automatic repair or timeout-based takeover
is provided by this profile.

## Shared chunk cache attribution

In distributed PGKV storage only, `text_chunks` UPSERT atomically unions distinct
`llm_cache_list` values with the existing row. NULL/missing lists are empty and
repeated keys are deduplicated. This protects a content-addressed chunk shared by
several documents from stale concurrent cache-reference snapshots. Other chunk
columns retain their existing UPSERT semantics. References are retired with the
chunk row, not by replacing its list with a stale/empty snapshot. A dangling
reference after cache clear is harmless and preferable to an unreachable cache
row containing document text. Default local replacement semantics are unchanged.

## Opt-in production release handover

`LIGHTRAG_RELEASE_ENABLED` is off by default. The enabled profile requires one
worker, distributed writes, an explicitly enrolled shared scope and the separate
`lightrag_release` schema. Normal initialization verifies that schema and runtime
privileges; it never migrates it. Existing coordination SQL/checksums and the
default test/local lifecycle are unchanged. The operator role is not the
application role; application roles cannot update release control tables.

The durable gate and writer admission serialize on the scope row. Release
tickets cover admission gaps and every admitted business/cache write; original
operation, keyed-lock and physical pending/ACK protections remain authoritative.
A detached task obtains its own durable ticket before takeover, even when its
parent command has an unbound ticket. Parent HTTP completion cannot invalidate a
child's writer permission. Unknown backend results preserve tickets/evidence;
neither elapsed time nor another runner grants takeover or replay authority.

Read-only queries do not wait for the release gate. Optional query/keyword cache
fills acquire a writer ticket before sending; no authority means an observable
skip, not a swallowed storage error. Already-sent writes must drain. Extraction
cache attribution keeps the existing reference-before-row ordering. Necessary
accounting/audit effects are not optional caches: activation refuses an attached
unverified accounting authority rather than silently dropping its events.

Stream ownership lasts through actual iteration and closure, not merely return
of an iterator. Unconsumed OpenAI streams still own their already-open response
and client. HTTP disconnect cleanup joins in-flight iteration before returning
the provider concurrency slot. Uncertain closure retains backend activity and
blocks retirement. SDK users must exhaust or explicitly close returned streams.

Release-enabled finalization drains only this instance's HTTP, queries, waiters,
background tasks, writers and backend activity, checks durable quiescence, then
closes its resources/witness. It does not take workspace maintenance exclusion
against healthy peers. Persisted retirement triggers native Uvicorn listener
shutdown with no artificial cancellation deadline; the operator waits for real
instance closure before scaling down. Unsupported launchers refuse activation.
This is not permission to force-delete busy Pods or to bypass a fence.

The normal phases, safe-abort/conditional reverse handover, qcloud route proof,
capacity and explicit onboarding boundaries are specified in
[the production runbook](../ProdDelivery.md). Local validation evidence lives in
the active `add-prod-zero-downtime-delivery` OpenSpec change; it is not proof of
real CLB continuity or production capacity.
