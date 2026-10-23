import pytest

from .test_handover_integration import prepare

pytestmark = [pytest.mark.integration, pytest.mark.requires_db]


async def test_rehearsal_queue_correlation_is_scoped_to_actual_principal_and_key(
    release_db,
):
    from scripts.performance.control import OrchestratorControl
    from lightrag.distributed.release.commands import CommandRegistry

    store, identity, blue, green = await prepare(release_db)
    try:
        registry = CommandRegistry()

        async def handler(payload):
            return payload

        registry.register("http.update_entity", version=1, handler=handler)
        envelope = registry.encode(
            "http.update_entity",
            {"fixture": "not-business-data"},
            principal={"id": "rehearsal-principal"},
            scope=identity.scope,
            key="perf-run-edit",
        )
        queued = await blue._distributed_runtime.release.store.enqueue(envelope)
        settings = dict(
            scope=identity.scope,
            release_id=identity.release_id,
            green_config_hash=identity.config_hash,
            green_digest=identity.digest,
            epoch=1,
        )
        control = OrchestratorControl(store, object(), identity, expected_epoch=1)
        observed = await control.receipt(
            settings, key="perf-run-edit", principal_id="rehearsal-principal"
        )
        assert observed == dict(id=str(queued["id"]), state="QUEUED")
        assert (
            await control.receipt(
                settings, key="perf-run-edit", principal_id="other-principal"
            )
            is None
        )
        assert (
            await control.receipt(
                settings, key="other-key", principal_id="rehearsal-principal"
            )
            is None
        )
    finally:
        await blue.finalize_storages()
        await green.finalize_storages()


async def test_rehearsal_start_captures_blue_before_native_entry_proof(release_db):
    from scripts.performance.control import OrchestratorControl
    from lightrag.distributed.release.models import ReleaseIdentity
    from lightrag.distributed.release.store import ReleaseStore
    from lightrag.distributed.release.transport import ProductionTransport
    from .test_runtime_integration import setup_scope, make_rag

    dep, work = await setup_scope(release_db)
    store = ReleaseStore(release_db)
    blue = await make_rag(release_db, deployment=dep, workspace=work, instance="blue-1")
    identity = ReleaseIdentity(
        dep + "/" + work,
        "tool-start",
        "v1.2.3",
        "c" * 40,
        "sha256:" + "d" * 64,
        "e" * 64,
    )
    settings = dict(
        scope=identity.scope,
        release_id=identity.release_id,
        green_config_hash=identity.config_hash,
        green_digest=identity.digest,
        epoch=1,
        blue_config_hash="b" * 64,
        blue_digest="sha256:" + "b" * 64,
        profile_hash="a" * 64,
    )

    class Boundary:
        async def pods(self, slot):
            return [object()] * 4 if slot == "blue" else []

        async def verified_pods(self, slot, **kwargs):
            return [
                (
                    {"metadata": {"uid": "blue-" + str(i)}},
                    {
                        "instance_id": "blue-" + str(i),
                        "slot": "blue",
                        "profile_hash": "a" * 64,
                        "config_hash": "b" * 64,
                        "readiness": {"ready": True},
                    },
                )
                for i in range(4)
            ]

        async def verify_route(self, identity, target):
            expected = await ProductionTransport.expected_slot(self, identity, target)
            assert expected == dict(config_hash="b" * 64, digest="sha256:" + "b" * 64)
            return {"provider": "qcloud-boundary-fixture"}

    transport = Boundary()
    transport.store = store
    try:
        c = OrchestratorControl(store, transport, identity, expected_epoch=1)
        await c.start(settings)
        assert (await c.inspect(settings))["phase"] == "PREPARED"
    finally:
        await blue.finalize_storages()
