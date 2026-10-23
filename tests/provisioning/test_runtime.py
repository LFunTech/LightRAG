from types import SimpleNamespace
from unittest.mock import AsyncMock
import importlib.util

import pytest


async def test_production_pg_startup_does_not_use_extension_provisioning(monkeypatch):
    from lightrag.kg.postgres_impl import PostgreSQLDB, ClientManager
    import asyncpg

    config = ClientManager.get_config(vector_storage="PGVectorStorage")
    config.update(
        connection_retry_attempts=1,
        user="fixture",
        password="synthetic",
        database="fixture",
    )
    db = PostgreSQLDB(config)
    db._distributed_runtime = SimpleNamespace(release=object())
    db.configure_vector_extension = AsyncMock(
        side_effect=AssertionError("DDL path invoked")
    )
    connection = SimpleNamespace(
        fetchval=AsyncMock(return_value="0.8.0"), close=AsyncMock()
    )
    monkeypatch.setattr(asyncpg, "connect", AsyncMock(return_value=connection))
    monkeypatch.setattr(asyncpg, "create_pool", AsyncMock(return_value=object()))
    await db.initdb()
    assert db.configure_vector_extension.await_count == 0
    assert connection.fetchval.await_count == 1


async def test_production_maintenance_cannot_use_legacy_business_bootstrap():
    from lightrag.distributed.runtime import DistributedRuntime

    runtime = DistributedRuntime(SimpleNamespace(), workspace="fixture")
    runtime.release = object()
    runtime.permit = lambda: SimpleNamespace(maintenance=True)
    runtime.db = SimpleNamespace(
        workspace="fixture",
        check_tables=AsyncMock(side_effect=AssertionError("legacy DDL")),
        execute=AsyncMock(side_effect=AssertionError("legacy DDL")),
    )
    runtime.verify_pg_table = AsyncMock()
    storage = SimpleNamespace(namespace="full_docs")
    await runtime.initialize_pg_storage(storage)
    assert runtime.verify_pg_table.await_count == 1
    assert runtime.db.check_tables.await_count == runtime.db.execute.await_count == 0


async def test_production_graph_maintenance_stays_verify_only():
    from lightrag.distributed.runtime import DistributedRuntime
    from lightrag.kg.hugegraph_impl import HugeGraphStorage

    runtime = DistributedRuntime(
        SimpleNamespace(heartbeat=AsyncMock()), workspace="fixture"
    )
    runtime.release = SimpleNamespace(require_writer=AsyncMock())
    runtime.permit = lambda **kwargs: SimpleNamespace(
        maintenance=True, kind="initialize", operation=object()
    )
    storage = object.__new__(HugeGraphStorage)
    storage._distributed_runtime = runtime
    storage.workspace = "fixture"
    storage.namespace = "graph"
    storage._client = SimpleNamespace(auto_create_schema=True, initialize=AsyncMock())
    await storage.initialize()
    assert storage._client.auto_create_schema is False


async def test_migration_gate_refuses_before_coordinator_registration(monkeypatch):
    assert importlib.util.find_spec("lightrag.provisioning.runtime"), (
        "Runtime schema gate is missing"
    )
    from lightrag.provisioning import runtime as gate
    from lightrag.distributed.runtime import DistributedRuntime

    coordinator = SimpleNamespace(initialize=AsyncMock())
    runtime = DistributedRuntime(coordinator, workspace="fixture")
    runtime.release = SimpleNamespace(initialize=AsyncMock())
    runtime.storage_migration_plan = object()
    check = AsyncMock(side_effect=ValueError("migration missing"))
    monkeypatch.setattr(gate, "verify_runtime_storage", check)
    with pytest.raises(ValueError):
        await runtime.initialize()
    assert (
        coordinator.initialize.await_count
        == runtime.release.initialize.await_count
        == 0
    )


async def test_runtime_gate_uses_business_identity_readonly_and_always_closes(
    monkeypatch,
):
    from contextlib import asynccontextmanager
    import asyncpg
    from lightrag.kg.postgres_impl import ClientManager
    from lightrag.provisioning import runtime as gate

    config = ClientManager.get_config(vector_storage="PGVectorStorage")
    config.update(
        host="business.invalid",
        user="runtime",
        password="fixture",
        database="business",
        ssl_mode="disable",
    )
    calls = []

    @asynccontextmanager
    async def transaction(**kwargs):
        calls.append(kwargs)
        yield

    conn = SimpleNamespace(transaction=transaction, close=AsyncMock())
    connect = AsyncMock(return_value=conn)
    monkeypatch.setattr(asyncpg, "connect", connect)
    verify = AsyncMock(side_effect=ValueError("not provisioned"))
    monkeypatch.setattr(gate, "verify", verify)
    monkeypatch.setattr(gate, "verify_runtime_access", AsyncMock())
    runtime = SimpleNamespace(pg_config=config, storage_migration_plan=object())
    with pytest.raises(ValueError):
        await gate.verify_runtime_storage(runtime)
    assert connect.call_args.kwargs["host"] == "business.invalid"
    assert connect.call_args.kwargs["user"] == "runtime"
    assert connect.call_args.kwargs["ssl"] is False
    assert calls == [{"readonly": True, "isolation": "repeatable_read"}]
    assert conn.close.await_count == 1


async def test_disabled_release_keeps_existing_test_initialization_without_migration_gate(
    monkeypatch,
):
    from lightrag.provisioning import runtime as gate
    from lightrag.distributed.runtime import DistributedRuntime

    check = AsyncMock(side_effect=AssertionError("test must not use new gate"))
    monkeypatch.setattr(gate, "verify_runtime_storage", check)
    coordinator = SimpleNamespace(initialize=AsyncMock())
    runtime = DistributedRuntime(coordinator, workspace="test")
    await runtime.initialize()
    assert runtime.initialized and coordinator.initialize.await_count == 1
    assert check.await_count == 0


async def test_runtime_gate_requires_prior_explicit_workspace_registration(monkeypatch):
    from contextlib import asynccontextmanager
    import asyncpg
    from lightrag.kg.postgres_impl import ClientManager
    from lightrag.provisioning import runtime as gate

    config = ClientManager.get_config(vector_storage="PGVectorStorage")
    config.update(user="runtime", password="fixture", database="fixture")

    @asynccontextmanager
    async def transaction(**kwargs):
        yield

    conn = SimpleNamespace(
        transaction=transaction,
        close=AsyncMock(),
        fetchrow=AsyncMock(return_value=None),
    )
    monkeypatch.setattr(asyncpg, "connect", AsyncMock(return_value=conn))
    monkeypatch.setattr(gate, "verify", AsyncMock())
    monkeypatch.setattr(gate, "verify_runtime_access", AsyncMock())
    runtime = SimpleNamespace(
        pg_config=config,
        storage_migration_plan=object(),
        coordinator=SimpleNamespace(_scope=("prod", "prod"), _manifest_hash="a" * 64),
    )
    with pytest.raises(ValueError, match="registration"):
        await gate.verify_runtime_storage(runtime)
    assert conn.close.await_count == 1
