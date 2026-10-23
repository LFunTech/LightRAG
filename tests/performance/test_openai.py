from types import SimpleNamespace

import pytest


@pytest.mark.parametrize("early_close", [False, True])
async def test_real_openai_stream_path_records_terminal_usage_and_keeps_close(
    monkeypatch,
    early_close,
):
    from lightrag.llm import openai as module
    from lightrag.performance import QueryMetrics, measured_query, queued_model
    from lightrag.utils import priority_limit_async_func_call
    from tests.llm.openai_impl.test_openai_stream_generatorexit_cot_cleanup import (
        _FakeOpenAIStream,
        _make_chunk,
        _make_stream_client,
    )

    terminal = _make_chunk()
    terminal.choices = []
    terminal.usage = SimpleNamespace(prompt_tokens=11, completion_tokens=5, total_tokens=16)
    content = _make_chunk(content="private response")
    if early_close:
        content.usage = terminal.usage
    upstream = _FakeOpenAIStream([content, terminal])
    client = _make_stream_client(upstream)
    monkeypatch.setattr(module, "create_openai_async_client", lambda **kwargs: client)
    metrics = QueryMetrics(enabled=True)

    async def raw():
        return await module.openai_complete_if_cache(
            "synthetic", "private input", stream=True, api_key="private-key"
        )

    function = queued_model(
        raw,
        role="query",
        limit_factory=lambda fn: priority_limit_async_func_call(1)(fn),
        metrics=metrics,
    )

    class Rag:
        query_metrics = metrics

        @measured_query
        async def aquery(self, *args):
            return await function()

    try:
        stream = await Rag().aquery("synthetic")
        assert metrics.snapshot()["active_queries"] == 1
        if early_close:
            assert await anext(stream) == "private response"
            await stream.aclose()
        else:
            assert "".join([chunk async for chunk in stream]) == "private response"
        result = metrics.snapshot()
        counts = {
            r["name"]: r["count"]
            for r in result["counters"]
            if r["category"] == "query"
        }
        assert counts["provider_calls"] == 1
        assert counts.get("usage_reports", 0) == int(not early_close)
        assert counts.get("input_tokens", 0) == (0 if early_close else 11)
        assert counts.get("output_tokens", 0) == (0 if early_close else 5)
        assert counts["usage_unknown"] == int(early_close)
        assert result["active_queries"] == 0
        assert any(r["stage"] == "provider_first_token" for r in result["durations"])
        assert "private" not in str(result)
        upstream.aclose.assert_awaited()
        client.close.assert_awaited()
    finally:
        await function.shutdown()


@pytest.mark.parametrize("kind", ["llm", "embedding"])
async def test_real_accounting_rejection_before_dispatch_does_not_count_sdk_call(
    monkeypatch, kind
):
    from unittest.mock import AsyncMock
    from lightrag.llm import openai as module
    from lightrag.performance import QueryMetrics, measured_query
    from lightrag.query_accounting import (
        query_usage_scope,
        retrieval_digest,
        QueryAccountingError,
    )
    from tests.llm.test_query_accounting import Authority

    send = AsyncMock(side_effect=AssertionError("SDK must not be invoked"))
    client = SimpleNamespace(
        max_retries=0,
        base_url="https://model.invalid/v1/",
        close=AsyncMock(),
        chat=SimpleNamespace(completions=SimpleNamespace(create=send)),
        embeddings=SimpleNamespace(create=send),
    )

    async def enter():
        return client

    async def exit(*args):
        pass

    # The embedding binding uses an async context manager.
    class Client:
        async def __aenter__(self):
            return client

        async def __aexit__(self, *args):
            pass

        def __getattr__(self, name):
            return getattr(client, name)

    monkeypatch.setattr(module, "create_openai_async_client", lambda **kwargs: Client())
    metrics = QueryMetrics(enabled=True)

    class Rag:
        query_metrics = metrics

        @measured_query
        async def aquery(self, *args):
            async with query_usage_scope(
                Authority(),
                query_id="synthetic",
                grant="synthetic-grant",
                request_digest=retrieval_digest("synthetic", "mix"),
                workspace="isolated-index",
            ):
                if kind == "llm":
                    return await module.openai_complete_if_cache.__wrapped__(
                        "unquoted-model", "synthetic", api_key="synthetic"
                    )
                return await module.openai_embed.func(
                    ["synthetic"],
                    model="unquoted-model",
                    api_key="synthetic",
                    max_token_size=0,
                )

    with pytest.raises(QueryAccountingError, match="not_quoted"):
        await Rag().aquery("synthetic")
    assert send.await_count == 0
    assert not any(
        r["name"] == "provider_calls" for r in metrics.snapshot()["counters"]
    )
