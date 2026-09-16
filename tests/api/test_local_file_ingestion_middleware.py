"""Pre-body guard for deployments that forbid local file ingestion."""

from __future__ import annotations

import importlib
import sys

import pytest

_original_argv = sys.argv[:]
sys.argv = [sys.argv[0]]
_middleware_mod = importlib.import_module("lightrag.api.local_file_ingestion_middleware")
sys.argv = _original_argv

LocalFileIngestionMiddleware = _middleware_mod.LocalFileIngestionMiddleware

pytestmark = pytest.mark.offline


def _scope(path="/documents/upload", method="POST", headers=None):
    return {
        "type": "http",
        "method": method,
        "path": path,
        "headers": headers or [],
    }


class _Recorder:
    def __init__(self):
        self.messages: list[dict] = []
        self.receives = 0

    async def receive(self):
        self.receives += 1
        raise AssertionError("disabled local upload must be refused before receive()")

    async def send(self, message):
        self.messages.append(message)

    @property
    def status(self):
        for message in self.messages:
            if message["type"] == "http.response.start":
                return message["status"]
        return None

    def body(self) -> bytes:
        return b"".join(
            message.get("body", b"")
            for message in self.messages
            if message["type"] == "http.response.body"
        )


class _Downstream:
    def __init__(self):
        self.calls = 0

    async def __call__(self, scope, receive, send):
        self.calls += 1
        await send({"type": "http.response.start", "status": 204, "headers": []})
        await send({"type": "http.response.body", "body": b""})


async def test_disabled_upload_is_refused_before_body_receive():
    downstream = _Downstream()
    recorder = _Recorder()

    await LocalFileIngestionMiddleware(downstream, enabled=False)(
        _scope(headers=[(b"content-length", b"10485760")]),
        recorder.receive,
        recorder.send,
    )

    assert recorder.status == 403
    assert recorder.receives == 0
    assert downstream.calls == 0
    assert b"Local file ingestion is disabled" in recorder.body()


async def test_object_upload_completion_is_not_a_local_file_entry():
    downstream = _Downstream()
    recorder = _Recorder()

    await LocalFileIngestionMiddleware(downstream, enabled=False)(
        _scope(path="/documents/uploads/complete"),
        recorder.receive,
        recorder.send,
    )

    assert recorder.status == 204
    assert downstream.calls == 1


async def test_api_prefix_is_stripped_before_matching():
    downstream = _Downstream()
    recorder = _Recorder()

    await LocalFileIngestionMiddleware(
        downstream,
        enabled=False,
        api_prefix="/api/v1",
    )(
        _scope(path="/api/v1/documents/upload"),
        recorder.receive,
        recorder.send,
    )

    assert recorder.status == 403
    assert recorder.receives == 0
    assert downstream.calls == 0
