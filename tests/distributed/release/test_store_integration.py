from uuid import uuid4

import pytest

pytestmark = [pytest.mark.integration, pytest.mark.requires_db]


async def setup_store(db):
    from lightrag.distributed.release.store import ReleaseStore
    from lightrag.distributed.release.migrations import migrate

    await migrate(db)
    store = ReleaseStore(db)
    scope = "prod/" + uuid4().hex
    await store.onboard(
        scope,
        active_slot="blue",
        profile_hash="a" * 64,
        config_hash="b" * 64,
        policy={"queue_capacity": 10},
    )
    return store, scope


async def test_release_migration_is_idempotent_and_detects_drift(release_db):
    from lightrag.distributed.release.migrations import migrate, verify
    from lightrag.distributed.release.models import ReleaseSchemaError

    await migrate(release_db)
    await migrate(release_db)
    await verify(release_db)
    async with release_db.transaction():
        await release_db.execute(
            "ALTER TABLE lightrag_release.scopes ADD COLUMN drift text"
        )
        with pytest.raises(ReleaseSchemaError):
            await verify(release_db)
        await release_db.execute(
            "ALTER TABLE lightrag_release.scopes DROP COLUMN drift"
        )
    await verify(release_db)


async def test_release_owner_cannot_expire_or_be_replaced(release_db):
    from lightrag.distributed.release.models import ReleaseIdentity, ReleaseConflict

    store, scope = await setup_store(release_db)
    a = ReleaseIdentity(scope, "r1", "v1.2.3", "c" * 40, "sha256:" + "d" * 64, "e" * 64)
    b = ReleaseIdentity(scope, "r2", "v1.2.4", "f" * 40, "sha256:" + "a" * 64, "e" * 64)
    snapshot = await store.begin(a, expected_epoch=1)
    assert snapshot["phase"] == "PREPARED"
    assert (await store.begin(a, expected_epoch=1))["owner"] == "r1"
    with pytest.raises(ReleaseConflict):
        await store.begin(b, expected_epoch=1)
    row = await store.transition(
        a, expected_epoch=1, expected_phase="PREPARED", next_phase="DRAINING"
    )
    assert row["gate_open"] is False
    assert row["epoch"] == 1
    with pytest.raises(ReleaseConflict):
        await store.transition(
            a, expected_epoch=1, expected_phase="DRAINING", next_phase="WRITER_SWITCHED"
        )


async def test_runtime_database_role_cannot_change_owner(release_db):
    from lightrag.distributed.release.migrations import grant_runtime
    import asyncpg

    store, scope = await setup_store(release_db)
    role = "release_app_" + uuid4().hex[:12]
    await release_db.execute(f"CREATE ROLE {role}")
    try:
        await grant_runtime(release_db, role)
        await release_db.execute(f"SET ROLE {role}")
        try:
            with pytest.raises(asyncpg.InsufficientPrivilegeError):
                await release_db.execute(
                    "UPDATE lightrag_release.scopes SET owner='forged' WHERE scope=$1",
                    scope,
                )
            row = await release_db.fetchval(
                "SELECT active_slot FROM lightrag_release.scopes WHERE scope=$1", scope
            )
            assert row == "blue"
        finally:
            await release_db.execute("RESET ROLE")
    finally:
        await release_db.execute(f"DROP OWNED BY {role}")
        await release_db.execute(f"DROP ROLE {role}")


async def test_gate_admission_is_ordered_and_unknown_tickets_are_not_released(
    release_db,
):
    from lightrag.distributed.release.models import ReleaseIdentity
    from lightrag.distributed.release.admission import RuntimeStore

    store, scope = await setup_store(release_db)
    runtime = RuntimeStore(release_db, scope=scope, instance_id="pod-1")
    await runtime.register(
        slot="blue",
        config_hash="b" * 64,
        profile_hash="a" * 64,
        coordinator_owner=uuid4(),
    )
    ticket = await runtime.admit_writer(epoch=1, kind="sdk.create")
    assert ticket is not None
    identity = ReleaseIdentity(
        scope, "r1", "v1.2.3", "c" * 40, "sha256:" + "d" * 64, "e" * 64
    )
    await store.begin(identity, expected_epoch=1)
    await store.transition(
        identity, expected_epoch=1, expected_phase="PREPARED", next_phase="DRAINING"
    )
    assert await runtime.admit_writer(epoch=1, kind="sdk.create") is None
    assert (await store.snapshot(scope))["unfinished_tickets"] == 1
    await runtime.finish_ticket(ticket, uncertain=True)
    assert (await store.snapshot(scope))["unfinished_tickets"] == 1


async def test_command_queue_is_bounded_idempotent_and_durable(release_db):
    from lightrag.distributed.release.admission import RuntimeStore
    from lightrag.distributed.release.commands import CommandRegistry
    from lightrag.distributed.release.models import QueueFull, ReleaseConflict

    store, scope = await setup_store(release_db)
    runtime = RuntimeStore(release_db, scope=scope, instance_id="pod-1")
    await runtime.register(
        slot="blue",
        config_hash="b" * 64,
        profile_hash="a" * 64,
        coordinator_owner=uuid4(),
    )
    await release_db.execute(
        "UPDATE lightrag_release.scopes SET policy='{\"queue_capacity\":1}' WHERE scope=$1",
        scope,
    )
    registry = CommandRegistry()
    registry.register("sdk.create", version=1, handler=lambda payload: payload)
    envelope = registry.encode(
        "sdk.create", {"a": 1}, principal={"id": "u"}, scope=scope, key="k"
    )
    receipt = await runtime.enqueue(envelope)
    assert (await runtime.enqueue(envelope))["id"] == receipt["id"]
    assert (await runtime.command(receipt["id"], principal_id="u"))["state"] == "QUEUED"
    with pytest.raises(ReleaseConflict):
        await runtime.enqueue(
            registry.encode(
                "sdk.create", {"a": 2}, principal={"id": "u"}, scope=scope, key="k"
            )
        )
    with pytest.raises(QueueFull):
        await runtime.enqueue(
            registry.encode(
                "sdk.create", {"a": 1}, principal={"id": "u"}, scope=scope, key="other"
            )
        )
    assert await runtime.command(receipt["id"], principal_id="different-user") is None


async def test_claimed_command_unknown_result_never_returns_to_queue(release_db):
    from lightrag.distributed.release.admission import RuntimeStore
    from lightrag.distributed.release.commands import CommandRegistry
    from lightrag.distributed.release.models import ReleaseConflict

    store, scope = await setup_store(release_db)
    runtime = RuntimeStore(release_db, scope=scope, instance_id="pod-1")
    await runtime.register(
        slot="blue",
        config_hash="b" * 64,
        profile_hash="a" * 64,
        coordinator_owner=uuid4(),
    )
    registry = CommandRegistry()
    registry.register("sdk.create", version=1, handler=lambda payload: payload)
    receipt = await runtime.enqueue(
        registry.encode("sdk.create", {}, principal={"id": "u"}, scope=scope, key="k")
    )
    claimed = await runtime.claim_next(epoch=1)
    assert claimed["id"] == receipt["id"]
    assert claimed["state"] == "EXECUTING"
    assert await runtime.claim_next(epoch=1) is None
    await runtime.finish_command(
        receipt["id"], status="UNCERTAIN", result={"error": "LostAck"}
    )
    assert (await runtime.command(receipt["id"], principal_id="u"))[
        "state"
    ] == "UNCERTAIN"
    assert await runtime.claim_next(epoch=1) is None
    assert (await store.snapshot(scope))["unfinished_tickets"] == 1
    with pytest.raises(ReleaseConflict):
        await runtime.finish_command(receipt["id"], status="SUCCEEDED", result={})


async def test_database_rejects_missing_envelope_identity_before_acceptance(release_db):
    from lightrag.distributed.release.admission import RuntimeStore
    from lightrag.distributed.release.models import ReleaseConflict

    store, scope = await setup_store(release_db)
    runtime = RuntimeStore(release_db, scope=scope, instance_id="pod-1")
    await runtime.register(
        slot="blue",
        config_hash="b" * 64,
        profile_hash="a" * 64,
        coordinator_owner=uuid4(),
    )
    with pytest.raises(ReleaseConflict):
        await runtime._call(
            "enqueue_command", scope, "pod-1", uuid4(), "u", "key", "a" * 64, "{}"
        )
    assert (await store.snapshot(scope))["commands"] == {}


async def test_source_cleanup_and_queue_acceptance_are_atomically_ordered(release_db):
    from lightrag.distributed.release.admission import RuntimeStore
    from lightrag.distributed.release.commands import CommandRegistry
    from lightrag.distributed.release.models import ReleaseConflict

    store, scope = await setup_store(release_db)
    runtime = RuntimeStore(release_db, scope=scope, instance_id="pod-1")
    await runtime.register(
        slot="blue",
        config_hash="b" * 64,
        profile_hash="a" * 64,
        coordinator_owner=uuid4(),
    )
    ticket = await runtime.admit_writer(epoch=1, kind="cleanup")
    registry = CommandRegistry()
    registry.register("http.complete", version=1, handler=lambda payload: payload)
    await runtime.enqueue(
        registry.encode(
            "http.complete",
            {},
            principal={"id": "u"},
            scope=scope,
            key="one",
            source_refs=["session:protected"],
        )
    )
    assert not await runtime._call(
        "claim_source_cleanup", scope, "pod-1", ticket, "session:protected"
    )
    assert await runtime._call(
        "claim_source_cleanup", scope, "pod-1", ticket, "session:cleaning"
    )
    with pytest.raises(ReleaseConflict):
        await runtime.enqueue(
            registry.encode(
                "http.complete",
                {},
                principal={"id": "u"},
                scope=scope,
                key="two",
                source_refs=["session:cleaning"],
            )
        )
    await runtime.finish_ticket(ticket)


async def test_grant_runtime_refuses_an_existing_control_table_writer(release_db):
    from lightrag.distributed.release.migrations import grant_runtime

    store, scope = await setup_store(release_db)
    role = "release_unsafe_" + uuid4().hex[:12]
    await release_db.execute(f"CREATE ROLE {role}")
    try:
        await release_db.execute(f"GRANT UPDATE ON lightrag_release.scopes TO {role}")
        with pytest.raises(ValueError):
            await grant_runtime(release_db, role)
    finally:
        await release_db.execute(f"DROP OWNED BY {role}")
        await release_db.execute(f"DROP ROLE {role}")
