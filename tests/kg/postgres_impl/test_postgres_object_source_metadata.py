"""PostgreSQL full_docs stores object-source metadata for object-backed parsing."""

from __future__ import annotations

import pytest

from lightrag.kg.postgres_impl import SQL_TEMPLATES, TABLES

pytestmark = pytest.mark.offline


def test_doc_full_schema_and_upsert_include_object_source_metadata():
    ddl = TABLES["LIGHTRAG_DOC_FULL"]["ddl"]
    assert "object_source JSONB" in ddl
    assert "object_source" in SQL_TEMPLATES["get_by_id_full_docs"]
    assert "object_source" in SQL_TEMPLATES["get_by_ids_full_docs"]
    upsert = SQL_TEMPLATES["upsert_doc_full"]
    assert "object_source" in upsert
    assert "EXCLUDED.object_source" in upsert
