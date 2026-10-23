"""Explicit isolated-target query load. Default CLI only validates the plan."""

import argparse
import asyncio
import hashlib
import json
import math
import os
from pathlib import Path
import time
from urllib.parse import urlsplit


FIELDS = {
    "version",
    "environment",
    "operation",
    "target_qps",
    "online_users",
    "duration_seconds",
    "max_inflight",
    "max_requests",
    "max_response_bytes",
    "request_timeout_seconds",
    "stream_fraction",
    "history_fraction",
    "slo",
    "isolation",
}
SLO = {
    "p95_seconds",
    "p99_seconds",
    "ttft_p95_seconds",
    "error_rate",
    "max_client_drop_rate",
}
BOUNDS = (
    0.001,
    0.005,
    0.01,
    0.025,
    0.05,
    0.1,
    0.2,
    0.25,
    0.5,
    1.0,
    2.0,
    3.0,
    5.0,
    10.0,
    20.0,
    30.0,
    60.0,
    120.0,
    300.0,
    600.0,
)


def validate_plan(plan):
    if not isinstance(plan, dict) or set(plan) != FIELDS:
        raise ValueError("plan: explicit versioned fields required")
    if (
        plan["version"] != 1
        or plan["environment"] != "isolated"
        or plan["operation"] != "full_rag"
    ):
        raise ValueError("plan: only explicitly isolated full RAG is allowed")
    missing = []
    for field in (
        "target_qps",
        "online_users",
        "duration_seconds",
        "max_inflight",
        "max_requests",
        "max_response_bytes",
        "request_timeout_seconds",
    ):
        value = plan[field]
        if value is None:
            missing.append(field)
            continue
        if (
            isinstance(value, bool)
            or not isinstance(value, (float, int))
            or not math.isfinite(value)
            or value <= 0
        ):
            raise ValueError("plan: invalid numeric field " + field)
        if (
            field
            in {"online_users", "max_inflight", "max_requests", "max_response_bytes"}
            and type(value) is not int
        ):
            raise ValueError("plan: integer field required " + field)
    for field in ("stream_fraction", "history_fraction"):
        value = plan[field]
        if value is None:
            missing.append(field)
        elif (
            isinstance(value, bool)
            or not isinstance(value, (float, int))
            or not math.isfinite(value)
            or not 0 <= value <= 1
        ):
            raise ValueError("plan: invalid fraction " + field)
    if not isinstance(plan["slo"], dict) or set(plan["slo"]) != SLO:
        raise ValueError("slo: explicit fields required")
    for field, value in plan["slo"].items():
        if value is None:
            missing.append("slo." + field)
        elif (
            isinstance(value, bool)
            or not isinstance(value, (int, float))
            or not math.isfinite(value)
            or value < 0
            or (field.endswith("seconds") and value == 0)
            or (field in {"error_rate", "max_client_drop_rate"} and value > 1)
        ):
            raise ValueError("slo: invalid numeric field " + field)
    isolation = plan["isolation"]
    if not isinstance(isolation, dict) or set(isolation) != {
        "evidence_id",
        "profile_hash",
        "config_hashes",
    }:
        raise ValueError("isolation: explicit evidence and identity fields required")
    import re

    for field in ("evidence_id", "profile_hash", "config_hashes"):
        if not isolation[field]:
            missing.append("isolation." + field)
    if isolation["profile_hash"] and not re.fullmatch(
        "[a-f0-9]{64}", isolation["profile_hash"]
    ):
        raise ValueError("isolation: invalid profile hash")
    if isolation["config_hashes"] and (
        not isinstance(isolation["config_hashes"], list)
        or not all(
            isinstance(v, str) and re.fullmatch("[a-f0-9]{64}", v)
            for v in isolation["config_hashes"]
        )
    ):
        raise ValueError("isolation: invalid configuration hashes")
    if (
        (plan["max_requests"] and plan["max_requests"] > 10000000)
        or (plan["max_inflight"] and plan["max_inflight"] > 20000)
        or (plan["max_response_bytes"] and plan["max_response_bytes"] > 16 * 1024**2)
        or (
            plan["max_response_bytes"]
            and plan["max_inflight"]
            and plan["max_response_bytes"] * plan["max_inflight"] > 256 * 1024**2
        )
    ):
        raise ValueError("plan: runner safety bound exceeded")
    if all(
        plan[k] is not None for k in ("max_requests", "target_qps", "duration_seconds")
    ) and plan["max_requests"] < math.ceil(
        plan["target_qps"] * plan["duration_seconds"]
    ):
        raise ValueError("plan: max_requests cannot truncate target load")
    if (
        all(plan["slo"][k] is not None for k in ("p95_seconds", "p99_seconds"))
        and plan["slo"]["p95_seconds"] > plan["slo"]["p99_seconds"]
    ):
        raise ValueError("slo: p95 cannot exceed p99")
    return {
        "ready": not missing,
        "missing": sorted(missing),
        "operation": plan["operation"],
        "target_qps": plan["target_qps"],
        "online_users": plan["online_users"],
        "max_inflight": plan["max_inflight"],
    }


class Histogram:
    def __init__(self):
        self.buckets = [0] * (len(BOUNDS) + 1)
        self.count = 0
        self.total = 0.0
        self.maximum = 0.0

    def add(self, value):
        self.buckets[
            next((i for i, b in enumerate(BOUNDS) if value <= b), len(BOUNDS))
        ] += 1
        self.count += 1
        self.total += value
        self.maximum = max(self.maximum, value)

    def percentile(self, p):
        if not self.count:
            return None
        rank = math.ceil(self.count * p)
        seen = 0
        for i, n in enumerate(self.buckets):
            seen += n
            if seen >= rank:
                return BOUNDS[i] if i < len(BOUNDS) else None

    def report(self):
        return {
            "count": self.count,
            "sum_seconds": self.total,
            "max_seconds": self.maximum,
            "p95_upper_seconds": self.percentile(0.95),
            "p99_upper_seconds": self.percentile(0.99),
            "buckets": [
                {"le": bound, "count": n}
                for bound, n in zip((*BOUNDS, None), self.buckets)
            ],
        }


async def run_load(
    plan,
    dataset,
    *,
    base_url,
    api_key,
    confirmed=False,
    transport=None,
    stop_event=None,
    usage_targets=None,
    usage_poll_seconds=1.0,
    usage_monitor=None,
):
    if confirmed is not True:
        raise ValueError("Explicit isolated-target confirmation is required")
    if usage_monitor is not None:
        from scripts.performance.dynamic_usage import DynamicUsage

        if (
            not isinstance(usage_monitor, DynamicUsage)
            or not usage_monitor.started
            or usage_targets is not None
        ):
            raise ValueError("Verified external dynamic usage owner required")
        usage_monitor.ensure_running()
    if not validate_plan(plan)["ready"]:
        raise ValueError("plan: missing approved inputs; refusing network load")
    u = urlsplit(base_url)
    if u.scheme != "https" and not (
        u.scheme == "http" and u.hostname in {"localhost", "127.0.0.1", "::1"}
    ):
        raise ValueError("target: HTTPS or explicit loopback required")
    if (
        u.username
        or u.password
        or u.query
        or u.fragment
        or not u.hostname
        or not api_key
    ):
        raise ValueError(
            "target: invalid endpoint or missing dedicated load credential"
        )
    if not isinstance(dataset, list) or not dataset or len(dataset) > 10000:
        raise ValueError("dataset: bounded nonempty query list required")
    for item in dataset:
        if (
            not isinstance(item, dict)
            or set(item) - {"query", "mode", "history"}
            or not isinstance(item.get("query"), str)
            or not item["query"].strip()
            or len(item["query"]) > 20000
        ):
            raise ValueError("dataset: explicit query, mode and optional history only")
        if item.get("mode", "mix") not in {"local", "global", "hybrid", "mix", "naive"}:
            raise ValueError("dataset: invalid query mode")
        history = item.get("history", [])
        if (
            not isinstance(history, list)
            or len(history) > 100
            or any(
                not isinstance(turn, dict)
                or set(turn) != {"role", "content"}
                or turn["role"] not in {"user", "assistant"}
                or not isinstance(turn["content"], str)
                or len(turn["content"]) > 20000
                for turn in history
            )
        ):
            raise ValueError("dataset: invalid bounded history")
        if plan["history_fraction"] and not history:
            raise ValueError(
                "dataset: declared multi-turn load requires explicit histories"
            )
    import httpx
    from scripts.performance.usage import UsageWatch

    if (
        type(usage_poll_seconds) not in (int, float)
        or not math.isfinite(usage_poll_seconds)
        or not 0.01 <= usage_poll_seconds <= 60
    ):
        raise ValueError("Bounded usage sampling interval required")
    stop_event = stop_event or asyncio.Event()
    usage_report = dict(
        verified=False,
        error_class="NotConfigured",
        samples=0,
        instance_count=0,
        calls=0,
    )
    durations = Histogram()
    first_tokens = Histogram()
    schedule_lag = Histogram()
    stats = dict(
        attempted=0,
        completed=0,
        errors=0,
        client_dropped=0,
        stream_requests=0,
        history_requests=0,
        response_bytes=0,
        not_sent=0,
    )
    error_classes = {}
    active = set()
    started = time.perf_counter()
    async with (
        httpx.AsyncClient(
            base_url=base_url.rstrip("/"),
            headers={"X-API-Key": api_key},
            timeout=plan["request_timeout_seconds"],
            limits=httpx.Limits(
                max_connections=plan["max_inflight"],
                max_keepalive_connections=plan["max_inflight"],
            ),
            follow_redirects=False,
            trust_env=False,
            transport=transport,
        ) as client,
        httpx.AsyncClient(
            headers={"X-API-Key": api_key},
            timeout=10,
            limits=httpx.Limits(max_connections=16, max_keepalive_connections=16),
            follow_redirects=False,
            trust_env=False,
            transport=transport,
        ) as metrics_client,
    ):
        identity = await client.get("/release/status")
        identity.raise_for_status()
        observed = identity.json()
        isolation = plan["isolation"]
        if (
            observed.get("profile_hash") != isolation["profile_hash"]
            or observed.get("config_hash") not in isolation["config_hashes"]
        ):
            raise ValueError("target: isolated release identity mismatch")
        started = time.perf_counter()

        async def request(index):
            if stop_event.is_set():
                stats["not_sent"] += 1
                return
            begin = time.perf_counter()
            first = None
            item = dataset[index % len(dataset)]
            streaming = (index * 0.61803398875) % 1 < plan["stream_fraction"]
            history = (index * 0.41421356237) % 1 < plan["history_fraction"]
            stats["stream_requests"] += int(streaming)
            stats["history_requests"] += int(history)
            payload = {
                "query": item["query"],
                "mode": item.get("mode", "mix"),
                "stream": streaming,
            }
            if history:
                payload["conversation_history"] = item["history"]

            async def exchange():
                nonlocal first
                async with client.stream(
                    "POST", "/query/stream" if streaming else "/query", json=payload
                ) as response:
                    response.raise_for_status()
                    content_type = (
                        response.headers.get("content-type", "")
                        .split(";", 1)[0]
                        .strip()
                        .lower()
                    )
                    if content_type != (
                        "application/x-ndjson" if streaming else "application/json"
                    ):
                        raise ValueError("InvalidContentType")
                    size = 0
                    buffer = bytearray()
                    answer = False

                    def accept_event(event):
                        nonlocal answer, first
                        if (
                            not isinstance(event, dict)
                            or event.get("error") is not None
                        ):
                            raise ValueError("StreamError")
                        if (
                            "llm_generated" in event
                            and event["llm_generated"] is not True
                        ):
                            raise ValueError("NoGeneration")
                        if "response" in event:
                            text = event["response"]
                            if not isinstance(text, str):
                                raise ValueError("InvalidResponse")
                            answer = answer or bool(text.strip())
                            if text and first is None:
                                first = time.perf_counter() - begin

                    async for chunk in response.aiter_bytes():
                        size += len(chunk)
                        if size > plan["max_response_bytes"]:
                            raise ValueError("ResponseLimit")
                        if not streaming:
                            buffer.extend(chunk)
                            continue
                        buffer.extend(chunk)
                        while b"\n" in buffer:
                            line, _, tail = buffer.partition(b"\n")
                            buffer[:] = tail
                            if not line.strip():
                                continue
                            event = json.loads(line)
                            accept_event(event)
                    if streaming:
                        if buffer.strip():
                            event = json.loads(buffer)
                            accept_event(event)
                        if not answer:
                            raise ValueError("EmptyStream")
                    else:
                        result = json.loads(buffer)
                        if (
                            not isinstance(result, dict)
                            or result.get("error") is not None
                            or result.get("llm_generated") is not True
                            or not isinstance(result.get("response"), str)
                            or not result["response"].strip()
                        ):
                            raise ValueError("InvalidResponse")
                    stats["response_bytes"] += size

            try:
                await asyncio.wait_for(
                    exchange(), timeout=plan["request_timeout_seconds"]
                )
                stats["completed"] += 1
                if first is not None:
                    first_tokens.add(first)
            except Exception as error:
                stats["errors"] += 1
                if stop_event is not None:
                    stop_event.set()
                name = (
                    "timeout"
                    if isinstance(error, (TimeoutError, httpx.TimeoutException))
                    else "http"
                    if isinstance(error, httpx.HTTPStatusError)
                    else "response_or_transport"
                )
                error_classes[name] = error_classes.get(name, 0) + 1
            finally:
                durations.add(time.perf_counter() - begin)

        watch = (
            None
            if usage_targets is None
            else UsageWatch(
                metrics_client, usage_targets, profile_hash=isolation["profile_hash"]
            )
        )
        done = asyncio.Event()
        watcher = None
        if watch is not None:
            if any(
                t["config_hash"] not in isolation["config_hashes"]
                for t in usage_targets
            ):
                raise ValueError("Usage target configuration not approved")
            await watch.check(initial=True)
        started = time.perf_counter()
        if watch is not None:
            watcher = asyncio.create_task(
                watch.watch(stop_event, done, interval=usage_poll_seconds)
            )
        try:
            count = math.ceil(plan["target_qps"] * plan["duration_seconds"])
            for i in range(count):
                if stop_event is not None and stop_event.is_set():
                    break
                delay = started + i / plan["target_qps"] - time.perf_counter()
                if delay > 0:
                    if stop_event is None:
                        await asyncio.sleep(delay)
                    else:
                        try:
                            await asyncio.wait_for(stop_event.wait(), timeout=delay)
                        except TimeoutError:
                            pass
                schedule_lag.add(
                    max(0.0, time.perf_counter() - started - i / plan["target_qps"])
                )
                if stop_event is not None and stop_event.is_set():
                    break
                stats["attempted"] += 1
                if len(active) >= plan["max_inflight"]:
                    stats["client_dropped"] += 1
                    if stop_event is not None:
                        stop_event.set()
                    continue
                task = asyncio.create_task(request(i))
                active.add(task)
                task.add_done_callback(active.discard)
            if active:
                await asyncio.gather(*tuple(active))
            finished = time.perf_counter()
        finally:
            done.set()
            if watcher is not None:
                await watcher
        if watch is not None:
            try:
                await watch.check(final=True)
            except Exception as error:
                watch.error_class = watch.error_class or type(error).__name__
                stop_event.set()
            usage_report = watch.report()
        elif usage_monitor is not None:
            usage_report = usage_monitor.report()
    elapsed = finished - started
    error_rate = stats["errors"] / max(1, stats["attempted"] - stats["client_dropped"])
    drop_rate = stats["client_dropped"] / max(1, stats["attempted"])
    d, tt = durations.report(), first_tokens.report()
    slo = plan["slo"]
    completion_qps = stats["completed"] / max(elapsed, plan["duration_seconds"])
    throughput_met = completion_qps >= plan["target_qps"]
    client_met = (
        throughput_met
        and not (stop_event is not None and stop_event.is_set())
        and stats["completed"] > 0
        and error_rate <= slo["error_rate"]
        and drop_rate <= slo["max_client_drop_rate"]
        and d["p95_upper_seconds"] is not None
        and d["p95_upper_seconds"] <= slo["p95_seconds"]
        and d["p99_upper_seconds"] is not None
        and d["p99_upper_seconds"] <= slo["p99_seconds"]
        and (
            not stats["stream_requests"]
            or tt["p95_upper_seconds"] is not None
            and tt["p95_upper_seconds"] <= slo["ttft_p95_seconds"]
        )
    )
    return dict(
        stats,
        usage=usage_report,
        client_stopped=stop_event is not None and stop_event.is_set(),
        target_qps=plan["target_qps"],
        online_users=plan["online_users"],
        max_inflight=plan["max_inflight"],
        elapsed_seconds=elapsed,
        achieved_completion_qps=completion_qps,
        throughput_met=throughput_met,
        schedule_lag=schedule_lag.report(),
        error_rate=error_rate,
        client_drop_rate=drop_rate,
        errors_by_class=error_classes,
        duration=d,
        first_token=tt,
        client_slo_met=bool(client_met),
        slo_met=bool(client_met and usage_report["verified"]),
        percentile_method="fixed_histogram_upper_bound",
        provider_capacity_verified=False,
        production_accepted=False,
        knowledge_quality_verified=False,
        topology_verified=False,
        dataset_sha256=hashlib.sha256(
            json.dumps(dataset, sort_keys=True).encode()
        ).hexdigest(),
        observed_identity={k: observed[k] for k in ("profile_hash", "config_hash")},
    )


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--plan", type=Path, required=True)
    parser.add_argument("--dataset", type=Path)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--usage-targets", type=Path)
    parser.add_argument("--run", action="store_true")
    parser.add_argument("--confirm-isolated-target", action="store_true")
    args = parser.parse_args(argv)
    try:
        plan = json.loads(args.plan.read_text())
        report = validate_plan(plan)
        if args.run:
            if not args.dataset or not args.usage_targets:
                raise ValueError(
                    "dataset and explicit per-instance usage targets are required"
                )
            report = asyncio.run(
                run_load(
                    plan,
                    json.loads(args.dataset.read_text()),
                    base_url=os.environ.get("LIGHTRAG_PERF_BASE_URL", ""),
                    api_key=os.environ.get("LIGHTRAG_PERF_API_KEY", ""),
                    confirmed=args.confirm_isolated_target,
                    usage_targets=json.loads(args.usage_targets.read_text()),
                )
            )
        payload = json.dumps(report, ensure_ascii=False, indent=2) + "\n"
        if args.output:
            descriptor = os.open(
                args.output, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600
            )
            with os.fdopen(descriptor, "w") as output:
                output.write(payload)
        print(payload)
        return 0 if report.get("slo_met", report.get("ready", False)) else 2
    except Exception as error:
        print(
            type(error).__name__
            + ": performance check refused; inputs or target require review"
        )
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
