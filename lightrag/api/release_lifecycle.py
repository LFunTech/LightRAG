"""ASGI release admission and full-response lifetime; never buffers read streams."""

from starlette.responses import JSONResponse

from lightrag.distributed.coordinator import CoordinationError
from lightrag.distributed.release.models import QueueFull, ReleaseConflict
from lightrag.distributed.release.query import (
    _streams,
    provider_stream_context,
    close_request_streams,
)
from .release_commands import WRITE_ROUTES, PUBLIC_ROUTES
from .route_paths import get_route_path


def create_release_uvicorn(app, options):
    import uvicorn

    server = uvicorn.Server(uvicorn.Config(**options, timeout_graceful_shutdown=None))
    release = app.state.rag._distributed_runtime.release

    def request_shutdown():
        server.should_exit = True

    release.shutdown_hook = request_shutdown
    return server


def run_release_uvicorn(app, options):
    create_release_uvicorn(app, options).run()


class ReleaseMiddleware:
    def __init__(self, app, *, bridge):
        self.app = app
        self.bridge = bridge

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or self.bridge.replaying():
            return await self.app(scope, receive, send)
        release = self.bridge.release
        path = get_route_path(scope)
        route = self.bridge.match(path, scope["method"])
        if path in {"/health", "/ready", "/release/status"} or path.startswith(
            "/release/commands/"
        ):
            return await self.app(scope, receive, send)
        bag = set()
        token = _streams.set(bag)
        response_started = False

        async def forward(message):
            nonlocal response_started
            if message["type"] == "http.response.start":
                response_started = True
            await send(message)

        try:
            async with release.lifecycle.track("http"):
                if route is not None and route.name in WRITE_ROUTES:
                    response = await self.bridge.submit(scope, receive, path, route)
                    await response(scope, receive, forward)
                elif route is not None and route.name not in PUBLIC_ROUTES:
                    with provider_stream_context():
                        async with release.runtime.operation("release_read"):
                            try:
                                await self.app(scope, receive, forward)
                            finally:
                                from anyio import CancelScope

                                with CancelScope(shield=True):
                                    await close_request_streams(bag)
                else:
                    await self.app(scope, receive, forward)
        except CoordinationError as error:
            if response_started:
                raise
            status = (
                429
                if isinstance(error, QueueFull)
                else 409
                if isinstance(error, ReleaseConflict)
                else 503
            )
            await JSONResponse(
                {"detail": {"error": type(error).__name__}},
                status_code=status,
                headers={"Retry-After": "1"} if status == 429 else None,
            )(scope, receive, send)
        finally:
            _streams.reset(token)
