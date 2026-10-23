"""Versioned graph DDL with durable per-request intent and no automatic replay."""

import asyncio
import math
import re
from urllib.parse import quote, unquote, urlsplit

import httpx

from .plan import digest, validate_graph_name
from .postgres import migration_lock, verify


class GraphProvisioner:
    def __init__(
        self,
        plan,
        endpoint,
        graphspace,
        graph,
        username,
        password,
        *,
        wait_seconds,
        client=None,
    ):
        uri = urlsplit(endpoint)
        if (
            uri.scheme not in {"http", "https"}
            or not uri.hostname
            or uri.username
            or uri.password
            or uri.query
            or uri.fragment
            or any(x in {".", ".."} for x in unquote(uri.path).split("/"))
        ):
            raise ValueError("Explicit graph endpoint without credentials required")
        if not math.isfinite(wait_seconds) or wait_seconds <= 0:
            raise ValueError("Positive finite graph wait budget required")
        validate_graph_name(graphspace)
        validate_graph_name(graph)
        if any(
            not isinstance(v, str) or not v or any(ord(c) < 32 for c in v)
            for v in (graphspace, graph, username, password)
        ):
            raise ValueError("Explicit graph target and operator credentials required")
        self.plan = plan
        self.endpoint = endpoint.rstrip("/")
        self.auth = (username, password)
        self.path = (
            "/graphspaces/"
            + quote(graphspace, safe="")
            + "/graphs/"
            + quote(graph, safe="")
        )
        self.wait_seconds = wait_seconds
        self.client = client or httpx.AsyncClient(
            timeout=min(wait_seconds, 15), trust_env=False, follow_redirects=False
        )

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        await self.client.aclose()

    async def request(self, method, path, *, payload=None, missing=False):
        async with self.client.stream(
            method,
            self.endpoint + path,
            json=payload,
            auth=self.auth,
            follow_redirects=False,
        ) as response:
            if missing and response.status_code == 404:
                return None
            response.raise_for_status()
            data = bytearray()
            async for block in response.aiter_bytes():
                data.extend(block)
                if len(data) > 524288:
                    raise ValueError("Graph schema metadata exceeds bound")
            import json

            return json.loads(data)

    @staticmethod
    def validate(kind, expected, actual):
        from lightrag.kg.hugegraph_client import HugeGraphClient, HugeGraphClientError

        try:
            HugeGraphClient._validate_schema(kind, expected, actual)
        except HugeGraphClientError:
            raise ValueError("Graph schema is incompatible") from None

    async def definition(self, kind, expected):
        actual = await self.request(
            "GET",
            self.path + "/schema/" + kind + "/" + quote(expected["name"], safe=""),
            missing=True,
        )
        if actual is not None:
            self.validate(kind, expected, actual)
        return actual

    async def preflight(self):
        version = await self.request("GET", "/versions")
        if not re.fullmatch(r"1\.7\.\d+", version.get("versions", {}).get("core", "")):
            raise ValueError("Graph version must be stable 1.7.x")
        actual = {}
        for kind, expected in self.plan.graph:
            actual[expected["name"]] = await self.definition(kind, expected)
        missing_indexes = [
            v
            for kind, v in self.plan.graph
            if kind == "indexlabels" and actual[v["name"]] is None
        ]
        if missing_indexes:
            inventory = await self.request("GET", self.path + "/schema/indexlabels")
            indexes = (
                inventory.get("indexlabels") if isinstance(inventory, dict) else None
            )
            if not isinstance(indexes, list) or any(
                not isinstance(v, dict) for v in indexes
            ):
                raise ValueError("Malformed graph index inventory")
            for wanted in missing_indexes:
                for index in indexes:
                    if (
                        index.get("name") == wanted["name"]
                        or index.get("base_type") != wanted["base_type"]
                        or index.get("base_value") != wanted["base_value"]
                        or index.get("index_type") not in {"SECONDARY", "SHARD"}
                    ):
                        continue
                    fields = index.get("fields")
                    if not isinstance(fields, list) or not all(
                        isinstance(v, str) for v in fields
                    ):
                        raise ValueError("Malformed graph index fields")
                    if (
                        fields[: len(wanted["fields"])] == wanted["fields"]
                        or wanted["fields"][: len(fields)] == fields
                    ):
                        raise ValueError(
                            "Graph index creation would replace an overlapping index"
                        )
        return actual

    async def ready(self, kind, expected, task_id=None):
        async def poll():
            if task_id:
                while True:
                    task = await self.request(
                        "GET", self.path + "/tasks/" + str(task_id)
                    )
                    status = task.get("task_status")
                    if status == "success":
                        break
                    if status not in {
                        "new",
                        "scheduling",
                        "scheduled",
                        "queued",
                        "running",
                        "restoring",
                    }:
                        raise ValueError("Graph schema task failed")
                    await asyncio.sleep(0.1)
            while True:
                actual = await self.definition(kind, expected)
                if actual is not None and actual.get("status", "CREATED") == "CREATED":
                    return
                await asyncio.sleep(0.1)

        await asyncio.wait_for(poll(), self.wait_seconds)

    async def verify(self):
        actual = await self.preflight()
        for kind, expected in self.plan.graph:
            if (
                actual[expected["name"]] is None
                or actual[expected["name"]].get("status", "CREATED") != "CREATED"
            ):
                raise ValueError("Graph schema is not provisioned and ready")

    async def apply(self, connection):
        if connection.is_in_transaction():
            raise ValueError(
                "Graph intent must commit before remote DDL; no caller transaction allowed"
            )
        async with migration_lock(connection):
            if self.plan.credential_exception is not None:
                await self.plan.credential_exception.verify_initialization_state(
                    connection
                )
            await verify(connection, self.plan, require_graph=False)
            if await connection.fetchval(
                "SELECT graph_complete FROM lightrag_storage.migrations"
            ):
                await verify(connection, self.plan)
                await self.verify()
                return
            actual = await self.preflight()
            steps = {
                r["name"]: r
                for r in await connection.fetch(
                    "SELECT name,payload_hash,state,task_id FROM lightrag_storage.graph_steps"
                )
            }
            expected_names = {v["name"] for _, v in self.plan.graph}
            if set(steps) - expected_names:
                raise ValueError("Unexpected graph migration journal entries")
            for kind, expected in self.plan.graph:
                name = expected["name"]
                payload_hash = digest([kind, expected])
                step = steps.get(name)
                if step and step["payload_hash"] != payload_hash:
                    raise ValueError("Graph migration intent checksum differs")
                task_id = step["task_id"] if step else None
                if actual[name] is None:
                    if step:
                        raise ValueError(
                            "Uncertain or disappeared graph mutation requires operator audit; no replay"
                        )
                    await connection.execute(
                        "INSERT INTO lightrag_storage.graph_steps(name,payload_hash,state) VALUES($1,$2,'EXECUTING')",
                        name,
                        payload_hash,
                    )
                    created = await self.request(
                        "POST", self.path + "/schema/" + kind, payload=expected
                    )
                    if kind == "indexlabels":
                        task_id = (
                            created.get("task_id")
                            if isinstance(created, dict)
                            else None
                        )
                        if type(task_id) is not int or task_id < 0:
                            raise ValueError("Graph index task response is unknown")
                        await connection.execute(
                            "UPDATE lightrag_storage.graph_steps SET task_id=$2 WHERE name=$1",
                            name,
                            task_id,
                        )
                elif step is None:
                    await connection.execute(
                        "INSERT INTO lightrag_storage.graph_steps(name,payload_hash,state) VALUES($1,$2,'EXECUTING')",
                        name,
                        payload_hash,
                    )
                await self.ready(kind, expected, task_id)
                await connection.execute(
                    "UPDATE lightrag_storage.graph_steps SET state='CONFIRMED' WHERE name=$1 AND payload_hash=$2",
                    name,
                    payload_hash,
                )
            await self.verify()
            await connection.execute(
                "UPDATE lightrag_storage.migrations SET graph_complete=true WHERE version=1 AND profile_hash=$1",
                self.plan.profile_hash,
            )
            await verify(connection, self.plan)

    async def verify_empty_scope(self, workspace):
        binding = self.path.split("/graphspaces/", 1)[1].split("/graphs/")
        from urllib.parse import unquote

        alias = unquote(binding[0]) + "-" + unquote(binding[1])
        for traversal, label, variable in [
            ("V", "lightrag_entity_v1", "vlabel"),
            ("E", "lightrag_relation_v1", "elabel"),
        ]:
            body = await self.request(
                "POST",
                "/gremlin",
                payload={
                    "gremlin": f"g.{traversal}().hasLabel({variable}).has(scope_key, scope).limit(1).count().next()",
                    "language": "gremlin-groovy",
                    "aliases": {"graph": alias, "g": "__g_" + alias},
                    "bindings": {
                        variable: label,
                        "scope_key": "lightrag_scope",
                        "scope": workspace,
                    },
                },
            )
            if body.get("status", {}).get("code") != 200 or body.get("result", {}).get(
                "data"
            ) != [0]:
                raise ValueError(
                    "First registration requires a confirmed empty graph scope"
                )
