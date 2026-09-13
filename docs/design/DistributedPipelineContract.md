# Distributed pipeline and API control contract

This extends [PipelineConcurrencyContract.md](PipelineConcurrencyContract.md) only
when `distributed_writes=True`. The default local pipeline and Manager mailbox
retain their existing contract. Physical writes and recovery remain governed by
[DistributedRuntimeContract.md](DistributedRuntimeContract.md) and
[PurgeRecoveryContract.md](PurgeRecoveryContract.md).

## Admission and discovery

`apipeline_process_enqueue_documents()` selects the distributed scheduler rather
than taking the Manager-wide processing reservation. A per-runtime lock protects
that instance's `_active_run_ctx`; it is **not a workspace pipeline leader**.
Every batch holds a shared durable operation and at most
`min(64, max_parallel_insert, configured_page_size_or_64)` document claims.
A strict scheduling keyset page supplies candidate IDs only. Atomic try-claim
precedes strict hydration, consistency repair, parser dispatch, and processing.
A fully claimed/filtered page advances its cursor; it is not an idle proof.
Claimed rows still outside automatic statuses on the strict re-read are skipped.
FAILED is never in the automatic status set.

Claimed batches run the existing `_run_pipeline_batch` parse/analyze/process
workers, including actual extraction, graph merge and vector commits. Only the
local in-batch feeder is disabled: admitting its unclaimed PENDING snapshot would
bypass ownership. Worker status transitions verify and report claim phase.
Claim release happens after every worker is joined and a durable heartbeat proves
that no swallowed backend failure fenced the operation. Exceptions/cancellation
still retain durable evidence under the coordinator's fail-closed protocol.

`apipeline_start_polling(interval=1.0)` starts a context-detached strict sweep on
the SDK instance; API lifespan calls it automatically. No mailbox publication is
required. `apipeline_process_enqueue_documents()` is an explicit processing and
resume request. Internal HTTP indexing callbacks and polling never resume a
paused workspace. Normal processing can stop at a batch boundary for maintenance.
A polling coordination failure is visible as its exception class in status and
does not fall back to local locking or automatically repair a fence.

The whole enqueue read/deduplicate/reserve/upsert window retains the local
`enqueue_serialize` namespace lock and additionally holds the durable
`PipelineIngress/enqueue_serialize` resource lock. It does not wait for unrelated
processing. Existing document IDs are never replaced by duplicate submissions. Error
enqueue uses the same insert-only window in distributed mode, since its
computed error ID can collide with a caller-supplied ordinary document ID;
repeated error submissions retain the original status rather than overwriting
a possibly claimed row. Local error enqueue retains its original behavior.

## Persistent cancellation and manual retry

Version 2 of the explicit coordinator migrations adds `pipeline_control`,
`pipeline_requests`, and `pipeline_retry_targets`. Version 1 SQL/checksum is
unchanged. Startup verifies all versions and the actual types, nullability,
defaults and primary keys of new columns. There is no startup DDL or schema repair.

Cancellation publishes `paused=true` and increments `cancel_epoch` in a short
coordinator transaction **without waiting for exclusive business admission**.
Otherwise it could not reach the shared writers it needs to stop. Every worker
checks durable cancellation; the epoch also catches cancel followed by a rapid
explicit resume before an old batch observes `paused`. Already active documents
settle through the existing cancellation/FAILED transitions, not forced task
cancellation. Queued documents remain available for a subsequent explicit run.
Polling and polling stop/start do not undo pause.

`apipeline_request_retry(request_id=None)` persists a server-generated (or
caller-supplied idempotency) key before drain and explicitly resumes the control
plane. API `/reprocess_failed` persists this intent and starts polling; `/scan`
persists intent before starting actual exclusive classification. Request lifetime
is independent of the accepting process or its in-memory mailbox.

A pending request causes processing schedulers to stop admitting new batches.
An exclusive reset selects FAILED rows in bounded pages. Each selected target's
original `updated_at` version is committed before any reset. Selection only
includes versions at or before the request's database timestamp, so rows failing
during the drain or restarted selection do not receive another attempt. Targets
are insert-once; restarting selection cannot replace an older target version.
After selection is durable, the request moves from `selecting` to `resetting`.

Each reset strictly reads status/content, skips custom-chunk journals and
confirmed-absent content, and only resets a row still FAILED at the selected
version. Target completion follows the confirmed reset; a crash in that gap
leaves a changed status/version, so replay only completes the target instead of
resetting a newer failure. Terminal request IDs and target progress are retained,
not evicted with the local mailbox. Clear terminally retires pending requests and targets after exclusive and
destructive admission, before dropping stores; a later clear failure remains
fenced and does not resurrect those requests. No TTL grants an attempt
or releases ownership. An uncertain reset still requires the existing audited
recovery protocol before replay.

## HTTP and file operations

All existing document/graph mutation routes retain `combined_auth`. No new public
recovery or inspection endpoint is introduced; this adds no ACL model or role
migration. Operator CLI credentials remain the recovery authority.

- Upload holds a shared operation and a canonical input-file resource lock across
  canonical precheck, `xb`/safe-opener file creation and managed handoff. Distinct
  files and normal processing remain concurrent. Request validation/confirmed
  client refusals do not fence; failed cleanup or unknown filesystem failures do.
- File/text indexing callbacks obtain their own detached operations. A completed
  request's inherited permit is not reused. The actual file enqueue helper is
  guarded too, including temporary-file cleanup.
- Clear, source-conflict repair and background deletion hold exclusive admission
  across actual storage and shared-file mutation, not only their HTTP preflight.
- Scan obtains its detached exclusive ticket before signaling background startup.
  It holds that ticket through rollback, retry reset, classification, archive and
  enqueue, then **releases it before** normal shared claimed processing. Unknown
  scan/filesystem errors propagate; they are not completed operation tickets.
- The API constructor passes `distributed_input_dir=args.input_dir`. Distributed
  parser source resolution uses that same root, including the workspace suffix,
  rather than silently switching back to an unrelated `INPUT_DIR` environment
  value when the CLI option was used.

`/documents/pipeline_status` keeps compatible progress fields, labels
`local_busy`, and adds bounded global `distributed` aggregates: pause/cancel epoch,
pending retries, active operations, claims, generation/recovery fence and local
polling state/error. It exposes neither operation metadata nor owner tokens,
credentials, SQL, documents or unbounded histories. Coordination busy returns 409;
other typed coordination failures return 503 with a stable exception-class code.
`/documents/scan/status/{track_id}` reads bounded cross-Pod diagnostics from the
actual classification and shared-processing operation phases. Only aggregate
discovered/enqueued/resumed/already-processed counts are exposed; operation
metadata is not a request queue. A fenced or orphaned running scan is reported as
abandoned, never silently expired or taken over.
`/recovery/force_reset` refuses distributed mode with an actionable 409 directing
operators to audited CLI recovery. It never clears durable ownership or claims.

## SDK, API shutdown and operator bootstrap

SDK users initialize storages and opt into discovery with
`await rag.apipeline_start_polling()`. `await rag.apipeline_stop_polling()` stops
new discovery and waits for already admitted workers normally. Its wait is
shielded: cancelling the *waiter* does not cancel the writer, close the runtime,
or release claims. The caller can wait again; an external hard stop preserves
normal durable recovery evidence. Polling can be restarted on an open runtime,
but that does not resume a cancelled workspace.

SDK finalization first stops/drains polling, then uses the existing exclusive
finalization gate. If exclusive admission times out or is cancelled, the runtime
and witness remain open; discovery remains explicitly stopped and is visible in
status. A caller wishing to continue must start polling explicitly. Finalization
never silently undoes global cancellation.

API shutdown follows the same poll drain, then joins its managed background tasks
before finalization. Graceful drain has no artificial write-cancellation timeout;
container grace periods must cover expected backend/LLM latency. Killing a process
at the deployment deadline deliberately leaves the durable fence/recovery path.

`create_app(args).state.rag` exposes the fully configured instance for operator
bootstrap **without running app lifespan**. A maintenance program can do:

```python
app = create_app(args)
rag = app.state.rag
async with rag.distributed_maintenance():
    await rag.initialize_storages()
    await rag.check_and_migrate_data()
await rag.finalize_storages()
```

Run `python -m lightrag.distributed migrate` first. Stop old writers and follow the
runtime maintenance/recovery prerequisites; this interface does not waive them.
Normal API replicas only initialize/verify, never call data migration on startup.

## Deliberate boundaries and residues

- No heartbeat/claim TTL, automatic orphan recovery, remote-write replay or global
  pipeline leader is introduced. The physical pending/ACK protocol is unchanged.
- A successful retry reset followed by a lost progress ACK is replay-safe through
  the selected version. A crash elsewhere is not proof that backend writes did
  not occur; the workspace stays fenced until operator audit/recovery.
- Coordinator history is retained. API status is bounded, but archival of the
  growing audit/request/target tables remains an operator retention concern.
- Arbitrary SDK validation errors escaping an admitted operation remain
  conservatively fenced, even if no physical mutation happened. HTTP shape/type
  validation and confirmed 4xx business refusals avoid that boundary where safe.
- A scan request that has durably accepted retry intent but cannot obtain its
  exclusive startup ticket reports failure; the accepted retry intent remains
  available to a later process. It does not claim file discovery ran.
- Shared-path configuration is an operator assertion, not a proof of RWX mounts.
  Independent-process deployment/fault acceptance is separate from in-process
  integration tests and belongs to deployment verification.
