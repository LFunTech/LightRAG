"""Pre-body guard for deployments that forbid local file ingestion.

``/documents/upload`` normally parses a multipart body before the FastAPI route
can decide anything, and ``/documents/scan`` consumes the local ``INPUT_DIR``.
Deployments that are object-store-only need the local scan closed, and they need
``/documents/upload`` closed before reading the body unless the S3-backed
official upload path is available. The route handlers keep an in-process guard
for tests and embedded apps, while this ASGI middleware avoids reading an upload
body at all when it cannot be accepted safely.
"""

from __future__ import annotations

from typing import Any

from .asgi_helpers import send_json
from .utils_api import get_route_path

LOCAL_FILE_INGESTION_DISABLED_DETAIL = (
    "Local file ingestion is disabled. Configure object-store ingestion for "
    "official /documents/upload support, or use /documents/uploads/presign and "
    "/documents/uploads/complete when object-store ingestion is available."
)

LOCAL_FILE_INGESTION_PATHS: tuple[str, ...] = (
    "/documents/upload",
    "/documents/scan",
)


class LocalFileIngestionMiddleware:
    """Reject local file ingestion endpoints before the request body is read."""

    def __init__(
        self,
        app,
        *,
        enabled: bool = True,
        object_upload_available: bool = False,
        api_prefix: str = "",
    ) -> None:
        self.app = app
        self._enabled = bool(enabled)
        self._object_upload_available = bool(object_upload_available)
        self._api_prefix = (api_prefix or "").rstrip("/")

    def _is_local_file_ingestion_path(self, scope: dict[str, Any]) -> bool:
        if scope.get("method") != "POST":
            return False
        route_path = get_route_path(scope, self._api_prefix)
        if route_path == "/documents/upload" and self._object_upload_available:
            return False
        return route_path in LOCAL_FILE_INGESTION_PATHS

    async def __call__(self, scope, receive, send) -> None:
        if (
            scope["type"] == "http"
            and not self._enabled
            and self._is_local_file_ingestion_path(scope)
        ):
            await send_json(send, 403, LOCAL_FILE_INGESTION_DISABLED_DETAIL)
            return

        await self.app(scope, receive, send)
