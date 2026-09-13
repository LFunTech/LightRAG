"""Real PG + HugeGraph runtime/SDK verification on fresh local_debug scopes."""

import asyncio
import os
from uuid import uuid4

import numpy as np
import pytest

from lightrag import LightRAG
from lightrag.constants import GRAPH_FIELD_SEP
from lightrag.distributed import (
    PostgresCoordinator,
    OperationOwnershipError,
    WorkspaceFencedError,
)
from lightrag.kg.shared_storage import get_storage_keyed_lock
from lightrag.utils import EmbeddingFunc, compute_mdhash_id

pytestmark = pytest.mark.integration


async def embed(texts, **kwargs):
    return np.array([[1.0, 0.0, 0.0] for _ in texts], dtype=np.float32)


async def llm(*args, **kwargs):
    return "Merged description"


@pytest.fixture
async def runtime_rags(monkeypatch, tmp_path):
    if not os.getenv("LIGHTRAG_COORDINATION_DSN") or not os.getenv("HUGEGRAPH_URI"):
        pytest.skip("Explicit isolated PostgreSQL/HugeGraph settings required")
    if not os.getenv("POSTGRES_DATABASE", "").startswith("local_debug_"):
        pytest.skip("Business database must be a disposable local_debug_ database")
    monkeypatch.setenv("LIGHTRAG_SHARED_STORAGE", "true")
    monkeypatch.setenv("LIGHTRAG_DEPLOYMENT_ID", "local_debug_runtime_tests")
    monkeypatch.setenv("POSTGRES_PASSWORD", os.getenv("POSTGRES_PASSWORD", "test"))
    monkeypatch.setenv("HUGEGRAPH_GRAPH", "hugegraph")
    monkeypatch.setenv("HUGEGRAPH_GRAPHSPACE", "DEFAULT")
    await PostgresCoordinator.migrate(os.environ["LIGHTRAG_COORDINATION_DSN"])
    workspace = "local_debug_" + uuid4().hex
    kwargs = dict(
        working_dir=str(tmp_path),
        workspace=workspace,
        distributed_writes=True,
        kv_storage="PGKVStorage",
        vector_storage="PGVectorStorage",
        doc_status_storage="PGDocStatusStorage",
        graph_storage="HugeGraphStorage",
        llm_model_func=llm,
        embedding_func=EmbeddingFunc(
            embedding_dim=3, func=embed, model_name="runtime_test"
        ),
    )
    first = LightRAG(**kwargs)
    second = LightRAG(**kwargs)
    try:
        async with first.distributed_maintenance():
            await first.initialize_storages()
            await first.check_and_migrate_data()
        await second.initialize_storages()
        yield first, second
    finally:
        for rag in (second, first):
            try:
                await rag.finalize_storages()
            except (WorkspaceFencedError, OperationOwnershipError):
                await rag._distributed_runtime.close()
        # Keep isolated durable histories and business rows for forensic review.


async def test_real_sdk_crud_cache_and_physical_journal(runtime_rags):
    rag, peer = runtime_rags
    await rag.acreate_entity("Alice", {"description": "A", "source_id": ""})
    await rag.acreate_entity("Bob", {"description": "B", "source_id": ""})
    await rag.acreate_relation(
        "Alice",
        "Bob",
        {"description": "AB", "keywords": "related", "weight": 0.5, "source_id": ""},
    )
    assert await peer.chunk_entity_relation_graph.has_edge("Alice", "Bob")
    assert await peer.entities_vdb.get_by_id(compute_mdhash_id("Alice", prefix="ent-"))
    await rag.aedit_entity("Alice", {"description": "updated"})
    assert (await peer.chunk_entity_relation_graph.get_node("Alice"))[
        "description"
    ] == "updated"
    await rag.aclear_cache()
    await rag.adelete_by_relation("Alice", "Bob")
    await asyncio.wait_for(rag.adelete_by_entity("Alice"), 3)
    assert not await peer.chunk_entity_relation_graph.has_node("Alice")
    state = await rag._distributed_runtime.coordinator.inspect()
    assert state["mutations"]
    assert all(item["state"] == "ack" for item in state["mutations"])
    assert not state["fenced"]


async def test_real_core_entity_rmw_retains_both_sources_and_commits_vector_under_lock(
    runtime_rags,
):
    from lightrag.operate import _merge_nodes_then_upsert

    rag, peer = runtime_rags

    async def merge(rag, source):
        rt = rag._distributed_runtime
        async with rt.operation("ingest"):
            async with get_storage_keyed_lock(
                ["Shared"], namespace=f"{rag.workspace}:GraphDB"
            ):
                await _merge_nodes_then_upsert(
                    "Shared",
                    [
                        {
                            "entity_name": "Shared",
                            "entity_type": "PERSON",
                            "description": source,
                            "source_id": source,
                            "file_path": "test",
                        }
                    ],
                    rag.chunk_entity_relation_graph,
                    rag.entities_vdb,
                    rag._build_global_config(),
                    entity_chunks_storage=rag.entity_chunks,
                )
                assert await rag.entities_vdb.get_by_id(
                    compute_mdhash_id("Shared", prefix="ent-")
                )

    await asyncio.gather(merge(rag, "chunk-1"), merge(peer, "chunk-2"))
    node = await rag.chunk_entity_relation_graph.get_node("Shared")
    assert set(node["source_id"].split(GRAPH_FIELD_SEP)) == {"chunk-1", "chunk-2"}
    assert set((await rag.entity_chunks.get_by_id("Shared"))["chunk_ids"]) == {
        "chunk-1",
        "chunk-2",
    }


async def test_real_sql_ack_loss_fences_next_storage_and_peer(
    runtime_rags, monkeypatch
):
    rag, peer = runtime_rags
    db = rag._distributed_runtime.db
    original = db._run_with_retry
    calls = []

    async def uncertain(operation, **kwargs):
        async def lost_ack(connection):
            await operation(connection)
            calls.append("committed")
            raise ConnectionResetError("SQL ACK lost")

        return await original(lost_ack, **kwargs)

    with pytest.raises(WorkspaceFencedError):
        async with rag._distributed_runtime.operation("cache-write"):
            monkeypatch.setattr(db, "_run_with_retry", uncertain)
            with pytest.raises(ConnectionResetError):
                await rag.llm_response_cache.upsert(
                    {
                        "default:query:key": {
                            "return": "cached",
                            "original_prompt": "query",
                            "cache_type": "query",
                        }
                    }
                )
            monkeypatch.setattr(db, "_run_with_retry", original)
            with pytest.raises(WorkspaceFencedError):
                await rag.llm_response_cache.upsert(
                    {"default:query:next": {"return": "no"}}
                )
    assert calls == ["committed"]
    with pytest.raises(WorkspaceFencedError):
        await peer.acreate_entity("Denied", {"description": "must not be sent"})


async def test_real_cancelled_delete_retains_exact_tracking_targets_and_fences(
    runtime_rags, monkeypatch
):
    rag, peer = runtime_rags
    await rag.acreate_entity("Cancel", {"description": "C", "source_id": "chunk-c"})
    removed = asyncio.Event()
    release = asyncio.Event()

    async def commit():
        removed.set()
        await release.wait()

    monkeypatch.setattr(rag.chunk_entity_relation_graph, "index_done_callback", commit)
    task = asyncio.create_task(rag.adelete_by_entity("Cancel"))
    await asyncio.wait_for(removed.wait(), 3)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert not await peer.chunk_entity_relation_graph.has_node("Cancel")
    assert (await peer.entity_chunks.get_by_id("Cancel"))["chunk_ids"] == ["chunk-c"]
    state = await rag._distributed_runtime.coordinator.inspect()
    assert state["fenced"]
    operation = next(
        op for op in state["operations"] if op["kind"] == "adelete_by_entity"
    )
    assert {"namespace": "entity_chunks", "key": "Cancel"} in operation["metadata"][
        "tracking_recovery"
    ]
    with pytest.raises(WorkspaceFencedError):
        await peer.acreate_entity("Cancel", {"description": "unsafe recreation"})


async def test_real_sdk_custom_kg_rename_and_merge(runtime_rags):
    rag, peer = runtime_rags
    await rag.ainsert_custom_kg(
        {
            "entities": [
                {"entity_name": "Source", "entity_type": "PERSON", "description": "S"},
                {"entity_name": "Target", "entity_type": "PERSON", "description": "T"},
            ],
            "relationships": [
                {
                    "src_id": "Source",
                    "tgt_id": "Target",
                    "description": "R",
                    "keywords": "R",
                    "weight": 0.5,
                }
            ],
        }
    )
    state = await rag._distributed_runtime.coordinator.inspect()
    custom = next(op for op in state["operations"] if op["kind"] == "ainsert_custom_kg")
    assert custom["metadata"].get("custom_kg_targets") == {
        "entity_names": ["Source", "Target"],
        "relation_pairs": [["Source", "Target"]],
        "chunk_ids": [],
        "full_doc_id": None,
    }
    await rag.aedit_entity("Source", {"entity_name": "Renamed"})
    assert await peer.chunk_entity_relation_graph.has_node("Renamed")
    assert not await peer.chunk_entity_relation_graph.has_node("Source")
    await rag.amerge_entities(["Renamed"], "Target")
    assert not await peer.chunk_entity_relation_graph.has_node("Renamed")
    assert await peer.chunk_entity_relation_graph.has_node("Target")


async def test_real_custom_chunks_query_cache_and_document_purge(runtime_rags):
    from lightrag import QueryParam

    rag, peer = runtime_rags
    await rag.ainsert_custom_chunks(
        "A short document", ["A short document"], doc_id="custom-doc"
    )
    assert await peer.full_docs.get_by_id("custom-doc")
    answer = await rag.aquery(
        "What is this?", QueryParam(mode="naive", enable_rerank=False)
    )
    assert answer == "Merged description"
    state = await rag._distributed_runtime.coordinator.inspect()
    assert any(
        row["backend"] == "PGKVStorage" and row["namespace"] == "llm_response_cache"
        for row in state["mutations"]
    )
    result = await rag.adelete_by_doc_id("custom-doc")
    assert result.status == "success"
    assert await peer.full_docs.get_by_id("custom-doc") is None


@pytest.mark.parametrize("initial_refs", [None, "null"])
async def test_real_chunk_cache_attribution_union_survives_stale_concurrent_snapshots(
    runtime_rags, initial_refs
):
    rag, peer = runtime_rags
    async with rag._distributed_runtime.operation("chunk-write"):
        await rag.text_chunks.upsert(
            {
                "shared-chunk": {
                    "tokens": 2,
                    "chunk_order_index": 0,
                    "full_doc_id": "doc",
                    "content": "shared",
                    "file_path": "shared.txt",
                }
            }
        )
    from lightrag.distributed.runtime import storage_write

    @storage_write
    async def legacy_null(storage):
        await storage.db.execute(
            "UPDATE LIGHTRAG_DOC_CHUNKS SET llm_cache_list=$3::jsonb WHERE workspace=$1 AND id=$2",
            {
                "workspace": storage.workspace,
                "id": "shared-chunk",
                "refs": initial_refs,
            },
        )

    async with rag._distributed_runtime.operation("legacy-fixture"):
        await legacy_null(rag.text_chunks)
    snapshots = []
    both = asyncio.Event()

    async def attach(rag, key):
        async with rag._distributed_runtime.operation("cache-reference"):
            row = await rag.text_chunks.get_by_id("shared-chunk")
            snapshots.append(row)
            if len(snapshots) == 2:
                both.set()
            await asyncio.wait_for(both.wait(), 1)
            row["llm_cache_list"] = [key, key]
            await rag.text_chunks.upsert({"shared-chunk": row})

    await asyncio.gather(attach(rag, "cache-a"), attach(peer, "cache-b"))
    row = await rag.text_chunks.get_by_id("shared-chunk")
    assert set(row["llm_cache_list"]) == {"cache-a", "cache-b"}
    assert len(row["llm_cache_list"]) == 2
    assert row["content"] == "shared" and row["file_path"] == "shared.txt"
    # A stale empty snapshot cannot retire attribution before chunk deletion.
    async with rag._distributed_runtime.operation("chunk-write"):
        row["llm_cache_list"] = []
        await rag.text_chunks.upsert({"shared-chunk": row})
    assert set(
        (await peer.text_chunks.get_by_id("shared-chunk"))["llm_cache_list"]
    ) == {"cache-a", "cache-b"}


@pytest.mark.parametrize("edit_kind", ["entity", "relation", "allow_merge"])
@pytest.mark.parametrize("failure", ["ack_loss", "cancel"])
async def test_real_shrink_journal_precedes_immediate_graph_commit(
    runtime_rags, monkeypatch, edit_kind, failure
):
    from lightrag.distributed import CoordinationError
    from lightrag.utils import make_relation_chunk_key

    rag, peer = runtime_rags
    old_sources = GRAPH_FIELD_SEP.join(["chunk-a", "chunk-b"])
    await rag.acreate_entity("A", {"description": "A", "source_id": old_sources})
    await rag.acreate_entity("B", {"description": "B", "source_id": ""})
    if edit_kind == "relation":
        await rag.acreate_relation(
            "A",
            "B",
            {
                "description": "R",
                "keywords": "R",
                "source_id": old_sources,
                "weight": 2,
            },
        )
    graph = rag.chunk_entity_relation_graph
    original = graph._client.request
    runtime = rag._distributed_runtime
    before_commit = []

    async def uncertain_request(method, path, *args, **kwargs):
        target = (
            "/graph/edges/batch" if edit_kind == "relation" else "/graph/vertices/batch"
        )
        if method == "POST" and path.endswith(target):
            state = await runtime.coordinator.inspect()
            op = next(
                row
                for row in state["operations"]
                if row["id"] == str(runtime.permit().operation.id)
            )
            before_commit.append(op["metadata"].get("tracking_recovery"))
            await original(method, path, *args, **kwargs)
            if failure == "cancel":
                raise asyncio.CancelledError()
            raise ConnectionResetError("graph committed but response lost")
        return await original(method, path, *args, **kwargs)

    monkeypatch.setattr(graph._client, "request", uncertain_request)
    if edit_kind == "relation":
        edit = rag.aedit_relation("A", "B", {"source_id": "chunk-a"})
        key = make_relation_chunk_key("A", "B")
        namespace = "relation_chunks"
        tracking = peer.relation_chunks
    else:
        data = {"source_id": "chunk-a"}
        if edit_kind == "allow_merge":
            data["entity_name"] = "B"
        edit = rag.aedit_entity("A", data, allow_merge=edit_kind == "allow_merge")
        key, namespace, tracking = "A", "entity_chunks", peer.entity_chunks
    with pytest.raises(
        asyncio.CancelledError if failure == "cancel" else CoordinationError
    ):
        await edit
    assert before_commit == [[{"namespace": namespace, "key": key}]]
    state = await runtime.coordinator.inspect()
    assert state["fenced"]
    assert set((await tracking.get_by_id(key))["chunk_ids"]) == {"chunk-a", "chunk-b"}
    actual = (
        await peer.chunk_entity_relation_graph.get_edge("A", "B")
        if edit_kind == "relation"
        else await peer.chunk_entity_relation_graph.get_node("A")
    )
    assert actual["source_id"] == "chunk-a"
    with pytest.raises(WorkspaceFencedError):
        await peer.acreate_entity("Denied", {"description": "fenced"})


@pytest.mark.parametrize("cancel", [False, True])
async def test_real_finalize_admission_failure_preserves_active_writer(
    runtime_rags, cancel
):
    from lightrag.distributed import CoordinationBusyError

    rag, peer = runtime_rags
    runtime = rag._distributed_runtime
    runtime.coordinator.wait_timeout = 5 if cancel else 0.05
    entered = asyncio.Event()
    release = asyncio.Event()

    async def write():
        async with runtime.operation("active_ingest"):
            entered.set()
            await release.wait()
            await rag.full_docs.upsert({"continued": {"content": "still active"}})

    writer = asyncio.create_task(write())
    await entered.wait()
    finalizer = asyncio.create_task(rag.finalize_storages())
    try:
        if cancel:
            await asyncio.sleep(0.03)
            assert not finalizer.done()
            finalizer.cancel()
        with pytest.raises(asyncio.CancelledError if cancel else CoordinationBusyError):
            await asyncio.wait_for(finalizer, 2)
        assert not runtime.closed
        assert rag.full_docs.db is runtime.db
        release.set()
        await asyncio.wait_for(writer, 2)
        assert (await peer.full_docs.get_by_id("continued"))[
            "content"
        ] == "still active"
        assert not (await runtime.coordinator.inspect())["fenced"]
    finally:
        release.set()
        if not writer.done():
            await writer
