"""Bounded reported-usage evidence; never billing or release authority."""

import asyncio
import json
import math
import os
import stat

from lightrag.performance import ROLES


FINAL_PATH = "/dev/termination-log"
FINAL_MAX_BYTES = 3500
FIELDS = (
    "provider_calls",
    "usage_reports",
    "usage_unknown",
    "usage_pending",
    "input_tokens",
    "output_tokens",
    "provider_errors",
    "provider_rate_limits",
)
LIFETIMES = {"http", "queries", "writers", "waiters", "backend", "background"}


class UsageUnknown(ValueError):
    pass


def compact_usage(metrics):
    snapshot = metrics.snapshot()
    if snapshot.get("enabled") is not True:
        raise ValueError("Usage observations must be enabled")
    counts = {}
    for row in snapshot["counters"]:
        if row["name"] not in FIELDS:
            continue
        if (
            row["category"] not in ROLES
            or type(row["count"]) is not int
            or row["count"] < 0
        ):
            raise ValueError("Invalid provider usage counter")
        values = counts.setdefault(row["category"], [0] * len(FIELDS))
        values[FIELDS.index(row["name"])] += row["count"]
    return {
        key: snapshot[key]
        for key in (
            "measurement_version",
            "observation_epoch",
            "uptime_seconds",
            "active_queries",
            "observation_errors",
        )
    } | {"version": 1, "counts": counts}


def validate_usage(previous, current, *, quiescent=False, allow_unknown=False):
    if not isinstance(current, dict) or set(current) != {
        "version",
        "measurement_version",
        "observation_epoch",
        "uptime_seconds",
        "active_queries",
        "observation_errors",
        "counts",
    }:
        raise ValueError("Complete compact usage evidence required")
    if (
        type(current["version"]) is not int
        or current["version"] != 1
        or type(current["measurement_version"]) is not int
        or current["measurement_version"] != 2
    ):
        raise ValueError("Unsupported usage evidence version")
    epoch = current["observation_epoch"]
    if not isinstance(epoch, str) or not 1 <= len(epoch) <= 128:
        raise ValueError("Usage process epoch required")
    uptime = current["uptime_seconds"]
    if type(uptime) not in (int, float) or not math.isfinite(uptime) or uptime < 0:
        raise ValueError("Invalid usage uptime")
    for key in ("active_queries", "observation_errors"):
        if type(current[key]) is not int or current[key] < 0:
            raise ValueError("Invalid usage observation count")
    counts = current["counts"]
    if not isinstance(counts, dict) or set(counts) - ROLES:
        raise ValueError("Bounded usage roles required")
    for values in counts.values():
        if (
            not isinstance(values, list)
            or len(values) != len(FIELDS)
            or any(type(v) is not int or v < 0 for v in values)
        ):
            raise ValueError("Complete integer usage totals required")
        if values[0] != sum(values[1:4]):
            raise ValueError("Usage lifecycle is incomplete")
    if previous is not None:
        if (
            previous["observation_epoch"] != epoch
            or previous["uptime_seconds"] > uptime
            or not set(previous["counts"]) <= set(counts)
        ):
            raise ValueError("Usage instance replaced or counters reset")
        for role, values in previous["counts"].items():
            if any(counts[role][i] < value for i, value in enumerate(values) if i != 3):
                raise ValueError("Usage counter decreased")
    if current["observation_errors"]:
        raise ValueError("Usage observer failed")
    if not allow_unknown and any(v[2] for v in counts.values()):
        raise UsageUnknown("Terminal usage missing")
    if quiescent and (current["active_queries"] or any(v[3] for v in counts.values())):
        raise ValueError("Usage has not settled")


def final_summary(release):
    usage = compact_usage(release.rag.query_metrics)
    validate_usage(None, usage, quiescent=True, allow_unknown=True)
    lifecycle = release.lifecycle.snapshot()
    if (
        not release.lifecycle.retiring
        or set(lifecycle) != LIFETIMES
        or any(type(v) is not int or v != 0 for v in lifecycle.values())
    ):
        raise ValueError("Instance must be retired and drained before final usage")
    c = release.config
    return dict(
        version=1,
        final=True,
        scope=c.scope,
        instance_id=c.instance_id,
        slot=c.slot,
        config_hash=c.config_hash,
        profile_hash=c.profile_hash,
        lifecycle=lifecycle,
        usage=usage,
    )


def write_final_summary(release):
    path = getattr(release.config, "usage_final_path", None)
    if path is None:
        return
    content = json.dumps(
        final_summary(release), sort_keys=True, separators=(",", ":"), allow_nan=False
    ).encode()
    if len(content) > FINAL_MAX_BYTES:
        raise ValueError("Final usage exceeds termination evidence bound")
    fd = os.open(path, os.O_WRONLY | os.O_NOFOLLOW)
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise ValueError("Termination evidence must be an existing regular file")
        os.ftruncate(fd, 0)
        while content:
            written = os.write(fd, content)
            if written <= 0:
                raise OSError("Termination evidence write failed")
            content = content[written:]
        os.fsync(fd)
    finally:
        os.close(fd)


async def publish_final_summary(release):
    """Join final evidence I/O even after cancellation; never acknowledge it early."""
    if getattr(release.config, "usage_final_path", None) is None:
        return
    task = asyncio.create_task(asyncio.to_thread(write_final_summary, release))
    cancelled = False
    while not task.done():
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            cancelled = True
    task.result()
    if cancelled:
        raise asyncio.CancelledError()
