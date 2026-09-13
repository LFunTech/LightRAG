"""Bounded claimed scheduling through the existing parse/analyze/process workers.

No distributed pipeline leader exists. Each process claims at most its local
batch capacity, before hydration/repair, and skips claimed pages. Polling is a
strict storage sweep, not a reliable-notification assumption.
See docs/design/DistributedPipelineContract.md for control and drain semantics.
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from lightrag import LightRAG
    from .runtime import DistributedRuntime
from datetime import datetime, timezone
from uuid import uuid4

from lightrag.base import CURSOR_START, CURSOR_END, DocStatus
from lightrag.utils_pipeline import doc_status_custom_chunk_patch
from .coordinator import (
    CoordinationError,
    CoordinationBusyError,
    OperationOwnershipError,
)
from .runtime import get_runtime


def _state(runtime: DistributedRuntime) -> dict[str, Any]:
    if not hasattr(runtime, "pipeline_lock"):
        runtime.pipeline_lock = asyncio.Lock()
        runtime.pipeline_status_lock = asyncio.Lock()
        runtime.pipeline_status = dict(
            busy=False, history_messages=[], docs=0, batchs=0, cur_batch=0
        )
        runtime.pipeline_stop = asyncio.Event()
        runtime.pipeline_task = None
        runtime.pipeline_error = None
    return runtime.pipeline_status


async def reset_requests(rag: LightRAG) -> None:
    """Persist target versions before reset; replay never resets a newer failure."""
    runtime = get_runtime(rag)
    control = runtime.coordinator.pipeline_control
    if not (await control.status())["pending_retries"]:
        return
    async with runtime.operation("manual_retry", exclusive=True) as operation:
        while request := await control.next_request(operation):
            request_id = request["request_id"]
            if request["state"] == "selecting":
                position = CURSOR_START
                while position is not CURSOR_END:
                    page = await rag.doc_status.get_docs_by_statuses_page(
                        [DocStatus.FAILED],
                        limit=64,
                        position=position,
                        strict=True,
                    )
                    rows = await rag.doc_status.get_full_docs_by_ids(
                        list(page.docs), strict=True
                    )
                    targets = {}
                    for doc_id, row in rows.items():
                        if (
                            row.status != DocStatus.FAILED
                            or doc_status_custom_chunk_patch(row) is not None
                        ):
                            continue
                        version = str(row.updated_at)
                        updated = datetime.fromisoformat(version.replace("Z", "+00:00"))
                        if updated.tzinfo is None:
                            updated = updated.replace(tzinfo=timezone.utc)
                        # Restart selection may rescan old pages, but neither a
                        # newly failed row nor a replacement version is selected.
                        if updated <= request["created_at"]:
                            targets[doc_id] = version
                    await control.add_targets(operation, request_id, targets)
                    position = page.next_position
                await control.finish_selection(operation, request_id)
            while targets := await control.targets(operation, request_id):
                for target in targets:
                    doc_id = target["doc_id"]
                    rows = await rag.doc_status.get_full_docs_by_ids(
                        [doc_id], strict=True
                    )
                    row = rows.get(doc_id)
                    if (
                        row is not None
                        and row.status == DocStatus.FAILED
                        and str(row.updated_at) == target["version"]
                        and doc_status_custom_chunk_patch(row) is None
                    ):
                        content = await rag.full_docs.get_by_id_strict(doc_id)
                        if content:
                            update, _, _ = rag._build_pending_reset_update(row, content)
                            await rag.doc_status.upsert({doc_id: update})
                    # ACK follows confirmed business reset. A crash between the
                    # two leaves a changed version, so replay only ACKs it.
                    await control.finish_target(operation, request_id, doc_id)
            await control.finish_request(operation, request_id)


async def process(rag: LightRAG, *, resume: bool = True) -> None:
    from lightrag.pipeline import _AUTO_RESUME_DOC_STATUSES

    runtime = get_runtime(rag)
    status = _state(runtime)
    if resume:
        await runtime.coordinator.pipeline_control.resume()
    # Local instance serialization protects _active_run_ctx, not the workspace.
    if runtime.pipeline_lock.locked():
        return
    async with runtime.pipeline_lock:
        control = runtime.coordinator.pipeline_control
        state = await control.status()
        if state["paused"]:
            return
        if state["pending_retries"]:
            # Nested ainsert may own a shared permit: let the independent poll
            # worker serve maintenance once that caller releases its permit.
            try:
                permit = runtime.permit()
            except OperationOwnershipError:
                pass
            else:
                if not permit.exclusive:
                    return
            await reset_requests(rag)
        position = CURSOR_START
        limit = min(
            64,
            max(1, rag.max_parallel_insert),
            max(1, rag.pipeline_scheduling_page_size or 64),
        )
        token = uuid4().hex
        status.update(
            busy=True,
            busy_owner={"token": token},
            cancellation_requested=False,
            cancellation_reason=None,
            cancellation_detail=None,
            cur_batch=0,
        )
        try:
            while position is not CURSOR_END:
                if not resume and runtime.pipeline_stop.is_set():
                    return
                async with runtime.operation("pipeline") as operation:
                    state = await control.status()
                    if state["paused"] or state["pending_retries"]:
                        return
                    runtime.pipeline_cancel_epoch = state["cancel_epoch"]
                    ids, _, position = await rag._fetch_scheduling_page(
                        _AUTO_RESUME_DOC_STATUSES,
                        position,
                        limit=limit,
                    )
                    claimed = []
                    for doc_id in ids:
                        if await runtime.coordinator.try_claim_document(
                            operation, doc_id
                        ):
                            claimed.append(doc_id)
                    # Strict re-read AFTER atomic ownership, never a PENDING
                    # snapshot borrowed from the scheduling query or feeder.
                    docs = (
                        await rag._hydrate_scheduling_page(
                            claimed, _AUTO_RESUME_DOC_STATUSES
                        )
                        if claimed
                        else {}
                    )
                    docs = (
                        await rag._validate_and_fix_document_consistency(
                            docs, status, runtime.pipeline_status_lock
                        )
                        if docs
                        else {}
                    )
                    if docs:
                        status.update(docs=len(docs), batchs=len(docs), cur_batch=0)
                        await runtime.coordinator.set_phase(operation, "processing")
                        await rag._run_pipeline_batch(
                            docs,
                            pipeline_status=status,
                            pipeline_status_lock=runtime.pipeline_status_lock,
                            ingress=None,
                            token=token,
                        )
                    # Guard cannot interpret swallowed worker exceptions as an
                    # ACK. Heartbeat checks the durable fence before release.
                    await runtime.coordinator.heartbeat(operation)
                    for doc_id in claimed:
                        await runtime.coordinator.release_claim(operation, doc_id)
                    if status.get("cancellation_requested"):
                        return
        finally:
            status["busy"] = False


async def start_polling(rag: LightRAG, *, interval: float = 1.0) -> None:
    from math import isfinite

    if not isfinite(interval) or interval <= 0:
        raise ValueError("Polling interval must be finite and positive")
    runtime = get_runtime(rag)
    if runtime is None:
        return
    _state(runtime)
    if runtime.closed:
        raise OperationOwnershipError("Create a new runtime after closure")
    if runtime.pipeline_task is not None and not runtime.pipeline_task.done():
        return
    runtime.pipeline_stop.clear()

    async def poll():
        while not runtime.pipeline_stop.is_set():
            try:
                await process(rag, resume=False)
                runtime.pipeline_error = None
            except CoordinationBusyError:
                runtime.pipeline_error = "CoordinationBusyError"
            except CoordinationError as error:
                runtime.pipeline_error = type(error).__name__
                # No mutation retry or local fallback after a durable fence.
                return
            except Exception:
                runtime.pipeline_error = "PipelineError"
                raise
            try:
                await asyncio.wait_for(runtime.pipeline_stop.wait(), interval)
            except asyncio.TimeoutError:
                pass

    # Background lifetime must never inherit a live/expired request permit.
    from contextvars import Context

    runtime.pipeline_task = Context().run(asyncio.create_task, poll())


async def stop_polling(rag: LightRAG) -> None:
    runtime = get_runtime(rag)
    if runtime is None or not hasattr(runtime, "pipeline_stop"):
        return
    runtime.pipeline_stop.set()
    if runtime.pipeline_task is not None:
        await asyncio.shield(runtime.pipeline_task)
        runtime.pipeline_task = None
