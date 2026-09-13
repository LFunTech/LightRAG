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

## Maintenance, cancellation, and recovery

Normal initialization verifies PG columns, primary keys and vector dimensions,
and HugeGraph schema, without DDL or data migration. Provision new tables/schema
inside the SDK's explicit `distributed_maintenance()` context, then call
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
