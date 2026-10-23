import asyncio
from contextlib import asynccontextmanager
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from starlette.responses import StreamingResponse


def test_required_accounting_authority_refuses_release_api_activation():
    from lightrag.api.release_commands import install_release_api
    from lightrag.distributed.release.models import ReleaseError

    with pytest.raises(ReleaseError, match="accounting authority"):
        install_release_api(
            FastAPI(),
            None,
            api_key="synthetic-private",
            auth_handler=None,
            usage_authority=object(),
        )


async def test_real_asgi_disconnect_drains_inflight_stream_before_releasing_slot():
    from lightrag.api.release_lifecycle import ReleaseMiddleware
    from lightrag.distributed.release.lifecycle import InstanceLifecycle
    from lightrag.distributed.release.query import run_query, provider_stream_context
    from lightrag.utils import priority_limit_async_func_call

    lifecycle = InstanceLifecycle()
    reading, finish, disconnected = asyncio.Event(), asyncio.Event(), asyncio.Event()
    events = []

    @priority_limit_async_func_call(1, queue_name="release-asgi-disconnect")
    async def provider():
        events.append("opened")

        async def content():
            try:
                yield b"first\n"
                reading.set()
                await finish.wait()
                yield b"last\n"
            finally:
                events.append("closed")

        return content()

    @asynccontextmanager
    async def operation(kind="release_read"):
        assert kind == "release_read"
        async with lifecycle.track("queries"):
            yield

    app = FastAPI()

    @app.post("/query/stream", name="query_text_stream")
    async def stream():
        return StreamingResponse(await run_query(provider, operation))

    release = SimpleNamespace(
        lifecycle=lifecycle, runtime=SimpleNamespace(operation=operation)
    )
    bridge = SimpleNamespace(
        release=release,
        replaying=lambda: False,
        match=lambda path, method: SimpleNamespace(name="query_text_stream"),
    )
    app.add_middleware(ReleaseMiddleware, bridge=bridge)
    messages = []

    async def receive():
        await reading.wait()
        disconnected.set()
        return {"type": "http.disconnect"}

    async def send(message):
        messages.append(message)

    scope = {
        "type": "http",
        "asgi": {"version": "3.0", "spec_version": "2.3"},
        "http_version": "1.1",
        "method": "POST",
        "scheme": "http",
        "path": "/query/stream",
        "raw_path": b"/query/stream",
        "query_string": b"",
        "root_path": "",
        "headers": [],
        "server": ("fixture", 80),
        "client": ("fixture", 1000),
    }
    request = asyncio.create_task(app(scope, receive, send))
    next_call = None
    try:
        await asyncio.wait_for(disconnected.wait(), 1)
        with provider_stream_context():
            next_call = asyncio.create_task(provider())
        await asyncio.sleep(0.03)
        assert not request.done() and not next_call.done()
        assert events == ["opened"]
        assert lifecycle.snapshot()["http"] == 1
        assert lifecycle.snapshot()["queries"] >= 1
        finish.set()
        await asyncio.wait_for(request, 1)
        assert not any(lifecycle.snapshot().values())
        second = await asyncio.wait_for(next_call, 1)
        await second.aclose()
        assert events[:2] == ["opened", "closed"]
        assert any(message.get("body") == b"first\n" for message in messages)
    finally:
        finish.set()
        if not request.done():
            await asyncio.wait_for(request, 1)
        if next_call is not None and not next_call.done():
            next_call.cancel()
        await provider.shutdown()
