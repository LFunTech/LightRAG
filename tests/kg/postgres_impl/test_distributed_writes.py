"""Physical PG commit boundaries in the distributed profile."""

import asyncio
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from lightrag.distributed import OperationOwnershipError
from lightrag.distributed.runtime import DistributedRuntime, storage_write
from lightrag.kg.postgres_impl import PostgreSQLDB, ClientManager, PGDocStatusStorage
from tests.kg.postgres_impl.test_postgres_vector_deferred import (
    _make_storage,
    _entity_data,
)
from tests.distributed.test_runtime import Coordinator

pytestmark = pytest.mark.offline


def runtime():
    coordinator = Coordinator()
    return DistributedRuntime(coordinator, workspace="test_ws"), coordinator


async def test_vector_upsert_commits_inside_entity_lock_without_shared_buffer():
    rt, c = runtime()
    s = _make_storage(namespace="entities")
    s._distributed_runtime = rt
    async with rt.operation("ingest"):
        async with rt.lock(["Alice"]):
            await s.upsert({"id": _entity_data()})
            assert len(s._captured_executemany) == 1, (
                "Vector upsert only buffered after entity lock"
            )
            assert s._pending_vector_docs == {}
    assert len(s._captured_executemany) == 1


async def test_vector_independent_upserts_can_embed_concurrently():
    rt, c = runtime()
    s = _make_storage(namespace="entities")
    s._distributed_runtime = rt
    entered = []
    release = asyncio.Event()
    original = s.embedding_func.func

    async def embed(texts, **kwargs):
        entered.append(texts)
        if len(entered) == 2:
            release.set()
        await asyncio.wait_for(release.wait(), 1)
        return await original(texts, **kwargs)

    s.embedding_func.func = embed

    async def write(name):
        async with rt.operation("ingest"):
            async with rt.lock([name]):
                await s.upsert({name: _entity_data(name)})

    await asyncio.gather(write("Alice"), write("Bob"))
    assert len(entered) == 2, (
        "Independent vector commits were serialized or merely buffered"
    )
    assert len(s._captured_executemany) == 2


async def test_vector_delete_is_immediate_and_direct_calls_cannot_bypass():
    rt, c = runtime()
    s = _make_storage()
    s._distributed_runtime = rt
    with pytest.raises(OperationOwnershipError):
        await s.delete(["id"])
    async with rt.operation("delete", exclusive=True):
        await s.delete(["id"])
        assert len(s._captured_execute) == 1
        assert not s._pending_vector_deletes


def db_with_connection(connection):
    config = ClientManager.get_config()
    config.update(
        connection_retry_attempts=3, connection_retry_backoff=0, password="test"
    )
    db = PostgreSQLDB(config)

    @asynccontextmanager
    async def acquire():
        yield connection

    db.pool = SimpleNamespace(acquire=acquire)
    db._before_sleep = AsyncMock()
    return db


async def test_uncertain_physical_write_is_not_retried_and_keeps_pending():
    rt, c = runtime()
    calls = []
    conn = SimpleNamespace()
    db = db_with_connection(conn)

    class Store:
        workspace = "test_ws"
        namespace = "full_docs"
        _distributed_runtime = rt

        @storage_write
        async def upsert(self):
            async def sql(connection):
                calls.append("committed-but-ack-lost")
                raise ConnectionResetError("lost")

            return await db._run_with_retry(sql)

    async with rt.operation("ingest"):
        with pytest.raises(ConnectionResetError):
            await Store().upsert()
    assert len(calls) == 1, "Uncertain write was replayed by connection retry"
    assert [e[0] for e in c.events].count("pending") == 1
    assert [e[0] for e in c.events].count("fence") == 1
    assert not any(e[0] == "ack" for e in c.events)


async def test_update_returning_journals_even_though_it_uses_query():
    rt, c = runtime()
    conn = SimpleNamespace(fetch=AsyncMock(return_value=[{"id": "doc"}]))
    db = db_with_connection(conn)
    s = PGDocStatusStorage("doc_status", "test_ws", {}, None)
    s.db = db
    s._distributed_runtime = rt
    async with rt.operation("ingest"):
        await s.update_doc_status_fields("doc", {"status": "processed"})
    assert any(e[0] == "pending" for e in c.events), (
        "UPDATE RETURNING bypassed physical journal"
    )
    assert any(e[0] == "ack" for e in c.events)


async def test_distributed_execute_never_swallows_unique_violation_as_upsert_success():
    import asyncpg

    rt, c = runtime()
    conn = SimpleNamespace(
        execute=AsyncMock(side_effect=asyncpg.UniqueViolationError("collision"))
    )
    db = db_with_connection(conn)

    class Store:
        namespace = "full_docs"
        workspace = "test_ws"
        _distributed_runtime = rt

        @storage_write
        async def upsert(self):
            await db.execute("INSERT INTO docs VALUES (1)", upsert=True)

    async with rt.operation("ingest"):
        with pytest.raises(asyncpg.UniqueViolationError):
            await Store().upsert()


async def test_distributed_normal_initialize_never_runs_table_migrations():
    rt, c = runtime()
    s = _make_storage(namespace="entities")
    s._distributed_runtime = rt
    db = s.db
    db.workspace = None
    db.query = AsyncMock(return_value={"type": "vector(3)"})
    db.execute = AsyncMock(side_effect=AssertionError("normal startup attempted DDL"))
    rt.db = db
    # A provisioned database must have all declared columns and the vector type.
    rt.verify_pg_table = AsyncMock()
    async with rt.operation("initialize"):
        await s.initialize()
    rt.verify_pg_table.assert_awaited_once_with(s)


async def test_pg_runtime_verify_rejects_wrong_vector_dimension_without_ddl():
    rt, c = runtime()
    s = _make_storage(namespace="entities")
    rt.db = s.db
    s.db.query = AsyncMock(
        side_effect=[[], {"columns": ["workspace", "id"]}, {"type": "vector(5)"}]
    )
    assert hasattr(rt, "verify_pg_table"), "Verify-only vector schema is missing"
    with pytest.raises(RuntimeError, match="vector"):
        await rt.verify_pg_table(s)
    s.db.execute.assert_not_awaited()


async def test_distributed_chunk_insert_normalizes_null_and_duplicate_cache_references():
    from lightrag.kg.postgres_impl import PGKVStorage

    rt, c = runtime()
    seen = []

    class Connection:
        async def executemany(self, sql, data):
            seen.extend(data)

    db = db_with_connection(Connection())
    s = PGKVStorage("text_chunks", "test_ws", {}, None)
    s._distributed_runtime = rt
    s.db = db
    async with rt.operation("chunk-write"):
        await s.upsert(
            {
                key: {
                    "tokens": 1,
                    "chunk_order_index": 0,
                    "full_doc_id": "doc",
                    "content": "same",
                    "file_path": "same.txt",
                    **value,
                }
                for key, value in {
                    "null": {"llm_cache_list": None},
                    "duplicate": {"llm_cache_list": ["a", "a"]},
                    "missing": {},
                }.items()
            }
        )
    import json

    assert [json.loads(row[7]) for row in seen] == [[], ["a"], []]


@pytest.mark.parametrize(
    "backend,method,args",
    [
        ("kv", "is_empty", ()),
        ("status", "is_empty", ()),
        ("vector", "get_by_id", ("id",)),
        ("vector", "get_by_ids", (["id"],)),
        ("vector", "get_vectors_by_ids", (["id"],)),
        ("kv", "delete", (["id"],)),
        ("status", "delete", (["id"],)),
    ],
)
async def test_distributed_pg_never_mistakes_backend_failure_for_absence_or_success(
    backend, method, args
):
    from lightrag.kg.postgres_impl import PGKVStorage

    rt, c = runtime()
    s = (
        _make_storage()
        if backend == "vector"
        else PGKVStorage("full_docs", "test_ws", {}, None)
        if backend == "kv"
        else PGDocStatusStorage("doc_status", "test_ws", {}, None)
    )
    if backend != "vector":
        s.db = SimpleNamespace()
    s._distributed_runtime = rt
    s.db.query = AsyncMock(side_effect=ConnectionResetError("backend read failed"))
    s.db._run_with_retry = AsyncMock(
        side_effect=ConnectionResetError("backend write failed")
    )
    async with rt.operation("test"):
        with pytest.raises(ConnectionResetError):
            await getattr(s, method)(*args)


async def test_distributed_status_batch_rejects_invalid_record_before_any_write():
    rt, c = runtime()
    s = PGDocStatusStorage("doc_status", "test_ws", {}, None)
    s._distributed_runtime = rt
    s.db = SimpleNamespace(_run_with_retry=AsyncMock())
    async with rt.operation("enqueue"):
        with pytest.raises(ValueError, match="Invalid distributed doc_status"):
            await s.upsert({"invalid": {"status": "pending"}})
    s.db._run_with_retry.assert_not_awaited()


async def test_distributed_read_only_pool_reconnect_does_not_require_write_ticket():
    rt, c = runtime()
    connection = SimpleNamespace(
        execute=AsyncMock(), fetchrow=AsyncMock(return_value={"extversion": "0.8.2"})
    )
    db = db_with_connection(connection)
    db._distributed_runtime = rt
    await db.configure_vector_extension(connection)
    connection.execute.assert_not_awaited()


async def test_distributed_read_queries_keep_connection_retries():
    rt, c = runtime()

    class Row(tuple):
        def keys(self):
            return ["id"]

    connection = SimpleNamespace(
        fetch=AsyncMock(
            side_effect=[ConnectionResetError("read failed"), [Row(["result"])]]
        )
    )
    db = db_with_connection(connection)
    db._distributed_runtime = rt
    assert await db.query("SELECT id FROM docs") == {"id": "result"}
    assert connection.fetch.await_count == 2
    assert not any(event[0] == "pending" for event in c.events)


@pytest.mark.parametrize("existing_type", ["vector(3)", "halfvec(5)"])
async def test_maintenance_rejects_existing_vector_schema_before_any_ddl(existing_type):
    rt, c = runtime()
    s = _make_storage(namespace="entities")
    s._distributed_runtime = rt
    db = db_with_connection(SimpleNamespace())
    db.workspace = None
    db.vector_index_type = "HNSW_HALFVEC"
    rt.db = db
    db.execute = AsyncMock()

    async def query(sql, *args, **kwargs):
        if "to_regclass" in sql and "pg_" not in sql:
            return {"table_name": s.table_name.lower()}
        if "pg_index " in sql:
            return {"columns": ["workspace", "id"]}
        if "format_type" in sql:
            return {"type": existing_type}
        return None

    db.query = AsyncMock(side_effect=query)
    async with rt.operation("bootstrap", exclusive=True, maintenance=True):
        with pytest.raises(RuntimeError, match="schema/vector"):
            await s.initialize()
    db.execute.assert_not_awaited()


@pytest.mark.parametrize("exists", [False, True])
async def test_maintenance_vector_provisioning_never_alters_or_drops(exists):
    rt, c = runtime()
    s = _make_storage(namespace="entities")
    s._distributed_runtime = rt
    db = db_with_connection(SimpleNamespace())
    db.workspace = None
    db.vector_index_type = "HNSW_HALFVEC"
    rt.db = db
    statements = []
    verified = False

    async def query(sql, *args, **kwargs):
        nonlocal verified
        if "to_regclass" in sql and "pg_" not in sql:
            return {"table_name": s.table_name.lower() if exists else None}
        if "pg_index " in sql:
            return {"columns": ["workspace", "id"]}
        if "format_type" in sql:
            verified = True
            return {"type": "halfvec(3)"}
        return None

    async def execute(sql, *args, **kwargs):
        assert not any(word in sql.upper() for word in ("ALTER ", "DROP "))
        if exists or "CREATE INDEX" in sql.upper():
            assert verified, "Existing schema must be verified before provisioning"
        statements.append(sql)

    db.query = AsyncMock(side_effect=query)
    db.execute = AsyncMock(side_effect=execute)
    async with rt.operation("bootstrap", exclusive=True, maintenance=True):
        await s.initialize()
    assert any("USING hnsw" in sql for sql in statements)
    assert any("CREATE TABLE" in sql for sql in statements) is not exists
