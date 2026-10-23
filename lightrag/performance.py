"""Opt-in, bounded per-instance query observations; never an authority cache."""

from __future__ import annotations

import asyncio
from contextlib import contextmanager
from contextvars import ContextVar
from functools import wraps
import math
import inspect
import threading
import time
from uuid import uuid4


STAGES = frozenset(
    {
        "query_total",
        "first_response",
        "keywords",
        "retrieval",
        "retrieval_total",
        "vector_backend",
        "embedding_total",
        "embedding_prepare",
        "embedding_input",
        "llm_prepare",
        "vector",
        "graph",
        "chunk",
        "kv",
        "rerank",
        "context",
        "cache_read",
        "cache_write",
        "queue_query",
        "queue_keyword",
        "queue_extract",
        "queue_vlm",
        "queue_embedding",
        "queue_rerank",
        "model_query",
        "model_keyword",
        "model_extract",
        "model_vlm",
        "model_embedding",
        "model_rerank",
        "provider_open",
        "provider_stream",
        "provider_first_token",
        "pg_pool",
        "coordination",
        "release",
        "http_total",
        "http_first_response",
        "event_loop_lag",
    }
)
KINDS = frozenset(
    {"full", "stream", "history", "history_stream", "retrieval", "http", "process"}
)
MODES = frozenset({"local", "global", "hybrid", "mix", "naive", "bypass", "other"})
OUTCOMES = frozenset({"ok", "error", "cancelled", "closed"})
ROLES = frozenset(
    {"query", "keyword", "extract", "vlm", "embedding", "rerank", "other"}
)
COUNTERS = frozenset(
    {
        "cache_hit",
        "cache_miss",
        "cache_bypass",
        "cache_skip",
        "cache_error",
        "cache_fill",
        "provider_calls",
        "provider_errors",
        "provider_rate_limits",
        "input_tokens",
        "output_tokens",
        "usage_unknown",
        "usage_pending",
        "usage_reports",
        "stream_close_error",
    }
)
CACHES = frozenset({"query", "keywords", "extract", "other"})
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
    2.5,
    3.0,
    5.0,
    10.0,
    20.0,
    30.0,
    60.0,
    120.0,
    300.0,
)
_trace = ContextVar("query_performance_trace", default=None)
_role = ContextVar("query_performance_role", default="other")
_queued = ContextVar("query_performance_queued", default=None)


class QueryMetrics:
    """Fixed label vocabulary and histogram buckets; no raw samples or inputs."""

    def __init__(self, *, enabled=False):
        self.enabled = enabled is True
        self._lock = threading.Lock()
        self._durations, self._counters = {}, {}
        self._usage_pending = {}
        self.active = 0
        self.observation_errors = 0
        self.observation_epoch = uuid4().hex
        self.started = time.monotonic()

    def observe(self, stage, seconds, *, kind="full", mode="other", outcome="ok"):
        if (
            not self.enabled
            or stage not in STAGES
            or kind not in KINDS
            or mode not in MODES
            or outcome not in OUTCOMES
        ):
            return
        value = float(seconds)
        if not math.isfinite(value) or value < 0:
            return
        with self._lock:
            row = self._durations.setdefault(
                (stage, kind, mode, outcome), [0, 0.0, 0.0, [0] * (len(BOUNDS) + 1)]
            )
            row[0] += 1
            row[1] += value
            row[2] = max(row[2], value)
            index = next(
                (i for i, bound in enumerate(BOUNDS) if value <= bound), len(BOUNDS)
            )
            row[3][index] += 1

    def increment(self, name, *, trace, category="other", amount=1):
        if not self.enabled or name not in COUNTERS or category not in ROLES | CACHES:
            return
        if type(amount) is not int or amount < 0:
            return
        with self._lock:
            key = (name, trace.kind, trace.mode, category)
            self._counters[key] = self._counters.get(key, 0) + amount

    def snapshot(self):
        with self._lock:
            counters = dict(self._counters)
            for (name, kind, mode, category), value in self._counters.items():
                if name == "provider_calls":
                    counters[("usage_unknown", kind, mode, category)] = max(
                        0,
                        value
                        - counters.get(("usage_reports", kind, mode, category), 0)
                        - self._usage_pending.get((kind, mode, category), 0),
                    )
                    counters[("usage_pending", kind, mode, category)] = (
                        self._usage_pending.get((kind, mode, category), 0)
                    )
            durations = []
            for (stage, kind, mode, outcome), row in sorted(self._durations.items()):
                cumulative = 0
                buckets = []
                for bound, count in zip((*BOUNDS, None), row[3]):
                    cumulative += count
                    buckets.append({"le": bound, "count": cumulative})
                durations.append(
                    dict(
                        stage=stage,
                        kind=kind,
                        mode=mode,
                        outcome=outcome,
                        count=row[0],
                        sum_seconds=row[1],
                        max_seconds=row[2],
                        buckets=buckets,
                    )
                )
            return {
                "observation_epoch": self.observation_epoch,
                "measurement_version": 2,
                "provider_call_scope": "SDK_dispatch_excluding_internal_transport_retries",
                "token_scope": "reported_usage_only_not_billing",
                "resources": resource_snapshot(),
                "observation_errors": self.observation_errors,
                "enabled": self.enabled,
                "scope": "instance_process",
                "uptime_seconds": time.monotonic() - self.started,
                "active_queries": self.active,
                "durations": durations,
                "counters": [
                    dict(name=k[0], kind=k[1], mode=k[2], category=k[3], count=v)
                    for k, v in sorted(counters.items())
                ],
            }

    async def sample_loop(self, *, interval=1.0):
        if not math.isfinite(interval) or interval <= 0:
            raise ValueError("Positive finite sample interval required")
        while self.enabled:
            expected = time.perf_counter() + interval
            await asyncio.sleep(interval)
            observe(
                self,
                "event_loop_lag",
                max(0.0, time.perf_counter() - expected),
                kind="process",
                mode="other",
            )


def resource_snapshot():
    import sys

    try:
        import resource

        usage = resource.getrusage(resource.RUSAGE_SELF)
    except (ImportError, OSError, AttributeError):
        return {"available": False}
    return {
        "available": True,
        "cpu_seconds": usage.ru_utime + usage.ru_stime,
        "rss_peak_bytes": usage.ru_maxrss * (1 if sys.platform == "darwin" else 1024),
    }


def observe(metrics, name, seconds, **labels):
    try:
        metrics.observe(name, seconds, **labels)
    except Exception:
        metrics.observation_errors += 1


def observe_elapsed(name, seconds):
    trace = current_trace()
    if trace:
        observe(trace.metrics, name, seconds, kind=trace.kind, mode=trace.mode)


class QueryTrace:
    def __init__(self, metrics, kind, mode, *, query=True):
        self.query = query
        self.metrics, self.kind, self.mode = metrics, kind, mode
        self.started = time.perf_counter()
        self.active = True
        if query:
            with metrics._lock:
                metrics.active += 1

    def finish(self, outcome):
        if self.active:
            if self.query:
                observe(
                    self.metrics,
                    "query_total",
                    time.perf_counter() - self.started,
                    kind=self.kind,
                    mode=self.mode,
                    outcome=outcome,
                )
                with self.metrics._lock:
                    self.metrics.active -= 1
            self.active = False


def current_trace():
    value = _trace.get()
    return value if value is not None and value.active else None


def count(name, *, category="other", amount=1):
    trace = current_trace()
    if trace:
        try:
            trace.metrics.increment(name, trace=trace, category=category, amount=amount)
        except Exception:
            trace.metrics.observation_errors += 1


@contextmanager
def stage(name):
    trace = current_trace()
    if trace is None:
        yield
        return
    started, outcome = time.perf_counter(), "ok"
    try:
        yield
    except BaseException as error:
        outcome = "cancelled" if isinstance(error, asyncio.CancelledError) else "error"
        raise
    finally:
        observe(
            trace.metrics,
            name,
            time.perf_counter() - started,
            kind=trace.kind,
            mode=trace.mode,
            outcome=outcome,
        )


def measured_stage(name):
    def decorate(function):
        @wraps(function)
        async def wrapped(*args, **kwargs):
            with stage(name):
                return await function(*args, **kwargs)

        return wrapped

    return decorate


class ObservedStream:
    """Forward close/iteration, retaining observations until actual completion."""

    def __init__(self, source, trace, *, request=False, role=None, owns_trace=False):
        self.owns_trace = owns_trace
        self.source, self.trace = source.__aiter__(), trace
        self.request, self.role = request, role
        self.started = trace.started if request else time.perf_counter()
        self.first = self.closed = self.source_closed = self.failed = False
        self.lock = asyncio.Lock()
        from lightrag.distributed.release.query import _streams

        bag = _streams.get()
        if bag is not None:
            bag.add(self)

    def __aiter__(self):
        return self

    def done(self, outcome):
        if self.closed:
            return
        self.closed = True
        if self.request:
            self.trace.finish(outcome)
        else:
            observe(
                self.trace.metrics,
                "provider_stream",
                time.perf_counter() - self.started,
                kind=self.trace.kind,
                mode=self.trace.mode,
                outcome=outcome,
            )

        if self.owns_trace:
            self.trace.finish(outcome)

    async def __anext__(self):
        async with self.lock:
            if self.closed or self.failed:
                raise StopAsyncIteration
            token = _trace.set(self.trace)
            role_token = _role.set(self.role or _role.get())
            try:
                value = await self.source.__anext__()
                if not self.first:
                    self.first = True
                    observe(
                        self.trace.metrics,
                        "first_response" if self.request else "provider_first_token",
                        time.perf_counter() - self.started,
                        kind=self.trace.kind,
                        mode=self.trace.mode,
                    )
                return value
            except StopAsyncIteration:
                self.done("ok")
                raise
            except asyncio.CancelledError:
                # In-flight provider reads may be shielded by the release owner.
                # Its normal close path, not a cancelled waiter, ends ownership.
                raise
            except BaseException:
                self.failed = True
                raise
            finally:
                _role.reset(role_token)
                _trace.reset(token)

    async def aclose(self):
        async with self.lock:
            if self.source_closed:
                return
            token = _trace.set(self.trace)
            role_token = _role.set(self.role or _role.get())
            try:
                close = getattr(self.source, "aclose", None)
                if close:
                    await close()
            except BaseException:
                count("stream_close_error")
                raise
            else:
                self.source_closed = True
                self.done("error" if self.failed else "closed")
            finally:
                _role.reset(role_token)
                _trace.reset(token)


def _wrap_result(result, trace):
    if hasattr(result, "__aiter__"):
        return ObservedStream(result, trace, request=True)
    if isinstance(result, dict):
        llm = result.get("llm_response")
        if isinstance(llm, dict) and hasattr(llm.get("response_iterator"), "__aiter__"):
            return result | {
                "llm_response": llm
                | {
                    "response_iterator": ObservedStream(
                        llm["response_iterator"], trace, request=True
                    )
                }
            }
    trace.finish("ok")
    return result


def measured_query(function):
    parameter = inspect.signature(function).parameters.get("param")
    default = (
        parameter.default
        if parameter is not None and parameter.default is not inspect.Parameter.empty
        else None
    )

    @wraps(function)
    async def wrapped(self, *args, **kwargs):
        metrics = getattr(self, "query_metrics", None)
        parent = current_trace()
        if (
            not isinstance(metrics, QueryMetrics)
            or not metrics.enabled
            or (parent is not None and parent.metrics is metrics)
        ):
            return await function(self, *args, **kwargs)
        param = kwargs.get("param", args[1] if len(args) > 1 else default)
        mode = getattr(param, "mode", "other")
        mode = mode if isinstance(mode, str) else "other"
        history = bool(getattr(param, "conversation_history", None))
        streaming = bool(getattr(param, "stream", False))
        kind = (
            "retrieval"
            if function.__name__ == "aquery_data"
            else (
                "history_stream"
                if history and streaming
                else "history"
                if history
                else "stream"
                if streaming
                else "full"
            )
        )
        trace = QueryTrace(metrics, kind, mode if mode in MODES else "other")
        token = _trace.set(trace)
        try:
            return _wrap_result(await function(self, *args, **kwargs), trace)
        except BaseException as error:
            trace.finish(
                "cancelled" if isinstance(error, asyncio.CancelledError) else "error"
            )
            raise
        finally:
            _trace.reset(token)

    return wrapped


def queued_model(raw, *, role, limit_factory, metrics=None):
    @wraps(raw)
    async def execute(*args, **kwargs):
        trace = current_trace()
        own = trace is None and metrics is not None and metrics.enabled
        if own:
            trace = QueryTrace(metrics, "process", "other", query=False)
        if trace is None:
            return await raw(*args, **kwargs)
        trace_token = _trace.set(trace)
        queued = _queued.get()
        if queued is not None:
            observe(
                trace.metrics,
                "queue_" + role,
                time.perf_counter() - queued,
                kind=trace.kind,
                mode=trace.mode,
            )
        token = _role.set(role)
        try:
            with stage("model_" + role):
                result = await raw(*args, **kwargs)
            if hasattr(result, "__aiter__"):
                return ObservedStream(result, trace, role=role, owns_trace=own)
            if own:
                trace.finish("ok")
            return result
        except BaseException as error:
            if own:
                trace.finish(
                    "cancelled"
                    if isinstance(error, asyncio.CancelledError)
                    else "error"
                )
            raise
        finally:
            _role.reset(token)
            _trace.reset(trace_token)

    limited = limit_factory(execute)

    @wraps(limited)
    async def enqueue(*args, **kwargs):
        token = _queued.set(time.perf_counter())
        try:
            if role == "embedding":
                with stage("embedding_total"):
                    return await limited(*args, **kwargs)
            return await limited(*args, **kwargs)
        finally:
            _queued.reset(token)

    return enqueue


def record_usage(usage, *, category=None, kind="llm", trace=None):
    """Count complete provider totals only, never estimated or partial tokens."""
    category = category or _role.get()
    trace = trace or current_trace()
    values = _usage_values(usage, kind)
    if values is None:
        return False
    if trace:
        with trace.metrics._lock:
            _record_usage_locked(trace, category, values)
    return True


def _usage_values(usage, kind):
    prompt = getattr(usage, "prompt_tokens", None)
    completion = 0 if kind == "embedding" else getattr(usage, "completion_tokens", None)
    total = getattr(usage, "total_tokens", None)
    if (
        any(type(v) is not int or v < 0 for v in (prompt, completion, total))
        or prompt + completion != total
    ):
        return None
    return prompt, completion


def _record_usage_locked(trace, category, values):
    for name, amount in (
        ("input_tokens", values[0]),
        ("output_tokens", values[1]),
        ("usage_reports", 1),
    ):
        key = (name, trace.kind, trace.mode, category)
        counters = trace.metrics._counters
        counters[key] = counters.get(key, 0) + amount


class _ProviderUsage:
    def __init__(self, trace, category, kind):
        self.trace, self.category, self.kind = trace, category, kind
        self.key = (trace.kind, trace.mode, category)
        self.done = False
        with trace.metrics._lock:
            key = ("provider_calls", *self.key)
            trace.metrics._counters[key] = trace.metrics._counters.get(key, 0) + 1
            pending = trace.metrics._usage_pending
            pending[self.key] = pending.get(self.key, 0) + 1

    def finish(self, usage=None):
        if self.done:
            return
        self.done = True
        values = _usage_values(usage, self.kind)
        with self.trace.metrics._lock:
            self.trace.metrics._usage_pending[self.key] -= 1
            if values is not None:
                _record_usage_locked(self.trace, self.category, values)


class StreamUsage:
    """Accept complete terminal totals once; conflicting totals remain unknown."""

    def __init__(self):
        self.value = None
        self.seen = False
        self.conflict = False

    def accept(self, chunk):
        usage = getattr(chunk, "usage", None)
        choices = getattr(chunk, "choices", None)
        if usage is None or not isinstance(choices, (tuple, list)):
            return
        if choices and any(getattr(c, "finish_reason", None) is None for c in choices):
            return
        if self.seen and _usage_values(self.value, "llm") != _usage_values(
            usage, "llm"
        ):
            self.conflict = True
        self.seen = True
        self.value = None if self.conflict else usage


class _UsageStream:
    """Settle one dispatch at EOF/close, including an unconsumed stream."""

    def __init__(self, source, usage):
        self.source, self.usage = source, usage
        self.iterator = source.__aiter__()
        self.final_usage = StreamUsage()

    def __aiter__(self):
        return self

    async def __anext__(self):
        try:
            chunk = await self.iterator.__anext__()
        except BaseException:
            self.usage.finish(self.final_usage.value)
            raise
        self.final_usage.accept(chunk)
        return chunk

    async def aclose(self):
        try:
            close = getattr(self.source, "aclose", None) or getattr(
                self.source, "close", None
            )
            if close:
                await close()
        finally:
            self.usage.finish(self.final_usage.value)

    close = aclose


async def provider_call(invoke, *, kind):
    trace = current_trace()
    if trace is None:
        return await invoke()
    category = "embedding" if kind == "embedding" else _role.get()
    usage = _ProviderUsage(trace, category, kind)
    try:
        with stage("provider_open"):
            result = await invoke()
    except BaseException as error:
        usage.finish()
        count("provider_errors", category=category)
        if getattr(error, "status_code", None) == 429:
            count("provider_rate_limits", category=category)
        raise
    if hasattr(result, "__aiter__"):
        return _UsageStream(result, usage)
    usage.finish(getattr(result, "usage", None))
    return result


def observe_storage(storage, category):
    """Wrap only existing read methods; do not change write or permit ordering."""
    import inspect

    for name in (
        "query",
        "get_by_id",
        "get_by_ids",
        "get_node",
        "get_nodes_batch",
        "get_edge",
        "get_edges_batch",
        "node_degree",
        "node_degrees_batch",
        "edge_degree",
        "edge_degrees_batch",
        "get_node_edges",
        "get_nodes_edges_batch",
    ):
        function = getattr(storage, name, None)
        if function is not None and inspect.iscoroutinefunction(function):
            setattr(storage, name, measured_stage(category)(function))


def measured_context(name):
    from contextlib import asynccontextmanager

    def decorate(factory):
        @wraps(factory)
        @asynccontextmanager
        async def wrapped(*args, **kwargs):
            with stage(name):
                async with factory(*args, **kwargs) as value:
                    yield value

        return wrapped

    return decorate


class QueryMetricsMiddleware:
    """Time ASGI responses without reading bodies, changing auth or buffering."""

    def __init__(self, app, *, metrics):
        self.app, self.metrics = app, metrics

    async def __call__(self, scope, receive, send):
        from lightrag.api.route_paths import get_route_path

        if scope["type"] != "http" or get_route_path(scope) not in {
            "/query",
            "/query/stream",
            "/query/data",
            "/api/chat",
            "/api/generate",
        }:
            return await self.app(scope, receive, send)
        begin = time.perf_counter()
        first, outcome = False, "ok"

        async def forward(message):
            nonlocal first, outcome
            if message["type"] == "http.response.start" and message["status"] >= 400:
                outcome = "error"
            if (
                message["type"] == "http.response.body"
                and message.get("body")
                and not first
            ):
                first = True
                observe(
                    self.metrics,
                    "http_first_response",
                    time.perf_counter() - begin,
                    kind="http",
                    mode="other",
                )
            await send(message)

        try:
            await self.app(scope, receive, forward)
        except BaseException as error:
            outcome = (
                "cancelled" if isinstance(error, asyncio.CancelledError) else "error"
            )
            raise
        finally:
            observe(
                self.metrics,
                "http_total",
                time.perf_counter() - begin,
                kind="http",
                mode="other",
                outcome=outcome,
            )
