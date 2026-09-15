"""PostgreSQL KV storage supports the upload_sessions namespace."""

from __future__ import annotations

import pytest

from lightrag.kg.postgres_impl import SQL_TEMPLATES, TABLES, namespace_to_table_name
from lightrag.namespace import NameSpace

pytestmark = pytest.mark.offline


def test_upload_sessions_namespace_has_table_and_sql_templates():
    assert NameSpace.KV_STORE_UPLOAD_SESSIONS == "upload_sessions"
    assert namespace_to_table_name(NameSpace.KV_STORE_UPLOAD_SESSIONS) == "LIGHTRAG_UPLOAD_SESSIONS"
    assert "LIGHTRAG_UPLOAD_SESSIONS" in TABLES
    assert "session JSONB" in TABLES["LIGHTRAG_UPLOAD_SESSIONS"]["ddl"]
    for template_name in (
        "get_by_id_upload_sessions",
        "get_by_ids_upload_sessions",
        "upsert_upload_session",
    ):
        assert template_name in SQL_TEMPLATES
