"""Durable control intent, separate from business operations and their witnesses."""

from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import datetime
from typing import TYPE_CHECKING, Any, Mapping

if TYPE_CHECKING:
    from .coordinator import Operation, PostgresCoordinator

from .coordinator import OperationOwnershipError


class PipelineControl:
    def __init__(self, coordinator: PostgresCoordinator) -> None:
        self.coordinator = coordinator

    @asynccontextmanager
    async def _transaction(self, operation: Operation | None = None):
        c = self.coordinator
        async with c._transaction() as (connection, row):
            await c._check_scope(connection, row)
            if operation is not None:
                ticket = await c._validate(connection, row, operation)
                if not ticket["exclusive"]:
                    raise OperationOwnershipError(
                        "Retry reset requires exclusive admission"
                    )
            yield connection

    async def pause(self) -> None:
        """Publish cancellation without waiting for active writers to drain."""
        async with self._transaction() as db:
            await db.execute(
                "INSERT INTO lightrag_coordination.pipeline_control (deployment_id,workspace,paused,cancel_epoch) "
                "VALUES($1,$2,true,1) ON CONFLICT(deployment_id,workspace) DO UPDATE "
                "SET paused=true,cancel_epoch=pipeline_control.cancel_epoch+1",
                *self.coordinator._scope,
            )

    async def resume(self) -> None:
        """Explicit process entry points may clear pause; retry resumes atomically."""
        async with self._transaction() as db:
            await db.execute(
                "UPDATE lightrag_coordination.pipeline_control SET paused=false WHERE deployment_id=$1 AND workspace=$2",
                *self.coordinator._scope,
            )

    async def status(self) -> dict[str, Any]:
        """Return bounded aggregate status, never operation metadata or credentials."""
        c = self.coordinator
        async with c._transaction(administrative=True) as (db, row):
            control = await db.fetchrow(
                "SELECT paused,cancel_epoch FROM lightrag_coordination.pipeline_control WHERE deployment_id=$1 AND workspace=$2",
                *c._scope,
            )
            active = await db.fetchval(
                "SELECT count(*) FROM lightrag_coordination.operations WHERE deployment_id=$1 AND workspace=$2 AND state='active'",
                *c._scope,
            )
            claims = await db.fetchval(
                "SELECT count(*) FROM lightrag_coordination.document_claims WHERE deployment_id=$1 AND workspace=$2",
                *c._scope,
            )
            pending = await db.fetchval(
                "SELECT count(*) FROM lightrag_coordination.pipeline_requests WHERE deployment_id=$1 AND workspace=$2 AND state<>'completed'",
                *c._scope,
            )
            orphaned = await db.fetchval(
                "SELECT EXISTS(SELECT 1 FROM lightrag_coordination.operations o "
                "WHERE deployment_id=$1 AND workspace=$2 AND state='active' "
                "AND NOT EXISTS (SELECT 1 FROM pg_locks l WHERE l.locktype='advisory' "
                "AND l.granted AND l.objsubid=1 AND l.database=(SELECT oid FROM pg_database "
                "WHERE datname=current_database()) AND l.classid::bigint=(o.witness >> 32) "
                "AND l.objid::bigint=(o.witness & 4294967295)))",
                *c._scope,
            )
            return dict(
                paused=bool(control and control["paused"]),
                cancel_epoch=control["cancel_epoch"] if control else 0,
                pending_retries=pending,
                active_operations=active,
                claimed_documents=claims,
                fenced=row["fenced"],
                generation=row["generation"],
                recovery_required=row["fenced"] or orphaned,
                orphaned_operations=orphaned,
            )

    async def request_retry(
        self, request_id: str, *, target_cutoff_at: datetime | None = None
    ) -> dict[str, Any]:
        """Atomically accept a new intent and resume; duplicates never undo pause.

        Request IDs retain their identity after completion. Replaying one only
        returns its durable state, without granting another attempt or resume.
        See docs/design/DistributedPipelineContract.md for commit ordering.
        """
        if not isinstance(request_id, str) or not request_id.strip():
            raise ValueError("request_id must be non-empty")
        async with self._transaction() as db:
            if target_cutoff_at is None:
                row = await db.fetchrow(
                    "INSERT INTO lightrag_coordination.pipeline_requests (deployment_id,workspace,request_id,state) VALUES($1,$2,$3,'selecting') "
                    "ON CONFLICT DO NOTHING RETURNING request_id,state,created_at,target_cutoff_at",
                    *self.coordinator._scope,
                    request_id,
                )
            else:
                row = await db.fetchrow(
                    "INSERT INTO lightrag_coordination.pipeline_requests (deployment_id,workspace,request_id,state,target_cutoff_at) VALUES($1,$2,$3,'selecting',$4) "
                    "ON CONFLICT DO NOTHING RETURNING request_id,state,created_at,target_cutoff_at",
                    *self.coordinator._scope,
                    request_id,
                    target_cutoff_at,
                )
            if row is not None:
                # The workspace transaction orders acceptance against pause.
                # Never resume in the caller after this transaction commits.
                await db.execute(
                    "UPDATE lightrag_coordination.pipeline_control SET paused=false WHERE deployment_id=$1 AND workspace=$2",
                    *self.coordinator._scope,
                )
            else:
                row = await db.fetchrow(
                    "SELECT request_id,state,created_at,target_cutoff_at FROM lightrag_coordination.pipeline_requests WHERE deployment_id=$1 AND workspace=$2 AND request_id=$3",
                    *self.coordinator._scope,
                    request_id,
                )
            return dict(row)

    async def next_request(self, operation: Operation) -> dict[str, Any] | None:
        async with self._transaction(operation) as db:
            row = await db.fetchrow(
                "SELECT request_id,state,created_at,target_cutoff_at FROM lightrag_coordination.pipeline_requests WHERE deployment_id=$1 AND workspace=$2 AND state<>'completed' ORDER BY created_at,request_id LIMIT 1",
                *self.coordinator._scope,
            )
            return dict(row) if row else None

    async def add_targets(
        self, operation: Operation, request_id: str, targets: Mapping[str, str]
    ) -> None:
        async with self._transaction(operation) as db:
            for doc_id, version in targets.items():
                await db.execute(
                    "INSERT INTO lightrag_coordination.pipeline_retry_targets (deployment_id,workspace,request_id,doc_id,version,done) VALUES($1,$2,$3,$4,$5,false) ON CONFLICT DO NOTHING",
                    *self.coordinator._scope,
                    request_id,
                    doc_id,
                    version,
                )

    async def finish_selection(self, operation: Operation, request_id: str) -> None:
        async with self._transaction(operation) as db:
            await db.execute(
                "UPDATE lightrag_coordination.pipeline_requests SET state='resetting' WHERE deployment_id=$1 AND workspace=$2 AND request_id=$3 AND state='selecting'",
                *self.coordinator._scope,
                request_id,
            )

    async def targets(
        self, operation: Operation, request_id: str, *, limit: int = 64
    ) -> list[dict[str, str]]:
        if not 1 <= limit <= 1024:
            raise ValueError("Retry page limit must be between 1 and 1024")
        async with self._transaction(operation) as db:
            rows = await db.fetch(
                "SELECT doc_id,version FROM lightrag_coordination.pipeline_retry_targets WHERE deployment_id=$1 AND workspace=$2 AND request_id=$3 AND NOT done ORDER BY doc_id LIMIT $4",
                *self.coordinator._scope,
                request_id,
                limit,
            )
            return [dict(row) for row in rows]

    async def finish_target(
        self, operation: Operation, request_id: str, doc_id: str
    ) -> None:
        async with self._transaction(operation) as db:
            await db.execute(
                "UPDATE lightrag_coordination.pipeline_retry_targets SET done=true WHERE deployment_id=$1 AND workspace=$2 AND request_id=$3 AND doc_id=$4",
                *self.coordinator._scope,
                request_id,
                doc_id,
            )

    async def finish_request(self, operation: Operation, request_id: str) -> None:
        async with self._transaction(operation) as db:
            await db.execute(
                "UPDATE lightrag_coordination.pipeline_requests r SET state='completed' WHERE deployment_id=$1 AND workspace=$2 AND request_id=$3 AND state='resetting' "
                "AND NOT EXISTS(SELECT 1 FROM lightrag_coordination.pipeline_retry_targets t WHERE t.deployment_id=r.deployment_id AND t.workspace=r.workspace AND t.request_id=r.request_id AND NOT t.done)",
                *self.coordinator._scope,
                request_id,
            )

    async def cancel_requests(self, operation: Operation) -> None:
        """Clear retires accepted requests permanently, without deleting history."""
        async with self._transaction(operation) as db:
            await db.execute(
                "UPDATE lightrag_coordination.pipeline_retry_targets SET done=true WHERE deployment_id=$1 AND workspace=$2 AND NOT done",
                *self.coordinator._scope,
            )
            await db.execute(
                "UPDATE lightrag_coordination.pipeline_requests SET state='completed' WHERE deployment_id=$1 AND workspace=$2 AND state<>'completed'",
                *self.coordinator._scope,
            )

    async def scan_status(self, track_id: str) -> dict[str, Any] | None:
        """Read bounded scan diagnostics across Pods; tickets are not a retry queue."""
        import json

        c = self.coordinator
        global_state = await self.status()
        async with c._transaction(administrative=True) as (db, _):
            row = await db.fetchrow(
                "SELECT metadata,created_at,heartbeat_at,MIN(created_at) OVER() AS started_at,COUNT(*) OVER() AS version "
                "FROM lightrag_coordination.operations WHERE deployment_id=$1 AND workspace=$2 "
                "AND kind IN ('run_scanning_process','scan_processing') AND metadata->>'scan_track_id'=$3 ORDER BY created_at DESC LIMIT 1",
                *c._scope,
                track_id,
            )
            if row is None:
                return None
            metadata = (
                json.loads(row["metadata"])
                if isinstance(row["metadata"], str)
                else row["metadata"]
            )
            status = metadata.get("scan_status", "running")
            message = ""
            if status == "running" and global_state["recovery_required"]:
                status = "abandoned"
                message = "Workspace recovery required; scan did not report completion."
            allowed = ("discovered", "enqueued", "resumed", "processed")
            counts = metadata.get("scan_counts", {})
            return dict(
                track_id=track_id,
                status=status,
                counts={key: int(counts[key]) for key in allowed if key in counts},
                created_at=row["started_at"].timestamp(),
                updated_at=row["heartbeat_at"].timestamp(),
                version=row["version"],
                message=message,
            )
