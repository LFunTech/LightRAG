"""Actual body admission and detached background lifetime regression tests."""

import asyncio
from types import SimpleNamespace
import pytest
from fastapi import HTTPException
from lightrag.distributed.runtime import DistributedRuntime
from tests.distributed.test_coordinator_integration import clients as _clients

clients = _clients

pytestmark = pytest.mark.integration


async def test_background_gets_new_ticket_after_request_completed(clients):
    import importlib.util

    assert importlib.util.find_spec("lightrag.api.distributed") is not None, (
        "API operation lifecycle is missing"
    )
    from lightrag.api import distributed

    assert hasattr(distributed, "background_operation"), (
        "Background operation guard is missing"
    )
    a, _ = clients
    rag = SimpleNamespace(
        _distributed_runtime=DistributedRuntime(a, workspace=a.workspace)
    )
    entered = asyncio.Event()

    @distributed.background_operation()
    async def work(rag):
        await a.heartbeat(rag._distributed_runtime.permit().operation)
        entered.set()

    release = asyncio.Event()
    async with rag._distributed_runtime.operation("request"):

        async def later():
            await release.wait()
            await work(rag)

        task = asyncio.create_task(later())
    release.set()
    await task
    assert entered.is_set()
    assert not (await a.inspect())["fenced"]


async def test_http_business_refusal_does_not_fence_but_exclusive_body_waits(clients):
    import importlib.util

    assert importlib.util.find_spec("lightrag.api.distributed") is not None, (
        "API operation lifecycle is missing"
    )
    from lightrag.api import distributed

    assert hasattr(distributed, "http_operation"), (
        "HTTP body operation guard is missing"
    )
    a, b = clients
    rag = SimpleNamespace(
        _distributed_runtime=DistributedRuntime(b, workspace=b.workspace)
    )

    @distributed.http_operation(rag, exclusive=True)
    async def body():
        raise HTTPException(409, "already exists")

    from lightrag.distributed import CoordinationBusyError

    async with a.operation("pipeline"):
        with pytest.raises(CoordinationBusyError):
            await body()
    with pytest.raises(HTTPException) as caught:
        await body()
    assert caught.value.status_code == 409
    assert not (await b.inspect())["fenced"]
