import asyncio
from types import SimpleNamespace

import pytest

from lightrag.base import QueryParam


@pytest.mark.parametrize("parallel", [False, True])
@pytest.mark.parametrize("with_progress", [False, True])
async def test_same_query_keywords_embed_once_and_keep_ordered_search_results(
    monkeypatch, parallel, with_progress
):
    from lightrag import operate

    batches = []
    entered = set()
    all_entered = asyncio.Event()
    progress_events = []

    async def progress(event):
        progress_events.append(event)

    async def embedding(texts, **kwargs):
        batches.append(texts)
        return [[float(i)] for i in range(len(texts))]

    async def branch(name):
        entered.add(name)
        if len(entered) == 3:
            all_entered.set()
        if parallel and not with_progress:
            await asyncio.wait_for(all_entered.wait(), 0.2)

    async def nodes(*args, **kwargs):
        await branch("nodes")
        assert kwargs["query_embedding"] == [0.0]
        return [dict(entity_name="local")], []

    async def edges(*args, **kwargs):
        await branch("edges")
        assert kwargs["query_embedding"] == [0.0]
        return [], [dict(entity_name="global")]

    async def chunks(query, vdb, param, query_embedding):
        await branch("chunks")
        assert query_embedding == [0.0]
        return [dict(chunk_id="chunk")]

    monkeypatch.setattr(operate, "_get_node_data", nodes)
    monkeypatch.setattr(operate, "_get_edge_data", edges)
    monkeypatch.setattr(operate, "_get_vector_context", chunks)
    storage = SimpleNamespace(
        embedding_func=embedding,
        global_config={
            "kg_chunk_pick_method": "VECTOR",
            "addon_params": {"parallel_retrieval": parallel},
        },
    )
    result = await operate._perform_kg_search(
        "same",
        "same",
        "same",
        None,
        None,
        None,
        storage,
        QueryParam(mode="mix"),
        object(),
        progress_callback=progress if with_progress else None,
    )
    assert batches == [["same"]]
    assert [r["entity_name"] for r in result["final_entities"]] == ["local", "global"]
    assert result["vector_chunks"] == [dict(chunk_id="chunk")]
    if with_progress:
        from lightrag.operate import QueryProgress

        assert progress_events == [
            QueryProgress.RETRIEVING_ENTITIES,
            QueryProgress.RETRIEVING_RELATIONS,
            QueryProgress.RETRIEVING_CHUNKS,
        ]


@pytest.mark.parametrize("cancel", [False, True])
async def test_parallel_reads_join_siblings_before_propagating_failure_or_cancel(
    cancel,
):
    from lightrag.operate import _gather_read_branches

    entered, closed, release = asyncio.Event(), asyncio.Event(), asyncio.Event()

    async def wait():
        entered.set()
        try:
            await release.wait()
        finally:
            closed.set()

    async def fail():
        await entered.wait()
        if cancel:
            await release.wait()
        raise OSError("synthetic first failure")

    task = asyncio.create_task(_gather_read_branches([wait(), fail()]))
    await entered.wait()
    if cancel:
        task.cancel()
    with pytest.raises(asyncio.CancelledError if cancel else OSError):
        await task
    assert closed.is_set()
