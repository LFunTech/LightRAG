import asyncio

import httpx
import pytest


async def test_http_stop_refuses_new_work_but_keeps_sent_write_and_tail_reads():
    from scripts.performance.http import BoundedClient, NewWorkStopped

    stop, entered, finish = asyncio.Event(), asyncio.Event(), asyncio.Event()
    calls = []

    async def service(request):
        calls.append((request.method, request.url.path))
        if request.method == "POST":
            entered.set()
            await finish.wait()
        return httpx.Response(200, json={"status": "success"})

    async with BoundedClient(
        "https://fixture.invalid",
        {"reader": "synthetic"},
        timeout=1,
        max_bytes=1000,
        max_calls=10,
        transport=httpx.MockTransport(service),
        stop_event=stop,
    ) as c:
        sent = asyncio.create_task(c.json("POST", "/graph/entity/edit", actor="reader"))
        await entered.wait()
        stop.set()
        with pytest.raises(NewWorkStopped):
            await c.json("POST", "/graph/entity/create", actor="reader")
        assert not sent.done()
        finish.set()
        assert (await sent)[0] == 200
        assert (await c.json("GET", "/graphs", actor="reader", allow_after_stop=True))[
            0
        ] == 200
        with pytest.raises(NewWorkStopped):
            await c.json(
                "POST", "/graph/entity/create", actor="reader", allow_after_stop=True
            )
    assert calls == [("POST", "/graph/entity/edit"), ("GET", "/graphs")]


async def test_usage_fault_stops_mixed_writes_and_advance_but_drains_accepted_work(
    tmp_path,
):
    from scripts.performance.rehearsal import run_rehearsal
    from tests.performance.test_rehearsal import Control, plan, scenario, service

    closed = []

    class Fault(Control):
        async def usage(self, settings):
            frame = await super().usage(settings)
            if getattr(self, "write_inflight", False):
                frame["members"][0]["usage"]["counts"]["query"] = [
                    1,
                    0,
                    1,
                    0,
                    0,
                    0,
                    1,
                    0,
                ]
            return frame

    c, calls = Fault(), []
    original = service(c, calls)

    class NaturalTail(httpx.AsyncByteStream):
        async def __aiter__(self):
            c.long_active = True
            try:
                yield b'{"response":"first"}\n'
                await asyncio.sleep(0.07)
                yield b'{"response":"last"}\n'
            finally:
                closed.append("natural-tail-closed")
                c.long_active = False

    async def handler(request):
        if request.headers.get("x-performance-rehearsal") == "long-stream":
            return httpx.Response(
                200,
                headers={"content-type": "application/x-ndjson"},
                stream=NaturalTail(),
            )
        if request.url.path == "/graph/entity/create":
            c.write_inflight = True
            await asyncio.sleep(0.02)
            c.write_finished = True
        if request.url.path in {"/query", "/query/data", "/query/stream"}:
            await asyncio.sleep(0.03)
        return await original.handle_async_request(request)

    report = await run_rehearsal(
        plan(),
        scenario(),
        base_url="https://fixture.invalid",
        actors={"reader": "synthetic"},
        control=c,
        output_dir=tmp_path / "run",
        confirmed=True,
        confirm_writes=True,
        transport=httpx.MockTransport(handler),
    )
    assert not report["rehearsal_checks_passed"]
    assert report["usage"]["error_class"] == "UsageUnknown"
    assert c.phase == 0 and c.write_finished
    assert calls.count(("POST", "/graph/entity/create")) == 1
    assert not any(path == "/graph/entity/edit" for _, path in calls)
    assert not any(method == "DELETE" for method, _ in calls)
    assert report["write_summary"]["CONFIRMED"] == 2
    assert closed == ["natural-tail-closed"]
    assert report["long_stream"]["response_events"] == 2
    assert not report["long_stream"]["completed_after_transfer"]


async def test_dynamic_rehearsal_requires_explicit_observation_budgets_before_owner(
    tmp_path,
):
    from scripts.performance.rehearsal import run_rehearsal
    from tests.performance.test_rehearsal import plan, scenario, Control

    c = Control()
    p = plan()
    p["usage"] = dict(
        poll_seconds=None,
        sample_timeout_seconds=None,
        transition_timeout_seconds=None,
        max_samples=None,
        max_evidence_bytes=None,
    )
    with pytest.raises(ValueError):
        await run_rehearsal(
            p,
            scenario(),
            base_url="https://fixture.invalid",
            actors={"reader": "synthetic"},
            control=c,
            output_dir=tmp_path / "blocked",
            confirmed=True,
            confirm_writes=True,
        )
    assert c.count == 0 and not (tmp_path / "blocked").exists()


async def test_usage_fault_at_closed_gate_keeps_timed_out_write_uncertain_without_replay(
    tmp_path,
):
    from scripts.performance.rehearsal import run_rehearsal
    from tests.performance.test_rehearsal import Control, plan, scenario, service

    class Fault(Control):
        async def usage(self, settings):
            frame = await super().usage(settings)
            if self.queued:
                frame["members"][0]["usage"]["counts"]["query"] = [
                    1,
                    0,
                    1,
                    0,
                    0,
                    0,
                    1,
                    0,
                ]
            return frame

    c, calls = Fault(), []
    underlying = service(c, calls)

    class NaturalTail(httpx.AsyncByteStream):
        async def __aiter__(self):
            c.long_active = True
            try:
                yield b'{"response":"first"}\n'
                await asyncio.sleep(0.1)
                yield b'{"response":"last"}\n'
            finally:
                c.long_active = False

    async def handle(request):
        if request.headers.get("x-performance-rehearsal") == "long-stream":
            return httpx.Response(
                200,
                headers={"content-type": "application/x-ndjson"},
                stream=NaturalTail(),
            )
        if request.url.path == "/graph/entity/edit":
            calls.append(("POST", "/graph/entity/edit"))
            c.queued = True
            await asyncio.sleep(0.025)
            raise httpx.ReadTimeout("ACK unknown; do not replay")
        return await underlying.handle_async_request(request)

    result = await run_rehearsal(
        plan(),
        scenario(),
        base_url="https://fixture.invalid",
        actors={"reader": "synthetic"},
        control=c,
        output_dir=tmp_path / "run",
        confirmed=True,
        confirm_writes=True,
        transport=httpx.MockTransport(handle),
    )
    assert c.phase == 1 and not c.edited
    assert calls.count(("POST", "/graph/entity/edit")) == 1
    assert not any(method == "DELETE" for method, _ in calls)
    assert result["write_summary"]["UNCERTAIN"] == 1
    assert result["long_stream"]["response_events"] == 2
    assert result["usage"]["error_class"] == "UsageUnknown"
    assert not result["rehearsal_checks_passed"] and not result["load"]["slo_met"]
