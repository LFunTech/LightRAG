import asyncio
from contextlib import asynccontextmanager

import pytest


async def test_query_lifetime_spans_actual_stream_consumption_and_close():
    from lightrag.distributed.release.query import run_query

    events = []

    @asynccontextmanager
    async def permit():
        events.append("enter")
        try:
            yield
        finally:
            events.append("exit")

    async def chunks():
        yield "one"
        yield "two"

    async def call():
        return {"llm_response": {"response_iterator": chunks()}}

    result = await run_query(call, permit)
    assert events == ["enter"]
    stream = result["llm_response"]["response_iterator"]
    assert await anext(stream) == "one"
    assert events == ["enter"]
    await stream.aclose()
    assert events == ["enter", "exit"]
    with pytest.raises(StopAsyncIteration):
        await anext(stream)


async def test_stream_is_consumed_under_original_context_and_final_error_is_not_hidden():
    from contextvars import ContextVar
    from lightrag.distributed.release.query import run_query

    current = ContextVar("permit", default=False)

    @asynccontextmanager
    async def permit():
        token = current.set(True)
        try:
            yield
        finally:
            current.reset(token)

    async def chunks():
        assert current.get()
        yield "one"
        raise OSError("stream read failed")

    async def call():
        return chunks()

    stream = await run_query(call, permit)
    assert not current.get()
    assert await anext(stream) == "one"
    with pytest.raises(OSError):
        await anext(stream)


async def test_non_stream_result_does_not_escape_before_permit_exit():
    from lightrag.distributed.release.query import run_query

    events = []

    @asynccontextmanager
    async def permit():
        try:
            yield
        finally:
            events.append("closed")

    async def call():
        return {"answer": "ok"}

    assert await run_query(call, permit) == {"answer": "ok"}
    assert events == ["closed"]


async def test_provider_concurrency_slot_is_held_until_stream_exhausted():
    from lightrag.distributed.release.query import provider_stream_context
    from lightrag.utils import priority_limit_async_func_call

    active = maximum = 0

    @priority_limit_async_func_call(1, queue_name="release-stream-test")
    async def provider():
        async def stream():
            nonlocal active, maximum
            active += 1
            maximum = max(maximum, active)
            try:
                await asyncio.sleep(0.01)
                yield "one"
                await asyncio.sleep(0.01)
            finally:
                active -= 1

        return stream()

    async def request():
        with provider_stream_context():
            response = await provider()
            return [item async for item in response]

    try:
        assert (
            await asyncio.wait_for(asyncio.gather(*(request() for _ in range(4))), 2)
            == [["one"]] * 4
        )
        assert maximum == 1
        assert active == 0
    finally:
        await provider.shutdown()


async def test_cancelled_consumer_closes_after_inflight_terminal_without_leak():
    from lightrag.distributed.release.query import run_query

    events = []
    gate = asyncio.Event()
    started = asyncio.Event()

    @asynccontextmanager
    async def permit():
        try:
            yield
        finally:
            events.append("closed")

    async def chunks():
        started.set()
        await gate.wait()
        if False:
            yield "never"

    async def call():
        return chunks()

    stream = await run_query(call, permit)
    consumer = asyncio.create_task(anext(stream))
    await started.wait()
    consumer.cancel()
    with pytest.raises(asyncio.CancelledError):
        await consumer
    gate.set()
    await asyncio.sleep(0.01)
    await asyncio.wait_for(stream.aclose(), 0.2)
    assert events == ["closed"]


async def test_real_openai_unconsumed_stream_closes_already_open_response_and_client(
    monkeypatch,
):
    from types import SimpleNamespace
    from unittest.mock import AsyncMock
    from lightrag.llm import openai as provider
    from lightrag.utils import priority_limit_async_func_call
    from lightrag.distributed.release.query import run_query

    class Response:
        def __init__(self):
            self.aclose = AsyncMock()

        def __aiter__(self):
            return self

        async def __anext__(self):
            raise StopAsyncIteration

    response = Response()
    client = SimpleNamespace(
        chat=SimpleNamespace(
            completions=SimpleNamespace(create=AsyncMock(return_value=response))
        ),
        close=AsyncMock(),
    )
    monkeypatch.setattr(provider, "create_openai_async_client", lambda **kwargs: client)
    limited = priority_limit_async_func_call(1, queue_name="review-openai-unconsumed")(
        provider.openai_complete_if_cache.__wrapped__
    )
    events = []

    @asynccontextmanager
    async def permit():
        events.append("enter")
        try:
            yield
        finally:
            events.append("exit")

    try:
        stream = await run_query(
            lambda: limited(
                model="fixture", prompt="synthetic", stream=True, api_key="synthetic"
            ),
            permit,
        )
        await stream.aclose()
        assert response.aclose.await_count == 1
        assert client.close.await_count == 1
        assert events == ["enter", "exit"]
    finally:
        await limited.shutdown()


async def test_request_cleanup_waits_for_inflight_iteration_before_releasing_provider_slot():
    from lightrag.distributed.release.query import (
        run_query,
        _streams,
        close_request_streams,
    )
    from lightrag.utils import priority_limit_async_func_call

    entered, finish = asyncio.Event(), asyncio.Event()
    active = []

    @priority_limit_async_func_call(1, queue_name="review-disconnect")
    async def provider():
        active.append("opened")

        async def chunks():
            try:
                entered.set()
                await finish.wait()
                yield "one"
            finally:
                active.append("closed")

        return chunks()

    @asynccontextmanager
    async def permit():
        yield

    bag = set()
    token = _streams.set(bag)
    second = None
    try:
        stream = await run_query(provider, permit)
        consuming = asyncio.create_task(anext(stream))
        await entered.wait()
        consuming.cancel()
        await asyncio.gather(consuming, return_exceptions=True)
        cleanup = asyncio.create_task(close_request_streams(bag))
        from lightrag.distributed.release.query import provider_stream_context

        with provider_stream_context():
            second = asyncio.create_task(provider())
        await asyncio.sleep(0.02)
        assert not cleanup.done() and not second.done()
        assert active == ["opened"]
        finish.set()
        await asyncio.wait_for(cleanup, 1)
        next_stream = await asyncio.wait_for(second, 1)
        await next_stream.aclose()
        assert active[:2] == ["opened", "closed"]
    finally:
        finish.set()
        _streams.reset(token)
        if second and not second.done():
            second.cancel()
        await provider.shutdown()
