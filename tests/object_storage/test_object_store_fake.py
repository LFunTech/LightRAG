"""Object-store protocol test double exercises S3-compatible semantics offline."""

from __future__ import annotations

import pytest

from lightrag.object_storage import (
    FakeObjectStore,
    ObjectNotFoundError,
    ObjectStoreConfig,
    ObjectStorePreconditionError,
)

pytestmark = pytest.mark.offline


@pytest.mark.asyncio
async def test_fake_object_store_put_head_get_list_and_delete_prefix(tmp_path):
    store = FakeObjectStore(ObjectStoreConfig(provider="fake", bucket="bucket", scratch_dir=str(tmp_path)))

    await store.put_bytes("workspace/doc/source.txt", b"hello", content_type="text/plain", metadata={"m": "1"})
    await store.put_bytes("workspace/doc/sidecar/blocks.jsonl", b"{}\n")

    head = await store.head_object("workspace/doc/source.txt")
    assert head.size == 5
    assert head.content_type == "text/plain"
    assert head.metadata == {"m": "1"}
    assert await store.get_bytes("workspace/doc/source.txt") == b"hello"
    assert await store.list_keys("workspace/doc/") == [
        "workspace/doc/sidecar/blocks.jsonl",
        "workspace/doc/source.txt",
    ]

    deleted = await store.delete_prefix("workspace/doc/")
    assert deleted == 2
    with pytest.raises(ObjectNotFoundError):
        await store.head_object("workspace/doc/source.txt")


@pytest.mark.asyncio
async def test_fake_object_store_presign_is_bounded_and_secret_safe(tmp_path):
    store = FakeObjectStore(
        ObjectStoreConfig(
            provider="fake",
            bucket="bucket",
            endpoint_url="https://objects.example.com",
            access_key_id="access-key",
            secret_access_key="secret-key",
            scratch_dir=str(tmp_path),
        )
    )

    signed = await store.presign_upload(
        "uploads/ws/session/report.pdf",
        content_type="application/pdf",
        size=1024,
        checksum_sha256="abc123",
        expires_in=600,
    )

    assert signed.method == "PUT"
    assert signed.key == "uploads/ws/session/report.pdf"
    assert signed.expires_in == 600
    assert signed.headers["Content-Type"] == "application/pdf"
    assert signed.headers["x-amz-meta-sha256"] == "abc123"
    assert "secret-key" not in signed.url
    assert "access-key" not in signed.url


@pytest.mark.asyncio
async def test_fake_object_store_rejects_path_escape_keys(tmp_path):
    store = FakeObjectStore(ObjectStoreConfig(provider="fake", bucket="bucket", scratch_dir=str(tmp_path)))

    with pytest.raises(ObjectStorePreconditionError):
        await store.put_bytes("../escape.txt", b"bad")


@pytest.mark.asyncio
async def test_fake_object_store_does_not_double_apply_configured_prefix(tmp_path):
    store = FakeObjectStore(
        ObjectStoreConfig(
            provider="fake",
            bucket="bucket",
            object_prefix="lightrag",
            scratch_dir=str(tmp_path),
        )
    )

    signed = await store.presign_upload(
        "lightrag/uploads/ws/upload_1/report.pdf",
        content_type="application/pdf",
        size=1,
    )

    assert signed.key == "lightrag/uploads/ws/upload_1/report.pdf"

@pytest.mark.asyncio
async def test_delete_prefix_preserves_segment_boundary():
    store = FakeObjectStore(ObjectStoreConfig(provider="fake", bucket="docs"))
    await store.put_bytes("uploads/ws/upload-1/report.pdf", b"delete")
    await store.put_bytes("uploads/ws/upload-10/report.pdf", b"keep")

    deleted = await store.delete_prefix("uploads/ws/upload-1/")

    assert deleted == 1
    assert await store.list_keys("uploads/ws/upload-1/") == []
    assert await store.get_bytes("uploads/ws/upload-10/report.pdf") == b"keep"
