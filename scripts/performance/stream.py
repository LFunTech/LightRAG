"""One bounded live stream spanning transfer; never defer consumption to fake overlap."""

import asyncio
import json


async def observe_long_stream(client, case, first_event, transferred):
    client.ensure_running()
    if case["actor"] not in client.actors or client.calls >= client.max_calls:
        raise ValueError("Stream actor/call budget refused")
    client.calls += 1

    async def consume():
        payload = dict(
            query=case["query"],
            mode=case["mode"],
            stream=True,
            conversation_history=case["history"],
        )
        async with client.client.stream(
            "POST",
            "/query/stream",
            headers={
                "X-API-Key": client.actors[case["actor"]],
                "X-Performance-Rehearsal": "long-stream",
            },
            json=payload,
        ) as response:
            if (
                response.status_code != 200
                or response.headers.get("content-type", "")
                .split(";", 1)[0]
                .strip()
                .lower()
                != "application/x-ndjson"
            ):
                raise ValueError("Long stream unavailable")
            size = 0
            buffer = bytearray()
            events = 0

            def accept(line):
                nonlocal events
                value = json.loads(line)
                if (
                    not isinstance(value, dict)
                    or value.get("error") is not None
                    or ("llm_generated" in value and value["llm_generated"] is not True)
                ):
                    raise ValueError("Long stream generation failed")
                if "response" in value:
                    if not isinstance(value["response"], str):
                        raise ValueError("Invalid long stream content")
                    if value["response"]:
                        events += 1
                        first_event.set()

            async for chunk in response.aiter_bytes():
                size += len(chunk)
                if size > client.max_bytes:
                    raise ValueError("Long stream size bound exceeded")
                buffer.extend(chunk)
                while b"\n" in buffer:
                    line, _, tail = buffer.partition(b"\n")
                    buffer[:] = tail
                    if line.strip():
                        accept(line)
            if buffer.strip():
                accept(buffer)
            if not events:
                raise ValueError("Empty long stream")
            return dict(
                completed_after_transfer=transferred.is_set(),
                response_events=events,
                bytes=size,
            )

    try:
        return await asyncio.wait_for(consume(), timeout=client.timeout)
    finally:
        first_event.set()  # Wake the coordinator on failure as well as first token.
