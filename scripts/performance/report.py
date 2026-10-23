"""Offline, redacted interval summaries of one process's query_performance snapshots."""

import argparse
import json
import math
import os
from pathlib import Path

from lightrag.performance import (
    BOUNDS,
    STAGES,
    KINDS,
    MODES,
    OUTCOMES,
    COUNTERS,
    ROLES,
    CACHES,
)


def _number(value):
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(value)
        or value < 0
    ):
        raise ValueError("invalid nonnegative metric")
    return value


def _rows(snapshot, field, labels, allowed):
    result = {}
    for row in snapshot[field]:
        if any(row[k] not in values for k, values in zip(labels, allowed)):
            raise ValueError("unknown metric vocabulary")
        key = tuple(row[k] for k in labels)
        if key in result:
            raise ValueError("duplicate metric")
        _count(row["count"])
        result[key] = row
    return result


def _count(value):
    if type(value) is not int or value < 0:
        raise ValueError("invalid integer metric count")
    return value


def summarize_interval(before, after):
    if before.get("measurement_version") != after.get("measurement_version"):
        raise ValueError("measurement version changed; split the report")
    if after.get("measurement_version") not in (None, 2):
        raise ValueError("unsupported measurement version")
    if not before.get("enabled") or not after.get("enabled"):
        raise ValueError("metrics are not enabled")
    epoch = before.get("observation_epoch")
    if not epoch or epoch != after.get("observation_epoch"):
        raise ValueError("process epoch changed; split the report at replacement")
    interval = _number(after["uptime_seconds"]) - _number(before["uptime_seconds"])
    if interval < 0:
        raise ValueError("process uptime decreased")
    labels = ("stage", "kind", "mode", "outcome")
    previous = _rows(before, "durations", labels, (STAGES, KINDS, MODES, OUTCOMES))
    current = _rows(after, "durations", labels, (STAGES, KINDS, MODES, OUTCOMES))
    if not set(previous) <= set(current):
        raise ValueError("histogram disappeared")
    durations = []
    for key, row in current.items():
        old = previous.get(
            key,
            dict(
                count=0,
                sum_seconds=0,
                buckets=[dict(le=b, count=0) for b in (*BOUNDS, None)],
            ),
        )
        count = row["count"] - old["count"]
        total = _number(row["sum_seconds"]) - _number(old["sum_seconds"])
        if (
            count < 0
            or total < 0
            or len(row["buckets"]) != len(BOUNDS) + 1
            or len(old["buckets"]) != len(BOUNDS) + 1
        ):
            raise ValueError("invalid histogram delta")
        buckets = []
        seen = 0
        for bound, new, base in zip((*BOUNDS, None), row["buckets"], old["buckets"]):
            n = _count(new["count"]) - _count(base["count"])
            if new["le"] != bound or base["le"] != bound or n < seen or n > count:
                raise ValueError("invalid histogram bucket")
            seen = n
            buckets.append(dict(le=bound, count=n))
        if seen != count:
            raise ValueError("invalid histogram count")

        def percentile(p):
            if not count:
                return None
            return next(b["le"] for b in buckets if b["count"] >= math.ceil(count * p))

        durations.append(
            dict(
                zip(labels, key),
                count=count,
                sum_seconds=total,
                buckets=buckets,
                p95_upper_seconds=percentile(0.95),
                p99_upper_seconds=percentile(0.99),
            )
        )
    labels = ("name", "kind", "mode", "category")
    previous = _rows(
        before, "counters", labels, (COUNTERS, KINDS, MODES, ROLES | CACHES)
    )
    current = _rows(after, "counters", labels, (COUNTERS, KINDS, MODES, ROLES | CACHES))
    if not set(previous) <= set(current):
        raise ValueError("counter disappeared")
    counters = []
    for key, row in current.items():
        if key[0] in {"usage_unknown", "usage_pending"}:
            continue  # Gauges are reported as endpoints, never monotonic deltas.
        n = row["count"] - previous.get(key, {"count": 0})["count"]
        if n < 0:
            raise ValueError("counter decreased")
        counters.append(dict(zip(labels, key), count=n))
    return dict(
        interval_seconds=interval,
        durations=durations,
        counters=counters,
        usage_unknown_end=sum(
            r["count"] for k, r in current.items() if k[0] == "usage_unknown"
        ),
        measurement_version=after.get("measurement_version"),
        usage_pending_end=sum(
            r["count"] for k, r in current.items() if k[0] == "usage_pending"
        ),
        active_queries_start=_number(before["active_queries"]),
        active_queries_end=_number(after["active_queries"]),
        observation_errors_end=_number(after["observation_errors"]),
        percentile_method="cumulative_fixed_histogram_upper_bound",
        production_accepted=False,
    )


def _read(path):
    if path.stat().st_size > 8 * 1024**2:
        raise ValueError("snapshot too large")
    value = json.loads(path.read_text())
    return value.get("query_performance", value)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--before", type=Path, required=True)
    parser.add_argument("--after", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        report = summarize_interval(_read(args.before), _read(args.after))
        content = json.dumps(report, indent=2, allow_nan=False) + "\n"
        fd = os.open(args.output, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w") as output:
            output.write(content)
        print("Offline interval report saved; not a capacity acceptance")
        return 0
    except Exception as error:
        print(
            type(error).__name__ + ": interval report refused; inspect inputs privately"
        )
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
