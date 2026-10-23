import copy
import json
import math
import stat

import pytest


@pytest.mark.parametrize(
    "stage,threshold,next_bound",
    [("vector", 0.2, 0.25), ("first_response", 3.0, 5.0), ("query_total", 20.0, 30.0)],
)
@pytest.mark.parametrize("position", ["below", "equal", "above"])
def test_interval_slo_quantiles_resolve_threshold_and_immediate_neighbours(
    stage, threshold, next_bound, position
):
    from lightrag.performance import QueryMetrics
    from scripts.performance.report import summarize_interval

    metrics = QueryMetrics(enabled=True)
    metrics.observe(stage, next_bound, kind="full", mode="mix")
    before = metrics.snapshot()
    value = {
        "below": math.nextafter(threshold, -math.inf),
        "equal": threshold,
        "above": math.nextafter(threshold, math.inf),
    }[position]
    for _ in range(20):
        metrics.observe(stage, value, kind="full", mode="mix")

    row = summarize_interval(before, metrics.snapshot())["durations"][0]
    expected = next_bound if position == "above" else threshold
    assert row["count"] == 20
    assert row["p95_upper_seconds"] == row["p99_upper_seconds"] == expected


@pytest.mark.parametrize(
    "stage,threshold,next_bound",
    [("vector", 0.2, 0.25), ("first_response", 3.0, 5.0), ("query_total", 20.0, 30.0)],
)
def test_interval_slo_quantile_ranks_use_delta_not_lifetime_samples(
    stage, threshold, next_bound
):
    from lightrag.performance import QueryMetrics
    from scripts.performance.report import summarize_interval

    metrics = QueryMetrics(enabled=True)
    for _ in range(10):
        metrics.observe(stage, 301.0)
    before = metrics.snapshot()
    for _ in range(94):
        metrics.observe(stage, math.nextafter(threshold, -math.inf))
    metrics.observe(stage, threshold)
    for _ in range(4):
        metrics.observe(stage, math.nextafter(threshold, math.inf))
    metrics.observe(stage, 301.0)

    row = summarize_interval(before, metrics.snapshot())["durations"][0]
    counts = {bucket["le"]: bucket["count"] for bucket in row["buckets"]}
    assert row["count"] == 100
    assert row["p95_upper_seconds"] == threshold
    assert row["p99_upper_seconds"] == next_bound
    assert counts.get(threshold) == 95
    assert counts[next_bound] == 99 and counts[None] == 100


@pytest.mark.parametrize("existing_histogram", [False, True])
def test_interval_zero_increment_never_reports_historical_quantiles(
    existing_histogram,
):
    from lightrag.performance import QueryMetrics
    from scripts.performance.report import summarize_interval

    metrics = QueryMetrics(enabled=True)
    if existing_histogram:
        metrics.observe("vector", 0.2, kind="full", mode="mix")
    before = metrics.snapshot()
    result = summarize_interval(before, metrics.snapshot())

    assert result["production_accepted"] is False
    if not existing_histogram:
        assert result["durations"] == []
    else:
        assert len(result["durations"]) == 1
        row = result["durations"][0]
        assert row["count"] == 0 and row["sum_seconds"] == 0
        assert all(bucket["count"] == 0 for bucket in row["buckets"])
        assert row["p95_upper_seconds"] is None
        assert row["p99_upper_seconds"] is None


@pytest.mark.parametrize(
    "finite_value,finite_count,overflow_count,p95,p99",
    [
        (20.0, 99, 2, 20.0, None),
        (20.0, 95, 6, None, None),
        (20.0, 0, 101, None, None),
        (300.0, 99, 2, 300.0, None),
    ],
)
def test_interval_overflow_quantiles_stay_unknown_not_zero_or_last_finite_bound(
    finite_value, finite_count, overflow_count, p95, p99
):
    from lightrag.performance import QueryMetrics
    from scripts.performance.report import summarize_interval

    metrics = QueryMetrics(enabled=True)
    metrics.observe("query_total", 0.1, kind="full", mode="mix")
    before = metrics.snapshot()
    for value, count in [(finite_value, finite_count), (301.0, overflow_count)]:
        for _ in range(count):
            metrics.observe("query_total", value, kind="full", mode="mix")

    result = summarize_interval(before, metrics.snapshot())
    row = result["durations"][0]
    counts = {bucket["le"]: bucket["count"] for bucket in row["buckets"]}
    assert row["count"] == 101
    assert counts[finite_value] == counts[300.0] == finite_count
    assert counts[None] == 101
    assert row["p95_upper_seconds"] == p95
    assert row["p99_upper_seconds"] == p99
    assert result["production_accepted"] is False
    encoded_row = json.loads(json.dumps(result, allow_nan=False))["durations"][0]
    assert encoded_row["p95_upper_seconds"] == p95
    assert encoded_row["p99_upper_seconds"] is None


@pytest.mark.parametrize("legacy_sides", [(True, False), (False, True), (True, True)])
def test_interval_rejects_legacy_bucket_layout_instead_of_inventing_counts(
    legacy_sides,
):
    from scripts.performance.report import summarize_interval

    metrics, _ = sample()
    before = metrics.snapshot()
    metrics.observe("vector", 0.2, kind="full", mode="mix")
    after = metrics.snapshot()
    for snapshot, legacy in zip((before, after), legacy_sides):
        if legacy:
            for row in snapshot["durations"]:
                row["buckets"] = [
                    bucket
                    for bucket in row["buckets"]
                    if bucket["le"] not in {0.2, 3.0, 20.0}
                ]
    with pytest.raises(ValueError, match="histogram"):
        summarize_interval(before, after)


def test_interval_cli_exports_slo_quantiles_with_private_output(tmp_path):
    from lightrag.performance import QueryMetrics
    from scripts.performance.report import main

    metrics = QueryMetrics(enabled=True)
    before = tmp_path / "before.json"
    before.write_text(json.dumps(metrics.snapshot()))
    for stage, seconds in [
        ("vector", 0.2),
        ("first_response", 3.0),
        ("query_total", 20.0),
    ]:
        metrics.observe(stage, seconds)
    after = tmp_path / "after.json"
    after.write_text(json.dumps(metrics.snapshot()))
    output = tmp_path / "interval.json"

    assert (
        main(["--before", str(before), "--after", str(after), "--output", str(output)])
        == 0
    )
    result = json.loads(output.read_text())
    rows = {row["stage"]: row for row in result["durations"]}
    assert rows["vector"]["p95_upper_seconds"] == 0.2
    assert rows["first_response"]["p95_upper_seconds"] == 3.0
    assert rows["query_total"]["p95_upper_seconds"] == 20.0
    assert result["production_accepted"] is False
    assert stat.S_IMODE(output.stat().st_mode) == 0o600


def sample():
    from lightrag.performance import QueryMetrics, QueryTrace

    m = QueryMetrics(enabled=True)
    t = QueryTrace(m, "full", "mix")
    m.observe("vector", 0.01, kind="full", mode="mix")
    m.increment("provider_calls", trace=t, category="query")
    return m, t


def test_interval_report_uses_deltas_without_leaking_input_or_guessing_usage():
    from scripts.performance.report import summarize_interval

    m, trace = sample()
    before = m.snapshot()
    m.observe("vector", 0.02, kind="full", mode="mix")
    m.increment("provider_calls", trace=trace, category="query")
    after = m.snapshot()
    before["private"] = "private-secret"
    result = summarize_interval(before, after)
    row = next(r for r in result["durations"] if r["stage"] == "vector")
    assert row["count"] == 1 and row["p95_upper_seconds"] == 0.025
    assert (
        next(r for r in result["counters"] if r["name"] == "provider_calls")["count"]
        == 1
    )
    assert result["usage_unknown_end"] == 2
    assert "private-secret" not in str(result)
    assert result["production_accepted"] is False


def test_process_replacement_cannot_be_mistaken_for_counter_delta():
    from scripts.performance.report import summarize_interval

    first, _ = sample()
    second, _ = sample()
    with pytest.raises(ValueError, match="epoch"):
        summarize_interval(first.snapshot(), second.snapshot())


def test_corrupt_or_nonmonotonic_histogram_is_rejected():
    from scripts.performance.report import summarize_interval

    m, _ = sample()
    before = m.snapshot()
    after = copy.deepcopy(before)
    after["durations"][0]["buckets"][-1]["count"] = 0
    with pytest.raises(ValueError, match="histogram"):
        summarize_interval(before, after)
