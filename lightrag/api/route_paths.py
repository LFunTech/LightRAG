"""Pure ASGI route matching without importing authentication configuration."""

from typing import Any, Mapping


def get_route_path(scope: Mapping[str, Any], mount_prefix: str = "") -> str:
    """The route path a matcher must compare against: ``scope["path"]`` minus the
    ASGI mount prefix.

    In canonical ASGI form ``scope["path"]`` *includes* ``root_path``, so a
    request routed to ``/documents`` on a server mounted at ``/site01`` carries
    ``path == "/site01/documents"``. Anything comparing against configured route
    paths (``WHITELIST_PATHS``, ``ADMISSION_PATHS``,
    ``_TOKEN_RENEWAL_SKIP_PATHS``) must subtract the prefix first, or it compares
    two different kinds of path.

    ``root_path`` is authoritative when present: ``FastAPI.__call__`` writes it
    from ``app.root_path`` before the middleware stack runs, so every layer sees
    it. ``mount_prefix`` is the fallback for callers whose scope was not built by
    FastAPI (hand-rolled ASGI scopes in tests, for instance).

    The subtraction is deliberately guarded rather than an unconditional slice,
    because the two callers do not see the same scope shape. The admission
    middleware runs *outside* ``_RootPathNormalizationMiddleware`` (that ordering
    is intentional, so CORS can wrap its refusals), so in a proxy-strip
    deployment it sees a bare ``/documents/upload`` while ``root_path`` is
    already ``/site01``. An unguarded ``path[len(prefix):]`` would silently
    mangle that into ``ments``.

    Mirrors ``starlette._utils.get_route_path`` — inlined rather than imported
    because that module is private — with one documented divergence: an
    exact-prefix hit returns ``"/"`` where Starlette returns ``""``. Configured
    paths are always written with a leading slash, so ``"/"`` is the normal form
    the matchers here expect.
    """
    path = scope.get("path") or ""
    prefix = (scope.get("root_path") or mount_prefix or "").rstrip("/")
    if not prefix or not path.startswith(prefix):
        return path
    remainder = path[len(prefix) :]
    if not remainder:
        return "/"
    if remainder.startswith("/"):
        return remainder
    # Mid-segment collision (prefix ``/api`` against path ``/apikeys``): the
    # prefix is not actually a path-segment prefix here, so nothing is removed.
    return path
