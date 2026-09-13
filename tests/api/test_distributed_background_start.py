"""Distributed startup restores child errors only after confirmed cleanup."""

import asyncio
from types import SimpleNamespace

import pytest

from lightrag.api.distributed import start_background_task
from lightrag.distributed import CoordinationBusyError
from lightrag.distributed.runtime import DistributedRuntime


@pytest.mark.parametrize("child_kind", ["busy", "cancel"])
@pytest.mark.parametrize("cleanup_kind", ["success", "runtime", "cancel"])
async def test_startup_cleanup_failure_has_priority(child_kind, cleanup_kind):
    rag = SimpleNamespace(
        _distributed_runtime=DistributedRuntime(None, workspace="startup-unit")
    )
    child_error = (
        CoordinationBusyError("admission refused")
        if child_kind == "busy"
        else asyncio.CancelledError("child cancelled")
    )
    cleanup_error = {
        "success": None,
        "runtime": RuntimeError("reservation cleanup failed"),
        "cancel": asyncio.CancelledError("cleanup cancelled"),
    }[cleanup_kind]
    events = []
    tasks = set()

    async def child(started):
        events.append("child failed before admission")
        raise child_error

    async def backstop():
        events.append("cleanup entered")
        if cleanup_error is not None:
            raise cleanup_error
        events.append("cleanup completed")

    expected = cleanup_error if cleanup_error is not None else child_error
    with pytest.raises(type(expected)) as caught:
        # Run the actual legacy starter; only its work/cleanup callbacks vary.
        await start_background_task(rag, tasks, work=child, backstop_release=backstop)
    assert caught.value is expected
    assert events[:2] == ["child failed before admission", "cleanup entered"]
    assert ("cleanup completed" in events) == (cleanup_error is None)
    assert not tasks
