import json
import math

import httpx
import pytest


@pytest.mark.parametrize(
    "threshold,next_bound", [(0.2, 0.25), (3.0, 5.0), (20.0, 30.0)]
)
@pytest.mark.parametrize("position", ["below", "equal", "above"])
def test_client_histogram_slo_quantiles_resolve_threshold_and_neighbours(
    threshold, next_bound, position
):
    from scripts.performance.query_load import Histogram

    histogram = Histogram()
    value = {
        "below": math.nextafter(threshold, -math.inf),
        "equal": threshold,
        "above": math.nextafter(threshold, math.inf),
    }[position]
    for _ in range(20):
        histogram.add(value)

    result = histogram.report()
    expected = next_bound if position == "above" else threshold
    assert result["count"] == 20
    assert result["p95_upper_seconds"] == result["p99_upper_seconds"] == expected
    assert sum(bucket["count"] for bucket in result["buckets"]) == 20


def test_client_histogram_retains_empty_and_overflow_as_unknown_quantiles():
    from scripts.performance.query_load import Histogram

    histogram = Histogram()
    assert histogram.report()["p95_upper_seconds"] is None
    assert histogram.report()["p99_upper_seconds"] is None
    histogram.add(601.0)
    result = histogram.report()
    assert result["count"] == result["buckets"][-1]["count"] == 1
    assert result["buckets"][-1]["le"] is None
    assert result["p95_upper_seconds"] is result["p99_upper_seconds"] is None


@pytest.mark.parametrize(
    "low,p95,p99", [(0.1, 0.2, 0.25), (2.0, 3.0, 5.0), (10.0, 20.0, 30.0)]
)
def test_client_mixed_quantile_ranks_use_ceiling_and_distinguish_p95_from_p99(
    low, p95, p99
):
    from scripts.performance.query_load import Histogram

    histogram = Histogram()
    for value, count in [(low, 95), (p95, 4), (p99, 2)]:
        for _ in range(count):
            histogram.add(value)

    result = histogram.report()
    counts = {bucket["le"]: bucket["count"] for bucket in result["buckets"]}
    # For 101 samples, nearest ranks are 96 and 100, not floor ranks 95 and 99.
    assert result["count"] == 101
    assert result["p95_upper_seconds"] == p95
    assert result["p99_upper_seconds"] == p99
    assert counts[low] == 95 and counts[p95] == 4 and counts[p99] == 2
    assert sum(counts.values()) == 101


def plan():
    return dict(
        version=1,
        environment="isolated",
        operation="full_rag",
        target_qps=1000,
        online_users=10000,
        duration_seconds=0.005,
        max_inflight=10,
        max_requests=10,
        max_response_bytes=10000,
        request_timeout_seconds=2,
        stream_fraction=0.0,
        history_fraction=0.0,
        slo=dict(
            p95_seconds=1,
            p99_seconds=2,
            ttft_p95_seconds=1,
            error_rate=0.0,
            max_client_drop_rate=0.0,
        ),
        isolation=dict(
            evidence_id="synthetic-only",
            profile_hash="a" * 64,
            config_hashes=["b" * 64],
        ),
    )


def test_plan_keeps_target_online_users_and_inflight_distinct():
    from scripts.performance.query_load import validate_plan

    result = validate_plan(plan())
    assert result["ready"]
    assert (
        result["target_qps"] == 1000
        and result["online_users"] == 10000
        and result["max_inflight"] == 10
    )


def test_missing_slo_is_not_replaced_by_a_guessed_budget():
    from scripts.performance.query_load import validate_plan

    data = plan()
    data["slo"]["p99_seconds"] = None
    report = validate_plan(data)
    assert not report["ready"] and "slo.p99_seconds" in report["missing"]


async def test_network_requires_explicit_isolated_approval_before_any_call():
    from scripts.performance.query_load import run_load

    with pytest.raises(ValueError, match="confirmation"):
        await run_load(
            plan(),
            [{"query": "synthetic"}],
            base_url="https://fixture.invalid",
            api_key="secret",
            confirmed=False,
        )


async def test_real_http_transport_reports_actual_load_and_never_body_or_key():
    from scripts.performance.query_load import run_load

    calls = []

    async def service(request):
        calls.append(request.url.path)
        if request.url.path == "/release/status":
            return httpx.Response(
                200, json=dict(profile_hash="a" * 64, config_hash="b" * 64)
            )
        return httpx.Response(
            200,
            json={
                "response": "private-answer-that-must-not-be-in-report",
                "llm_generated": True,
            },
        )

    report = await run_load(
        plan(),
        [{"query": "private-question"}],
        base_url="https://fixture.invalid",
        api_key="private-key",
        confirmed=True,
        transport=httpx.MockTransport(service),
    )
    assert report["completed"] > 0 and report["provider_capacity_verified"] is False
    assert report["target_qps"] == 1000 and report["attempted"] <= 10
    assert not any(
        value in json.dumps(report)
        for value in ["private-key", "private-question", "private-answer"]
    )
    assert calls[0] == "/release/status"


async def test_ndjson_error_inside_200_is_not_success():
    from scripts.performance.query_load import run_load

    data = plan()
    data["stream_fraction"] = 1.0

    async def service(request):
        if request.url.path == "/release/status":
            return httpx.Response(
                200, json=dict(profile_hash="a" * 64, config_hash="b" * 64)
            )
        return httpx.Response(
            200, content=b'{"response":"first"}\n{"error":"private failure"}\n'
        )

    report = await run_load(
        data,
        [{"query": "synthetic"}],
        base_url="https://fixture.invalid",
        api_key="synthetic",
        confirmed=True,
        transport=httpx.MockTransport(service),
    )
    assert report["errors"] > 0 and report["slo_met"] is False
    assert "private failure" not in str(report)


def test_request_budget_cannot_silently_truncate_offered_load():
    from scripts.performance.query_load import validate_plan

    data = plan()
    data["duration_seconds"] = 1
    with pytest.raises(ValueError, match="max_requests"):
        validate_plan(data)


def test_generator_memory_and_latency_budgets_are_validated():
    from scripts.performance.query_load import validate_plan

    data = plan()
    data["max_response_bytes"] = 1024**3
    with pytest.raises(ValueError, match="safety bound"):
        validate_plan(data)
    data = plan()
    data["slo"]["p95_seconds"] = 3
    with pytest.raises(ValueError, match="p95"):
        validate_plan(data)


async def test_draining_slow_answers_cannot_claim_target_throughput():
    import asyncio
    from scripts.performance.query_load import run_load

    async def service(request):
        if request.url.path == "/release/status":
            return httpx.Response(
                200, json=dict(profile_hash="a" * 64, config_hash="b" * 64)
            )
        await asyncio.sleep(0.03)
        return httpx.Response(
            200, json={"response": "synthetic", "llm_generated": True}
        )

    report = await run_load(
        plan(),
        [{"query": "synthetic"}],
        base_url="https://fixture.invalid",
        api_key="synthetic",
        confirmed=True,
        transport=httpx.MockTransport(service),
    )
    assert report["achieved_completion_qps"] < report["target_qps"]
    assert report["slo_met"] is False
    assert report["throughput_met"] is False
    assert report["schedule_lag"]["count"] == report["attempted"]


async def test_invalid_history_is_rejected_before_network():
    from scripts.performance.query_load import run_load

    data = plan()
    data["history_fraction"] = 1

    def no_network(request):
        raise AssertionError("validation must precede network")

    with pytest.raises(ValueError, match="history"):
        await run_load(
            data,
            [
                {
                    "query": "synthetic",
                    "history": [{"role": "system", "content": "inject"}],
                }
            ],
            base_url="https://fixture.invalid",
            api_key="synthetic",
            confirmed=True,
            transport=httpx.MockTransport(no_network),
        )


@pytest.mark.parametrize("streaming", [False, True])
@pytest.mark.parametrize(
    "body",
    [
        {
            "response": "No relevant context found for the query.",
            "llm_generated": False,
        },
        {"response": "", "llm_generated": True},
        {"response": {"not": "text"}, "llm_generated": True},
    ],
)
async def test_full_rag_never_counts_placeholder_or_invalid_answer_as_generation(
    streaming, body
):
    from scripts.performance.query_load import run_load

    data = plan()
    data["stream_fraction"] = float(streaming)

    async def service(request):
        if request.url.path == "/release/status":
            return httpx.Response(
                200, json=dict(profile_hash="a" * 64, config_hash="b" * 64)
            )
        if streaming:
            return httpx.Response(
                200,
                content=(json.dumps(body) + "\n").encode(),
                headers={"content-type": "application/x-ndjson"},
            )
        return httpx.Response(200, json=body)

    report = await run_load(
        data,
        [{"query": "synthetic"}],
        base_url="https://fixture.invalid",
        api_key="synthetic",
        confirmed=True,
        transport=httpx.MockTransport(service),
    )
    assert report["completed"] == 0 and report["errors"] > 0 and not report["slo_met"]


async def test_cooperative_stop_ends_new_arrivals_but_drains_existing_queries():
    import asyncio
    from scripts.performance.query_load import run_load

    data = plan()
    data.update(target_qps=100, duration_seconds=0.1, max_requests=10)
    stop = asyncio.Event()
    calls = []

    async def service(request):
        if request.url.path == "/release/status":
            return httpx.Response(
                200, json=dict(profile_hash="a" * 64, config_hash="b" * 64)
            )
        calls.append(request.url.path)
        stop.set()
        await asyncio.sleep(0.005)
        return httpx.Response(
            200, json={"response": "synthetic", "llm_generated": True}
        )

    report = await run_load(
        data,
        [{"query": "synthetic"}],
        base_url="https://fixture.invalid",
        api_key="synthetic",
        confirmed=True,
        transport=httpx.MockTransport(service),
        stop_event=stop,
    )
    assert (
        report["completed"] == 1
        and len(calls) == 1
        and report["client_stopped"]
        and not report["slo_met"]
    )


async def test_cooperative_stop_interrupts_long_scheduled_arrival_without_cancelling_request():
    import asyncio
    import httpx
    from scripts.performance.query_load import run_load

    p = plan()
    p.update(target_qps=0.01, duration_seconds=101, max_requests=2)
    stop = asyncio.Event()

    async def service(request):
        if request.url.path == "/release/status":
            return httpx.Response(
                200, json={"profile_hash": "a" * 64, "config_hash": "b" * 64}
            )
        stop.set()
        return httpx.Response(200, json={"response": "ok", "llm_generated": True})

    report = await asyncio.wait_for(
        run_load(
            p,
            [{"query": "q"}],
            base_url="https://fixture.invalid",
            api_key="private",
            confirmed=True,
            transport=httpx.MockTransport(service),
            stop_event=stop,
        ),
        timeout=0.2,
    )
    assert report["client_stopped"] and report["completed"] == 1
