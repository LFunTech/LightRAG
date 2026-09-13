"""Application wiring stays verify-only and drains before finalization."""

from types import SimpleNamespace

import pytest
from tests.api import test_error_message_sanitization as sanitization

from lightrag.distributed.runtime import DistributedRuntime
from tests.api.test_error_message_sanitization import (
    _FakeLightRAG,
    _build_client,
)


@pytest.fixture(autouse=True)
def _isolated_config(monkeypatch):
    import lightrag.api.config as config

    saved = (config._global_args, config._initialized)
    fixture = sanitization._init_config.__wrapped__(monkeypatch)
    next(fixture)
    try:
        yield
    finally:
        fixture.close()
        config._global_args, config._initialized = saved


def test_distributed_lifespan_has_real_input_manifest_and_no_automatic_migration(
    monkeypatch,
):
    calls = []

    def init(self, **kwargs):
        self.received = kwargs
        self._distributed_runtime = DistributedRuntime(
            SimpleNamespace(), workspace="test"
        )

    async def initialize(self):
        calls.append("initialize")

    async def migrate(self):
        calls.append("migrate")

    async def start(self, **kwargs):
        calls.append("start_polling")

    async def stop(self):
        calls.append("stop_polling")

    async def finalize(self):
        calls.append("finalize")

    monkeypatch.setattr(_FakeLightRAG, "__init__", init)
    for name, function in [
        ("initialize_storages", initialize),
        ("check_and_migrate_data", migrate),
        ("apipeline_start_polling", start),
        ("apipeline_stop_polling", stop),
        ("finalize_storages", finalize),
    ]:
        monkeypatch.setattr(_FakeLightRAG, name, function, raising=False)
    with _build_client(monkeypatch) as client:
        from lightrag.api.config import global_args

        assert (
            client.app.state.rag.received["distributed_input_dir"]
            == global_args.input_dir
        )
        assert calls == ["initialize", "start_polling"]
    assert calls == ["initialize", "start_polling", "stop_polling", "finalize"]


def test_legacy_route_exception_conversion_preserves_typed_coordination_failure(
    monkeypatch,
):
    from lightrag.api.utils_api import internal_server_error
    from lightrag.distributed import WorkspaceFencedError

    client = _build_client(monkeypatch)

    @client.app.get("/_distributed_probe")
    async def probe():
        raise internal_server_error(
            WorkspaceFencedError("Workspace requires audited recovery")
        )

    response = client.get("/_distributed_probe")
    assert response.status_code == 503
    assert response.json()["detail"]["error"] == "WorkspaceFencedError"


async def test_cancelled_api_drain_waiter_does_not_cancel_background_writer():
    import asyncio
    import pytest
    from lightrag.api import distributed

    assert hasattr(distributed, "drain_background_tasks"), (
        "Safe API background drain is missing"
    )
    started, release = asyncio.Event(), asyncio.Event()

    async def writer():
        started.set()
        await release.wait()

    task = asyncio.create_task(writer())
    await started.wait()
    waiter = asyncio.create_task(distributed.drain_background_tasks({task}))
    await asyncio.sleep(0)
    waiter.cancel()
    with pytest.raises(asyncio.CancelledError):
        await waiter
    assert not task.cancelled()
    release.set()
    await distributed.drain_background_tasks({task})
