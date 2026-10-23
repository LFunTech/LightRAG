import copy
import importlib.util
import json

import httpx
import pytest

from .test_plan import build


class GraphServer:
    def __init__(self):
        self.definitions = {}
        self.posts = []
        self.failure = None

    def __call__(self, request):
        path = request.url.path
        if path == "/versions":
            return httpx.Response(200, json={"versions": {"core": "1.7.0"}})
        if path == "/gremlin":
            data = json.loads(request.content)
            assert "label" not in data["bindings"]
            return httpx.Response(
                200, json={"status": {"code": 200}, "result": {"data": [0]}}
            )
        if request.method == "POST":
            data = json.loads(request.content)
            self.posts.append(data["name"])
            if self.failure == "absent":
                raise httpx.ReadTimeout("synthetic")
            self.definitions[data["name"]] = dict(data, status="CREATED")
            if self.failure == "present":
                self.failure = None
                raise httpx.ReadTimeout("synthetic")
            return httpx.Response(
                200, json={"task_id": 0} if "/indexlabels" in path else data
            )
        name = path.rsplit("/", 1)[-1]
        if name == "indexlabels":
            return httpx.Response(
                200,
                json={
                    "indexlabels": [
                        d for d in self.definitions.values() if "index_type" in d
                    ]
                },
            )
        value = self.definitions.get(name)
        return httpx.Response(200, json=value) if value else httpx.Response(404)


def graph(server):
    assert importlib.util.find_spec("lightrag.provisioning.graph"), (
        "Versioned graph migration is missing"
    )
    from lightrag.provisioning.graph import GraphProvisioner

    client = httpx.AsyncClient(transport=httpx.MockTransport(server))
    return GraphProvisioner(
        build(),
        "http://graph.invalid",
        "prod",
        "prod",
        "operator",
        "synthetic",
        wait_seconds=0.2,
        client=client,
    )


@pytest.mark.integration
async def test_graph_migration_is_journalled_and_repeat_does_not_post(migration_db):
    from lightrag.provisioning.postgres import migrate, verify

    server = GraphServer()
    g = graph(server)
    await migrate(migration_db, build(), "migration_test_app", confirm_empty=True)
    async with g:
        await g.apply(migration_db)
        assert len(server.posts) == 7
        await g.apply(migration_db)
        assert len(server.posts) == 7
        await verify(migration_db, build())
        assert (
            await migration_db.fetchval(
                "SELECT count(*) FROM lightrag_storage.graph_steps WHERE state='CONFIRMED'"
            )
            == 7
        )


@pytest.mark.integration
@pytest.mark.parametrize("failure", ["absent", "present"])
async def test_unknown_graph_post_is_never_replayed_without_confirmed_present_definition(
    migration_db, failure
):
    from lightrag.provisioning.postgres import migrate

    server = GraphServer()
    server.failure = failure
    g = graph(server)
    await migrate(migration_db, build(), "migration_test_app", confirm_empty=True)
    async with g:
        with pytest.raises(httpx.ReadTimeout):
            await g.apply(migration_db)
        assert (
            await migration_db.fetchval(
                "SELECT state FROM lightrag_storage.graph_steps WHERE name='lightrag_scope'"
            )
            == "EXECUTING"
        )
        server.failure = None
        if failure == "absent":
            with pytest.raises(ValueError):
                await g.apply(migration_db)
            assert server.posts == ["lightrag_scope"]
        else:
            await g.apply(migration_db)
            assert server.posts.count("lightrag_scope") == 1
            assert (
                await migration_db.fetchval(
                    "SELECT graph_complete FROM lightrag_storage.migrations"
                )
                is True
            )


async def test_graph_preflight_rejects_drift_and_overlapping_index_before_mutating():
    server = GraphServer()
    plan = build()
    server.definitions = {v["name"]: copy.deepcopy(v) for _, v in plan.graph}
    server.definitions["lightrag_scope"]["data_type"] = "INT"
    async with graph(server) as g:
        with pytest.raises(ValueError):
            await g.preflight()
    assert not server.posts
    server.definitions = {}
    server.definitions["legacy_scope_index"] = {
        "name": "legacy_scope_index",
        "base_type": "VERTEX_LABEL",
        "base_value": "lightrag_entity_v1",
        "index_type": "SECONDARY",
        "fields": ["lightrag_scope"],
    }
    async with graph(server) as g:
        with pytest.raises(ValueError):
            await g.preflight()
    assert not server.posts


@pytest.mark.integration
async def test_unknown_graph_intent_is_durable_to_another_connection(migration_db):
    import os
    import asyncpg
    from urllib.parse import urlsplit, urlunsplit
    from lightrag.provisioning.postgres import migrate

    server = GraphServer()
    server.failure = "absent"
    await migrate(migration_db, build(), "migration_test_app", confirm_empty=True)
    async with graph(server) as g:
        with pytest.raises(httpx.ReadTimeout):
            await g.apply(migration_db)
    target = urlsplit(os.environ["LIGHTRAG_PROVISIONING_TEST_DSN"])._replace(
        path="/" + await migration_db.fetchval("SELECT current_database()")
    )
    other = await asyncpg.connect(urlunsplit(target))
    try:
        assert (
            await other.fetchval(
                "SELECT state FROM lightrag_storage.graph_steps WHERE name='lightrag_scope'"
            )
            == "EXECUTING"
        )
        assert (
            await other.fetchval(
                "SELECT graph_complete FROM lightrag_storage.migrations"
            )
            is False
        )
    finally:
        await other.close()


@pytest.mark.integration
@pytest.mark.parametrize("status", ["failed", "running"])
async def test_index_task_failure_or_deadline_preserves_intent_without_reposting(
    migration_db, status
):
    from lightrag.provisioning.postgres import migrate

    class Tasks(GraphServer):
        def __call__(self, request):
            if "/tasks/" in request.url.path:
                return httpx.Response(200, json={"task_status": status})
            response = super().__call__(request)
            if request.method == "POST" and "/indexlabels" in request.url.path:
                return httpx.Response(200, json={"task_id": 42})
            return response

    server = Tasks()
    await migrate(migration_db, build(), "migration_test_app", confirm_empty=True)
    async with graph(server) as g:
        error = ValueError if status == "failed" else TimeoutError
        with pytest.raises(error):
            await g.apply(migration_db)
        before = list(server.posts)
        with pytest.raises(error):
            await g.apply(migration_db)
        assert server.posts == before
        assert (
            await migration_db.fetchval(
                "SELECT graph_complete FROM lightrag_storage.migrations"
            )
            is False
        )
        assert (
            await migration_db.fetchval(
                "SELECT count(*) FROM lightrag_storage.graph_steps WHERE state='EXECUTING'"
            )
            == 1
        )


@pytest.mark.integration
async def test_caller_transaction_is_refused_before_graph_intent_or_post(migration_db):
    from lightrag.provisioning.postgres import migrate

    await migrate(migration_db, build(), "migration_test_app", confirm_empty=True)
    server = GraphServer()
    async with graph(server) as g:
        async with migration_db.transaction():
            with pytest.raises(ValueError, match="caller transaction"):
                await g.apply(migration_db)
    assert not server.posts
    assert (
        await migration_db.fetchval("SELECT count(*) FROM lightrag_storage.graph_steps")
        == 0
    )


async def test_empty_scope_uses_previously_validated_nonreserved_bindings():
    requests = []

    def handler(request):
        requests.append(json.loads(request.content))
        return httpx.Response(
            200, json={"status": {"code": 200}, "result": {"data": [0]}}
        )

    async with graph(handler) as g:
        await g.verify_empty_scope("prod")
    assert len(requests) == 2
    for body, traversal, binding, label in zip(
        requests,
        ["V", "E"],
        ["vlabel", "elabel"],
        ["lightrag_entity_v1", "lightrag_relation_v1"],
    ):
        assert (
            body["gremlin"]
            == f"g.{traversal}().hasLabel({binding}).has(scope_key, scope).limit(1).count().next()"
        )
        assert body["bindings"] == {
            binding: label,
            "scope_key": "lightrag_scope",
            "scope": "prod",
        }
        assert body["aliases"] == {"graph": "prod-prod", "g": "__g_prod-prod"}


@pytest.mark.parametrize(
    "field,value",
    [
        ("graphspace", ".."),
        ("graph", "."),
        ("graphspace", " "),
        ("graph", "bad\x7fgraph"),
    ],
)
def test_graph_target_validation_matches_runtime_before_requests(field, value):
    from lightrag.provisioning.graph import GraphProvisioner

    kwargs = {
        "endpoint": "http://graph.invalid",
        "graphspace": "prod",
        "graph": "prod",
        "username": "operator",
        "password": "fixture",
        "wait_seconds": 1,
    }
    kwargs[field] = value
    with pytest.raises(ValueError):
        GraphProvisioner(build(), **kwargs)
