import asyncio
import threading
from types import SimpleNamespace
from unittest.mock import AsyncMock

import numpy as np
import pytest

from lightrag.llm import openai as binding


async def test_cold_tokenizer_and_encoding_do_not_block_loop_and_preserve_prefix(
    monkeypatch,
):
    entered, release = threading.Event(), threading.Event()
    loop = asyncio.get_running_loop()
    encoded, progressed = [], []

    class Encoding:
        def encode(self, text, **kwargs):
            encoded.append((text, kwargs))
            return list(text)

        def decode(self, tokens):
            return "".join(tokens)

    def cold_encoding(model):
        entered.set()
        progressed.append(release.wait(0.3))
        return Encoding()

    def watchdog():
        if entered.wait(1):
            loop.call_soon_threadsafe(release.set)

    class Client:
        embeddings = SimpleNamespace(
            create=AsyncMock(
                return_value=SimpleNamespace(
                    data=[SimpleNamespace(embedding=[0.1, 0.2])] * 2
                )
            )
        )

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            pass

    client = Client()
    monkeypatch.setattr(binding, "_get_tiktoken_encoding_for_model", cold_encoding)
    monkeypatch.setattr(binding, "create_openai_async_client", lambda **kwargs: client)
    thread = threading.Thread(target=watchdog)
    thread.start()
    try:
        result = await binding.openai_embed.func(
            ["abcdef", "abcdef"],
            model="synthetic",
            api_key="synthetic",
            context="query",
            query_prefix="Q:",
            max_token_size=5,
        )
    finally:
        release.set()
        thread.join(1)
    assert progressed == [True]
    assert encoded == [("Q:abcdef", {"disallowed_special": ()})]
    assert client.embeddings.create.call_args.kwargs["input"] == ["Q:abc", "Q:abc"]
    assert np.array_equal(result, np.array([[0.1, 0.2], [0.1, 0.2]], dtype=np.float32))


async def test_cancel_input_prepare_joins_thread_without_dispatch(monkeypatch):
    entered, release = threading.Event(), threading.Event()

    class Encoding:
        def encode(self, text, **kwargs):
            entered.set()
            release.wait(1)
            return [1]

    monkeypatch.setattr(
        binding, "_get_tiktoken_encoding_for_model", lambda m: Encoding()
    )
    dispatch = AsyncMock(side_effect=AssertionError("no dispatch after cancellation"))
    monkeypatch.setattr(binding, "_prepare_openai_async_client", dispatch)
    task = asyncio.create_task(
        binding.openai_embed.func(
            ["text"], model="synthetic", api_key="synthetic", max_token_size=5
        )
    )
    try:
        while not entered.is_set():
            await asyncio.sleep(0.001)
        task.cancel()
        await asyncio.sleep(0.01)
        assert not task.done()
        task.cancel()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        dispatch.assert_not_called()
    finally:
        release.set()
