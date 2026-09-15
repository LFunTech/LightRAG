"""Presigned object upload routes are additive and secret-safe."""

from __future__ import annotations

import importlib
import sys
from types import SimpleNamespace

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
    UploadSessionManager,
)

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

    def __init__(self):
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


def _make_client(monkeypatch, tmp_path, *, with_object_store=True, max_upload_size=100):
    rag = _Rag()
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
        sessions = UploadSessionManager(
            InMemoryUploadSessionStore(), bucket="docs", prefix="lightrag"
        )
    routes_module_args = SimpleNamespace(
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
    return TestClient(app), rag, object_store


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
