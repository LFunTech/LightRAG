"""HugeGraph distributed commits do not use a long whole-workspace mutex."""

import asyncio
import pytest

from lightrag.distributed import OperationOwnershipError
from lightrag.distributed.runtime import DistributedRuntime
from tests.distributed.test_runtime import Coordinator
from tests.kg.hugegraph_impl.test_storage import storage

pytestmark = pytest.mark.offline


async def test_each_hugegraph_batch_is_journaled_before_request_and_ack(monkeypatch):
    s = storage(monkeypatch)
    coordinator = Coordinator()
    rt = DistributedRuntime(coordinator, workspace=s.workspace)
    s._distributed_runtime = rt

    async def request(method, path, **kwargs):
        assert coordinator.events[-1][0] == "pending"
        return [r["id"] for r in kwargs["json"]]

    s._client.request.side_effect = request
    with pytest.raises(OperationOwnershipError):
        await s.upsert_node("a", {})
    async with rt.operation("ingest"):
        await s.upsert_nodes_batch([(str(i), {}) for i in range(3)])
    assert sum(e[0] == "pending" for e in coordinator.events) == 2
    assert sum(e[0] == "ack" for e in coordinator.events) == 2
    locks = [e[1] for e in coordinator.events if e[0] == "lock"]
    assert ("GraphDB/0", "GraphDB/1", "GraphDB/2") in locks


async def test_independent_hugegraph_entities_have_overlapping_real_requests(
    monkeypatch,
):
    s = storage(monkeypatch)
    rt = DistributedRuntime(Coordinator(), workspace=s.workspace)
    s._distributed_runtime = rt
    started = []
    both = asyncio.Event()

    async def request(method, path, **kwargs):
        started.append(kwargs["json"][0]["id"])
        if len(started) == 2:
            both.set()
        await asyncio.wait_for(both.wait(), 0.5)
        return [r["id"] for r in kwargs["json"]]

    s._client.request.side_effect = request

    async def write(name):
        async with rt.operation("ingest"):
            await s.upsert_node(name, {})

    await asyncio.gather(write("a"), write("b"))
    assert len(started) == 2


async def test_core_graph_keyed_lock_delegates_to_durable_runtime(monkeypatch):
    from lightrag.kg.shared_storage import get_storage_keyed_lock

    rt = DistributedRuntime(Coordinator(), workspace="test")
    async with rt.operation("ingest"):
        async with get_storage_keyed_lock(["a", "b"], namespace="test:GraphDB"):
            assert any(
                e == ("lock", ("GraphDB/a", "GraphDB/b")) for e in rt.coordinator.events
            )
