"""Object-backed documents preserve source metadata without changing file_path."""

from __future__ import annotations

import sys
import time
import types
from uuid import uuid4

import numpy as np
import pytest
from lightrag import LightRAG
from lightrag.base import DocStatus
from lightrag.constants import FULL_DOCS_FORMAT_PENDING_PARSE, FULL_DOCS_FORMAT_RAW
from lightrag.constants import FULL_DOCS_FORMAT_LIGHTRAG
from lightrag.object_storage import FakeObjectStore, ObjectStoreConfig
from lightrag.parser import registry
from lightrag.parser.base import ParseResult
from lightrag.utils import EmbeddingFunc, compute_mdhash_id
from lightrag.utils import Tokenizer
from lightrag.utils_pipeline import make_lightrag_doc_content, sidecar_uri_for
from lightrag.utils_pipeline import doc_status_reset_metadata
from lightrag.utils_pipeline import doc_status_transition_metadata

from .conftest import request_failed_retry

pytestmark = pytest.mark.offline


def test_object_source_metadata_survives_status_transition_for_cleanup_and_retry():
    object_source = {
        "source_kind": "s3_object",
        "bucket": "docs",
        "object_key": "lightrag/uploads/ws/upload_1/report.txt",
        "upload_id": "upload_1",
    }

    metadata = doc_status_transition_metadata(
        {
            "metadata": {
                "source_file": "report.[native].txt",
                "process_options": "F",
                "object_source": object_source,
                "parse_warnings": ["stale warning"],
            }
        },
        extra={"parse_engine": "native"},
    )

    assert metadata["object_source"] == object_source
    assert metadata["source_file"] == "report.[native].txt"
    assert metadata["parse_warnings"] == ["stale warning"]
    assert metadata["parse_engine"] == "native"


def test_object_source_metadata_survives_failed_retry_reset():
    object_source = {
        "source_kind": "s3_object",
        "bucket": "docs",
        "object_key": "lightrag/uploads/ws/upload_1/report.txt",
        "upload_id": "upload_1",
    }

    metadata = doc_status_reset_metadata(
        {
            "metadata": {
                "source_file": "report.[native].txt",
                "process_options": "F",
                "object_source": object_source,
                "parse_warnings": ["stale warning"],
                "error_stage": "parse",
            }
        }
    )

    assert metadata == {
        "process_options": "F",
        "source_file": "report.[native].txt",
        "object_source": object_source,
    }


class _SimpleTokenizerImpl:
    def encode(self, text: str):
        return list(text.encode("utf-8"))

    def decode(self, tokens):
        return bytes(tokens).decode("utf-8", errors="ignore")


async def _dummy_llm(*args, **kwargs) -> str:
    return "ok"


async def _dummy_embedding(texts):
    return np.ones((len(texts), 8), dtype=float)


def _deterministic_chunking(
    tokenizer,
    content: str,
    split_by_character,
    split_by_character_only: bool,
    chunk_overlap_token_size: int,
    chunk_token_size: int,
) -> list[dict]:
    return [
        {"tokens": 1, "content": f"{content}::chunk1", "chunk_order_index": 0},
    ]


async def _build_rag(tmp_path, *, input_dir=None) -> LightRAG:
    rag = LightRAG(
        working_dir=str(tmp_path / "wd" / uuid4().hex),
        workspace=f"object-source-{uuid4().hex[:8]}",
        distributed_input_dir=str(input_dir) if input_dir is not None else None,
        llm_model_func=_dummy_llm,
        embedding_func=EmbeddingFunc(
            embedding_dim=8, max_token_size=8192, func=_dummy_embedding
        ),
        tokenizer=Tokenizer("mock-tokenizer", _SimpleTokenizerImpl()),
        chunking_func=_deterministic_chunking,
        max_parallel_insert=1,
    )
    await rag.initialize_storages()
    return rag


class _ObjectEchoParser:
    engine_name = "objectecho"
    seen_source_path = None

    async def parse(self, ctx):
        source_path = ctx.source_path(self.engine_name)
        _ObjectEchoParser.seen_source_path = source_path
        if not source_path.is_file():
            raise FileNotFoundError(f"source file not found: {source_path}")
        content = source_path.read_text(encoding="utf-8")
        await ctx.rag._persist_parsed_full_docs(
            ctx.doc_id,
            {
                "content": content,
                "file_path": ctx.file_path,
                "parse_format": FULL_DOCS_FORMAT_RAW,
                "parse_engine": self.engine_name,
                "update_time": int(time.time()),
            },
        )
        return ParseResult(
            doc_id=ctx.doc_id,
            file_path=ctx.file_path,
            parse_format=FULL_DOCS_FORMAT_RAW,
            content=content,
            parse_engine=self.engine_name,
        )


class _ObjectSidecarParser:
    engine_name = "objectsidecar"

    async def parse(self, ctx):
        rs = ctx.resolve(self.engine_name)
        source_path = rs.source_path
        if not source_path.is_file():
            raise FileNotFoundError(f"source file not found: {source_path}")
        content = source_path.read_text(encoding="utf-8")
        rs.parsed_dir.mkdir(parents=True, exist_ok=True)
        blocks_path = rs.parsed_dir / "report.blocks.jsonl"
        blocks_path.write_text(
            '{"type":"meta","doc_id":"%s"}\n'
            '{"type":"content","content":"%s"}\n' % (ctx.doc_id, content),
            encoding="utf-8",
        )
        await ctx.rag._persist_parsed_full_docs(
            ctx.doc_id,
            {
                "content": make_lightrag_doc_content(content),
                "file_path": ctx.file_path,
                "parse_format": FULL_DOCS_FORMAT_LIGHTRAG,
                "sidecar_location": sidecar_uri_for(rs.parsed_dir),
                "parse_engine": self.engine_name,
                "update_time": int(time.time()),
            },
        )
        return ParseResult(
            doc_id=ctx.doc_id,
            file_path=ctx.file_path,
            parse_format=FULL_DOCS_FORMAT_LIGHTRAG,
            content=content,
            blocks_path=str(blocks_path),
            parse_engine=self.engine_name,
        )


@pytest.fixture
def object_echo_parser(monkeypatch):
    fake_mod = types.ModuleType("_test_object_echo_parser_mod")
    fake_mod.ObjectEchoParser = _ObjectEchoParser
    monkeypatch.setitem(sys.modules, "_test_object_echo_parser_mod", fake_mod)
    registry.register_parser(
        registry.ParserSpec(
            engine_name="objectecho",
            impl="_test_object_echo_parser_mod:ObjectEchoParser",
            suffixes=frozenset({"txt"}),
            queue_group="native",
        )
    )
    _ObjectEchoParser.seen_source_path = None
    try:
        yield _ObjectEchoParser
    finally:
        registry._REGISTRY.pop("objectecho", None)
        registry._INSTANCE_CACHE.pop(
            ("objectecho", "_test_object_echo_parser_mod:ObjectEchoParser"),
            None,
        )


@pytest.fixture
def object_sidecar_parser(monkeypatch):
    fake_mod = types.ModuleType("_test_object_sidecar_parser_mod")
    fake_mod.ObjectSidecarParser = _ObjectSidecarParser
    monkeypatch.setitem(sys.modules, "_test_object_sidecar_parser_mod", fake_mod)
    registry.register_parser(
        registry.ParserSpec(
            engine_name="objectsidecar",
            impl="_test_object_sidecar_parser_mod:ObjectSidecarParser",
            suffixes=frozenset({"txt"}),
            queue_group="native",
        )
    )
    try:
        yield _ObjectSidecarParser
    finally:
        registry._REGISTRY.pop("objectsidecar", None)
        registry._INSTANCE_CACHE.pop(
            ("objectsidecar", "_test_object_sidecar_parser_mod:ObjectSidecarParser"),
            None,
        )


@pytest.mark.asyncio
async def test_pending_parse_object_source_is_recorded_without_replacing_file_path(tmp_path):
    rag = await _build_rag(tmp_path)
    try:
        object_source = {
            "source_kind": "s3_object",
            "bucket": "docs",
            "object_key": "lightrag/uploads/ws/upload_1/report.pdf",
            "etag": "etag-1",
            "size": 5,
            "content_type": "application/pdf",
            "checksum_sha256": "a" * 64,
            "upload_id": "upload_1",
        }

        await rag.apipeline_enqueue_documents(
            "",
            file_paths="report.pdf",
            track_id="track-object",
            docs_format=FULL_DOCS_FORMAT_PENDING_PARSE,
            parse_engine="legacy",
            object_source=object_source,
        )

        doc_id = compute_mdhash_id("report.pdf", prefix="doc-")
        full_doc = await rag.full_docs.get_by_id(doc_id)
        status_doc = await rag.doc_status.get_by_id(doc_id)

        assert full_doc["file_path"] == "report.pdf"
        assert full_doc["object_source"] == object_source
        assert status_doc["file_path"] == "report.pdf"
        assert status_doc["metadata"]["source_kind"] == "s3_object"
        assert status_doc["metadata"]["object_source"] == object_source
    finally:
        await rag.finalize_storages()


@pytest.mark.asyncio
async def test_object_backed_parse_uploads_sidecar_artifacts_to_object_store(
    tmp_path, monkeypatch, object_sidecar_parser
):
    input_dir = tmp_path / "inputs"
    input_dir.mkdir()
    monkeypatch.setenv("INPUT_DIR", str(input_dir))
    object_key = "lightrag/uploads/tenant_a/upload_1/report.txt"
    object_store = FakeObjectStore(
        ObjectStoreConfig(
            provider="fake",
            bucket="docs",
            object_prefix="lightrag",
            scratch_dir=str(tmp_path / "scratch"),
        )
    )
    await object_store.put_bytes(
        object_key,
        b"sidecar body",
        content_type="text/plain",
    )
    rag = await _build_rag(tmp_path, input_dir=input_dir)
    rag.object_store = object_store
    rag.object_storage_scratch_dir = str(tmp_path / "scratch")
    try:
        await rag.apipeline_enqueue_documents(
            "",
            file_paths="report.txt",
            track_id="track-object-sidecar",
            docs_format=FULL_DOCS_FORMAT_PENDING_PARSE,
            parse_engine="objectsidecar",
            object_source={
                "source_kind": "s3_object",
                "bucket": "docs",
                "object_key": object_key,
                "size": len(b"sidecar body"),
                "content_type": "text/plain",
                "checksum_sha256": "fe4d3c6aee207ff3b9ffbf26da6464a71d208aab411caf3d3805795b20832638",
                "upload_id": "upload_1",
            },
        )

        await rag.apipeline_process_enqueue_documents()

        doc_id = compute_mdhash_id("report.txt", prefix="doc-")
        full_doc = await rag.full_docs.get_by_id(doc_id)
        artifact_prefix = f"lightrag/artifacts/{rag.workspace}/{doc_id}/"
        assert full_doc["sidecar_location"] == f"s3://docs/{artifact_prefix}"
        assert full_doc["object_source"]["artifact_prefix"] == artifact_prefix.rstrip("/")
        assert await object_store.list_keys(artifact_prefix) == [
            f"{artifact_prefix}report.blocks.jsonl"
        ]
    finally:
        await rag.finalize_storages()


@pytest.mark.asyncio
async def test_local_pending_parse_keeps_existing_metadata_shape(tmp_path):
    rag = await _build_rag(tmp_path)
    try:
        await rag.apipeline_enqueue_documents(
            "",
            file_paths="report.pdf",
            track_id="track-local",
            docs_format=FULL_DOCS_FORMAT_PENDING_PARSE,
            parse_engine="legacy",
        )

        doc_id = compute_mdhash_id("report.pdf", prefix="doc-")
        full_doc = await rag.full_docs.get_by_id(doc_id)
        status_doc = await rag.doc_status.get_by_id(doc_id)

        assert "object_source" not in full_doc
        assert "object_source" not in status_doc.get("metadata", {})
        assert status_doc["metadata"]["source_file"] == "report.pdf"
    finally:
        await rag.finalize_storages()


@pytest.mark.asyncio
async def test_object_backed_pending_parse_downloads_source_to_scratch(
    tmp_path, monkeypatch, object_echo_parser
):
    input_dir = tmp_path / "inputs"
    input_dir.mkdir()
    monkeypatch.setenv("INPUT_DIR", str(input_dir))
    object_key = "lightrag/uploads/tenant_a/upload_1/report.txt"
    object_store = FakeObjectStore(
        ObjectStoreConfig(provider="fake", bucket="docs", scratch_dir=str(tmp_path / "scratch"))
    )
    await object_store.put_bytes(
        object_key,
        b"object-backed body",
        content_type="text/plain",
    )
    rag = await _build_rag(tmp_path, input_dir=input_dir)
    rag.object_store = object_store
    rag.object_storage_scratch_dir = str(tmp_path / "scratch")
    try:
        await rag.apipeline_enqueue_documents(
            "",
            file_paths="report.txt",
            track_id="track-object-download",
            docs_format=FULL_DOCS_FORMAT_PENDING_PARSE,
            parse_engine="objectecho",
            object_source={
                "source_kind": "s3_object",
                "bucket": "docs",
                "object_key": object_key,
                "size": len(b"object-backed body"),
                "content_type": "text/plain",
                "checksum_sha256": "d32c30cf788c16b2fdeed9688f3d760b5bdbb00ebcd7090faa1d478776a1b2aa",
                "upload_id": "upload_1",
            },
        )

        await rag.apipeline_process_enqueue_documents()

        doc_id = compute_mdhash_id("report.txt", prefix="doc-")
        status_doc = await rag.doc_status.get_by_id(doc_id)
        full_doc = await rag.full_docs.get_by_id(doc_id)
        source_path = object_echo_parser.seen_source_path
        assert status_doc["status"] == DocStatus.PROCESSED
        assert full_doc["content"] == "object-backed body"
        assert source_path is not None
        assert tmp_path / "scratch" in source_path.parents
        assert source_path.name == "report.txt"
        assert not (input_dir / "report.txt").exists()
    finally:
        await rag.finalize_storages()


@pytest.mark.asyncio
async def test_object_backed_legacy_parser_reads_scratch_source_without_input_dir(
    tmp_path, monkeypatch
):
    input_dir = tmp_path / "inputs"
    input_dir.mkdir()
    monkeypatch.setenv("INPUT_DIR", str(input_dir))
    object_key = "lightrag/uploads/tenant_a/upload_1/legacy.txt"
    payload = b"legacy parser object-backed body"
    object_store = FakeObjectStore(
        ObjectStoreConfig(provider="fake", bucket="docs", scratch_dir=str(tmp_path / "scratch"))
    )
    await object_store.put_bytes(object_key, payload, content_type="text/plain")
    rag = await _build_rag(tmp_path, input_dir=input_dir)
    rag.object_store = object_store
    rag.object_storage_scratch_dir = str(tmp_path / "scratch")
    try:
        await rag.apipeline_enqueue_documents(
            "",
            file_paths="legacy.txt",
            track_id="track-object-legacy",
            docs_format=FULL_DOCS_FORMAT_PENDING_PARSE,
            parse_engine="legacy",
            object_source={
                "source_kind": "s3_object",
                "bucket": "docs",
                "object_key": object_key,
                "size": len(payload),
                "content_type": "text/plain",
                "checksum_sha256": "59d38dbe392ca1111d93325d74f6983b10ff329418b271b384f2b2f7b144173d",
                "upload_id": "upload_1",
            },
        )

        await rag.apipeline_process_enqueue_documents()

        doc_id = compute_mdhash_id("legacy.txt", prefix="doc-")
        status_doc = await rag.doc_status.get_by_id(doc_id)
        full_doc = await rag.full_docs.get_by_id(doc_id)
        assert status_doc["status"] == DocStatus.PROCESSED
        assert full_doc["content"] == payload.decode()
        assert full_doc["object_source"]["object_key"] == object_key
        assert not (input_dir / "legacy.txt").exists()
    finally:
        await rag.finalize_storages()


@pytest.mark.asyncio
async def test_object_backed_failed_retry_reuses_recorded_object_source(
    tmp_path, monkeypatch
):
    input_dir = tmp_path / "inputs"
    input_dir.mkdir()
    monkeypatch.setenv("INPUT_DIR", str(input_dir))
    object_key = "lightrag/uploads/tenant_a/upload_1/retry.txt"
    failed_payload = b" " * 64
    recovered_payload = (b"object-backed retry recovered" + b" " * 64)[:64]
    object_store = FakeObjectStore(
        ObjectStoreConfig(provider="fake", bucket="docs", scratch_dir=str(tmp_path / "scratch"))
    )
    await object_store.put_bytes(object_key, failed_payload, content_type="text/plain")
    rag = await _build_rag(tmp_path, input_dir=input_dir)
    rag.object_store = object_store
    rag.object_storage_scratch_dir = str(tmp_path / "scratch")
    try:
        await rag.apipeline_enqueue_documents(
            "",
            file_paths="retry.txt",
            track_id="track-object-retry",
            docs_format=FULL_DOCS_FORMAT_PENDING_PARSE,
            parse_engine="legacy",
            object_source={
                "source_kind": "s3_object",
                "bucket": "docs",
                "object_key": object_key,
                "size": len(failed_payload),
                "content_type": "text/plain",
                "upload_id": "upload_1",
            },
        )
        await rag.apipeline_process_enqueue_documents()

        doc_id = compute_mdhash_id("retry.txt", prefix="doc-")
        failed_status = await rag.doc_status.get_by_id(doc_id)
        assert failed_status["status"] == DocStatus.FAILED
        assert failed_status["metadata"]["object_source"]["object_key"] == object_key

        await object_store.put_bytes(
            object_key, recovered_payload, content_type="text/plain"
        )
        await request_failed_retry(rag)
        await rag.apipeline_process_enqueue_documents()

        retried_status = await rag.doc_status.get_by_id(doc_id)
        full_doc = await rag.full_docs.get_by_id(doc_id)
        assert retried_status["status"] == DocStatus.PROCESSED
        assert full_doc["content"] == recovered_payload.decode()
        assert full_doc["object_source"]["object_key"] == object_key
        assert not (input_dir / "retry.txt").exists()
    finally:
        await rag.finalize_storages()


@pytest.mark.asyncio
async def test_object_backed_native_markdown_parser_uses_scratch_and_remote_sidecar(
    tmp_path, monkeypatch
):
    input_dir = tmp_path / "inputs"
    input_dir.mkdir()
    monkeypatch.setenv("INPUT_DIR", str(input_dir))
    object_key = "lightrag/uploads/tenant_a/upload_1/native.md"
    payload = b"# Heading\n\nnative parser object-backed body\n"
    object_store = FakeObjectStore(
        ObjectStoreConfig(
            provider="fake",
            bucket="docs",
            object_prefix="lightrag",
            scratch_dir=str(tmp_path / "scratch"),
        )
    )
    await object_store.put_bytes(object_key, payload, content_type="text/markdown")
    rag = await _build_rag(tmp_path, input_dir=input_dir)
    rag.object_store = object_store
    rag.object_storage_scratch_dir = str(tmp_path / "scratch")
    try:
        await rag.apipeline_enqueue_documents(
            "",
            file_paths="native.md",
            track_id="track-object-native",
            docs_format=FULL_DOCS_FORMAT_PENDING_PARSE,
            parse_engine="native",
            object_source={
                "source_kind": "s3_object",
                "bucket": "docs",
                "object_key": object_key,
                "size": len(payload),
                "content_type": "text/markdown",
                "checksum_sha256": "0d709fe9703d57689bb3cc19617398230f69f9fed894c5a02176729fdfee0ae3",
                "upload_id": "upload_1",
            },
        )

        await rag.apipeline_process_enqueue_documents()

        doc_id = compute_mdhash_id("native.md", prefix="doc-")
        status_doc = await rag.doc_status.get_by_id(doc_id)
        full_doc = await rag.full_docs.get_by_id(doc_id)
        artifact_prefix = f"lightrag/artifacts/{rag.workspace}/{doc_id}/"
        assert status_doc["status"] == DocStatus.PROCESSED
        assert "native parser object-backed body" in full_doc["content"]
        assert full_doc["sidecar_location"] == f"s3://docs/{artifact_prefix}"
        assert await object_store.list_keys(artifact_prefix)
        assert not (input_dir / "native.md").exists()
    finally:
        await rag.finalize_storages()


@pytest.mark.asyncio
async def test_object_backed_pending_parse_checksum_mismatch_fails_before_parser(
    tmp_path, monkeypatch, object_echo_parser
):
    input_dir = tmp_path / "inputs"
    input_dir.mkdir()
    monkeypatch.setenv("INPUT_DIR", str(input_dir))
    object_key = "lightrag/uploads/tenant_a/upload_1/report.txt"
    object_store = FakeObjectStore(
        ObjectStoreConfig(provider="fake", bucket="docs", scratch_dir=str(tmp_path / "scratch"))
    )
    await object_store.put_bytes(
        object_key,
        b"tampered body",
        content_type="text/plain",
    )
    rag = await _build_rag(tmp_path, input_dir=input_dir)
    rag.object_store = object_store
    rag.object_storage_scratch_dir = str(tmp_path / "scratch")
    try:
        await rag.apipeline_enqueue_documents(
            "",
            file_paths="report.txt",
            track_id="track-object-checksum",
            docs_format=FULL_DOCS_FORMAT_PENDING_PARSE,
            parse_engine="objectecho",
            object_source={
                "source_kind": "s3_object",
                "bucket": "docs",
                "object_key": object_key,
                "size": len(b"tampered body"),
                "content_type": "text/plain",
                "checksum_sha256": "0" * 64,
                "upload_id": "upload_1",
            },
        )

        await rag.apipeline_process_enqueue_documents()

        doc_id = compute_mdhash_id("report.txt", prefix="doc-")
        status_doc = await rag.doc_status.get_by_id(doc_id)
        assert status_doc["status"] == DocStatus.FAILED
        assert "checksum" in status_doc["error_msg"].lower()
        assert object_echo_parser.seen_source_path is None
    finally:
        await rag.finalize_storages()
