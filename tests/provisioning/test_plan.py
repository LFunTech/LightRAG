import importlib.util
import json

import pytest

from tests.ci.test_release_profile import profile_env


def environment():
    return dict(
        profile_env(), WORKING_DIR="/app/data/rag_storage", INPUT_DIR="/app/data/inputs"
    )


def build(values=None):
    assert importlib.util.find_spec("lightrag.provisioning"), (
        "Versioned storage provisioning is missing"
    )
    from lightrag.provisioning.plan import build_plan

    return build_plan(environment() if values is None else values)


def test_offline_plan_matches_runtime_vector_names_without_credentials():
    values = dict(environment(), POSTGRES_PASSWORD="synthetic-must-not-appear")
    plan = build(values)
    assert "lightrag_vdb_chunks_fixture_8d" in plan.tables
    assert len(plan.tables) == 12
    assert len(plan.graph) == 7
    assert len(plan.profile_hash) == len(plan.checksum) == 64
    report = json.dumps(plan.summary())
    assert "synthetic-must-not-appear" not in report
    assert "CREATE TABLE public.lightrag_doc_status" in "\n".join(plan.statements)
    assert "USING hnsw (content_vector vector_cosine_ops)" in "\n".join(plan.statements)


@pytest.mark.parametrize(
    "field,value",
    [
        ("EMBEDDING_DIM", "0"),
        ("EMBEDDING_DIM", "2001"),
        ("EMBEDDING_MODEL", ""),
        ("EMBEDDING_MODEL", "x" * 80),
        ("POSTGRES_VECTOR_INDEX_TYPE", "IVFFLAT"),
        ("POSTGRES_HNSW_M", "1"),
        ("POSTGRES_HNSW_EF", "1"),
    ],
)
def test_plan_rejects_unsupported_profile_before_any_connection(field, value):
    with pytest.raises(ValueError):
        build(dict(environment(), **{field: value}))


def test_halfvec_is_explicit_and_dimension_bound():
    plan = build(
        dict(
            environment(),
            EMBEDDING_DIM="3072",
            POSTGRES_VECTOR_INDEX_TYPE="HNSW_HALFVEC",
        )
    )
    assert "halfvec(3072)" in "\n".join(plan.statements).lower()
    assert "halfvec_cosine_ops" in "\n".join(plan.statements)
    assert plan.checksum != build().checksum


def test_storage_contract_changes_when_embedding_or_hnsw_parameters_change():
    original = build()
    for field, value in [
        ("EMBEDDING_MODEL", "other"),
        ("EMBEDDING_DIM", "16"),
        ("POSTGRES_HNSW_M", "32"),
        ("POSTGRES_HNSW_EF", "128"),
    ]:
        assert (
            build(dict(environment(), **{field: value})).checksum != original.checksum
        )


@pytest.mark.parametrize(
    "field,value",
    [
        ("WORKSPACE", "bad/scope"),
        ("WORKSPACE", "x" * 129),
        ("LIGHTRAG_DEPLOYMENT_ID", "../prod"),
    ],
)
def test_scope_identifiers_match_release_protocol_before_connections(field, value):
    with pytest.raises(ValueError):
        build(dict(environment(), **{field: value}))


def test_frozen_initial_schema_still_matches_runtime_contract():
    import re
    from lightrag.kg.postgres_impl import TABLES
    from lightrag.kg.hugegraph_client import _schema_definitions
    from lightrag.provisioning.plan import resource

    def normalize(text):
        return " ".join(re.sub(r"--[^\n]*", "", text).lower().split())

    for name in ("001_business.sql", "001_vectors.sql"):
        for ddl in re.sub(r"--[^\n]*", "", resource(name)).split(";"):
            if not ddl.strip():
                continue
            table = re.search(r"CREATE TABLE (LIGHTRAG_[A-Z_]+)", ddl)[1]
            assert normalize(ddl) == normalize(
                TABLES[table]["ddl"].rstrip().rstrip(";")
            )
    assert json.loads(resource("001_graph.json")) == [
        [kind, value] for kind, value in _schema_definitions()
    ]
