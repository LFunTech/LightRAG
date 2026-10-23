import asyncio
from contextlib import asynccontextmanager
from types import SimpleNamespace


async def test_one_operator_session_is_not_used_concurrently_by_observer_and_control():
    from lightrag.distributed.release.store import ReleaseStore

    store = ReleaseStore(SimpleNamespace())
    inside, peak = 0, 0

    async def query():
        nonlocal inside, peak
        async with store.connection():
            inside += 1
            peak = max(peak, inside)
            await asyncio.sleep(0.01)
            inside -= 1

    await asyncio.gather(query(), query())
    assert peak == 1


async def test_runtime_pool_keeps_independent_connection_concurrency():
    from lightrag.distributed.release.store import ReleaseStore

    class Pool:
        @asynccontextmanager
        async def acquire(self):
            yield object()

    store = ReleaseStore(Pool())
    inside, peak = 0, 0

    async def query():
        nonlocal inside, peak
        async with store.connection():
            inside += 1
            peak = max(peak, inside)
            await asyncio.sleep(0.01)
            inside -= 1

    await asyncio.gather(query(), query())
    assert peak == 2
