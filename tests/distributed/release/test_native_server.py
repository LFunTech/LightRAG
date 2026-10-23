import asyncio
from types import SimpleNamespace
import socket

import httpx
from fastapi import FastAPI
from starlette.responses import StreamingResponse


async def test_real_native_shutdown_waits_for_ongoing_stream_without_cancelling():
    from lightrag.api.release_lifecycle import create_release_uvicorn

    app = FastAPI()
    release = SimpleNamespace(shutdown_hook=None)
    app.state.rag = SimpleNamespace(
        _distributed_runtime=SimpleNamespace(release=release)
    )
    finish = asyncio.Event()
    cancelled = []

    @app.get("/stream")
    async def stream():
        async def content():
            try:
                yield b"first\n"
                await finish.wait()
                yield b"last\n"
            except asyncio.CancelledError:
                cancelled.append(True)
                raise

        return StreamingResponse(content())

    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    sock.listen(10)
    port = sock.getsockname()[1]
    server = create_release_uvicorn(
        app, {"app": app, "host": "127.0.0.1", "port": port, "log_level": "error"}
    )
    assert server.config.timeout_graceful_shutdown is None
    task = asyncio.create_task(server.serve(sockets=[sock]))
    try:
        async with httpx.AsyncClient(timeout=3) as client:
            async with client.stream(
                "GET", f"http://127.0.0.1:{port}/stream"
            ) as response:
                iterator = response.aiter_lines()
                assert await anext(iterator) == "first"
                release.shutdown_hook()
                await asyncio.sleep(0.15)
                assert not task.done()
                assert not cancelled
                finish.set()
                assert await anext(iterator) == "last"
        await asyncio.wait_for(task, 3)
        assert not cancelled
    finally:
        finish.set()
        server.should_exit = True
        await task
        sock.close()
