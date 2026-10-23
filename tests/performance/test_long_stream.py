import asyncio
import httpx


async def test_long_stream_is_consumed_live_across_actual_transfer_marker():
    from scripts.performance.http import BoundedClient
    from scripts.performance.stream import observe_long_stream

    first, transfer = asyncio.Event(), asyncio.Event()

    class Body(httpx.AsyncByteStream):
        async def __aiter__(self):
            yield b'{"response":"first"}\n'
            await transfer.wait()
            yield b'{"response":"last"}\n'

    async def service(request):
        return httpx.Response(
            200, headers={"content-type": "application/x-ndjson"}, stream=Body()
        )

    async with BoundedClient(
        "https://fixture.invalid",
        {"reader": "private"},
        timeout=1,
        max_bytes=1000,
        max_calls=10,
        transport=httpx.MockTransport(service),
    ) as client:
        task = asyncio.create_task(
            observe_long_stream(
                client,
                dict(query="synthetic", mode="mix", history=[], actor="reader"),
                first,
                transfer,
            )
        )
        await asyncio.wait_for(first.wait(), 1)
        assert not task.done()
        transfer.set()
        report = await task
    assert (
        report["completed_after_transfer"]
        and report["response_events"] == 2
        and "first" not in str(report)
    )
