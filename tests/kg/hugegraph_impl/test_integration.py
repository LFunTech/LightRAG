"""Opt-in HugeGraph 1.7 graph contract and real LightRAG lifecycle tests.

Run with ``PYTHON=.venv/bin/python ./scripts/test.sh
 tests/kg/hugegraph_impl/test_integration.py --run-integration -o addopts=''``.
Set HUGEGRAPH_URI explicitly; other HUGEGRAPH_* settings are inherited.
Every test creates UUID-scoped data, and cleanup drops ONLY those scopes. No
fixture deletes a graph, schema, or data that predates this test invocation.
LLM/embedding responses are deterministic test doubles; HugeGraph and the
LightRAG ingestion, extraction parsing, retrieval and purge paths are real.
"""

from __future__ import annotations

import asyncio
import json
from uuid import uuid4

import numpy as np
import pytest

from lightrag import LightRAG, QueryParam
from lightrag.base import DocStatus
from lightrag.constants import GRAPH_FIELD_SEP
from lightrag.kg.shared_storage import initialize_share_data
from lightrag.utils import EmbeddingFunc, Tokenizer, make_relation_chunk_key

pytestmark = [pytest.mark.integration, pytest.mark.requires_db, pytest.mark.asyncio]


@pytest.fixture
async def hugegraph_factory(request, monkeypatch, tmp_path):
    """Require explicit opt-in and own a fresh random scope for every client."""
    if not request.config.getoption("--run-integration"):
        pytest.skip("HugeGraph mutations require explicit --run-integration")
    from lightrag.kg.hugegraph_impl import HugeGraphStorage

    initialize_share_data()
    # Keep operator-selected connection/auth settings, while forcing small
    # pages so even the small fixtures traverse multiple server-side pages.
    monkeypatch.setenv("HUGEGRAPH_BATCH_SIZE", "2")
    clients = []
    test_id = uuid4().hex
    workspace = f"local-debug-lightrag-{test_id}"
    namespace = f"local-debug-graph-{test_id}"

    async def create(*, workspace_name=None, namespace_name=None):
        client = HugeGraphStorage(
            namespace=namespace_name or namespace,
            workspace=workspace_name or workspace,
            global_config={"working_dir": str(tmp_path), "max_graph_nodes": 1000},
            embedding_func=None,
        )
        try:
            await client.initialize()
        except BaseException:
            await client.finalize()
            raise
        clients.append(client)
        return client

    yield create
    errors = []
    for client in reversed(clients):
        try:
            # A restart test may have finalized this instance already.
            await client.initialize()
            outcome = await client.drop()
            assert outcome["status"] == "success", outcome
        except Exception as error:
            errors.append(error)
        finally:
            await client.finalize()
    if errors:
        raise RuntimeError(
            f"Failed to clean up HugeGraph test scopes: {errors!r}"
        ) from errors[0]


async def test_single_batch_crud_preserves_scalar_partial_updates(hugegraph_factory):
    storage = await hugegraph_factory()
    assert await storage.has_node("missing") is False
    assert await storage.get_node("missing") is None
    assert await storage.get_node_edges("missing") is None
    assert await storage.get_edge("A", "B") is None
    assert await storage.has_edge("A", "B") is False
    assert await storage.node_degree("missing") == 0

    await storage.upsert_node(
        "A",
        {
            "entity_id": "A",
            "description": "first",
            "active": True,
            "count": 7,
            "fraction": 0.125,
            "blank": "",
        },
    )
    await storage.upsert_nodes_batch(
        [
            ("B", {"entity_id": "B"}),
            ("isolated", {"entity_id": "isolated"}),
            ("A", {"description": "second"}),
            ("A", {"description": "last", "added": False}),
        ]
    )
    node = await storage.get_node("A")
    assert node == {
        "entity_id": "A",
        "description": "last",
        "active": True,
        "count": 7,
        "fraction": 0.125,
        "blank": "",
        "added": False,
    }
    assert type(node["active"]) is bool
    assert type(node["count"]) is int
    assert type(node["fraction"]) is float
    assert await storage.get_node_edges("isolated") == []
    assert await storage.has_nodes_batch(["A", "B", "missing", "A"]) == {"A", "B"}
    assert set(await storage.get_nodes_batch(["A", "B", "missing"])) == {"A", "B"}

    await storage.upsert_edge("A", "B", {"weight": 0.25, "kept": "first"})
    await storage.upsert_edges_batch(
        [
            ("B", "A", {"weight": 2.5, "reverse": True}),
            ("A", "B", {"weight": 3.75, "description": "last input"}),
        ]
    )
    edge = {
        "weight": 3.75,
        "kept": "first",
        "reverse": True,
        "description": "last input",
    }
    assert await storage.get_edge("A", "B") == edge
    assert await storage.get_edge("B", "A") == edge
    assert await storage.has_edge("B", "A") is True
    assert len(await storage.get_all_edges()) == 1
    assert await storage.node_degree("A") == 1
    assert await storage.edge_degree("A", "B") == 2
    assert await storage.node_degrees_batch(["A", "B", "isolated", "missing"]) == {
        "A": 1,
        "B": 1,
        "isolated": 0,
        "missing": 0,
    }
    assert await storage.edge_degrees_batch([("A", "B"), ("B", "A")]) == {
        ("A", "B"): 2,
        ("B", "A"): 2,
    }
    assert await storage.get_edges_batch(
        [
            {"src": "A", "tgt": "B"},
            {"src": "B", "tgt": "A"},
            {"src": "A", "tgt": "missing"},
        ]
    ) == {("A", "B"): edge, ("B", "A"): edge}
    adjacent = await storage.get_nodes_edges_batch(["A", "isolated", "missing"])
    assert len(adjacent["A"]) == 1
    assert set(adjacent["A"][0]) == {"A", "B"}
    assert adjacent["isolated"] == adjacent["missing"] == []

    await storage.remove_edges([("B", "A"), ("A", "missing")])
    assert await storage.get_edge("A", "B") is None
    assert await storage.get_node_edges("A") == []
    await storage.upsert_edge("A", "B", {"weight": 1.0})
    await storage.delete_node("A")
    assert await storage.get_node("A") is None
    assert await storage.get_node("B") is not None
    assert await storage.get_all_edges() == []
    await storage.delete_node("A")
    await storage.remove_nodes(["B", "isolated", "missing"])
    assert await storage.get_all_labels() == []
    assert await storage.get_all_nodes() == []
    await storage.upsert_nodes_batch([])
    await storage.upsert_edges_batch([])
    assert await storage.get_nodes_batch([]) == {}
    assert await storage.has_nodes_batch([]) == set()


async def test_original_special_ids_and_restart_are_lossless(hugegraph_factory):
    storage = await hugegraph_factory()
    names = [
        "北京/研究中心😀",
        "quote'\"\\slash\nline",
        "'; 1 + 1; //",
        "long-" + "学" * 5000,
    ]
    for name in names:
        await storage.upsert_node(name, {"entity_id": name, "description": name})
    await storage.upsert_edges_batch(
        [(names[0], name, {"description": name, "weight": 1.0}) for name in names[1:]]
    )
    await storage.index_done_callback()
    await storage.finalize()
    restarted = await hugegraph_factory()
    assert await restarted.get_all_labels() == sorted(names)
    for name in names:
        assert await restarted.get_node(name) == {
            "entity_id": name,
            "description": name,
        }
    assert (await restarted.get_edge(names[3], names[0]))["description"] == names[3]
    assert await restarted.node_degree(names[0]) == 3
    view = await restarted.get_knowledge_graph("*", max_nodes=10)
    assert {node.id for node in view.nodes} == set(names)
    assert all(edge.source in names and edge.target in names for edge in view.edges)


async def test_paged_iteration_export_and_label_search(hugegraph_factory):
    storage = await hugegraph_factory()
    names = [f"Node-{index:02d}" for index in range(9)]
    await storage.upsert_nodes_batch(
        [
            (name, {"entity_id": name, "ordinal": index})
            for index, name in enumerate(names)
        ]
    )
    await storage.upsert_edges_batch(
        [
            (names[index], names[index + 1], {"weight": index + 0.5})
            for index in range(8)
        ]
    )
    label_pages = [page async for page in storage.iter_labels(batch_size=3)]
    assert len(label_pages) >= 3
    assert all(0 < len(page) <= 3 for page in label_pages)
    labels = [label for page in label_pages for label in page]
    assert sorted(labels) == names
    edge_pages = [page async for page in storage.iter_edges(batch_size=3)]
    assert len(edge_pages) >= 3
    assert all(0 < len(page) <= 3 for page in edge_pages)
    edges = [edge for page in edge_pages for edge in page]
    assert len(edges) == 8
    assert {frozenset((edge["source"], edge["target"])) for edge in edges} == {
        frozenset((names[index], names[index + 1])) for index in range(8)
    }
    exported_nodes = {node["id"]: node for node in await storage.get_all_nodes()}
    assert set(exported_nodes) == set(names)
    assert exported_nodes["Node-08"]["ordinal"] == 8
    assert len(await storage.get_all_edges()) == 8
    assert await storage.search_labels("Node-08", limit=1) == ["Node-08"]
    assert set(await storage.search_labels("node-0", limit=20)) == set(names)
    assert await storage.search_labels("absent-name") == []
    assert await storage.search_labels("   ") == []


async def test_rank_ties_isolated_nodes_and_capped_breadth_first(hugegraph_factory):
    storage = await hugegraph_factory()
    # Reverse insertion must not determine the rank cutoff. The star leaves
    # all tie at degree 1, including non-ASCII names; isolated nodes rank last.
    names = ["中", "é", "Zulu", "Alpha", "Hub", "isolated"]
    await storage.upsert_nodes_batch([(name, {"entity_id": name}) for name in names])
    await storage.upsert_edges_batch(
        [("Hub", name, {"weight": 1.0}) for name in names[:4]]
    )
    assert await storage.get_popular_labels(6) == [
        "Hub",
        "Alpha",
        "Zulu",
        "é",
        "中",
        "isolated",
    ]
    view = await storage.get_knowledge_graph("*", max_nodes=3)
    assert {node.id for node in view.nodes} == {"Hub", "Alpha", "Zulu"}
    assert view.is_truncated is True
    capped = await storage.get_knowledge_graph("Hub", max_depth=2, max_nodes=3)
    assert len(capped.nodes) == 3
    assert "Hub" in {node.id for node in capped.nodes}
    assert capped.is_truncated is True
    for graph in [view, capped]:
        node_ids = {node.id for node in graph.nodes}
        assert all(
            edge.source in node_ids and edge.target in node_ids for edge in graph.edges
        )
    complete = await storage.get_knowledge_graph("Hub", max_depth=1, max_nodes=10)
    assert {node.id for node in complete.nodes} == {"Hub", "Alpha", "Zulu", "é", "中"}
    assert complete.is_truncated is False
    missing = await storage.get_knowledge_graph("missing", max_nodes=2)
    assert missing.nodes == missing.edges == []
    isolated = await storage.get_knowledge_graph("isolated", max_nodes=2)
    assert [node.id for node in isolated.nodes] == ["isolated"]
    assert isolated.edges == []
    await storage.upsert_node("Beyond", {"entity_id": "Beyond"})
    await storage.upsert_edge("Alpha", "Beyond", {"weight": 1.0})
    shallow = await storage.get_knowledge_graph("Hub", max_depth=1, max_nodes=10)
    assert {node.id for node in shallow.nodes} == {"Hub", "Alpha", "Zulu", "é", "中"}
    deeper = await storage.get_knowledge_graph("Hub", max_depth=2, max_nodes=10)
    assert {node.id for node in deeper.nodes} == {
        "Hub",
        "Alpha",
        "Zulu",
        "é",
        "中",
        "Beyond",
    }


async def test_workspace_namespace_and_drop_are_isolated(hugegraph_factory):
    primary = await hugegraph_factory()
    namespace_peer = await hugegraph_factory(
        namespace_name=f"local-debug-peer-{uuid4().hex}"
    )
    workspace_peer = await hugegraph_factory(
        workspace_name=f"local-debug-lightrag-{uuid4().hex}"
    )
    for number, storage in enumerate([primary, namespace_peer, workspace_peer]):
        await storage.upsert_nodes_batch(
            [("A", {"owner": number}), ("B", {"owner": number})]
        )
        await storage.upsert_edge("A", "B", {"weight": number + 1.0})
    await primary.delete_node("A")
    assert await primary.get_all_edges() == []
    for number, peer in enumerate([namespace_peer, workspace_peer], start=1):
        assert await peer.get_node("A") == {"owner": number}
        assert (await peer.get_edge("A", "B"))["weight"] == number + 1.0
    assert (await primary.drop())["status"] == "success"
    assert await primary.get_all_labels() == []
    for peer in [namespace_peer, workspace_peer]:
        assert await peer.get_all_labels() == ["A", "B"]
        assert len(await peer.get_all_edges()) == 1


async def test_shared_clients_concurrent_partial_writes_do_not_lose_attributes(
    hugegraph_factory,
):
    first = await hugegraph_factory()
    second = await hugegraph_factory()
    await first.upsert_nodes_batch([("A", {"seed": True}), ("B", {"seed": True})])
    await first.upsert_edge("A", "B", {"weight": 1.0})
    await asyncio.gather(
        *[
            client.upsert_node("A", {f"field_{index}": index})
            for index, client in enumerate([first, second] * 4)
        ]
    )
    assert await second.get_node("A") == {
        "seed": True,
        **{f"field_{i}": i for i in range(8)},
    }
    await asyncio.gather(
        *[
            client.upsert_edge("B", "A", {f"field_{index}": index})
            for index, client in enumerate([first, second] * 4)
        ]
    )
    assert await first.get_edge("A", "B") == {
        "weight": 1.0,
        **{f"field_{i}": i for i in range(8)},
    }
    assert await first.node_degree("A") == 1
    assert len(await second.get_all_edges()) == 1


class _Characters:
    def encode(self, content: str) -> list[int]:
        return [ord(character) for character in content]

    def decode(self, tokens: list[int]) -> str:
        return "".join(chr(token) for token in tokens)


async def _embedding(texts: list[str]) -> np.ndarray:
    # Constant, nonzero vectors make all fixture records relevant. Each row
    # still corresponds to exactly one input, as required by EmbeddingFunc.
    return np.ones((len(texts), 8), dtype=np.float32)


async def _llm(prompt: str, system_prompt=None, **kwargs) -> str:
    if "high_level_keywords" in prompt:
        return json.dumps(
            {
                "high_level_keywords": ["cooperates"],
                "low_level_keywords": ["Atlas", "Borealis"],
            }
        )
    if "<|#|>" in (system_prompt or ""):
        return (
            "entity<|#|>Atlas<|#|>organization<|#|>Atlas research company.\n"
            "entity<|#|>Borealis<|#|>organization<|#|>Borealis research company.\n"
            "relation<|#|>Atlas<|#|>Borealis<|#|>cooperates<|#|>Atlas cooperates with Borealis.\n"
            "<|COMPLETE|>"
        )
    return "Atlas cooperates with Borealis on research."


def _one_chunk(tokenizer, content, *args, **kwargs):
    return [
        {
            "tokens": len(tokenizer.encode(content)),
            "content": content,
            "chunk_order_index": 0,
        }
    ]


@pytest.fixture
async def rag_factory(hugegraph_factory, tmp_path):
    # Provisioning is serialized before starting LightRAG's normal registry
    # path. This also proves that its second initialization is idempotent.
    await hugegraph_factory()
    instances = []
    workspace = f"local-debug-lightrag-{uuid4().hex}"

    async def create():
        rag = LightRAG(
            working_dir=str(tmp_path / "lifecycle"),
            workspace=workspace,
            graph_storage="HugeGraphStorage",
            kv_storage="JsonKVStorage",
            vector_storage="NanoVectorDBStorage",
            doc_status_storage="JsonDocStatusStorage",
            llm_model_func=_llm,
            embedding_func=EmbeddingFunc(
                embedding_dim=8, max_token_size=8192, func=_embedding
            ),
            tokenizer=Tokenizer("characters", _Characters()),
            chunking_func=_one_chunk,
            entity_extract_max_gleaning=0,
            entity_extraction_use_json=False,
            force_llm_summary_on_merge=100,
            max_parallel_insert=1,
            vector_db_storage_cls_kwargs={"cosine_better_than_threshold": 0.0},
        )
        await rag.initialize_storages()
        instances.append(rag)
        return rag

    yield create
    errors = []
    for rag in reversed(instances):
        try:
            graph = rag.chunk_entity_relation_graph
            await graph.initialize()
            assert (await graph.drop())["status"] == "success"
        except Exception as error:
            errors.append(error)
        finally:
            await rag.finalize_storages()
            await graph.finalize()
    if errors:
        raise RuntimeError(
            f"Failed to clean up LightRAG HugeGraph test scopes: {errors!r}"
        ) from errors[0]


def _status(row):
    status = row["status"]
    return status.value if isinstance(status, DocStatus) else status


async def test_lightrag_ingestion_queries_edits_merge_and_shared_doc_purge(rag_factory):
    rag = await rag_factory()
    graph = rag.chunk_entity_relation_graph
    await rag.ainsert(
        [
            "Atlas and Borealis cooperate on the old harbor project.",
            "Atlas and Borealis cooperate on the new mountain project.",
        ],
        ids=["old", "new"],
        file_paths=["old.txt", "new.txt"],
    )
    old = await rag.doc_status.get_by_id("old")
    new = await rag.doc_status.get_by_id("new")
    assert _status(old) == _status(new) == DocStatus.PROCESSED.value
    old_chunk, new_chunk = old["chunks_list"][0], new["chunks_list"][0]
    edge = await graph.get_edge("Atlas", "Borealis")
    assert set(edge["source_id"].split(GRAPH_FIELD_SEP)) == {old_chunk, new_chunk}
    assert edge["weight"] == 2.0
    for mode in ["local", "global", "hybrid", "mix"]:
        context = await rag.aquery(
            "How do Atlas and Borealis cooperate?",
            param=QueryParam(
                mode=mode,
                only_need_context=True,
                enable_rerank=False,
                top_k=10,
                chunk_top_k=10,
                max_entity_tokens=5000,
                max_relation_tokens=5000,
                max_total_tokens=20000,
            ),
        )
        assert isinstance(context, str), mode
        assert "Atlas" in context and "Borealis" in context, (mode, context)
        assert "Atlas cooperates with Borealis" in context, (mode, context)

    await rag.aedit_entity("Atlas", {"description": "Atlas revised description"})
    assert (await graph.get_node("Atlas"))["description"] == "Atlas revised description"
    await rag.aedit_relation(
        "Borealis", "Atlas", {"description": "Revised cooperation", "weight": 3.0}
    )
    assert (await graph.get_edge("Atlas", "Borealis"))["weight"] == 3.0
    await rag.acreate_entity(
        "Atlas Alias", {"description": "Alias for Atlas", "entity_type": "organization"}
    )
    await rag.acreate_relation(
        "Atlas Alias",
        "Borealis",
        {"description": "Alias cooperation", "keywords": "cooperates", "weight": 0.5},
    )
    await rag.amerge_entities(["Atlas Alias"], "Atlas")
    assert await graph.get_node("Atlas Alias") is None
    assert len(await graph.get_all_edges()) == 1
    assert (await graph.get_edge("Atlas", "Borealis"))["weight"] >= 2

    result = await rag.adelete_by_doc_id("old")
    assert result.status == "success", result.message
    assert await rag.doc_status.get_by_id("old") is None
    assert await rag.text_chunks.get_by_id(old_chunk) is None
    assert await rag.text_chunks.get_by_id(new_chunk) is not None
    for name in ["Atlas", "Borealis"]:
        assert (await graph.get_node(name))["source_id"].split(GRAPH_FIELD_SEP) == [
            new_chunk
        ]
        assert (await rag.entity_chunks.get_by_id(name))["chunk_ids"] == [new_chunk]
    assert (await graph.get_edge("Atlas", "Borealis"))["source_id"].split(
        GRAPH_FIELD_SEP
    ) == [new_chunk]
    relation_key = make_relation_chunk_key("Atlas", "Borealis")
    assert (await rag.relation_chunks.get_by_id(relation_key))["chunk_ids"] == [
        new_chunk
    ]
    assert set((await rag.full_entities.get_by_id("new"))["entity_names"]) == {
        "Atlas",
        "Borealis",
    }
    assert await rag.full_relations.get_by_id("new") is not None


async def test_committed_graph_write_failure_requires_manual_retry_and_converges(
    rag_factory, monkeypatch
):
    rag = await rag_factory()
    graph = rag.chunk_entity_relation_graph
    original = graph.upsert_edge

    async def commit_then_fail(*args, **kwargs):
        await original(*args, **kwargs)
        raise OSError("injected caller failure after acknowledged HugeGraph edge write")

    with monkeypatch.context() as patch:
        patch.setattr(graph, "upsert_edge", commit_then_fail)
        await rag.ainsert(
            "Atlas and Borealis cooperate on a retry project.",
            ids=["retry"],
            file_paths=["retry.txt"],
        )
    assert _status(await rag.doc_status.get_by_id("retry")) == DocStatus.FAILED.value
    assert await graph.has_edge("Atlas", "Borealis") is True
    await rag.finalize_storages()
    restarted = await rag_factory()
    await restarted.apipeline_process_enqueue_documents()
    assert (
        _status(await restarted.doc_status.get_by_id("retry")) == DocStatus.FAILED.value
    )

    from lightrag.kg.pipeline_ingress import PipelineIngressMessage
    from lightrag.kg.shared_storage import get_pipeline_ingress

    ingress = await get_pipeline_ingress(restarted.workspace)
    request_id = uuid4().hex
    ingress.request_manual_retry(
        request_id,
        PipelineIngressMessage(
            kind="rescan",
            retry_failed=True,
            request_id=request_id,
        ),
    )
    await restarted.apipeline_process_enqueue_documents()
    row = await restarted.doc_status.get_by_id("retry")
    assert _status(row) == DocStatus.PROCESSED.value
    edge = await restarted.chunk_entity_relation_graph.get_edge("Atlas", "Borealis")
    assert edge["weight"] == 1.0
    assert edge["source_id"].split(GRAPH_FIELD_SEP) == row["chunks_list"]
    assert len(await restarted.chunk_entity_relation_graph.get_all_nodes()) == 2
    assert len(await restarted.chunk_entity_relation_graph.get_all_edges()) == 1
    assert set((await restarted.full_entities.get_by_id("retry"))["entity_names"]) == {
        "Atlas",
        "Borealis",
    }
    assert await restarted.full_relations.get_by_id("retry") is not None
