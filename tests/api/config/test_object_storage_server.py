"""Server wiring for object-backed uploads uses persistent KV sessions."""

from __future__ import annotations

import importlib
import sys
from types import SimpleNamespace

import pytest

pytestmark = pytest.mark.offline


class _KVStorage:
    def __init__(self, **kwargs):
        self.kwargs = kwargs
        self.initialized = False

    async def initialize(self):
        self.initialized = True

    async def finalize(self):
        self.initialized = False


class _Rag:
    workspace = "tenant_a"
    embedding_func = object()

    def __init__(self):
        self.created_storage = None

    def key_string_value_json_storage_cls(self, **kwargs):
        self.created_storage = _KVStorage(**kwargs)
        return self.created_storage


def _args(**overrides):
    base = {
        "object_storage_enabled": False,
        "object_storage": "disabled",
        "s3_endpoint_url": None,
        "s3_bucket": None,
        "s3_region": None,
        "s3_force_path_style": False,
        "s3_access_key_id": None,
        "s3_secret_access_key": None,
        "s3_session_token": None,
        "s3_object_prefix": "",
        "s3_presign_ttl_seconds": 900,
        "s3_upload_session_ttl_seconds": 3600,
        "s3_scratch_dir": None,
    }
    base.update(overrides)
    return SimpleNamespace(**base)


def _build_components_fn():
    original_argv = sys.argv[:]
    sys.argv = [sys.argv[0]]
    try:
        module = importlib.import_module("lightrag.api.lightrag_server")
    finally:
        sys.argv = original_argv
    return module._build_object_upload_components


def test_disabled_object_upload_components_are_noops():
    _build_object_upload_components = _build_components_fn()

    components = _build_object_upload_components(_args(), _Rag())

    assert components.object_store is None
    assert components.upload_session_manager is None
    assert components.upload_session_storage is None


def test_enabled_object_upload_components_use_s3_and_kv_namespace():
    _build_object_upload_components = _build_components_fn()

    rag = _Rag()
    components = _build_object_upload_components(
        _args(
            object_storage_enabled=True,
            object_storage="s3",
            s3_endpoint_url="https://objects.example.com",
            s3_bucket="docs",
            s3_region="us-east-1",
            s3_force_path_style=True,
            s3_access_key_id="access-key",
            s3_secret_access_key="secret-key",
            s3_object_prefix="lightrag",
            s3_scratch_dir="/tmp/lightrag-scratch",
        ),
        rag,
    )

    assert components.object_store is not None
    assert components.object_store.config.provider == "s3"
    assert components.object_store.config.bucket == "docs"
    assert components.object_store.config.object_prefix == "lightrag"
    assert components.upload_session_manager is not None
    assert components.upload_session_manager.prefix == "lightrag"
    assert components.upload_session_storage is rag.created_storage
    assert rag.created_storage.kwargs["namespace"] == "upload_sessions"
    assert rag.created_storage.kwargs["workspace"] == "tenant_a"


def test_object_upload_components_derive_enabled_from_provider_when_flag_missing():
    _build_object_upload_components = _build_components_fn()

    rag = _Rag()
    args = _args(
        object_storage_enabled=True,
        object_storage="s3",
        s3_endpoint_url="https://objects.example.com",
        s3_bucket="docs",
        s3_access_key_id="access-key",
        s3_secret_access_key="secret-key",
    )
    delattr(args, "object_storage_enabled")

    components = _build_object_upload_components(args, rag)

    assert components.object_store is not None
    assert components.upload_session_manager is not None
