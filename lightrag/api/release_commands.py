"""Versioned HTTP command transport through the original API and auth checks."""

import base64
from contextvars import ContextVar
from datetime import datetime, timezone
import json
import time
from uuid import UUID, uuid4
from hashlib import sha256

from fastapi.routing import APIRoute
from starlette.requests import Request
from starlette.responses import JSONResponse, Response

from lightrag.distributed.release.authorization import AuthorizationPolicy
from lightrag.distributed.release.commands import canonical
from lightrag.distributed.release.executor import CommandOutcome
from lightrag.distributed.release.models import ReleaseError, ReleaseUnavailable


WRITE_ROUTES = {
    "presign_object_upload",
    "complete_object_upload",
    "scan_for_new_documents",
    "repair_source_conflict",
    "upload_to_input_dir",
    "insert_text",
    "insert_texts",
    "clear_documents",
    "delete_document",
    "reprocess_failed_documents",
    "force_reset_recovery",
    "cancel_pipeline",
    "update_entity",
    "update_relation",
    "create_entity",
    "create_relation",
    "merge_entities",
    "delete_entity",
    "delete_relation",
}
READ_POST_ROUTES = {
    "query_text",
    "query_text_stream",
    "query_data",
    "generate",
    "chat",
    "get_documents_paginated",
}
PUBLIC_ROUTES = {
    "login",
    "health_check",
    "get_health",
    "get_readiness",
    "release_local_status",
    "release_command_status",
}
_replaying = ContextVar("release_http_replay", default=None)


def encoded(value):
    return base64.b64encode(value).decode("ascii")


def decoded(value):
    return base64.b64decode(value, validate=True)


class ReleaseHTTPBridge:
    def __init__(self, app, release, *, api_key, auth_handler):
        self.app = app
        self.release = release
        self.authorization = AuthorizationPolicy(
            scope=release.config.scope, api_key=api_key, auth_handler=auth_handler
        )
        self.routes = []
        self.release.http_bridge = self

    def install(self):
        names = set()
        for route in self.app.routes:
            if not isinstance(route, APIRoute):
                continue
            mutations = set(route.methods) - {"GET", "HEAD", "OPTIONS"}
            if (
                mutations
                and route.name not in WRITE_ROUTES | READ_POST_ROUTES | PUBLIC_ROUTES
            ):
                raise ReleaseError("API writer lacks a versioned release codec")
            if route.name in WRITE_ROUTES:
                if route.name in names:
                    raise ReleaseError("Duplicate HTTP command identity")
                names.add(route.name)
                self.release.registry.register(
                    "http." + route.name, version=1, handler=self._handler(route)
                )
            self.routes.append(route)

    def match(self, path, method):
        for route in self.routes:
            if method in route.methods and route.path_regex.fullmatch(path):
                return route
        return None

    def replaying(self):
        return _replaying.get() is self and self.release.has_ticket()

    async def authenticate(self, scope):
        headers = {}
        for key, value in scope.get("headers", []):
            name = key.decode("latin-1").lower()
            if name in {"authorization", "x-api-key"} and name in headers:
                raise ReleaseError("AuthenticationDenied")
            headers[name] = value.decode("latin-1")
        return await self.authorization.authenticate(headers), headers

    def _handler(self, route):
        async def handle(payload):
            if (
                payload["method"] not in route.methods
                or route.path_regex.fullmatch(payload["path"]) is None
            ):
                raise ReleaseError("Persisted HTTP route version does not match")
            principal = self.current_command.principal
            headers = [
                (key.encode("latin-1"), value.encode("latin-1"))
                for key, value in payload["headers"]
            ]
            headers += await self.authorization.execution_headers(principal)
            body = decoded(payload["body"])
            headers.append((b"content-length", str(len(body)).encode()))
            scope = {
                "type": "http",
                "asgi": {"version": "3.0", "spec_version": "2.4"},
                "http_version": "1.1",
                "method": payload["method"],
                "scheme": "https",
                "path": payload["path"],
                "raw_path": payload["path"].encode(),
                "root_path": "",
                "query_string": decoded(payload["query"]),
                "headers": headers,
                "server": ("release-internal", 443),
                "client": ("127.0.0.1", 0),
            }
            delivered = False
            response = {"status": None, "headers": [], "chunks": []}

            async def receive():
                nonlocal delivered
                if not delivered:
                    delivered = True
                    return {"type": "http.request", "body": body, "more_body": False}
                return {"type": "http.disconnect"}

            async def send(message):
                if message["type"] == "http.response.start":
                    response["status"] = message["status"]
                    response["headers"] = message.get("headers", [])
                elif message["type"] == "http.response.body":
                    response["chunks"].append(message.get("body", b""))

            token = _replaying.set(self)
            try:
                await self.app(scope, receive, send)
            finally:
                _replaying.reset(token)
            if response["status"] is None:
                raise ReleaseUnavailable(
                    "HTTP execution did not acknowledge a response"
                )
            output = b"".join(response["chunks"])
            presign = False
            if route.name == "presign_object_upload" and response["status"] < 400:
                data = json.loads(output)
                data.pop("upload_url", None)
                data.pop("headers", None)
                output = canonical(data).encode()
                presign = True
            safe_headers = []
            renew = False
            for key, value in response["headers"]:
                name = key.decode("latin-1").lower()
                if name in {
                    "content-type",
                    "cache-control",
                    "content-disposition",
                    "etag",
                }:
                    safe_headers.append((name, value.decode("latin-1")))
                if name == "x-new-token":
                    renew = True
            result = {
                "transport": "http-v1",
                "status": response["status"],
                "headers": safe_headers,
                "body": encoded(output),
                "renew_token": renew,
                "presign": presign,
            }
            status = (
                "SUCCEEDED"
                if response["status"] < 400
                else ("FAILED" if response["status"] < 500 else "UNCERTAIN")
            )
            return CommandOutcome(status, result)

        return handle

    async def execute(self, envelope, handler):
        token = _command.set(envelope)
        try:
            return await handler(envelope.payload)
        finally:
            _command.reset(token)

    @property
    def current_command(self):
        return _command.get()

    async def submit(self, scope, receive, path, route):
        try:
            principal, headers = await self.authenticate(scope)
        except ReleaseError:
            return JSONResponse({"detail": "Authentication required"}, status_code=401)
        await self.release.refresh()
        limit = self.release.scope_state["policy"].get("command_byte_limit", 1048576)
        chunks = []
        total = 0
        while True:
            message = await receive()
            if message["type"] == "http.disconnect":
                return Response(status_code=499)
            data = message.get("body", b"")
            total += len(data)
            if total > limit:
                return JSONResponse(
                    {"detail": "Request exceeds release command capacity"},
                    status_code=429,
                    headers={"Retry-After": "1"},
                )
            chunks.append(data)
            if not message.get("more_body"):
                break
        body = b"".join(chunks)
        refs = []
        if route.name == "complete_object_upload":
            try:
                data = json.loads(body)
                if isinstance(data.get("upload_id"), str):
                    refs.append("session:" + data["upload_id"])
                if isinstance(data.get("object_key"), str):
                    refs.append("object:" + data["object_key"])
            except (ValueError, AttributeError):
                pass
        payload = {
            "method": scope["method"],
            "path": path,
            "query": encoded(scope.get("query_string", b"")),
            "body": encoded(body),
            "headers": [
                (name, headers[name])
                for name in ("content-type", "accept")
                if name in headers
            ],
        }
        envelope = self.release.registry.encode(
            "http." + route.name,
            payload,
            principal=principal,
            scope=self.release.config.scope,
            key=headers.get("idempotency-key") or uuid4().hex,
            source_refs=refs,
        )
        async with self.release.lifecycle.track("waiters"):
            receipt = await self.release.store.enqueue(envelope)
            result = await self.release.store.wait_result(
                receipt,
                principal_id=principal["id"],
                deadline=time.monotonic()
                + self.release.scope_state["policy"]["request_wait_seconds"],
            )
        response = await self.response(result, principal)
        response.headers["X-Release-Command-Id"] = str(receipt["id"])
        return response

    async def response(self, record, principal):
        result = record["result"]
        if result is None:
            return JSONResponse(
                {"detail": {"command_id": str(record["id"]), "state": record["state"]}},
                status_code=504,
            )
        if not isinstance(result, dict) or result.get("transport") != "http-v1":
            return JSONResponse(
                {
                    "detail": {
                        "command_id": str(record["id"]),
                        "state": record["state"],
                        "error": result.get("error", "ReleaseCommandFailed"),
                    }
                },
                status_code=403
                if result.get("error") == "AuthorizationDenied"
                else 503,
            )
        body = decoded(result["body"])
        headers = dict(result["headers"])
        if (
            result.get("renew_token")
            and principal.get("scheme") == "account"
            and await self.authorization.authorize(
                principal, "http.response", self.release.config.scope
            )
        ):
            headers["X-New-Token"] = self.authorization.auth_handler.create_token(
                principal["subject"], role=principal["roles"][0]
            )
        if result.get("presign"):
            data = json.loads(body)
            store = getattr(self.release.rag, "object_store", None)
            sessions = getattr(self, "sessions", None)
            if store is None or sessions is None:
                raise ReleaseUnavailable("Presign response signer is not available")
            async with self.release.runtime.operation("release_read"):
                session = await sessions.get_session(data["upload_id"])
            ttl = min(
                data["expires_in"],
                int((session.expires_at - datetime.now(timezone.utc)).total_seconds()),
            )
            if ttl < 1:
                return JSONResponse(
                    {
                        "detail": "Upload session expired",
                        "command_id": str(record["id"]),
                    },
                    status_code=409,
                )
            signed = await store.presign_upload(
                session.object_key,
                content_type=session.content_type,
                size=session.declared_size,
                checksum_sha256=session.checksum_sha256,
                expires_in=ttl,
            )
            data.update(
                upload_url=signed.url,
                method=signed.method,
                headers=signed.headers,
                expires_in=ttl,
            )
            body = canonical(data).encode()
        return Response(body, status_code=result["status"], headers=headers)


_command = ContextVar("release_http_command", default=None)


def register_release_endpoints(app, bridge):
    from lightrag.distributed.release.readiness import readiness

    release = bridge.release

    @app.get("/ready", include_in_schema=False)
    async def get_readiness():
        report = await readiness(release.runtime, release)
        return JSONResponse(report, status_code=200 if report["ready"] else 503)

    @app.get("/release/status", include_in_schema=False)
    async def release_local_status(request: Request):
        try:
            await bridge.authenticate(request.scope)
        except ReleaseError:
            return JSONResponse({"detail": "Authentication required"}, status_code=401)
        await release.refresh()
        task = getattr(release.runtime, "pipeline_task", None)
        credentials = bridge.authorization
        auth_version = sha256(
            canonical(
                [
                    credentials.api_key,
                    credentials.auth_handler.accounts,
                    getattr(credentials.auth_handler, "secret", None),
                    getattr(credentials.auth_handler, "algorithm", None),
                ]
            ).encode()
        ).hexdigest()
        report = {
            "instance_id": release.config.instance_id,
            "slot": release.config.slot,
            "epoch": release.scope_state["epoch"],
            "phase": release.scope_state["phase"],
            "active_slot": release.scope_state["active_slot"],
            "config_hash": release.config.config_hash,
            "profile_hash": release.config.profile_hash,
            "protocol_version": 1,
            "command_formats_hash": sha256(
                canonical(sorted(release.registry.handlers)).encode()
            ).hexdigest(),
            "authorization_version": auth_version,
            "shutdown_ready": release.shutdown_hook is not None,
            "lifecycle": release.lifecycle.snapshot(),
            "polling_active": task is not None and not task.done(),
            "polling_error": getattr(release.runtime, "pipeline_error", None),
            "readiness": await readiness(release.runtime, release),
            "cache_fills_skipped": release.cache_skipped,
        }
        metrics = getattr(release.rag, "query_metrics", None)
        if metrics is not None and metrics.enabled:
            from lightrag.distributed.release.usage import compact_usage, FINAL_PATH

            report["query_usage"] = compact_usage(metrics)
            report["usage_final_supported"] = (
                getattr(release.config, "usage_final_path", None) == FINAL_PATH
            )
        return report

    @app.get("/release/commands/{command_id}", include_in_schema=False)
    async def release_command_status(command_id: UUID, request: Request):
        try:
            principal, _ = await bridge.authenticate(request.scope)
        except ReleaseError:
            return JSONResponse({"detail": "Authentication required"}, status_code=401)
        record = await release.store.command(command_id, principal_id=principal["id"])
        if record is None:
            return JSONResponse({"detail": "Command not found"}, status_code=404)
        if record["state"] in {"QUEUED", "EXECUTING"}:
            return {"command_id": str(command_id), "state": record["state"]}
        response = await bridge.response(record, principal)
        response.headers["X-Release-Command-Id"] = str(command_id)
        return response


def install_release_api(
    app, release, *, api_key, auth_handler, sessions=None, usage_authority=None
):
    if usage_authority is not None:
        raise ReleaseError(
            "Query accounting authority requires separately verified release reliability before activation"
        )
    bridge = ReleaseHTTPBridge(app, release, api_key=api_key, auth_handler=auth_handler)
    bridge.sessions = sessions
    if sessions is not None:
        sessions.release_retention = release
    register_release_endpoints(app, bridge)
    bridge.install()
    from .release_lifecycle import ReleaseMiddleware

    app.add_middleware(ReleaseMiddleware, bridge=bridge)
    return bridge
