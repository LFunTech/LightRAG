"""Opt-in business admission and physical-write boundaries.

Context variables carry permits, never replace the coordinator's durable checks.
Keep runtime objects off dataclass fields and API configuration dictionaries.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from functools import wraps
import os
import asyncio
import re
from pathlib import Path
from typing import Any

from .coordinator import OperationOwnershipError, PostgresCoordinator


@dataclass
class _Permit:
    runtime: DistributedRuntime
    operation: Any
    exclusive: bool
    maintenance: bool = False
    active: bool = True


_current: ContextVar[_Permit | None] = ContextVar("distributed_operation", default=None)
_write: ContextVar[tuple | None] = ContextVar("distributed_storage_write", default=None)


def enabled_from_env() -> bool:
    value = os.getenv("LIGHTRAG_DISTRIBUTED_WRITES", "false").lower()
    if value not in {"true", "false"}:
        raise ValueError("LIGHTRAG_DISTRIBUTED_WRITES must be true or false")
    return value == "true"


def get_runtime(obj) -> DistributedRuntime | None:
    runtime = getattr(obj, "_distributed_runtime", None)
    return runtime if isinstance(runtime, DistributedRuntime) else None


def current_runtime() -> DistributedRuntime | None:
    permit = _current.get()
    return permit.runtime if permit is not None else None


class DistributedRuntime:
    def __init__(self, coordinator, *, workspace: str):
        self.coordinator = coordinator
        self.workspace = workspace
        self.initialized = False
        self.closed = False
        self._close_requested = False
        self.db = None
        self._db_init_lock = asyncio.Lock()
        self._init_lock = asyncio.Lock()
        self._pg_maintenance_schema_checked = False

    async def initialize(self):
        if self.closed:
            raise OperationOwnershipError(
                "Distributed runtime is closed; create a new instance"
            )
        async with self._init_lock:
            if not self.initialized:
                await self.coordinator.initialize()
                self.initialized = True

    def permit(self, *, exclusive=False, maintenance=False) -> _Permit:
        permit = _current.get()
        if (
            self.closed
            or permit is None
            or permit.runtime is not self
            or not permit.active
        ):
            raise OperationOwnershipError(
                "Mutation requires a live distributed operation"
            )
        if exclusive and not permit.exclusive:
            raise OperationOwnershipError(
                "An exclusive operation is required; shared admission cannot be upgraded"
            )
        if maintenance and not permit.maintenance:
            raise OperationOwnershipError(
                "Explicit distributed_maintenance() is required for migration"
            )
        return permit

    @asynccontextmanager
    async def operation(
        self, kind, *, exclusive=False, maintenance=False, detached=False, metadata=None
    ):
        existing = _current.get()
        if existing is not None and not detached:
            permit = self.permit(exclusive=exclusive, maintenance=maintenance)
            await self.coordinator.heartbeat(permit.operation)
            yield permit.operation
            return
        if self.closed:
            raise OperationOwnershipError("Distributed runtime is closed")
        try:
            async with self.coordinator.operation(
                kind, exclusive=exclusive, metadata=metadata
            ) as operation:
                permit = _Permit(self, operation, exclusive, maintenance)
                token = _current.set(permit)
                try:
                    yield operation
                finally:
                    # Children inherit this object, not a reusable bearer token.
                    permit.active = False
                    _current.reset(token)
        finally:
            if self._close_requested:
                await self.close()

    @asynccontextmanager
    async def lock(self, keys, *, namespace="GraphDB"):
        permit = self.permit()
        if isinstance(keys, str):
            keys = [keys]
        async with self.coordinator.lock(
            permit.operation, [f"{namespace}/{key}" for key in keys]
        ):
            yield

    @asynccontextmanager
    async def maintenance(self, *, kind="maintenance", metadata=None):
        await self.initialize()
        async with self.operation(
            kind, exclusive=True, maintenance=True, metadata=metadata
        ) as op:
            yield op

    async def initialize_pg_storage(self, storage):
        from lightrag.kg.postgres_impl import PostgreSQLDB, PGVectorStorage, TABLES

        permit = self.permit()
        async with self._db_init_lock:
            if self.db is None:
                self.db = PostgreSQLDB(self.pg_config)
                self.db._distributed_runtime = self
                await self.db.initdb()
            storage.db = self.db
            if storage.db.workspace and storage.db.workspace != self.workspace:
                raise ValueError("PostgreSQL workspace override disagrees with runtime")
            if permit.maintenance:
                if isinstance(storage, PGVectorStorage):
                    existing = await self.db.query(
                        "SELECT to_regclass($1) AS table_name",
                        [storage.table_name.lower()],
                    )
                    if not existing or existing["table_name"] is None:
                        kind = (
                            "HALFVEC"
                            if self.db.vector_index_type == "HNSW_HALFVEC"
                            else "VECTOR"
                        )
                        ddl = (
                            TABLES[storage.legacy_table_name]["ddl"]
                            .replace(storage.legacy_table_name, storage.table_name)
                            .replace(
                                "VECTOR(dimension)",
                                f"{kind}({storage.embedding_func.embedding_dim})",
                            )
                            .replace("CREATE TABLE ", "CREATE TABLE IF NOT EXISTS ", 1)
                        )
                        await self.db.execute(ddl)
                    # Tables are shared by workspaces: this permit never grants
                    # permission to convert types or remove existing indexes.
                    await self.verify_pg_table(storage)
                    await self.db._create_vector_index(
                        storage.table_name,
                        storage.embedding_func.embedding_dim,
                        migrate=False,
                    )
                else:
                    if not self._pg_maintenance_schema_checked:
                        # Explicit maintenance bootstrap is the only distributed
                        # path allowed to converge shared business PG tables.
                        # Normal startup below remains verify-only so an
                        # unprepared schema fails closed while writers may be
                        # active.
                        await self.db.check_tables()
                        self._pg_maintenance_schema_checked = True
                    from lightrag.kg.postgres_impl import namespace_to_table_name

                    table = namespace_to_table_name(storage.namespace)
                    ddl = TABLES[table]["ddl"].replace(
                        "CREATE TABLE ", "CREATE TABLE IF NOT EXISTS ", 1
                    )
                    await self.db.execute(ddl)
            await self.verify_pg_table(storage)
        if isinstance(storage, PGVectorStorage) and storage._flush_lock is None:
            # Immediate writes use per-call batches, never this shared buffer.
            storage._flush_lock = asyncio.Lock()

    async def verify_pg_table(self, storage):
        from lightrag.kg.postgres_impl import (
            PGVectorStorage,
            TABLES,
            namespace_to_table_name,
        )

        vector = isinstance(storage, PGVectorStorage)
        table = (
            storage.table_name if vector else namespace_to_table_name(storage.namespace)
        )
        base = storage.legacy_table_name if vector else table
        # Column inventory follows the authoritative DDL, not a duplicated schema.
        columns = re.findall(
            r"^\s*([a-z_]+)\s+(?:VARCHAR|TEXT|JSONB|INTEGER|INT4|TIMESTAMP|VECTOR)\b",
            TABLES[base]["ddl"],
            re.M | re.I,
        )
        try:
            await self.db.query(f"SELECT {', '.join(columns)} FROM {table} LIMIT 0")
            pk = await self.db.query(
                "SELECT array_agg(a.attname ORDER BY k.n) AS columns "
                "FROM pg_index i CROSS JOIN LATERAL unnest(i.indkey) WITH ORDINALITY k(attnum,n) "
                "JOIN pg_attribute a ON a.attrelid=i.indrelid AND a.attnum=k.attnum "
                "WHERE i.indrelid=to_regclass($1) AND i.indisprimary",
                [table.lower()],
            )
            if not pk or list(pk.get("columns") or []) != ["workspace", "id"]:
                raise RuntimeError("Distributed PG requires workspace/id primary key")
            if vector:
                row = await self.db.query(
                    "SELECT format_type(atttypid, atttypmod) AS type FROM pg_attribute "
                    "WHERE attrelid=to_regclass($1) AND attname='content_vector' AND NOT attisdropped",
                    [table.lower()],
                )
                kind = (
                    "halfvec"
                    if self.db.vector_index_type == "HNSW_HALFVEC"
                    else "vector"
                )
                if (
                    not row
                    or row["type"] != f"{kind}({storage.embedding_func.embedding_dim})"
                ):
                    raise RuntimeError("Distributed PG vector type/dimension mismatch")
        except Exception:
            raise RuntimeError(
                "Distributed PG schema/vector contract is not prepared; run explicit maintenance or offline legacy migration"
            ) from None

    async def request_close(self):
        permit = _current.get()
        if permit is not None and permit.runtime is self and permit.active:
            self._close_requested = True
        else:
            await self.close()

    async def close(self):
        self.closed = True
        try:
            try:
                if self.db is not None and self.db.pool is not None:
                    await self.db.pool.close()
            finally:
                failures = []
                for storage in getattr(self, "storages", []):
                    client = getattr(storage, "_client", None)
                    if client is not None:
                        try:
                            await client.close()
                        except Exception as error:
                            failures.append(error)
                if failures:
                    raise failures[0]
        finally:
            await self.coordinator.close()


def operation_guard(*, exclusive=False):
    """Guard a public SDK entry; nested calls retain the outer admission."""

    def decorate(function):
        @wraps(function)
        async def guarded(self, *args, **kwargs):
            runtime = get_runtime(self)
            if runtime is None:
                return await function(self, *args, **kwargs)
            async with runtime.operation(function.__name__, exclusive=exclusive):
                try:
                    result = await function(self, *args, **kwargs)
                except Exception:
                    # Existing business layers may wrap a CoordinationError.
                    # Re-check the durable fence before exposing that wrapper.
                    await runtime.coordinator.heartbeat(runtime.permit().operation)
                    raise
                # A swallowed physical-write failure must never become success.
                await runtime.coordinator.heartbeat(runtime.permit().operation)
                return result

        return guarded

    return decorate


def storage_write(function):
    """Require controlled storage calls; physical I/O journals independently."""

    @wraps(function)
    async def guarded(self, *args, **kwargs):
        runtime = get_runtime(self)
        if runtime is None:
            config = getattr(self, "global_config", {})
            if (
                isinstance(config, dict)
                and (
                    config["distributed_writes"]
                    if "distributed_writes" in config
                    else enabled_from_env()
                )
                is True
            ):
                raise OperationOwnershipError(
                    "Distributed storage is not bound to a runtime"
                )
            return await function(self, *args, **kwargs)
        if self.workspace != runtime.workspace:
            raise OperationOwnershipError(
                "Storage workspace disagrees with its runtime"
            )
        permit = runtime.permit(
            exclusive=function.__name__ in {"drop", "repair_source_conflict"}
        )
        await runtime.coordinator.heartbeat(permit.operation)
        token = _write.set(
            (runtime, type(self).__name__, self.namespace, function.__name__)
        )
        try:
            result = await function(self, *args, **kwargs)
            if isinstance(result, dict) and result.get("status") == "error":
                raise RuntimeError("Distributed storage mutation failed")
            await runtime.coordinator.heartbeat(permit.operation)
            return result
        finally:
            _write.reset(token)

    return guarded


def physical_write_active() -> bool:
    return _write.get() is not None


def _object_store_source_enabled_from_env() -> str | None:
    raw = os.getenv("LIGHTRAG_OBJECT_STORAGE", "").strip().lower()
    if raw in {"s3", "s3objectstore", "s3objectstorage"}:
        return "s3"
    return None


@asynccontextmanager
async def physical_write():
    descriptor = _write.get()
    if descriptor is None:
        yield
        return
    runtime, backend, namespace, method = descriptor
    permit = runtime.permit()
    async with runtime.coordinator.mutation(
        permit.operation, backend, namespace, method
    ):
        yield


def configure_runtime(rag) -> DistributedRuntime | None:
    """Validate the opt-in profile before constructing or opening any backend."""
    if rag.distributed_writes is not True:
        return None
    supported = {
        "kv_storage": "PGKVStorage",
        "vector_storage": "PGVectorStorage",
        "doc_status_storage": "PGDocStatusStorage",
        "graph_storage": "HugeGraphStorage",
    }
    if not isinstance(rag.workspace, str) or not rag.workspace.strip():
        raise ValueError("Distributed writes require a non-empty workspace")
    for key, expected in supported.items():
        if getattr(rag, key) != expected:
            raise ValueError(f"Distributed writes require {key}={expected}")
    object_storage_provider = _object_store_source_enabled_from_env()
    shared_filesystem_required = object_storage_provider is None
    if (
        os.getenv("LIGHTRAG_SHARED_STORAGE", "").lower() != "true"
        and shared_filesystem_required
    ):
        raise ValueError(
            "Distributed writes require LIGHTRAG_SHARED_STORAGE=true and shared persistent paths"
        )
    dsn = os.getenv("LIGHTRAG_COORDINATION_DSN")
    deployment = os.getenv("LIGHTRAG_DEPLOYMENT_ID", "")
    if not dsn or not deployment.strip():
        raise ValueError(
            "Distributed writes require LIGHTRAG_COORDINATION_DSN and LIGHTRAG_DEPLOYMENT_ID"
        )
    if os.getenv("LIGHTRAG_COORDINATION_POOL_MODE", "direct") not in {
        "direct",
        "session",
    }:
        raise ValueError(
            "Coordination requires direct PostgreSQL or session pooling; transaction/statement pooling cannot hold a witness"
        )
    from lightrag.kg.postgres_impl import ClientManager
    from lightrag.kg.hugegraph_client import HugeGraphClient

    pg = ClientManager.get_config(vector_storage="PGVectorStorage")
    if pg["workspace"] and pg["workspace"] != rag.workspace:
        raise ValueError(
            "POSTGRES_WORKSPACE must not override the distributed workspace"
        )
    settings = dict(
        part.split("=", 1)
        for part in (pg.get("server_settings") or "").split("&")
        if "=" in part
    )
    if settings.get("search_path", "public") != "public":
        raise ValueError("Distributed PostgreSQL requires search_path=public")
    settings["search_path"] = "public"
    pg["server_settings"] = "&".join(
        f"{key}={value}" for key, value in settings.items()
    )
    graph = HugeGraphClient()
    embedding = rag.embedding_func
    model = getattr(embedding, "model_name", None)
    dimension = getattr(embedding, "embedding_dim", None)
    if not model or not isinstance(dimension, int) or dimension < 1:
        raise ValueError(
            "Distributed writes require embedding model_name and positive embedding_dim"
        )
    manifest = {
        "profile": 1,
        "storages": supported,
        "workspace": rag.workspace,
        "postgres_schema": "public",
        "postgres": {
            key: str(pg[key])
            for key in ("host", "port", "database", "vector_index_type")
        },
        "hugegraph": {"uri": graph.uri, "graph_path": graph.graph_path},
        "embedding": {"model": model, "dimension": dimension},
        "paths": {
            "working": str(Path(rag.working_dir).resolve()),
            "input": str(
                Path(
                    getattr(rag, "distributed_input_dir", None)
                    or os.getenv("INPUT_DIR", "./inputs")
                ).resolve()
            ),
        },
        "shared_filesystem_required": shared_filesystem_required,
    }
    if object_storage_provider is not None:
        manifest["object_storage"] = {"provider": object_storage_provider}
    runtime = DistributedRuntime(
        PostgresCoordinator(dsn, deployment, rag.workspace, manifest),
        workspace=rag.workspace,
    )
    # Private runtime-only configuration: never a dataclass or exported config.
    runtime.pg_config = pg
    return runtime


def initialization_guard(function):
    @wraps(function)
    async def guarded(self, *args, **kwargs):
        runtime = get_runtime(self)
        if runtime is None:
            return await function(self, *args, **kwargs)
        await runtime.initialize()
        async with runtime.operation("initialize"):
            return await function(self, *args, **kwargs)

    return guarded


def finalization_guard(function):
    @wraps(function)
    async def guarded(self, *args, **kwargs):
        runtime = get_runtime(self)
        if runtime is None:
            return await function(self, *args, **kwargs)
        from .pipeline import stop_polling

        await stop_polling(self)
        admitted = False
        try:
            async with runtime.operation("finalize", exclusive=True):
                admitted = True
                result = await function(self, *args, **kwargs)
                await runtime.coordinator.heartbeat(runtime.permit().operation)
                return result
        finally:
            # Admission refusal/cancellation must not shut down active callers.
            if admitted:
                await runtime.request_close()

    return guarded


async def record_tracking_recovery(targets):
    """Durably name tracking rows before source removal or attribution shrink."""
    runtime = current_runtime()
    if runtime is None:
        return
    rows = [
        {"namespace": storage.namespace, "key": key}
        for storage, key in targets
        if storage is not None and key
    ]
    await runtime.coordinator.set_phase(
        runtime.permit().operation,
        "settle_tracking",
        metadata={"tracking_recovery": rows},
    )
