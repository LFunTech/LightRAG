"""Read pinned Pods and existing closed acknowledgements; no control mutations."""

import asyncio
import json

from lightrag.distributed.release.usage import (
    FINAL_PATH,
    FINAL_MAX_BYTES,
    LIFETIMES,
    validate_usage,
)
from lightrag.distributed.release.store import json_value


async def joined_read(awaitable):
    """Keep native thread-backed reads alive until their bounded I/O closes."""
    task = asyncio.ensure_future(awaitable)
    cancelled = False
    while not task.done():
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            cancelled = True
    if cancelled:
        # Retrieve errors without letting a late read revive the stopped run.
        if not task.cancelled():
            task.exception()
        raise asyncio.CancelledError()
    return task.result()


def _pin(control, settings, pod, slot, *, joining=False):
    containers = pod["spec"]["containers"]
    statuses = pod.get("status", {}).get("containerStatuses", [])
    digest = settings[slot + "_digest"]
    if (
        len(containers) != 1
        or containers[0]["name"] != "lightrag"
        or containers[0]["image"] != control.transport.image + "@" + digest
    ):
        raise ValueError("Usage Pod image differs")
    container = containers[0]
    env = {e["name"]: e.get("value") for e in container.get("env", [])}
    if (
        env.get("LIGHTRAG_USAGE_FINAL_PATH") != FINAL_PATH
        or container.get("terminationMessagePath") != FINAL_PATH
        or container.get("terminationMessagePolicy") != "File"
    ):
        raise ValueError("Pinned final usage exporter required")
    if not statuses and joining:
        return None
    if len(statuses) != 1:
        raise ValueError("Usage runtime image evidence required")
    status = statuses[0]
    image = status.get("imageID", "")
    if not image and joining:
        return status
    if not (image.endswith("@" + digest) or image == "containerd://" + digest):
        raise ValueError("Usage actual image digest differs")
    if type(status.get("restartCount")) is not int or status["restartCount"] < 0:
        raise ValueError("Usage restart identity required")
    return status


def _closed_usage(settings, uid, slot, row, status):
    if (
        slot != "blue"
        or not row.get("retiring")
        or json_value(row["status"]) != {k: 0 for k in LIFETIMES}
    ):
        raise ValueError("Old closed acknowledgement is incomplete")
    if status["restartCount"] not in (0, 1):
        raise ValueError("Final process exit evidence was replaced")
    terminated = status.get("state", {}).get("terminated")
    if not terminated and status["restartCount"] == 1:
        # The closed DB row prevents the restarted process from registering.
        # Only the immediate previous successful process can carry its final.
        terminated = status.get("lastState", {}).get("terminated")
    if not terminated:
        return None
    if type(terminated.get("exitCode")) is not int or terminated["exitCode"] != 0:
        raise ValueError("Old usage process did not exit cleanly")
    message = terminated.get("message")
    if not isinstance(message, str) or not 0 < len(message.encode()) <= FINAL_MAX_BYTES:
        raise ValueError("Final usage termination evidence missing or truncated")
    value = json.loads(message)
    expected = dict(
        version=1,
        final=True,
        scope=settings["scope"],
        instance_id=uid,
        slot=slot,
        config_hash=settings[slot + "_config_hash"],
        profile_hash=settings["profile_hash"],
        lifecycle={k: 0 for k in LIFETIMES},
    )
    if set(value) != set(expected) | {"usage"} or any(
        value[k] != v for k, v in expected.items()
    ):
        raise ValueError("Final usage identity or drain evidence differs")
    validate_usage(None, value["usage"], quiescent=True, allow_unknown=True)
    return value["usage"]


async def collect_usage(control, settings):
    control.validate(settings)
    # A phase may advance while the parallel HTTP reads are in progress. Retry
    # only an actual forward transition, never a failure in a stable phase.
    for _ in range(8):
        state = await control.store.snapshot(settings["scope"])
        try:
            frame = await _collect_usage(control, settings, state)
        except Exception:
            latest = await control.store.snapshot(settings["scope"])
            if _forward(state, latest):
                continue
            raise
        latest = await control.store.snapshot(settings["scope"])
        if _authority(state) == _authority(latest):
            if latest.get("commands", {}).get("UNCERTAIN"):
                raise ValueError("Uncertain command stops usage rehearsal")
            return frame
        if not _forward(state, latest):
            raise ValueError("Usage control identity changed during collection")
    raise ValueError("Usage control never reached a coherent sample")


def _authority(state):
    return tuple(
        state.get(k)
        for k in (
            "scope",
            "owner",
            "epoch",
            "phase",
            "active_slot",
            "candidate_slot",
            "gate_open",
            "config_hash",
            "profile_hash",
            "identity",
        )
    )


def _forward(before, after):
    from scripts.performance.control import PHASES

    old = PHASES.index(before["phase"]) if before["phase"] in PHASES else -1
    new = PHASES.index(after["phase"]) if after["phase"] in PHASES else -1
    return (
        new > old
        and after["epoch"] in {before["epoch"], before["epoch"] + 1}
        and before.get("profile_hash") == after.get("profile_hash")
    )


async def _collect_usage(control, settings, state):
    phase = state["phase"]
    moved = phase in {"WRITER_SWITCHED", "RETIRING", "COMPLETED"}
    baseline = (
        state.get("owner") is None
        and state["epoch"] == settings["epoch"]
        and state["active_slot"] == "blue"
    )
    if not baseline and (
        state["epoch"] != settings["epoch"] + int(moved)
        or state["active_slot"] != ("green" if moved else "blue")
        or state.get("owner")
        != (None if phase == "COMPLETED" else settings["release_id"])
    ):
        raise ValueError("Usage owner/phase/epoch differs")
    pods = [
        (slot, p)
        for slot in ("blue", "green")
        for p in await joined_read(control.transport.pods(slot))
    ]
    if len(pods) > 8 or len({p["metadata"]["uid"] for _, p in pods}) != len(pods):
        raise ValueError("Unexpected usage Pod membership")
    async with control.store.connection() as db:
        rows = await db.fetch(
            "SELECT instance_id,slot,config_hash,profile_hash,closed,retiring,status "
            "FROM lightrag_release.instances WHERE scope=$1 AND instance_id=ANY($2::text[])",
            settings["scope"],
            [p["metadata"]["uid"] for _, p in pods],
        )
    registered = {r["instance_id"]: r for r in rows}
    if len(registered) != len(rows):
        raise ValueError("Duplicate usage registration")

    async def inspect(slot, pod):
        uid = pod["metadata"]["uid"]
        joining = not baseline and phase == "PREPARED" and slot == "green"
        status = _pin(control, settings, pod, slot, joining=joining)
        result = dict(
            uid=uid,
            slot=slot,
            config_hash=settings[slot + "_config_hash"],
            profile_hash=settings["profile_hash"],
            digest=settings[slot + "_digest"],
        )
        row = registered.get(uid)
        if row is not None and any(
            row[k] != result[k] for k in ("slot", "config_hash", "profile_hash")
        ):
            raise ValueError("Usage registration identity differs")
        if row is not None and row.get("closed"):
            if phase not in {"RETIRING", "COMPLETED"} or status is None:
                raise ValueError("Unexpected closed usage member")
            value = _closed_usage(settings, uid, slot, row, status)
            return result | (
                {"state": "closing"}
                if value is None
                else dict(state="final", usage=value)
            )
        if status is not None and status.get("restartCount", 0) != 0:
            raise ValueError("Live usage process was restarted")
        if status is not None and status.get("state", {}).get("terminated"):
            if status["state"]["terminated"].get("exitCode") != 0:
                raise ValueError("Usage process failed before closed acknowledgement")
            if (
                phase == "RETIRING"
                and slot == "blue"
                and row is not None
                and row["retiring"]
            ):
                return result | dict(state="closing")
            raise ValueError("Usage process exited unexpectedly")
        if joining and (
            row is None or status is None or status.get("ready") is not True
        ):
            return result | dict(state="joining")
        if row is None:
            raise ValueError("Usage instance is unregistered")
        try:
            local = await joined_read(control.transport.local_status(pod))
        except Exception:
            if phase == "RETIRING" and slot == "blue" and row["retiring"]:
                return result | dict(state="closing")
            raise
        if any(
            local.get(k) != v
            for k, v in dict(
                instance_id=uid,
                slot=slot,
                config_hash=result["config_hash"],
                profile_hash=result["profile_hash"],
                protocol_version=1,
                usage_final_supported=True,
            ).items()
        ):
            raise ValueError("Authenticated usage instance identity differs")
        if (
            not (phase == "RETIRING" and slot == "blue")
            and local.get("readiness", {}).get("ready") is not True
        ):
            raise ValueError("Usage participant is unexpectedly unready")
        validate_usage(None, local.get("query_usage"), allow_unknown=True)
        return result | dict(state="live", usage=local["query_usage"])

    tasks = [asyncio.create_task(inspect(slot, pod)) for slot, pod in pods]
    try:
        members = await asyncio.gather(*tasks)
    except BaseException:
        for task in tasks:
            task.cancel()
        joined = asyncio.gather(*tasks, return_exceptions=True)
        while not joined.done():
            try:
                await asyncio.shield(joined)
            except asyncio.CancelledError:
                continue
        joined.result()
        raise
    return dict(phase=phase, epoch=state["epoch"], members=members)
