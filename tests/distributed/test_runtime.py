"""Distributed runtime admission and scope contracts, without external services."""

import asyncio
from contextlib import asynccontextmanager
import importlib
from types import SimpleNamespace
from uuid import uuid4

import pytest

from lightrag.distributed import OperationOwnershipError

pytestmark = pytest.mark.offline


def runtime_module():
    spec = importlib.util.find_spec("lightrag.distributed.runtime")
    assert spec is not None, "Distributed runtime boundary is missing"
    return importlib.import_module("lightrag.distributed.runtime")


class Coordinator:
    def __init__(self):
        self.events = []

    @asynccontextmanager
    async def operation(self, kind, **kwargs):
        op = SimpleNamespace(id=uuid4())
        self.events.append(("enter", kind, kwargs["exclusive"]))
        yield op
        self.events.append(("exit", kind))

    async def heartbeat(self, operation):
        self.events.append(("validate", operation.id))

    @asynccontextmanager
    async def lock(self, operation, keys):
        self.events.append(("lock", tuple(keys)))
        yield
        self.events.append(("unlock", tuple(keys)))

    @asynccontextmanager
    async def mutation(self, operation, backend, namespace, method):
        self.events.append(("pending", backend, namespace, method))
        try:
            yield
        except BaseException:
            self.events.append(("fence", method))
            raise
        else:
            self.events.append(("ack", method))


def make_runtime():
    module = runtime_module()
    coordinator = Coordinator()
    return module.DistributedRuntime(coordinator, workspace="test"), coordinator


async def test_nested_admission_reuses_ticket_but_refuses_shared_upgrade():
    runtime, coordinator = make_runtime()
    async with runtime.operation("query") as first:
        async with runtime.operation("cache") as second:
            assert first is second
        with pytest.raises(OperationOwnershipError):
            async with runtime.operation("clear", exclusive=True):
                pytest.fail("Shared operation upgraded itself")
    assert sum(e[0] == "enter" for e in coordinator.events) == 1


async def test_inherited_ticket_cannot_outlive_parent():
    runtime, coordinator = make_runtime()
    ready = asyncio.Event()

    async def late_child():
        await ready.wait()
        with pytest.raises(OperationOwnershipError):
            async with runtime.operation("late"):
                pytest.fail("An ended ticket was reused")

    async with runtime.operation("request"):
        child = asyncio.create_task(late_child())
    ready.set()
    await child
    assert sum(e[0] == "enter" for e in coordinator.events) == 1


async def test_retry_request_carries_application_clock_cutoff():
    """Manual retry target selection must not compare against DB server time."""

    from datetime import datetime, timezone

    from lightrag.distributed.runtime import DistributedRuntime
    from lightrag.pipeline import _PipelineMixin

    class PipelineControl:
        def __init__(self):
            self.calls = []

        async def request_retry(self, request_id, *, target_cutoff_at=None):
            self.calls.append((request_id, target_cutoff_at))

    control = PipelineControl()
    rag = SimpleNamespace(
        _distributed_runtime=DistributedRuntime(
            SimpleNamespace(pipeline_control=control), workspace="test"
        )
    )

    before = datetime.now(timezone.utc)
    await _PipelineMixin.apipeline_request_retry(rag, "clock-safe")
    after = datetime.now(timezone.utc)

    assert control.calls[0][0] == "clock-safe"
    cutoff = control.calls[0][1]
    assert cutoff is not None
    assert cutoff.tzinfo is not None
    assert before <= cutoff <= after


async def test_uncontrolled_storage_mutation_is_rejected_and_ack_follows_actual_write():
    module = runtime_module()
    runtime, coordinator = make_runtime()

    class Storage:
        namespace = "text_chunks"
        workspace = "test"
        _distributed_runtime = runtime

        @module.storage_write
        async def upsert(self):
            async with module.physical_write():
                coordinator.events.append(("actual-write",))

    storage = Storage()
    with pytest.raises(OperationOwnershipError):
        await storage.upsert()
    assert coordinator.events == []
    async with runtime.operation("ingest"):
        await storage.upsert()
    names = [e[0] for e in coordinator.events]
    assert names.index("pending") < names.index("actual-write") < names.index("ack")


async def test_thread_bridge_preserves_active_exclusive_ticket_without_deadlock():
    runtime, coordinator = make_runtime()
    loop = asyncio.get_running_loop()

    async def cache():
        async with runtime.operation("cache"):
            return 42

    def bridge():
        return asyncio.run_coroutine_threadsafe(cache(), loop).result(timeout=2)

    async with runtime.operation("scan", exclusive=True):
        assert await asyncio.to_thread(bridge) == 42
    assert sum(e[0] == "enter" for e in coordinator.events) == 1


def test_default_profile_does_not_parse_or_create_coordinator(monkeypatch):
    module = runtime_module()
    monkeypatch.setenv("LIGHTRAG_COORDINATION_DSN", "secret")
    assert module.configure_runtime(SimpleNamespace(distributed_writes=False)) is None


@pytest.mark.parametrize(
    "change",
    [
        {"workspace": ""},
        {"kv_storage": "JsonKVStorage"},
        {"vector_storage": "NanoVectorDBStorage"},
        {"doc_status_storage": "JsonDocStatusStorage"},
        {"graph_storage": "NetworkXStorage"},
    ],
)
def test_unsafe_profile_rejected_before_backend_initialization(monkeypatch, change):
    module = runtime_module()
    monkeypatch.setenv("LIGHTRAG_SHARED_STORAGE", "true")
    monkeypatch.setenv("LIGHTRAG_COORDINATION_DSN", "not-used")
    monkeypatch.setenv("LIGHTRAG_DEPLOYMENT_ID", "deployment")
    rag = SimpleNamespace(
        distributed_writes=True,
        workspace="test",
        kv_storage="PGKVStorage",
        vector_storage="PGVectorStorage",
        doc_status_storage="PGDocStatusStorage",
        graph_storage="HugeGraphStorage",
    )
    rag.__dict__.update(change)
    with pytest.raises(ValueError):
        module.configure_runtime(rag)


def valid_profile(monkeypatch, tmp_path):
    from lightrag.utils import EmbeddingFunc

    monkeypatch.setenv("LIGHTRAG_SHARED_STORAGE", "true")
    monkeypatch.setenv(
        "LIGHTRAG_COORDINATION_DSN", "postgresql://user:coord-secret@localhost/test"
    )
    monkeypatch.setenv("LIGHTRAG_DEPLOYMENT_ID", "deployment")
    monkeypatch.setenv("HUGEGRAPH_URI", "http://localhost:8080")
    monkeypatch.setenv("POSTGRES_PASSWORD", "pg-secret")
    monkeypatch.delenv("POSTGRES_WORKSPACE", raising=False)
    monkeypatch.delenv("LIGHTRAG_OBJECT_STORAGE", raising=False)
    return SimpleNamespace(
        distributed_writes=True,
        workspace="test",
        kv_storage="PGKVStorage",
        vector_storage="PGVectorStorage",
        doc_status_storage="PGDocStatusStorage",
        graph_storage="HugeGraphStorage",
        embedding_func=EmbeddingFunc(
            embedding_dim=3, func=lambda x: x, model_name="test"
        ),
        working_dir=str(tmp_path),
        distributed_input_dir=str(tmp_path / "inputs"),
    )


@pytest.mark.parametrize(
    "key,value",
    [
        ("LIGHTRAG_SHARED_STORAGE", "false"),
        ("LIGHTRAG_DEPLOYMENT_ID", ""),
        ("LIGHTRAG_COORDINATION_DSN", ""),
        ("POSTGRES_WORKSPACE", "other"),
        ("LIGHTRAG_COORDINATION_POOL_MODE", "transaction"),
        ("LIGHTRAG_COORDINATION_POOL_MODE", "statement"),
    ],
)
def test_profile_rejects_unsafe_environment(monkeypatch, tmp_path, key, value):
    module = runtime_module()
    rag = valid_profile(monkeypatch, tmp_path)
    monkeypatch.setenv(key, value)
    with pytest.raises(ValueError):
        module.configure_runtime(rag)


def test_object_store_profile_allows_ephemeral_input_path(monkeypatch, tmp_path):
    module = runtime_module()
    rag = valid_profile(monkeypatch, tmp_path)
    monkeypatch.setenv("LIGHTRAG_SHARED_STORAGE", "false")
    monkeypatch.setenv("LIGHTRAG_OBJECT_STORAGE", "s3")
    captured = []

    def coordinator(dsn, deployment, workspace, manifest):
        captured.append(manifest)
        return Coordinator()

    monkeypatch.setattr(module, "PostgresCoordinator", coordinator)

    runtime = module.configure_runtime(rag)

    assert runtime is not None
    assert captured[0]["paths"]["input"] == str(tmp_path / "inputs")
    assert captured[0]["object_storage"]["provider"] == "s3"
    assert captured[0]["shared_filesystem_required"] is False


def test_manifest_includes_actual_input_path_and_not_credentials(monkeypatch, tmp_path):
    module = runtime_module()
    rag = valid_profile(monkeypatch, tmp_path)
    captured = []

    def coordinator(dsn, deployment, workspace, manifest):
        captured.append(manifest)
        return Coordinator()

    monkeypatch.setattr(module, "PostgresCoordinator", coordinator)
    module.configure_runtime(rag)
    assert captured[0]["paths"]["input"] == str(tmp_path / "inputs")
    text = repr(captured)
    assert "coord-secret" not in text and "pg-secret" not in text


def test_postgres_search_path_cannot_silently_change_storage_target(
    monkeypatch, tmp_path
):
    module = runtime_module()
    rag = valid_profile(monkeypatch, tmp_path)
    monkeypatch.setenv("POSTGRES_SERVER_SETTINGS", "search_path=other_schema")
    with pytest.raises(ValueError, match="search_path"):
        module.configure_runtime(rag)


async def test_distributed_commit_region_does_not_fork_its_lock_owner():
    from lightrag.utils_graph import _finish_deferring_cancellation

    runtime, c = make_runtime()
    owning_task = asyncio.current_task()

    async def commit():
        assert asyncio.current_task() is owning_task, (
            "Commit forked away from durable lock owner"
        )

    async with runtime.operation("delete", exclusive=True):
        await _finish_deferring_cancellation(commit(), "test")


async def test_raw_opted_in_storage_cannot_bypass_runtime_binding(monkeypatch):
    from lightrag.distributed.runtime import storage_write

    monkeypatch.setenv("LIGHTRAG_DISTRIBUTED_WRITES", "true")

    class RawStorage:
        global_config = {}

        @storage_write
        async def upsert(self):
            pytest.fail("Unbound direct storage wrote in distributed mode")

    with pytest.raises(OperationOwnershipError):
        await RawStorage().upsert()


async def test_sdk_reframes_a_wrapped_physical_failure_as_coordination_error():
    from lightrag.distributed import WorkspaceFencedError
    from lightrag.distributed.runtime import operation_guard

    runtime, c = make_runtime()

    async def heartbeat(op):
        raise WorkspaceFencedError("uncertain")

    class SDK:
        _distributed_runtime = runtime

        @operation_guard()
        async def write(self):
            c.heartbeat = heartbeat
            raise RuntimeError("Existing business wrapper obscured storage failure")

    with pytest.raises(WorkspaceFencedError):
        await SDK().write()


async def test_nested_maintenance_finalization_closes_after_ticket_completion():
    from lightrag.distributed.runtime import finalization_guard
    from unittest.mock import AsyncMock

    runtime, c = make_runtime()
    c.close = AsyncMock()

    class SDK:
        _distributed_runtime = runtime

        @finalization_guard
        async def finalize(self):
            pass

    async with runtime.operation("maintenance", exclusive=True, maintenance=True):
        await SDK().finalize()
        c.close.assert_not_awaited()
    c.close.assert_awaited_once()


async def test_client_close_failure_still_closes_coordinator():
    from unittest.mock import AsyncMock

    runtime, c = make_runtime()
    c.close = AsyncMock()
    runtime.storages = [
        SimpleNamespace(
            _client=SimpleNamespace(
                close=AsyncMock(side_effect=RuntimeError("close failed"))
            )
        )
    ]
    with pytest.raises(RuntimeError):
        await runtime.close()
    c.close.assert_awaited_once()


async def test_concurrent_initialization_uses_one_coordinator_transport():
    runtime, c = make_runtime()
    calls = []

    async def initialize():
        calls.append("opened")
        await asyncio.sleep(0)

    c.initialize = initialize
    await asyncio.gather(runtime.initialize(), runtime.initialize())
    assert calls == ["opened"]


async def test_storage_cannot_change_workspace_after_runtime_binding():
    from lightrag.distributed.runtime import storage_write

    runtime, c = make_runtime()

    class Storage:
        workspace = "other"
        namespace = "full_docs"
        _distributed_runtime = runtime

        @storage_write
        async def upsert(self):
            pytest.fail("Storage wrote into an uncoordinated workspace")

    async with runtime.operation("write"):
        with pytest.raises(OperationOwnershipError):
            await Storage().upsert()


async def test_finalization_body_failure_after_admission_still_closes():
    from lightrag.distributed.runtime import finalization_guard
    from unittest.mock import AsyncMock

    runtime, c = make_runtime()
    c.close = AsyncMock()

    class SDK:
        _distributed_runtime = runtime

        @finalization_guard
        async def finalize(self):
            raise RuntimeError("finalization failed after admission")

    with pytest.raises(RuntimeError, match="after admission"):
        await SDK().finalize()
    c.close.assert_awaited_once()
    assert runtime.closed
