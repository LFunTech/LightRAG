"""Real PostgreSQL tests; only disposable local_debug_* coordination scopes."""

import asyncio
import importlib
import os
from uuid import uuid4

import pytest

pytestmark = pytest.mark.integration
DSN_ENV = "LIGHTRAG_TEST_COORDINATION_DSN"
MANIFEST = {"graph": "local_debug_graph", "embedding_dim": 3}


@pytest.fixture
async def clients():
    api = importlib.import_module("lightrag.distributed")
    assert hasattr(api, "PostgresCoordinator"), "Durable coordinator is missing"
    dsn = os.environ.get(DSN_ENV)
    if not dsn:
        pytest.skip(f"Set {DSN_ENV} to an isolated test PostgreSQL database")
    await api.PostgresCoordinator.migrate(dsn)
    scope = "local_debug_" + uuid4().hex
    result = [
        api.PostgresCoordinator(
            dsn,
            "local_debug_tests",
            scope,
            MANIFEST,
            wait_timeout=0.15,
            poll_interval=0.01,
        )
        for _ in range(2)
    ]
    for c in result:
        await c.initialize()
    yield result
    for c in result:
        await c.close()
    # Retain durable history as production would; scopes are isolated by UUID.


async def test_migration_is_idempotent_and_manifest_mismatch_refuses(clients):
    first, _ = clients
    api = importlib.import_module("lightrag.distributed")
    await first.migrate(os.environ[DSN_ENV])
    other = api.PostgresCoordinator(
        os.environ[DSN_ENV], "local_debug_tests", first.workspace, {"embedding_dim": 4}
    )
    with pytest.raises(api.ConfigurationMismatchError):
        await other.initialize()
    await other.close()
    assert (await first.inspect())["generation"] == 1


async def test_shared_parallel_and_exclusive_wait_timeout_does_not_fence(clients):
    a, b = clients
    api = importlib.import_module("lightrag.distributed")
    async with a.operation("ingest") as one:
        async with b.operation("ingest") as two:
            assert one.id != two.id
        with pytest.raises(api.CoordinationBusyError):
            async with b.operation("clear", exclusive=True):
                pytest.fail("Exclusive ticket overlapped a shared ticket")
        assert not (await a.inspect())["fenced"]
    async with b.operation("clear", exclusive=True):
        with pytest.raises(api.CoordinationBusyError):
            async with a.operation("ingest"):
                pytest.fail("Shared ticket bypassed maintenance")
    async with a.operation("ingest"):
        pass


async def test_resource_multilocks_parallel_and_child_task_not_reentrant(clients):
    a, b = clients
    api = importlib.import_module("lightrag.distributed")
    async with a.operation("ingest") as one, b.operation("ingest") as two:
        async with a.lock(one, ["B", "A", "A"]):
            async with a.lock(one, ["A"]):
                pass
            async with b.lock(two, ["C"]):
                pass

            async def contend(coordinator, operation):
                with pytest.raises(api.CoordinationBusyError):
                    async with coordinator.lock(operation, ["A"]):
                        pytest.fail("Conflicting lock bypassed task ownership")

            await asyncio.gather(contend(a, one), contend(b, two))
        async with b.lock(two, ["A", "B"]):
            pass


async def test_claim_is_owned_and_persists_all_phases(clients):
    a, b = clients
    api = importlib.import_module("lightrag.distributed")
    async with a.operation("ingest") as one, b.operation("ingest") as two:
        assert await a.try_claim_document(one, "doc-1")
        assert not await b.try_claim_document(two, "doc-1")
        await a.set_phase(one, "parse", doc_id="doc-1")
        await a.heartbeat(one)
        assert (await a.inspect())["claims"][0]["phase"] == "parse"
        with pytest.raises(api.OperationOwnershipError):
            await b.release_claim(two, "doc-1")
        await a.assert_claim(one, "doc-1")
        await a.release_claim(one, "doc-1")
        assert await b.try_claim_document(two, "doc-1")


async def test_pending_before_yield_ack_and_disjoint_mutations_overlap(clients):
    a, b = clients
    async with a.operation("ingest") as one, b.operation("ingest") as two:
        async with a.mutation(one, "graph", "entities", "upsert"):
            assert (await b.inspect())["mutations"][0]["state"] == "pending"
            async with b.mutation(two, "vector", "chunks", "upsert"):
                states = [m["state"] for m in (await a.inspect())["mutations"]]
                assert states == ["pending", "pending"]
    assert {m["state"] for m in (await a.inspect())["mutations"]} == {"ack"}


async def test_caught_mutation_failure_keeps_fence_after_normal_operation_exit(clients):
    a, b = clients
    api = importlib.import_module("lightrag.distributed")
    with pytest.raises(api.WorkspaceFencedError):
        async with a.operation("ingest") as op:
            with pytest.raises(RuntimeError, match="lost response"):
                async with a.mutation(op, "graph", "entities", "upsert"):
                    raise RuntimeError("lost response")
    state = await b.inspect()
    assert state["fenced"]
    assert state["mutations"][0]["state"] == "pending"
    with pytest.raises(api.WorkspaceFencedError):
        async with b.operation("delete", exclusive=True):
            pytest.fail("Uncertain graph write was forgotten")


async def test_recovery_requires_three_true_confirmations_and_keeps_audit(clients):
    a, b = clients
    api = importlib.import_module("lightrag.distributed")
    with pytest.raises(RuntimeError):
        async with a.operation("ingest", metadata={"request_id": "req-1"}) as op:
            assert await a.try_claim_document(op, "doc-1")
            await a.set_phase(op, "graph", metadata={"retry_intent": "manual"})
            async with a.lock(op, ["A"]):
                async with a.mutation(op, "graph", "entities", "upsert"):
                    raise RuntimeError("response lost")
    assert hasattr(b, "recover"), "Audited recovery is missing"
    for missing in ("writers_stopped", "inflight_finished", "state_audited"):
        confirmations = dict(
            writers_stopped=True, inflight_finished=True, state_audited=True
        )
        confirmations[missing] = False
        with pytest.raises(ValueError):
            await b.recover(
                expected_generation=1,
                actor="operator",
                reason="audited",
                **confirmations,
            )
        assert (await b.inspect())["generation"] == 1
    await a.close()
    result = await b.recover(
        expected_generation=1,
        actor="operator",
        reason="anchor audit complete",
        writers_stopped=True,
        inflight_finished=True,
        state_audited=True,
    )
    assert result["generation"] == 2
    assert not result["fenced"]
    assert not result["locks"] and not result["claims"]
    assert result["mutations"][0]["state"] == "recovered"
    assert result["operations"][0]["metadata"] == {
        "request_id": "req-1",
        "retry_intent": "manual",
    }
    assert result["recovery_audit"][0]["actor"] == "operator"
    assert result["recovery_audit"][0]["snapshot"]["locks"][0]["resource_key"] == "A"
    async with b.operation("ingest") as new_op:
        assert new_op.generation == 2
        with pytest.raises(api.OperationOwnershipError):
            await b.assert_claim(op, "doc-1")


async def test_read_only_inspection_does_not_register_or_change_scope(clients):
    a, _ = clients
    api = importlib.import_module("lightrag.distributed")
    inspector = api.PostgresCoordinator(
        os.environ[DSN_ENV], "local_debug_tests", a.workspace
    )
    await inspector.initialize(read_only=True)
    before = await a.inspect()
    assert await inspector.inspect() == before
    assert await a.inspect() == before
    await inspector.close()


async def test_witness_loss_fences_other_client_and_old_owner_cannot_reconnect(clients):
    a, b = clients
    api = importlib.import_module("lightrag.distributed")
    context = a.operation("ingest")
    op = await context.__aenter__()
    assert await a.try_claim_document(op, "doc-1")
    lock = a.lock(op, ["A"])
    await lock.__aenter__()
    pending = a.mutation(op, "graph", "entities", "upsert")
    await pending.__aenter__()
    # Abruptly lose only the session witness; the Python process and pool live.
    a._witness_connection.terminate()
    with pytest.raises(api.WorkspaceFencedError):
        async with b.operation("delete", exclusive=True):
            pytest.fail("A vanished witness allowed unsafe takeover")
    state = await b.inspect()
    assert state["fenced"] and state["locks"] and state["claims"]
    assert state["mutations"][0]["state"] == "pending"
    with pytest.raises(api.CoordinationUnavailableError):
        await a.heartbeat(op)
    with pytest.raises(api.CoordinationUnavailableError):
        await a.initialize()
    with pytest.raises(api.CoordinationUnavailableError):
        await pending.__aexit__(None, None, None)
    with pytest.raises(api.CoordinationUnavailableError):
        await lock.__aexit__(None, None, None)
    with pytest.raises(api.CoordinationUnavailableError):
        await context.__aexit__(None, None, None)


async def test_cancelled_mutation_keeps_pending_even_if_cleanup_database_unavailable(
    clients,
):
    a, b = clients
    api = importlib.import_module("lightrag.distributed")
    started = asyncio.Event()

    async def writer():
        async with a.operation("ingest") as op:
            async with a.lock(op, ["A"]):
                async with a.mutation(op, "graph", "entities", "upsert"):
                    started.set()
                    await asyncio.Event().wait()

    task = asyncio.create_task(writer())
    await started.wait()
    a._pool.terminate()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    with pytest.raises(api.WorkspaceFencedError):
        async with b.operation("write"):
            pytest.fail("Cancellation cleanup lost durable ownership")
    state = await b.inspect()
    assert state["locks"] and state["mutations"][0]["state"] == "pending"


async def test_late_ack_does_not_clear_another_mutations_fence(clients):
    a, b = clients
    api = importlib.import_module("lightrag.distributed")
    one_ctx, two_ctx = a.operation("ingest"), b.operation("ingest")
    one, two = await one_ctx.__aenter__(), await two_ctx.__aenter__()
    late = a.mutation(one, "graph", "A", "upsert")
    await late.__aenter__()
    with pytest.raises(RuntimeError):
        async with b.mutation(two, "graph", "B", "upsert"):
            raise RuntimeError("lost")
    await late.__aexit__(None, None, None)
    state = await b.inspect()
    assert state["fenced"]
    assert {m["state"] for m in state["mutations"]} == {"ack", "pending"}
    with pytest.raises(api.WorkspaceFencedError):
        async with a.mutation(one, "graph", "A", "upsert"):
            pytest.fail("An already admitted writer bypassed the new fence")
    for ctx in (one_ctx, two_ctx):
        with pytest.raises(api.WorkspaceFencedError):
            await ctx.__aexit__(None, None, None)


async def test_killed_independent_process_leaves_claim_lock_and_pending(clients):
    import sys
    from pathlib import Path

    a, b = clients
    api = importlib.import_module("lightrag.distributed")
    script = """
import asyncio, os, sys
from lightrag.distributed import PostgresCoordinator
async def main():
    c = PostgresCoordinator(os.environ["LIGHTRAG_TEST_COORDINATION_DSN"],
                           "local_debug_tests", sys.argv[1],
                           {"graph": "local_debug_graph", "embedding_dim": 3})
    await c.initialize()
    async with c.operation("ingest") as op:
        await c.try_claim_document(op, "killed-doc")
        async with c.lock(op, ["killed-entity"]):
            async with c.mutation(op, "graph", "entities", "upsert"):
                print("pending", flush=True)
                await asyncio.Event().wait()
asyncio.run(main())
"""
    process = await asyncio.create_subprocess_exec(
        sys.executable,
        "-c",
        script,
        a.workspace,
        cwd=Path(__file__).parents[2],
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        assert await asyncio.wait_for(process.stdout.readline(), 15) == b"pending\n"
        async with b.operation("independent") as op:
            async with b.lock(op, ["other-entity"]):
                async with b.mutation(op, "graph", "other", "upsert"):
                    assert len((await b.inspect())["mutations"]) == 2
            with pytest.raises(api.CoordinationBusyError):
                async with b.lock(op, ["killed-entity"]):
                    pytest.fail("A process-held resource was stolen")
        process.kill()
        await process.wait()
        for _ in range(100):
            state = await b.inspect()
            if state["orphaned_operations"]:
                break
            await asyncio.sleep(0.01)
        assert state["orphaned_operations"]
        fresh = api.PostgresCoordinator(
            os.environ[DSN_ENV], "local_debug_tests", a.workspace, MANIFEST
        )
        await fresh.initialize()
        try:
            with pytest.raises(api.WorkspaceFencedError):
                async with fresh.operation("delete", exclusive=True):
                    pytest.fail("Restart cleared a killed writer's barrier")
            state = await fresh.inspect()
            assert state["claims"][0]["doc_id"] == "killed-doc"
            assert state["locks"][0]["resource_key"] == "killed-entity"
            assert state["mutations"][0]["state"] == "pending"
        finally:
            await fresh.close()
    finally:
        if process.returncode is None:
            process.kill()
            await process.wait()


async def test_verify_only_fresh_database_never_creates_schema():
    import asyncpg
    from urllib.parse import urlsplit, urlunsplit

    api = importlib.import_module("lightrag.distributed")
    dsn = os.environ.get(DSN_ENV)
    if not dsn:
        pytest.skip(f"Set {DSN_ENV}")
    database = "local_debug_coordination_" + uuid4().hex
    admin = await asyncpg.connect(dsn)
    await admin.execute(f'CREATE DATABASE "{database}"')
    parsed = urlsplit(dsn)
    isolated_dsn = urlunsplit(parsed._replace(path="/" + database))
    c = api.PostgresCoordinator(
        isolated_dsn, "local_debug_tests", "workspace", MANIFEST
    )
    connection = None
    try:
        with pytest.raises(api.CoordinationSchemaError):
            await c.initialize()
        connection = await asyncpg.connect(isolated_dsn)
        assert (
            await connection.fetchval("SELECT to_regnamespace('lightrag_coordination')")
            is None
        )
        await api.PostgresCoordinator.migrate(isolated_dsn)
        await api.PostgresCoordinator.migrate(isolated_dsn)
        assert (
            await connection.fetchval(
                "SELECT count(*) FROM lightrag_coordination.schema_version"
            )
            == 1
        )
        await connection.execute(
            "ALTER TABLE lightrag_coordination.resource_locks DROP CONSTRAINT resource_locks_pkey"
        )
        drifted = api.PostgresCoordinator(
            isolated_dsn, "local_debug_tests", "workspace", MANIFEST
        )
        try:
            with pytest.raises(api.CoordinationSchemaError):
                await drifted.initialize()
        finally:
            await drifted.close()

    finally:
        await c.close()
        if connection:
            await connection.close()
        await admin.execute(f'DROP DATABASE "{database}"')
        await admin.close()


async def test_recovery_rejects_live_writer_and_stale_inspection_generation(clients):
    a, b = clients
    api = importlib.import_module("lightrag.distributed")
    async with a.operation("ingest"):
        with pytest.raises(api.CoordinationBusyError):
            await b.recover(
                expected_generation=1,
                actor="operator",
                reason="checked",
                writers_stopped=True,
                inflight_finished=True,
                state_audited=True,
            )
        assert (await b.inspect())["generation"] == 1
    await b.recover(
        expected_generation=1,
        actor="operator",
        reason="checked",
        writers_stopped=True,
        inflight_finished=True,
        state_audited=True,
    )
    with pytest.raises(api.OperationOwnershipError):
        await b.recover(
            expected_generation=1,
            actor="operator",
            reason="stale",
            writers_stopped=True,
            inflight_finished=True,
            state_audited=True,
        )
    assert len((await b.inspect())["recovery_audit"]) == 1


async def test_claim_release_refuses_inflight_mutation_and_operation_retains_child_lock(
    clients,
):
    a, b = clients
    api = importlib.import_module("lightrag.distributed")
    context = a.operation("ingest")
    op = await context.__aenter__()
    await a.try_claim_document(op, "doc-1")
    async with a.mutation(op, "graph", "entities", "upsert"):
        with pytest.raises(api.OperationOwnershipError):
            await a.release_claim(op, "doc-1")
    entered = asyncio.Event()
    release = asyncio.Event()

    async def child():
        async with a.lock(op, ["A"]):
            entered.set()
            await release.wait()

    task = asyncio.create_task(child())
    await entered.wait()
    with pytest.raises(api.WorkspaceFencedError):
        await context.__aexit__(None, None, None)
    release.set()
    with pytest.raises(api.WorkspaceFencedError):
        await task
    assert (await b.inspect())["locks"]


async def test_cli_inspect_and_recover_execute_real_audited_protocol(clients):
    import json
    import sys
    from pathlib import Path

    a, b = clients
    with pytest.raises(RuntimeError):
        async with a.operation("ingest") as op:
            async with a.mutation(op, "graph", "entities", "upsert"):
                raise RuntimeError("lost response")
    await a.close()

    async def command(action, *extra):
        arguments = (
            []
            if action == "migrate"
            else [
                "--deployment-id",
                "local_debug_tests",
                "--workspace",
                b.workspace,
            ]
        )
        process = await asyncio.create_subprocess_exec(
            sys.executable,
            "-m",
            "lightrag.distributed",
            action,
            *arguments,
            *extra,
            cwd=Path(__file__).parents[2],
            env={**os.environ, "LIGHTRAG_COORDINATION_DSN": os.environ[DSN_ENV]},
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        out, error = await asyncio.wait_for(process.communicate(), 15)
        assert process.returncode == 0, error.decode()
        return json.loads(out)

    assert await command("migrate") == {"migration": "verified"}
    before = await b.inspect()
    assert (await command("inspect"))["fenced"]
    assert await b.inspect() == before
    after = await command(
        "recover",
        "--expected-generation",
        "1",
        "--actor",
        "cli-operator",
        "--reason",
        "backend quiescence and anchors audited",
        "--confirm-writers-stopped",
        "--confirm-inflight-finished",
        "--confirm-state-audited",
    )
    assert after["generation"] == 2 and not after["fenced"]
    assert after["recovery_audit"][0]["actor"] == "cli-operator"
    assert after["mutations"][0]["state"] == "recovered"


@pytest.fixture
async def isolated_database():
    """Create and remove only this test's own database for destructive drift probes."""
    import asyncpg
    from urllib.parse import urlsplit, urlunsplit
    from lightrag.distributed import PostgresCoordinator

    dsn = os.environ.get(DSN_ENV)
    if not dsn:
        pytest.skip(f"Set {DSN_ENV}")
    database = "local_debug_coordination_" + uuid4().hex
    admin = await asyncpg.connect(dsn)
    await admin.execute(f'CREATE DATABASE "{database}"')
    isolated_dsn = urlunsplit(urlsplit(dsn)._replace(path="/" + database))
    connection = None
    try:
        await PostgresCoordinator.migrate(isolated_dsn)
        connection = await asyncpg.connect(isolated_dsn)
        yield isolated_dsn, connection
    finally:
        if connection:
            await connection.close()
        await admin.execute(f'DROP DATABASE "{database}"')
        await admin.close()


@pytest.mark.parametrize(
    "column,wrong_default,wrong_type",
    [
        ("resource_locks.depth", "0", "bigint"),
        ("mutations.state", "'ack'", "varchar"),
        ("operations.state", "'completed'", "varchar"),
        ("workspaces.generation", "0", "integer"),
        ("workspaces.fenced", "true", "text USING fenced::text"),
    ],
)
@pytest.mark.parametrize("drift", ["default", "nullable", "type"])
async def test_verify_refuses_safety_column_drift_without_repairing_it(
    isolated_database,
    column,
    wrong_default,
    wrong_type,
    drift,
):
    from lightrag.distributed import CoordinationSchemaError, PostgresCoordinator

    dsn, connection = isolated_database
    table, name = column.split(".")

    async def catalog():
        return await connection.fetchrow(
            "SELECT format_type(a.atttypid,a.atttypmod) AS type,a.attnotnull,"
            "pg_get_expr(d.adbin,d.adrelid) AS default_expr "
            "FROM pg_attribute a LEFT JOIN pg_attrdef d "
            "ON d.adrelid=a.attrelid AND d.adnum=a.attnum "
            "WHERE a.attrelid=$1::regclass AND a.attname=$2",
            f"lightrag_coordination.{table}",
            name,
        )

    original = await catalog()
    if drift == "default":
        change = f"SET DEFAULT {wrong_default}"
    elif drift == "nullable":
        change = "DROP NOT NULL"
    else:
        # A boolean default cannot be implicitly cast to text by ALTER TYPE.
        if column == "workspaces.fenced":
            await connection.execute(
                "ALTER TABLE lightrag_coordination.workspaces ALTER COLUMN fenced DROP DEFAULT"
            )
        change = f"TYPE {wrong_type}"
    await connection.execute(
        f"ALTER TABLE lightrag_coordination.{table} ALTER COLUMN {name} {change}"
    )

    before = await catalog()
    coordinator = PostgresCoordinator(dsn, "local_debug_tests", "workspace", MANIFEST)
    try:
        with pytest.raises(CoordinationSchemaError):
            await coordinator.initialize()
        assert await catalog() == before
        with pytest.raises(CoordinationSchemaError):
            await PostgresCoordinator.migrate(dsn)
        assert await catalog() == before
    finally:
        await coordinator.close()
        # Restore this test's schema before its disposable database is removed.
        prefix = f"ALTER TABLE lightrag_coordination.{table} ALTER COLUMN {name}"
        await connection.execute(f"{prefix} DROP DEFAULT")
        await connection.execute(
            f"{prefix} TYPE {original['type']} USING {name}::{original['type']}"
        )
        await connection.execute(f"{prefix} SET NOT NULL")
        await connection.execute(f"{prefix} SET DEFAULT {original['default_expr']}")
        assert await catalog() == original


async def test_live_default_drift_cannot_release_outer_reentrant_lock(
    isolated_database,
):
    from lightrag.distributed import CoordinationBusyError, PostgresCoordinator

    dsn, connection = isolated_database
    clients = [
        PostgresCoordinator(
            dsn, "local_debug_tests", "workspace", MANIFEST, wait_timeout=0.05
        )
        for _ in range(2)
    ]
    for coordinator in clients:
        await coordinator.initialize()
    try:
        await connection.execute(
            "ALTER TABLE lightrag_coordination.resource_locks ALTER COLUMN depth SET DEFAULT 0"
        )
        a, b = clients
        async with a.operation("write") as one, b.operation("write") as two:
            async with a.lock(one, ["A"]):
                async with a.lock(one, ["A"]):
                    pass
                with pytest.raises(CoordinationBusyError):
                    async with b.lock(two, ["A"]):
                        pytest.fail(
                            "Default drift released an active outer resource lock"
                        )
    finally:
        await connection.execute(
            "ALTER TABLE lightrag_coordination.resource_locks ALTER COLUMN depth SET DEFAULT 1"
        )
        for coordinator in clients:
            await coordinator.close()


async def test_live_default_drift_never_publishes_premature_mutation_ack(
    isolated_database,
):
    from lightrag.distributed import PostgresCoordinator

    dsn, connection = isolated_database
    coordinator = PostgresCoordinator(dsn, "local_debug_tests", "workspace", MANIFEST)
    await coordinator.initialize()
    try:
        await connection.execute(
            "ALTER TABLE lightrag_coordination.mutations ALTER COLUMN state SET DEFAULT 'ack'"
        )
        async with coordinator.operation("write") as operation:
            async with coordinator.mutation(operation, "graph", "entities", "upsert"):
                assert (await coordinator.inspect())["mutations"][0][
                    "state"
                ] == "pending"
        assert (await coordinator.inspect())["mutations"][0]["state"] == "ack"
    finally:
        await connection.execute(
            "ALTER TABLE lightrag_coordination.mutations ALTER COLUMN state SET DEFAULT 'pending'"
        )
        await coordinator.close()
