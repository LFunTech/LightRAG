"""Real claim ownership with small instrumented scheduler collaborators."""

import asyncio
from types import SimpleNamespace
import pytest
from tests.distributed.test_coordinator_integration import clients as _clients
from lightrag.distributed.runtime import DistributedRuntime
from lightrag.pipeline import _PipelineMixin, CURSOR_START, CURSOR_END
from lightrag.base import DocStatus

clients = _clients

pytestmark = pytest.mark.integration


class Rig(_PipelineMixin):
    max_parallel_insert = 1
    pipeline_scheduling_page_size = 1

    def __init__(self, coordinator, rows, handled):
        self.workspace = coordinator.workspace
        self._distributed_runtime = DistributedRuntime(
            coordinator, workspace=self.workspace
        )
        self.rows = rows
        self.handled = handled
        self.doc_status = self

    async def _fetch_scheduling_page(self, statuses, position, *, limit):
        index = 0 if position is CURSOR_START else position
        ids = list(self.rows)
        return (
            ids[index : index + limit],
            [],
            CURSOR_END if index + limit >= len(ids) else index + limit,
        )

    async def _hydrate_scheduling_page(self, ids, statuses):
        runtime = self._distributed_runtime
        for key in ids:
            await runtime.coordinator.assert_claim(runtime.permit().operation, key)
        return {
            key: self.rows[key]
            for key in ids
            if self.rows[key].status == DocStatus.PENDING
        }

    async def _validate_and_fix_document_consistency(self, docs, *args):
        for key in docs:
            await self._distributed_runtime.coordinator.assert_claim(
                self._distributed_runtime.permit().operation, key
            )
        return docs

    async def _run_pipeline_batch(self, docs, **kwargs):
        assert kwargs["ingress"] is None
        for key in docs:
            self.handled.append(key)
            self.rows[key].status = DocStatus.PROCESSED
        await asyncio.sleep(0.02)


async def test_claim_precedes_hydration_and_skips_claimed_first_page(clients):
    a, b = clients
    rows = {
        key: SimpleNamespace(status=DocStatus.PENDING) for key in ["first", "second"]
    }
    handled = []
    rag = Rig(b, rows, handled)
    assert hasattr(rag, "apipeline_start_polling"), (
        "Distributed scheduler lifecycle is missing"
    )
    async with a.operation("other") as op:
        await a.try_claim_document(op, "first")
        await rag.apipeline_process_enqueue_documents()
        assert handled == ["second"]
        await a.release_claim(op, "first")
    await rag.apipeline_process_enqueue_documents()
    assert handled == ["second", "first"]


async def test_two_claimed_schedulers_do_not_process_same_document(clients):
    a, b = clients
    rows = {str(i): SimpleNamespace(status=DocStatus.PENDING) for i in range(6)}
    handled = []
    assert hasattr(Rig, "apipeline_start_polling"), (
        "Distributed scheduler lifecycle is missing"
    )
    await asyncio.gather(
        *(Rig(c, rows, handled).apipeline_process_enqueue_documents() for c in [a, b])
    )
    assert sorted(handled) == list(rows)


async def test_cancelled_drain_waiter_does_not_cancel_admitted_worker(clients):
    a, _ = clients
    rows = {"one": SimpleNamespace(status=DocStatus.PENDING)}
    rag = Rig(a, rows, [])
    entered, release = asyncio.Event(), asyncio.Event()
    original = rag._run_pipeline_batch

    async def blocked(*args, **kwargs):
        entered.set()
        await release.wait()
        await original(*args, **kwargs)

    rag._run_pipeline_batch = blocked
    await rag.apipeline_start_polling(interval=0.01)
    await entered.wait()
    waiter = asyncio.create_task(rag.apipeline_stop_polling())
    await asyncio.sleep(0.01)
    waiter.cancel()
    with pytest.raises(asyncio.CancelledError):
        await waiter
    assert not rag._distributed_runtime.pipeline_task.cancelled()
    release.set()
    await rag.apipeline_stop_polling()
    state = await a.inspect()
    assert not state["fenced"]
    assert not state["claims"]


async def test_polling_supports_python310_create_task_signature(clients, monkeypatch):
    a, _ = clients
    rag = Rig(a, {}, [])
    create_task = asyncio.create_task

    def legacy_create_task(coro):
        return create_task(coro)

    monkeypatch.setattr(asyncio, "create_task", legacy_create_task)
    await rag.apipeline_start_polling(interval=0.01)
    await rag.apipeline_stop_polling()
