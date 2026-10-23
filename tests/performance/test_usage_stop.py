import asyncio
from types import SimpleNamespace

import httpx
import pytest

from lightrag.performance import QueryMetrics, QueryTrace
from tests.performance.test_load import plan


def snapshot():
    return QueryMetrics(enabled=True).snapshot()


def target():
    return dict(
        base_url="https://pod.invalid", instance_id="uid-one", config_hash="b" * 64
    )


def identity():
    return dict(instance_id="uid-one", config_hash="b" * 64, profile_hash="a" * 64)


@pytest.mark.parametrize(
    "fault",
    [
        "unknown",
        "replacement",
        "disabled",
        "missing",
        "reset",
        "observation",
        "bad_integer",
        "identity",
    ],
)
async def test_usage_watch_refuses_missing_or_uncertain_observations(fault):
    from scripts.performance.usage import UsageWatch

    metrics = QueryMetrics(enabled=True)
    current = metrics.snapshot()
    who = identity()

    def service(request):
        return httpx.Response(
            200,
            json=who
            if request.url.path == "/release/status"
            else {"query_performance": current},
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(service)) as client:
        watch = UsageWatch(client, [target()], profile_hash="a" * 64)
        await watch.check(initial=True)
        if fault == "unknown":
            metrics.increment(
                "provider_calls",
                trace=QueryTrace(metrics, "stream", "mix"),
                category="query",
            )
            current = metrics.snapshot()
        elif fault == "replacement":
            current["observation_epoch"] = "different"
        elif fault == "disabled":
            current["enabled"] = False
        elif fault == "missing":
            current.pop("measurement_version")
        elif fault == "reset":
            current["uptime_seconds"] = 0
        elif fault == "observation":
            current["observation_errors"] = 1
        elif fault == "bad_integer":
            current["counters"] = [
                dict(
                    name="usage_unknown",
                    kind="stream",
                    mode="mix",
                    category="query",
                    count=True,
                )
            ]
        else:
            who["instance_id"] = "other"
        with pytest.raises(ValueError):
            await watch.check()


async def test_pending_is_not_unknown_but_unsettled_final_window_is_refused():
    from scripts.performance.usage import UsageWatch

    current = snapshot()

    def service(request):
        return httpx.Response(
            200,
            json=identity()
            if request.url.path == "/release/status"
            else {"query_performance": current},
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(service)) as client:
        watch = UsageWatch(client, [target()], profile_hash="a" * 64)
        await watch.check(initial=True)
        current["counters"] = [
            dict(name=name, kind="stream", mode="mix", category="query", count=value)
            for name, value in [
                ("provider_calls", 1),
                ("usage_pending", 1),
                ("usage_unknown", 0),
            ]
        ]
        await watch.check()
        with pytest.raises(ValueError, match="unsettled"):
            await watch.check(final=True)


async def test_real_load_stops_future_arrivals_on_unknown_usage_without_cancel_or_retry():
    from scripts.performance.query_load import run_load

    metrics = QueryMetrics(enabled=True)
    trace = SimpleNamespace(kind="full", mode="mix")
    p = plan()
    p.update(target_qps=10, duration_seconds=0.3, max_requests=3)
    calls = []

    async def service(request):
        if request.url.path == "/release/status":
            return httpx.Response(200, json=identity())
        if request.url.path == "/health":
            return httpx.Response(200, json={"query_performance": metrics.snapshot()})
        calls.append(request.url.path)
        metrics.increment("provider_calls", trace=trace, category="query")
        await asyncio.sleep(0.02)
        return httpx.Response(200, json=dict(response="answer", llm_generated=True))

    report = await run_load(
        p,
        [{"query": "synthetic"}],
        base_url="https://pod.invalid",
        api_key="synthetic",
        confirmed=True,
        transport=httpx.MockTransport(service),
        usage_targets=[target()],
        usage_poll_seconds=0.01,
    )
    assert calls == ["/query"]
    assert report["completed"] == 1 and report["errors"] == 0
    assert report["client_stopped"] and not report["slo_met"]
    assert report["usage"]["error_class"] == "UsageUnknown"
    assert report["usage"]["verified"] is False


def test_interval_refuses_mixed_usage_and_timing_semantics():
    from scripts.performance.report import summarize_interval

    before, after = snapshot(), snapshot()
    after["observation_epoch"] = before["observation_epoch"]
    after.pop("measurement_version")
    with pytest.raises(ValueError, match="measurement"):
        summarize_interval(before, after)


def test_interval_refuses_disappearing_counter_instead_of_silently_losing_usage():
    from scripts.performance.report import summarize_interval

    metrics = QueryMetrics(enabled=True)
    metrics.increment(
        "provider_calls",
        trace=SimpleNamespace(kind="full", mode="mix"),
        category="query",
    )
    before = metrics.snapshot()
    after = metrics.snapshot()
    after["counters"] = []
    with pytest.raises(ValueError, match="counter disappeared"):
        summarize_interval(before, after)


async def test_all_instance_observations_are_bounded_concurrent_not_serial():
    from scripts.performance.usage import UsageWatch

    entered = set()
    ready = asyncio.Event()
    targets = [
        dict(
            base_url=f"https://pod{i}.invalid",
            instance_id=f"uid-{i}",
            config_hash="b" * 64,
        )
        for i in range(4)
    ]

    async def service(request):
        i = int(request.url.host[3])
        if request.url.path == "/release/status":
            entered.add(i)
            if len(entered) == 4:
                ready.set()
            await asyncio.wait_for(ready.wait(), 0.2)
            return httpx.Response(
                200,
                json=dict(
                    instance_id=f"uid-{i}", config_hash="b" * 64, profile_hash="a" * 64
                ),
            )
        return httpx.Response(200, json={"query_performance": snapshot()})

    async with httpx.AsyncClient(transport=httpx.MockTransport(service)) as client:
        watch = UsageWatch(client, targets, profile_hash="a" * 64)
        await watch.check(initial=True)
    assert watch.samples == 1 and watch.calls == 8


@pytest.mark.parametrize("where", ["row", "bucket"])
def test_interval_rejects_floating_point_observation_counts(where):
    from scripts.performance.report import summarize_interval

    metrics = QueryMetrics(enabled=True)
    before = metrics.snapshot()
    metrics.observe("vector_backend", 0.02)
    after = metrics.snapshot()
    if where == "row":
        after["durations"][0]["count"] = 1.0
    else:
        after["durations"][0]["buckets"][-1]["count"] = 1.0
    with pytest.raises(ValueError):
        summarize_interval(before, after)
