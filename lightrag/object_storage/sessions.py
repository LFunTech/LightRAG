"""Upload session records for presigned object ingestion."""

from __future__ import annotations

import secrets
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Protocol

from lightrag.object_storage import ObjectMetadata
from lightrag.utils_pipeline import normalize_document_file_path
from lightrag.utils import validate_workspace


class UploadSessionError(RuntimeError):
    """Base class for upload-session validation failures."""


class UploadSessionNotFoundError(UploadSessionError):
    """Raised when an upload session id does not exist."""


class UploadSessionExpiredError(UploadSessionError):
    """Raised when an expired upload session is completed."""


class UploadSessionMismatchError(UploadSessionError):
    """Raised when a completion request does not match the issued session."""


class UploadSessionConflictError(UploadSessionError):
    """Raised when a terminal session is mutated in an incompatible way."""


@dataclass(frozen=True)
class UploadSession:
    upload_id: str
    workspace: str
    bucket: str
    object_key: str
    filename: str
    canonical_file_path: str
    content_type: str
    declared_size: int
    checksum_sha256: str | None
    status: str
    created_at: datetime
    expires_at: datetime
    completed_at: datetime | None = None
    object_etag: str | None = None
    object_size: int | None = None
    object_content_type: str | None = None
    track_id: str | None = None
    enqueued_at: datetime | None = None

    def to_record(self) -> dict[str, Any]:
        record = asdict(self)
        for key in ("created_at", "expires_at", "completed_at", "enqueued_at"):
            value = record.get(key)
            if isinstance(value, datetime):
                record[key] = value.isoformat()
        return record

    @classmethod
    def from_record(cls, record: dict[str, Any]) -> "UploadSession":
        data = dict(record)
        data.pop("_id", None)
        data.pop("id", None)
        for key in ("created_at", "expires_at", "completed_at", "enqueued_at"):
            value = data.get(key)
            if isinstance(value, str) and value:
                data[key] = datetime.fromisoformat(value)
        return cls(**data)


class UploadSessionStore(Protocol):
    async def get(self, upload_id: str) -> UploadSession | None: ...

    async def put(self, session: UploadSession) -> None: ...

    async def list(self) -> list[UploadSession]: ...


class InMemoryUploadSessionStore:
    def __init__(self):
        self._sessions: dict[str, UploadSession] = {}

    async def get(self, upload_id: str) -> UploadSession | None:
        return self._sessions.get(upload_id)

    async def put(self, session: UploadSession) -> None:
        self._sessions[session.upload_id] = session

    async def list(self) -> list[UploadSession]:
        return list(self._sessions.values())


class KVUploadSessionStore:
    """Upload-session store backed by a LightRAG KV namespace."""

    def __init__(self, kv_storage):
        self.kv_storage = kv_storage

    async def get(self, upload_id: str) -> UploadSession | None:
        record = await self.kv_storage.get_by_id(upload_id)
        if not record:
            return None
        return UploadSession.from_record(record)

    async def put(self, session: UploadSession) -> None:
        await self.kv_storage.upsert({session.upload_id: session.to_record()})
        callback = getattr(self.kv_storage, "index_done_callback", None)
        if callback is not None:
            await callback()

    async def list(self) -> list[UploadSession]:
        get_all = getattr(self.kv_storage, "get_all", None)
        if get_all is not None:
            return [
                UploadSession.from_record(dict(row)) for row in await get_all() if row
            ]
        data = getattr(self.kv_storage, "_data", None)
        if data is None:
            return []
        return [UploadSession.from_record(dict(row)) for row in data.values()]


class UploadSessionManager:
    def __init__(
        self,
        store: UploadSessionStore,
        *,
        bucket: str,
        prefix: str = "",
        now: Callable[[], datetime] | None = None,
    ):
        self.store = store
        self.bucket = bucket
        self.prefix = prefix.strip().strip("/")
        self.now = now or (lambda: datetime.now(timezone.utc))

    async def create_session(
        self,
        *,
        workspace: str,
        filename: str,
        content_type: str,
        size: int,
        checksum_sha256: str | None,
        ttl_seconds: int,
    ) -> UploadSession:
        validate_workspace(workspace)
        if size < 0:
            raise UploadSessionMismatchError("declared size must be non-negative")
        safe_filename = Path(filename).name
        if safe_filename != filename or not safe_filename:
            raise UploadSessionMismatchError("filename must be a single basename")
        canonical = normalize_document_file_path(safe_filename)
        upload_id = self._new_id()
        created = self.now()
        key_parts = [part for part in (self.prefix, "uploads", workspace or "default", upload_id, safe_filename) if part]
        session = UploadSession(
            upload_id=upload_id,
            workspace=workspace,
            bucket=self.bucket,
            object_key="/".join(key_parts),
            filename=safe_filename,
            canonical_file_path=canonical,
            content_type=content_type,
            declared_size=size,
            checksum_sha256=checksum_sha256,
            status="issued",
            created_at=created,
            expires_at=created + timedelta(seconds=ttl_seconds),
        )
        await self.store.put(session)
        return session

    async def get_session(self, upload_id: str) -> UploadSession:
        session = await self.store.get(upload_id)
        if session is None:
            raise UploadSessionNotFoundError(f"upload session not found: {upload_id}")
        return session

    async def complete_session(
        self,
        upload_id: str,
        *,
        workspace: str,
        object_key: str,
        metadata: ObjectMetadata,
    ) -> UploadSession:
        session = await self.get_session(upload_id)
        if session.status == "completed":
            self._validate_completion(session, workspace, object_key, metadata)
            return session
        if session.status != "issued":
            raise UploadSessionConflictError(f"upload session is {session.status}")
        if self.now() > session.expires_at:
            expired = self._replace(session, status="expired")
            await self.store.put(expired)
            raise UploadSessionExpiredError(f"upload session expired: {upload_id}")
        self._validate_completion(session, workspace, object_key, metadata)
        completed = self._replace(
            session,
            status="completed",
            completed_at=self.now(),
            object_etag=metadata.etag,
            object_size=metadata.size,
            object_content_type=metadata.content_type,
        )
        await self.store.put(completed)
        return completed

    async def abort_session(self, upload_id: str, *, workspace: str) -> UploadSession:
        session = await self.get_session(upload_id)
        if session.workspace != workspace:
            raise UploadSessionMismatchError("workspace does not match upload session")
        if session.status == "completed":
            raise UploadSessionConflictError("completed upload session cannot be aborted")
        aborted = self._replace(session, status="aborted")
        await self.store.put(aborted)
        return aborted

    async def mark_enqueued(
        self,
        upload_id: str,
        *,
        workspace: str,
        track_id: str,
    ) -> UploadSession:
        session = await self.get_session(upload_id)
        if session.workspace != workspace:
            raise UploadSessionMismatchError("workspace does not match upload session")
        if session.status != "completed":
            raise UploadSessionConflictError(
                "upload session must be completed before it can be marked enqueued"
            )
        if session.track_id:
            if session.track_id != track_id:
                raise UploadSessionConflictError(
                    "upload session is already associated with another track"
                )
            return session
        enqueued = self._replace(
            session,
            track_id=track_id,
            enqueued_at=self.now(),
        )
        await self.store.put(enqueued)
        return enqueued

    async def expired_unfinished_sessions(self, at: datetime | None = None) -> list[UploadSession]:
        at = at or self.now()
        sessions = await self.store.list()
        return [
            session
            for session in sessions
            if session.status == "issued" and session.expires_at < at
        ]

    async def cleanup_abandoned_uploads(
        self,
        object_store: Any,
        *,
        at: datetime | None = None,
    ) -> list[UploadSession]:
        """Expire unfinished sessions and delete only their upload prefixes."""
        cleaned: list[UploadSession] = []
        for session in await self.expired_unfinished_sessions(at):
            prefix = session.object_key.rsplit("/", 1)[0] + "/"
            await object_store.delete_prefix(prefix)
            expired = self._replace(session, status="expired")
            await self.store.put(expired)
            cleaned.append(expired)
        return cleaned

    def _validate_completion(
        self,
        session: UploadSession,
        workspace: str,
        object_key: str,
        metadata: ObjectMetadata,
    ) -> None:
        if session.workspace != workspace:
            raise UploadSessionMismatchError("workspace does not match upload session")
        if object_key != session.object_key or metadata.key != session.object_key:
            raise UploadSessionMismatchError("object key does not match upload session")
        if metadata.size != session.declared_size:
            raise UploadSessionMismatchError("object size does not match upload session")
        if metadata.content_type and metadata.content_type != session.content_type:
            raise UploadSessionMismatchError("object content type does not match upload session")
        if session.checksum_sha256:
            if not metadata.checksum_sha256:
                raise UploadSessionMismatchError(
                    "object checksum does not match upload session"
                )
            if metadata.checksum_sha256.lower() != session.checksum_sha256.lower():
                raise UploadSessionMismatchError(
                    "object checksum does not match upload session"
                )

    @staticmethod
    def _replace(session: UploadSession, **updates: Any) -> UploadSession:
        data = session.to_record()
        data.update(updates)
        return UploadSession.from_record(data)

    @staticmethod
    def _new_id() -> str:
        return "upload_" + secrets.token_urlsafe(18).rstrip("=")
