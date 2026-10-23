import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from lightrag.performance import QueryMetrics, QueryTrace


def metrics():
    m = QueryMetrics(enabled=True)
    trace = QueryTrace(m, "stream", "mix", query=False)
    for name, value in [
        ("provider_calls", 1),
        ("usage_reports", 1),
        ("input_tokens", 7),
        ("output_tokens", 3),
    ]:
        m.increment(name, trace=trace, category="query", amount=value)
    return m


def release(path=None):
    from lightrag.distributed.release.runtime import ReleaseRuntime
    from lightrag.distributed.release.lifecycle import InstanceLifecycle

    r = ReleaseRuntime.__new__(ReleaseRuntime)
    r.config = SimpleNamespace(
        scope="isolated/run",
        instance_id="blue0",
        slot="blue",
        config_hash="b" * 64,
        profile_hash="a" * 64,
        usage_final_path=path,
    )
    r.rag = SimpleNamespace(query_metrics=metrics())
    r.lifecycle = InstanceLifecycle()
    r.controller_task = r.executor_task = None
    r.stop_event = asyncio.Event()
    r.registered = True
    r.store = SimpleNamespace(_call=AsyncMock())
    r.runtime = SimpleNamespace(close=AsyncMock())
    r.closed = False
    return r


def test_compact_usage_is_bounded_role_totals_without_body_or_histograms():
    from lightrag.distributed.release.usage import compact_usage, validate_usage

    m = metrics()
    m.observe("vector", 0.1)
    value = compact_usage(m)
    assert value["counts"]["query"][:6] == [1, 1, 0, 0, 7, 3]
    assert "durations" not in value
    validate_usage(None, value, quiescent=True)
    assert len(json.dumps(value)) < 1500


@pytest.mark.parametrize(
    "damage", ["pending", "unknown", "reset", "epoch", "partial", "observation"]
)
def test_compact_interval_rejects_unsettled_or_missing_evidence(damage):
    from lightrag.distributed.release.usage import compact_usage, validate_usage

    before = compact_usage(metrics())
    after = json.loads(json.dumps(before))
    if damage == "pending":
        after["counts"]["query"][0] += 1
        after["counts"]["query"][3] = 1
    elif damage == "unknown":
        after["counts"]["query"][0] += 1
        after["counts"]["query"][2] = 1
    elif damage == "reset":
        after["counts"] = {}
    elif damage == "epoch":
        after["observation_epoch"] = "different"
    elif damage == "partial":
        after["counts"]["query"].pop()
    else:
        after["observation_errors"] = 1
    with pytest.raises(ValueError):
        validate_usage(before, after, quiescent=True)


async def test_shutdown_captures_late_usage_after_finalizer_before_closed_ack(
    monkeypatch, tmp_path
):
    from lightrag.distributed.release import usage

    target = tmp_path / "termination"
    target.touch()
    r = release(str(target))
    monkeypatch.setattr("lightrag.distributed.pipeline._stop_polling", AsyncMock())
    order = []

    async def finalizer(*args):
        m = r.rag.query_metrics
        trace = QueryTrace(m, "history_stream", "mix", query=False)
        for name, value in [
            ("provider_calls", 1),
            ("usage_reports", 1),
            ("input_tokens", 2),
            ("output_tokens", 1),
        ]:
            m.increment(name, trace=trace, category="query", amount=value)
        order.append("late-usage")

    async def call(name, *args):
        if name == "close_instance":
            body = json.loads(target.read_text())
            assert body["usage"]["counts"]["query"][:6] == [2, 2, 0, 0, 9, 4]
            assert body["final"] is True and body["instance_id"] == "blue0"
            order.append("closed-ack")

    r.store._call = call
    await r.close_instance(finalizer, r.rag, (), {})
    assert r.closed and order == ["late-usage", "closed-ack"]
    assert target.stat().st_size <= usage.FINAL_MAX_BYTES <= 4096


async def test_disabled_export_has_no_file_dependency(monkeypatch):
    r = release()
    monkeypatch.setattr("lightrag.distributed.pipeline._stop_polling", AsyncMock())
    await r.close_instance(AsyncMock(), r.rag, (), {})
    assert r.closed


async def test_failed_export_never_acknowledges_closed(monkeypatch, tmp_path):
    r = release(str(tmp_path / "missing"))
    monkeypatch.setattr("lightrag.distributed.pipeline._stop_polling", AsyncMock())
    with pytest.raises(OSError):
        await r.close_instance(AsyncMock(), r.rag, (), {})
    assert not r.closed
    assert not any(c.args[0] == "close_instance" for c in r.store._call.call_args_list)


def test_config_only_accepts_explicit_native_termination_path(monkeypatch):
    from lightrag.distributed.release.config import ReleaseConfig

    env = dict(
        LIGHTRAG_RELEASE_ENABLED="true",
        LIGHTRAG_DISTRIBUTED_WRITES="true",
        WORKERS="1",
        LIGHTRAG_DEPLOYMENT_ID="isolated",
        WORKSPACE="run",
        LIGHTRAG_RELEASE_SLOT="blue",
        LIGHTRAG_RELEASE_INSTANCE_ID="blue0",
        LIGHTRAG_RELEASE_CONFIG_HASH="b" * 64,
        LIGHTRAG_RELEASE_PROFILE_HASH="a" * 64,
        LIGHTRAG_USAGE_FINAL_PATH="/dev/termination-log",
    )
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    assert ReleaseConfig.from_env().usage_final_path == "/dev/termination-log"
    monkeypatch.setenv("LIGHTRAG_USAGE_FINAL_PATH", "/arbitrary/file")
    with pytest.raises(ValueError):
        ReleaseConfig.from_env()
    monkeypatch.setenv("LIGHTRAG_RELEASE_ENABLED", "false")
    assert ReleaseConfig.from_env() is None


async def test_usage_status_retains_auth_and_does_not_expose_public_metrics():
    import httpx
    from fastapi import FastAPI
    from lightrag.api.release_commands import register_release_endpoints
    from lightrag.distributed.release.authorization import AuthorizationPolicy
    from lightrag.distributed.release.usage import FINAL_PATH

    r = release(FINAL_PATH)
    r.scope_state = dict(
        epoch=1, phase="PREPARED", active_slot="green", gate_open=False
    )
    r.refresh = AsyncMock()
    r.shutdown_hook = object()
    r.registry = SimpleNamespace(handlers={})
    r.controller_error = r.executor_error = None
    r.cache_skipped = 0
    r.runtime = SimpleNamespace(
        initialized=True,
        closed=False,
        pipeline_task=None,
        coordinator=SimpleNamespace(
            _broken=False,
            _pool=object(),
            _witness_connection=SimpleNamespace(is_closed=lambda: False),
        ),
    )
    policy = AuthorizationPolicy(
        scope=r.config.scope,
        api_key="synthetic",
        auth_handler=SimpleNamespace(
            accounts={}, secret="synthetic", algorithm="HS256"
        ),
    )

    async def authenticate(scope):
        return await policy.authenticate(
            {k.decode(): v.decode() for k, v in scope["headers"]}
        )

    bridge = SimpleNamespace(release=r, authenticate=authenticate, authorization=policy)
    app = FastAPI()
    register_release_endpoints(app, bridge)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app), base_url="http://local"
    ) as c:
        denied = await c.get("/release/status")
        assert denied.status_code == 401 and "query_usage" not in denied.json()
        public = await c.get("/ready")
        assert "query_usage" not in public.json()
        answer = await c.get("/release/status", headers={"X-API-Key": "synthetic"})
    assert answer.status_code == 200
    assert answer.json()["usage_final_supported"] is True
    assert answer.json()["query_usage"]["counts"]["query"][4:6] == [7, 3]
    assert "synthetic" not in str(answer.json())


async def test_export_io_does_not_stall_event_loop(monkeypatch):
    import threading
    from lightrag.distributed.release import usage

    r = release("synthetic")
    entered, proceed = threading.Event(), threading.Event()
    monkeypatch.setattr("lightrag.distributed.pipeline._stop_polling", AsyncMock())

    def slow(_):
        entered.set()
        proceed.wait(0.3)

    monkeypatch.setattr(usage, "write_final_summary", slow)
    task = asyncio.create_task(r.close_instance(AsyncMock(), r.rag, (), {}))
    for _ in range(100):
        await asyncio.sleep(0.001)
        if entered.is_set():
            break
    try:
        assert entered.is_set() and not task.done()
    finally:
        proceed.set()
        await task


async def test_cancelled_export_joins_io_without_sending_closed_ack(monkeypatch):
    import threading
    from lightrag.distributed.release import usage

    r = release("synthetic")
    entered, proceed = threading.Event(), threading.Event()
    monkeypatch.setattr("lightrag.distributed.pipeline._stop_polling", AsyncMock())

    def slow(_):
        entered.set()
        proceed.wait(0.3)

    monkeypatch.setattr(usage, "write_final_summary", slow)
    task = asyncio.create_task(r.close_instance(AsyncMock(), r.rag, (), {}))
    for _ in range(100):
        await asyncio.sleep(0.001)
        if entered.is_set():
            break
    try:
        task.cancel()
        await asyncio.sleep(0.001)
        task.cancel()
        await asyncio.sleep(0.001)
        assert not task.done()
    finally:
        proceed.set()
        await asyncio.gather(task, return_exceptions=True)
    assert task.cancelled() and not r.closed
    assert not any(c.args[0] == "close_instance" for c in r.store._call.call_args_list)


@pytest.mark.parametrize(
    "fault", ["symlink", "directory", "oversize", "zero_write", "fsync"]
)
def test_final_evidence_failures_never_silently_truncate_or_succeed(
    tmp_path, monkeypatch, fault
):
    from lightrag.distributed.release import usage

    target = tmp_path / "termination"
    target.write_text("original")
    r = release(str(target))
    r.lifecycle.begin_retire()
    if fault == "symlink":
        target = tmp_path / "link"
        target.symlink_to(tmp_path / "termination")
        r.config.usage_final_path = str(target)
    elif fault == "directory":
        r.config.usage_final_path = str(tmp_path)
    elif fault == "oversize":
        r.config.scope = "x" * 4000
    elif fault == "zero_write":
        monkeypatch.setattr(usage.os, "write", lambda *_: 0)
    else:

        def fail(_):
            raise OSError("disk unavailable")

        monkeypatch.setattr(usage.os, "fsync", fail)
    with pytest.raises((ValueError, OSError)):
        usage.write_final_summary(r)
    assert not r.closed
    if fault in {"symlink", "oversize"}:
        assert (tmp_path / "termination").read_text() == "original"
