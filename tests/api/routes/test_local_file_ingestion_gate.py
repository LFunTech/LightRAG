"""HTTP route gates for operators that disable local file ingestion."""

from __future__ import annotations

import importlib
import sys
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

pytestmark = pytest.mark.offline

_HEADERS = {"X-API-Key": "test-key"}


class _Rag:
    def __init__(self):
        self.workspace = f"localgate-{uuid4().hex[:8]}"
        self.process_calls = 0
        self.reset_calls = []

    async def arollback_failed_custom_chunk_patches(self, **kwargs):
        return {"rolled_back": [], "failed": []}

    async def apipeline_reset_failed_for_scan(self, *args, **kwargs):
        self.reset_calls.append((args, kwargs))
        return True

    async def apipeline_process_enqueue_documents(self):
        self.process_calls += 1


def _client(monkeypatch, tmp_path, *, enabled: bool) -> tuple[TestClient, _Rag]:
    rag = _Rag()
    app = FastAPI()
    app.state.background_tasks = set()
    monkeypatch.setattr(
        _dr,
        "global_args",
        SimpleNamespace(
            enable_local_file_ingestion=enabled,
            max_upload_size=1024 * 1024,
            scan_enqueue_batch_size=10,
            scan_spool_dir="",
        ),
    )
    app.include_router(
        create_document_routes(
            rag,
            DocumentManager(str(tmp_path / "inputs"), workspace=rag.workspace),
            api_key="test-key",
        )
    )
    return TestClient(app), rag


def test_upload_route_refuses_when_local_file_ingestion_is_disabled_without_object_store(
    monkeypatch, tmp_path
):
    client, _rag = _client(monkeypatch, tmp_path, enabled=False)

    response = client.post(
        "/documents/upload",
        headers=_HEADERS,
        files={"file": ("local.txt", b"should not land", "text/plain")},
    )

    assert response.status_code == 503
    assert "Object-store document ingestion is not configured" in response.text
    assert [p.name for p in (tmp_path / "inputs").rglob("*") if p.is_file()] == []


def test_scan_route_refuses_when_local_file_ingestion_is_disabled(
    monkeypatch, tmp_path
):
    async def _run():
        client, rag = _client(monkeypatch, tmp_path, enabled=False)

        response = client.post("/documents/scan", headers=_HEADERS)

        assert response.status_code == 403
        assert "Local file ingestion is disabled" in response.text
        assert rag.process_calls == 0
        assert rag.reset_calls == []

    import asyncio

    asyncio.run(_run())
