"""Upload session state machine for presigned object ingestion."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from lightrag.object_storage import FakeObjectStore, ObjectMetadata, ObjectStoreConfig
from lightrag.object_storage.sessions import (
    InMemoryUploadSessionStore,
    UploadSessionConflictError,
    UploadSessionExpiredError,
    UploadSessionManager,
    UploadSessionMismatchError,
)

pytestmark = pytest.mark.offline


def _clock(now: datetime):
    return lambda: now


@pytest.mark.asyncio
async def test_create_session_generates_server_owned_workspace_key():
    now = datetime(2026, 9, 15, tzinfo=timezone.utc)
    manager = UploadSessionManager(
        InMemoryUploadSessionStore(),
        bucket="docs",
        prefix="lightrag",
        now=_clock(now),
    )

    session = await manager.create_session(
        workspace="tenant_a",
        filename="report.[native-P].docx",
        content_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        size=42,
        checksum_sha256="a" * 64,
        ttl_seconds=600,
    )

    assert session.status == "issued"
    assert session.workspace == "tenant_a"
    assert session.object_key.startswith("lightrag/uploads/tenant_a/")
    assert session.object_key.endswith("/report.[native-P].docx")
    assert session.canonical_file_path == "report.docx"
    assert session.expires_at == now + timedelta(seconds=600)


@pytest.mark.asyncio
async def test_complete_rejects_foreign_key_and_metadata_mismatch():
    now = datetime(2026, 9, 15, tzinfo=timezone.utc)
    manager = UploadSessionManager(
        InMemoryUploadSessionStore(), bucket="docs", now=_clock(now)
    )
    session = await manager.create_session(
        workspace="tenant_a",
        filename="report.pdf",
        content_type="application/pdf",
        size=5,
        checksum_sha256="b" * 64,
        ttl_seconds=600,
    )

    with pytest.raises(UploadSessionMismatchError, match="object key"):
        await manager.complete_session(
            session.upload_id,
            workspace="tenant_a",
            object_key="uploads/tenant_b/other/report.pdf",
            metadata=ObjectMetadata(key="uploads/tenant_b/other/report.pdf", size=5),
        )

    with pytest.raises(UploadSessionMismatchError, match="size"):
        await manager.complete_session(
            session.upload_id,
            workspace="tenant_a",
            object_key=session.object_key,
            metadata=ObjectMetadata(
                key=session.object_key,
                size=4,
                content_type="application/pdf",
                checksum_sha256="b" * 64,
            ),
        )

    with pytest.raises(UploadSessionMismatchError, match="checksum"):
        await manager.complete_session(
            session.upload_id,
            workspace="tenant_a",
            object_key=session.object_key,
            metadata=ObjectMetadata(
                key=session.object_key,
                size=5,
                content_type="application/pdf",
                checksum_sha256=None,
            ),
        )


@pytest.mark.asyncio
async def test_complete_marks_session_completed_and_is_idempotent():
    now = datetime(2026, 9, 15, tzinfo=timezone.utc)
    manager = UploadSessionManager(
        InMemoryUploadSessionStore(), bucket="docs", now=_clock(now)
    )
    session = await manager.create_session(
        workspace="tenant_a",
        filename="report.pdf",
        content_type="application/pdf",
        size=5,
        checksum_sha256="c" * 64,
        ttl_seconds=600,
    )
    metadata = ObjectMetadata(
        key=session.object_key,
        size=5,
        content_type="application/pdf",
        etag="etag-1",
        checksum_sha256="c" * 64,
    )

    completed = await manager.complete_session(
        session.upload_id,
        workspace="tenant_a",
        object_key=session.object_key,
        metadata=metadata,
    )
    repeated = await manager.complete_session(
        session.upload_id,
        workspace="tenant_a",
        object_key=session.object_key,
        metadata=metadata,
    )

    assert completed.status == "completed"
    assert completed.completed_at == now
    assert completed.object_etag == "etag-1"
    assert repeated == completed


@pytest.mark.asyncio
async def test_expired_session_cannot_complete():
    now = datetime(2026, 9, 15, tzinfo=timezone.utc)
    manager = UploadSessionManager(
        InMemoryUploadSessionStore(), bucket="docs", now=_clock(now)
    )
    session = await manager.create_session(
        workspace="tenant_a",
        filename="report.pdf",
        content_type="application/pdf",
        size=5,
        checksum_sha256=None,
        ttl_seconds=1,
    )
    manager.now = _clock(now + timedelta(seconds=2))

    with pytest.raises(UploadSessionExpiredError):
        await manager.complete_session(
            session.upload_id,
            workspace="tenant_a",
            object_key=session.object_key,
            metadata=ObjectMetadata(key=session.object_key, size=5, content_type="application/pdf"),
        )

    stored = await manager.get_session(session.upload_id)
    assert stored.status == "expired"


@pytest.mark.asyncio
async def test_completed_session_cannot_be_aborted_or_garbage_collected():
    now = datetime(2026, 9, 15, tzinfo=timezone.utc)
    store = InMemoryUploadSessionStore()
    manager = UploadSessionManager(store, bucket="docs", now=_clock(now))
    session = await manager.create_session(
        workspace="tenant_a",
        filename="report.pdf",
        content_type="application/pdf",
        size=5,
        checksum_sha256=None,
        ttl_seconds=1,
    )
    await manager.complete_session(
        session.upload_id,
        workspace="tenant_a",
        object_key=session.object_key,
        metadata=ObjectMetadata(key=session.object_key, size=5, content_type="application/pdf"),
    )

    with pytest.raises(UploadSessionConflictError):
        await manager.abort_session(session.upload_id, workspace="tenant_a")
    assert await manager.expired_unfinished_sessions(now + timedelta(days=1)) == []


@pytest.mark.asyncio
async def test_cleanup_abandoned_uploads_deletes_only_expired_unfinished_prefixes():
    now = datetime(2026, 9, 15, tzinfo=timezone.utc)
    store = InMemoryUploadSessionStore()
    manager = UploadSessionManager(
        store,
        bucket="docs",
        prefix="lightrag",
        now=_clock(now),
    )
    object_store = FakeObjectStore(
        ObjectStoreConfig(provider="fake", bucket="docs", object_prefix="lightrag")
    )
    expired = await manager.create_session(
        workspace="tenant_a",
        filename="expired.pdf",
        content_type="application/pdf",
        size=7,
        checksum_sha256=None,
        ttl_seconds=1,
    )
    completed = await manager.create_session(
        workspace="tenant_a",
        filename="completed.pdf",
        content_type="application/pdf",
        size=9,
        checksum_sha256=None,
        ttl_seconds=1,
    )
    await object_store.put_bytes(expired.object_key, b"expired")
    await object_store.put_bytes(completed.object_key, b"completed")
    await manager.complete_session(
        completed.upload_id,
        workspace="tenant_a",
        object_key=completed.object_key,
        metadata=ObjectMetadata(
            key=completed.object_key,
            size=9,
            content_type="application/pdf",
        ),
    )

    cleaned = await manager.cleanup_abandoned_uploads(
        object_store,
        at=now + timedelta(seconds=2),
    )

    assert [item.upload_id for item in cleaned] == [expired.upload_id]
    assert (await manager.get_session(expired.upload_id)).status == "expired"
    assert await object_store.list_keys(f"lightrag/uploads/tenant_a/{expired.upload_id}/") == []
    assert await object_store.get_bytes(completed.object_key) == b"completed"
