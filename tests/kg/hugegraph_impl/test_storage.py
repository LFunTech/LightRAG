"""HugeGraph adapter contracts; only the external protocol is substituted."""

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from uuid import uuid4

from lightrag.kg.shared_storage import initialize_share_data


@pytest.fixture(autouse=True)
def isolated_mutation_fences(monkeypatch):
    monkeypatch.setenv("HUGEGRAPH_GRAPH", "unit-" + uuid4().hex)


def storage(monkeypatch, workspace="test", namespace="kg", uri="http://127.0.0.1:8080"):
    from lightrag.kg.hugegraph_impl import HugeGraphStorage

    monkeypatch.setenv("HUGEGRAPH_URI", uri)
    initialize_share_data()
    result = HugeGraphStorage(namespace, workspace, {}, None)
    result._client = SimpleNamespace(
        batch_size=2,
        graph_path="/graphspaces/DEFAULT/graphs/hugegraph",
        gremlin=AsyncMock(return_value=[]),
        request=AsyncMock(return_value=[]),
        initialize=AsyncMock(),
        close=AsyncMock(),
    )
    return result


def row(s, name, **attrs):
    return {
        "id": s._vertex_id(name),
        "name": name,
        "data": json.dumps(attrs),
        "scope": s._scope,
        "label": "lightrag_entity_v1",
    }


def vertex(s, name, **attrs):
    return {
        "id": s._vertex_id(name),
        "label": "lightrag_entity_v1",
        "properties": {
            "lightrag_scope": s._scope,
            "lightrag_name": name,
            "lightrag_data": json.dumps(attrs),
        },
    }


async def test_ids_are_stable_bounded_and_isolated(monkeypatch):
    a = storage(monkeypatch)
    b = storage(monkeypatch, workspace="other")
    c = storage(monkeypatch, namespace="other")
    text = "中文/\\\"'🙂" * 300
    assert a._vertex_id(text) == storage(monkeypatch)._vertex_id(text)
    assert len(a._vertex_id(text)) < 100
    assert len({a._vertex_id(text), b._vertex_id(text), c._vertex_id(text)}) == 3


async def test_node_batch_merges_scalars_without_interpolating_names(monkeypatch):
    s = storage(monkeypatch)
    name = "'); g.V().drop(); //中文"
    s._client.gremlin.return_value = [row(s, name, keep=True, score=1)]
    writes = []

    async def request(method, path, **kwargs):
        writes.extend(kwargs["json"])
        return [v["id"] for v in kwargs["json"]]

    s._client.request.side_effect = request
    await s.upsert_nodes_batch([(name, {"score": 2.5}), (name, {"description": "new"})])
    assert len(writes) == 1
    props = writes[0]["properties"]
    assert props["lightrag_name"] == name
    assert json.loads(props["lightrag_data"]) == {
        "keep": True,
        "score": 2.5,
        "description": "new",
    }
    script, bindings = s._client.gremlin.call_args.args
    assert name not in script
    assert s._scope in bindings.values()


@pytest.mark.parametrize("value", [None, [], {}, float("nan"), float("inf")])
async def test_invalid_attributes_rejected_before_any_write(monkeypatch, value):
    s = storage(monkeypatch)
    with pytest.raises(ValueError):
        await s.upsert_nodes_batch([("valid", {}), ("bad", {"x": value})])
    assert s._client.request.await_count == 0


async def test_read_preserves_types_and_missing_is_not_failure(monkeypatch):
    s = storage(monkeypatch)
    s._client.gremlin.return_value = [row(s, "A", flag=True, count=3, weight=0.25)]
    assert await s.get_node("A") == {
        "flag": True,
        "count": 3,
        "weight": 0.25,
    }
    s._client.gremlin.return_value = []
    assert await s.get_node("missing") is None
    s._client.gremlin.side_effect = TimeoutError("service timeout")
    with pytest.raises(TimeoutError):
        await s.get_node("missing")


async def test_corrupt_payload_and_wrong_identity_fail_closed(monkeypatch):
    s = storage(monkeypatch)
    for r in [dict(row(s, "A"), data="[]"), dict(row(s, "A"), id="wrong")]:
        s._client.gremlin.return_value = [r]
        with pytest.raises(ValueError):
            await s.get_node("A")


async def test_reverse_edge_batch_merges_not_accumulates(monkeypatch):
    s = storage(monkeypatch)
    s._client.gremlin.side_effect = [
        [row(s, "A"), row(s, "B")],
        [
            {
                "idx": 0,
                "data": '{"keep":true,"weight":8}',
                "id": "edge",
                "scope": s._scope,
            }
        ],
    ]
    s._client.request.return_value = ["edge"]
    await s.upsert_edges_batch(
        [("B", "A", {"weight": 1}), ("A", "B", {"description": "d"})]
    )
    written = s._client.request.call_args.kwargs["json"]
    assert len(written) == 1
    assert written[0]["outV"] == s._vertex_id("A")
    assert written[0]["inV"] == s._vertex_id("B")
    assert json.loads(written[0]["properties"]["lightrag_data"]) == {
        "keep": True,
        "weight": 1,
        "description": "d",
    }


async def test_missing_endpoint_is_not_success(monkeypatch):
    s = storage(monkeypatch)
    s._client.gremlin.return_value = [row(s, "A")]
    with pytest.raises(ValueError, match="endpoint"):
        await s.upsert_edge("A", "B", {"weight": 1})
    assert s._client.request.await_count == 0


async def test_short_batch_ack_is_not_success(monkeypatch):
    s = storage(monkeypatch)
    s._client.request.return_value = []
    with pytest.raises(ValueError):
        await s.upsert_node("A", {})


async def test_edges_read_returns_requested_orientation(monkeypatch):
    s = storage(monkeypatch)
    s._client.gremlin.return_value = [
        {"idx": 0, "data": '{"weight":0.25}', "id": "e", "scope": s._scope}
    ]
    assert await s.get_edges_batch([{"src": "B", "tgt": "A"}]) == {
        ("B", "A"): {"weight": 0.25}
    }
    assert await s.get_edge("B", "A") == {"weight": 0.25}


async def test_adjacency_distinguishes_absent_isolated_and_failure(monkeypatch):
    s = storage(monkeypatch)
    assert await s.get_node_edges("A") is None
    s._client.gremlin.return_value = [{"name": "A", "neighbors": []}]
    assert await s.get_node_edges("A") == []
    s._client.gremlin.return_value = [{"name": "A", "neighbors": ["B"]}]
    assert await s.get_node_edges("A") == [("A", "B")]
    s._client.gremlin.side_effect = RuntimeError("backend error")
    with pytest.raises(RuntimeError):
        await s.get_node_edges("A")


async def test_label_iteration_uses_native_cursor_not_full_collection(monkeypatch):
    s = storage(monkeypatch)
    s._client.request.side_effect = [
        {"vertices": [vertex(s, "B"), vertex(s, "A")], "page": "cursor"},
        {"vertices": [vertex(s, "C")], "page": None},
    ]
    iterator = s.iter_labels(2)
    assert await anext(iterator) == ["B", "A"]
    assert s._client.request.await_count == 1
    assert await anext(iterator) == ["C"]
    with pytest.raises(StopAsyncIteration):
        await anext(iterator)
    assert s._client.request.call_args.kwargs["params"]["page"] == "cursor"


async def test_repeated_cursor_and_scope_mismatch_raise(monkeypatch):
    s = storage(monkeypatch)
    s._client.request.return_value = {"vertices": [vertex(s, "A")], "page": "same"}
    with pytest.raises(ValueError, match="cursor"):
        _ = [batch async for batch in s.iter_labels(2)]
    bad = vertex(s, "A")
    bad["properties"]["lightrag_scope"] = "another workspace"
    s._client.request.return_value = {"vertices": [bad], "page": None}
    with pytest.raises(ValueError):
        _ = [batch async for batch in s.iter_labels(2)]


async def test_rank_includes_isolated_unicode_codepoint_ties(monkeypatch):
    s = storage(monkeypatch)
    s._client.request.return_value = {
        "vertices": [vertex(s, n) for n in ["🙂", "\ue000", "B", "A"]],
        "page": None,
    }
    s._client.gremlin.side_effect = [
        [{"name": "🙂", "degree": 0}, {"name": "\ue000", "degree": 0}],
        [{"name": "B", "degree": 1}, {"name": "A", "degree": 1}],
    ]
    assert await s.get_popular_labels(4) == ["A", "B", "\ue000", "🙂"]


async def test_search_ranks_exact_prefix_then_substring(monkeypatch):
    s = storage(monkeypatch)
    s._client.request.return_value = {
        "vertices": [vertex(s, n) for n in ["ZAlpha", "Alphabet", "Alpha", "Other"]],
        "page": None,
    }
    assert await s.search_labels("alpha", 3) == ["Alpha", "Alphabet", "ZAlpha"]
    assert await s.search_labels("  ") == []


@pytest.mark.parametrize("method,args", [("iter_labels", (0,)), ("iter_edges", (-1,))])
async def test_invalid_page_sizes_fail(monkeypatch, method, args):
    s = storage(monkeypatch)
    with pytest.raises(ValueError):
        await anext(getattr(s, method)(*args))


async def test_bfs_cap_uses_original_ids_and_induced_edges(monkeypatch):
    s = storage(monkeypatch)
    # Exact root; expansion includes one extra node to establish truncation.
    s._client.gremlin.side_effect = [
        [row(s, "A")],
        ["B", "C"],
        [row(s, "A"), row(s, "B")],
        [{"source": "A", "target": "B", "data": '{"weight":1}'}],
    ]
    graph = await s.get_knowledge_graph("A", max_depth=2, max_nodes=2)
    assert {n.id for n in graph.nodes} == {"A", "B"}
    assert graph.is_truncated
    assert [(e.source, e.target) for e in graph.edges] == [("A", "B")]
    assert all(n.properties["entity_id"] == n.id for n in graph.nodes)


async def test_induced_edge_queries_chunk_included_ids_to_stay_under_request_limit(
    monkeypatch,
):
    from lightrag.kg.hugegraph_impl import _INDUCED, _NODES

    s = storage(monkeypatch)
    names = ["A", "B", "C", "D", "E"]
    ids_to_names = {s._vertex_id(name): name for name in names}
    included_lengths = []
    s.get_popular_labels = AsyncMock(return_value=names)

    async def gremlin(script, bindings, **_kwargs):
        if script == _NODES:
            return [row(s, ids_to_names[vertex_id]) for vertex_id in bindings["ids"]]
        if script == _INDUCED:
            included_lengths.append(len(bindings["included"]))
            source_names = [ids_to_names[vertex_id] for vertex_id in bindings["ids"]]
            included_names = {
                ids_to_names[vertex_id] for vertex_id in bindings["included"]
            }
            if "A" in source_names and "C" in included_names:
                return [{"source": "A", "target": "C", "data": '{"weight":1}'}]
            return []
        raise AssertionError(f"unexpected gremlin script: {script}")

    s._client.gremlin.side_effect = gremlin

    graph = await s.get_knowledge_graph("*", max_nodes=len(names))

    assert {node.id for node in graph.nodes} == set(names)
    assert [(edge.source, edge.target) for edge in graph.edges] == [("A", "C")]
    assert included_lengths
    assert max(included_lengths) <= s._client.batch_size


async def test_depth_zero_does_not_expand(monkeypatch):
    s = storage(monkeypatch)
    s._client.gremlin.side_effect = [[row(s, "A")], [row(s, "A")], []]
    graph = await s.get_knowledge_graph("A", max_depth=0, max_nodes=10)
    assert [n.id for n in graph.nodes] == ["A"]
    assert not graph.is_truncated


async def test_drop_never_calls_graph_clear_or_schema_delete(monkeypatch):
    s = storage(monkeypatch)
    # Each bounded mutation returns the number removed; zero ends that phase.
    s._client.gremlin.side_effect = [[2], [0], [1], [0]]
    result = await s.drop()
    assert result["status"] == "success"
    assert s._client.request.await_count == 0
    assert s._client.gremlin.await_count == 4
    for call in s._client.gremlin.call_args_list:
        assert s._scope in call.args[1].values()
        assert call.kwargs["read_only"] is False


async def test_empty_inputs_do_not_call_service(monkeypatch):
    s = storage(monkeypatch)
    assert await s.get_nodes_batch([]) == {}
    assert await s.has_nodes_batch([]) == set()
    assert await s.get_edges_batch([]) == {}
    assert await s.get_nodes_edges_batch([]) == {}
    assert await s.node_degrees_batch([]) == {}
    await s.upsert_nodes_batch([])
    await s.upsert_edges_batch([])
    await s.remove_nodes([])
    await s.remove_edges([])
    assert s._client.gremlin.await_count == 0
    assert s._client.request.await_count == 0


async def test_property_bag_does_not_gain_unrequested_attributes(monkeypatch):
    s = storage(monkeypatch)
    s._client.gremlin.return_value = [row(s, "A", owner=1)]
    assert await s.get_node("A") == {"owner": 1}


async def test_duplicate_acknowledgement_cannot_hide_missing_edge_write(monkeypatch):
    s = storage(monkeypatch)
    with pytest.raises(ValueError):
        s._ack(["same-edge", "same-edge"], 2)


async def test_wrong_scope_vertex_is_not_treated_as_absent_for_upsert(monkeypatch):
    s = storage(monkeypatch)
    collision = row(s, "A", preserve="another owner")
    collision.update(scope="another scope", label="lightrag_entity_v1")
    s._client.gremlin.return_value = [collision]
    with pytest.raises(ValueError, match="scope"):
        await s.upsert_node("A", {"new": "value"})
    assert s._client.request.await_count == 0


async def test_missing_page_metadata_is_not_complete_iteration(monkeypatch):
    s = storage(monkeypatch)
    s._client.request.return_value = {"vertices": [vertex(s, "A")]}
    with pytest.raises(ValueError, match="page"):
        _ = [batch async for batch in s.iter_labels(2)]


async def test_multi_step_cursor_cycle_fails_in_bounded_time(monkeypatch):
    s = storage(monkeypatch)
    s._client.request.side_effect = [
        {"vertices": [vertex(s, "A")], "page": cursor}
        for cursor in ["a", "b", "a", "b", "a", "b", "a", "b"]
    ]
    with pytest.raises(ValueError, match="cursor"):
        _ = [batch async for batch in s.iter_labels(2)]


async def test_list_bindings_need_no_server_side_json_parser(monkeypatch):
    s = storage(monkeypatch)
    await s.get_nodes_batch(["A", "B"])
    bindings = s._client.gremlin.call_args.args[1]
    assert isinstance(bindings.get("ids"), list)
    assert len(bindings["ids"]) == 2


async def test_wrong_scope_edge_is_not_overwritten_or_deleted(monkeypatch):
    s = storage(monkeypatch)
    edge = {
        "idx": 0,
        "data": '{"preserve":true}',
        "id": "edge",
        "scope": "another scope",
    }
    s._client.gremlin.side_effect = [[row(s, "A"), row(s, "B")], [edge]]
    with pytest.raises(ValueError, match="scope"):
        await s.upsert_edge("A", "B", {"weight": 1})
    assert s._client.request.await_count == 0
    s._client.gremlin.reset_mock(side_effect=True)
    s._client.gremlin.return_value = [edge]
    with pytest.raises(ValueError, match="scope"):
        await s.remove_edges([("A", "B")])
    assert all(
        c.kwargs.get("read_only", True) for c in s._client.gremlin.call_args_list
    )


async def test_wrong_scope_node_is_not_silently_skipped_during_delete(monkeypatch):
    s = storage(monkeypatch)
    collision = row(s, "A", preserve=True)
    collision["scope"] = "another scope"
    s._client.gremlin.return_value = [collision]
    with pytest.raises(ValueError, match="scope"):
        await s.delete_node("A")
    assert all(
        c.kwargs.get("read_only", True) for c in s._client.gremlin.call_args_list
    )


async def test_uncertain_commit_fences_queued_writer_and_survives_reinitialize(
    monkeypatch,
):
    import asyncio

    first = storage(monkeypatch)
    second = storage(monkeypatch)
    started, release = asyncio.Event(), asyncio.Event()
    calls = []
    committed = {"seed": True}
    delayed = []

    async def read(*args, **kwargs):
        return [row(first, "A", **committed)]

    async def write(method, path, **kwargs):
        calls.append(kwargs["json"])
        payload = json.loads(kwargs["json"][0]["properties"]["lightrag_data"])
        if len(calls) == 1:
            delayed.append(payload)
            started.set()
            await release.wait()
            raise TimeoutError("response unknown, server still executing")
        committed.clear()
        committed.update(payload)
        return [kwargs["json"][0]["id"]]

    for s in [first, second]:
        s._client.gremlin.side_effect = read
        s._client.request.side_effect = write
    pending = asyncio.create_task(first.upsert_node("A", {"first": 1}))
    await started.wait()
    successor = asyncio.create_task(second.upsert_node("A", {"second": 2}))
    release.set()
    with pytest.raises(TimeoutError):
        await pending
    with pytest.raises(RuntimeError, match="unconfirmed|Unconfirmed"):
        await successor
    assert len(calls) == 1
    committed.update(delayed.pop())
    await second.finalize()
    await second.initialize()
    with pytest.raises(RuntimeError, match="unconfirmed|Unconfirmed"):
        await second.upsert_node("A", {"third": 3})
    assert await second.get_node("A") == {"seed": True, "first": 1}


async def test_cancelled_mutation_fences_later_deletion(monkeypatch):
    import asyncio

    s = storage(monkeypatch)
    s._client.request.side_effect = asyncio.CancelledError()
    with pytest.raises(asyncio.CancelledError):
        await s.upsert_node("A", {})
    with pytest.raises(RuntimeError, match="unconfirmed|Unconfirmed"):
        await storage(monkeypatch).drop()


async def test_confirmed_write_clears_fence_and_read_failure_does_not_poison(
    monkeypatch,
):
    s = storage(monkeypatch)
    s._client.gremlin.side_effect = TimeoutError("read unavailable")
    with pytest.raises(TimeoutError):
        await s.upsert_node("A", {})
    s._client.gremlin.side_effect = None
    s._client.request.return_value = [s._vertex_id("A")]
    await s.upsert_node("A", {})
    await s.upsert_node("A", {"next": 1})
    assert s._client.request.await_count == 2


@pytest.mark.parametrize(
    "first_uri,second_uri",
    [
        ("HTTP://LOCALHOST:80", "http://localhost"),
        ("https://LOCALHOST:443/p%61th/", "https://localhost/path"),
        ("http://[0:0:0:0:0:0:0:1]:80", "http://[::1]"),
    ],
)
async def test_equivalent_transport_urls_cannot_bypass_fence(
    monkeypatch, first_uri, second_uri
):
    first = storage(monkeypatch, uri=first_uri)
    second = storage(monkeypatch, uri=second_uri)
    first._client.request.side_effect = TimeoutError("server may still commit")
    second._client.request.return_value = [second._vertex_id("A")]
    with pytest.raises(TimeoutError):
        await first.upsert_node("A", {"first": 1})
    with pytest.raises(RuntimeError, match="Unconfirmed"):
        await second.upsert_node("A", {"second": 2})
    second._client.request.assert_not_awaited()


async def test_pending_fence_does_not_block_other_destination_scopes(monkeypatch):
    first = storage(monkeypatch)
    first._client.request.side_effect = TimeoutError("unconfirmed")
    with pytest.raises(TimeoutError):
        await first.upsert_node("A", {})
    others = [
        storage(monkeypatch, workspace="other"),
        storage(monkeypatch, namespace="other"),
        storage(monkeypatch, uri="http://another-service.invalid:8080"),
    ]
    with monkeypatch.context() as patch:
        patch.setenv("HUGEGRAPH_GRAPH", "another-graph")
        others.append(storage(patch))
    with monkeypatch.context() as patch:
        patch.setenv("HUGEGRAPH_GRAPHSPACE", "another-graphspace")
        others.append(storage(patch))
    for other in others:
        other._client.request.return_value = [other._vertex_id("A")]
        await other.upsert_node("A", {})
        other._client.request.assert_awaited_once()
