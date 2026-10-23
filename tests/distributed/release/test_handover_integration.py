import pytest

from .test_runtime_integration import setup_scope, make_rag

pytestmark = [pytest.mark.integration, pytest.mark.requires_db]


async def prepare(db):
    from lightrag.distributed.release.store import ReleaseStore
    from lightrag.distributed.release.models import ReleaseIdentity

    dep, work = await setup_scope(db)
    store = ReleaseStore(db)
    identity = ReleaseIdentity(
        dep + "/" + work, "r1", "v1.2.3", "c" * 40, "sha256:" + "d" * 64, "e" * 64
    )
    blue = await make_rag(db, deployment=dep, workspace=work, instance="blue-1")
    await store.begin(identity, expected_epoch=1)
    green = await make_rag(
        db,
        deployment=dep,
        workspace=work,
        instance="green-1",
        slot="green",
        config_hash="e" * 64,
    )
    await store.capture_baseline(
        identity, expected_epoch=1, digest="sha256:" + "b" * 64
    )
    for phase, following in [("PREPARED", "DRAINING"), ("DRAINING", "READ_SWITCHED")]:
        await store.transition(
            identity, expected_epoch=1, expected_phase=phase, next_phase=following
        )
    return store, identity, blue, green


async def test_transfer_uses_real_tickets_keeps_readers_and_fences_late_blue(
    release_db,
):
    from lightrag.distributed.release.models import ReleaseConflict

    store, identity, blue, green = await prepare(release_db)
    b = blue._distributed_runtime.release
    g = green._distributed_runtime.release
    try:
        # A live query must not require a workspace-exclusive release operation.
        async with blue._distributed_runtime.operation("aquery"):
            moved = await store.transfer_writer(
                identity, expected_epoch=1, expected_instances=["green-1"]
            )
            assert moved["epoch"] == 2 and moved["active_slot"] == "green"
            assert moved["gate_open"]
        assert await b.store.admit_writer(epoch=1, kind="late") is None
        assert await g.store.admit_writer(epoch=1, kind="stale") is None
        ticket = await g.store.admit_writer(epoch=2, kind="new")
        assert ticket is not None
        await g.store.finish_ticket(ticket)
        with pytest.raises(ReleaseConflict):
            await store.transfer_writer(
                identity, expected_epoch=1, expected_instances=["green-1"]
            )
    finally:
        await blue.finalize_storages()
        await green.finalize_storages()


async def test_unknown_writer_blocks_transfer_without_releasing_owner(release_db):
    from lightrag.distributed.release.models import ReleaseConflict
    from uuid import uuid4

    store, identity, blue, green = await prepare(release_db)
    try:
        # Unbound admission represents a crash before the original coordinator op.
        await release_db.execute(
            "INSERT INTO lightrag_release.tickets(id,scope,instance_id,slot,epoch,kind) VALUES($1,$2,'blue-1','blue',1,'crashed')",
            uuid4(),
            identity.scope,
        )
        with pytest.raises(ReleaseConflict):
            await store.transfer_writer(
                identity, expected_epoch=1, expected_instances=["green-1"]
            )
        snapshot = await store.snapshot(identity.scope)
        assert snapshot["owner"] == "r1" and snapshot["epoch"] == 1
        assert not snapshot["gate_open"]
        await release_db.execute(
            "UPDATE lightrag_release.tickets SET state='COMPLETED' WHERE scope=$1",
            identity.scope,
        )
    finally:
        await blue.finalize_storages()
        await green.finalize_storages()


async def test_uncertain_ticket_blocks_new_admission_even_with_open_gate(release_db):
    dep, work = await setup_scope(release_db)
    rag = await make_rag(release_db, deployment=dep, workspace=work, instance="blue-1")
    runtime = rag._distributed_runtime.release
    try:
        ticket = await runtime.store.admit_writer(epoch=1, kind="unknown")
        await runtime.store.finish_ticket(ticket, uncertain=True)
        assert await runtime.store.admit_writer(epoch=1, kind="other") is None
        # Test-only operator cleanup, not an application recovery path.
        await release_db.execute(
            "UPDATE lightrag_release.tickets SET state='COMPLETED' WHERE scope=$1",
            dep + "/" + work,
        )
    finally:
        await rag.finalize_storages()


async def test_runner_reentry_survives_epoch_transfer_and_refuses_older_version(
    release_db,
):
    from lightrag.distributed.release.models import ReleaseConflict, ReleaseIdentity

    store, identity, blue, green = await prepare(release_db)
    try:
        await store.transfer_writer(
            identity, expected_epoch=1, expected_instances=["green-1"]
        )
        assert (await store.begin(identity, expected_epoch=1))["epoch"] == 2
        with pytest.raises(ReleaseConflict):
            await store.begin(identity, expected_epoch=8)
        await store.transition(
            identity,
            expected_epoch=2,
            expected_phase="WRITER_SWITCHED",
            next_phase="RETIRING",
        )
        await blue.finalize_storages()
        await store.transition(
            identity,
            expected_epoch=2,
            expected_phase="RETIRING",
            next_phase="COMPLETED",
        )
        older = ReleaseIdentity(
            identity.scope, "r2", "v1.2.2", "c" * 40, "sha256:" + "d" * 64, "e" * 64
        )
        with pytest.raises(ReleaseConflict):
            await store.begin(older, expected_epoch=2)
    finally:
        if not blue.finalized:
            await blue.finalize_storages()
        await green.finalize_storages()


async def test_explicit_rollback_requires_bound_approval_and_new_drain(release_db):
    from lightrag.distributed.release.models import ReleaseConflict

    store, identity, blue, green = await prepare(release_db)
    try:
        await store.transfer_writer(
            identity, expected_epoch=1, expected_instances=["green-1"]
        )
        with pytest.raises(ReleaseConflict):
            await store.begin_rollback(identity, expected_epoch=2, approval={})
        approval = {
            "from_digest": identity.digest,
            "to_config_hash": "b" * 64,
            "to_digest": "sha256:" + "b" * 64,
            "profile_hash": "a" * 64,
            "read_write_compatible": True,
            "evidence_id": "explicit-fixture-approval",
        }
        row = await store.begin_rollback(identity, expected_epoch=2, approval=approval)
        assert not row["gate_open"]
        row = await store.reverse_transfer(
            identity, expected_epoch=2, expected_instances=["blue-1"]
        )
        assert row["epoch"] == 3 and row["active_slot"] == "blue" and row["gate_open"]
        assert (
            await green._distributed_runtime.release.store.admit_writer(
                epoch=2, kind="late"
            )
            is None
        )
        assert (
            await store.begin_rollback(identity, expected_epoch=2, approval=approval)
        )["phase"] == "ROLLBACK_WRITER_SWITCHED"
    finally:
        await blue.finalize_storages()
        await green.finalize_storages()


async def test_budget_exhaustion_is_typed_and_abort_keeps_owner_until_candidate_closes(
    release_db,
):
    from lightrag.distributed.release.models import (
        ReleaseBudgetExceeded,
        ReleaseConflict,
    )

    store, identity, blue, green = await prepare(release_db)
    try:
        await release_db.execute(
            "UPDATE lightrag_release.scopes SET drain_deadline=clock_timestamp()-interval '1 second' WHERE scope=$1",
            identity.scope,
        )
        with pytest.raises(ReleaseBudgetExceeded):
            await store.wait_for_drain(identity, expected_epoch=1)
        row = await store.abort_before_transfer(identity, expected_epoch=1)
        assert (
            row["phase"] == "ABORTING"
            and row["owner"] == identity.release_id
            and row["gate_open"]
        )
        with pytest.raises(ReleaseConflict):
            await store.finish_abort(identity, expected_epoch=1)
        await green.finalize_storages()
        row = await store.finish_abort(identity, expected_epoch=1)
        assert (
            row["phase"] == "ABORTED"
            and row["owner"] is None
            and row["active_slot"] == "blue"
        )
    finally:
        await blue.finalize_storages()
        if not green.finalized:
            await green.finalize_storages()
