from types import SimpleNamespace

import pytest

from lightrag.base import QueryContextResult, QueryParam
from lightrag.performance import (
    QueryMetrics,
    measured_query,
    observe_storage,
    queued_model,
)


@pytest.mark.parametrize("stop", ["ok", "empty", "error", "keywords_empty"])
async def test_kg_whole_retrieval_includes_cold_keywords_not_final_generation(
    monkeypatch, stop
):
    from lightrag import operate
    from tests.llm.test_query_cache_kg_chunk_selection import _config, _TextChunks

    clock = [0.0]
    monkeypatch.setattr("lightrag.performance.time.perf_counter", lambda: clock[0])

    async def keywords(*args, **kwargs):
        clock[0] += 1.25
        return ([], []) if stop == "keywords_empty" else (["high"], ["low"])

    async def context(*args, **kwargs):
        clock[0] += 0.4
        if stop == "error":
            raise OSError("synthetic")
        return (
            None
            if stop == "empty"
            else QueryContextResult(context="context", raw_data={})
        )

    async def model(*args, **kwargs):
        clock[0] += 10
        return "answer"

    monkeypatch.setattr(operate, "get_keywords_from_query", keywords)
    monkeypatch.setattr(operate, "_build_query_context", context)
    metrics = QueryMetrics(enabled=True)
    config = _config(model)

    class Rag:
        query_metrics = metrics

        @measured_query
        async def aquery(self, query):
            return await operate.kg_query(
                query,
                None,
                None,
                None,
                _TextChunks(config),
                QueryParam(mode="mix", enable_rerank=False),
                config,
            )

    if stop == "error":
        with pytest.raises(OSError):
            await Rag().aquery("synthetic " * 10)
    else:
        await Rag().aquery("synthetic " * 10)
    rows = [
        r for r in metrics.snapshot()["durations"] if r["stage"] == "retrieval_total"
    ]
    assert len(rows) == 1
    assert rows[0]["sum_seconds"] == pytest.approx(
        1.25 if stop == "keywords_empty" else 1.65
    )
    assert rows[0]["outcome"] == ("error" if stop == "error" else "ok")


@pytest.mark.parametrize("precomputed", [False, True])
async def test_pg_backend_excludes_embedding_and_old_vector_remains_inclusive(
    monkeypatch, precomputed
):
    from lightrag.kg.postgres_impl import PGVectorStorage
    from lightrag.namespace import NameSpace

    clock = [0.0]
    monkeypatch.setattr("lightrag.performance.time.perf_counter", lambda: clock[0])
    metrics = QueryMetrics(enabled=True)

    async def embed(*args, **kwargs):
        clock[0] += 0.25
        return [[0.1, 0.2]]

    async def query(*args, **kwargs):
        clock[0] += 0.05
        return []

    storage = PGVectorStorage.__new__(PGVectorStorage)
    storage.embedding_func = queued_model(
        embed, role="embedding", limit_factory=lambda f: f
    )
    storage.db = SimpleNamespace(query=query, vector_index_type=None)
    storage.namespace = NameSpace.VECTOR_STORE_CHUNKS
    storage.table_name = "synthetic"
    storage.workspace = "synthetic"
    storage.cosine_better_than_threshold = 0.2
    observe_storage(storage, "vector")

    class Rag:
        query_metrics = metrics

        @measured_query
        async def aquery(self, text):
            return await storage.query(
                text, 1, query_embedding=[0.1, 0.2] if precomputed else None
            )

    assert await Rag().aquery("synthetic") == []
    durations = {r["stage"]: r["sum_seconds"] for r in metrics.snapshot()["durations"]}
    assert durations["vector_backend"] == pytest.approx(0.05)
    assert durations["vector"] == pytest.approx(0.05 if precomputed else 0.3)
    if not precomputed:
        assert durations["embedding_total"] == pytest.approx(0.25)


@pytest.mark.parametrize("empty", [False, True])
async def test_naive_whole_retrieval_closes_before_generation_and_on_no_result(
    monkeypatch, empty
):
    from lightrag import operate
    from tests.llm.test_query_cache_kg_chunk_selection import _config

    clock = [0.0]
    monkeypatch.setattr("lightrag.performance.time.perf_counter", lambda: clock[0])
    metrics = QueryMetrics(enabled=True)

    async def vector(*args, **kwargs):
        clock[0] += 0.3
        return (
            []
            if empty
            else [
                dict(
                    chunk_id="chunk-one",
                    content="synthetic fact",
                    file_path="synthetic.txt",
                )
            ]
        )

    async def model(*args, **kwargs):
        clock[0] += 10
        return "answer"

    monkeypatch.setattr(operate, "_get_vector_context", vector)
    config = _config(model)

    class Rag:
        query_metrics = metrics

        @measured_query
        async def aquery(self, query):
            return await operate.naive_query(
                query, None, QueryParam(mode="naive", enable_rerank=False), config
            )

    result = await Rag().aquery("synthetic")
    assert (result is None) == empty
    durations = {r["stage"]: r["sum_seconds"] for r in metrics.snapshot()["durations"]}
    assert durations["retrieval_total"] == pytest.approx(0.3)
    assert durations["query_total"] == pytest.approx(0.3 if empty else 10.3)
