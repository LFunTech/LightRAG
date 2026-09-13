"""Exercise worker death with a real shared Manager, without external services."""

import asyncio
import multiprocessing
import os
import subprocess
import sys
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest


@pytest.mark.offline
def test_dead_worker_fence_survives_lock_reclamation_and_instance_replacement():
    if "fork" not in multiprocessing.get_all_start_methods():
        pytest.skip("The supported Gunicorn preload model requires fork")
    env = {k: v for k, v in os.environ.items() if not k.startswith("HUGEGRAPH_")}
    env["HUGEGRAPH_URI"] = "http://example.invalid:8080"
    # Isolate the Manager lifecycle from all other tests' shared dictionaries.
    result = subprocess.run(
        [sys.executable, "-m", "tests.kg.hugegraph_impl.test_write_fence"],
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stdout + result.stderr


def _storage():
    from lightrag.kg.hugegraph_impl import HugeGraphStorage

    storage = HugeGraphStorage("graph", "worker-fence", {}, None)
    storage._client = SimpleNamespace(
        batch_size=2,
        graph_path="/graphspaces/DEFAULT/graphs/hugegraph",
        gremlin=AsyncMock(return_value=[]),
        request=AsyncMock(return_value=[storage._vertex_id("A")]),
    )
    return storage


def _abrupt_writer():
    storage = _storage()

    async def die_after_send(*args, **kwargs):
        # No Python finally/context-manager exit runs; the pending flag must
        # already exist in the Manager before this transport boundary.
        os._exit(17)

    storage._client.request.side_effect = die_after_send
    asyncio.run(storage.upsert_node("A", {"first": 1}))


async def _assert_fenced():
    storage = _storage()
    with pytest.raises(RuntimeError, match="Unconfirmed HugeGraph write"):
        await asyncio.wait_for(storage.upsert_node("A", {"successor": 2}), 5)
    storage._client.request.assert_not_awaited()
    storage._client.gremlin.assert_not_awaited()
    assert await storage.get_node("A") is None


async def _assert_new_domain_can_write():
    storage = _storage()
    await storage.upsert_node("A", {"replay": 1})
    storage._client.request.assert_awaited_once()


if __name__ == "__main__":
    from lightrag.kg.shared_storage import finalize_share_data, initialize_share_data

    initialize_share_data(2)
    child = multiprocessing.get_context("fork").Process(target=_abrupt_writer)
    try:
        child.start()
        child.join(10)
        if child.is_alive():
            child.kill()
            child.join()
            raise AssertionError("Writer did not reach the simulated transport")
        assert child.exitcode == 17
        asyncio.run(_assert_fenced())
    finally:
        finalize_share_data()

    # There is no actual server request in this isolated simulation; the old
    # worker is reaped, so an operator audit can establish quiescence here.
    initialize_share_data(2)
    try:
        asyncio.run(_assert_new_domain_can_write())
    finally:
        finalize_share_data()
