"""Authenticated distributed controls exercise the actual FastAPI handlers."""

import asyncio
import importlib
import sys
import httpx
import pytest
from fastapi import FastAPI
from lightrag.distributed import CoordinationError
from lightrag.api.distributed import coordination_error_handler
from tests.distributed.test_runtime_integration import runtime_rags as _runtime_rags
from tests.distributed.test_pipeline_business_integration import configure

runtime_rags = _runtime_rags

pytestmark = pytest.mark.integration


@pytest.fixture
async def distributed_api(runtime_rags, tmp_path, monkeypatch):
    monkeypatch.setattr(sys, "argv", ["test"])
    routes = importlib.import_module("lightrag.api.routers.document_routes")
    rag, peer = runtime_rags
    await configure(runtime_rags)
    manager = routes.DocumentManager(str(tmp_path / "inputs"), workspace=rag.workspace)
    rag.distributed_input_dir = peer.distributed_input_dir = str(tmp_path / "inputs")
    app = FastAPI()
    app.state.background_tasks = set()
    app.add_exception_handler(CoordinationError, coordination_error_handler)
    app.include_router(routes.create_document_routes(rag, manager, api_key="test-key"))
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        yield client, rag, peer, manager, app
    await rag.apipeline_stop_polling()
    if app.state.background_tasks:
        await asyncio.gather(*list(app.state.background_tasks))


async def test_controls_keep_auth_and_global_pause_force_reset_refusal(distributed_api):
    client, rag, peer, _, _ = distributed_api
    denied = await client.post("/documents/cancel_pipeline")
    assert denied.status_code in {401, 403}
    assert not (await peer._distributed_runtime.coordinator.pipeline_control.status())[
        "paused"
    ]
    headers = {"X-API-Key": "test-key"}
    cancelled = await client.post("/documents/cancel_pipeline", headers=headers)
    assert cancelled.status_code == 200
    assert (await peer._distributed_runtime.coordinator.pipeline_control.status())[
        "paused"
    ]
    status = await client.get("/documents/pipeline_status", headers=headers)
    assert status.status_code == 200
    assert status.json()["distributed"]["paused"] is True
    assert "local_busy" in status.json()
    refused = await client.post(
        "/documents/recovery/force_reset", headers=headers, json={"confirm": True}
    )
    assert refused.status_code == 409
    assert refused.json()["detail"]["error"] == "DistributedRecoveryRequired"


async def test_clear_waits_for_other_pod_and_bad_upload_does_not_fence(distributed_api):
    client, rag, peer, _, _ = distributed_api
    headers = {"X-API-Key": "test-key"}
    rag._distributed_runtime.coordinator.wait_timeout = 0.05
    async with peer._distributed_runtime.operation("live_pipeline"):
        from contextvars import Context

        response = await Context().run(
            asyncio.create_task, client.delete("/documents", headers=headers)
        )
        assert response.status_code == 409
        assert response.json()["detail"]["error"] == "CoordinationBusyError"
    bad = await client.post(
        "/documents/upload",
        headers=headers,
        files={"file": ("bad.unsupported", b"not supported")},
    )
    assert bad.status_code == 400
    assert not (await rag._distributed_runtime.coordinator.inspect())["fenced"]


async def test_scan_releases_exclusive_before_real_processing(distributed_api):
    client, rag, _, manager, app = distributed_api
    (manager.input_dir / "scan.txt").write_text(
        "Atlas cooperates with Borealis from a scan."
    )
    response = await client.post("/documents/scan", headers={"X-API-Key": "test-key"})
    assert response.status_code == 200
    if app.state.background_tasks:
        await asyncio.wait_for(asyncio.gather(*list(app.state.background_tasks)), 20)
    from lightrag.base import DocStatus

    rows = await rag.doc_status.get_docs_by_statuses([DocStatus.PROCESSED], strict=True)
    assert len(rows) == 1
    state = await rag._distributed_runtime.coordinator.inspect()
    processing = [
        row
        for row in state["operations"]
        if row["kind"] in {"pipeline", "scan_processing"}
    ]
    assert processing
    assert all(not row["exclusive"] for row in processing)
    assert not state["fenced"]
    progress = await client.get(
        "/documents/scan/status/" + response.json()["track_id"],
        headers={"X-API-Key": "test-key"},
    )
    assert progress.status_code == 200
    assert progress.json()["status"] == "completed"
    assert progress.json()["counts"]["discovered"] == 1


async def test_scan_failure_is_not_swallowed_as_completed_ticket(
    distributed_api, monkeypatch
):
    _, rag, _, manager, _ = distributed_api
    routes = importlib.import_module("lightrag.api.routers.document_routes")
    (manager.input_dir / "broken.txt").write_text("content")

    async def fail(*args, **kwargs):
        raise OSError("test classification failure")

    monkeypatch.setattr(routes, "classify_scan_file", fail)
    with pytest.raises(OSError, match="test classification failure"):
        await routes.run_scanning_process(rag, manager)
    assert (await rag._distributed_runtime.coordinator.inspect())["fenced"]


async def test_clear_terminally_retires_durable_retry_requests(distributed_api):
    client, rag, peer, _, _ = distributed_api
    await rag.apipeline_request_retry("clear-request")
    response = await client.delete("/documents", headers={"X-API-Key": "test-key"})
    assert response.status_code == 200
    assert (await peer._distributed_runtime.coordinator.pipeline_control.status())[
        "pending_retries"
    ] == 0
    replay = await peer._distributed_runtime.coordinator.pipeline_control.request_retry(
        "clear-request"
    )
    assert replay["state"] == "completed"


async def test_background_file_failure_is_not_swallowed(distributed_api, monkeypatch):
    _, rag, _, manager, _ = distributed_api
    routes = importlib.import_module("lightrag.api.routers.document_routes")

    async def fail(*args, **kwargs):
        raise OSError("test file cleanup failure")

    monkeypatch.setattr(routes, "pipeline_enqueue_file", fail)
    with pytest.raises(OSError, match="test file cleanup failure"):
        await routes.pipeline_index_file(rag, manager.input_dir / "file.txt")
    assert (await rag._distributed_runtime.coordinator.inspect())["fenced"]


async def test_scan_coordination_failure_never_drives_from_finally(
    distributed_api, monkeypatch
):
    _, rag, _, manager, _ = distributed_api
    routes = importlib.import_module("lightrag.api.routers.document_routes")
    (manager.input_dir / "broken.txt").write_text("content")
    drives = []

    async def fail(*args, **kwargs):
        raise CoordinationError("test coordination failure")

    async def deferred(*args):
        return True

    async def drive(*args):
        drives.append(True)

    monkeypatch.setattr(routes, "classify_scan_file", fail)
    monkeypatch.setattr(routes, "drive_pipeline", drive)
    monkeypatch.setattr(
        "lightrag.kg.shared_storage.has_scan_deferred_processing", deferred
    )
    with pytest.raises(CoordinationError, match="test coordination failure"):
        await routes.run_scanning_process(rag, manager)
    assert drives == []
    assert (await rag._distributed_runtime.coordinator.inspect())["fenced"]
