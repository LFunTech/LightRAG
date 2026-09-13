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


@pytest.mark.parametrize("failure", ["busy", "cancel"])
async def test_delete_handoff_waits_for_exclusive_admission_and_releases_reservation(
    distributed_api, monkeypatch, failure
):
    from contextlib import asynccontextmanager
    from contextvars import Context
    from lightrag.kg.shared_storage import get_namespace_data

    client, rag, peer, _, app = distributed_api
    entered, release = asyncio.Event(), asyncio.Event()
    coordinator = rag._distributed_runtime.coordinator
    original = coordinator.operation
    coordinator.wait_timeout = 0.03

    @asynccontextmanager
    async def delayed(kind, *args, **kwargs):
        if kind == "background_delete_documents":
            entered.set()
            await release.wait()
        async with original(kind, *args, **kwargs) as operation:
            yield operation

    monkeypatch.setattr(coordinator, "operation", delayed)
    async with peer._distributed_runtime.operation("live_writer"):
        request = Context().run(
            asyncio.create_task,
            client.request(
                "DELETE",
                "/documents/delete_document",
                headers={"X-API-Key": "test-key"},
                json={"doc_ids": ["missing"]},
            ),
        )
        try:
            await asyncio.wait_for(entered.wait(), 2)
            await asyncio.sleep(0.02)
            assert not request.done(), (
                "Delete returned success before durable admission"
            )
            if failure == "cancel":
                request.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await request
            else:
                release.set()
                response = await request
                assert response.status_code == 409
                assert response.json()["detail"]["error"] == "CoordinationBusyError"
            state = await get_namespace_data("pipeline_status", workspace=rag.workspace)
            assert not state.get("destructive_busy")
            assert not state.get("busy")
            assert not (await coordinator.inspect())["fenced"]
        finally:
            release.set()
            await asyncio.gather(
                request, *list(app.state.background_tasks), return_exceptions=True
            )


@pytest.mark.parametrize("endpoint", ["upload", "text", "texts"])
async def test_ingress_handoff_has_no_remote_maintenance_gap(
    distributed_api, monkeypatch, endpoint
):
    from contextlib import asynccontextmanager
    from contextvars import Context
    from lightrag.distributed import CoordinationBusyError

    client, rag, peer, _, app = distributed_api
    await rag._distributed_runtime.coordinator.pipeline_control.pause()
    entered, release = asyncio.Event(), asyncio.Event()
    coordinator = rag._distributed_runtime.coordinator
    original = coordinator.operation
    background_kind = (
        "pipeline_index_file" if endpoint == "upload" else "pipeline_index_texts"
    )

    @asynccontextmanager
    async def delayed(kind, *args, **kwargs):
        if kind == background_kind:
            entered.set()
            await release.wait()
        async with original(kind, *args, **kwargs) as operation:
            yield operation

    monkeypatch.setattr(coordinator, "operation", delayed)
    payload = (
        {"files": {"file": ("handoff.txt", b"Atlas cooperates with Borealis.")}}
        if endpoint == "upload"
        else {
            "json": {
                "text": "Atlas cooperates with Borealis.",
                "file_source": "handoff.txt",
            }
        }
        if endpoint == "text"
        else {
            "json": {
                "texts": ["Atlas cooperates with Borealis."],
                "file_sources": ["handoff.txt"],
            }
        }
    )
    request = Context().run(
        asyncio.create_task,
        client.post(
            "/documents/" + endpoint, headers={"X-API-Key": "test-key"}, **payload
        ),
    )
    try:
        await asyncio.wait_for(entered.wait(), 2)
        await asyncio.sleep(0.02)
        assert not request.done(), "Accepted ingress lost its durable handoff coverage"
        peer._distributed_runtime.coordinator.wait_timeout = 0.03
        with pytest.raises(CoordinationBusyError):
            async with peer._distributed_runtime.operation(
                "remote_clear", exclusive=True
            ):
                pytest.fail("Remote maintenance entered the handoff gap")
        release.set()
        response = await request
        assert response.status_code == 200
        await asyncio.gather(*list(app.state.background_tasks))
        operations = (await coordinator.inspect())["operations"]
        assert any(row["kind"] == background_kind for row in operations)
        assert all(
            not row["exclusive"]
            for row in operations
            if row["kind"]
            in {background_kind, "upload_to_input_dir", "insert_text", "insert_texts"}
        )
        assert not (await coordinator.inspect())["fenced"]
    finally:
        release.set()
        await asyncio.gather(
            request, *list(app.state.background_tasks), return_exceptions=True
        )


async def test_scan_file_cleanup_failure_propagates_through_actual_enqueue_batch(
    distributed_api, monkeypatch
):
    from pathlib import Path

    _, rag, _, manager, _ = distributed_api
    routes = importlib.import_module("lightrag.api.routers.document_routes")
    target = manager.input_dir / "__tmp__cleanup_failure.txt"
    target.write_text("Atlas cooperates with Borealis.")
    original_unlink = Path.unlink
    failed = False

    def unlink(path, *args, **kwargs):
        nonlocal failed
        if path == target:
            failed = True
            raise OSError("unconfirmed scan file cleanup")
        return original_unlink(path, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", unlink)
    try:
        with pytest.raises(OSError, match="unconfirmed scan file cleanup"):
            await routes.run_scanning_process(
                rag, manager, track_id="failed-file-cleanup"
            )
    finally:
        assert failed, "Fault must run inside the real file enqueue cleanup"
    state = await rag._distributed_runtime.coordinator.inspect()
    assert state["fenced"]
    scan = await rag._distributed_runtime.coordinator.pipeline_control.scan_status(
        "failed-file-cleanup"
    )
    assert scan["status"] == "abandoned"


@pytest.mark.parametrize("endpoint", ["upload", "text", "texts"])
@pytest.mark.parametrize("failure", ["busy", "cancel"])
async def test_ingress_failed_admission_releases_local_reservation(
    distributed_api, monkeypatch, endpoint, failure
):
    from contextlib import asynccontextmanager
    from contextvars import Context
    from lightrag.distributed import CoordinationBusyError
    from lightrag.kg.shared_storage import get_namespace_data

    client, rag, _, _, app = distributed_api
    coordinator = rag._distributed_runtime.coordinator
    original = coordinator.operation
    entered, release = asyncio.Event(), asyncio.Event()
    background_kind = (
        "pipeline_index_file" if endpoint == "upload" else "pipeline_index_texts"
    )

    @asynccontextmanager
    async def refused(kind, *args, **kwargs):
        if kind == background_kind:
            entered.set()
            await release.wait()
            raise CoordinationBusyError("Injected background admission refusal")
        async with original(kind, *args, **kwargs) as operation:
            yield operation

    monkeypatch.setattr(coordinator, "operation", refused)
    payload = (
        {
            "files": {
                "file": ("refused.txt", b"Content remains owned by the failed request.")
            }
        }
        if endpoint == "upload"
        else {"json": {"text": "Text", "file_source": "refused.txt"}}
        if endpoint == "text"
        else {"json": {"texts": ["Text"], "file_sources": ["refused.txt"]}}
    )
    request = Context().run(
        asyncio.create_task,
        client.post(
            "/documents/" + endpoint, headers={"X-API-Key": "test-key"}, **payload
        ),
    )
    try:
        await asyncio.wait_for(entered.wait(), 2)
        if failure == "cancel":
            request.cancel()
            with pytest.raises(asyncio.CancelledError):
                await request
        else:
            release.set()
            response = await request
            assert response.status_code == 409
            assert response.json()["detail"]["error"] == "CoordinationBusyError"
        local = await get_namespace_data("pipeline_status", workspace=rag.workspace)
        assert local.get("pending_enqueues") == 0
        assert not local.get("pending_enqueue_tokens")
        operations = (await coordinator.inspect())["operations"]
        assert not any(row["kind"] == background_kind for row in operations)
        # The request itself was admitted (upload may have written a file).
        # Its conservative fence is retained; cleanup must not fake an ACK.
        assert (await coordinator.inspect())["fenced"]
    finally:
        release.set()
        await asyncio.gather(
            request, *list(app.state.background_tasks), return_exceptions=True
        )


@pytest.mark.parametrize("failure", ["busy", "unavailable", "cancel"])
async def test_scan_admission_http_error_retains_intent_and_joins_child(
    distributed_api, monkeypatch, failure
):
    from contextlib import asynccontextmanager
    from contextvars import Context

    from lightrag.distributed import CoordinationUnavailableError

    _, rag, peer, manager, app = distributed_api
    coordinator = rag._distributed_runtime.coordinator
    coordinator.wait_timeout = 0.05 if failure == "busy" else 10
    entered = asyncio.Event()
    joined = asyncio.Event()
    original_operation = coordinator.operation
    original_wait = coordinator._wait

    async def observed_wait(attempt, timeout):
        async def observed_attempt():
            result = await attempt()
            if result is None:
                entered.set()  # Conflict confirmed; no transaction is in flight.
            return result

        return await original_wait(observed_attempt, timeout)

    @asynccontextmanager
    async def observed_operation(*args, **kwargs):
        if failure != "cancel":
            entered.set()
        try:
            if failure == "unavailable":
                raise CoordinationUnavailableError("test admission unavailable")
            async with original_operation(*args, **kwargs) as op:
                yield op
        finally:
            joined.set()

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app, raise_app_exceptions=False),
        base_url="http://test",
    ) as client:
        with monkeypatch.context() as patch:
            patch.setattr(coordinator, "operation", observed_operation)
            if failure == "cancel":
                patch.setattr(coordinator, "_wait", observed_wait)
            # Keep the real shared peer ticket across the actual HTTP/exclusive wait.
            async with peer._distributed_runtime.operation("peer_writer"):
                request = Context().run(
                    asyncio.create_task,
                    client.post("/documents/scan", headers={"X-API-Key": "test-key"}),
                )
                await asyncio.wait_for(entered.wait(), 2)
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
                assert joined.is_set()
                assert not app.state.background_tasks
                control = await peer._distributed_runtime.coordinator.pipeline_control.status()
                assert control["pending_retries"] == 1
                assert control["active_operations"] == 1  # Only the independent writer.
    state = await peer._distributed_runtime.coordinator.inspect()
    assert not state["fenced"]
    assert not any(row["kind"] == "run_scanning_process" for row in state["operations"])
    assert not list(manager.input_dir.iterdir())
