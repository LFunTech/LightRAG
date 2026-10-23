import asyncio
import copy

import pytest

from lightrag.distributed.release.usage import compact_usage
from lightrag.performance import QueryMetrics


def limits():
    return dict(
        poll_seconds=0.001,
        sample_timeout_seconds=0.2,
        transition_timeout_seconds=0.2,
        max_samples=3000,
        max_evidence_bytes=4 * 1024**2,
    )


def member(slot, i, disposition="live"):
    return dict(
        uid=slot + str(i),
        slot=slot,
        config_hash=("b" if slot == "blue" else "c") * 64,
        profile_hash="a" * 64,
        digest="sha256:" + ("d" if slot == "blue" else "e") * 64,
        state=disposition,
        usage=compact_usage(QueryMetrics(enabled=True)),
    )


class Collector:
    def __init__(self):
        self.frame = dict(
            phase="IDLE", epoch=1, members=[member("blue", i) for i in range(4)]
        )

    async def usage(self, settings):
        return copy.deepcopy(self.frame)


def settings():
    from tests.performance.test_rehearsal import plan

    return plan()["release"]


async def guard(tmp_path):
    from scripts.performance.dynamic_usage import DynamicUsage

    c, stop = Collector(), asyncio.Event()
    watcher = DynamicUsage(c, settings(), limits(), tmp_path, stop)
    await watcher.start()
    return c, watcher, stop


def join(c):
    c.frame.update(phase="DRAINING")
    c.frame["members"] += [member("green", i) for i in range(4)]


async def test_dynamic_four_eight_four_keeps_old_final_totals_after_disappearance(
    tmp_path,
):
    c, watcher, stop = await guard(tmp_path)
    join(c)
    await watcher.sample()
    c.frame.update(phase="RETIRING", epoch=2)
    old = c.frame["members"][0]
    old["usage"]["counts"]["query"] = [1, 0, 0, 1, 0, 0, 0, 0]
    await watcher.sample()
    for m in c.frame["members"][:4]:
        m["state"] = "final"
        m["usage"]["counts"]["query"] = [1, 1, 0, 0, 7, 3, 0, 0]
    await watcher.before_scale()
    assert len(list(tmp_path.glob("usage-final-*.json"))) == 4
    c.frame.update(phase="COMPLETED", members=c.frame["members"][4:])
    await watcher.finish(success=True)
    result = watcher.report()
    assert result["verified"] and not stop.is_set()
    assert result["counts"]["query"][:6] == [4, 4, 0, 0, 28, 12]
    assert result["retired_instances"] == 4
    assert result["unresolved_instances"] == []


@pytest.mark.parametrize(
    "fault",
    [
        "missing",
        "replacement",
        "epoch",
        "unknown",
        "reset",
        "wrong_config",
        "duplicate",
    ],
)
async def test_membership_or_usage_fault_sets_shared_stop_and_refuses_scale(
    tmp_path, fault
):
    c, watcher, stop = await guard(tmp_path)
    join(c)
    await watcher.sample()
    m = c.frame["members"][0]
    if fault == "missing":
        c.frame["members"].pop(0)
    elif fault == "replacement":
        m["uid"] = "replacement"
    elif fault == "epoch":
        m["usage"]["observation_epoch"] = "replacement"
    elif fault == "unknown":
        m["usage"]["counts"]["query"] = [1, 0, 1, 0, 0, 0, 1, 0]
    elif fault == "reset":
        m["usage"]["uptime_seconds"] = -1
    elif fault == "wrong_config":
        m["config_hash"] = "f" * 64
    else:
        c.frame["members"].append(copy.deepcopy(m))
    with pytest.raises(ValueError):
        await watcher.sample()
    assert stop.is_set() and not watcher.report()["verified"]
    with pytest.raises(ValueError):
        await watcher.before_scale()


async def test_missing_final_is_bounded_and_never_zero_filled(tmp_path):
    c, watcher, stop = await guard(tmp_path)
    join(c)
    await watcher.sample()
    watcher.limits["transition_timeout_seconds"] = 0.01
    c.frame.update(phase="RETIRING", epoch=2)
    for m in c.frame["members"][:4]:
        m["state"] = "closing"
        m.pop("usage")
    with pytest.raises(ValueError):
        await watcher.before_scale()
    assert stop.is_set()
    assert watcher.report()["retired_instances"] == 0
    assert len(watcher.report()["unresolved_instances"]) == 4


async def test_final_evidence_write_failure_stops_before_marking_retired(
    monkeypatch, tmp_path
):
    from scripts.performance import dynamic_usage as module

    c, watcher, stop = await guard(tmp_path)
    join(c)
    await watcher.sample()
    c.frame.update(phase="RETIRING", epoch=2)
    for m in c.frame["members"][:4]:
        m["state"] = "final"

    def fail(*args):
        raise OSError("disk unavailable")

    monkeypatch.setattr(module, "save_json", fail)
    with pytest.raises(OSError):
        await watcher.sample()
    assert stop.is_set() and watcher.report()["retired_instances"] == 0


async def test_joining_member_cannot_be_silently_replaced_before_first_usage(tmp_path):
    c, watcher, stop = await guard(tmp_path)
    c.frame.update(phase="PREPARED")
    m = member("green", 0, "joining")
    m.pop("usage")
    c.frame["members"].append(m)
    await watcher.sample()
    m["uid"] = "another-green"
    with pytest.raises(ValueError):
        await watcher.sample()
    assert stop.is_set()


async def test_closing_keeps_last_known_totals_and_original_process_epoch(tmp_path):
    c, watcher, stop = await guard(tmp_path)
    join(c)
    m = c.frame["members"][0]
    m["usage"]["counts"]["query"] = [1, 1, 0, 0, 7, 3, 0, 0]
    await watcher.sample()
    original = copy.deepcopy(m["usage"])
    c.frame.update(phase="RETIRING", epoch=2)
    m["state"] = "closing"
    m.pop("usage")
    await watcher.sample()
    assert watcher.report()["counts"]["query"][:6] == [1, 1, 0, 0, 7, 3]
    m.update(state="final", usage=dict(original, observation_epoch="different-process"))
    with pytest.raises(ValueError):
        await watcher.sample()
    assert stop.is_set()


async def test_private_unknown_fields_never_enter_final_evidence(tmp_path):
    c, watcher, stop = await guard(tmp_path)
    join(c)
    await watcher.sample()
    c.frame.update(phase="RETIRING", epoch=2)
    c.frame["members"][0].update(state="final", prompt="not-allowed")
    with pytest.raises(ValueError):
        await watcher.sample()
    assert stop.is_set() and not list(tmp_path.glob("usage-final-*.json"))


async def test_sampling_deadline_stops_work_before_read_cleanup_finishes(tmp_path):
    c, watcher, stop = await guard(tmp_path)
    cleanup, finish = asyncio.Event(), asyncio.Event()

    async def slow(_):
        try:
            await asyncio.Event().wait()
        finally:
            cleanup.set()
            await finish.wait()

    c.usage = slow
    watcher.limits["sample_timeout_seconds"] = 0.005
    task = asyncio.create_task(watcher.sample())
    await cleanup.wait()
    try:
        assert stop.is_set() and not task.done()
    finally:
        finish.set()
        await asyncio.gather(task, return_exceptions=True)


async def test_final_files_share_total_evidence_budget(tmp_path):
    import json

    c, watcher, stop = await guard(tmp_path)
    join(c)
    await watcher.sample()
    c.frame.update(phase="RETIRING", epoch=2)
    for row in c.frame["members"][:4]:
        row["state"] = "final"
    journal_size = len(
        (
            json.dumps(
                dict(
                    phase="RETIRING", epoch=2, initial=False, members=c.frame["members"]
                ),
                sort_keys=True,
                separators=(",", ":"),
            )
            + "\n"
        ).encode()
    )
    watcher.limits["max_evidence_bytes"] = watcher.bytes + journal_size + 1
    with pytest.raises(ValueError):
        await watcher.before_scale()
    assert stop.is_set() and not watcher.verified


async def test_unknown_retains_actual_counts_and_failed_sample_evidence(tmp_path):
    import json

    c, watcher, stop = await guard(tmp_path)
    join(c)
    await watcher.sample()
    c.frame["members"][0]["usage"]["counts"]["query"] = [1, 0, 1, 0, 0, 0, 1, 0]
    with pytest.raises(ValueError):
        await watcher.sample()
    assert stop.is_set() and watcher.report()["counts"]["query"][2] == 1
    last = json.loads(
        (tmp_path / "usage-observations.jsonl").read_text().splitlines()[-1]
    )
    assert last["members"][0]["usage"]["counts"]["query"][2] == 1


@pytest.mark.parametrize("target", ["journal", "final"])
async def test_slow_observer_fsync_keeps_stop_and_tail_live_and_joins_cancel(
    tmp_path, monkeypatch, target
):
    import threading
    import httpx
    from scripts.performance import dynamic_usage as module
    from scripts.performance.http import BoundedClient, NewWorkStopped

    c, watcher, stop = await guard(tmp_path)
    join(c)
    await watcher.sample()
    if target == "final":
        c.frame.update(phase="RETIRING", epoch=2)
        for row in c.frame["members"][:4]:
            row["state"] = "final"
    entered, proceed = threading.Event(), threading.Event()
    finish_tail = asyncio.Event()
    fsync = module.os.fsync

    def slow(fd):
        entered.set()
        proceed.wait(0.3)
        fsync(fd)

    async def tail():
        await finish_tail.wait()
        return "naturally consumed"

    monkeypatch.setattr(module.os, "fsync", slow)
    task = asyncio.create_task(watcher.sample())
    tail_task = asyncio.create_task(tail())
    try:
        for _ in range(100):
            await asyncio.sleep(0.001)
            if entered.is_set():
                break
        assert entered.is_set() and not task.done()
        stop.set()
        finish_tail.set()
        async with BoundedClient(
            "https://fixture.invalid",
            {"reader": "synthetic"},
            timeout=1,
            max_bytes=1000,
            max_calls=10,
            stop_event=stop,
            transport=httpx.MockTransport(lambda _: pytest.fail("new write sent")),
        ) as client:
            with pytest.raises(NewWorkStopped):
                await client.json("POST", "/graph/entity/create", actor="reader")
        assert await tail_task == "naturally consumed"
        task.cancel()
        await asyncio.sleep(0.001)
        task.cancel()
        await asyncio.sleep(0.001)
        assert not task.done() and watcher.report()["retired_instances"] == 0
    finally:
        finish_tail.set()
        proceed.set()
        await asyncio.gather(task, tail_task, return_exceptions=True)
    assert task.cancelled() and stop.is_set()
    with pytest.raises(ValueError):
        await watcher.before_scale()


@pytest.mark.parametrize("state", ["live", "final"])
async def test_unknown_stops_before_slow_failure_evidence_is_fsynced(
    tmp_path, monkeypatch, state
):
    import threading
    from scripts.performance import dynamic_usage as module

    c, watcher, stop = await guard(tmp_path)
    join(c)
    await watcher.sample()
    if state == "final":
        c.frame.update(phase="RETIRING", epoch=2)
        for row in c.frame["members"][:4]:
            row["state"] = "final"
    index = 3 if state == "final" else 7
    c.frame["members"][index]["usage"]["counts"]["query"] = [1, 0, 1, 0, 0, 0, 1, 0]
    entered, proceed = threading.Event(), threading.Event()
    fsync = module.os.fsync

    def slow(fd):
        entered.set()
        proceed.wait(0.3)
        fsync(fd)

    monkeypatch.setattr(module.os, "fsync", slow)
    task = asyncio.create_task(watcher.sample())
    try:
        for _ in range(100):
            await asyncio.sleep(0.001)
            if entered.is_set():
                break
        assert entered.is_set() and stop.is_set() and not task.done()
    finally:
        proceed.set()
        await asyncio.gather(task, return_exceptions=True)
    assert watcher.report()["error_class"] == "UsageUnknown"
