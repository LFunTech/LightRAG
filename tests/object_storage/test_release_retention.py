from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone

from lightrag.object_storage import FakeObjectStore, ObjectMetadata, ObjectStoreConfig
from lightrag.object_storage.sessions import (
    InMemoryUploadSessionStore,
    UploadSessionManager,
)


async def test_release_accepted_completion_does_not_expire_during_queue_wait():
    now = datetime(2026, 10, 21, tzinfo=timezone.utc)
    manager = UploadSessionManager(
        InMemoryUploadSessionStore(), bucket="docs", now=lambda: now
    )
    session = await manager.create_session(
        workspace="prod",
        filename="a.txt",
        content_type="text/plain",
        size=3,
        checksum_sha256=None,
        ttl_seconds=1,
    )
    manager.now = lambda: now + timedelta(seconds=2)

    class Retention:
        async def accepted_before_expiry(self, upload_id, expires):
            return upload_id == session.upload_id and expires == session.expires_at

    manager.release_retention = Retention()
    result = await manager.complete_session(
        session.upload_id,
        workspace="prod",
        object_key=session.object_key,
        metadata=ObjectMetadata(
            key=session.object_key, size=3, content_type="text/plain"
        ),
    )
    assert result.status == "completed"


async def test_cleanup_skips_source_reserved_by_unfinished_command():
    now = datetime(2026, 10, 21, tzinfo=timezone.utc)
    store = FakeObjectStore(ObjectStoreConfig(provider="fake", bucket="docs"))
    manager = UploadSessionManager(
        InMemoryUploadSessionStore(), bucket="docs", now=lambda: now
    )
    session = await manager.create_session(
        workspace="prod",
        filename="a.txt",
        content_type="text/plain",
        size=3,
        checksum_sha256=None,
        ttl_seconds=1,
    )
    await store.put_bytes(session.object_key, b"abc")
    manager.now = lambda: now + timedelta(seconds=2)

    class Retention:
        @asynccontextmanager
        async def source_cleanup(self, upload_id):
            yield False

    manager.release_retention = Retention()
    assert await manager.cleanup_abandoned_uploads(store) == []
    assert await store.get_bytes(session.object_key) == b"abc"
