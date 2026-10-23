"""Real tool/control/export code with only HTTP, PG and K8s boundaries faked."""

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

from lightrag.distributed.release.lifecycle import InstanceLifecycle
from lightrag.distributed.release.usage import compact_usage, publish_final_summary
from lightrag.performance import QueryTrace
from scripts.performance.control import PHASES
from scripts.performance.rehearsal import run_rehearsal
from tests.performance.test_rehearsal import plan, scenario, service
from tests.performance.test_usage_control import fixture


async def test_native_dynamic_four_eight_four_exports_late_usage_before_scale(tmp_path):
    c, pods, statuses, rows, metrics, store, settings = fixture()
    green, pods["green"] = pods["green"], []
    store.state["candidate_slot"] = "green"
    output = tmp_path / "run"
    scaled = []

    class Backend:
        queued = edited = long_active = False

        @property
        def phase(self):
            return (
                PHASES.index(store.state["phase"])
                if store.state["phase"] in PHASES
                else 0
            )

    backend = Backend()

    async def begin(identity, expected_epoch):
        if store.state["owner"] is None:
            store.state.update(owner=identity.release_id, phase="PREPARED")
        return dict(store.state)

    async def transition(identity, *, expected_phase, next_phase, **kwargs):
        assert store.state["phase"] == expected_phase
        store.state["phase"] = next_phase
        if next_phase == "COMPLETED":
            store.state["owner"] = None

    async def transfer(identity, **kwargs):
        store.state.update(
            phase="WRITER_SWITCHED", epoch=2, active_slot="green", candidate_slot="blue"
        )

    async def prepare(*_):
        for pod in green:
            pods["green"].append(pod)
            await asyncio.sleep(0.002)

    async def local(pod):
        uid = pod["metadata"]["uid"]
        if rows[uid]["retiring"]:
            raise OSError("native listener already closed")
        return dict(
            statuses[uid],
            query_usage=compact_usage(metrics[uid]),
            lifecycle={"queries": int(uid.startswith("blue") and backend.long_active)},
        )

    async def verified(slot, **_):
        return [(p, await local(p)) for p in pods[slot]]

    async def retire(*_, **__):
        assert not backend.long_active
        for pod in pods["blue"]:
            rows[pod["metadata"]["uid"]]["retiring"] = True
        for pod in pods["blue"]:
            uid = pod["metadata"]["uid"]
            m = metrics[uid]
            trace = QueryTrace(m, "stream", "mix", query=False)
            for name, value in [
                ("provider_calls", 1),
                ("usage_reports", 1),
                ("input_tokens", 7),
                ("output_tokens", 3),
            ]:
                m.increment(name, trace=trace, category="query", amount=value)
            lifecycle = InstanceLifecycle()
            lifecycle.begin_retire()
            target = tmp_path / (uid + "-termination")
            target.touch()
            await publish_final_summary(
                SimpleNamespace(
                    config=SimpleNamespace(
                        scope=settings["scope"],
                        instance_id=uid,
                        slot="blue",
                        config_hash="b" * 64,
                        profile_hash="a" * 64,
                        usage_final_path=str(target),
                    ),
                    rag=SimpleNamespace(query_metrics=m),
                    lifecycle=lifecycle,
                )
            )
            rows[uid].update(retiring=True, closed=True)
            pod["status"]["containerStatuses"][0]["state"] = dict(
                terminated=dict(exitCode=0, message=target.read_text())
            )

    async def scale(*_):
        evidence = list(output.glob("usage-final-*.json"))
        assert len(evidence) == 4
        assert all(
            json.loads(p.read_text())["usage"]["counts"]["query"][4] == 7
            for p in evidence
        )
        assert (output / "usage-observations.jsonl").stat().st_size > 0
        scaled.append("after-final-fsync")
        pods["blue"] = []

    async def receipt(*_, **__):
        return (
            dict(
                id="22222222-2222-2222-2222-222222222222",
                state="SUCCEEDED" if backend.phase >= 3 else "QUEUED",
            )
            if backend.queued
            else None
        )

    store.begin, store.transition, store.transfer_writer = begin, transition, transfer
    store.retire_old_instances = retire
    store.capture_baseline = store.record_evidence = store.wait_for_drain = AsyncMock()
    c.transport.config.target["host"] = "fixture.invalid"
    c.transport.prepare = prepare
    c.transport.verify_candidate = AsyncMock(
        return_value=[p["metadata"]["uid"] for p in green]
    )
    c.transport.switch_read_route = AsyncMock()
    c.transport.verify_route = AsyncMock(return_value={"provider": "fake-boundary"})
    c.transport.local_status, c.transport.verified_pods = local, verified
    c.transport.wait_closed_and_scale = scale
    c.receipt = receipt
    report = await run_rehearsal(
        plan(),
        scenario(),
        base_url="https://fixture.invalid",
        actors={"reader": "synthetic"},
        control=c,
        output_dir=output,
        confirmed=True,
        confirm_writes=True,
        transport=service(backend, []),
    )
    assert report["rehearsal_checks_passed"] and report["usage"]["verified"]
    assert report["usage"]["counts"]["query"][:6] == [4, 4, 0, 0, 28, 12]
    frames = [
        json.loads(s)
        for s in (output / "usage-observations.jsonl").read_text().splitlines()
    ]
    assert len(frames[0]["members"]) == len(frames[-1]["members"]) == 4
    assert max(len(f["members"]) for f in frames) == 8
    assert any(4 < len(f["members"]) < 8 for f in frames)
    assert scaled == ["after-final-fsync"] and report["write_summary"] == {
        "CONFIRMED": 4
    }
    assert not report["production_accepted"] and not report["capacity_accepted"]
