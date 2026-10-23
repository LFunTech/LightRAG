"""Streaming lifetime ownership without transferring ContextVar reset tokens."""

import asyncio
from contextlib import contextmanager
from contextvars import ContextVar


_provider_lifetime = ContextVar("release_provider_lifetime", default=False)
_streams = ContextVar("release_request_streams", default=None)


@contextmanager
def provider_stream_context():
    token = _provider_lifetime.set(True)
    try:
        yield
    finally:
        _provider_lifetime.reset(token)


class OwnedProviderStream:
    """Own already-open upstream resources even before generator consumption."""

    def __init__(self, iterator, closer):
        self.iterator = iterator.__aiter__()
        self.closer = closer
        self.closed = False
        self.lock = asyncio.Lock()
        from lightrag.distributed.runtime import current_runtime

        runtime = current_runtime()
        self.release = getattr(runtime, "release", None)
        if self.release is not None:
            self.release.lifecycle.counts["backend"] += 1
            self.release.lifecycle.changed.set()

    def __aiter__(self):
        return self

    async def __anext__(self):
        if self.closed:
            raise StopAsyncIteration
        try:
            return await self.iterator.__anext__()
        except BaseException:
            await self.aclose()
            raise

    async def aclose(self):
        async with self.lock:
            if self.closed:
                return
            try:
                try:
                    close = getattr(self.iterator, "aclose", None)
                    if close is not None:
                        await close()
                finally:
                    await self.closer()
            except BaseException:
                if self.release is not None:
                    self.release.controller_error = "ProviderCloseUncertain"
                raise
            self.closed = True
            if self.release is not None:
                self.release.lifecycle.counts["backend"] -= 1
                self.release.lifecycle.changed.set()


def own_provider_stream(iterator, closer):
    return (
        OwnedProviderStream(iterator, closer) if _provider_lifetime.get() else iterator
    )


class ProviderStreamLease:
    def __init__(self, iterator):
        self.iterator = iterator.__aiter__()
        self.closed = asyncio.Event()
        self.iteration = asyncio.Lock()

    def __aiter__(self):
        return self

    async def __anext__(self):
        async with self.iteration:
            if self.closed.is_set():
                raise StopAsyncIteration
            try:
                return await self.iterator.__anext__()
            except BaseException:
                await self._close()
                raise

    async def _close(self):
        if self.closed.is_set():
            return
        close = getattr(self.iterator, "aclose", None)
        if close is not None:
            await close()
        self.closed.set()

    async def aclose(self):
        # An HTTP disconnect does not cancel an unknown in-flight provider
        # read. Wait for its owning iteration before closing the resource.
        async with self.iteration:
            await self._close()


async def close_request_streams(streams):
    failures = []
    # Managed owners must finish their shielded anext before provider leases
    # are closed; set iteration order is not a lifecycle protocol.
    ordered = sorted(
        list(streams),
        key=lambda value: 0 if isinstance(value, ManagedQueryStream) else 1,
    )
    for stream in ordered:
        try:
            await stream.aclose()
        except BaseException as error:
            failures.append(error)
    if failures:
        raise failures[0]


def prepare_provider_stream(result):
    if _provider_lifetime.get() and hasattr(result, "__aiter__"):
        stream = ProviderStreamLease(result)
        bag = _streams.get()
        if bag is not None:
            bag.add(stream)
        return stream
    return result


class ManagedQueryStream:
    """Consume/close in the owning Task; SDK callers must exhaust or close it."""

    def __init__(self, queue):
        self.queue = queue
        self.closed = False
        self.pending = None
        self.close_task = None

    def __aiter__(self):
        return self

    async def __anext__(self):
        if self.closed:
            raise StopAsyncIteration
        if self.pending is not None and not self.pending.done():
            raise RuntimeError("Concurrent stream consumption is not supported")
        future = asyncio.get_running_loop().create_future()
        self.pending = future
        await self.queue.put(("next", future))
        try:
            return await asyncio.shield(future)
        except asyncio.CancelledError:
            if self.close_task is None:
                self.close_task = asyncio.create_task(self._close())
            raise
        except BaseException:
            self.closed = True
            raise

    async def _close(self):
        if self.pending is not None:
            try:
                await asyncio.shield(self.pending)
            except StopAsyncIteration:
                self.closed = True
                return
            except Exception:
                self.closed = True
                raise
        if self.closed:
            return
        future = asyncio.get_running_loop().create_future()
        await self.queue.put(("close", future))
        try:
            await asyncio.shield(future)
        finally:
            self.closed = True

    async def aclose(self):
        if self.close_task is None:
            self.close_task = asyncio.create_task(self._close())
        await asyncio.shield(self.close_task)


def _response_iterator(result):
    if hasattr(result, "__aiter__"):
        return result, lambda value: value
    if isinstance(result, dict):
        response = result.get("llm_response")
        if isinstance(response, dict) and hasattr(
            response.get("response_iterator"), "__aiter__"
        ):

            def replace(iterator):
                return result | {
                    "llm_response": response | {"response_iterator": iterator}
                }

            return response["response_iterator"], replace
    return None, None


async def run_query(invoke, permit):
    ready = asyncio.get_running_loop().create_future()
    queue = asyncio.Queue()

    async def owner():
        pending = None
        terminal = None
        result = None
        iterator = None
        try:
            async with permit():
                with provider_stream_context():
                    result = await invoke()
                    iterator, replace = _response_iterator(result)
                    if iterator is not None:
                        iterator = iterator.__aiter__()
                        managed = ManagedQueryStream(queue)
                        bag = _streams.get()
                        if bag is not None:
                            bag.add(managed)
                        ready.set_result(replace(managed))
                        try:
                            while True:
                                action, pending = await queue.get()
                                if action == "close":
                                    break
                                try:
                                    item = await iterator.__anext__()
                                except StopAsyncIteration as error:
                                    terminal = error
                                    break
                                if not pending.done():
                                    pending.set_result(item)
                                pending = None
                        finally:
                            close = getattr(iterator, "aclose", None)
                            if close is not None:
                                await close()
            if not ready.done():
                ready.set_result(result)
            elif pending is not None and not pending.done():
                if terminal is not None:
                    pending.set_exception(terminal)
                else:
                    pending.set_result(None)
        except BaseException as error:
            if not ready.done():
                ready.set_exception(error)
            elif pending is not None and not pending.done():
                pending.set_exception(error)

    task = asyncio.create_task(owner())
    # The task owns the permit until consumption/close. Retain it on the stream
    # rather than letting a caller's cancellation cancel unknown backend I/O.
    try:
        result = await asyncio.shield(ready)
    except asyncio.CancelledError:

        async def close_when_ready():
            try:
                value = await ready
                iterator, _ = _response_iterator(value)
                if iterator is not None:
                    await iterator.aclose()
            except Exception:
                pass

        asyncio.create_task(close_when_ready())
        raise
    iterator, _ = _response_iterator(result)
    if iterator is not None:
        iterator.owner_task = task
    else:
        await task
    return result
