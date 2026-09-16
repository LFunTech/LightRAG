"""Actual scan HTTP startup must retain typed failures and join unadmitted work."""

import asyncio
import importlib
import sys
from contextlib import asynccontextmanager
from types import MethodType, SimpleNamespace

import httpx
import pytest
from fastapi import FastAPI

from lightrag.api.distributed import coordination_error_handler
from lightrag.distributed import (
    CoordinationBusyError,
    CoordinationError,
    CoordinationUnavailableError,
)
from lightrag.distributed.runtime import DistributedRuntime
from lightrag.pipeline import _PipelineMixin


@pytest.mark.parametrize("failure", ["busy", "unavailable", "cancel"])
async def test_scan_http_startup_failure_is_typed_and_joined(
    tmp_path, monkeypatch, failure
):
    monkeypatch.setattr(sys, "argv", ["test"])
    routes = importlib.import_module("lightrag.api.routers.document_routes")
    accepted = set()
    admission_entered = asyncio.Event()
    admission_joined = asyncio.Event()

    async def request_retry(request_id, *, target_cutoff_at=None):
        accepted.add(request_id)

    async def resume():
        pass

    @asynccontextmanager
    async def operation(*args, **kwargs):
        assert accepted
        assert kwargs["exclusive"] is True
        admission_entered.set()
        try:
            if failure == "cancel":
                await asyncio.Event().wait()
            raise (
                CoordinationBusyError("peer writer")
                if failure == "busy"
                else CoordinationUnavailableError("coordination unavailable")
            )
            yield  # pragma: no cover
        finally:
            admission_joined.set()

    coordinator = SimpleNamespace(
        operation=operation,
        pipeline_control=SimpleNamespace(request_retry=request_retry, resume=resume),
    )
    rag = SimpleNamespace(
        workspace="scan-start-unit",
        _distributed_runtime=DistributedRuntime(
            coordinator, workspace="scan-start-unit"
        ),
    )
    rag.apipeline_request_retry = MethodType(
        _PipelineMixin.apipeline_request_retry, rag
    )
    manager = routes.DocumentManager(str(tmp_path), workspace=rag.workspace)
    app = FastAPI()
    app.state.background_tasks = set()
    app.add_exception_handler(CoordinationError, coordination_error_handler)
    app.include_router(routes.create_document_routes(rag, manager, api_key="test-key"))
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app, raise_app_exceptions=False),
        base_url="http://test",
    ) as client:
        denied = await client.post("/documents/scan")
        assert denied.status_code in {401, 403}
        assert not accepted
        request = asyncio.create_task(
            client.post("/documents/scan", headers={"X-API-Key": "test-key"})
        )
        await asyncio.wait_for(admission_entered.wait(), 2)
        if failure == "cancel":
            request.cancel()
            with pytest.raises(asyncio.CancelledError):
                await request
        else:
            response = await request
            assert response.status_code == (409 if failure == "busy" else 503)
            assert response.json()["detail"]["error"] == (
                "CoordinationBusyError"
                if failure == "busy"
                else "CoordinationUnavailableError"
            )
            assert "scanning_started" not in response.text
    assert admission_joined.is_set()
    assert not app.state.background_tasks
    assert len(accepted) == 1
