"""Offline guardrails for the opt-in Kubernetes acceptance tooling."""

import json
from pathlib import Path
import stat
import subprocess
import sys
from types import SimpleNamespace

import pytest
import yaml

from .verify import Cluster


PREPARE = Path(__file__).with_name("prepare.py")


def generate(directory):
    return subprocess.run(
        [
            sys.executable,
            str(PREPARE),
            "--directory",
            str(directory),
            "--image-tag",
            "test-immutable-image",
            "--workspace",
            "local_debug_offline_fixture",
        ],
        capture_output=True,
        text=True,
    )


def test_generator_keeps_secrets_protected_and_out_of_manifests(tmp_path):
    directory = tmp_path / "fresh"
    result = generate(directory)
    assert result.returncode == 0, result.stderr
    credentials = directory / "credentials.env"
    assert stat.S_IMODE(credentials.stat().st_mode) == 0o600
    values = dict(line.split("=", 1) for line in credentials.read_text().splitlines())
    manifests = (directory / "infra.yaml").read_text()
    helm = (directory / "values.yaml").read_text()
    for key in ("POSTGRES_PASSWORD", "HUGEGRAPH_PASSWORD", "LIGHTRAG_API_KEY"):
        assert values[key] not in manifests + helm + result.stdout + result.stderr
    objects = list(yaml.safe_load_all(manifests))
    assert all(o["kind"] != "Secret" for o in objects)
    assert all(
        o["spec"].get("type", "ClusterIP") == "ClusterIP"
        for o in objects
        if o["kind"] == "Service"
    )
    assert yaml.safe_load(helm)["replicaCount"] == 0
    kind = yaml.safe_load((directory / "kind.yaml").read_text())
    workers = [n for n in kind["nodes"] if n["role"] == "worker"]
    assert len(workers) == 2 and workers[0]["extraMounts"] == workers[1]["extraMounts"]
    patch = yaml.safe_load((directory / "writers-patch.yaml").read_text())
    assert "requiredDuringSchedulingIgnoredDuringExecution" in json.dumps(patch)


def test_generator_never_replaces_existing_credentials(tmp_path):
    directory = tmp_path / "fresh"
    assert generate(directory).returncode == 0
    directory.chmod(0o750)
    original = (directory / "credentials.env").read_bytes()
    result = generate(directory)
    assert result.returncode != 0
    assert "existing credentials are never overwritten" in result.stderr
    assert (directory / "credentials.env").read_bytes() == original
    assert stat.S_IMODE(directory.stat().st_mode) == 0o750


@pytest.mark.parametrize(
    ("context", "namespace"),
    [
        ("production", "lightrag-concurrency-test"),
        ("kind-lightrag-concurrency", "production"),
        ("default", "default"),
    ],
)
def test_runner_refuses_nonacceptance_targets(context, namespace):
    with pytest.raises(ValueError, match="isolated local acceptance"):
        Cluster(SimpleNamespace(context=context, namespace=namespace))


def test_runner_always_uses_explicit_kubeconfig_context_namespace():
    cluster = Cluster(
        SimpleNamespace(
            context="kind-lightrag-concurrency",
            namespace="lightrag-concurrency-test",
            kubeconfig="/tmp/isolated/kubeconfig",
        )
    )
    assert cluster.base == [
        "kubectl",
        "--kubeconfig",
        "/tmp/isolated/kubeconfig",
        "--context",
        "kind-lightrag-concurrency",
        "-n",
        "lightrag-concurrency-test",
    ]


def test_gateway_preserves_compressed_backend_responses(monkeypatch):
    """HugeGraph can gzip responses even with Accept-Encoding: identity."""
    import gzip
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    import threading
    import urllib.request

    from . import gateway

    body = gzip.compress(b'{"vertices":[],"page":null}')

    class Backend(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_GET(self):
            self.send_response(200)
            self.send_header("Content-Type", "application/json;charset=UTF-8")
            self.send_header("Content-Encoding", "gzip")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    servers = [
        ThreadingHTTPServer(("127.0.0.1", 0), handler)
        for handler in (Backend, gateway.Handler)
    ]
    monkeypatch.setattr(
        gateway, "BACKEND", f"http://127.0.0.1:{servers[0].server_port}"
    )
    threads = [threading.Thread(target=s.serve_forever) for s in servers]
    for thread in threads:
        thread.start()
    try:
        with urllib.request.urlopen(
            f"http://127.0.0.1:{servers[1].server_port}/graph/vertices"
        ) as response:
            assert response.headers["Content-Encoding"] == "gzip"
            payload = response.read()
            assert payload == body
            assert json.loads(gzip.decompress(payload)) == {
                "vertices": [],
                "page": None,
            }
    finally:
        for server, thread in zip(servers, threads):
            server.shutdown()
            thread.join()
            server.server_close()
