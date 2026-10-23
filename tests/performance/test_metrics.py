import asyncio
import math
from types import SimpleNamespace

import pytest


@pytest.mark.parametrize(
    "stage,threshold,next_bound",
    [("vector", 0.2, 0.25), ("first_response", 3.0, 5.0), ("query_total", 20.0, 30.0)],
)
def test_slo_histogram_boundaries_include_equality_without_rounding_down(
    stage, threshold, next_bound
):
    from lightrag.performance import QueryMetrics

    metrics = QueryMetrics(enabled=True)
    for value in (
        math.nextafter(threshold, -math.inf),
        threshold,
        math.nextafter(threshold, math.inf),
    ):
        metrics.observe(stage, value, kind="full", mode="mix")

    row = metrics.snapshot()["durations"][0]
    counts = {bucket["le"]: bucket["count"] for bucket in row["buckets"]}
    assert row["count"] == 3
    assert counts.get(threshold) == 2
    assert counts[next_bound] == counts[None] == 3


def test_slo_histogram_samples_do_not_enable_default_disabled_metrics():
    from lightrag.performance import QueryMetrics

    metrics = QueryMetrics()
    for stage, seconds in [
        ("vector", 0.2),
        ("first_response", 3.0),
        ("query_total", 20.0),
    ]:
        metrics.observe(stage, seconds, kind="full", mode="mix")
    snapshot = metrics.snapshot()
    assert snapshot["enabled"] is False
    assert snapshot["durations"] == []


def test_histograms_are_fixed_bounded_and_do_not_accept_payload_labels():
    from lightrag.performance import QueryMetrics

    metrics = QueryMetrics(enabled=True)
    for _ in range(100):
        metrics.observe("vector", 0.02, kind="full", mode="mix")
        metrics.observe("secret:query text", 1, kind="user-123", mode="private")
    result = metrics.snapshot()
    assert len(result["durations"]) == 1
    row = result["durations"][0]
    assert row["count"] == 100 and row["stage"] == "vector"
    assert row["buckets"][-1]["count"] == 100
    assert "secret" not in str(result) and "user-123" not in str(result)


async def test_query_stream_metrics_end_only_after_close_without_context_leak():
    from lightrag.performance import QueryMetrics, measured_query, current_trace

    metrics = QueryMetrics(enabled=True)
    events = []

    class Rag:
        query_metrics = metrics

        @measured_query
        async def aquery_llm(self, query, param):
            async def content():
                assert current_trace() is not None
                try:
                    yield "one"
                    yield "two"
                finally:
                    events.append("closed")

            return {"llm_response": {"response_iterator": content()}}

    result = await Rag().aquery_llm(
        "sensitive", SimpleNamespace(mode="mix", stream=True, conversation_history=[])
    )
    assert current_trace() is None
    assert metrics.snapshot()["active_queries"] == 1
    stream = result["llm_response"]["response_iterator"]
    assert await anext(stream) == "one"
    assert metrics.snapshot()["active_queries"] == 1
    await stream.aclose()
    assert metrics.snapshot()["active_queries"] == 0 and events == ["closed"]
    assert "sensitive" not in str(metrics.snapshot())


async def test_nested_query_and_failures_are_counted_once_and_never_swallowed():
    from lightrag.performance import QueryMetrics, measured_query

    metrics = QueryMetrics(enabled=True)

    class Rag:
        query_metrics = metrics

        @measured_query
        async def aquery(self, query, param=None):
            return await self.aquery_llm(query, param)

        @measured_query
        async def aquery_llm(self, query, param=None):
            raise OSError("private backend error")

    with pytest.raises(OSError, match="private backend error"):
        await Rag().aquery("secret")
    snapshot = metrics.snapshot()
    assert snapshot["active_queries"] == 0
    assert (
        sum(
            row["count"]
            for row in snapshot["durations"]
            if row["stage"] == "query_total"
        )
        == 1
    )
    assert "private" not in str(snapshot)


async def test_model_queue_context_and_usage_are_attributed_to_enqueuing_query():
    from lightrag.performance import (
        QueryMetrics,
        measured_query,
        queued_model,
        provider_call,
    )
    from lightrag.utils import priority_limit_async_func_call

    metrics = QueryMetrics(enabled=True)

    async def raw():
        async def call():
            await asyncio.sleep(0.005)
            return SimpleNamespace(
                usage=SimpleNamespace(prompt_tokens=3, completion_tokens=2, total_tokens=5)
            )

        return await provider_call(call, kind="llm")

    provider = queued_model(
        raw,
        role="query",
        limit_factory=lambda fn: priority_limit_async_func_call(1)(fn),
    )

    class Rag:
        query_metrics = metrics

        @measured_query
        async def aquery_llm(self, query, param=None):
            return await provider()

    try:
        await asyncio.gather(*(Rag().aquery_llm("not-a-label") for _ in range(3)))
        snapshot = metrics.snapshot()
        assert (
            sum(
                v["count"]
                for v in snapshot["counters"]
                if v["name"] == "provider_calls"
            )
            == 3
        )
        assert (
            sum(v["count"] for v in snapshot["counters"] if v["name"] == "input_tokens")
            == 9
        )
        assert any(v["stage"] == "queue_query" for v in snapshot["durations"])
    finally:
        await provider.shutdown()


async def test_observation_failure_never_masks_success_or_business_error(monkeypatch):
    from lightrag.performance import QueryMetrics, measured_query, count

    metrics = QueryMetrics(enabled=True)

    def broken(*args, **kwargs):
        raise RuntimeError("collector failure")

    monkeypatch.setattr(metrics, "observe", broken)
    monkeypatch.setattr(metrics, "increment", broken)

    class Rag:
        query_metrics = metrics

        @measured_query
        async def aquery(self, query, param=None):
            count("provider_calls")
            if query == "fail":
                raise OSError("business error")
            return "answer"

    assert await Rag().aquery("success") == "answer"
    with pytest.raises(OSError, match="business error"):
        await Rag().aquery("fail")


async def test_event_loop_sampler_is_bounded_and_stops_without_business_cancellation():
    from lightrag.performance import QueryMetrics

    metrics = QueryMetrics(enabled=True)
    task = asyncio.create_task(metrics.sample_loop(interval=0.001))
    try:
        await asyncio.sleep(0.015)
        snapshot = metrics.snapshot()
        assert any(r["stage"] == "event_loop_lag" for r in snapshot["durations"])
        assert snapshot["resources"]["cpu_seconds"] >= 0
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


async def test_context_manager_instrumentation_preserves_exception_and_authority():
    from contextlib import asynccontextmanager
    from lightrag.performance import QueryMetrics, measured_query, measured_context

    metrics = QueryMetrics(enabled=True)
    events = []

    @measured_context("coordination")
    @asynccontextmanager
    async def transaction():
        events.append("enter")
        try:
            yield
        finally:
            events.append("exit")

    class Rag:
        query_metrics = metrics

        @measured_query
        async def aquery(self, *args):
            async with transaction():
                raise OSError("original")

    with pytest.raises(OSError, match="original"):
        await Rag().aquery("synthetic")
    assert events == ["enter", "exit"]
    assert any(
        r["stage"] == "coordination" and r["outcome"] == "error"
        for r in metrics.snapshot()["durations"]
    )


async def test_asgi_metrics_preserve_streaming_and_do_not_publish_request_data():
    from lightrag.performance import QueryMetrics, QueryMetricsMiddleware

    metrics = QueryMetrics(enabled=True)
    first = asyncio.Event()
    finish = asyncio.Event()
    messages = []

    async def app(scope, receive, send):
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"first", "more_body": True})
        first.set()
        await finish.wait()
        await send({"type": "http.response.body", "body": b"last", "more_body": False})

    async def receive():
        return {"type": "http.request", "body": b""}

    async def send(message):
        messages.append(message)

    wrapped = QueryMetricsMiddleware(app, metrics=metrics)
    task = asyncio.create_task(
        wrapped(
            {
                "type": "http",
                "path": "/query/stream",
                "root_path": "",
                "query_string": b"secret=private",
            },
            receive,
            send,
        )
    )
    await first.wait()
    assert len(messages) == 2 and not any(
        r["stage"] == "http_total" for r in metrics.snapshot()["durations"]
    )
    finish.set()
    await task
    assert (
        sum(
            r["count"]
            for r in metrics.snapshot()["durations"]
            if r["stage"] == "http_total"
        )
        == 1
    )
    assert "private" not in str(metrics.snapshot())


async def test_nested_different_rag_instances_keep_separate_registries():
    from lightrag.performance import QueryMetrics, measured_query

    class Rag:
        def __init__(self):
            self.query_metrics = QueryMetrics(enabled=True)

        @measured_query
        async def aquery(self, query, param=None):
            return await inner.aquery("inner") if self is outer else "answer"

    outer, inner = Rag(), Rag()
    assert await outer.aquery("outer") == "answer"
    for rag in (outer, inner):
        assert (
            sum(
                r["count"]
                for r in rag.query_metrics.snapshot()["durations"]
                if r["stage"] == "query_total"
            )
            == 1
        )


def test_resource_metrics_are_optional_when_os_does_not_support_them(monkeypatch):
    import sys
    from lightrag.performance import QueryMetrics

    monkeypatch.setitem(sys.modules, "resource", None)
    result = QueryMetrics(enabled=True).snapshot()
    assert result["resources"]["available"] is False


async def test_cancelled_stream_remains_active_until_owner_closes():
    from lightrag.performance import QueryMetrics, measured_query

    metrics = QueryMetrics(enabled=True)
    entered, done = asyncio.Event(), asyncio.Event()

    class Rag:
        query_metrics = metrics

        @measured_query
        async def aquery(self, *args):
            async def content():
                entered.set()
                try:
                    await asyncio.Event().wait()
                    yield "unused"
                finally:
                    done.set()

            return content()

    stream = await Rag().aquery("synthetic")
    task = asyncio.create_task(anext(stream))
    await entered.wait()
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)
    assert done.is_set() and metrics.snapshot()["active_queries"] == 1
    await stream.aclose()
    assert metrics.snapshot()["active_queries"] == 0


async def test_unconsumed_stream_is_closed_by_existing_request_cleanup():
    from lightrag.performance import QueryMetrics, measured_query
    from lightrag.distributed.release.query import _streams, close_request_streams

    metrics = QueryMetrics(enabled=True)

    class Source:
        closed = False

        def __aiter__(self):
            return self

        async def __anext__(self):
            return "synthetic"

        async def aclose(self):
            self.closed = True

    source = Source()

    class Rag:
        query_metrics = metrics

        @measured_query
        async def aquery(self, *args):
            return source

    streams = set()
    token = _streams.set(streams)
    try:
        await Rag().aquery("synthetic")
    finally:
        _streams.reset(token)
    await close_request_streams(streams)
    assert source.closed and metrics.snapshot()["active_queries"] == 0


async def test_default_query_param_is_classified_from_real_signature():
    from lightrag.base import QueryParam
    from lightrag.performance import QueryMetrics, measured_query

    metrics = QueryMetrics(enabled=True)

    class Rag:
        query_metrics = metrics

        @measured_query
        async def aquery(self, query, param=QueryParam(mode="naive", stream=True)):
            return "answer"

    await Rag().aquery("synthetic")
    row = next(
        r for r in metrics.snapshot()["durations"] if r["stage"] == "query_total"
    )
    assert (row["kind"], row["mode"]) == ("stream", "naive")


async def test_background_role_calls_are_counted_without_faking_query_count():
    from lightrag.performance import QueryMetrics, queued_model, provider_call
    from lightrag.utils import priority_limit_async_func_call

    metrics = QueryMetrics(enabled=True)

    async def raw():
        return await provider_call(
            lambda: asyncio.sleep(0, result=SimpleNamespace(usage=None)), kind="llm"
        )

    function = queued_model(
        raw,
        role="extract",
        limit_factory=lambda fn: priority_limit_async_func_call(1)(fn),
        metrics=metrics,
    )
    try:
        await function()
        result = metrics.snapshot()
        assert result["active_queries"] == 0
        assert not any(r["stage"] == "query_total" for r in result["durations"])
        assert any(
            r["name"] == "provider_calls"
            and r["category"] == "extract"
            and r["kind"] == "process"
            for r in result["counters"]
        )
        assert any(r["stage"] == "queue_extract" for r in result["durations"])
    finally:
        await function.shutdown()


async def test_failed_iteration_does_not_hide_explicit_source_cleanup():
    from lightrag.performance import QueryMetrics, measured_query

    metrics = QueryMetrics(enabled=True)

    class Source:
        closed = False

        def __aiter__(self):
            return self

        async def __anext__(self):
            raise OSError("original read failure")

        async def aclose(self):
            self.closed = True

    source = Source()

    class Rag:
        query_metrics = metrics

        @measured_query
        async def aquery(self, *args):
            return source

    stream = await Rag().aquery("synthetic")
    with pytest.raises(OSError, match="original read failure"):
        await anext(stream)
    assert metrics.snapshot()["active_queries"] == 1
    await stream.aclose()
    assert source.closed and metrics.snapshot()["active_queries"] == 0


async def test_exhausted_stream_still_forwards_explicit_close():
    from lightrag.performance import QueryMetrics, measured_query

    metrics = QueryMetrics(enabled=True)

    class Source:
        closed = False

        def __aiter__(self):
            return self

        async def __anext__(self):
            raise StopAsyncIteration

        async def aclose(self):
            self.closed = True

    source = Source()

    class Rag:
        query_metrics = metrics

        @measured_query
        async def aquery(self, *args):
            return source

    stream = await Rag().aquery("synthetic")
    with pytest.raises(StopAsyncIteration):
        await anext(stream)
    await stream.aclose()
    assert source.closed


async def test_release_provider_unknown_close_cannot_be_hidden_by_observation():
    from lightrag.performance import QueryMetrics, QueryTrace, ObservedStream
    from lightrag.distributed.release.query import (
        OwnedProviderStream,
        ProviderStreamLease,
    )

    async def content():
        yield "synthetic"
        raise OSError("read failed")

    async def close():
        raise OSError("close unknown")

    metrics = QueryMetrics(enabled=True)
    observed = ObservedStream(
        OwnedProviderStream(content(), close),
        QueryTrace(metrics, "stream", "mix"),
        request=True,
    )
    lease = ProviderStreamLease(observed)
    assert await anext(lease) == "synthetic"
    with pytest.raises(OSError):
        await anext(lease)
    assert not lease.closed.is_set() and metrics.snapshot()["active_queries"] == 1
    with pytest.raises(OSError, match="close unknown"):
        await lease.aclose()
    assert not lease.closed.is_set()


async def test_cancel_before_response_follows_real_release_owner_not_waiter(
    monkeypatch,
):
    from contextlib import asynccontextmanager
    from lightrag import LightRAG
    from lightrag.distributed.release.query import run_query
    from lightrag.performance import QueryMetrics, provider_call

    started, gate, closed = asyncio.Event(), asyncio.Event(), asyncio.Event()

    @asynccontextmanager
    async def permit():
        try:
            yield
        finally:
            closed.set()

    async def query(function, rag, args, kwargs, **options):
        return await run_query(lambda: function(rag, *args, **kwargs), permit)

    runtime = SimpleNamespace(release=SimpleNamespace(query=query))
    monkeypatch.setattr("lightrag.distributed.runtime.get_runtime", lambda obj: runtime)
    rag = LightRAG.__new__(LightRAG)
    rag.query_metrics = QueryMetrics(enabled=True)

    async def response(*args, **kwargs):
        async def send():
            started.set()
            await gate.wait()
            return SimpleNamespace(
                usage=SimpleNamespace(prompt_tokens=5, completion_tokens=2, total_tokens=7)
            )

        await provider_call(send, kind="llm")
        return {"llm_response": {"content": "synthetic"}}

    rag.aquery_llm = response
    task = asyncio.create_task(rag.aquery("synthetic"))
    await started.wait()
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)
    try:
        assert (
            not closed.is_set() and rag.query_metrics.snapshot()["active_queries"] == 1
        )
    finally:
        gate.set()
        await closed.wait()
        await asyncio.sleep(0)
    counters = rag.query_metrics.snapshot()["counters"]
    assert sum(r["count"] for r in counters if r["name"] == "input_tokens") == 5
    assert rag.query_metrics.snapshot()["active_queries"] == 0
