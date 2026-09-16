"""HugeGraph's isolated, undirected graph adapter.

Use one shared-storage coordination domain for writers to a scope. See
``docs/HugeGraphStorage.md`` for schema, scan costs and uncertain-write recovery.
"""

from __future__ import annotations

import hashlib
import heapq
import json
import math
from dataclasses import dataclass
from contextlib import asynccontextmanager
from typing import Any, AsyncIterator

from lightrag.distributed.runtime import get_runtime, physical_write, storage_write

from lightrag.base import BaseGraphStorage
from lightrag.kg.hugegraph_client import (
    DATA,
    EDGE_LABEL,
    NAME,
    SCOPE,
    VERTEX_LABEL,
    HugeGraphClient,
)
from lightrag.kg.shared_storage import get_namespace_data, get_storage_keyed_lock
from lightrag.types import KnowledgeGraph, KnowledgeGraphEdge, KnowledgeGraphNode
from lightrag.utils import validate_workspace


_NODES = """
g.V(ids.toArray()).toList().collect { v ->
    [id: v.id().toString(), name: v.value(name_key), data: v.value(data_key),
     scope: v.value(scope_key), label: v.label()]
}
"""
_EDGES = """
def found = [];
pairs.eachWithIndex { p, i ->
    def edges = g.V(p[0]).hasLabel(vlabel).has(scope_key, scope)
        .outE(elabel)
        .filter { e -> e.get().inVertex().id().toString() == p[1] }.toList();
    if (edges.size() > 1) { throw new IllegalStateException('Duplicate relation'); }
    if (!edges.isEmpty()) {
        found.add([idx: i, id: edges[0].id().toString(), data: edges[0].value(data_key),
                   scope: edges[0].value(scope_key)]);
    }
};
found
"""
_ADJACENCY = """
g.V(ids.toArray()).hasLabel(vlabel).has(scope_key, scope).toList().collect { v ->
    def neighbors = g.V(v.id()).bothE(elabel).has(scope_key, scope).otherV()
        .hasLabel(vlabel).has(scope_key, scope).dedup().values(name_key).toList();
    [name: v.value(name_key), neighbors: neighbors]
}
"""
_DEGREES = """
g.V(ids.toArray()).hasLabel(vlabel).has(scope_key, scope).toList().collect { v ->
    [name: v.value(name_key), degree: g.V(v.id()).bothE(elabel)
        .has(scope_key, scope).count().next()]
}
"""
_EXPAND = """
def excluded_set = new HashSet(excluded);
g.V(ids.toArray()).hasLabel(vlabel).has(scope_key, scope)
    .bothE(elabel).has(scope_key, scope).otherV().hasLabel(vlabel).has(scope_key, scope)
    .filter { v -> !excluded_set.contains(v.get().id().toString()) }
    .dedup().limit(bound).values(name_key).toList()
"""
_INDUCED = """
def included_set = new HashSet(included);
g.V(ids.toArray()).hasLabel(vlabel).has(scope_key, scope)
    .outE(elabel).has(scope_key, scope)
    .filter { e -> included_set.contains(e.get().inVertex().id().toString()) }
    .toList().collect { e ->
        [source: e.outVertex().value(name_key), target: e.inVertex().value(name_key),
         data: e.value(data_key)]
    }
"""
_REMOVE_NODES = """
def vertices = g.V(ids.toArray()).hasLabel(vlabel).has(scope_key, scope).toList();
try {
    vertices.each { v -> v.remove() };
    graph.tx().commit();
} catch (Exception e) { graph.tx().rollback(); throw e; }
vertices.size()
"""
_REMOVE_EDGES = """
def count = 0;
try {
    pairs.each { p ->
        def edges = g.V(p[0]).hasLabel(vlabel).has(scope_key, scope)
            .outE(elabel).has(scope_key, scope)
            .filter { e -> e.get().inVertex().id().toString() == p[1] }.toList();
        edges.each { e -> e.remove(); count++ };
    };
    graph.tx().commit();
} catch (Exception e) { graph.tx().rollback(); throw e; }
count
"""
_DROP = """
def elements = kind == 'edges'
    ? g.E().hasLabel(elabel).has(scope_key, scope).limit(bound).toList()
    : g.V().hasLabel(vlabel).has(scope_key, scope).limit(bound).toList();
try {
    elements.each { e -> e.remove() };
    graph.tx().commit();
} catch (Exception e) { graph.tx().rollback(); throw e; }
elements.size()
"""


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), allow_nan=False)


def _attributes(data: Any) -> dict:
    # JSON can carry legacy attribute names that other stores refuse. Only
    # reject values this representation cannot round-trip, not ingress rules.
    if not isinstance(data, dict):
        raise ValueError("HugeGraph attributes must be a dictionary")
    for key, value in data.items():
        if not isinstance(key, str) or not isinstance(value, (str, bool, int, float)):
            raise ValueError(
                "HugeGraph attributes must have string keys and scalar values"
            )
        if isinstance(value, float) and not math.isfinite(value):
            raise ValueError("HugeGraph attributes must be finite")
    return dict(data)


def _decode(value: Any) -> dict:
    if not isinstance(value, str):
        raise ValueError("Malformed HugeGraph attribute payload")
    try:
        return _attributes(json.loads(value))
    except (ValueError, TypeError) as exc:
        raise ValueError("Malformed HugeGraph attribute payload") from exc


def _positive(value: int, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError(f"{name} must be a positive integer")
    return value


@dataclass
class HugeGraphStorage(BaseGraphStorage):
    """Store one workspace/namespace using versioned HugeGraph schema.

    Initialize shared storage before mutation and await ``initialize()`` before
    using this backend. Writes are immediate; a failed response can represent
    a committed write. Never switch only the graph backend of populated RAG
    storage; rebuild into a fresh, consistently configured workspace instead.
    """

    def __post_init__(self) -> None:
        self.workspace = "" if self.workspace is None else self.workspace
        validate_workspace(self.workspace)
        if not isinstance(self.namespace, str) or not self.namespace:
            raise ValueError("HugeGraph namespace must be a nonempty string")
        self._scope = _json([self.workspace, self.namespace])
        self._client = HugeGraphClient()
        self._storage_key = hashlib.sha256(
            _json([self._client.uri, self._client.graph_path, self._scope]).encode()
        ).hexdigest()

    def _vertex_id(self, node_id: str) -> str:
        if not isinstance(node_id, str) or not node_id:
            raise ValueError("HugeGraph entity ID must be a nonempty string")
        return (
            "lr-" + hashlib.sha256(_json([self._scope, node_id]).encode()).hexdigest()
        )

    def _bindings(self, **extra: Any) -> dict:
        return {
            "scope": self._scope,
            "scope_key": SCOPE,
            "name_key": NAME,
            "data_key": DATA,
            "vlabel": VERTEX_LABEL,
            "elabel": EDGE_LABEL,
            **extra,
        }

    def _batches(self, items: list, size: int | None = None):
        size = self._client.batch_size if size is None else size
        for start in range(0, len(items), size):
            yield items[start : start + size]

    @asynccontextmanager
    async def _mutation_lock(self, keys=None):
        runtime = get_runtime(self)
        if runtime is not None:
            if keys is None:
                runtime.permit(exclusive=True)
                yield
            else:
                async with runtime.lock(keys):
                    yield
            return
        # A distinct lock avoids reentering core entity locks. Do not extend its
        # guarantee to independent deployments with separate Manager instances.
        async with get_storage_keyed_lock(
            self._storage_key, namespace="HugeGraphStorage:mutation"
        ):
            self._write_fences = await get_namespace_data(
                "hugegraph_write_fences", workspace=self.workspace
            )
            if self._write_fences.get(self._storage_key):
                raise RuntimeError(
                    "Unconfirmed HugeGraph write fences this scope. Stop all writers, "
                    "confirm server-side requests have finished and inspect committed "
                    "state, then restart the entire LightRAG coordination domain "
                    "before manually retrying. See docs/HugeGraphStorage.md."
                )
            yield

    @asynccontextmanager
    async def _pending_write(self):
        if get_runtime(self) is not None:
            async with physical_write():
                yield
            return
        # Set BEFORE sending, not only in except: a killed worker cannot run
        # cleanup. Clearing only after a verified acknowledgement also fences
        # timeout/cancellation and malformed-success responses. No await occurs
        # between acknowledgement verification and this synchronous clear.
        self._write_fences[self._storage_key] = True
        yield
        del self._write_fences[self._storage_key]

    @storage_write
    async def initialize(self) -> None:
        runtime = get_runtime(self)
        if runtime is not None:
            self._client.auto_create_schema = runtime.permit().maintenance
        await self._client.initialize()

    async def finalize(self) -> None:
        await self._client.close()

    async def index_done_callback(self) -> None:
        """Writes are committed by each successful REST/Gremlin mutation."""

    def _read_vertex(self, row: dict) -> tuple[str, dict]:
        try:
            if row.get("scope") != self._scope or row.get("label") != VERTEX_LABEL:
                raise ValueError("HugeGraph vertex scope or label mismatch")
            name = row["name"]
            if row["id"] != self._vertex_id(name):
                raise ValueError("HugeGraph vertex identity mismatch")
            data = _decode(row["data"])
            if data.get("entity_id", name) != name:
                raise ValueError("HugeGraph entity_id mismatch")
            return name, data
        except (KeyError, TypeError) as exc:
            raise ValueError("Malformed HugeGraph vertex") from exc

    async def _fetch_vertices(self, ids: list[str]) -> dict[str, tuple[str, dict]]:
        result = {}
        for batch in self._batches(list(dict.fromkeys(ids))):
            rows = await self._client.gremlin(_NODES, self._bindings(ids=batch))
            for row in rows:
                name, data = self._read_vertex(row)
                if row["id"] not in batch or row["id"] in result:
                    raise ValueError("Unexpected or duplicate HugeGraph vertex")
                result[row["id"]] = (name, data)
        return result

    async def get_nodes_batch(self, node_ids: list[str]) -> dict[str, dict]:
        rows = await self._fetch_vertices([self._vertex_id(n) for n in node_ids])
        return {name: data for name, data in rows.values()}

    async def get_node(self, node_id: str) -> dict | None:
        return (await self.get_nodes_batch([node_id])).get(node_id)

    async def has_nodes_batch(self, node_ids: list[str]) -> set[str]:
        return set(await self.get_nodes_batch(node_ids))

    async def has_node(self, node_id: str) -> bool:
        return node_id in await self.has_nodes_batch([node_id])

    async def get_edges_batch(
        self, pairs: list[dict[str, str]]
    ) -> dict[tuple[str, str], dict]:
        result = {}
        for batch in self._batches(pairs):
            ids = [
                [self._vertex_id(n) for n in sorted((p["src"], p["tgt"]))]
                for p in batch
            ]
            rows = await self._client.gremlin(_EDGES, self._bindings(pairs=ids))
            seen = set()
            for row in rows:
                if row.get("scope") != self._scope:
                    raise ValueError("HugeGraph edge scope mismatch")
                idx = row.get("idx")
                if type(idx) is not int or not 0 <= idx < len(batch) or idx in seen:
                    raise ValueError("Malformed HugeGraph edge result")
                seen.add(idx)
                pair = batch[idx]
                result[pair["src"], pair["tgt"]] = _decode(row.get("data"))
        return result

    async def get_edge(self, source_node_id: str, target_node_id: str) -> dict | None:
        return (
            await self.get_edges_batch([{"src": source_node_id, "tgt": target_node_id}])
        ).get((source_node_id, target_node_id))

    async def has_edge(self, source_node_id: str, target_node_id: str) -> bool:
        return await self.get_edge(source_node_id, target_node_id) is not None

    async def _adjacency(self, node_ids: list[str]) -> dict[str, list[tuple[str, str]]]:
        result = {}
        for batch in self._batches(list(dict.fromkeys(node_ids))):
            ids = [self._vertex_id(n) for n in batch]
            rows = await self._client.gremlin(_ADJACENCY, self._bindings(ids=ids))
            for row in rows:
                name, neighbors = row.get("name"), row.get("neighbors")
                if (
                    name not in batch
                    or name in result
                    or not isinstance(neighbors, list)
                    or not all(isinstance(n, str) and n for n in neighbors)
                ):
                    raise ValueError("Malformed HugeGraph adjacency")
                result[name] = [(name, n) for n in dict.fromkeys(neighbors)]
        return result

    async def get_node_edges(self, source_node_id: str) -> list[tuple[str, str]] | None:
        return (await self._adjacency([source_node_id])).get(source_node_id)

    async def get_nodes_edges_batch(
        self, node_ids: list[str]
    ) -> dict[str, list[tuple[str, str]]]:
        found = await self._adjacency(node_ids)
        return {n: found.get(n, []) for n in node_ids}

    async def node_degrees_batch(self, node_ids: list[str]) -> dict[str, int]:
        result = dict.fromkeys(node_ids, 0)
        for batch in self._batches(list(dict.fromkeys(node_ids))):
            rows = await self._client.gremlin(
                _DEGREES,
                self._bindings(ids=[self._vertex_id(n) for n in batch]),
            )
            seen = set()
            for row in rows:
                name, degree = row.get("name"), row.get("degree")
                if (
                    name not in batch
                    or name in seen
                    or type(degree) is not int
                    or degree < 0
                ):
                    raise ValueError("Malformed HugeGraph degree")
                seen.add(name)
                result[name] = degree
        return result

    async def node_degree(self, node_id: str) -> int:
        return (await self.node_degrees_batch([node_id]))[node_id]

    async def edge_degrees_batch(
        self, edge_pairs: list[tuple[str, str]]
    ) -> dict[tuple[str, str], int]:
        degrees = await self.node_degrees_batch(
            list(dict.fromkeys(n for pair in edge_pairs for n in pair))
        )
        return {(a, b): degrees[a] + degrees[b] for a, b in edge_pairs}

    async def edge_degree(self, src_id: str, tgt_id: str) -> int:
        return (await self.edge_degrees_batch([(src_id, tgt_id)]))[src_id, tgt_id]

    @staticmethod
    def _ack(result: Any, expected: int) -> None:
        if (
            not isinstance(result, list)
            or len(result) != expected
            or not all(isinstance(x, str) and x for x in result)
            or len(set(result)) != expected
        ):
            raise ValueError(
                "Incomplete HugeGraph write acknowledgement; outcome may be committed"
            )

    @storage_write
    async def upsert_nodes_batch(self, nodes: list[tuple[str, dict[str, str]]]) -> None:
        merged: dict[str, dict] = {}
        for name, attrs in nodes:
            self._vertex_id(name)
            attrs = _attributes(attrs)
            if attrs.get("entity_id", name) != name:
                raise ValueError("HugeGraph entity_id must match the node ID")
            merged.setdefault(name, {}).update(attrs)
        if not merged:
            return
        async with self._mutation_lock(list(merged)):
            for batch in self._batches(list(merged)):
                old = await self.get_nodes_batch(batch)
                records = [
                    {
                        "id": self._vertex_id(n),
                        "label": VERTEX_LABEL,
                        "properties": {
                            SCOPE: self._scope,
                            NAME: n,
                            DATA: _json({**old.get(n, {}), **merged[n]}),
                        },
                    }
                    for n in batch
                ]
                async with self._pending_write():
                    result = await self._client.request(
                        "POST",
                        self._client.graph_path + "/graph/vertices/batch",
                        json=records,
                    )
                    self._ack(result, len(records))
                    if result != [r["id"] for r in records]:
                        raise ValueError(
                            "Unexpected HugeGraph vertex write identities; outcome may be committed"
                        )

    @storage_write
    async def upsert_node(self, node_id: str, node_data: dict[str, str]) -> None:
        await self.upsert_nodes_batch([(node_id, node_data)])

    @storage_write
    async def upsert_edges_batch(
        self, edges: list[tuple[str, str, dict[str, str]]]
    ) -> None:
        merged: dict[tuple[str, str], dict] = {}
        for a, b, attrs in edges:
            self._vertex_id(a)
            self._vertex_id(b)
            merged.setdefault(tuple(sorted((a, b))), {}).update(_attributes(attrs))
        if not merged:
            return
        async with self._mutation_lock(
            list(dict.fromkeys(n for pair in merged for n in pair))
        ):
            # Check every endpoint before the first write, not just each chunk.
            names = list(dict.fromkeys(n for pair in merged for n in pair))
            existing = await self.has_nodes_batch(names)
            if len(existing) != len(names):
                raise ValueError("HugeGraph relation endpoint does not exist")
            for batch in self._batches(list(merged)):
                old = await self.get_edges_batch(
                    [{"src": a, "tgt": b} for a, b in batch]
                )
                records = [
                    {
                        "label": EDGE_LABEL,
                        "outV": self._vertex_id(a),
                        "inV": self._vertex_id(b),
                        "outVLabel": VERTEX_LABEL,
                        "inVLabel": VERTEX_LABEL,
                        "properties": {
                            SCOPE: self._scope,
                            DATA: _json({**old.get((a, b), {}), **merged[a, b]}),
                        },
                    }
                    for a, b in batch
                ]
                async with self._pending_write():
                    result = await self._client.request(
                        "POST",
                        self._client.graph_path + "/graph/edges/batch",
                        params={"check_vertex": "true"},
                        json=records,
                    )
                    self._ack(result, len(records))

    @storage_write
    async def upsert_edge(
        self, source_node_id: str, target_node_id: str, edge_data: dict[str, str]
    ) -> None:
        await self.upsert_edges_batch([(source_node_id, target_node_id, edge_data)])

    @staticmethod
    def _removed(result: Any) -> int:
        if (
            not isinstance(result, list)
            or len(result) != 1
            or type(result[0]) is not int
            or result[0] < 0
        ):
            raise ValueError(
                "Malformed HugeGraph deletion acknowledgement; outcome may be committed"
            )
        return result[0]

    @storage_write
    async def remove_nodes(self, nodes: list[str]) -> None:
        ids = list(dict.fromkeys(self._vertex_id(n) for n in nodes))
        if not ids:
            return
        async with self._mutation_lock(nodes):
            for batch in self._batches(ids):
                await self._fetch_vertices(batch)
                async with self._pending_write():
                    self._removed(
                        await self._client.gremlin(
                            _REMOVE_NODES,
                            self._bindings(ids=batch),
                            read_only=False,
                        )
                    )

    @storage_write
    async def delete_node(self, node_id: str) -> None:
        await self.remove_nodes([node_id])

    @storage_write
    async def remove_edges(self, edges: list[tuple[str, str]]) -> None:
        pairs = [[self._vertex_id(n) for n in sorted(pair)] for pair in edges]
        if not pairs:
            return
        async with self._mutation_lock(
            list(dict.fromkeys(n for pair in edges for n in pair))
        ):
            await self.get_edges_batch([{"src": a, "tgt": b} for a, b in edges])
            for batch in self._batches(pairs):
                async with self._pending_write():
                    self._removed(
                        await self._client.gremlin(
                            _REMOVE_EDGES,
                            self._bindings(pairs=batch),
                            read_only=False,
                        )
                    )

    @storage_write
    async def drop(self) -> dict[str, str]:
        """Delete only this scope, retaining schema and every other scope."""
        async with self._mutation_lock():
            for kind in ("edges", "vertices"):
                while True:
                    async with self._pending_write():
                        removed = self._removed(
                            await self._client.gremlin(
                                _DROP,
                                self._bindings(
                                    kind=kind, bound=self._client.batch_size
                                ),
                                read_only=False,
                            )
                        )
                    if not removed:
                        break
        return {
            "status": "success",
            "message": "HugeGraph workspace/namespace data deleted",
        }

    async def _pages(self, kind: str, batch_size: int) -> AsyncIterator[list[dict]]:
        _positive(batch_size, "batch_size")
        size = min(batch_size, self._client.batch_size)
        label = VERTEX_LABEL if kind == "vertices" else EDGE_LABEL
        page = ""
        # Brent's cycle detector keeps one checkpoint rather than retaining
        # every cursor (which would make maintenance memory grow with the graph).
        checkpoint, distance, power = "", 0, 1
        while True:
            response = await self._client.request(
                "GET",
                self._client.graph_path + "/graph/" + kind,
                params={
                    "label": label,
                    "properties": _json({SCOPE: self._scope}),
                    "page": page,
                    "limit": size,
                },
                retry=True,
            )
            if (
                not isinstance(response, dict)
                or not isinstance(response.get(kind), list)
                or "page" not in response
            ):
                raise ValueError("Malformed HugeGraph page")
            records = response[kind]
            next_page = response.get("page")
            if next_page is not None and not isinstance(next_page, str):
                raise ValueError("Malformed HugeGraph page cursor")
            if next_page and (next_page == page or not records):
                raise ValueError("HugeGraph pagination cursor made no progress")
            if next_page:
                if next_page == checkpoint:
                    raise ValueError("HugeGraph pagination cursor contains a cycle")
                distance += 1
                if distance == power:
                    checkpoint, distance, power = next_page, 0, power * 2
            for record in records:
                if (
                    not isinstance(record, dict)
                    or record.get("label") != label
                    or not isinstance(record.get("properties"), dict)
                    or record["properties"].get(SCOPE) != self._scope
                ):
                    raise ValueError("HugeGraph returned an object outside this scope")
            # Validate the service response, but still bound each yielded batch
            # if a compatible server sends more records than the requested cap.
            for batch in self._batches(records, size):
                yield batch
            if not next_page:
                break
            page = next_page

    def _rest_vertex(self, record: dict) -> tuple[str, dict]:
        props = record["properties"]
        return self._read_vertex(
            {
                "id": record.get("id"),
                "name": props.get(NAME),
                "data": props.get(DATA),
                "scope": props.get(SCOPE),
                "label": record.get("label"),
            }
        )

    async def iter_labels(self, batch_size: int) -> AsyncIterator[list[str]]:
        async for records in self._pages("vertices", batch_size):
            yield [self._rest_vertex(r)[0] for r in records]

    async def get_all_labels(self) -> list[str]:
        return sorted(
            n
            for batch in [b async for b in self.iter_labels(self._client.batch_size)]
            for n in batch
        )

    async def get_all_nodes(self) -> list[dict]:
        result = []
        async for records in self._pages("vertices", self._client.batch_size):
            for record in records:
                name, attrs = self._rest_vertex(record)
                result.append({**attrs, "id": name})
        return result

    async def iter_edges(self, batch_size: int) -> AsyncIterator[list[dict]]:
        async for records in self._pages("edges", batch_size):
            ids = []
            for r in records:
                if not isinstance(r.get("outV"), str) or not isinstance(
                    r.get("inV"), str
                ):
                    raise ValueError("Malformed HugeGraph edge endpoints")
                ids.extend([r["outV"], r["inV"]])
            vertices = await self._fetch_vertices(ids)
            batch = []
            for r in records:
                if r["outV"] not in vertices or r["inV"] not in vertices:
                    raise ValueError(
                        "HugeGraph edge endpoint is missing or outside this scope"
                    )
                a, b = vertices[r["outV"]][0], vertices[r["inV"]][0]
                if a > b:
                    raise ValueError("HugeGraph relation is not canonical")
                batch.append(
                    {**_decode(r["properties"].get(DATA)), "source": a, "target": b}
                )
            yield batch

    async def get_all_edges(self) -> list[dict]:
        result = []
        async for batch in self.iter_edges(self._client.batch_size):
            result.extend(batch)
        return result

    async def get_popular_labels(self, limit: int = 300) -> list[str]:
        if limit <= 0:
            return []
        best: list[tuple[int, str]] = []
        async for names in self.iter_labels(self._client.batch_size):
            degrees = await self.node_degrees_batch(names)
            best = heapq.nsmallest(limit, best + [(-degrees[n], n) for n in names])
        return [name for _, name in best]

    async def search_labels(self, query: str, limit: int = 50) -> list[str]:
        query = query.lower().strip()
        if not query or limit <= 0:
            return []
        best: list[tuple[int, int, str]] = []
        async for names in self.iter_labels(self._client.batch_size):
            candidates = []
            for name in names:
                folded = name.lower()
                if query in folded:
                    rank = (
                        0 if folded == query else 1 if folded.startswith(query) else 2
                    )
                    candidates.append((rank, len(name), name))
            best = heapq.nsmallest(limit, best + candidates)
        return [name for _, _, name in best]

    async def get_knowledge_graph(
        self, node_label: str, max_depth: int = 3, max_nodes: int = 1000
    ) -> KnowledgeGraph:
        if type(max_depth) is not int or max_depth < 0:
            raise ValueError("max_depth must be a nonnegative integer")
        max_nodes = min(
            _positive(max_nodes, "max_nodes"),
            _positive(
                self.global_config.get("max_graph_nodes", 1000), "max_graph_nodes"
            ),
        )
        truncated = False
        if node_label == "*":
            names = await self.get_popular_labels(max_nodes + 1)
            truncated = len(names) > max_nodes
            names = names[:max_nodes]
        else:
            root = await self.get_node(node_label)
            if root is None:
                return KnowledgeGraph()
            names = [node_label]
            seen = {node_label}
            frontier = [node_label]
            for _ in range(max_depth):
                next_frontier = []
                for batch in self._batches(frontier):
                    found = await self._client.gremlin(
                        _EXPAND,
                        self._bindings(
                            ids=[self._vertex_id(n) for n in batch],
                            excluded=[self._vertex_id(n) for n in seen],
                            bound=max_nodes - len(seen) + 1,
                        ),
                    )
                    for n in found:
                        self._vertex_id(n)
                        if n in seen:
                            continue
                        if len(seen) == max_nodes:
                            truncated = True
                            break
                        seen.add(n)
                        names.append(n)
                        next_frontier.append(n)
                    if truncated:
                        break
                if truncated or not next_frontier:
                    break
                frontier = next_frontier
        properties = await self.get_nodes_batch(names)
        graph = KnowledgeGraph(is_truncated=truncated)
        graph.nodes = [
            KnowledgeGraphNode(id=n, labels=[n], properties={**attrs, "entity_id": n})
            for n, attrs in properties.items()
        ]
        included = [self._vertex_id(n) for n in properties]
        seen_edges = set()
        for batch in self._batches(list(properties)):
            # The source batch and the included target set both cross the HTTP
            # boundary as Gremlin bindings.  Repeating every selected node id in
            # each source-batch request can exceed HugeGraph's request-size
            # limit for label=* graph views, so chunk the target set too.
            for included_batch in self._batches(included):
                rows = await self._client.gremlin(
                    _INDUCED,
                    self._bindings(
                        ids=[self._vertex_id(n) for n in batch],
                        included=included_batch,
                    ),
                )
                for row in rows:
                    a, b = row.get("source"), row.get("target")
                    if (
                        a not in properties
                        or b not in properties
                        or a > b
                        or (a, b) in seen_edges
                    ):
                        raise ValueError("Malformed HugeGraph induced edge")
                    seen_edges.add((a, b))
                    edge_id = (
                        "lr-e-"
                        + hashlib.sha256(
                            _json([self._scope, a, b]).encode()
                        ).hexdigest()
                    )
                    graph.edges.append(
                        KnowledgeGraphEdge(
                            id=edge_id,
                            type="RELATED",
                            source=a,
                            target=b,
                            properties=_decode(row.get("data")),
                        )
                    )
        return graph
