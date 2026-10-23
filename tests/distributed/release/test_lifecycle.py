import asyncio
from types import SimpleNamespace

import pytest


async def test_instance_drain_waits_for_own_connections_not_peer_queries():
    from lightrag.distributed.release.lifecycle import InstanceLifecycle

    a, b = InstanceLifecycle(), InstanceLifecycle()
    async with b.track("queries"):
        async with a.track("waiters"):
            a.begin_retire()
            waiter = asyncio.create_task(a.drain())
            await asyncio.sleep(0)
            assert not waiter.done()
        await asyncio.wait_for(waiter, 0.2)
        assert b.snapshot()["queries"] == 1


async def test_cancelling_drain_waiter_does_not_cancel_active_work():
    from lightrag.distributed.release.lifecycle import InstanceLifecycle

    lifecycle = InstanceLifecycle()
    async with lifecycle.track("backend"):
        lifecycle.begin_retire()
        waiter = asyncio.create_task(lifecycle.drain())
        await asyncio.sleep(0)
        waiter.cancel()
        with pytest.raises(asyncio.CancelledError):
            await waiter
        assert lifecycle.snapshot()["backend"] == 1
    await lifecycle.drain()


async def test_readiness_distinguishes_standby_from_broken_polling():
    from lightrag.distributed.release.readiness import readiness
    from lightrag.distributed.release.lifecycle import InstanceLifecycle

    class Witness:
        def is_closed(self):
            return False

    coordinator = SimpleNamespace(
        _broken=False, _pool=object(), _witness_connection=Witness()
    )
    runtime = SimpleNamespace(
        initialized=True,
        closed=False,
        coordinator=coordinator,
        pipeline_task=None,
        pipeline_error=None,
    )
    release = SimpleNamespace(
        lifecycle=InstanceLifecycle(),
        controller_error=None,
        executor_error=None,
        scope_state={"active_slot": "blue", "gate_open": True},
        config=SimpleNamespace(slot="green"),
    )
    assert (await readiness(runtime, release))["ready"] is True
    release.config.slot = "blue"
    assert (await readiness(runtime, release)) == {
        "ready": False,
        "reason": "PollingUnavailable",
    }
    coordinator._broken = True
    assert (await readiness(runtime, release)) == {
        "ready": False,
        "reason": "CoordinatorUnavailable",
    }


def test_native_server_stops_listener_without_deleting_pod(monkeypatch):
    from lightrag.api.release_lifecycle import run_release_uvicorn
    from types import SimpleNamespace
    import uvicorn

    release = SimpleNamespace(shutdown_hook=None)
    app = SimpleNamespace(
        state=SimpleNamespace(
            rag=SimpleNamespace(_distributed_runtime=SimpleNamespace(release=release))
        )
    )
    calls = []

    class Server:
        def __init__(self, config):
            self.config = config
            self.should_exit = False

        def run(self):
            release.shutdown_hook()
            calls.append(self.should_exit)

    monkeypatch.setattr(uvicorn, "Server", Server)
    run_release_uvicorn(app, {"app": app, "host": "127.0.0.1", "port": 12345})
    assert calls == [True]
