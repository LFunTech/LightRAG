import asyncio
import json
from types import SimpleNamespace

import httpx
import pytest

from lightrag.performance import QueryMetrics, measured_query, queued_model


def counts(metrics):
    return {r["name"]: r["count"] for r in metrics.snapshot()["counters"]}


@pytest.mark.parametrize("options", [None, {}, {"include_usage": False}])
async def test_explicit_stream_usage_policy_and_unsupported_provider_never_replay(
    monkeypatch, options
):
    from lightrag.llm import openai as binding
    from openai import AsyncOpenAI, BadRequestError

    requests = []

    def service(request):
        requests.append(json.loads(request.content))
        return httpx.Response(
            400,
            json={
                "error": {
                    "message": "unsupported stream_options",
                    "type": "invalid_request_error",
                }
            },
        )

    def client(**kwargs):
        return AsyncOpenAI(
            api_key="synthetic",
            max_retries=0,
            http_client=httpx.AsyncClient(transport=httpx.MockTransport(service)),
        )

    monkeypatch.setattr(binding, "create_openai_async_client", client)
    kwargs = {} if options is None else {"stream_options": options}
    with pytest.raises(BadRequestError):
        await binding.openai_complete_if_cache(
            "synthetic", "synthetic", stream=True, **kwargs
        )
    assert len(requests) == 1
    assert requests[0]["stream_options"]["include_usage"] is (
        options != {"include_usage": False}
    )


@pytest.mark.parametrize(
    "end",
    [
        "usage",
        "missing",
        "partial",
        "invalid",
        "zero",
        "close",
        "unused",
        "intermediate",
        "duplicate",
        "conflict",
    ],
)
async def test_real_sdk_stream_usage_is_requested_and_settled_once(monkeypatch, end):
    from lightrag.llm import openai as binding
    from openai import AsyncOpenAI

    requests = []

    def service(request):
        requests.append(json.loads(request.content))
        chunks = [
            dict(
                id="synthetic",
                object="chat.completion.chunk",
                created=1,
                model="synthetic",
                choices=[dict(index=0, delta=dict(content="ok"))],
            )
        ]
        usage = dict(prompt_tokens=3, completion_tokens=2, total_tokens=5)
        if end == "partial":
            usage.pop("completion_tokens")
        if end == "invalid":
            usage["total_tokens"] = 6
        if end == "zero":
            usage = dict(prompt_tokens=0, completion_tokens=0, total_tokens=0)
        if end == "intermediate":
            chunks[0]["usage"] = usage
        if end not in {"missing", "close", "unused", "intermediate"}:
            chunks.append(
                dict(
                    id="synthetic",
                    object="chat.completion.chunk",
                    created=1,
                    model="synthetic",
                    choices=[],
                    usage=usage,
                )
            )
        if end in {"duplicate", "conflict"}:
            other = dict(usage)
            if end == "conflict":
                other.update(completion_tokens=3, total_tokens=6)
            chunks.append({**chunks[-1], "usage": other})
        content = "".join("data: " + json.dumps(c) + "\n\n" for c in chunks)
        return httpx.Response(
            200,
            content=content + "data: [DONE]\n\n",
            headers={"content-type": "text/event-stream"},
        )

    clients = []
    tracked = []

    def client(**kwargs):
        obj = AsyncOpenAI(
            api_key="synthetic",
            base_url="https://synthetic.invalid/v1",
            max_retries=0,
            http_client=httpx.AsyncClient(transport=httpx.MockTransport(service)),
        )
        clients.append(obj)
        return obj

    monkeypatch.setattr(binding, "create_openai_async_client", client)
    metrics = QueryMetrics(enabled=True)

    async def raw():
        return await binding.openai_complete_if_cache(
            "synthetic",
            "synthetic",
            stream=True,
            token_tracker=SimpleNamespace(add_usage=tracked.append),
        )

    model = queued_model(raw, role="query", limit_factory=lambda f: f)

    class Rag:
        query_metrics = metrics

        @measured_query
        async def aquery(self, query):
            return await model()

    stream = await Rag().aquery("synthetic")
    assert requests[0]["stream_options"]["include_usage"] is True
    assert counts(metrics)["usage_pending"] == 1
    assert counts(metrics)["usage_unknown"] == 0
    if end == "close":
        assert await anext(stream) == "ok"
    elif end != "unused":
        assert "".join([s async for s in stream]) == "ok"
    await stream.aclose()
    await stream.aclose()
    values = counts(metrics)
    known = end in {"usage", "zero", "duplicate"}
    assert values["provider_calls"] == 1
    assert values["usage_pending"] == 0
    assert values["usage_unknown"] == (0 if known else 1)
    assert values.get("usage_reports", 0) == int(known)
    assert values.get("input_tokens", 0) == (3 if end in {"usage", "duplicate"} else 0)
    assert values.get("output_tokens", 0) == (2 if end in {"usage", "duplicate"} else 0)
    assert len(tracked) == int(known)
    assert all(c.is_closed() for c in clients)


@pytest.mark.parametrize(
    "usage,known",
    [
        (SimpleNamespace(prompt_tokens=4, total_tokens=4), True),
        (SimpleNamespace(prompt_tokens=4), False),
        (SimpleNamespace(prompt_tokens=True, total_tokens=1), False),
    ],
)
async def test_embedding_usage_requires_complete_integer_total(usage, known):
    from lightrag.performance import provider_call

    metrics = QueryMetrics(enabled=True)

    class Rag:
        query_metrics = metrics

        @measured_query
        async def aquery(self, query):
            async def send():
                await asyncio.sleep(0)
                assert counts(metrics)["usage_pending"] == 1
                return SimpleNamespace(usage=usage)

            return await provider_call(send, kind="embedding")

    await Rag().aquery("synthetic")
    assert counts(metrics)["usage_unknown"] == int(not known)
    assert counts(metrics)["usage_pending"] == 0
