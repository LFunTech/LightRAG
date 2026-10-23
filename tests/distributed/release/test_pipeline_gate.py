from contextlib import asynccontextmanager
from types import SimpleNamespace


async def test_closed_gate_stops_new_batches_even_with_an_existing_parent_ticket():
    from lightrag.distributed.pipeline import process

    events = []

    class Release:
        async def can_claim_batch(self):
            events.append("checked")
            return False

    class Control:
        async def status(self):
            return {"paused": False, "pending_retries": False}

    @asynccontextmanager
    async def operation(kind):
        raise AssertionError("A closed release must not start a new pipeline batch")
        yield

    from lightrag.distributed.runtime import DistributedRuntime

    rt = DistributedRuntime(
        SimpleNamespace(pipeline_control=Control()), workspace="fixture"
    )
    rt.release = Release()
    rt.operation = operation
    rag = SimpleNamespace(
        _distributed_runtime=rt, max_parallel_insert=1, pipeline_scheduling_page_size=1
    )
    await process(rag, resume=False)
    assert events == ["checked"]
