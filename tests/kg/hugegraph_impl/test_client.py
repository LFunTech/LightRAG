"""HugeGraph's HTTP boundary is exercised against an isolated mock HTTP service."""

import asyncio
import copy
import gzip
import json
import os
from collections import defaultdict, deque

import pytest
import pytest_asyncio
from aiohttp import web

from lightrag.kg.hugegraph_client import HugeGraphClient, HugeGraphClientError


GRAPH_PATH = "/graphspaces/DEFAULT/graphs/hugegraph"
SCHEMA = {
    "propertykeys": [
        {
            "name": name,
            "data_type": "TEXT",
            "cardinality": "SINGLE",
            "aggregate_type": "NONE",
            "write_type": "OLTP",
            "properties": [],
            "status": "CREATED",
        }
        for name in ("lightrag_scope", "lightrag_name", "lightrag_data")
    ],
    "vertexlabels": [
        {
            "name": "lightrag_entity_v1",
            "id_strategy": "CUSTOMIZE_STRING",
            "properties": ["lightrag_scope", "lightrag_name", "lightrag_data"],
            "primary_keys": [],
            "nullable_keys": [],
            "enable_label_index": True,
            "ttl": 0,
            "ttl_start_time": "",
            "status": "CREATED",
        }
    ],
    "edgelabels": [
        {
            "name": "lightrag_relation_v1",
            "source_label": "lightrag_entity_v1",
            "target_label": "lightrag_entity_v1",
            "frequency": "SINGLE",
            "properties": ["lightrag_scope", "lightrag_data"],
            "sort_keys": [],
            "nullable_keys": [],
            "enable_label_index": True,
            "ttl": 0,
            "ttl_start_time": "",
            "status": "CREATED",
        }
    ],
    "indexlabels": [
        {
            "name": name,
            "base_type": base_type,
            "base_value": base_value,
            "index_type": "SECONDARY",
            "fields": fields,
            "status": "CREATED",
        }
        for name, base_type, base_value, fields in [
            (
                "lightrag_entity_scope_name_v1",
                "VERTEX_LABEL",
                "lightrag_entity_v1",
                ["lightrag_scope", "lightrag_name"],
            ),
            (
                "lightrag_relation_scope_v1",
                "EDGE_LABEL",
                "lightrag_relation_v1",
                ["lightrag_scope"],
            ),
        ]
    ],
}


class MockHugeGraph:
    def __init__(self):
        self.schema = copy.deepcopy(SCHEMA)
        self.responses = defaultdict(deque)
        self.requests = []
        self.version = "1.7.0"
        self.task_status = deque(["success"])

    async def handle(self, request):
        body = await request.json() if request.can_read_body else None
        self.requests.append(
            (request.method, request.path, body, dict(request.headers))
        )
        pending = self.responses[(request.method, request.path)]
        if pending:
            response = pending.popleft()
            if callable(response):
                return await response(request)
            return response
        if request.path == "/versions":
            return web.json_response({"versions": {"core": self.version}})
        if "/schema/" in request.path:
            suffix = request.path.split("/schema/", 1)[1].split("/")
            group = suffix[0]
            if request.method == "GET":
                if len(suffix) == 1:
                    return web.json_response({group: self.schema[group]})
                for item in self.schema[group]:
                    if item["name"] == suffix[1]:
                        return web.json_response(item)
                return web.json_response(
                    {
                        "exception": "class org.apache.hugegraph.exception.NotFoundException"
                    },
                    status=404,
                )
            if request.method == "POST":
                item = {**body, "status": "CREATED"}
                if group == "indexlabels":
                    related = [
                        old
                        for old in self.schema[group]
                        if old["base_type"] == body["base_type"]
                        and old["base_value"] == body["base_value"]
                    ]
                    if any(
                        old["fields"][: len(body["fields"])] == body["fields"]
                        for old in related
                    ):
                        return web.json_response(
                            {"exception": "class java.lang.IllegalArgumentException"},
                            status=400,
                        )
                    # HugeGraph eliminates existing SECONDARY prefix indexes.
                    self.schema[group] = [
                        old
                        for old in self.schema[group]
                        if old not in related
                        or body["fields"][: len(old["fields"])] != old["fields"]
                    ]
                self.schema[group].append(item)
                if group == "indexlabels":
                    return web.json_response(
                        {"index_label": item, "task_id": 123}, status=202
                    )
                return web.json_response(item, status=201)
        if "/tasks/" in request.path:
            status = self.task_status[0]
            if len(self.task_status) > 1:
                self.task_status.popleft()
            return web.json_response({"id": 123, "task_status": status})
        return web.json_response({"message": "unknown mock route"}, status=404)


@pytest.fixture(autouse=True)
def clean_config(monkeypatch):
    for key in os.environ:
        if key.startswith("HUGEGRAPH_"):
            monkeypatch.delenv(key)
    monkeypatch.setenv("HUGEGRAPH_URI", "http://127.0.0.1:8080")


@pytest_asyncio.fixture
async def server(monkeypatch):
    service = MockHugeGraph()
    app = web.Application()
    app.router.add_route("*", "/{path:.*}", service.handle)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    port = site._server.sockets[0].getsockname()[1]
    monkeypatch.setenv("HUGEGRAPH_URI", f"http://127.0.0.1:{port}")
    try:
        yield service
    finally:
        await runner.cleanup()


@pytest_asyncio.fixture
async def client(server):
    value = HugeGraphClient()
    await value.initialize()
    try:
        yield value
    finally:
        await value.close()


@pytest.mark.parametrize(
    "name,value",
    [
        ("HUGEGRAPH_URI", ""),
        ("HUGEGRAPH_URI", "ftp://localhost"),
        ("HUGEGRAPH_URI", "http://user:secret@localhost"),
        ("HUGEGRAPH_URI", "http://localhost?token=secret"),
        ("HUGEGRAPH_URI", "http://localhost#fragment"),
        ("HUGEGRAPH_URI", "http://localhost:broken"),
        ("HUGEGRAPH_URI", "http://localhost/../other"),
        ("HUGEGRAPH_URI", "http://localhost/%2e%2e/other"),
        ("HUGEGRAPH_URI", "http://local\nhost"),
        ("HUGEGRAPH_GRAPH", ""),
        ("HUGEGRAPH_GRAPH", ".."),
        ("HUGEGRAPH_GRAPHSPACE", " "),
        ("HUGEGRAPH_GRAPHSPACE", "bad\nname"),
        ("HUGEGRAPH_TIMEOUT", "0"),
        ("HUGEGRAPH_TIMEOUT", "nan"),
        ("HUGEGRAPH_TIMEOUT", "inf"),
        ("HUGEGRAPH_TIMEOUT", "invalid"),
        ("HUGEGRAPH_BATCH_SIZE", "0"),
        ("HUGEGRAPH_BATCH_SIZE", "1.5"),
        ("HUGEGRAPH_MAX_CONNECTIONS", "-1"),
        ("HUGEGRAPH_RETRIES", "-1"),
        ("HUGEGRAPH_RETRIES", "1000000"),
        ("HUGEGRAPH_AUTO_CREATE_SCHEMA", "typo"),
        ("HUGEGRAPH_USERNAME", "user"),
        ("HUGEGRAPH_PASSWORD", "secret"),
        ("HUGEGRAPH_TOKEN", "token\r\nInjected: header"),
    ],
)
def test_invalid_config_rejected_before_connecting(monkeypatch, name, value):
    monkeypatch.setenv(name, value)
    with pytest.raises(ValueError, match=name):
        HugeGraphClient()


def test_missing_uri_rejected(monkeypatch):
    monkeypatch.delenv("HUGEGRAPH_URI")
    with pytest.raises(ValueError, match="HUGEGRAPH_URI"):
        HugeGraphClient()


def test_conflicting_auth_rejected(monkeypatch):
    monkeypatch.setenv("HUGEGRAPH_USERNAME", "user")
    monkeypatch.setenv("HUGEGRAPH_PASSWORD", "secret")
    monkeypatch.setenv("HUGEGRAPH_TOKEN", "token")
    with pytest.raises(ValueError, match="HUGEGRAPH_TOKEN"):
        HugeGraphClient()


def test_graph_names_encoded_as_path_segments(monkeypatch):
    monkeypatch.setenv("HUGEGRAPH_GRAPHSPACE", "space /中")
    monkeypatch.setenv("HUGEGRAPH_GRAPH", "graph /中")
    client = HugeGraphClient()
    assert client.graph_path == (
        "/graphspaces/space%20%2F%E4%B8%AD/graphs/graph%20%2F%E4%B8%AD"
    )
    assert client.batch_size == 100


@pytest.mark.parametrize(
    "uri,expected",
    [
        ("HTTP://HOST:80", "http://host"),
        ("http://host/", "http://host"),
        ("HTTPS://HOST:443/", "https://host"),
        ("https://host", "https://host"),
        ("http://HOST:80/pr%6Fxy/%7e/", "http://host/proxy/~"),
        ("http://host/proxy/~/", "http://host/proxy/~"),
        ("http://[0:0:0:0:0:0:0:1]:80/", "http://[::1]"),
        ("http://[::1]", "http://[::1]"),
        ("http://host/a%2fb/", "http://host/a%2Fb"),
        ("http://host/a%2Fb", "http://host/a%2Fb"),
        ("http://host/a b/", "http://host/a%20b"),
        ("http://host/a%20b", "http://host/a%20b"),
        ("http://host/中/", "http://host/%E4%B8%AD"),
        ("http://host/%e4%b8%ad", "http://host/%E4%B8%AD"),
    ],
)
def test_equivalent_uri_spellings_share_canonical_coordination_identity(
    monkeypatch, uri, expected
):
    monkeypatch.setenv("HUGEGRAPH_URI", uri)
    assert HugeGraphClient().uri == expected


@pytest.mark.parametrize(
    "left,right",
    [
        ("http://host", "https://host"),
        ("http://host:8080", "http://host"),
        ("http://host/Proxy", "http://host/proxy"),
        ("http://host/a%2Fb", "http://host/a/b"),
        ("http://localhost", "http://127.0.0.1"),
        ("http://proxy/backend", "http://backend"),
    ],
)
def test_uri_normalization_keeps_distinct_endpoints_and_dns_aliases_separate(
    monkeypatch, left, right
):
    monkeypatch.setenv("HUGEGRAPH_URI", left)
    left_uri = HugeGraphClient().uri
    monkeypatch.setenv("HUGEGRAPH_URI", right)
    assert HugeGraphClient().uri != left_uri


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "version", ["1.5.0", "1.8.0", "2.0.0", "1.7.0-RC1", None, "oops"]
)
async def test_unsupported_version_closes_connection(server, version):
    server.version = version
    client = HugeGraphClient()
    with pytest.raises(HugeGraphClientError, match="version"):
        await client.initialize()
    with pytest.raises(HugeGraphClientError, match="initializ"):
        await client.request("GET", "/versions")
    assert [r[1] for r in server.requests] == ["/versions"]
    await client.close()


@pytest.mark.asyncio
async def test_initialize_existing_schema_is_idempotent_and_close_reopens(server):
    client = HugeGraphClient()
    try:
        await client.initialize()
        request_count = len(server.requests)
        await client.initialize()
        assert len(server.requests) == request_count
        assert all(r[0] == "GET" for r in server.requests)
        await client.close()
        await client.close()
        await client.initialize()
        assert len(server.requests) > request_count
    finally:
        await client.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("authentication", ["basic", "bearer"])
async def test_auth_sent_to_server(server, monkeypatch, authentication):
    if authentication == "basic":
        monkeypatch.setenv("HUGEGRAPH_USERNAME", "user")
        monkeypatch.setenv("HUGEGRAPH_PASSWORD", "secret")
        expected = "Basic dXNlcjpzZWNyZXQ="
    else:
        monkeypatch.setenv("HUGEGRAPH_TOKEN", "secret-token")
        expected = "Bearer secret-token"
    client = HugeGraphClient()
    try:
        await client.initialize()
        assert server.requests[0][3]["Authorization"] == expected
    finally:
        await client.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("proxy_path", ["/proxy/", "/%70roxy/"])
async def test_reverse_proxy_path_preserved(server, monkeypatch, proxy_path):
    original = os.environ["HUGEGRAPH_URI"]
    monkeypatch.setenv("HUGEGRAPH_URI", original + proxy_path)
    server.responses[("GET", "/proxy/versions")].append(
        web.json_response({"versions": {"core": "1.7.0"}})
    )
    client = HugeGraphClient()
    try:
        await client.initialize()
        assert client.uri == original + "/proxy"
        assert all(r[1].startswith("/proxy/") for r in server.requests)
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_gremlin_gzip_aliases_and_safe_binding(client, server):
    response = {"status": {"code": 200}, "result": {"data": [{"name": "中'文"}]}}
    server.responses[("POST", "/gremlin")].append(
        web.Response(
            body=gzip.compress(json.dumps(response).encode()),
            headers={"Content-Encoding": "gzip", "Content-Type": "application/json"},
        )
    )
    data = await client.gremlin("g.V().has(name)", {"name": "中'文; g.V().drop()"})
    assert data == [{"name": "中'文"}]
    assert server.requests[-1][2] == {
        "gremlin": "g.V().has(name)",
        "bindings": {"name": "中'文; g.V().drop()"},
        "language": "gremlin-groovy",
        "aliases": {"graph": "DEFAULT-hugegraph", "g": "__g_DEFAULT-hugegraph"},
    }


@pytest.mark.asyncio
async def test_nondefault_graphspace_applies_to_rest_and_gremlin(server, monkeypatch):
    monkeypatch.setenv("HUGEGRAPH_GRAPHSPACE", "tenant")
    monkeypatch.setenv("HUGEGRAPH_GRAPH", "knowledge")
    client = HugeGraphClient()
    server.responses[("POST", "/gremlin")].append(
        web.json_response({"status": {"code": 200}, "result": {"data": []}})
    )
    try:
        await client.initialize()
        assert all(
            "/graphspaces/tenant/graphs/knowledge/schema/" in request[1]
            for request in server.requests[1:]
        )
        assert await client.gremlin("g.V().limit(0)") == []
        assert server.requests[-1][2]["aliases"] == {
            "graph": "tenant-knowledge",
            "g": "__g_tenant-knowledge",
        }
    finally:
        await client.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [400, 401, 403, 404, 429, 500, 503])
async def test_http_errors_not_reported_as_absence(client, server, status):
    server.responses[("GET", "/error")].append(
        web.json_response({"message": "upstream detail"}, status=status)
    )
    if status == 404:
        assert await client.request("GET", "/error", allow_not_found=True) is None
    else:
        with pytest.raises(HugeGraphClientError) as exc:
            await client.request("GET", "/error", allow_not_found=True)
        assert exc.value.status == status


@pytest.mark.asyncio
async def test_not_found_requires_explicit_opt_in(client, server):
    with pytest.raises(HugeGraphClientError) as exc:
        await client.request("GET", "/missing")
    assert exc.value.status == 404


@pytest.mark.asyncio
@pytest.mark.parametrize("body", [b"not-json", b"null", b'"text"', b"4", b""])
async def test_malformed_success_response_is_loud(client, server, body):
    server.responses[("GET", "/bad")].append(web.Response(body=body))
    with pytest.raises(HugeGraphClientError, match="JSON|response"):
        await client.request("GET", "/bad")


@pytest.mark.asyncio
async def test_no_content_is_success_not_absence(client, server):
    server.responses[("DELETE", "/vertex")].append(web.Response(status=204))
    assert await client.request("DELETE", "/vertex") == {}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "body",
    [
        {},
        {"status": {"code": 200}},
        {"status": {"code": 200}, "result": {"data": None}},
        {"status": {"code": 200}, "result": {"data": {}}},
        {"status": {"code": "200"}, "result": {"data": []}},
        {"status": {"code": 206}, "result": {"data": ["partial"]}},
        {"status": {"code": 500, "message": "failed"}, "result": {"data": []}},
    ],
)
async def test_gremlin_validates_inner_status_and_complete_list(client, server, body):
    server.responses[("POST", "/gremlin")].append(web.json_response(body))
    with pytest.raises(HugeGraphClientError, match="Gremlin"):
        await client.gremlin("g.V()", read_only=False)


@pytest.mark.asyncio
async def test_read_retries_bounded_and_write_never_retried(client, server):
    server.responses[("GET", "/retry")].extend(
        [
            web.Response(status=503),
            web.Response(status=503),
            web.json_response({"ok": 1}),
        ]
    )
    assert await client.request("GET", "/retry", retry=True) == {"ok": 1}
    server.responses[("GET", "/retry")].extend(
        [web.Response(status=503) for _ in range(4)]
    )
    with pytest.raises(HugeGraphClientError) as exc:
        await client.request("GET", "/retry", retry=True)
    assert exc.value.status == 503
    assert len(server.responses[("GET", "/retry")]) == 1
    server.responses[("POST", "/mutation")].extend(
        [web.Response(status=503), web.json_response({"ok": 1})]
    )
    with pytest.raises(HugeGraphClientError):
        await client.request("POST", "/mutation", retry=True)
    assert len(server.responses[("POST", "/mutation")]) == 1


@pytest.mark.asyncio
async def test_readonly_gremlin_can_retry_inner_transient_failure(client, server):
    server.responses[("POST", "/gremlin")].extend(
        [
            web.json_response({"status": {"code": 500}, "result": {"data": []}}),
            web.json_response({"status": {"code": 200}, "result": {"data": [1]}}),
        ]
    )
    assert await client.gremlin("g.V().count()") == [1]


@pytest.mark.asyncio
async def test_readonly_gremlin_can_retry_http_transient_failure(client, server):
    server.responses[("POST", "/gremlin")].extend(
        [
            web.Response(status=503),
            web.json_response({"status": {"code": 200}, "result": {"data": [2]}}),
        ]
    )
    assert await client.gremlin("g.V().count()") == [2]


@pytest.mark.asyncio
async def test_auth_failure_not_retried_even_for_read(client, server):
    server.responses[("GET", "/unauthorized")].extend(
        [web.Response(status=401), web.json_response({"ok": True})]
    )
    with pytest.raises(HugeGraphClientError):
        await client.request("GET", "/unauthorized", retry=True)
    assert len(server.responses[("GET", "/unauthorized")]) == 1


@pytest.mark.asyncio
async def test_cancelled_initialize_releases_connections(server):
    entered = asyncio.Event()

    async def slow(request):
        entered.set()
        await asyncio.sleep(0.1)
        return web.json_response({"versions": {"core": "1.7.0"}})

    server.responses[("GET", "/versions")].append(slow)
    client = HugeGraphClient()
    task = asyncio.create_task(client.initialize())
    await entered.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    with pytest.raises(HugeGraphClientError, match="initializ"):
        await client.request("GET", "/versions")
    await client.close()


@pytest.mark.asyncio
async def test_concurrent_initializations_share_one_session(server):
    client = HugeGraphClient()
    try:
        await asyncio.gather(client.initialize(), client.initialize())
        assert sum(r[1] == "/versions" for r in server.requests) == 1
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_timeout_is_loud_and_write_not_retried(server, monkeypatch):
    monkeypatch.setenv("HUGEGRAPH_TIMEOUT", "0.03")
    client = HugeGraphClient()

    async def slow(request):
        await asyncio.sleep(0.1)
        return web.json_response({"ok": True})

    server.responses[("POST", "/slow")].append(slow)
    try:
        await client.initialize()
        with pytest.raises(HugeGraphClientError, match="transport|timeout"):
            await client.request("POST", "/slow")
        assert sum(r[1] == "/slow" for r in server.requests) == 1
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_error_never_echoes_credentials_or_bindings(server, monkeypatch):
    monkeypatch.setenv("HUGEGRAPH_TOKEN", "secret-token")
    client = HugeGraphClient()
    try:
        await client.initialize()
        server.responses[("POST", "/gremlin")].append(
            web.json_response(
                {"status": {"code": 500, "message": "secret-token private-document"}}
            )
        )
        with pytest.raises(HugeGraphClientError) as exc:
            await client.gremlin(
                "g.V(value)", {"value": "private-document"}, read_only=False
            )
        assert "secret-token" not in str(exc.value)
        assert "private-document" not in str(exc.value)
        assert exc.value.code == 500
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_redirects_not_followed(client, server):
    server.responses[("GET", "/redirect")].append(
        web.Response(status=302, headers={"Location": "/versions"})
    )
    before = len(server.requests)
    with pytest.raises(HugeGraphClientError) as exc:
        await client.request("GET", "/redirect")
    assert exc.value.status == 302
    assert len(server.requests) == before + 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "path", ["https://other/", "//other/", "relative", "/../outside"]
)
async def test_request_cannot_escape_configured_endpoint(client, path):
    with pytest.raises(ValueError, match="path"):
        await client.request("GET", path)


@pytest.mark.asyncio
async def test_verify_only_missing_schema_never_writes(server, monkeypatch):
    monkeypatch.setenv("HUGEGRAPH_AUTO_CREATE_SCHEMA", "false")
    server.schema["propertykeys"] = []
    client = HugeGraphClient()
    with pytest.raises(HugeGraphClientError, match="missing|Missing"):
        await client.initialize()
    assert all(r[0] == "GET" for r in server.requests)


@pytest.mark.asyncio
async def test_create_only_own_schema_waits_for_index_tasks(server):
    server.schema = {key: [] for key in SCHEMA}
    server.task_status = deque(["running", "success"])
    client = HugeGraphClient()
    try:
        await client.initialize()
        creates = [(r[1], r[2]) for r in server.requests if r[0] == "POST"]
        assert len(creates) == 7
        assert all(body["name"].startswith("lightrag_") for _, body in creates)
        assert sum("/tasks/123" in r[1] for r in server.requests) == 3
        for group, items in SCHEMA.items():
            actual = {item["name"]: item for item in server.schema[group]}
            for expected in items:
                assert all(
                    actual[expected["name"]].get(k, v) == v for k, v in expected.items()
                )
    finally:
        await client.close()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "group,field,value",
    [
        ("propertykeys", "data_type", "INT"),
        ("propertykeys", "cardinality", "LIST"),
        ("propertykeys", "aggregate_type", "SUM"),
        ("propertykeys", "write_type", "OLAP_COMMON"),
        ("vertexlabels", "id_strategy", "PRIMARY_KEY"),
        ("vertexlabels", "properties", ["lightrag_scope", "lightrag_name"]),
        ("vertexlabels", "nullable_keys", ["lightrag_scope"]),
        ("vertexlabels", "enable_label_index", False),
        ("vertexlabels", "ttl", 1000),
        ("edgelabels", "frequency", "MULTIPLE"),
        ("edgelabels", "source_label", "business"),
        ("edgelabels", "target_label", "business"),
        ("edgelabels", "sort_keys", ["lightrag_scope"]),
        ("edgelabels", "ttl", 1000),
        ("edgelabels", "edgelabel_type", "PARENT"),
        ("edgelabels", "links", [{"business": "business"}]),
        ("vertexlabels", "properties", [{"bad": "shape"}]),
        ("indexlabels", "fields", ["lightrag_name"]),
        ("indexlabels", "index_type", "SEARCH"),
        ("indexlabels", "base_type", "EDGE_LABEL"),
        ("indexlabels", "status", "INVALID"),
    ],
)
async def test_schema_drift_refused_without_mutations(server, group, field, value):
    server.schema[group][0][field] = value
    client = HugeGraphClient()
    with pytest.raises(HugeGraphClientError, match="schema|Schema"):
        await client.initialize()
    assert all(r[0] == "GET" for r in server.requests)


@pytest.mark.asyncio
async def test_concurrent_create_rereads_and_validates_winner(server):
    path = GRAPH_PATH + "/schema/propertykeys/lightrag_scope"
    server.responses[("GET", path)].append(web.Response(status=404))
    server.responses[("POST", GRAPH_PATH + "/schema/propertykeys")].append(
        web.json_response(
            {"exception": "class org.apache.hugegraph.exception.ExistedException"},
            status=400,
        )
    )
    client = HugeGraphClient()
    try:
        await client.initialize()
        assert sum(r[1] == path for r in server.requests) == 2
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_concurrent_index_creation_waits_for_schema_created(server):
    path = GRAPH_PATH + "/schema/indexlabels/lightrag_entity_scope_name_v1"
    creating = {**SCHEMA["indexlabels"][0], "status": "CREATING"}
    server.responses[("GET", path)].extend(
        [web.Response(status=404), web.json_response(creating)]
    )
    server.responses[("POST", GRAPH_PATH + "/schema/indexlabels")].append(
        web.Response(status=409)
    )
    client = HugeGraphClient()
    try:
        await client.initialize()
        assert sum(r[1] == path for r in server.requests) == 3
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_concurrent_creator_incompatible_schema_is_rejected(server):
    path = GRAPH_PATH + "/schema/propertykeys/lightrag_scope"
    server.responses[("GET", path)].append(web.Response(status=404))
    server.responses[("POST", GRAPH_PATH + "/schema/propertykeys")].append(
        web.Response(status=409)
    )
    server.schema["propertykeys"][0]["data_type"] = "INT"
    client = HugeGraphClient()
    with pytest.raises(HugeGraphClientError, match="incompatible"):
        await client.initialize()


@pytest.mark.asyncio
async def test_schema_drift_preflight_precedes_creating_missing_definitions(server):
    server.schema["propertykeys"] = []
    server.schema["vertexlabels"][0]["id_strategy"] = "PRIMARY_KEY"
    client = HugeGraphClient()
    with pytest.raises(HugeGraphClientError, match="incompatible"):
        await client.initialize()
    assert all(r[0] == "GET" for r in server.requests)


@pytest.mark.asyncio
async def test_create_failure_not_hidden_by_reread(server):
    path = GRAPH_PATH + "/schema/propertykeys/lightrag_scope"
    server.responses[("GET", path)].append(web.Response(status=404))
    server.responses[("POST", GRAPH_PATH + "/schema/propertykeys")].append(
        web.Response(status=503)
    )
    client = HugeGraphClient()
    with pytest.raises(HugeGraphClientError) as exc:
        await client.initialize()
    assert exc.value.status == 503
    assert sum(r[1] == path for r in server.requests) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("status", ["failed", "cancelled", "unknown", None, {}])
async def test_index_task_failure_prevents_success(server, status):
    server.schema["indexlabels"] = []
    server.task_status = deque([status])
    client = HugeGraphClient()
    with pytest.raises(HugeGraphClientError, match="task|Task"):
        await client.initialize()


@pytest.mark.asyncio
async def test_index_task_wait_is_bounded(server, monkeypatch):
    monkeypatch.setenv("HUGEGRAPH_TIMEOUT", "0.03")
    server.schema["indexlabels"] = []
    server.task_status = deque(["running"])
    client = HugeGraphClient()
    with pytest.raises(HugeGraphClientError, match="task|Task"):
        await client.initialize()


@pytest.mark.asyncio
async def test_creating_schema_wait_is_bounded(server, monkeypatch):
    monkeypatch.setenv("HUGEGRAPH_TIMEOUT", "0.03")
    server.schema["indexlabels"][0]["status"] = "CREATING"
    client = HugeGraphClient()
    with pytest.raises(HugeGraphClientError, match="schema.*timeout|schema.*TIMEOUT"):
        await client.initialize()


@pytest.mark.asyncio
async def test_created_schema_can_reopen_with_secondary_prefix_elimination(server):
    server.schema = {key: [] for key in SCHEMA}
    client = HugeGraphClient()
    try:
        await client.initialize()
        await client.close()
        writes = sum(r[0] == "POST" for r in server.requests)
        await client.initialize()
        assert sum(r[0] == "POST" for r in server.requests) == writes
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_schema_provisioning_never_implicitly_removes_existing_prefix_index(
    server,
):
    existing_index = {
        "name": "operator_owned_scope_index",
        "base_type": "VERTEX_LABEL",
        "base_value": "lightrag_entity_v1",
        "index_type": "SECONDARY",
        "fields": ["lightrag_scope"],
        "status": "CREATED",
    }
    server.schema["indexlabels"] = [existing_index]
    client = HugeGraphClient()
    with pytest.raises(HugeGraphClientError, match="index.*overlap|overlap.*index"):
        await client.initialize()
    assert all(r[0] == "GET" for r in server.requests)
    assert server.schema["indexlabels"] == [existing_index]


@pytest.mark.asyncio
@pytest.mark.parametrize("method", ["POST", "PUT", "DELETE"])
async def test_disconnect_after_committing_mutation_never_replayed(
    client, server, method
):
    committed = 0

    async def commit_then_disconnect(request):
        nonlocal committed
        committed += 1
        request.transport.abort()
        return web.Response()

    server.responses[(method, "/uncertain")].extend([commit_then_disconnect] * 4)
    with pytest.raises(HugeGraphClientError):
        await client.request(method, "/uncertain", json={"version": 1}, retry=True)
    assert committed == 1


@pytest.mark.asyncio
async def test_transport_read_attempts_obey_explicit_retry_budget(server, monkeypatch):
    monkeypatch.setenv("HUGEGRAPH_RETRIES", "1")
    client = HugeGraphClient()
    attempts = 0

    async def disconnect(request):
        nonlocal attempts
        attempts += 1
        request.transport.abort()
        return web.Response()

    server.responses[("GET", "/disconnected")].extend([disconnect] * 6)
    try:
        await client.initialize()
        with pytest.raises(HugeGraphClientError):
            await client.request("GET", "/disconnected", retry=True)
        assert attempts == 2
    finally:
        await client.close()
