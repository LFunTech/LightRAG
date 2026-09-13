"""Explicit mutation classification, independent background tickets and HTTP errors."""

from contextlib import AsyncExitStack
from contextvars import ContextVar
from functools import wraps

from fastapi import HTTPException
from fastapi.responses import JSONResponse
from lightrag.distributed import CoordinationBusyError
from lightrag.distributed.runtime import get_runtime

_scan_scope = ContextVar("distributed_scan_scope", default=None)


async def coordination_error_handler(request, error):
    return JSONResponse(
        status_code=409 if isinstance(error, CoordinationBusyError) else 503,
        content={"detail": {"error": type(error).__name__, "message": str(error)}},
    )


def http_operation(rag, *, exclusive=False, resource=None):
    """Guard the real handler, not an HTTP-method heuristic or preflight ticket."""

    def decorate(function):
        @wraps(function)
        async def guarded(*args, **kwargs):
            runtime = get_runtime(rag)
            if runtime is None:
                return await function(*args, **kwargs)
            refusal = None
            async with runtime.operation(function.__name__, exclusive=exclusive):
                async with AsyncExitStack() as stack:
                    if resource is not None:
                        await stack.enter_async_context(
                            runtime.lock(resource(kwargs), namespace="InputFiles")
                        )
                    try:
                        result = await function(*args, **kwargs)
                    except HTTPException as error:
                        if error.status_code >= 500:
                            raise
                        # Only confirmed business refusal is settled here. A
                        # swallowed storage failure still fails heartbeat below.
                        refusal = error
                    await runtime.coordinator.heartbeat(runtime.permit().operation)
            if refusal is not None:
                raise refusal
            return result

        return guarded

    return decorate


def background_operation(*, exclusive=False, scan=False):
    def decorate(function):
        @wraps(function)
        async def guarded(rag, *args, **kwargs):
            started = kwargs.pop("_distributed_started", None)
            runtime = get_runtime(rag)
            if runtime is None:
                if started is not None:
                    started.set()
                return await function(rag, *args, **kwargs)
            metadata = None
            if scan:
                from inspect import signature

                track_id = (
                    signature(function)
                    .bind(rag, *args, **kwargs)
                    .arguments.get("track_id")
                )
                if track_id:
                    metadata = {"scan_track_id": track_id, "scan_status": "running"}
            async with AsyncExitStack() as stack:
                await stack.enter_async_context(
                    runtime.operation(
                        function.__name__,
                        exclusive=exclusive,
                        detached=True,
                        metadata=metadata,
                    )
                )
                if started is not None:
                    started.set()
                token = _scan_scope.set(stack) if scan else None
                try:
                    result = await function(rag, *args, **kwargs)
                    if not scan or _scan_scope.get() is not None:
                        await runtime.coordinator.heartbeat(runtime.permit().operation)
                    return result
                finally:
                    if token is not None:
                        _scan_scope.reset(token)

        return guarded

    return decorate


async def finish_scan_classification(rag):
    """Release exclusive classification BEFORE starting ordinary claimed workers."""
    stack = _scan_scope.get()
    if get_runtime(rag) is not None and stack is not None:
        await stack.aclose()
        _scan_scope.set(None)


async def drive_pipeline(rag):
    """Internal background wakeups never reverse a user's durable cancellation."""
    if get_runtime(rag) is None:
        await rag.apipeline_process_enqueue_documents()
    else:
        from lightrag.distributed.pipeline import process

        await process(rag, resume=False)


async def drain_background_tasks(tasks):
    """Wait for all admitted work; caller cancellation never cancels writers."""
    import asyncio

    if not tasks:
        return
    outcomes = await asyncio.shield(
        asyncio.gather(*list(tasks), return_exceptions=True)
    )
    for outcome in outcomes:
        if isinstance(outcome, BaseException):
            raise outcome


async def start_background_task(rag, background_tasks, *, work, backstop_release):
    """Preserve admission failures after the existing starter has joined cleanup."""
    from lightrag.kg.shared_storage import start_reserved_background_task

    if get_runtime(rag) is None:
        return await start_reserved_background_task(
            background_tasks, work=work, backstop_release=backstop_release
        )
    startup_error = None
    cleanup_completed = False

    async def capture_failure(started):
        nonlocal startup_error
        try:
            return await work(started)
        except BaseException as error:
            startup_error = error
            raise

    async def confirmed_backstop():
        nonlocal cleanup_completed
        await backstop_release()
        cleanup_completed = True

    try:
        return await start_reserved_background_task(
            background_tasks, work=capture_failure, backstop_release=confirmed_backstop
        )
    except RuntimeError:
        # Cleanup failures take precedence over the child's admission error.
        # Only successful backstop completion proves this is the legacy
        # starter's wrapper, rather than a RuntimeError raised by cleanup.
        if cleanup_completed and startup_error is not None:
            raise startup_error
        raise
