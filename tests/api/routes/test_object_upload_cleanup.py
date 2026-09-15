"""Object-backed document cleanup never treats S3 keys as local paths."""

from __future__ import annotations

import importlib
import sys

import pytest

from lightrag.object_storage import FakeObjectStore, ObjectStoreConfig

pytestmark = pytest.mark.offline

_original_argv = sys.argv[:]
sys.argv = [sys.argv[0]]
_dr = importlib.import_module("lightrag.api.routers.document_routes")
sys.argv = _original_argv


@pytest.mark.asyncio
async def test_delete_object_source_objects_removes_only_owned_source_and_artifacts():
    store = FakeObjectStore(ObjectStoreConfig(provider="fake", bucket="docs"))
    await store.put_bytes("lightrag/uploads/ws/upload_1/report.pdf", b"source")
    await store.put_bytes("lightrag/artifacts/ws/doc-1/report.blocks.jsonl", b"{}\n")
    await store.put_bytes("lightrag/other/ws/doc-1/keep.txt", b"keep")

    deleted, errors = await _dr.delete_object_source_objects(
        store,
        {
            "source_kind": "s3_object",
            "bucket": "docs",
            "object_key": "lightrag/uploads/ws/upload_1/report.pdf",
            "artifact_prefix": "lightrag/artifacts/ws/doc-1",
        },
    )

    assert errors == []
    assert deleted == [
        "s3://docs/lightrag/uploads/ws/upload_1/report.pdf",
        "s3://docs/lightrag/artifacts/ws/doc-1/",
    ]
    assert await store.list_keys("lightrag/uploads/ws/upload_1/") == []
    assert await store.list_keys("lightrag/artifacts/ws/doc-1/") == []
    assert await store.get_bytes("lightrag/other/ws/doc-1/keep.txt") == b"keep"


@pytest.mark.asyncio
async def test_delete_object_source_objects_refuses_unowned_prefixes():
    store = FakeObjectStore(ObjectStoreConfig(provider="fake", bucket="docs"))
    await store.put_bytes("outside/report.pdf", b"source")

    deleted, errors = await _dr.delete_object_source_objects(
        store,
        {
            "source_kind": "s3_object",
            "bucket": "docs",
            "object_key": "outside/report.pdf",
            "artifact_prefix": "outside/artifacts/doc-1",
        },
    )

    assert deleted == []
    assert len(errors) == 2
    assert await store.get_bytes("outside/report.pdf") == b"source"

class _ObjectBackedDocStatus:
    def __init__(self, rows):
        self.rows = rows

    async def get_by_id(self, doc_id):
        row = self.rows.get(doc_id)
        return dict(row) if row is not None else None

    async def resolve_doc_source_strict(self, canonical_source_key):
        from lightrag.base import SourceAbsent

        return SourceAbsent()


class _ObjectBackedDeleteRag:
    def __init__(self, result, *, rows, object_store):
        from uuid import uuid4

        self.result = result
        self.workspace = f"object-delete-{uuid4().hex}"
        self.doc_status = _ObjectBackedDocStatus(rows)
        self.object_store = object_store
        self.deleted_doc_ids = []

    async def adelete_by_doc_id(self, doc_id, delete_llm_cache=False):
        self.deleted_doc_ids.append((doc_id, delete_llm_cache))
        self.doc_status.rows.pop(doc_id, None)
        return self.result

    async def apipeline_process_enqueue_documents(self):
        return None


@pytest.mark.asyncio
async def test_background_delete_cleans_s3_objects_without_deleting_local_namesakes(tmp_path):
    from lightrag.api.routers.document_routes import DocumentManager
    from lightrag.base import DeletionResult
    from lightrag.kg import shared_storage

    local_namesake = tmp_path / "report.pdf"
    local_namesake.write_bytes(b"local file owned by a different flow")
    store = FakeObjectStore(ObjectStoreConfig(provider="fake", bucket="docs"))
    await store.put_bytes("lightrag/uploads/ws/upload-1/report.pdf", b"source")
    await store.put_bytes("lightrag/artifacts/ws/doc-s3/report.blocks.jsonl", b"{}\n")

    object_source = {
        "source_kind": "s3_object",
        "bucket": "docs",
        "object_key": "lightrag/uploads/ws/upload-1/report.pdf",
        "artifact_prefix": "lightrag/artifacts/ws/doc-s3",
    }
    rag = _ObjectBackedDeleteRag(
        DeletionResult(
            status="success",
            doc_id="doc-s3",
            message="deleted",
            file_path="report.pdf",
        ),
        rows={
            "doc-s3": {
                "file_path": "report.pdf",
                "metadata": {"object_source": object_source},
            }
        },
        object_store=store,
    )
    shared_storage.initialize_share_data()
    await shared_storage.initialize_pipeline_status(workspace=rag.workspace)

    await _dr.background_delete_documents(
        rag,
        DocumentManager(str(tmp_path)),
        ["doc-s3"],
        delete_file=True,
    )

    assert rag.deleted_doc_ids == [("doc-s3", False)]
    assert local_namesake.read_bytes() == b"local file owned by a different flow"
    assert await store.list_keys("lightrag/uploads/ws/upload-1/") == []
    assert await store.list_keys("lightrag/artifacts/ws/doc-s3/") == []

class _ClearObjectStorage:
    def __init__(self, workspace, *, docs=None, drop_result=None):
        self.workspace = workspace
        self.namespace = "clear-object"
        self.docs = docs or {}
        self._drop_result = drop_result or {"status": "success", "message": "dropped"}

    async def drop(self):
        return self._drop_result

    async def initialize(self):
        return None

    async def get_docs_by_statuses(self, statuses, strict=False):
        return self.docs


class _ObjectBackedClearRag:
    def __init__(self, *, workspace, object_store, docs):
        self.workspace = workspace
        storage = _ClearObjectStorage(workspace)
        self.text_chunks = storage
        self.full_docs = storage
        self.full_entities = storage
        self.full_relations = storage
        self.entity_chunks = storage
        self.relation_chunks = storage
        self.entities_vdb = storage
        self.relationships_vdb = storage
        self.chunks_vdb = storage
        self.chunk_entity_relation_graph = storage
        self.doc_status = _ClearObjectStorage(workspace, docs=docs)
        self.object_store = object_store

    async def aclear_cache(self, modes=None):
        return None


def _clear_endpoint(rag, input_dir):
    from lightrag.api.routers.document_routes import DocumentManager, create_document_routes

    router = create_document_routes(rag, DocumentManager(str(input_dir)))
    return [
        route.endpoint
        for route in router.routes
        if getattr(route, "name", "") == "clear_documents"
    ][-1]


@pytest.mark.asyncio
async def test_clear_documents_cleans_object_sources_and_preserves_artifacts_by_default(tmp_path):
    from types import SimpleNamespace
    from uuid import uuid4

    from lightrag.base import DocStatus
    from lightrag.kg import shared_storage

    workspace = f"clear-object-{uuid4().hex}"
    store = FakeObjectStore(ObjectStoreConfig(provider="fake", bucket="docs"))
    await store.put_bytes("lightrag/uploads/ws/upload-1/report.pdf", b"source")
    await store.put_bytes("lightrag/artifacts/ws/doc-s3/report.blocks.jsonl", b"{}\n")
    object_source = {
        "source_kind": "s3_object",
        "bucket": "docs",
        "object_key": "lightrag/uploads/ws/upload-1/report.pdf",
        "artifact_prefix": "lightrag/artifacts/ws/doc-s3",
    }
    rag = _ObjectBackedClearRag(
        workspace=workspace,
        object_store=store,
        docs={
            "doc-s3": SimpleNamespace(
                status=DocStatus.PROCESSED,
                metadata={"object_source": object_source},
            )
        },
    )
    shared_storage.initialize_share_data()
    await shared_storage.initialize_pipeline_status(workspace=workspace)

    response = await _clear_endpoint(rag, tmp_path)()

    assert response.status == "success"
    assert await store.list_keys("lightrag/uploads/ws/upload-1/") == []
    assert await store.get_bytes("lightrag/artifacts/ws/doc-s3/report.blocks.jsonl") == b"{}\n"


@pytest.mark.asyncio
async def test_clear_documents_deletes_object_artifacts_when_parsed_files_are_requested(tmp_path):
    from types import SimpleNamespace
    from uuid import uuid4

    from lightrag.base import DocStatus
    from lightrag.kg import shared_storage

    workspace = f"clear-object-artifacts-{uuid4().hex}"
    store = FakeObjectStore(ObjectStoreConfig(provider="fake", bucket="docs"))
    await store.put_bytes("lightrag/uploads/ws/upload-1/report.pdf", b"source")
    await store.put_bytes("lightrag/artifacts/ws/doc-s3/report.blocks.jsonl", b"{}\n")
    object_source = {
        "source_kind": "s3_object",
        "bucket": "docs",
        "object_key": "lightrag/uploads/ws/upload-1/report.pdf",
        "artifact_prefix": "lightrag/artifacts/ws/doc-s3",
    }
    rag = _ObjectBackedClearRag(
        workspace=workspace,
        object_store=store,
        docs={
            "doc-s3": SimpleNamespace(
                status=DocStatus.PROCESSED,
                metadata={"object_source": object_source},
            )
        },
    )
    shared_storage.initialize_share_data()
    await shared_storage.initialize_pipeline_status(workspace=workspace)

    response = await _clear_endpoint(rag, tmp_path)(delete_parsed_files=True)

    assert response.status == "success"
    assert await store.list_keys("lightrag/uploads/ws/upload-1/") == []
    assert await store.list_keys("lightrag/artifacts/ws/doc-s3/") == []

@pytest.mark.asyncio
async def test_clear_documents_reports_object_cleanup_failures(tmp_path):
    from types import SimpleNamespace
    from uuid import uuid4

    from lightrag.base import DocStatus
    from lightrag.kg import shared_storage

    workspace = f"clear-object-fail-{uuid4().hex}"
    object_source = {
        "source_kind": "s3_object",
        "bucket": "docs",
        "object_key": "lightrag/uploads/ws/upload-1/report.pdf",
        "artifact_prefix": "lightrag/artifacts/ws/doc-s3",
    }
    rag = _ObjectBackedClearRag(
        workspace=workspace,
        object_store=None,
        docs={
            "doc-s3": SimpleNamespace(
                status=DocStatus.PROCESSED,
                metadata={"object_source": object_source},
            )
        },
    )
    shared_storage.initialize_share_data()
    await shared_storage.initialize_pipeline_status(workspace=workspace)

    response = await _clear_endpoint(rag, tmp_path)(delete_parsed_files=True)

    assert response.status == "partial_success"
    assert "object-store cleanup failed" in response.message

@pytest.mark.asyncio
async def test_delete_object_source_objects_accepts_default_upload_prefix():
    store = FakeObjectStore(ObjectStoreConfig(provider="fake", bucket="docs"))
    await store.put_bytes("uploads/ws/upload-1/report.pdf", b"source")

    deleted, errors = await _dr.delete_object_source_objects(
        store,
        {
            "source_kind": "s3_object",
            "bucket": "docs",
            "object_key": "uploads/ws/upload-1/report.pdf",
        },
    )

    assert errors == []
    assert deleted == ["s3://docs/uploads/ws/upload-1/report.pdf"]
    assert await store.list_keys("uploads/ws/upload-1/") == []

@pytest.mark.asyncio
async def test_delete_object_source_objects_refuses_upload_prefix_lookalike():
    store = FakeObjectStore(ObjectStoreConfig(provider="fake", bucket="docs"))
    await store.put_bytes("uploads2/ws/upload-1/report.pdf", b"keep")

    deleted, errors = await _dr.delete_object_source_objects(
        store,
        {
            "source_kind": "s3_object",
            "bucket": "docs",
            "object_key": "uploads2/ws/upload-1/report.pdf",
        },
    )

    assert deleted == []
    assert errors == ["Refused to delete object source outside owned upload prefix"]
    assert await store.get_bytes("uploads2/ws/upload-1/report.pdf") == b"keep"
