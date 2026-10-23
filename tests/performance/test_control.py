from types import SimpleNamespace

import pytest


def settings():
    return dict(
        scope="isolation/run",
        release_id="release1",
        profile_hash="a" * 64,
        blue_config_hash="b" * 64,
        green_config_hash="c" * 64,
        epoch=1,
    )


class Transport:
    def __init__(self):
        self.counts = {"blue": 4, "green": 4}
        self.calls = []

    async def verified_pods(self, slot, **kwargs):
        return [
            (
                {"metadata": {"uid": slot + str(i)}},
                {
                    "instance_id": slot + str(i),
                    "slot": slot,
                    "profile_hash": "a" * 64,
                    "config_hash": ("b" if slot == "blue" else "c") * 64,
                    "readiness": {"ready": True},
                    "protocol_version": 1,
                    "shutdown_ready": True,
                },
            )
            for i in range(self.counts[slot])
        ]

    async def pods(self, slot):
        return [object()] * self.counts[slot]

    async def verify_route(self, identity, slot):
        self.calls.append(slot)
        return {"provider": "qcloud", "observed": "actual-adapter-boundary"}


async def test_control_checks_four_plus_four_and_route_not_just_ready():
    from scripts.performance.control import OrchestratorControl
    from lightrag.distributed.release.models import ReleaseIdentity

    identity = ReleaseIdentity(
        "isolation/run", "release1", "v1.0.0", "d" * 40, "sha256:" + "e" * 64, "c" * 64
    )
    state = dict(
        phase="DRAINING",
        epoch=1,
        active_slot="blue",
        candidate_slot="green",
        owner="release1",
    )

    class Store:
        async def snapshot(self, scope):
            return state.copy()

    t = Transport()
    control = OrchestratorControl(Store(), t, identity, expected_epoch=1)
    result = await control.inspect(settings())
    assert len(result["instances"]) == 8 and t.calls == ["blue"]
    t.counts["green"] = 3
    with pytest.raises(ValueError, match="slot"):
        await control.inspect(settings())
    assert state["phase"] == "DRAINING"


async def test_control_uses_real_orchestrator_and_refuses_foreign_identity_before_advance():
    from scripts.performance.control import OrchestratorControl
    from lightrag.distributed.release.models import ReleaseIdentity
    from tests.distributed.release.test_orchestrator import (
        Store,
        Transport as BaseTransport,
    )

    class T(BaseTransport):
        async def verify_candidate(self, *args):
            return ["g1"]

    store = Store()
    identity = ReleaseIdentity(
        "isolation/run", "release1", "v1.0.0", "d" * 40, "sha256:" + "e" * 64, "c" * 64
    )
    control = OrchestratorControl(store, T(), identity, expected_epoch=1)
    bad = settings()
    bad["scope"] = "prod/not-this-run"
    with pytest.raises(ValueError):
        await control.advance(bad)
    assert store.state["phase"] == "PREPARED"
    await control.advance(settings())
    assert store.state["phase"] == "DRAINING" and store.calls == ["DRAINING"]


async def test_delivery_context_is_reused_without_changing_normal_runner(monkeypatch):
    from contextlib import asynccontextmanager
    from lightrag.distributed.release import delivery

    assert hasattr(delivery, "delivery_session")
    from tests.distributed.release.test_orchestrator import Store, Transport as T

    store = Store()
    session = SimpleNamespace(
        store=store,
        transport=T(),
        identity=SimpleNamespace(scope="isolation/run"),
        original_epoch=1,
    )

    @asynccontextmanager
    async def context(**kwargs):
        yield session

    monkeypatch.setattr(delivery, "delivery_session", context)
    result = await delivery.deliver(
        config_path="unused",
        identity_path="unused",
        environment_path="unused",
        image="unused",
    )
    assert result["phase"] == "COMPLETED" and store.calls.count("TRANSFER") == 1


async def test_queue_receipt_reads_exact_scope_principal_and_key_without_mutation():
    from contextlib import asynccontextmanager
    from scripts.performance.control import OrchestratorControl
    from lightrag.distributed.release.models import ReleaseIdentity

    class DB:
        async def fetch(self, sql, *args):
            assert args == ("isolation/run", "principal-hash", "perf-run1-edit")
            assert sql.startswith("SELECT ")
            return [dict(id="11111111-1111-1111-1111-111111111111", state="QUEUED")]

    class Store:
        @asynccontextmanager
        async def connection(self):
            yield DB()

    identity = ReleaseIdentity(
        "isolation/run", "release1", "v1.0.0", "d" * 40, "sha256:" + "e" * 64, "c" * 64
    )
    control = OrchestratorControl(Store(), object(), identity, expected_epoch=1)
    result = await control.receipt(
        settings(), key="perf-run1-edit", principal_id="principal-hash"
    )
    assert result == dict(id="11111111-1111-1111-1111-111111111111", state="QUEUED")


def test_data_requests_must_use_the_same_origin_as_verified_qcloud_entry():
    from scripts.performance.control import OrchestratorControl
    from lightrag.distributed.release.models import ReleaseIdentity

    identity = ReleaseIdentity(
        "isolation/run", "release1", "v1.0.0", "d" * 40, "sha256:" + "e" * 64, "c" * 64
    )
    transport = SimpleNamespace(
        config=SimpleNamespace(target={"host": "isolated.invalid"})
    )
    control = OrchestratorControl(object(), transport, identity, expected_epoch=1)
    control.validate_target("https://isolated.invalid")
    for url in (
        "https://prod.invalid",
        "http://isolated.invalid",
        "https://isolated.invalid:8443",
    ):
        with pytest.raises(ValueError):
            control.validate_target(url)
