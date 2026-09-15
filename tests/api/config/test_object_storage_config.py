"""Object-store ingestion configuration stays opt-in and secret-safe."""

from __future__ import annotations

import sys

import pytest

from lightrag.api.config import (
    object_storage_config_summary,
    parse_args,
    validate_object_storage_configuration,
)

pytestmark = pytest.mark.offline

_OBJECT_ENV = (
    "LIGHTRAG_OBJECT_STORAGE",
    "S3_ENDPOINT_URL",
    "S3_BUCKET",
    "S3_REGION",
    "S3_FORCE_PATH_STYLE",
    "S3_ACCESS_KEY_ID",
    "S3_SECRET_ACCESS_KEY",
    "S3_SESSION_TOKEN",
    "S3_OBJECT_PREFIX",
    "S3_PRESIGN_TTL_SECONDS",
    "S3_UPLOAD_SESSION_TTL_SECONDS",
    "S3_SCRATCH_DIR",
)


def _parse(monkeypatch, values: dict[str, str] | None = None):
    monkeypatch.setattr(sys, "argv", ["lightrag-server"])
    for key in _OBJECT_ENV:
        monkeypatch.delenv(key, raising=False)
    for key, value in (values or {}).items():
        monkeypatch.setenv(key, value)
    return parse_args()


def test_object_storage_is_disabled_by_default(monkeypatch):
    args = _parse(monkeypatch)

    assert args.object_storage == "disabled"
    assert args.object_storage_enabled is False
    validate_object_storage_configuration(args)
    assert object_storage_config_summary(args) == {"enabled": False, "provider": "disabled"}


def test_enabled_s3_requires_endpoint_bucket_and_credentials(monkeypatch):
    with pytest.raises(
        ValueError,
        match="S3_ENDPOINT_URL.*S3_BUCKET.*S3_ACCESS_KEY_ID.*S3_SECRET_ACCESS_KEY",
    ):
        _parse(monkeypatch, {"LIGHTRAG_OBJECT_STORAGE": "s3"})


def test_enabled_s3_configuration_is_secret_safe(monkeypatch):
    args = _parse(
        monkeypatch,
        {
            "LIGHTRAG_OBJECT_STORAGE": "S3ObjectStorage",
            "S3_ENDPOINT_URL": "https://minio.example.com",
            "S3_BUCKET": "lightrag-documents",
            "S3_REGION": "us-east-1",
            "S3_FORCE_PATH_STYLE": "true",
            "S3_ACCESS_KEY_ID": "access-key",
            "S3_SECRET_ACCESS_KEY": "secret-key",
            "S3_SESSION_TOKEN": "session-token",
            "S3_OBJECT_PREFIX": "tenant-a/lightrag",
            "S3_PRESIGN_TTL_SECONDS": "900",
            "S3_UPLOAD_SESSION_TTL_SECONDS": "7200",
            "S3_SCRATCH_DIR": "/scratch/lightrag",
        },
    )

    validate_object_storage_configuration(args)

    assert args.object_storage == "s3"
    assert args.object_storage_enabled is True
    assert args.s3_endpoint_url == "https://minio.example.com"
    assert args.s3_bucket == "lightrag-documents"
    assert args.s3_force_path_style is True
    assert args.s3_presign_ttl_seconds == 900
    assert args.s3_upload_session_ttl_seconds == 7200
    summary = object_storage_config_summary(args)
    assert summary == {
        "enabled": True,
        "provider": "s3",
        "endpoint_url": "https://minio.example.com",
        "bucket": "lightrag-documents",
        "region": "us-east-1",
        "force_path_style": True,
        "object_prefix": "tenant-a/lightrag",
        "presign_ttl_seconds": 900,
        "upload_session_ttl_seconds": 7200,
        "scratch_dir": "/scratch/lightrag",
    }
    assert "secret-key" not in repr(summary)
    assert "access-key" not in repr(summary)
    assert "session-token" not in repr(summary)
