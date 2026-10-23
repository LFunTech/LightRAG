"""Opt-in SDK admission, command consumption and instance-local ownership."""

import asyncio
from contextlib import asynccontextmanager, contextmanager
from contextvars import Context, ContextVar
from dataclasses import asdict, dataclass, is_dataclass
from functools import wraps
import inspect
import time

from lightrag.distributed.coordinator import (
    CoordinationBusyError,
    OperationOwnershipError,
)
from .admission import RuntimeStore
from .commands import CommandEnvelope, CommandRegistry, canonical
from .executor import execute_command
from .lifecycle import InstanceLifecycle
from .models import ReleaseError, ReleaseUnavailable
from .query import run_query


READ_OPERATIONS = {"aquery", "aquery_data", "aquery_llm", "release_read"}
INITIAL_OPERATIONS = {"initialize", "initialize_upload_sessions"}
SDK_WRITES = {
    "ainsert",
    "ainsert_custom_chunks",
    "ainsert_custom_kg",
    "aclear_cache",
    "adelete_by_doc_id",
    "adelete_by_entity",
    "adelete_by_relation",
    "aedit_entity",
    "aedit_relation",
    "acreate_entity",
    "acreate_relation",
    "amerge_entities",
    "apipeline_enqueue_documents",
    "apipeline_enqueue_error_documents",
    "apipeline_request_retry",
    "apipeline_process_enqueue_documents",
    "apipeline_reset_failed_for_scan",
}
BACKGROUND_OPERATIONS = {
    "pipeline",
    "manual_retry",
    "pipeline_index_file",
    "pipeline_index_texts",
    "run_scanning_process",
    "background_delete_documents",
    "scan_processing",
}
_request = ContextVar("release_sdk_request", default=None)
_ticket = ContextVar("release_writer_ticket", default=None)
_executing_record = ContextVar("release_executing_record", default=None)


@dataclass
class Ticket:
    runtime: object
    id: object
    epoch: int
    active: bool = True
    operation_id: object = None


@contextmanager
def request_context(*, service_id, idempotency_key):
    if (
        not isinstance(service_id, str)
        or not service_id
        or not isinstance(idempotency_key, str)
        or not idempotency_key
    ):
        raise ValueError("Explicit SDK service and idempotency identity required")
    token = _request.set((service_id, idempotency_key))
    try:
        yield
    finally:
        _request.reset(token)


def release_sdk_guard(function):
    """Add release-only queuing to SDK control methods without changing test."""

    @wraps(function)
    async def guarded(self, *args, **kwargs):
        from lightrag.distributed.runtime import get_runtime

        runtime = get_runtime(self)
        release = getattr(runtime, "release", None)
        if release is not None and not release.has_ticket():
            return await release.submit_sdk(function, self, args, kwargs)
        return await function(self, *args, **kwargs)

    return guarded


class ReleaseRuntime:
    def __init__(self, runtime, rag, config):
        self.runtime = runtime
        self.rag = rag
        self.config = config
        self.store = None
        self.scope_state = None
        self.registry = CommandRegistry()
        self.lifecycle = InstanceLifecycle()
        self.controller_error = None
        self.executor_error = None
        self.controller_task = None
        self.executor_task = None
        self.stop_event = asyncio.Event()
        self.http_bridge = None
        self.shutdown_hook = None
        self.shutdown_requested = False
        self.polling_desired = False
        self.polling_interval = 1.0
        self.closed = False
        self.registered = False
        self.cache_skipped = 0
        for name in sorted(SDK_WRITES):
            if hasattr(rag, name):
                self.registry.register(
                    "sdk." + name, version=1, handler=self._sdk_handler(name)
                )

    def has_ticket(self):
        value = _ticket.get()
        return value is not None and value.runtime is self and value.active

    async def initialize(self):
        if self.registered:
            return
        self.store = RuntimeStore(
            self.runtime.coordinator._pool,
            scope=self.config.scope,
            instance_id=self.config.instance_id,
        )
        await self.store.verify_schema()
        if getattr(self.runtime, "release_require_privileges", False):
            from .migrations import verify_runtime_role

            async with self.store.connection() as connection:
                await verify_runtime_role(connection)
        await self.store.register(
            slot=self.config.slot,
            config_hash=self.config.config_hash,
            profile_hash=self.config.profile_hash,
            coordinator_owner=self.runtime.coordinator._owner_id,
        )
        self.runtime.coordinator._release_runtime = self
        await self.refresh()
        self.registered = True

    async def refresh(self):
        async with self.store.connection() as connection:
            self.scope_state = await self.store._scope(connection, self.config.scope)
        return self.scope_state

    async def probe(self):
        await self.refresh()
        coordinator = self.runtime.coordinator
        async with coordinator._pool.acquire() as connection:
            async with connection.transaction(readonly=True):
                row = await connection.fetchrow(
                    "SELECT fenced FROM lightrag_coordination.workspaces WHERE deployment_id=$1 AND workspace=$2",
                    *coordinator._scope,
                )
                if (
                    row is None
                    or row["fenced"]
                    or await coordinator._orphans(connection)
                ):
                    raise ReleaseUnavailable(
                        "Workspace is fenced or has an orphaned operation"
                    )
        snapshot = await self.store.snapshot(self.config.scope)
        if snapshot["commands"].get("UNCERTAIN"):
            raise ReleaseUnavailable(
                "Uncertain release command requires operator audit"
            )

    def _sdk_handler(self, name):
        async def handle(payload):
            result = await getattr(self.rag, name)(**payload["kwargs"])
            if is_dataclass(result):
                from lightrag.base import DeletionResult

                if isinstance(result, DeletionResult):
                    return {"type": "DeletionResult", "value": asdict(result)}
                raise ReleaseError("SDK result type is not registered")
            canonical(result)
            return {"type": "json", "value": result}

        return handle

    async def authorize(self, principal, operation, scope):
        await self.refresh()
        if scope != self.config.scope:
            return False
        if operation.startswith("sdk."):
            return principal.get("scheme") == "service" and principal.get(
                "id"
            ) in self.scope_state["policy"].get("writers", [])
        return (
            self.http_bridge is not None
            and await self.http_bridge.authorization.authorize(
                principal, operation, scope
            )
        )

    async def submit_sdk(self, function, obj, args, kwargs):
        request = _request.get()
        if request is None or function.__name__ not in SDK_WRITES:
            raise ReleaseError(
                "Registered SDK request_context is required before production writes"
            )
        arguments = inspect.signature(function).bind(obj, *args, **kwargs)
        arguments.apply_defaults()
        payload = {
            key: value for key, value in arguments.arguments.items() if key != "self"
        }
        service, key = request
        envelope = self.registry.encode(
            "sdk." + function.__name__,
            {"kwargs": payload},
            principal={"id": service, "scheme": "service"},
            scope=self.config.scope,
            key=key,
        )
        await self.start()
        async with self.lifecycle.track("waiters"):
            receipt = await self.store.enqueue(envelope)
            row = await self.store.wait_result(
                receipt,
                principal_id=service,
                deadline=time.monotonic()
                + self.scope_state["policy"]["request_wait_seconds"],
            )
        if row["state"] != "SUCCEEDED":
            raise ReleaseError(
                "Queued SDK command "
                + row["state"]
                + "; command id "
                + str(receipt["id"])
            )
        result = row["result"]
        if result["type"] == "DeletionResult":
            from lightrag.base import DeletionResult

            return DeletionResult(**result["value"])
        return result["value"]

    @asynccontextmanager
    async def use_command(self, command):
        value = Ticket(self, command["ticket_id"], command["epoch"])
        token = _ticket.set(value)
        record_token = _executing_record.set(command)
        try:
            yield
        finally:
            _executing_record.reset(record_token)
            value.active = False
            _ticket.reset(token)

    async def accepted_before_expiry(self, upload_id, expires):
        command = _executing_record.get()
        return bool(
            self.has_ticket()
            and command is not None
            and command["created_at"] <= expires
            and "session:" + upload_id in command["envelope"].get("source_refs", [])
        )

    @asynccontextmanager
    async def source_cleanup(self, upload_id):
        await self.require_writer()
        ticket = _ticket.get()
        ref = "session:" + upload_id
        allowed = await self.store._call(
            "claim_source_cleanup",
            self.config.scope,
            self.config.instance_id,
            ticket.id,
            ref,
        )
        yield allowed
        if allowed:
            await self.store._call(
                "finish_source_cleanup",
                self.config.scope,
                self.config.instance_id,
                ticket.id,
                ref,
            )

    async def start(self, *, polling=False, interval=1.0):
        await self.initialize()
        if polling and self.http_bridge is not None and self.shutdown_hook is None:
            raise ReleaseError(
                "Release API requires the managed single-process server shutdown hook"
            )
        if self.closed:
            raise ReleaseError("Release runtime is closed")
        self.polling_desired = self.polling_desired or polling
        self.polling_interval = interval
        if self.executor_task is None:
            self.stop_event.clear()
            self.executor_task = Context().run(asyncio.create_task, self._consume())
            self.controller_task = Context().run(asyncio.create_task, self._control())

    async def _pause(self):
        try:
            await asyncio.wait_for(self.stop_event.wait(), 0.1)
        except asyncio.TimeoutError:
            pass

    async def _consume(self):
        try:
            while not self.stop_event.is_set():
                await self.refresh()
                command = await self.store.claim_next(epoch=self.scope_state["epoch"])
                if command is None:
                    await self._pause()
                    continue
                envelope = CommandEnvelope(**command["envelope"])
                async with self.lifecycle.track("writers"):
                    async with self.use_command(command):
                        if envelope.operation.startswith("http."):
                            original = self.registry.handler(envelope)

                            async def execute_http(payload):
                                return await self.http_bridge.execute(
                                    envelope, original
                                )

                            class BoundRegistry:
                                def handler(self, value):
                                    return execute_http

                            registry = BoundRegistry()
                        else:
                            registry = self.registry
                        result = await execute_command(
                            registry, envelope, authorize=self.authorize
                        )
                    await self.store.finish_command(
                        command["id"], status=result["status"], result=result["result"]
                    )
                if result["status"] == "UNCERTAIN":
                    self.executor_error = "UncertainCommand"
                    return
        except BaseException as error:
            self.executor_error = type(error).__name__
            if isinstance(error, asyncio.CancelledError):
                raise

    async def can_claim_batch(self):
        await self.refresh()
        return (
            self.scope_state["active_slot"] == self.config.slot
            and self.scope_state["gate_open"]
            and not self.lifecycle.retiring
            and not self.executor_error
            and not self.controller_error
        )

    async def _control(self):
        from lightrag.distributed.pipeline import _start_polling, _stop_polling

        try:
            while not self.stop_event.is_set():
                await self.refresh()
                async with self.store.connection() as connection:
                    retiring = await connection.fetchval(
                        "SELECT retiring FROM lightrag_release.instances WHERE scope=$1 AND instance_id=$2",
                        self.config.scope,
                        self.config.instance_id,
                    )
                if retiring:
                    self.lifecycle.begin_retire()
                    if self.shutdown_hook is not None and not self.shutdown_requested:
                        self.shutdown_requested = True
                        self.shutdown_hook()
                active = (
                    self.scope_state["active_slot"] == self.config.slot
                    and self.scope_state["gate_open"]
                    and not self.lifecycle.retiring
                )
                if self.executor_error:
                    active = False
                if self.polling_desired and active:
                    task = getattr(self.runtime, "pipeline_task", None)
                    if (
                        task is not None
                        and task.done()
                        and getattr(self.runtime, "pipeline_error", None)
                    ):
                        raise ReleaseUnavailable("Polling stopped unexpectedly")
                    await _start_polling(self.rag, interval=self.polling_interval)
                else:
                    await _stop_polling(self.rag)
                await self._pause()
        except BaseException as error:
            self.controller_error = type(error).__name__
            if isinstance(error, asyncio.CancelledError):
                raise

    @asynccontextmanager
    async def operation(self, kind, *, maintenance=False, detached=False):
        if maintenance:
            raise ReleaseError(
                "Maintenance must be separately provisioned before release activation"
            )
        if kind in INITIAL_OPERATIONS:
            yield
            return
        if kind in READ_OPERATIONS:
            if self.lifecycle.retiring:
                raise ReleaseUnavailable("Instance is retiring")
            async with self.lifecycle.track("queries"):
                yield
            return
        parent = _ticket.get()
        own = False
        if (
            parent is not None
            and parent.runtime is self
            and parent.active
            and parent.operation_id is None
            and not detached
        ):
            value = parent
        else:
            if kind not in BACKGROUND_OPERATIONS and not self.has_ticket():
                raise ReleaseError(
                    "Unregistered writer cannot bypass release admission"
                )
            await self.refresh()
            identifier = await self.store.admit_writer(
                epoch=self.scope_state["epoch"],
                kind=kind,
                command_id=(_executing_record.get() or {}).get("id"),
                parent_ticket=parent.id if self.has_ticket() else None,
            )
            if identifier is None:
                raise CoordinationBusyError("Release write gate is closed")
            value = Ticket(self, identifier, self.scope_state["epoch"])
            own = True
        token = _ticket.set(value)
        uncertain = False
        try:
            async with self.lifecycle.track("writers"):
                yield
        except BaseException:
            uncertain = value.operation_id is not None
            raise
        finally:
            _ticket.reset(token)
            if own:
                value.active = False
                await self.store.finish_ticket(value.id, uncertain=uncertain)

    async def bind_operation(self, operation):
        value = _ticket.get()
        if self.has_ticket() and value.operation_id is None:
            value.operation_id = operation.id
            await self.store.attach_ticket(value.id, operation.id)

    async def require_writer(self):
        value = _ticket.get()
        if not self.has_ticket():
            raise OperationOwnershipError("A live release writer ticket is required")
        await self.store._call(
            "assert_ticket", self.config.scope, self.config.instance_id, value.id
        )

    async def confirm_read_end(self, kind, operation):
        if kind not in READ_OPERATIONS | INITIAL_OPERATIONS:
            return False
        coordinator = self.runtime.coordinator
        async with coordinator._transaction() as (connection, row):
            await coordinator._validate(connection, row, operation)
            return not await connection.fetchval(
                "SELECT EXISTS(SELECT 1 FROM lightrag_coordination.mutations WHERE operation_id=$1 AND state='pending') "
                "OR EXISTS(SELECT 1 FROM lightrag_coordination.resource_locks WHERE operation_id=$1) "
                "OR EXISTS(SELECT 1 FROM lightrag_coordination.document_claims WHERE operation_id=$1) "
                "OR EXISTS(SELECT 1 FROM lightrag_release.tickets WHERE operation_id=$1 AND state<>'COMPLETED')",
                operation.id,
            )

    @asynccontextmanager
    async def cache_fill(self):
        if self.has_ticket():
            yield True
            return
        await self.refresh()
        identifier = await self.store.admit_writer(
            epoch=self.scope_state["epoch"], kind="query_cache"
        )
        if identifier is None:
            yield False
            return
        value = Ticket(self, identifier, self.scope_state["epoch"])
        token = _ticket.set(value)
        uncertain = False
        try:
            async with self.lifecycle.track("writers"):
                await self.bind_operation(self.runtime.permit().operation)
                yield True
        except BaseException:
            uncertain = True
            raise
        finally:
            value.active = False
            _ticket.reset(token)
            await self.store.finish_ticket(identifier, uncertain=uncertain)

    async def query(self, function, obj, args, kwargs, *, exclusive=False):
        return await run_query(
            lambda: function(obj, *args, **kwargs),
            lambda: self.runtime.operation(function.__name__, exclusive=exclusive),
        )

    async def close_instance(self, function, obj, args, kwargs):
        from lightrag.distributed.runtime import _current

        if _current.get() is not None:
            raise OperationOwnershipError("Cannot finalize inside an active operation")
        self.lifecycle.begin_retire()
        self.polling_desired = False
        self.stop_event.set()
        tasks = [
            task
            for task in (self.controller_task, self.executor_task)
            if task is not None
        ]
        if tasks:
            await asyncio.shield(asyncio.gather(*tasks))
        from lightrag.distributed.pipeline import _stop_polling

        await _stop_polling(self.rag)
        await self.lifecycle.drain()
        if self.registered:
            await self.store._call(
                "assert_instance_quiescent",
                self.config.scope,
                self.config.instance_id,
                canonical(self.lifecycle.snapshot()),
            )
        result = await function(obj, *args, **kwargs)
        from .usage import publish_final_summary

        await publish_final_summary(self)
        if self.registered:
            await self.store._call(
                "close_instance",
                self.config.scope,
                self.config.instance_id,
                canonical(self.lifecycle.snapshot()),
            )
        self.closed = True
        await self.runtime.close()
        return result
