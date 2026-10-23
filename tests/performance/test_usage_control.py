import copy
import json
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from lightrag.distributed.release.usage import compact_usage, final_summary
from lightrag.performance import QueryMetrics
from tests.performance.test_rehearsal import plan


async def test_cancelled_native_read_joins_underlying_thread_before_return():
    import asyncio
    import threading
    from scripts.performance.usage_control import joined_read

    entered, finish = threading.Event(), threading.Event()

    def slow():
        entered.set()
        finish.wait(0.3)

    task = asyncio.create_task(joined_read(asyncio.to_thread(slow)))
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
        finish.set()
        await asyncio.gather(task, return_exceptions=True)
    assert task.cancelled()


def fixture():
    from scripts.performance.control import OrchestratorControl
    from lightrag.distributed.release.models import ReleaseIdentity
    from lightrag.distributed.release.transport import ProductionTransport

    settings = plan()["release"]
    pods, statuses, rows, metrics = {}, {}, {}, {}
    for slot in ("blue", "green"):
        pods[slot] = []
        for i in range(4):
            uid = slot + str(i)
            m = metrics[uid] = QueryMetrics(enabled=True)
            digest = settings[slot + "_digest"]
            p = dict(
                metadata=dict(uid=uid, name=uid),
                spec=dict(
                    containers=[
                        dict(
                            name="lightrag",
                            image="registry/lightrag@" + digest,
                            env=[
                                dict(
                                    name="LIGHTRAG_USAGE_FINAL_PATH",
                                    value="/dev/termination-log",
                                )
                            ],
                            terminationMessagePath="/dev/termination-log",
                            terminationMessagePolicy="File",
                        )
                    ]
                ),
                status=dict(
                    containerStatuses=[
                        dict(
                            ready=True,
                            restartCount=0,
                            imageID="registry/lightrag@" + digest,
                            state=dict(running={}),
                        )
                    ]
                ),
            )
            pods[slot].append(p)
            statuses[uid] = dict(
                instance_id=uid,
                slot=slot,
                config_hash=settings[slot + "_config_hash"],
                profile_hash=settings["profile_hash"],
                usage_final_supported=True,
                protocol_version=1,
                query_usage=compact_usage(m),
                readiness=dict(ready=True),
            )
            rows[uid] = dict(
                instance_id=uid,
                slot=slot,
                config_hash=settings[slot + "_config_hash"],
                profile_hash=settings["profile_hash"],
                closed=False,
                retiring=False,
                status={
                    k: 0
                    for k in (
                        "http",
                        "queries",
                        "writers",
                        "waiters",
                        "backend",
                        "background",
                    )
                },
            )

    class Store:
        state = dict(phase="IDLE", epoch=1, active_slot="blue", owner=None)

        async def snapshot(self, scope):
            return dict(self.state, profile_hash=settings["profile_hash"])

        @asynccontextmanager
        async def connection(self):
            yield SimpleNamespace(fetch=AsyncMock(return_value=list(rows.values())))

    store = Store()
    t = ProductionTransport(
        config=SimpleNamespace(
            replicas=4,
            target={"namespace": settings["namespace"]},
            onboarding={
                k: settings[k]
                for k in ("namespace_uid", "kube_system_uid", "profile_hash")
            },
        ),
        kubernetes=None,
        cloud=None,
        store=store,
        image="registry/lightrag",
        environment={},
    )

    async def get_pods(slot):
        return copy.deepcopy(pods[slot])

    t.pods = get_pods
    t.local_status = AsyncMock(
        side_effect=lambda p: copy.deepcopy(statuses[p["metadata"]["uid"]])
    )
    identity = ReleaseIdentity(
        settings["scope"],
        settings["release_id"],
        "v1.0.0",
        "d" * 40,
        settings["green_digest"],
        settings["green_config_hash"],
    )
    c = OrchestratorControl(store, t, identity, expected_epoch=1)
    return c, pods, statuses, rows, metrics, store, settings


async def test_native_control_reads_live_compact_usage_from_pinned_members():
    c, pods, statuses, rows, metrics, store, settings = fixture()
    pods["green"] = []
    result = await c.usage(settings)
    assert len(result["members"]) == 4
    assert all(m["state"] == "live" for m in result["members"])


@pytest.mark.parametrize("fault", ["uid", "image", "export_off", "restart"])
async def test_control_rejects_identity_or_export_drift_before_accepting_usage(fault):
    c, pods, statuses, rows, metrics, store, settings = fixture()
    if fault == "uid":
        statuses["blue0"]["instance_id"] = "different"
    elif fault == "image":
        pods["blue"][0]["status"]["containerStatuses"][0]["imageID"] = "sha256:bad"
    elif fault == "export_off":
        pods["blue"][0]["spec"]["containers"][0]["env"] = []
    else:
        pods["blue"][0]["status"]["containerStatuses"][0]["restartCount"] = 1
    with pytest.raises(ValueError):
        await c.usage(settings)


@pytest.mark.parametrize("previous", [False, True])
async def test_closed_ack_exit_zero_and_native_final_message_prove_old_settlement(
    previous,
):
    c, pods, statuses, rows, metrics, store, settings = fixture()
    store.state.update(
        phase="RETIRING", epoch=2, active_slot="green", owner=settings["release_id"]
    )
    rows["blue0"].update(closed=True, retiring=True)
    from lightrag.distributed.release.lifecycle import InstanceLifecycle

    lifecycle = InstanceLifecycle()
    lifecycle.begin_retire()
    message = json.dumps(
        final_summary(
            SimpleNamespace(
                config=SimpleNamespace(
                    scope=settings["scope"],
                    slot="blue",
                    instance_id="blue0",
                    profile_hash="a" * 64,
                    config_hash="b" * 64,
                ),
                rag=SimpleNamespace(query_metrics=metrics["blue0"]),
                lifecycle=lifecycle,
            )
        )
    )
    cs = pods["blue"][0]["status"]["containerStatuses"][0]
    termination = dict(terminated=dict(exitCode=0, message=message))
    cs.update(ready=False, restartCount=int(previous))
    cs["lastState" if previous else "state"] = termination
    result = await c.usage(settings)
    old = next(m for m in result["members"] if m["uid"] == "blue0")
    assert old["state"] == "final"
    assert old["usage"]["observation_epoch"] == metrics["blue0"].observation_epoch


@pytest.mark.parametrize(
    "fault", ["bad_json", "nonzero_exit", "wrong_uid", "not_drained"]
)
async def test_bad_native_final_evidence_is_never_treated_as_zero(fault):
    c, pods, statuses, rows, metrics, store, settings = fixture()
    store.state.update(
        phase="RETIRING", epoch=2, active_slot="green", owner=settings["release_id"]
    )
    rows["blue0"].update(closed=True, retiring=True)
    message = dict(
        version=1,
        final=True,
        scope=settings["scope"],
        instance_id="blue0",
        slot="blue",
        config_hash="b" * 64,
        profile_hash="a" * 64,
        lifecycle=copy.deepcopy(rows["blue0"]["status"]),
        usage=compact_usage(metrics["blue0"]),
    )
    if fault == "wrong_uid":
        message["instance_id"] = "other"
    if fault == "not_drained":
        message["lifecycle"]["backend"] = 1
    cs = pods["blue"][0]["status"]["containerStatuses"][0]
    cs["state"] = dict(
        terminated=dict(
            exitCode=1 if fault == "nonzero_exit" else 0,
            message="truncated{" if fault == "bad_json" else json.dumps(message),
        )
    )
    with pytest.raises(ValueError):
        await c.usage(settings)


async def test_sampling_rechecks_phase_after_old_listener_closes_mid_read():
    c, pods, statuses, rows, metrics, store, settings = fixture()
    store.state.update(
        phase="WRITER_SWITCHED",
        epoch=2,
        active_slot="green",
        owner=settings["release_id"],
    )
    original = c.transport.local_status

    async def race(pod):
        if pod["metadata"]["uid"] == "blue0":
            store.state["phase"] = "RETIRING"
            rows["blue0"]["retiring"] = True
            raise OSError("listener closed")
        return await original(pod)

    c.transport.local_status = race
    frame = await c.usage(settings)
    assert frame["phase"] == "RETIRING"
    assert frame["members"][0]["state"] == "closing"


async def test_queue_count_change_does_not_invalidate_same_phase_sample():
    c, pods, statuses, rows, metrics, store, settings = fixture()
    original = c.transport.local_status

    async def queue(pod):
        store.state["tickets"] = store.state.get("tickets", 0) + 1
        return await original(pod)

    c.transport.local_status = queue
    assert len((await c.usage(settings))["members"]) == 8
