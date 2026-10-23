import sys
from types import SimpleNamespace
from unittest.mock import MagicMock


def test_complete_api_route_registration_accepts_the_versioned_release_codecs(
    monkeypatch, tmp_path
):
    monkeypatch.setattr(sys, "argv", ["lightrag-server"])
    for key, value in {
        "LLM_BINDING": "openai",
        "EMBEDDING_BINDING": "openai",
        "EMBEDDING_BINDING_HOST": "https://model.invalid/v1",
        "EMBEDDING_MODEL": "fixture",
        "EMBEDDING_DIM": "8",
        "EMBEDDING_ASYMMETRIC": "false",
        "EMBEDDING_DOCUMENT_PREFIX": "NO_PREFIX",
        "EMBEDDING_QUERY_PREFIX": "NO_PREFIX",
        "LIGHTRAG_API_KEY": "synthetic-private",
        "WORKING_DIR": str(tmp_path / "working"),
        "INPUT_DIR": str(tmp_path / "inputs"),
        "LIGHTRAG_OBJECT_STORAGE": "",
        "ENABLE_LOCAL_FILE_INGESTION": "true",
    }.items():
        monkeypatch.setenv(key, value)
    from lightrag.api.config import parse_args
    from lightrag.api import lightrag_server
    from lightrag.distributed.runtime import DistributedRuntime
    from lightrag.distributed.release.runtime import ReleaseRuntime
    from lightrag.distributed.release.config import ReleaseConfig

    rag = MagicMock()
    runtime = DistributedRuntime(SimpleNamespace(), workspace="fixture")
    runtime.release = ReleaseRuntime(
        runtime,
        rag,
        ReleaseConfig("prod/fixture", "green", "pod-1", "a" * 64, "b" * 64),
    )
    rag._distributed_runtime = runtime
    monkeypatch.setattr(lightrag_server, "LightRAG", lambda **kwargs: rag)
    app = lightrag_server.create_app(parse_args())
    assert app is not None
    names = {route.name for route in app.routes}
    assert {"get_readiness", "release_local_status", "release_command_status"} <= names
    assert "http.insert_text" in {
        name for name, version in runtime.release.registry.handlers
    }
