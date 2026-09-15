"""Object-store support for document source and parsed artifact storage.

The module is deliberately independent from the document API routes so object
storage can stay an opt-in extension. A disabled deployment never imports S3
client libraries on the existing local upload/scan path.
"""

from __future__ import annotations

import base64
import hashlib
import os
import posixpath
import shutil
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol
from urllib.parse import quote, unquote, urlsplit


class ObjectStoreError(RuntimeError):
    """Base class for object-store failures that are safe to surface."""


class ObjectNotFoundError(ObjectStoreError):
    """Raised when an object key does not exist."""


class ObjectStorePreconditionError(ObjectStoreError):
    """Raised when a caller asks for an unsafe or invalid object operation."""


class ObjectStoreUnavailableError(ObjectStoreError):
    """Raised when the configured object store cannot be reached or used."""


@dataclass(frozen=True)
class ObjectStoreConfig:
    provider: str
    bucket: str
    endpoint_url: str | None = None
    region: str | None = None
    force_path_style: bool = False
    access_key_id: str | None = None
    secret_access_key: str | None = None
    session_token: str | None = None
    object_prefix: str = ""
    presign_ttl_seconds: int = 900
    upload_session_ttl_seconds: int = 3600
    scratch_dir: str | None = None

    def normalized_prefix(self) -> str:
        prefix = (self.object_prefix or "").strip().strip("/")
        if not prefix:
            return ""
        _validate_key(prefix + "/probe")
        return prefix

    def scoped_key(self, key: str) -> str:
        key = key.strip().lstrip("/")
        return _validate_key(key)


@dataclass(frozen=True)
class ObjectMetadata:
    key: str
    size: int
    content_type: str | None = None
    etag: str | None = None
    checksum_sha256: str | None = None
    metadata: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class PresignedUpload:
    method: str
    bucket: str
    key: str
    url: str
    headers: dict[str, str]
    expires_in: int


class ObjectStore(Protocol):
    config: ObjectStoreConfig

    async def preflight(self) -> None: ...

    async def head_object(self, key: str) -> ObjectMetadata: ...

    async def get_bytes(self, key: str) -> bytes: ...

    async def put_bytes(
        self,
        key: str,
        data: bytes,
        *,
        content_type: str | None = None,
        metadata: dict[str, str] | None = None,
    ) -> ObjectMetadata: ...

    async def delete_object(self, key: str) -> bool: ...

    async def list_keys(self, prefix: str) -> list[str]: ...

    async def delete_prefix(self, prefix: str) -> int: ...

    async def presign_upload(
        self,
        key: str,
        *,
        content_type: str,
        size: int,
        checksum_sha256: str | None = None,
        expires_in: int | None = None,
    ) -> PresignedUpload: ...


_SAFE_KEY_SEGMENTS = {"", ".", ".."}


def _validate_key(key: str) -> str:
    key = str(key or "").strip()
    if not key:
        raise ObjectStorePreconditionError("object key must not be empty")
    if key.startswith("/") or "\x00" in key:
        raise ObjectStorePreconditionError("object key must be relative and non-NUL")
    normalized = posixpath.normpath(key)
    if normalized == "." or normalized.startswith("../") or normalized == "..":
        raise ObjectStorePreconditionError("object key must not escape its prefix")
    parts = normalized.split("/")
    if any(part in _SAFE_KEY_SEGMENTS for part in parts):
        raise ObjectStorePreconditionError("object key must not contain empty or dot segments")
    return normalized


def _validate_prefix(prefix: str) -> str:
    prefix = str(prefix or "").strip().lstrip("/")
    if not prefix:
        raise ObjectStorePreconditionError("object prefix must not be empty")
    return _validate_key(prefix.rstrip("/")) + "/"


def object_store_uri(bucket: str, key: str) -> str:
    safe_key = quote(_validate_key(key), safe="/")
    return f"s3://{bucket}/{safe_key}"


def parse_object_store_uri(uri: str) -> tuple[str, str]:
    parts = urlsplit(uri or "")
    if parts.scheme != "s3" or not parts.netloc:
        raise ObjectStorePreconditionError(f"unsupported object-store URI: {uri!r}")
    key = unquote(parts.path.lstrip("/")).rstrip("/")
    return parts.netloc, _validate_key(key)


class FakeObjectStore:
    """In-memory object store for offline tests.

    It enforces the same key-safety rule as the real adapter and intentionally
    returns presigned-looking URLs without exposing configured credentials.
    """

    def __init__(self, config: ObjectStoreConfig):
        self.config = config
        self._objects: dict[str, tuple[bytes, ObjectMetadata]] = {}

    async def preflight(self) -> None:
        if not self.config.bucket:
            raise ObjectStoreUnavailableError("object-store bucket is not configured")

    async def head_object(self, key: str) -> ObjectMetadata:
        key = self.config.scoped_key(key)
        try:
            return self._objects[key][1]
        except KeyError as exc:
            raise ObjectNotFoundError(f"object not found: {key}") from exc

    async def get_bytes(self, key: str) -> bytes:
        key = self.config.scoped_key(key)
        try:
            return self._objects[key][0]
        except KeyError as exc:
            raise ObjectNotFoundError(f"object not found: {key}") from exc

    async def put_bytes(
        self,
        key: str,
        data: bytes,
        *,
        content_type: str | None = None,
        metadata: dict[str, str] | None = None,
    ) -> ObjectMetadata:
        key = self.config.scoped_key(key)
        payload = bytes(data)
        sha256 = hashlib.sha256(payload).hexdigest()
        meta = {str(k): str(v) for k, v in (metadata or {}).items()}
        obj_meta = ObjectMetadata(
            key=key,
            size=len(payload),
            content_type=content_type,
            etag=hashlib.md5(payload, usedforsecurity=False).hexdigest(),
            checksum_sha256=sha256,
            metadata=meta,
        )
        self._objects[key] = (payload, obj_meta)
        return obj_meta

    async def delete_object(self, key: str) -> bool:
        key = self.config.scoped_key(key)
        return self._objects.pop(key, None) is not None

    async def list_keys(self, prefix: str) -> list[str]:
        prefix = _validate_prefix(prefix)
        return sorted(key for key in self._objects if key.startswith(prefix))

    async def delete_prefix(self, prefix: str) -> int:
        keys = await self.list_keys(prefix)
        for key in keys:
            self._objects.pop(key, None)
        return len(keys)

    async def presign_upload(
        self,
        key: str,
        *,
        content_type: str,
        size: int,
        checksum_sha256: str | None = None,
        expires_in: int | None = None,
    ) -> PresignedUpload:
        if size < 0:
            raise ObjectStorePreconditionError("object size must be non-negative")
        key = self.config.scoped_key(key)
        ttl = expires_in or self.config.presign_ttl_seconds
        token = base64.urlsafe_b64encode(hashlib.sha256(key.encode()).digest()[:12]).decode().rstrip("=")
        headers = {"Content-Type": content_type, "x-amz-meta-size": str(size)}
        if checksum_sha256:
            headers["x-amz-meta-sha256"] = checksum_sha256
        endpoint = (self.config.endpoint_url or "https://object-store.local").rstrip("/")
        return PresignedUpload(
            method="PUT",
            bucket=self.config.bucket,
            key=key,
            url=f"{endpoint}/{quote(self.config.bucket)}/{quote(key, safe='/')}?upload={token}",
            headers=headers,
            expires_in=ttl,
        )


class S3ObjectStore:
    """S3-compatible object-store adapter using aioboto3 lazily."""

    def __init__(self, config: ObjectStoreConfig):
        self.config = config

    def _client_kwargs(self) -> dict[str, Any]:
        kwargs: dict[str, Any] = {
            "endpoint_url": self.config.endpoint_url,
            "region_name": self.config.region,
            "aws_access_key_id": self.config.access_key_id,
            "aws_secret_access_key": self.config.secret_access_key,
            "aws_session_token": self.config.session_token,
        }
        kwargs = {k: v for k, v in kwargs.items() if v not in (None, "")}
        if self.config.force_path_style:
            from botocore.config import Config

            kwargs["config"] = Config(s3={"addressing_style": "path"})
        return kwargs

    def _session(self):
        try:
            import aioboto3
        except ImportError as exc:  # pragma: no cover - depends on optional env
            raise ObjectStoreUnavailableError(
                "aioboto3 is required when LIGHTRAG_OBJECT_STORAGE=s3"
            ) from exc
        return aioboto3.Session()

    async def preflight(self) -> None:
        probe_key = "/".join(
            part
            for part in (
                self.config.normalized_prefix(),
                ".preflight",
                f"{int(time.time())}-{os.urandom(4).hex()}.txt",
            )
            if part
        )
        probe_created = False
        try:
            async with self._session().client("s3", **self._client_kwargs()) as client:
                await client.head_bucket(Bucket=self.config.bucket)
                await client.put_object(
                    Bucket=self.config.bucket,
                    Key=probe_key,
                    Body=b"lightrag object-store preflight\n",
                    ContentType="text/plain",
                    Metadata={"purpose": "lightrag-preflight"},
                )
                probe_created = True
                await client.head_object(Bucket=self.config.bucket, Key=probe_key)
                await client.delete_object(Bucket=self.config.bucket, Key=probe_key)
        except Exception as exc:  # pragma: no cover - exercised by integration
            if probe_created:
                try:
                    async with self._session().client(
                        "s3", **self._client_kwargs()
                    ) as cleanup_client:
                        await cleanup_client.delete_object(
                            Bucket=self.config.bucket,
                            Key=probe_key,
                        )
                except Exception:
                    pass
            raise ObjectStoreUnavailableError(
                f"object-store bucket preflight failed for bucket {self.config.bucket!r}"
            ) from exc

    async def head_object(self, key: str) -> ObjectMetadata:
        key = self.config.scoped_key(key)
        try:
            async with self._session().client("s3", **self._client_kwargs()) as client:
                response = await client.head_object(Bucket=self.config.bucket, Key=key)
        except Exception as exc:  # pragma: no cover - exercised by integration
            if _is_not_found_error(exc):
                raise ObjectNotFoundError(f"object not found: {key}") from exc
            raise ObjectStoreUnavailableError(f"failed to read object metadata: {key}") from exc
        return _metadata_from_s3_head(key, response)

    async def get_bytes(self, key: str) -> bytes:
        key = self.config.scoped_key(key)
        try:
            async with self._session().client("s3", **self._client_kwargs()) as client:
                response = await client.get_object(Bucket=self.config.bucket, Key=key)
                async with response["Body"] as body:
                    return await body.read()
        except Exception as exc:  # pragma: no cover - exercised by integration
            if _is_not_found_error(exc):
                raise ObjectNotFoundError(f"object not found: {key}") from exc
            raise ObjectStoreUnavailableError(f"failed to read object: {key}") from exc

    async def put_bytes(
        self,
        key: str,
        data: bytes,
        *,
        content_type: str | None = None,
        metadata: dict[str, str] | None = None,
    ) -> ObjectMetadata:
        key = self.config.scoped_key(key)
        payload = bytes(data)
        args: dict[str, Any] = {"Bucket": self.config.bucket, "Key": key, "Body": payload}
        if content_type:
            args["ContentType"] = content_type
        if metadata:
            args["Metadata"] = {str(k): str(v) for k, v in metadata.items()}
        try:
            async with self._session().client("s3", **self._client_kwargs()) as client:
                await client.put_object(**args)
        except Exception as exc:  # pragma: no cover - exercised by integration
            raise ObjectStoreUnavailableError(f"failed to write object: {key}") from exc
        return await self.head_object(key)

    async def delete_object(self, key: str) -> bool:
        key = self.config.scoped_key(key)
        try:
            async with self._session().client("s3", **self._client_kwargs()) as client:
                await client.delete_object(Bucket=self.config.bucket, Key=key)
        except Exception as exc:  # pragma: no cover - exercised by integration
            raise ObjectStoreUnavailableError(f"failed to delete object: {key}") from exc
        return True

    async def list_keys(self, prefix: str) -> list[str]:
        prefix = _validate_prefix(prefix)
        keys: list[str] = []
        continuation: str | None = None
        try:
            async with self._session().client("s3", **self._client_kwargs()) as client:
                while True:
                    args: dict[str, Any] = {"Bucket": self.config.bucket, "Prefix": prefix}
                    if continuation:
                        args["ContinuationToken"] = continuation
                    response = await client.list_objects_v2(**args)
                    keys.extend(item["Key"] for item in response.get("Contents", []))
                    if not response.get("IsTruncated"):
                        break
                    continuation = response.get("NextContinuationToken")
        except Exception as exc:  # pragma: no cover - exercised by integration
            raise ObjectStoreUnavailableError(f"failed to list object prefix: {prefix}") from exc
        return sorted(keys)

    async def delete_prefix(self, prefix: str) -> int:
        keys = await self.list_keys(prefix)
        if not keys:
            return 0
        try:
            async with self._session().client("s3", **self._client_kwargs()) as client:
                for i in range(0, len(keys), 1000):
                    await client.delete_objects(
                        Bucket=self.config.bucket,
                        Delete={"Objects": [{"Key": key} for key in keys[i : i + 1000]]},
                    )
        except Exception as exc:  # pragma: no cover - exercised by integration
            raise ObjectStoreUnavailableError(f"failed to delete object prefix: {prefix}") from exc
        return len(keys)

    async def presign_upload(
        self,
        key: str,
        *,
        content_type: str,
        size: int,
        checksum_sha256: str | None = None,
        expires_in: int | None = None,
    ) -> PresignedUpload:
        if size < 0:
            raise ObjectStorePreconditionError("object size must be non-negative")
        key = self.config.scoped_key(key)
        ttl = expires_in or self.config.presign_ttl_seconds
        params: dict[str, Any] = {
            "Bucket": self.config.bucket,
            "Key": key,
            "ContentType": content_type,
            "Metadata": {"size": str(size)},
        }
        if checksum_sha256:
            params["Metadata"]["sha256"] = checksum_sha256
        try:
            async with self._session().client("s3", **self._client_kwargs()) as client:
                url = await client.generate_presigned_url(
                    "put_object",
                    Params=params,
                    ExpiresIn=ttl,
                    HttpMethod="PUT",
                )
        except Exception as exc:  # pragma: no cover - exercised by integration
            raise ObjectStoreUnavailableError(f"failed to presign object upload: {key}") from exc
        headers = {"Content-Type": content_type, "x-amz-meta-size": str(size)}
        if checksum_sha256:
            headers["x-amz-meta-sha256"] = checksum_sha256
        return PresignedUpload(
            method="PUT",
            bucket=self.config.bucket,
            key=key,
            url=url,
            headers=headers,
            expires_in=ttl,
        )


def _is_not_found_error(exc: Exception) -> bool:
    code = getattr(getattr(exc, "response", None), "get", lambda *_: {}) ("Error", {}).get("Code")
    return str(code) in {"NoSuchKey", "404", "NotFound"}


def _metadata_from_s3_head(key: str, response: dict[str, Any]) -> ObjectMetadata:
    metadata = {str(k): str(v) for k, v in (response.get("Metadata") or {}).items()}
    checksum = metadata.get("sha256") or metadata.get("checksum_sha256")
    return ObjectMetadata(
        key=key,
        size=int(response.get("ContentLength") or 0),
        content_type=response.get("ContentType"),
        etag=str(response.get("ETag") or "").strip('"') or None,
        checksum_sha256=checksum,
        metadata=metadata,
    )


def build_object_store(config: ObjectStoreConfig | None) -> ObjectStore | None:
    if config is None or config.provider in ("", "disabled", "none"):
        return None
    if config.provider == "fake":
        return FakeObjectStore(config)
    if config.provider == "s3":
        return S3ObjectStore(config)
    raise ObjectStorePreconditionError(f"unsupported object-store provider: {config.provider}")


async def download_to_scratch(
    store: ObjectStore,
    key: str,
    *,
    scratch_dir: str | Path,
    filename: str,
    expected_size: int | None = None,
    checksum_sha256: str | None = None,
) -> Path:
    data = await store.get_bytes(key)
    if expected_size is not None and len(data) != expected_size:
        raise ObjectStorePreconditionError(
            f"object size does not match expected metadata: {key}"
        )
    if checksum_sha256:
        actual_sha256 = hashlib.sha256(data).hexdigest()
        if actual_sha256.lower() != checksum_sha256.lower():
            raise ObjectStorePreconditionError(
                f"object checksum does not match expected metadata: {key}"
            )
    root = Path(scratch_dir)
    root.mkdir(parents=True, exist_ok=True)
    target = root / Path(filename).name
    target.write_bytes(data)
    return target


async def upload_directory(
    store: ObjectStore,
    source_dir: str | Path,
    *,
    target_prefix: str,
) -> int:
    root = Path(source_dir)
    if not root.is_dir():
        return 0
    count = 0
    for path in sorted(root.rglob("*")):
        if not path.is_file():
            continue
        rel = path.relative_to(root).as_posix()
        key = f"{target_prefix.rstrip('/')}/{rel}"
        await store.put_bytes(key, path.read_bytes())
        count += 1
    return count


async def download_prefix_to_scratch(
    store: ObjectStore,
    prefix: str,
    *,
    scratch_dir: str | Path,
) -> Path:
    prefix = _validate_prefix(prefix)
    keys = await store.list_keys(prefix)
    if not keys:
        raise ObjectNotFoundError(f"object prefix not found: {prefix}")
    root = Path(scratch_dir)
    root.mkdir(parents=True, exist_ok=True)
    for key in keys:
        rel = key[len(prefix) :].lstrip("/")
        if not rel:
            continue
        _validate_key(rel)
        target = root / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(await store.get_bytes(key))
    return root


def remove_scratch(path: str | Path) -> None:
    p = Path(path)
    if not p.exists():
        return
    if p.is_dir():
        shutil.rmtree(p)
    else:
        p.unlink()


def new_upload_id() -> str:
    return f"upload_{int(time.time())}_{os.urandom(8).hex()}"
