"""PostgreSQL full_docs stores object-source metadata for object-backed parsing."""

from __future__ import annotations

import pytest
from unittest.mock import AsyncMock, MagicMock

from lightrag.kg.postgres_impl import PGKVStorage, SQL_TEMPLATES, TABLES
from lightrag.namespace import NameSpace

pytestmark = pytest.mark.offline


def test_doc_full_schema_and_upsert_include_object_source_metadata():
    ddl = TABLES["LIGHTRAG_DOC_FULL"]["ddl"]
    assert "object_source JSONB" in ddl
    assert "object_source" in SQL_TEMPLATES["get_by_id_full_docs"]
    assert "object_source" in SQL_TEMPLATES["get_by_ids_full_docs"]
    upsert = SQL_TEMPLATES["upsert_doc_full"]
    assert "object_source" in upsert
    assert "EXCLUDED.object_source" in upsert


@pytest.mark.asyncio
async def test_full_docs_reads_treat_empty_object_source_as_absent():
    db = MagicMock()
    db.workspace = "test_ws"
    db.query = AsyncMock(
        side_effect=[
            {
                "id": "doc-local",
                "content": "",
                "file_path": "report.pdf",
                "chunk_options": "{}",
                "object_source": "{}",
            },
            [
                {
                    "id": "doc-local",
                    "content": "",
                    "file_path": "report.pdf",
                    "chunk_options": {},
                    "object_source": {},
                }
            ],
        ]
    )
    storage = PGKVStorage.__new__(PGKVStorage)
    storage.namespace = NameSpace.KV_STORE_FULL_DOCS
    storage.workspace = "test_ws"
    storage.global_config = {}
    storage.db = db
    storage.__post_init__()

    single = await storage.get_by_id("doc-local")
    batch = await storage.get_by_ids(["doc-local"])

    assert "object_source" not in single
    assert "object_source" not in batch[0]
