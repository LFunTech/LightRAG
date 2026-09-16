"""Presigned object upload routes are additive and secret-safe."""

from __future__ import annotations

import importlib
import sys
from contextlib import asynccontextmanager
from types import SimpleNamespace
from uuid import uuid4

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

_original_argv = sys.argv[:]
sys.argv = [sys.argv[0]]
_dr = importlib.import_module("lightrag.api.routers.document_routes")
sys.argv = _original_argv

DocumentManager = _dr.DocumentManager
create_document_routes = _dr.create_document_routes

from lightrag.object_storage import FakeObjectStore, ObjectStoreConfig  # noqa: E402
from lightrag.object_storage import ObjectStoreUnavailableError  # noqa: E402
from lightrag.object_storage.sessions import (  # noqa: E402
    InMemoryUploadSessionStore,
    UploadSession,
    UploadSessionManager,
)
from lightrag.distributed.runtime import DistributedRuntime, storage_write  # noqa: E402

pytestmark = pytest.mark.offline

_HEADERS = {"X-API-Key": "test-key"}


class _DocStatus:
    async def resolve_doc_source_strict(self, _canonical_source_key):
        from lightrag.base import SourceAbsent

        return SourceAbsent()

    async def get_doc_by_file_basename(self, _basename):
        return None


class _Rag:
    workspace = "tenant_a"
    addon_params = {}

    def __init__(self, runtime=None):
        self._distributed_runtime = runtime
        self.doc_status = _DocStatus()
        self.enqueued = []
        self.process_calls = 0

    async def apipeline_enqueue_documents(self, **kwargs):
        self.enqueued.append(kwargs)
        return kwargs.get("track_id")

    async def apipeline_process_enqueue_documents(self):
        self.process_calls += 1


class _UnavailablePresignStore(FakeObjectStore):
    async def presign_upload(self, *args, **kwargs):
        raise ObjectStoreUnavailableError("object store unavailable")


class _UnavailablePutStore(FakeObjectStore):
    async def put_bytes(self, *args, **kwargs):
        raise ObjectStoreUnavailableError("object store unavailable during upload")


class _Coordinator:
    def __init__(self):
        self.events = []

    @asynccontextmanager
    async def operation(self, kind, **kwargs):
        operation = SimpleNamespace(id=uuid4())
        self.events.append(("enter", kind, kwargs.get("exclusive", False)))
        try:
            yield operation
        finally:
            self.events.append(("exit", kind))

    async def heartbeat(self, operation):
        self.events.append(("heartbeat", operation.id))


class _DistributedUploadSessionStore(InMemoryUploadSessionStore):
    namespace = "upload_sessions"
    workspace = "tenant_a"

    def __init__(self, runtime):
        super().__init__()
        self._distributed_runtime = runtime

    @storage_write
    async def put(self, session: UploadSession) -> None:
        await super().put(session)


def _make_runtime():
    coordinator = _Coordinator()
    return DistributedRuntime(coordinator, workspace="tenant_a"), coordinator


def _make_client(
    monkeypatch,
    tmp_path,
    *,
    with_object_store=True,
    max_upload_size=100,
    local_file_ingestion_enabled=True,
    runtime=None,
    session_manager=None,
    raise_server_exceptions=True,
):
    rag = _Rag(runtime=runtime)
    app = FastAPI()
    app.state.background_tasks = set()
    manager = DocumentManager(str(tmp_path / "inputs"), workspace=rag.workspace)
    object_store = None
    sessions = None
    if with_object_store:
        object_store = FakeObjectStore(
            ObjectStoreConfig(
                provider="fake",
                bucket="docs",
                endpoint_url="https://objects.example.com",
                access_key_id="access-key",
                secret_access_key="secret-key",
            )
        )
        sessions = session_manager or UploadSessionManager(
            InMemoryUploadSessionStore(), bucket="docs", prefix="lightrag"
        )
    routes_module_args = SimpleNamespace(
        enable_local_file_ingestion=local_file_ingestion_enabled,
        max_upload_size=max_upload_size,
        s3_presign_ttl_seconds=600,
        s3_upload_session_ttl_seconds=1800,
    )
    monkeypatch.setattr(_dr, "global_args", routes_module_args)
    app.include_router(
        create_document_routes(
            rag,
            manager,
            api_key="test-key",
            object_store=object_store,
            upload_session_manager=sessions,
        )
    )
    return (
        TestClient(app, raise_server_exceptions=raise_server_exceptions),
        rag,
        object_store,
    )


def test_official_upload_uses_object_store_when_local_ingestion_is_disabled(
    monkeypatch, tmp_path
):
    client, rag, object_store = _make_client(
        monkeypatch,
        tmp_path,
        local_file_ingestion_enabled=False,
    )

    response = client.post(
        "/documents/upload",
        headers=_HEADERS,
        files={
            "file": (
                "notes.[-R(chunk_ts=800,chunk_ol=80)].md",
                b"hello",
                "text/markdown",
            )
        },
    )

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "success"
    assert body["track_id"].startswith("upload_")
    assert [p.name for p in (tmp_path / "inputs").rglob("*") if p.is_file()] == []
    assert rag.enqueued
    enqueued = rag.enqueued[0]
    assert enqueued["file_paths"] == "notes.md"
    assert enqueued["docs_format"] == "pending_parse"
    assert enqueued["object_source"]["source_kind"] == "s3_object"
    assert enqueued["object_source"]["object_key"].startswith(
        "lightrag/uploads/tenant_a/"
    )
    assert enqueued["parse_engine"] == "legacy"
    assert enqueued["process_options"] == "R"
    recursive = enqueued["chunk_options"]["recursive_character"]
    assert recursive["chunk_token_size"] == 800
    assert recursive["chunk_overlap_token_size"] == 80

    import anyio

    keys = anyio.run(object_store.list_keys, "lightrag/uploads/tenant_a/")
    assert keys == [enqueued["object_source"]["object_key"]]


def test_official_upload_returns_after_enqueue_without_waiting_for_pipeline_drive(
    monkeypatch, tmp_path
):
    client, rag, _object_store = _make_client(
        monkeypatch,
        tmp_path,
        local_file_ingestion_enabled=False,
        raise_server_exceptions=False,
    )

    async def _failing_drive_pipeline(_rag):
        _rag.process_calls += 1
        raise RuntimeError("pipeline drive must stay in background")

    monkeypatch.setattr(_dr, "drive_pipeline", _failing_drive_pipeline)

    response = client.post(
        "/documents/upload",
        headers=_HEADERS,
        files={"file": ("report.pdf", b"hello", "application/pdf")},
    )

    assert response.status_code == 200
    assert response.json()["status"] == "success"
    assert rag.enqueued
    assert rag.process_calls == 1


def test_official_upload_fails_closed_without_object_store_when_local_disabled(
    monkeypatch, tmp_path
):
    client, rag, _object_store = _make_client(
        monkeypatch,
        tmp_path,
        with_object_store=False,
        local_file_ingestion_enabled=False,
    )

    response = client.post(
        "/documents/upload",
        headers=_HEADERS,
        files={"file": ("report.pdf", b"hello", "application/pdf")},
    )

    assert response.status_code == 503
    assert "Object-store document ingestion is not configured" in response.text
    assert rag.enqueued == []
    assert [p.name for p in (tmp_path / "inputs").rglob("*") if p.is_file()] == []


def test_official_upload_object_store_failure_does_not_enqueue_or_write_input_dir(
    monkeypatch, tmp_path
):
    client, rag, object_store = _make_client(
        monkeypatch,
        tmp_path,
        local_file_ingestion_enabled=False,
    )
    client.app.router.routes.clear()
    manager = DocumentManager(str(tmp_path / "inputs-unavailable"), workspace=rag.workspace)
    sessions = UploadSessionManager(
        InMemoryUploadSessionStore(), bucket="docs", prefix="lightrag"
    )
    unavailable = _UnavailablePutStore(object_store.config)
    client.app.include_router(
        create_document_routes(
            rag,
            manager,
            api_key="test-key",
            object_store=unavailable,
            upload_session_manager=sessions,
        )
    )

    response = client.post(
        "/documents/upload",
        headers=_HEADERS,
        files={"file": ("report.pdf", b"hello", "application/pdf")},
    )

    assert response.status_code == 503
    assert rag.enqueued == []
    assert [p.name for p in (tmp_path / "inputs-unavailable").rglob("*") if p.is_file()] == []


def test_presign_requires_authentication(monkeypatch, tmp_path):
    client, _, _ = _make_client(monkeypatch, tmp_path)

    response = client.post(
        "/documents/uploads/presign",
        json={"filename": "report.pdf", "content_type": "application/pdf", "size": 5},
    )

    assert response.status_code in {401, 403}


def test_presign_rejects_unsafe_filename_before_signing(monkeypatch, tmp_path):
    client, _, _ = _make_client(monkeypatch, tmp_path)

    response = client.post(
        "/documents/uploads/presign",
        headers=_HEADERS,
        json={"filename": "../report.pdf", "content_type": "application/pdf", "size": 5},
    )

    assert response.status_code == 400
    assert "upload_url" not in response.text


def test_presign_rejects_oversized_file_before_signing(monkeypatch, tmp_path):
    client, _, _ = _make_client(monkeypatch, tmp_path, max_upload_size=4)

    response = client.post(
        "/documents/uploads/presign",
        headers=_HEADERS,
        json={"filename": "report.pdf", "content_type": "application/pdf", "size": 5},
    )

    assert response.status_code == 413
    assert "objects.example.com" not in response.text


def test_presign_issues_server_owned_key_and_secret_safe_url(monkeypatch, tmp_path):
    client, _, _ = _make_client(monkeypatch, tmp_path)

    response = client.post(
        "/documents/uploads/presign",
        headers=_HEADERS,
        json={
            "filename": "report.[native-P].docx",
            "content_type": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            "size": 5,
            "checksum_sha256": "a" * 64,
            "object_key": "attacker/chosen/key.docx",
        },
    )

    assert response.status_code == 200
    body = response.json()
    assert body["upload_id"].startswith("upload_")
    assert body["object_key"].startswith("lightrag/uploads/tenant_a/")
    assert body["object_key"].endswith("/report.[native-P].docx")
    assert "attacker" not in body["object_key"]
    assert body["method"] == "PUT"
    assert body["headers"]["Content-Type"].startswith("application/vnd")
    assert "secret-key" not in response.text
    assert "access-key" not in response.text


def test_presign_stays_available_when_local_file_ingestion_is_disabled(
    monkeypatch, tmp_path
):
    client, _, _ = _make_client(
        monkeypatch,
        tmp_path,
        local_file_ingestion_enabled=False,
    )

    response = client.post(
        "/documents/uploads/presign",
        headers=_HEADERS,
        json={"filename": "report.pdf", "content_type": "application/pdf", "size": 5},
    )

    assert response.status_code == 200
    assert response.json()["object_key"].startswith("lightrag/uploads/tenant_a/")


def test_presign_records_upload_session_inside_distributed_operation(
    monkeypatch, tmp_path
):
    runtime, coordinator = _make_runtime()
    sessions = UploadSessionManager(
        _DistributedUploadSessionStore(runtime), bucket="docs", prefix="lightrag"
    )
    client, _, _ = _make_client(
        monkeypatch, tmp_path, runtime=runtime, session_manager=sessions
    )

    response = client.post(
        "/documents/uploads/presign",
        headers=_HEADERS,
        json={"filename": "report.pdf", "content_type": "application/pdf", "size": 5},
    )

    assert response.status_code == 200
    assert ("enter", "object_upload_presign", False) in coordinator.events


def test_complete_updates_upload_session_inside_distributed_operation(
    monkeypatch, tmp_path
):
    import anyio

    runtime, coordinator = _make_runtime()
    sessions = UploadSessionManager(
        _DistributedUploadSessionStore(runtime), bucket="docs", prefix="lightrag"
    )
    client, rag, object_store = _make_client(
        monkeypatch, tmp_path, runtime=runtime, session_manager=sessions
    )
    async def _drive_pipeline(_rag):
        return None

    monkeypatch.setattr(_dr, "drive_pipeline", _drive_pipeline)

    async def _seed_session_and_object():
        async with runtime.operation("seed_upload_session"):
            session = await sessions.create_session(
                workspace=rag.workspace,
                filename="report.pdf",
                content_type="application/pdf",
                size=5,
                checksum_sha256=None,
                ttl_seconds=1800,
            )
        await object_store.put_bytes(
            session.object_key, b"hello", content_type="application/pdf"
        )
        return session

    session = anyio.run(_seed_session_and_object)

    response = client.post(
        "/documents/uploads/complete",
        headers=_HEADERS,
        json={"upload_id": session.upload_id, "object_key": session.object_key},
    )

    assert response.status_code == 200
    assert rag.enqueued
    assert ("enter", "object_upload_complete", False) in coordinator.events


def test_complete_verifies_object_and_enqueues_object_backed_document(monkeypatch, tmp_path):
    client, rag, object_store = _make_client(monkeypatch, tmp_path)
    presign = client.post(
        "/documents/uploads/presign",
        headers=_HEADERS,
        json={"filename": "report.pdf", "content_type": "application/pdf", "size": 5},
    ).json()

    import anyio

    async def _put_object():
        await object_store.put_bytes(
            presign["object_key"], b"hello", content_type="application/pdf"
        )

    anyio.run(_put_object)
    response = client.post(
        "/documents/uploads/complete",
        headers=_HEADERS,
        json={"upload_id": presign["upload_id"], "object_key": presign["object_key"]},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "success"
    assert rag.enqueued
    enqueued = rag.enqueued[0]
    assert enqueued["file_paths"] == "report.pdf"
    assert enqueued["docs_format"] == "pending_parse"
    assert enqueued["object_source"]["source_kind"] == "s3_object"
    assert enqueued["object_source"]["object_key"] == presign["object_key"]


def test_complete_returns_after_enqueue_without_waiting_for_pipeline_drive(
    monkeypatch, tmp_path
):
    client, rag, object_store = _make_client(
        monkeypatch, tmp_path, raise_server_exceptions=False
    )
    presign = client.post(
        "/documents/uploads/presign",
        headers=_HEADERS,
        json={"filename": "report.pdf", "content_type": "application/pdf", "size": 5},
    ).json()

    import anyio

    async def _put_object():
        await object_store.put_bytes(
            presign["object_key"], b"hello", content_type="application/pdf"
        )

    async def _failing_drive_pipeline(_rag):
        _rag.process_calls += 1
        raise RuntimeError("pipeline drive must stay in background")

    anyio.run(_put_object)
    monkeypatch.setattr(_dr, "drive_pipeline", _failing_drive_pipeline)
    response = client.post(
        "/documents/uploads/complete",
        headers=_HEADERS,
        json={"upload_id": presign["upload_id"], "object_key": presign["object_key"]},
    )

    assert response.status_code == 200
    assert response.json()["status"] == "success"
    assert rag.enqueued
    assert rag.process_calls == 1


def test_complete_applies_filename_hint_process_and_chunk_options(monkeypatch, tmp_path):
    client, rag, object_store = _make_client(monkeypatch, tmp_path)
    presign = client.post(
        "/documents/uploads/presign",
        headers=_HEADERS,
        json={
            "filename": "notes.[-R(chunk_ts=800,chunk_ol=80)].md",
            "content_type": "text/markdown",
            "size": 5,
        },
    ).json()

    import anyio

    async def _put_object():
        await object_store.put_bytes(
            presign["object_key"], b"hello", content_type="text/markdown"
        )

    anyio.run(_put_object)
    response = client.post(
        "/documents/uploads/complete",
        headers=_HEADERS,
        json={"upload_id": presign["upload_id"], "object_key": presign["object_key"]},
    )

    assert response.status_code == 200
    enqueued = rag.enqueued[0]
    assert enqueued["file_paths"] == "notes.md"
    assert enqueued["parse_engine"] == "legacy"
    assert enqueued["process_options"] == "R"
    recursive = enqueued["chunk_options"]["recursive_character"]
    assert recursive["chunk_token_size"] == 800
    assert recursive["chunk_overlap_token_size"] == 80


def test_complete_missing_object_does_not_enqueue(monkeypatch, tmp_path):
    client, rag, _ = _make_client(monkeypatch, tmp_path)
    presign = client.post(
        "/documents/uploads/presign",
        headers=_HEADERS,
        json={"filename": "report.pdf", "content_type": "application/pdf", "size": 5},
    ).json()

    response = client.post(
        "/documents/uploads/complete",
        headers=_HEADERS,
        json={"upload_id": presign["upload_id"], "object_key": presign["object_key"]},
    )

    assert response.status_code == 404
    assert rag.enqueued == []


def test_object_upload_routes_fail_closed_when_not_configured(monkeypatch, tmp_path):
    client, _, _ = _make_client(monkeypatch, tmp_path, with_object_store=False)

    response = client.post(
        "/documents/uploads/presign",
        headers=_HEADERS,
        json={"filename": "report.pdf", "content_type": "application/pdf", "size": 5},
    )

    assert response.status_code == 503


def test_presign_object_store_unavailable_returns_503(monkeypatch, tmp_path):
    client, _, object_store = _make_client(monkeypatch, tmp_path)
    client.app.router.routes.clear()
    rag = _Rag()
    manager = DocumentManager(str(tmp_path / "inputs-unavailable"), workspace=rag.workspace)
    sessions = UploadSessionManager(
        InMemoryUploadSessionStore(), bucket="docs", prefix="lightrag"
    )
    unavailable = _UnavailablePresignStore(object_store.config)
    client.app.include_router(
        create_document_routes(
            rag,
            manager,
            api_key="test-key",
            object_store=unavailable,
            upload_session_manager=sessions,
        )
    )

    response = client.post(
        "/documents/uploads/presign",
        headers=_HEADERS,
        json={"filename": "report.pdf", "content_type": "application/pdf", "size": 5},
    )

    assert response.status_code == 503
    assert "secret-key" not in response.text


def test_complete_metadata_mismatch_does_not_enqueue(monkeypatch, tmp_path):
    client, rag, object_store = _make_client(monkeypatch, tmp_path)
    presign = client.post(
        "/documents/uploads/presign",
        headers=_HEADERS,
        json={"filename": "report.pdf", "content_type": "application/pdf", "size": 5},
    ).json()

    import anyio

    async def _put_wrong_size():
        await object_store.put_bytes(
            presign["object_key"], b"four", content_type="application/pdf"
        )

    anyio.run(_put_wrong_size)
    response = client.post(
        "/documents/uploads/complete",
        headers=_HEADERS,
        json={"upload_id": presign["upload_id"], "object_key": presign["object_key"]},
    )

    assert response.status_code == 409
    assert rag.enqueued == []


def test_repeated_complete_returns_original_track_without_second_enqueue(monkeypatch, tmp_path):
    client, rag, object_store = _make_client(monkeypatch, tmp_path)
    presign = client.post(
        "/documents/uploads/presign",
        headers=_HEADERS,
        json={"filename": "report.pdf", "content_type": "application/pdf", "size": 5},
    ).json()

    import anyio

    async def _put_object():
        await object_store.put_bytes(
            presign["object_key"], b"hello", content_type="application/pdf"
        )

    anyio.run(_put_object)
    first = client.post(
        "/documents/uploads/complete",
        headers=_HEADERS,
        json={"upload_id": presign["upload_id"], "object_key": presign["object_key"]},
    )
    second = client.post(
        "/documents/uploads/complete",
        headers=_HEADERS,
        json={"upload_id": presign["upload_id"], "object_key": presign["object_key"]},
    )

    assert first.status_code == 200
    assert second.status_code == 200
    assert second.json()["track_id"] == first.json()["track_id"]
    assert len(rag.enqueued) == 1
