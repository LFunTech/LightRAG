"""Credential-free readiness, distinct from the public liveness endpoint."""

import asyncio


async def readiness(runtime, release):
    reason = None
    coordinator = runtime.coordinator
    witness = coordinator._witness_connection
    if not runtime.initialized or runtime.closed:
        reason = "RuntimeUnavailable"
    elif (
        coordinator._broken
        or coordinator._pool is None
        or witness is None
        or witness.is_closed()
    ):
        reason = "CoordinatorUnavailable"
    elif release.lifecycle.retiring:
        reason = "Retiring"
    elif release.controller_error or release.executor_error:
        reason = "ReleaseControllerUnavailable"
    else:
        try:
            probe = getattr(release, "probe", None)
            if probe is not None:
                await asyncio.wait_for(probe(), 1.5)
            active = release.scope_state["active_slot"] == release.config.slot
            if (
                active
                and release.scope_state["gate_open"]
                and (runtime.pipeline_task is None or runtime.pipeline_task.done())
            ):
                reason = "PollingUnavailable"
        except Exception:
            reason = "ReleaseUnavailable"
    return {"ready": False, "reason": reason} if reason else {"ready": True}
