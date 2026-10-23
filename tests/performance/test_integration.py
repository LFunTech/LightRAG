import pytest


async def test_actual_kg_cache_hit_keeps_retrieval_and_reports_history_bypass(
    monkeypatch,
):
    from lightrag.performance import QueryMetrics, measured_query
    from tests.llm.test_query_cache_kg_chunk_selection import (
        _Cache,
        _Model,
        _config,
        _TextChunks,
    )
    from lightrag.operate import kg_query
    from lightrag.base import QueryParam, QueryContextResult

    calls = []

    async def context(*args, **kwargs):
        calls.append(True)
        return QueryContextResult(context="synthetic context", raw_data={})

    async def keywords(*args, **kwargs):
        return [], ["topic"]

    monkeypatch.setattr("lightrag.operate._build_query_context", context)
    monkeypatch.setattr("lightrag.operate.get_keywords_from_query", keywords)
    metrics, cache, model = QueryMetrics(enabled=True), _Cache(), _Model()
    config = _config(model)

    class Rag:
        query_metrics = metrics

        @measured_query
        async def aquery_llm(self, query, param):
            return await kg_query(
                query,
                None,
                None,
                None,
                _TextChunks(config),
                param,
                config,
                hashing_kv=cache,
            )

    rag = Rag()
    p = QueryParam(mode="local", enable_rerank=False)
    await rag.aquery_llm("synthetic", p)
    await rag.aquery_llm("synthetic", p)
    assert len(calls) == 2 and model.calls == 1
    counters = metrics.snapshot()["counters"]
    assert (
        sum(
            r["count"]
            for r in counters
            if r["name"] == "cache_hit" and r["category"] == "query"
        )
        == 1
    )
    await rag.aquery_llm(
        "synthetic",
        QueryParam(
            mode="local",
            enable_rerank=False,
            conversation_history=[{"role": "user", "content": "synthetic"}],
        ),
    )
    assert model.calls == 2
    assert any(
        r["name"] == "cache_bypass" and r["kind"] == "history"
        for r in metrics.snapshot()["counters"]
    )


def test_core_metrics_is_off_by_default_and_explicitly_enabled(tmp_path, monkeypatch):
    from lightrag import LightRAG
    from lightrag.utils import EmbeddingFunc
    from lightrag.performance import QueryMetrics

    monkeypatch.delenv("LIGHTRAG_QUERY_METRICS", raising=False)

    async def model(*args, **kwargs):
        return "synthetic"

    async def embed(texts, **kwargs):
        return [[0.0] * 8 for _ in texts]

    embedding = EmbeddingFunc(
        embedding_dim=8, max_token_size=1000, func=embed, model_name="synthetic"
    )
    rag = LightRAG(
        working_dir=str(tmp_path / "off"),
        llm_model_func=model,
        embedding_func=embedding,
    )
    assert isinstance(rag.query_metrics, QueryMetrics) and not rag.query_metrics.enabled
    monkeypatch.setenv("LIGHTRAG_QUERY_METRICS", "true")
    enabled = LightRAG(
        working_dir=str(tmp_path / "on"), llm_model_func=model, embedding_func=embedding
    )
    assert enabled.query_metrics.enabled


async def test_answer_cache_can_be_bypassed_without_disabling_keyword_or_extract_cache():
    from lightrag.operate import _answer_cache_kv
    from lightrag.base import QueryParam
    from lightrag.utils import handle_cache

    class Cache:
        global_config = {
            "enable_llm_cache": True,
            "enable_query_answer_cache": False,
            "enable_llm_cache_for_entity_extract": True,
        }

        async def get_by_id(self, key):
            return {"return": "synthetic", "create_time": 1}

    cache = Cache()
    assert _answer_cache_kv(QueryParam(), cache) is None
    assert await handle_cache(cache, "hash", "synthetic", "mix", "keywords") == (
        "synthetic",
        1,
    )
    assert await handle_cache(cache, "hash", "synthetic", "default", "extract") == (
        "synthetic",
        1,
    )


@pytest.mark.parametrize(
    "change",
    ["knowledge_update", "deleted_document", "permission_revoked", "model_revision"],
)
async def test_prod_policy_never_serves_legacy_answer_after_scope_changes(
    monkeypatch, change
):
    from lightrag.base import QueryContextResult
    from tests.llm.test_query_cache_kg_chunk_selection import (
        _Cache,
        _Model,
        _config,
        _run,
    )

    state = {"context": "initial accessible document"}

    async def context(*args, **kwargs):
        return QueryContextResult(context=state["context"], raw_data={})

    async def keywords(*args, **kwargs):
        return [], ["topic"]

    monkeypatch.setattr("lightrag.operate._build_query_context", context)
    monkeypatch.setattr("lightrag.operate.get_keywords_from_query", keywords)
    cache, model = _Cache(), _Model()
    config = _config(model)
    await _run(config, cache)  # Legacy answer exists, but may no longer be valid.
    cache.global_config["enable_query_answer_cache"] = False
    state["context"] = change
    result = await _run(config, cache)
    assert result.content == "answer-2" and model.calls == 2
    assert len([key for key in cache.store if ":query:" in key]) == 1
