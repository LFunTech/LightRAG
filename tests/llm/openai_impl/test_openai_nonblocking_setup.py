"""SDK construction and lazy resources cannot stall the coordinator's loop."""

import asyncio
from contextvars import ContextVar
from contextlib import asynccontextmanager
from functools import cached_property
import threading
from uuid import uuid4

import httpx
from openai import AsyncOpenAI
import pytest

from lightrag.llm import openai as binding

pytestmark = pytest.mark.offline


def response(request, requests):
    requests.append(request)
    if request.url.path.endswith("embeddings"):
        return httpx.Response(
            200,
            json={
                "object": "list",
                "model": "offline-model",
                "data": [{"index": 0, "object": "embedding", "embedding": [0.1, 0.2]}],
                "usage": {"prompt_tokens": 2, "total_tokens": 2},
            },
        )
    return httpx.Response(
        200,
        json={
            "id": "offline-chat",
            "object": "chat.completion",
            "created": 1,
            "model": "offline-model",
            "choices": [
                {
                    "index": 0,
                    "finish_reason": "stop",
                    "message": {"role": "assistant", "content": "offline answer"},
                }
            ],
            "usage": {"prompt_tokens": 2, "completion_tokens": 2, "total_tokens": 4},
        },
    )


async def invoke(kind, client, **kwargs):
    if kind == "embedding":
        result = await binding.openai_embed.func(
            ["offline fixture"],
            model="offline-model",
            api_key="synthetic-key",
            max_token_size=0,
            client_configs={"http_client": client},
            **kwargs,
        )
        assert result.shape == (1, 2)
    else:
        assert (
            await binding.openai_complete_if_cache(
                "offline-model",
                "offline fixture",
                api_key="synthetic-key",
                openai_client_configs={"http_client": client},
                **kwargs,
            )
            == "offline answer"
        )


@pytest.mark.parametrize("kind", ["embedding", "llm"])
@pytest.mark.parametrize("boundary", ["constructor", "resource"])
async def test_loop_progresses_during_synchronous_sdk_setup(
    monkeypatch, kind, boundary
):
    loop = asyncio.get_running_loop()
    entered, release = threading.Event(), threading.Event()
    progressed = []
    requests = []

    def slow_setup():
        entered.set()
        progressed.append(release.wait(timeout=1))

    def watchdog():
        if entered.wait(timeout=2):
            loop.call_soon_threadsafe(release.set)

    class ColdClient(AsyncOpenAI):
        @cached_property
        def embeddings(self):
            if boundary == "resource" and kind == "embedding":
                slow_setup()
            return super().embeddings

        @cached_property
        def chat(self):
            if boundary == "resource" and kind == "llm":
                slow_setup()
            return super().chat

    original = binding.create_openai_async_client

    def create(**kwargs):
        if boundary == "constructor":
            slow_setup()
        return original(**kwargs)

    monkeypatch.setattr(binding, "AsyncOpenAI", ColdClient)
    monkeypatch.setattr(binding, "create_openai_async_client", create)
    guard = threading.Thread(target=watchdog)
    guard.start()
    client = httpx.AsyncClient(
        transport=httpx.MockTransport(lambda r: response(r, requests)), trust_env=False
    )
    try:
        await invoke(kind, client)
        assert progressed == [True], "SDK setup starved the event-loop callback"
        assert len(requests) == 1
        assert client.is_closed
    finally:
        release.set()
        await asyncio.to_thread(guard.join, 2)
        await client.aclose()


@pytest.mark.parametrize("kind", ["embedding", "llm"])
async def test_cancelled_setup_joins_worker_and_closes_without_dispatch(
    monkeypatch, kind
):
    loop = asyncio.get_running_loop()
    entered = asyncio.Event()
    release = threading.Event()
    requests = []
    original = binding.create_openai_async_client

    def create(**kwargs):
        loop.call_soon_threadsafe(entered.set)
        release.wait(timeout=2)
        return original(**kwargs)

    monkeypatch.setattr(binding, "create_openai_async_client", create)
    client = httpx.AsyncClient(
        transport=httpx.MockTransport(lambda r: response(r, requests)), trust_env=False
    )
    task = asyncio.create_task(invoke(kind, client))
    try:
        await asyncio.wait_for(entered.wait(), timeout=3)
        task.cancel()
        await asyncio.sleep(0)
        task.cancel()  # Repeated cancellation cannot abandon the thread/client.
        await asyncio.sleep(0)
        assert not task.done(), "Cancelled setup abandoned its resource owner"
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, timeout=3)
        assert client.is_closed and not requests
    finally:
        release.set()
        await asyncio.gather(task, return_exceptions=True)
        await client.aclose()


async def test_setup_preserves_caller_context_without_sharing_clients(monkeypatch):
    marker = ContextVar("offline-test-context")
    seen = []
    clients = []
    original = binding.create_openai_async_client

    def create(**kwargs):
        seen.append(marker.get())
        result = original(**kwargs)
        clients.append(result)
        return result

    monkeypatch.setattr(binding, "create_openai_async_client", create)

    async def call(value):
        token = marker.set(value)
        transport = httpx.AsyncClient(
            transport=httpx.MockTransport(lambda r: response(r, [])), trust_env=False
        )
        try:
            await invoke("embedding", transport)
            assert transport.is_closed
        finally:
            marker.reset(token)
            await transport.aclose()

    await asyncio.gather(call("one"), call("two"))
    assert sorted(seen) == ["one", "two"]
    assert len(clients) == 2 and clients[0] is not clients[1]


@pytest.mark.parametrize("kind", ["embedding", "llm"])
async def test_resource_preparation_failure_closes_owned_client(monkeypatch, kind):
    requests = []

    class BrokenResource(AsyncOpenAI):
        @cached_property
        def embeddings(self):
            raise ValueError("offline resource import failed")

        @cached_property
        def chat(self):
            raise ValueError("offline resource import failed")

    monkeypatch.setattr(binding, "AsyncOpenAI", BrokenResource)
    client = httpx.AsyncClient(
        transport=httpx.MockTransport(lambda r: response(r, requests)), trust_env=False
    )
    try:
        with pytest.raises(ValueError, match="offline resource import failed"):
            await invoke(kind, client)
        assert client.is_closed and not requests
    finally:
        await client.aclose()


async def test_repeated_cancellation_joins_slow_close_before_returning(monkeypatch):
    loop = asyncio.get_running_loop()
    entered, close_entered = asyncio.Event(), asyncio.Event()
    release = threading.Event()
    close_release = asyncio.Event()
    requests = []

    class SlowClose(AsyncOpenAI):
        @cached_property
        def embeddings(self):
            loop.call_soon_threadsafe(entered.set)
            release.wait(timeout=2)
            return super().embeddings

        async def close(self):
            close_entered.set()
            await close_release.wait()
            await super().close()

    monkeypatch.setattr(binding, "AsyncOpenAI", SlowClose)
    client = httpx.AsyncClient(
        transport=httpx.MockTransport(lambda r: response(r, requests)), trust_env=False
    )
    task = asyncio.create_task(invoke("embedding", client))
    try:
        await asyncio.wait_for(entered.wait(), timeout=3)
        task.cancel()
        release.set()
        await asyncio.wait_for(close_entered.wait(), timeout=3)
        task.cancel()
        await asyncio.sleep(0)
        assert not task.done() and not client.is_closed
        close_release.set()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, timeout=3)
        assert client.is_closed and not requests
    finally:
        release.set()
        close_release.set()
        await asyncio.gather(task, return_exceptions=True)
        await client.aclose()


@pytest.mark.parametrize("kind", ["embedding", "llm"])
@pytest.mark.parametrize("azure", [False, True])
async def test_dispatch_stays_on_caller_loop_and_context(kind, azure):
    loop, thread_id = asyncio.get_running_loop(), threading.get_ident()
    marker = ContextVar("offline-dispatch-context", default="missing")
    requests = []

    async def serve(request):
        assert asyncio.get_running_loop() is loop
        assert threading.get_ident() == thread_id
        assert marker.get() == "caller"
        return response(request, requests)

    kwargs = (
        {
            "use_azure": True,
            "base_url": "https://azure.invalid",
            "azure_deployment": "offline-deployment",
            "api_version": "2024-10-21",
        }
        if azure
        else {}
    )
    client = httpx.AsyncClient(transport=httpx.MockTransport(serve), trust_env=False)
    token = marker.set("caller")
    try:
        await invoke(kind, client, **kwargs)
        assert len(requests) == 1 and client.is_closed
        if azure:
            assert "/deployments/offline-deployment/" in requests[0].url.path
    finally:
        marker.reset(token)
        await client.aclose()


@pytest.mark.parametrize("kind", ["embedding", "llm"])
async def test_cold_sdk_setup_does_not_expire_concurrent_coordinator_command(
    monkeypatch, kind
):
    from lightrag.distributed.coordinator import Operation, PostgresCoordinator

    loop = asyncio.get_running_loop()
    entered, release = threading.Event(), threading.Event()
    waiting = asyncio.Event()

    class ColdClient(AsyncOpenAI):
        @cached_property
        def embeddings(self):
            entered.set()
            release.wait(timeout=0.5)
            return super().embeddings

        @cached_property
        def chat(self):
            entered.set()
            release.wait(timeout=0.5)
            return super().chat

    class Connection:
        def get_server_pid(self):
            return 101

        @asynccontextmanager
        async def transaction(self):
            yield

        async def fetchrow(self, *args):
            waiting.set()
            # A bounded driver round trip sharing the application's event loop.
            await asyncio.wait_for(asyncio.sleep(0.005), timeout=0.1)
            return {"generation": 1}

        async def execute(self, *args):
            pass

    class Pool:
        terminated = False

        def is_closed(self):
            return self.terminated

        def get_server_pid(self):
            return 102

        @asynccontextmanager
        async def acquire(self):
            yield Connection()

        def terminate(self):
            self.terminated = True

    coor = PostgresCoordinator("not-used", "deployment", "workspace", {})
    coor._pool = Pool()
    coor._witness_connection = Pool()

    async def validated(*args, **kwargs):
        pass

    monkeypatch.setattr(coor, "_validate", validated)
    monkeypatch.setattr(binding, "AsyncOpenAI", ColdClient)

    def watchdog():
        if entered.wait(timeout=2):
            loop.call_soon_threadsafe(loop.call_later, 0.01, release.set)

    guard = threading.Thread(target=watchdog)
    guard.start()
    client = httpx.AsyncClient(
        transport=httpx.MockTransport(lambda r: response(r, [])), trust_env=False
    )
    heartbeat = asyncio.create_task(coor.heartbeat(Operation(uuid4(), 1, uuid4())))
    try:
        await waiting.wait()
        results = await asyncio.gather(
            heartbeat, invoke(kind, client), return_exceptions=True
        )
        assert not [x for x in results if isinstance(x, BaseException)]
        assert not coor._broken and not coor._pool.terminated
        await coor.heartbeat(Operation(uuid4(), 1, uuid4()))
    finally:
        release.set()
        await asyncio.to_thread(guard.join, 2)
        await asyncio.gather(heartbeat, return_exceptions=True)
        await client.aclose()
        await coor.close()
