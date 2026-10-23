"""Opt-in query usage authority; never infer usage or release unknown charges.

The authority owns durable admission, call reservations and receipts. This module
holds only request-local bounded metadata. See docs/design/QueryAccounting.md.
"""
from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from functools import lru_cache
import hashlib
import json
import math
import ssl
import time
from typing import Any, Awaitable, Callable, Protocol


class QueryAccountingError(RuntimeError):
    """A safe accounting failure, not a retryable provider error."""


class QueryUsageAuthority(Protocol):
    async def claim(self, query_id: str, grant: str, request_digest: str, workspace: str) -> dict: ...
    async def reserve_call(self, lease_id: str, call_id: str, **fields: Any) -> None: ...
    async def settle_call(self, lease_id: str, call_id: str, usage: dict | None) -> None: ...
    async def finish(self, lease_id: str, status: str) -> dict: ...


_active: ContextVar[QueryUsageContext | None] = ContextVar("query_usage", default=None)


def retrieval_digest(query: str, mode: str) -> str:
    return hashlib.sha256(json.dumps(
        ["retrieval-only-v1", query.strip(), mode], ensure_ascii=False,
        separators=(",", ":"),
    ).encode()).hexdigest()


async def _finish_owned(work: Awaitable) -> Any:
    task = asyncio.ensure_future(work)
    cancelled = False
    while not task.done():
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            cancelled = True
    result = task.result()
    if cancelled:
        raise asyncio.CancelledError()
    return result


@dataclass
class QueryUsageContext:
    authority: QueryUsageAuthority
    quote: dict
    count: int = 0
    closed: bool = False
    failed: bool = False
    receipt: dict | None = None
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)

    def limits(self, kind: str, model: str, origin: str) -> dict:
        if self.closed or self.failed or time.time() >= self.quote['deadline']:
            raise QueryAccountingError('query_usage_closed_or_unknown')
        matches = [m for m in self.quote['models'] if
                   (m['kind'], m['model'], m['origin'].rstrip('/')) == (kind, model, origin.rstrip('/'))]
        if len(matches) != 1:
            raise QueryAccountingError('query_usage_model_not_quoted')
        return matches[0]


def _validate_quote(quote: dict, query_id: str) -> None:
    def require(condition: bool) -> None:
        if not condition:
            raise ValueError('query_usage_quote_invalid')

    try:
        require(quote['query_id'] == query_id and isinstance(quote['lease_id'], str))
        require(0 < len(quote['lease_id']) <= 255)
        require(type(quote['max_calls']) is int and 1 <= quote['max_calls'] <= 16)
        require(type(quote['deadline']) in (int, float) and math.isfinite(quote['deadline']))
        require(quote['deadline'] > time.time())
        require(isinstance(quote['models'], list) and 1 <= len(quote['models']) <= 16)
        seen = set()
        for m in quote['models']:
            key = (m['kind'], m['model'], m['origin'])
            require(key not in seen and m['kind'] in ('llm', 'embedding'))
            seen.add(key)
            require(all(isinstance(m[k], str) and 0 < len(m[k]) <= 255 for k in ('model', 'origin')))
            require(type(m['input_ceiling']) is int and 0 < m['input_ceiling'] <= 1_048_576)
            require(type(m['output_ceiling']) is int and 0 <= m['output_ceiling'] <= 32768)
            require((m['output_ceiling'] == 0) == (m['kind'] == 'embedding'))
    except (KeyError, TypeError, ValueError):
        raise QueryAccountingError('query_usage_quote_invalid') from None


@asynccontextmanager
async def query_usage_scope(authority: QueryUsageAuthority, *, query_id: str, grant: str,
                            request_digest: str, workspace: str):
    quote = await authority.claim(query_id, grant, request_digest, workspace)
    _validate_quote(quote, query_id)
    scope = QueryUsageContext(authority, quote)
    token = _active.set(scope)
    status = 'completed'
    try:
        yield scope
    except asyncio.CancelledError:
        status = 'cancelled'
        raise
    except BaseException:
        status = 'failed'
        raise
    finally:
        scope.closed = True
        try:
            scope.receipt = await _finish_owned(authority.finish(
                quote['lease_id'], 'unknown' if scope.failed and status == 'completed' else status))
        finally:
            _active.reset(token)


@lru_cache(maxsize=1)
def _strict_tls_context() -> ssl.SSLContext:
    # Match httpx's no-environment public trust roots; share only immutable trust,
    # never clients, credentials, reservations or authorization between calls.
    import certifi
    return ssl.create_default_context(cafile=certifi.where())


def prewarm_openai_accounting() -> ssl.SSLContext:
    """Load lazy SDK resources and public trust before an opted-in host is ready."""
    import importlib
    importlib.import_module('openai.resources.chat.completions')
    importlib.import_module('openai.resources.embeddings')
    return _strict_tls_context()


class OpenAIAccountingTransport:
    """A host-owned stateless pool; request clients still own headers and cookies."""
    def __init__(self, *, max_connections: int, keepalive_expiry: float = 5):
        if type(max_connections) is not int or not 1 <= max_connections <= 64:
            raise ValueError('bounded accounting transport required')
        if (type(keepalive_expiry) not in (int, float)
                or not 0 < keepalive_expiry <= 60):
            raise ValueError('bounded host transport options required')
        import httpx
        self._closed = False
        self._close_task = None
        self._slots = asyncio.Semaphore(max_connections)
        self._transport = httpx.AsyncHTTPTransport(verify=_strict_tls_context(),
            trust_env=False, retries=0, limits=httpx.Limits(
                max_connections=max_connections, max_keepalive_connections=max_connections,
                keepalive_expiry=keepalive_expiry))

    def borrow(self):
        if self._closed:
            raise QueryAccountingError('query_usage_transport_closed')
        import httpx
        owner = self
        class BorrowedTransport(httpx.AsyncBaseTransport):
            async def handle_async_request(self, request):
                if owner._closed:
                    raise QueryAccountingError('query_usage_transport_closed')
                return await owner._transport.handle_async_request(request)

            async def aclose(self):
                # The host closes the pool after request/worker drain. A cancelled
                # query must not close other queries' connections or liability.
                pass
        return BorrowedTransport()

    @asynccontextmanager
    async def slot(self, deadline):
        acquired = False
        try:
            async with asyncio.timeout(max(0, deadline - time.time())):
                await self._slots.acquire()
                acquired = True
            if self._closed:
                raise QueryAccountingError('query_usage_transport_closed')
            yield
        finally:
            if acquired:
                self._slots.release()

    async def aclose(self):
        if self._close_task is None:
            self._closed = True
            self._close_task = asyncio.create_task(self._transport.aclose())
        await _finish_owned(self._close_task)


async def _guard_provider_wire(request):
    if _active.get() is None:
        raise QueryAccountingError('query_usage_missing_wire_scope')
    original = request.extensions.get('trace')
    started = False

    async def trace(event, info):
        nonlocal started
        if event in ('http11.send_request_headers.started', 'http2.send_request_headers.started'):
            if started:
                raise QueryAccountingError('query_usage_hidden_wire_replay')
            started = True
        if original is not None:
            await original(event, info)

    request.extensions['trace'] = trace


def openai_client_configs(configs: dict | None, timeout: float | None = None) -> dict | None:
    """Strict queries own one no-retry/no-redirect client per actual attempt."""
    scope = _active.get()
    if scope is None:
        return configs
    if configs and (any(configs.get(k) for k in ('http_client', 'default_headers', 'default_query'))
                    or configs.get('max_retries', 0) != 0):
        raise QueryAccountingError('query_usage_hidden_transport_attempts')
    import httpx
    remaining = scope.quote['deadline'] - time.time()
    if remaining <= 0 or scope.closed or scope.failed:
        raise QueryAccountingError('query_usage_closed_or_unknown')
    owner = getattr(scope.authority, 'openai_accounting_transport', None)
    if owner is not None and not isinstance(owner, OpenAIAccountingTransport):
        raise QueryAccountingError('query_usage_untrusted_transport')
    transport = {'transport': owner.borrow()} if owner is not None else {}
    result = dict(configs or {})
    result.update(max_retries=0, http_client=httpx.AsyncClient(
        verify=_strict_tls_context(), follow_redirects=False, trust_env=False,
        event_hooks={'request': [_guard_provider_wire]},
        timeout=min(timeout or 60, remaining), **transport))
    return result


def _raw_usage(response: Any, kind: str, expected_model: str) -> dict | None:
    usage = getattr(response, 'usage', None)
    if usage is None or getattr(response, 'model', None) != expected_model:
        return None
    counts = {'prompt_tokens': getattr(usage, 'prompt_tokens', None),
              'completion_tokens': 0 if kind == 'embedding' else getattr(usage, 'completion_tokens', None),
              'total_tokens': getattr(usage, 'total_tokens', None)}
    if not all(type(v) is int and v >= 0 for v in counts.values()):
        return None
    if counts['total_tokens'] != counts['prompt_tokens'] + counts['completion_tokens']:
        return None
    return {**counts, 'model': expected_model, 'billing_usage_complete': True, 'estimated': False}


async def account_openai_call(kind: str, client: Any, payload: dict,
                              send: Callable[[], Awaitable]) -> Any:
    scope = _active.get()
    if scope is None:
        return await send()
    owner = getattr(scope.authority, 'openai_accounting_transport', None)
    if owner is not None:
        if not isinstance(owner, OpenAIAccountingTransport):
            raise QueryAccountingError('query_usage_untrusted_transport')
        scope.limits(kind, payload.get('model'), str(client.base_url).rstrip('/'))
        # Capacity wait precedes fresh authority/reservation. After waiting the
        # existing scope, deadline and post-reservation checks run unchanged.
        async with owner.slot(scope.quote['deadline']):
            return await _account_scoped_openai_call(scope, kind, client, payload, send)
    return await _account_scoped_openai_call(scope, kind, client, payload, send)


async def _account_scoped_openai_call(scope, kind, client, payload, send):
    model, origin = payload.get('model'), str(client.base_url).rstrip('/')
    limits = scope.limits(kind, model, origin)
    if client.max_retries != 0 or payload.get('stream'):
        raise QueryAccountingError('query_usage_hidden_transport_attempts')
    if any(payload.get(k) for k in ('extra_body', 'extra_headers', 'extra_query', 'max_completion_tokens')):
        raise QueryAccountingError('query_usage_unquoted_wire_overrides')
    if kind == 'llm':
        if type(payload.get('n', 1)) is not int or payload.get('n', 1) != 1:
            raise QueryAccountingError('query_usage_choice_limit')
        output = payload.setdefault('max_tokens', limits['output_ceiling'])
        if type(output) is not int or not 0 < output <= limits['output_ceiling']:
            raise QueryAccountingError('query_usage_output_limit')
    else:
        output = 0
    try:
        size = len(json.dumps(payload, ensure_ascii=False, separators=(',', ':')).encode())
    except (TypeError, ValueError):
        raise QueryAccountingError('query_usage_request_unbounded') from None
    if size > limits['input_ceiling']:
        raise QueryAccountingError('query_usage_input_limit')
    async with scope.lock:
        scope.limits(kind, model, origin)
        if scope.count >= scope.quote['max_calls']:
            raise QueryAccountingError('query_usage_call_limit')
        scope.count += 1
        call_id = hashlib.sha256(f"{scope.quote['query_id']}:{scope.count}".encode()).hexdigest()
        try:
            await scope.authority.reserve_call(scope.quote['lease_id'], call_id, kind=kind,
                model=model, origin=origin, input_bytes=size, output_ceiling=output)
        except BaseException:
            scope.failed = True
            raise
    if scope.closed or scope.failed or time.time() >= scope.quote['deadline']:
        scope.failed = True
        await _finish_owned(scope.authority.settle_call(scope.quote['lease_id'], call_id, None))
        raise QueryAccountingError('query_usage_closed_before_send')
    try:
        response = await send()
    except BaseException as error:
        scope.failed = True
        try:
            await _finish_owned(scope.authority.settle_call(scope.quote['lease_id'], call_id, None))
        finally:
            if isinstance(error, (asyncio.CancelledError, GeneratorExit)) and hasattr(client, 'close'):
                await _finish_owned(client.close())
        if isinstance(error, (asyncio.CancelledError, GeneratorExit)):
            raise
        raise QueryAccountingError('query_usage_provider_outcome_unknown') from error
    usage = _raw_usage(response, kind, model)
    try:
        await _finish_owned(scope.authority.settle_call(scope.quote['lease_id'], call_id, usage))
    except BaseException:
        scope.failed = True
        raise
    if usage is None:
        scope.failed = True
        raise QueryAccountingError('query_usage_provider_proof_missing')
    return response
