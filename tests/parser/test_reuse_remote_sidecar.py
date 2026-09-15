"""Reuse parser can mirror object-backed sidecars into local scratch."""

from __future__ import annotations

from pathlib import Path

import pytest

from lightrag.constants import FULL_DOCS_FORMAT_LIGHTRAG
from lightrag.object_storage import FakeObjectStore, ObjectStoreConfig
from lightrag.parser.base import ParseContext
from lightrag.parser.external._base import ExternalParserBase
from lightrag.parser.noop import ReuseParser
from lightrag.pipeline import _PipelineMixin
from lightrag.sidecar.ir import IRBlock, IRDoc
from lightrag.utils_pipeline import make_lightrag_doc_content

pytestmark = pytest.mark.offline


class _RagWithRemoteSidecar:
    def __init__(self, store, scratch_dir: Path):
        self.object_store = store
        self.object_storage_scratch_dir = str(scratch_dir)
        self.workspace = "tenant_a"


class _RagWithObjectResolver(_PipelineMixin):
    def __init__(self, store, scratch_dir: Path):
        self.object_store = store
        self.object_storage_scratch_dir = str(scratch_dir)
        self.working_dir = str(scratch_dir.parent)
        self.workspace = "tenant_a"
        self.persisted = {}

    async def _persist_parsed_full_docs(self, doc_id: str, record: dict):
        self.persisted[doc_id] = record


class _ExternalObjectSmokeParser(ExternalParserBase):
    engine_name = "external-object-smoke"
    raw_dir_suffix = ".external_smoke_raw"
    force_reparse_env = "LIGHTRAG_EXTERNAL_OBJECT_SMOKE_FORCE_REPARSE"

    def __init__(self):
        self.seen_source_path: Path | None = None

    def is_bundle_valid(self, raw_dir, source_path, *, engine_params=None):
        return False

    async def download_into(
        self, raw_dir, source_path, *, upload_name, engine_params=None
    ):
        self.seen_source_path = source_path
        raw_dir.mkdir(parents=True, exist_ok=True)
        (raw_dir / "content.txt").write_text(
            source_path.read_text(encoding="utf-8"), encoding="utf-8"
        )

    def build_ir(self, raw_dir, document_name):
        return IRDoc(
            document_name=document_name,
            document_format="txt",
            doc_title=document_name,
            split_option={},
            blocks=[
                IRBlock(
                    content_template=(raw_dir / "content.txt").read_text(
                        encoding="utf-8"
                    )
                )
            ],
        )


@pytest.mark.asyncio
async def test_reuse_parser_downloads_remote_sidecar_before_returning_blocks_path(
    tmp_path,
):
    object_store = FakeObjectStore(
        ObjectStoreConfig(provider="fake", bucket="docs", scratch_dir=str(tmp_path))
    )
    prefix = "lightrag/artifacts/tenant_a/doc-1"
    await object_store.put_bytes(
        f"{prefix}/report.blocks.jsonl",
        b'{"type":"meta"}\n{"type":"content","content":"hello"}\n',
        content_type="application/jsonl",
    )
    rag = _RagWithRemoteSidecar(object_store, tmp_path / "scratch")
    content_data = {
        "content": make_lightrag_doc_content("hello"),
        "parse_format": FULL_DOCS_FORMAT_LIGHTRAG,
        "sidecar_location": f"s3://docs/{prefix}/",
        "object_source": {
            "source_kind": "s3_object",
            "bucket": "docs",
            "artifact_prefix": prefix,
        },
    }

    result = await ReuseParser().parse(
        ParseContext(rag, "doc-1", "report.txt", content_data)
    )

    assert result.blocks_path
    blocks_path = Path(result.blocks_path)
    assert blocks_path.is_file()
    assert tmp_path / "scratch" in blocks_path.parents
    assert blocks_path.read_text(encoding="utf-8").splitlines()[1].endswith('"hello"}')


@pytest.mark.asyncio
async def test_external_parser_template_reads_object_backed_scratch_source(tmp_path):
    object_store = FakeObjectStore(
        ObjectStoreConfig(provider="fake", bucket="docs", scratch_dir=str(tmp_path))
    )
    object_key = "lightrag/uploads/tenant_a/upload_1/external.txt"
    await object_store.put_bytes(
        object_key,
        b"external parser object-backed body",
        content_type="text/plain",
    )
    rag = _RagWithObjectResolver(object_store, tmp_path / "scratch")
    content_data = {
        "parse_engine": "external-object-smoke",
        "object_source": {
            "source_kind": "s3_object",
            "bucket": "docs",
            "object_key": object_key,
            "size": len(b"external parser object-backed body"),
            "content_type": "text/plain",
            "checksum_sha256": "7476dda046e93df75533f4024c6108e47be5c74ec201e8ecb506f617706fe422",
            "upload_id": "upload_1",
        },
    }

    await rag._prepare_object_source_for_parse("doc-external", "external.txt", content_data)
    parser = _ExternalObjectSmokeParser()
    result = await parser.parse(ParseContext(rag, "doc-external", "external.txt", content_data))

    assert result.content == "external parser object-backed body"
    assert parser.seen_source_path is not None
    assert tmp_path / "scratch" in parser.seen_source_path.parents
    assert parser.seen_source_path.name == "external.txt"
    assert rag.persisted["doc-external"]["parse_engine"] == "external-object-smoke"
