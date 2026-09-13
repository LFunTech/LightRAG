"""Asynchronous HugeGraph 1.7 HTTP protocol and additive, versioned schema setup."""

from __future__ import annotations

import asyncio
import json as json_module
import math
import os
import re
from typing import Any
from urllib.parse import quote, unquote, urlsplit

from lightrag.distributed.runtime import physical_write

import aiohttp
from yarl import URL


VERTEX_LABEL = "lightrag_entity_v1"
EDGE_LABEL = "lightrag_relation_v1"
SCOPE = "lightrag_scope"
NAME = "lightrag_name"
DATA = "lightrag_data"

_TRANSIENT_CODES = frozenset({408, 429, 500, 502, 503, 504})
_PENDING_TASK_STATES = frozenset(
    {"new", "scheduling", "scheduled", "queued", "running", "restoring"}
)


class HugeGraphClientError(RuntimeError):
    """Protocol failure without server messages, credentials or document contents."""

    def __init__(
        self,
        message: str,
        *,
        status: int | None = None,
        code: int | None = None,
        exception: str | None = None,
        transient: bool = False,
    ) -> None:
        super().__init__(message)
        self.status = status
        self.code = code
        self.exception = exception
        self.transient = transient


def _integer_setting(
    name: str, default: int, minimum: int, maximum: int | None = None
) -> int:
    try:
        raw = os.getenv(name, str(default))
        if re.fullmatch(r"[0-9]+", raw) is None:
            raise ValueError
        value = int(raw)
        if value < minimum or (maximum is not None and value > maximum):
            raise ValueError
        return value
    except ValueError:
        bounds = f"{minimum}..{maximum}" if maximum is not None else f">= {minimum}"
        raise ValueError(f"{name} must be an integer {bounds}") from None


def _has_control(value: str) -> bool:
    return any(ord(char) < 32 or ord(char) == 127 for char in value)


def _graph_name(name: str, default: str) -> str:
    value = os.getenv(name, default)
    if not value.strip() or value in {".", ".."} or _has_control(value):
        raise ValueError(
            f"{name} must be a non-empty graph name without control characters"
        )
    return value


def _schema_definitions() -> list[tuple[str, dict[str, Any]]]:
    """Return only the dedicated LightRAG schema, in dependency order."""
    definitions = [
        (
            "propertykeys",
            {
                "name": key,
                "data_type": "TEXT",
                "cardinality": "SINGLE",
                "aggregate_type": "NONE",
                "write_type": "OLTP",
                "properties": [],
            },
        )
        for key in (SCOPE, NAME, DATA)
    ]
    definitions.extend(
        [
            (
                "vertexlabels",
                {
                    "name": VERTEX_LABEL,
                    "id_strategy": "CUSTOMIZE_STRING",
                    "properties": [SCOPE, NAME, DATA],
                    "primary_keys": [],
                    "nullable_keys": [],
                    "enable_label_index": True,
                    "ttl": 0,
                },
            ),
            (
                "edgelabels",
                {
                    "name": EDGE_LABEL,
                    "source_label": VERTEX_LABEL,
                    "target_label": VERTEX_LABEL,
                    "frequency": "SINGLE",
                    "properties": [SCOPE, DATA],
                    "sort_keys": [],
                    "nullable_keys": [],
                    "enable_label_index": True,
                    "ttl": 0,
                },
            ),
        ]
    )
    # SECONDARY composite indexes cover their leftmost prefixes. HugeGraph
    # removes a prior [SCOPE] index when [SCOPE, NAME] is created and refuses to
    # recreate it afterwards, so the composite is the only vertex index needed.
    for name, base_type, base_value, fields in (
        ("lightrag_entity_scope_name_v1", "VERTEX_LABEL", VERTEX_LABEL, [SCOPE, NAME]),
        ("lightrag_relation_scope_v1", "EDGE_LABEL", EDGE_LABEL, [SCOPE]),
    ):
        definitions.append(
            (
                "indexlabels",
                {
                    "name": name,
                    "base_type": base_type,
                    "base_value": base_value,
                    "index_type": "SECONDARY",
                    "fields": fields,
                },
            )
        )
    return definitions


class HugeGraphClient:
    """Initialize before use and close after the last operation.

    ``request`` retries only explicitly opted-in GET/HEAD requests. For Gremlin,
    callers must mark every mutation ``read_only=False``; a failed write may
    already have committed and is never replayed automatically. Bind all input
    values instead of interpolating them into scripts. Only 1.7.x is supported.

    Schema setup only creates missing LightRAG definitions. It never alters or
    deletes existing definitions, and incompatibility requires operator action.
    """

    def __init__(self) -> None:
        uri = os.getenv("HUGEGRAPH_URI", "")
        try:
            parts = urlsplit(uri)
            port = parts.port
            if (
                parts.scheme not in {"http", "https"}
                or not parts.hostname
                or parts.username is not None
                or parts.password is not None
                or parts.query
                or parts.fragment
                or "?" in uri
                or "#" in uri
                or _has_control(uri)
                or any(char.isspace() for char in parts.netloc)
                or (port is not None and not 0 < port <= 65535)
                or any(
                    segment in {".", ".."} for segment in unquote(parts.path).split("/")
                )
            ):
                raise ValueError
            # Match aiohttp's wire URL canonicalization so equivalent endpoint
            # spellings share the storage mutation lock and uncertain-write fence.
            self.uri = str(URL(uri)).rstrip("/")
        except ValueError:
            raise ValueError(
                "HUGEGRAPH_URI must be an HTTP(S) endpoint without credentials, query or fragment"
            ) from None
        self.graph = _graph_name("HUGEGRAPH_GRAPH", "hugegraph")
        self.graphspace = _graph_name("HUGEGRAPH_GRAPHSPACE", "DEFAULT")
        self.graph_path = (
            f"/graphspaces/{quote(self.graphspace, safe='')}"
            f"/graphs/{quote(self.graph, safe='')}"
        )
        self.batch_size = _integer_setting("HUGEGRAPH_BATCH_SIZE", 100, 1)
        self.max_connections = _integer_setting("HUGEGRAPH_MAX_CONNECTIONS", 10, 1)
        self.retries = _integer_setting("HUGEGRAPH_RETRIES", 2, 0, 10)
        try:
            self.timeout = float(os.getenv("HUGEGRAPH_TIMEOUT", "30"))
            if not math.isfinite(self.timeout) or self.timeout <= 0:
                raise ValueError
        except ValueError:
            raise ValueError(
                "HUGEGRAPH_TIMEOUT must be a finite positive number"
            ) from None
        auto_create = os.getenv("HUGEGRAPH_AUTO_CREATE_SCHEMA", "true").lower()
        if auto_create not in {"true", "false"}:
            raise ValueError("HUGEGRAPH_AUTO_CREATE_SCHEMA must be true or false")
        self.auto_create_schema = auto_create == "true"

        username = os.getenv("HUGEGRAPH_USERNAME", "")
        password = os.getenv("HUGEGRAPH_PASSWORD", "")
        token = os.getenv("HUGEGRAPH_TOKEN", "")
        for name, value in (
            ("HUGEGRAPH_USERNAME", username),
            ("HUGEGRAPH_PASSWORD", password),
            ("HUGEGRAPH_TOKEN", token),
        ):
            if _has_control(value):
                raise ValueError(f"{name} must not contain control characters")
        if bool(username) != bool(password):
            raise ValueError(
                "HUGEGRAPH_USERNAME and HUGEGRAPH_PASSWORD must be set together"
            )
        if token and (username or password):
            raise ValueError(
                "HUGEGRAPH_TOKEN cannot be combined with username/password"
            )
        if ":" in username:
            raise ValueError("HUGEGRAPH_USERNAME must not contain a colon")
        self._auth = aiohttp.BasicAuth(username, password) if username else None
        self._headers = {"Accept": "application/json"}
        if token:
            self._headers["Authorization"] = f"Bearer {token}"
        self._session: aiohttp.ClientSession | None = None
        self._initialized = False
        self._initialize_lock = asyncio.Lock()

    async def initialize(self) -> None:
        """Verify the server and schema, closing the session on every failure."""
        async with self._initialize_lock:
            if self._initialized:
                return
            try:
                self._session = aiohttp.ClientSession(
                    connector=aiohttp.TCPConnector(limit=self.max_connections),
                    timeout=aiohttp.ClientTimeout(total=self.timeout),
                    headers=self._headers,
                    auth=self._auth,
                    auto_decompress=True,
                    trust_env=False,
                )
                # Newer aiohttp automatically replays PUT/DELETE after a
                # disconnect, even after the server may have committed. Its
                # internal switch is also used by aiohttp.test_utils; older
                # versions without the switch have no implicit replay. Keep
                # retries exclusively in our read-only policy below.
                if hasattr(self._session, "_retry_connection"):
                    self._session._retry_connection = False
                response = await self.request("GET", "/versions", retry=True)
                versions = (
                    response.get("versions") if isinstance(response, dict) else None
                )
                version = versions.get("core") if isinstance(versions, dict) else None
                if (
                    not isinstance(version, str)
                    or re.fullmatch(r"1\.7\.[0-9]+", version) is None
                ):
                    raise HugeGraphClientError(
                        "HugeGraph server version must be a stable 1.7.x release"
                    )
                await self._ensure_schema()
                self._initialized = True
            except BaseException:
                await self.close()
                raise

    async def close(self) -> None:
        """Release connections; repeated close and subsequent initialize are safe."""
        session, self._session = self._session, None
        self._initialized = False
        if session is not None:
            await session.close()

    def _request_url(self, path: str) -> str:
        if (
            not isinstance(path, str)
            or not path.startswith("/")
            or path.startswith("//")
            or _has_control(path)
            or "?" in path
            or "#" in path
            or any(segment in {".", ".."} for segment in unquote(path).split("/"))
        ):
            raise ValueError(
                "HugeGraph request path must stay within the configured endpoint"
            )
        return self.uri + path

    async def request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        json: Any = None,
        allow_not_found: bool = False,
        retry: bool = False,
    ) -> dict[str, Any] | list[Any] | None:
        """Return JSON, ``{}`` for 204, or ``None`` only for an opted-in 404."""
        method = method.upper()
        attempts = self.retries + 1 if retry and method in {"GET", "HEAD"} else 1
        for attempt in range(attempts):
            try:
                return await self._request_once(
                    method, path, params, json, allow_not_found
                )
            except HugeGraphClientError as error:
                if not error.transient or attempt + 1 == attempts:
                    raise
                await asyncio.sleep(min(0.1 * 2**attempt, 1.0))
        raise AssertionError("unreachable")

    async def _request_once(
        self,
        method: str,
        path: str,
        params: dict[str, Any] | None,
        payload: Any,
        allow_not_found: bool = False,
    ) -> dict[str, Any] | list[Any] | None:
        url = self._request_url(path)
        if self._session is None or self._session.closed:
            raise HugeGraphClientError("HugeGraph client is not initialized")
        try:
            async with self._session.request(
                method, url, params=params, json=payload, allow_redirects=False
            ) as response:
                if response.status == 404 and allow_not_found:
                    return None
                if not 200 <= response.status < 300:
                    exception = None
                    try:
                        error_body = await response.json(content_type=None)
                        candidate = (
                            error_body.get("exception")
                            if isinstance(error_body, dict)
                            else None
                        )
                        if isinstance(candidate, str) and candidate.endswith(
                            ".ExistedException"
                        ):
                            exception = "ExistedException"
                    except (ValueError, UnicodeDecodeError):
                        pass
                    hint = (
                        " Check authentication and graph permissions."
                        if response.status in {401, 403}
                        else ""
                    )
                    raise HugeGraphClientError(
                        f"HugeGraph HTTP {method} failed with status {response.status}.{hint}",
                        status=response.status,
                        exception=exception,
                        transient=response.status in _TRANSIENT_CODES,
                    )
                if response.status == 204:
                    return {}
                try:
                    body = await response.json(content_type=None)
                except (ValueError, UnicodeDecodeError):
                    raise HugeGraphClientError(
                        "HugeGraph returned an invalid JSON response"
                    ) from None
                if not isinstance(body, (dict, list)):
                    raise HugeGraphClientError(
                        "HugeGraph JSON response must be an object or list"
                    )
                return body
        except (aiohttp.ClientError, asyncio.TimeoutError, OSError):
            # aiohttp exception strings can include URLs and credentials, and
            # upstream errors can echo bindings. Neither crosses this boundary.
            raise HugeGraphClientError(
                f"HugeGraph HTTP {method} transport failure or timeout",
                transient=True,
            ) from None

    async def gremlin(
        self,
        script: str,
        bindings: dict[str, Any] | None = None,
        *,
        read_only: bool = True,
    ) -> list[Any]:
        """Execute a fixed script and validate both HTTP and Gremlin status."""
        graph = f"{self.graphspace}-{self.graph}"
        payload = {
            "gremlin": script,
            "bindings": bindings or {},
            "language": "gremlin-groovy",
            "aliases": {"graph": graph, "g": "__g_" + graph},
        }
        # Refuse NaN and unserializable bindings locally before sending a request.
        try:
            json_module.dumps(payload, allow_nan=False)
        except (TypeError, ValueError):
            raise ValueError(
                "HugeGraph Gremlin bindings must be valid JSON values"
            ) from None
        attempts = self.retries + 1 if read_only else 1
        for attempt in range(attempts):
            try:
                body = await self._request_once("POST", "/gremlin", None, payload)
                status = body.get("status") if isinstance(body, dict) else None
                code = status.get("code") if isinstance(status, dict) else None
                if type(code) is not int or code not in {200, 204}:
                    raise HugeGraphClientError(
                        "HugeGraph Gremlin failed or returned an incomplete status",
                        code=code if type(code) is int else None,
                        transient=type(code) is int and code in _TRANSIENT_CODES,
                    )
                result = body.get("result")
                data = result.get("data") if isinstance(result, dict) else None
                if not isinstance(data, list):
                    raise HugeGraphClientError(
                        "HugeGraph Gremlin response must contain result.data list"
                    )
                return data
            except HugeGraphClientError as error:
                if not error.transient or attempt + 1 == attempts:
                    raise
                await asyncio.sleep(min(0.1 * 2**attempt, 1.0))
        raise AssertionError("unreachable")

    @staticmethod
    def _validate_schema(kind: str, expected: dict[str, Any], actual: Any) -> None:
        if not isinstance(actual, dict):
            raise HugeGraphClientError(
                f"HugeGraph schema {kind}/{expected['name']} is malformed"
            )
        for field, want in expected.items():
            got = actual.get(field)
            if field == "properties" and isinstance(got, list):
                matches = (
                    len(got) == len(want)
                    and all(isinstance(item, str) for item in got)
                    and set(got) == set(want)
                )
            else:
                matches = type(got) is type(want) and got == want
            if not matches:
                raise HugeGraphClientError(
                    f"HugeGraph schema {kind}/{expected['name']} has incompatible {field}; "
                    "correct the schema explicitly or use another graph"
                )
        if actual.get("ttl_start_time", "") not in ("", None):
            raise HugeGraphClientError(
                f"HugeGraph schema {expected['name']} has incompatible TTL"
            )
        if kind == "edgelabels" and (
            actual.get("edgelabel_type", "NORMAL") != "NORMAL"
            or actual.get("links", [{VERTEX_LABEL: VERTEX_LABEL}])
            != [{VERTEX_LABEL: VERTEX_LABEL}]
        ):
            raise HugeGraphClientError(
                f"HugeGraph schema {expected['name']} has incompatible edge links"
            )
        if actual.get("status", "CREATED") not in ("CREATED", "CREATING", "REBUILDING"):
            raise HugeGraphClientError(
                f"HugeGraph schema {expected['name']} has invalid status"
            )

    async def _validate_ready_schema(
        self, kind: str, expected: dict[str, Any], actual: Any
    ) -> None:
        async def poll() -> None:
            value = actual
            while True:
                self._validate_schema(kind, expected, value)
                if value.get("status", "CREATED") == "CREATED":
                    return
                await asyncio.sleep(0.1)
                value = await self.request(
                    "GET",
                    f"{self.graph_path}/schema/{kind}/{expected['name']}",
                    retry=True,
                )

        try:
            await asyncio.wait_for(poll(), timeout=self.timeout)
        except asyncio.TimeoutError:
            raise HugeGraphClientError(
                f"HugeGraph schema {expected['name']} did not become ready before HUGEGRAPH_TIMEOUT"
            ) from None

    async def _ensure_schema(self) -> None:
        missing = []
        for kind, expected in _schema_definitions():
            collection = f"{self.graph_path}/schema/{kind}"
            path = f"{collection}/{expected['name']}"
            actual = await self.request("GET", path, allow_not_found=True, retry=True)
            if actual is None:
                if not self.auto_create_schema:
                    raise HugeGraphClientError(
                        f"HugeGraph schema {kind}/{expected['name']} is missing; "
                        "provision it or enable HUGEGRAPH_AUTO_CREATE_SCHEMA"
                    )
                missing.append((kind, expected))
            else:
                await self._validate_ready_schema(kind, expected, actual)

        missing_indexes = [
            expected for kind, expected in missing if kind == "indexlabels"
        ]
        if missing_indexes:
            await self._check_index_overlap(missing_indexes)

        # Reject all existing drift before adding definitions. A later create
        # failure may leave compatible additions; the next startup reuses them.
        for kind, expected in missing:
            collection = f"{self.graph_path}/schema/{kind}"
            path = f"{collection}/{expected['name']}"
            async with physical_write():
                try:
                    created = await self.request("POST", collection, json=expected)
                except HugeGraphClientError as error:
                    if error.status != 409 and not (
                        error.status == 400 and error.exception == "ExistedException"
                    ):
                        raise
                    # A confirmed create conflict is not an uncertain write.
                    # Read the concurrent winner, then apply the same drift check.
                else:
                    if kind == "indexlabels":
                        task_id = (
                            created.get("task_id")
                            if isinstance(created, dict)
                            else None
                        )
                        if type(task_id) is not int or task_id < 0:
                            raise HugeGraphClientError(
                                "HugeGraph index creation returned an invalid task ID"
                            )
                        if task_id > 0:
                            await self._wait_task(task_id)
                actual = await self.request("GET", path, retry=True)
                await self._validate_ready_schema(kind, expected, actual)

    async def _check_index_overlap(
        self, expected_indexes: list[dict[str, Any]]
    ) -> None:
        # One schema-index inventory only when provisioning is needed, never a
        # graph-data scan or a repeated inventory on normal startup. Operators
        # must not concurrently alter these labels during schema provisioning.
        result = await self.request(
            "GET", f"{self.graph_path}/schema/indexlabels", retry=True
        )
        indexes = result.get("indexlabels") if isinstance(result, dict) else None
        if not isinstance(indexes, list) or not all(
            isinstance(index, dict) for index in indexes
        ):
            raise HugeGraphClientError("HugeGraph schema index inventory is malformed")
        for expected in expected_indexes:
            for index in indexes:
                if (
                    index.get("name") == expected["name"]
                    or index.get("base_type") != expected["base_type"]
                    or index.get("base_value") != expected["base_value"]
                    or index.get("index_type") not in ("SECONDARY", "SHARD")
                ):
                    continue
                fields = index.get("fields")
                if not isinstance(fields, list) or not all(
                    isinstance(field, str) for field in fields
                ):
                    raise HugeGraphClientError(
                        "HugeGraph schema index fields are malformed"
                    )
                new_fields = expected["fields"]
                if (
                    new_fields[: len(fields)] == fields
                    or fields[: len(new_fields)] == new_fields
                ):
                    raise HugeGraphClientError(
                        f"HugeGraph schema index {expected['name']} overlaps an existing index; "
                        "refusing a creation that can implicitly remove or replace it"
                    )

    async def _wait_task(self, task_id: int) -> None:
        async def poll() -> None:
            while True:
                task = await self.request(
                    "GET", f"{self.graph_path}/tasks/{task_id}", retry=True
                )
                status = task.get("task_status") if isinstance(task, dict) else None
                if status == "success":
                    return
                if not isinstance(status, str) or status not in _PENDING_TASK_STATES:
                    raise HugeGraphClientError(
                        f"HugeGraph schema task {task_id} failed or returned invalid status"
                    )
                await asyncio.sleep(0.1)

        try:
            await asyncio.wait_for(poll(), timeout=self.timeout)
        except asyncio.TimeoutError:
            raise HugeGraphClientError(
                f"HugeGraph schema task {task_id} did not finish before HUGEGRAPH_TIMEOUT; "
                "it may still complete on the server, inspect it before retrying initialization"
            ) from None
