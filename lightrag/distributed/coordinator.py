"""Durable workspace tickets, resource ownership and uncertain-write barriers."""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from dataclasses import dataclass
from hashlib import sha256
import json
from math import isfinite
import secrets
from typing import Any, AsyncIterator, Iterable, Mapping
from uuid import UUID, uuid4
from weakref import WeakKeyDictionary


class CoordinationError(RuntimeError):
    """A recognizable, credential-free coordination failure."""


class CoordinationSchemaError(CoordinationError):
    """Explicit schema migration is required."""


class ConfigurationMismatchError(CoordinationError):
    """Participants disagree about the physical deployment manifest."""


class CoordinationBusyError(CoordinationError):
    """An unadmitted operation or lock exhausted its bounded wait."""


class WorkspaceFencedError(CoordinationError):
    """Uncertain work requires an explicitly audited recovery."""


class OperationOwnershipError(CoordinationError):
    """The operation, generation, task or document owner is not valid."""


class CoordinationUnavailableError(CoordinationError):
    """Coordination was lost; no local-lock fallback is permitted."""


@dataclass(frozen=True)
class Operation:
    """An explicit permit, shareable with children only while its context is open."""

    id: UUID
    generation: int
    owner_id: UUID


@dataclass(frozen=True)
class Mutation:
    id: UUID
    operation_id: UUID


def _json(value: Mapping[str, Any]) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _label(value: str, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must be non-empty")
    return value


class PostgresCoordinator:
    """Use a separate pool; initialize verifies schema but never applies migrations.

    Resource/ticket/claim ownership never expires. A dedicated session witness
    detects a disappeared owner ONLY to fence, never to release its resources.
    Heartbeats are diagnostic, not takeover authorization. Closing or rebuilding
    a coordinator with unfinished work leaves that work for explicit recovery.

    The manifest is hashed, not stored or emitted. Supply only canonical physical
    targets and non-secret settings; credentials are not a target identity.
    Metadata, labels and recovery notes are persisted: callers must not put secrets
    or document contents in them. Await all child tasks before ending an operation.
    Lock full read/modify/write sequences; mutation records alone do not serialize
    business resources. A mutation block must return only after a validated ACK.
    """

    def __init__(
        self,
        dsn: str,
        deployment_id: str,
        workspace: str,
        manifest: Mapping[str, Any] | None = None,
        *,
        wait_timeout: float = 30.0,
        poll_interval: float = 0.05,
        command_timeout: float = 10.0,
        pool_size: int = 4,
    ):
        from .control import PipelineControl

        self.pipeline_control = PipelineControl(self)
        self._dsn = dsn
        self.deployment_id = _label(deployment_id, "deployment_id")
        self.workspace = _label(workspace, "workspace")
        self._scope = (self.deployment_id, self.workspace)
        self._manifest_hash = (
            sha256(_json(manifest).encode()).hexdigest()
            if manifest is not None
            else None
        )
        if (
            not all(
                isfinite(value)
                for value in (wait_timeout, poll_interval, command_timeout)
            )
            or wait_timeout < 0
            or poll_interval <= 0
            or command_timeout <= 0
            or pool_size < 1
        ):
            raise ValueError("Invalid coordination timeout or pool size")
        self.wait_timeout = wait_timeout
        self.poll_interval = poll_interval
        self._command_timeout = command_timeout
        self._pool_size = pool_size
        self._pool = None
        self._witness_connection = None
        self._owner_id = uuid4()
        self._witness = secrets.randbits(63)
        self._task_ids: WeakKeyDictionary = WeakKeyDictionary()
        self._broken = False
        self._read_only = False

    @staticmethod
    async def migrate(dsn: str) -> None:
        """Explicitly apply versioned, idempotent coordination-only migrations."""
        import asyncpg
        from .migrations import migrate

        connection = None
        try:
            connection = await asyncpg.connect(dsn, command_timeout=30)
            await migrate(connection)
        except CoordinationError:
            raise
        except Exception:
            raise CoordinationSchemaError("Coordination migration failed") from None
        finally:
            if connection is not None:
                await connection.close()

    async def initialize(self, *, read_only: bool = False) -> None:
        """Verify schema and register a compatible scope; read_only never registers.

        A closed or failed coordinator cannot be revived. Create a new instance
        to obtain a new owner identity, leaving old durable permits untouched.
        """
        import asyncpg
        from .migrations import verify

        if self._broken:
            raise CoordinationUnavailableError(
                "Create a new coordinator after closure or failure"
            )
        if self._pool is not None:
            return
        if not read_only and self._manifest_hash is None:
            raise ValueError("Writer initialization requires a deployment manifest")
        self._read_only = read_only
        try:
            self._pool = await asyncpg.create_pool(
                self._dsn,
                min_size=1,
                max_size=self._pool_size,
                command_timeout=self._command_timeout,
            )
            async with self._pool.acquire() as connection:
                await verify(connection)
                if not read_only:
                    async with connection.transaction():
                        await connection.execute(
                            "INSERT INTO lightrag_coordination.workspaces "
                            "(deployment_id,workspace,manifest_hash,generation,fenced) VALUES($1,$2,$3,1,false) "
                            "ON CONFLICT DO NOTHING",
                            *self._scope,
                            self._manifest_hash,
                        )
                        value = await connection.fetchval(
                            "SELECT manifest_hash FROM lightrag_coordination.workspaces "
                            "WHERE deployment_id=$1 AND workspace=$2",
                            *self._scope,
                        )
                        if value != self._manifest_hash:
                            raise ConfigurationMismatchError(
                                "Workspace deployment manifest mismatch"
                            )
            if not read_only:
                self._witness_connection = await asyncpg.connect(
                    self._dsn,
                    command_timeout=self._command_timeout,
                )
                acquired = await self._witness_connection.fetchval(
                    "SELECT pg_try_advisory_lock($1::bigint)",
                    self._witness,
                )
                if not acquired:
                    raise CoordinationUnavailableError(
                        "Could not establish owner witness"
                    )
                # Discover orphans before any business work. Fenced scopes remain
                # inspectable; admission will report the recognizable fence error.
                try:
                    async with self._transaction() as (connection, row):
                        await self._check_scope(connection, row)
                except WorkspaceFencedError:
                    pass
        except BaseException as exc:
            self._quarantine()
            if isinstance(exc, (CoordinationError, asyncio.CancelledError)):
                raise
            raise CoordinationUnavailableError(
                "Coordination initialization failed"
            ) from None

    def _quarantine(self) -> None:
        self._broken = True
        if self._witness_connection is not None:
            self._witness_connection.terminate()
        if self._pool is not None:
            self._pool.terminate()

    async def close(self) -> None:
        """Close transport only; never release unresolved durable ownership."""
        self._quarantine()

    @asynccontextmanager
    async def _transaction(self, *, administrative: bool = False):
        if self._pool is None or self._broken:
            raise CoordinationUnavailableError("Coordinator is not available")
        if not administrative and (
            self._read_only
            or self._witness_connection is None
            or self._witness_connection.is_closed()
        ):
            self._quarantine()
            raise CoordinationUnavailableError("Writer witness is unavailable")
        delayed = None
        try:
            async with self._pool.acquire() as connection:
                async with connection.transaction():
                    row = await connection.fetchrow(
                        "SELECT * FROM lightrag_coordination.workspaces "
                        "WHERE deployment_id=$1 AND workspace=$2 FOR UPDATE",
                        *self._scope,
                    )
                    if row is None:
                        raise ConfigurationMismatchError("Workspace is not registered")
                    try:
                        yield connection, row
                    except WorkspaceFencedError as exc:
                        # Persist fence discovery even though admission is denied.
                        delayed = exc
        except CoordinationError:
            raise
        except BaseException as exc:
            # A commit response can be lost. Removing only the witness makes the
            # durable operation/pending row observable as orphaned to every peer.
            self._quarantine()
            if isinstance(exc, asyncio.CancelledError):
                raise
            raise CoordinationUnavailableError(
                "Coordination transaction failed"
            ) from None
        if delayed:
            raise delayed

    async def _orphans(self, connection) -> list[UUID]:
        rows = await connection.fetch(
            "SELECT o.id FROM lightrag_coordination.operations o "
            "WHERE deployment_id=$1 AND workspace=$2 AND state='active' "
            "AND NOT EXISTS (SELECT 1 FROM pg_locks l WHERE l.locktype='advisory' "
            "AND l.granted AND l.objsubid=1 AND l.database=(SELECT oid FROM pg_database "
            "WHERE datname=current_database()) "
            "AND l.classid::bigint=(o.witness >> 32) "
            "AND l.objid::bigint=(o.witness & 4294967295))",
            *self._scope,
        )
        return [r["id"] for r in rows]

    async def _fence(self, connection, reason: str) -> None:
        await connection.execute(
            "UPDATE lightrag_coordination.workspaces SET fenced=true, "
            "fence_reason=COALESCE(fence_reason,$3) WHERE deployment_id=$1 AND workspace=$2",
            *self._scope,
            reason,
        )

    async def _check_scope(self, connection, row) -> None:
        if row["fenced"]:
            raise WorkspaceFencedError("Workspace requires audited recovery")
        if await self._orphans(connection):
            await self._fence(connection, "Owner witness disappeared")
            raise WorkspaceFencedError(
                "Unfinished owner disappeared; audited recovery required"
            )

    async def _validate(
        self, connection, row, operation: Operation, *, check_fence=True
    ):
        if operation.owner_id != self._owner_id:
            raise OperationOwnershipError("Operation belongs to another coordinator")
        if operation.generation != row["generation"]:
            raise OperationOwnershipError("Operation generation is obsolete")
        found = await connection.fetchrow(
            "SELECT * FROM lightrag_coordination.operations "
            "WHERE id=$1 AND deployment_id=$2 AND workspace=$3 AND owner_id=$4 "
            "AND generation=$5 AND state='active'",
            operation.id,
            *self._scope,
            self._owner_id,
            operation.generation,
        )
        if found is None:
            raise OperationOwnershipError("Operation is not active")
        if check_fence:
            await self._check_scope(connection, row)
        return found

    async def _wait(self, attempt, timeout: float | None):
        duration = self.wait_timeout if timeout is None else timeout
        if not isfinite(duration) or duration < 0:
            raise ValueError("timeout must be finite and non-negative")
        deadline = asyncio.get_running_loop().time() + duration
        while True:
            result = await attempt()
            if result is not None:
                return result
            remaining = deadline - asyncio.get_running_loop().time()
            if remaining <= 0:
                raise CoordinationBusyError("Coordination wait deadline exceeded")
            await asyncio.sleep(min(self.poll_interval, remaining))

    @asynccontextmanager
    async def operation(
        self,
        kind: str,
        exclusive: bool = False,
        *,
        timeout: float | None = None,
        metadata: Mapping[str, Any] | None = None,
    ) -> AsyncIterator[Operation]:
        """Acquire a durable ticket; wait timeouts before admission leave no ticket.

        Exceptional exits retain ownership and fence even without pending writes.
        Ordinary business failures may be caught inside this context only when all
        mutations are confirmed and the caller has completed its recovery journal.
        """
        _label(kind, "kind")
        encoded = _json(metadata or {})

        async def attempt():
            async with self._transaction() as (connection, row):
                await self._check_scope(connection, row)
                conflict = await connection.fetchval(
                    "SELECT EXISTS(SELECT 1 FROM lightrag_coordination.operations "
                    "WHERE deployment_id=$1 AND workspace=$2 AND state='active' "
                    "AND ($3 OR exclusive))",
                    *self._scope,
                    exclusive,
                )
                if conflict:
                    return None
                op = Operation(uuid4(), row["generation"], self._owner_id)
                await connection.execute(
                    "INSERT INTO lightrag_coordination.operations "
                    "(id,deployment_id,workspace,generation,owner_id,witness,kind,exclusive,metadata,state,phase) "
                    "VALUES($1,$2,$3,$4,$5,$6,$7,$8,$9::jsonb,'active','admitted')",
                    op.id,
                    *self._scope,
                    op.generation,
                    op.owner_id,
                    self._witness,
                    kind,
                    exclusive,
                    encoded,
                )
                return op

        op = await self._wait(attempt, timeout)
        try:
            yield op
        except BaseException:
            await self._mark_uncertain(
                op, "Operation exited without confirmed completion"
            )
            raise
        else:
            async with self._transaction() as (connection, row):
                await self._validate(connection, row, op)
                pending = await connection.fetchval(
                    "SELECT EXISTS(SELECT 1 FROM lightrag_coordination.mutations "
                    "WHERE operation_id=$1 AND state='pending')",
                    op.id,
                )
                locked = await connection.fetchval(
                    "SELECT EXISTS(SELECT 1 FROM lightrag_coordination.resource_locks "
                    "WHERE operation_id=$1)",
                    op.id,
                )
                if pending or locked:
                    await self._fence(
                        connection,
                        "Operation ended with unfinished children or mutations",
                    )
                    raise WorkspaceFencedError("Operation still owns unfinished work")
                await connection.execute(
                    "DELETE FROM lightrag_coordination.document_claims WHERE operation_id=$1",
                    op.id,
                )
                await connection.execute(
                    "UPDATE lightrag_coordination.operations SET state='completed',phase='completed', "
                    "finished_at=clock_timestamp(),heartbeat_at=clock_timestamp() WHERE id=$1",
                    op.id,
                )

    async def _mark_uncertain(self, operation: Operation, reason: str) -> None:
        try:
            async with self._transaction() as (connection, row):
                await self._validate(connection, row, operation, check_fence=False)
                await self._fence(connection, reason)
                await connection.execute(
                    "UPDATE lightrag_coordination.operations SET phase='uncertain' WHERE id=$1",
                    operation.id,
                )
        except BaseException:
            # Fence durability could not be confirmed. Witness removal is a
            # fail-closed fallback, not cleanup; the original failure still raises.
            self._quarantine()

    def _task_id(self) -> UUID:
        task = asyncio.current_task()
        if task is None:
            raise OperationOwnershipError("Resource locks require an asyncio task")
        if task not in self._task_ids:
            self._task_ids[task] = uuid4()
        return self._task_ids[task]

    @asynccontextmanager
    async def lock(
        self, operation: Operation, keys: Iterable[str], *, timeout: float | None = None
    ):
        """Atomically hold sorted keys throughout the full business read/modify/write.

        Only the exact asyncio task can reenter. Child tasks sharing the same
        operation must compete. Uncertain exits retain all acquired resources.
        """
        ordered = sorted({_label(key, "resource key") for key in keys})
        task_id = self._task_id()

        async def attempt():
            async with self._transaction() as (connection, row):
                await self._validate(connection, row, operation)
                existing = await connection.fetch(
                    "SELECT operation_id,task_id FROM lightrag_coordination.resource_locks "
                    "WHERE deployment_id=$1 AND workspace=$2 AND resource_key=ANY($3::text[])",
                    *self._scope,
                    ordered,
                )
                if any(
                    r["operation_id"] != operation.id or r["task_id"] != task_id
                    for r in existing
                ):
                    return None
                for key in ordered:
                    await connection.execute(
                        "INSERT INTO lightrag_coordination.resource_locks "
                        "(deployment_id,workspace,resource_key,operation_id,task_id,depth) VALUES($1,$2,$3,$4,$5,1) "
                        "ON CONFLICT(deployment_id,workspace,resource_key) DO UPDATE "
                        "SET depth=lightrag_coordination.resource_locks.depth+1",
                        *self._scope,
                        key,
                        operation.id,
                        task_id,
                    )
                return True

        await self._wait(attempt, timeout)
        try:
            yield
        except BaseException:
            await self._mark_uncertain(
                operation, "Resource owner exited without confirmed completion"
            )
            raise
        else:
            async with self._transaction() as (connection, row):
                await self._validate(connection, row, operation)
                await connection.execute(
                    "UPDATE lightrag_coordination.resource_locks SET depth=depth-1 "
                    "WHERE operation_id=$1 AND task_id=$2 AND resource_key=ANY($3::text[])",
                    operation.id,
                    task_id,
                    ordered,
                )
                await connection.execute(
                    "DELETE FROM lightrag_coordination.resource_locks "
                    "WHERE operation_id=$1 AND task_id=$2 AND depth=0",
                    operation.id,
                    task_id,
                )

    async def try_claim_document(self, operation: Operation, doc_id: str) -> bool:
        """Try once before any status repair or parsing; an existing claim is false."""
        _label(doc_id, "doc_id")
        async with self._transaction() as (connection, row):
            await self._validate(connection, row, operation)
            result = await connection.fetchval(
                "INSERT INTO lightrag_coordination.document_claims "
                "(deployment_id,workspace,doc_id,operation_id,phase) VALUES($1,$2,$3,$4,'claimed') "
                "ON CONFLICT DO NOTHING RETURNING doc_id",
                *self._scope,
                doc_id,
                operation.id,
            )
            return result is not None

    async def _claim(self, connection, operation: Operation, doc_id: str):
        found = await connection.fetchval(
            "SELECT operation_id FROM lightrag_coordination.document_claims "
            "WHERE deployment_id=$1 AND workspace=$2 AND doc_id=$3",
            *self._scope,
            doc_id,
        )
        if found != operation.id:
            raise OperationOwnershipError(
                "Document belongs to another operation or is not claimed"
            )

    async def assert_claim(self, operation: Operation, doc_id: str) -> None:
        async with self._transaction() as (connection, row):
            await self._validate(connection, row, operation)
            await self._claim(connection, operation, doc_id)

    async def release_claim(self, operation: Operation, doc_id: str) -> None:
        async with self._transaction() as (connection, row):
            await self._validate(connection, row, operation)
            await self._claim(connection, operation, doc_id)
            pending = await connection.fetchval(
                "SELECT EXISTS(SELECT 1 FROM lightrag_coordination.mutations "
                "WHERE operation_id=$1 AND state='pending')",
                operation.id,
            )
            if pending:
                raise OperationOwnershipError(
                    "Cannot release a claim while mutations are pending"
                )
            await connection.execute(
                "DELETE FROM lightrag_coordination.document_claims "
                "WHERE deployment_id=$1 AND workspace=$2 AND doc_id=$3",
                *self._scope,
                doc_id,
            )

    async def set_phase(
        self,
        operation: Operation,
        phase: str,
        *,
        doc_id: str | None = None,
        metadata: Mapping[str, Any] | None = None,
    ) -> None:
        """Persist progress and shallow-merge non-secret recovery metadata."""
        _label(phase, "phase")
        encoded = _json(metadata or {})
        async with self._transaction() as (connection, row):
            await self._validate(connection, row, operation)
            if doc_id is not None:
                await self._claim(connection, operation, doc_id)
                await connection.execute(
                    "UPDATE lightrag_coordination.document_claims SET phase=$4,heartbeat_at=clock_timestamp() "
                    "WHERE deployment_id=$1 AND workspace=$2 AND doc_id=$3",
                    *self._scope,
                    doc_id,
                    phase,
                )
            await connection.execute(
                "UPDATE lightrag_coordination.operations SET phase=$2,metadata=metadata || $3::jsonb, "
                "heartbeat_at=clock_timestamp() WHERE id=$1",
                operation.id,
                phase,
                encoded,
            )

    async def heartbeat(self, operation: Operation) -> None:
        """Refresh diagnostic operation/claim timestamps, never extend a lease."""
        async with self._transaction() as (connection, row):
            await self._validate(connection, row, operation)
            await connection.execute(
                "UPDATE lightrag_coordination.operations SET heartbeat_at=clock_timestamp() WHERE id=$1",
                operation.id,
            )
            await connection.execute(
                "UPDATE lightrag_coordination.document_claims SET heartbeat_at=clock_timestamp() "
                "WHERE operation_id=$1",
                operation.id,
            )

    @asynccontextmanager
    async def mutation(
        self, operation: Operation, backend: str, namespace: str, method: str
    ):
        """Publish pending BEFORE yield; ACK only on a positively confirmed return.

        Catching a backend exception outside this context cannot erase its fence.
        Cancellation and lost ACKs retain pending. Other already-in-flight writes
        may ACK after a fence, but their ACK never clears that fence or other rows.
        """
        for name, value in (
            ("backend", backend),
            ("namespace", namespace),
            ("method", method),
        ):
            _label(value, name)
        mutation = Mutation(uuid4(), operation.id)
        async with self._transaction() as (connection, row):
            await self._validate(connection, row, operation)
            await connection.execute(
                "INSERT INTO lightrag_coordination.mutations(id,operation_id,backend,namespace,method,state) "
                "VALUES($1,$2,$3,$4,$5,'pending')",
                mutation.id,
                operation.id,
                backend,
                namespace,
                method,
            )
        try:
            yield mutation
        except BaseException:
            await self._mark_uncertain(
                operation, "Storage mutation acknowledgement is uncertain"
            )
            raise
        else:
            # Failure here leaves the row pending and closes the witness via
            # _transaction; peers will retain and fence that uncertain write.
            async with self._transaction() as (connection, row):
                await self._validate(connection, row, operation, check_fence=False)
                await connection.execute(
                    "UPDATE lightrag_coordination.mutations SET state='ack',acknowledged_at=clock_timestamp() "
                    "WHERE id=$1 AND operation_id=$2 AND state='pending'",
                    mutation.id,
                    operation.id,
                )

    async def _snapshot(self, connection) -> dict[str, Any]:
        row = await connection.fetchrow(
            "SELECT deployment_id,workspace,generation,fenced,fence_reason,manifest_hash "
            "FROM lightrag_coordination.workspaces WHERE deployment_id=$1 AND workspace=$2",
            *self._scope,
        )
        if row is None:
            raise ConfigurationMismatchError("Workspace is not registered")
        result = dict(row)
        for name, query in {
            "operations": "SELECT * FROM lightrag_coordination.operations WHERE deployment_id=$1 AND workspace=$2 ORDER BY created_at,id",
            "locks": "SELECT * FROM lightrag_coordination.resource_locks WHERE deployment_id=$1 AND workspace=$2 ORDER BY resource_key",
            "claims": "SELECT * FROM lightrag_coordination.document_claims WHERE deployment_id=$1 AND workspace=$2 ORDER BY doc_id",
            "mutations": "SELECT m.* FROM lightrag_coordination.mutations m JOIN lightrag_coordination.operations o ON o.id=m.operation_id WHERE o.deployment_id=$1 AND o.workspace=$2 ORDER BY m.created_at,m.id",
            "recovery_audit": "SELECT * FROM lightrag_coordination.recovery_audit WHERE deployment_id=$1 AND workspace=$2 ORDER BY recovered_at,id",
        }.items():
            result[name] = [
                dict(r) for r in await connection.fetch(query, *self._scope)
            ]
            for record in result[name]:
                for field in ("metadata", "confirmations", "snapshot"):
                    if field in record and isinstance(record[field], str):
                        record[field] = json.loads(record[field])
        result["orphaned_operations"] = await self._orphans(connection)
        result["fenced"] = result["fenced"] or bool(result["orphaned_operations"])
        return json.loads(json.dumps(result, default=str))

    async def inspect(self) -> dict[str, Any]:
        """Read a consistent diagnostic snapshot without modifying any state."""
        if self._pool is None or self._broken:
            raise CoordinationUnavailableError("Coordinator is not available")
        try:
            async with self._pool.acquire() as connection:
                async with connection.transaction(
                    isolation="repeatable_read", readonly=True
                ):
                    return await self._snapshot(connection)
        except CoordinationError:
            raise
        except Exception:
            raise CoordinationUnavailableError(
                "Coordination inspection failed"
            ) from None

    async def recover(
        self,
        *,
        expected_generation: int,
        actor: str,
        reason: str,
        writers_stopped: bool = False,
        inflight_finished: bool = False,
        state_audited: bool = False,
    ) -> dict[str, Any]:
        """Release a scope ONLY after all three explicit operational confirmations.

        Run from a read-only initialized administrative client after stopping all
        writers. These confirmations cannot prove storage quiescence: an operator
        must actually establish it. Generation does not fence already sent remote
        requests. A live unfinished owner or stale generation refuses recovery.
        Resource/claim snapshots and all mutation/operation history are retained.
        """
        confirmations = {
            "writers_stopped": writers_stopped,
            "inflight_finished": inflight_finished,
            "state_audited": state_audited,
        }
        if not all(value is True for value in confirmations.values()):
            raise ValueError("Recovery requires all three explicit confirmations")
        _label(actor, "actor")
        _label(reason, "reason")
        if type(expected_generation) is not int or expected_generation < 1:
            raise ValueError("expected_generation must be a positive integer")
        async with self._transaction(administrative=True) as (connection, row):
            if row["generation"] != expected_generation:
                raise OperationOwnershipError(
                    "Recovery generation changed; inspect and audit again"
                )
            snapshot = await self._snapshot(connection)
            orphaned = set(snapshot["orphaned_operations"])
            if any(
                o["state"] == "active" and o["id"] not in orphaned
                for o in snapshot["operations"]
            ):
                raise CoordinationBusyError(
                    "An unfinished writer still has a live witness; stop all writers"
                )
            # Do not recursively embed earlier audit snapshots in later audits.
            snapshot.pop("recovery_audit")
            await connection.execute(
                "INSERT INTO lightrag_coordination.recovery_audit "
                "(id,deployment_id,workspace,old_generation,new_generation,actor,reason,confirmations,snapshot) "
                "VALUES($1,$2,$3,$4,$5,$6,$7,$8::jsonb,$9::jsonb)",
                uuid4(),
                *self._scope,
                expected_generation,
                expected_generation + 1,
                actor,
                reason,
                _json(confirmations),
                _json(snapshot),
            )
            await connection.execute(
                "UPDATE lightrag_coordination.mutations m SET state='recovered',recovered_at=clock_timestamp() "
                "FROM lightrag_coordination.operations o WHERE o.id=m.operation_id "
                "AND o.deployment_id=$1 AND o.workspace=$2 AND m.state='pending'",
                *self._scope,
            )
            await connection.execute(
                "UPDATE lightrag_coordination.operations SET state='recovered',finished_at=clock_timestamp() "
                "WHERE deployment_id=$1 AND workspace=$2 AND state='active'",
                *self._scope,
            )
            for table in ("resource_locks", "document_claims"):
                await connection.execute(
                    f"DELETE FROM lightrag_coordination.{table} WHERE deployment_id=$1 AND workspace=$2",
                    *self._scope,
                )
            await connection.execute(
                "UPDATE lightrag_coordination.workspaces SET generation=generation+1,fenced=false,fence_reason=NULL "
                "WHERE deployment_id=$1 AND workspace=$2",
                *self._scope,
            )
            result = await self._snapshot(connection)
        return result
