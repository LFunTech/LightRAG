import asyncio
import os
from uuid import uuid4

import pytest

pytestmark = [pytest.mark.integration, pytest.mark.requires_db]


async def make_rag(
    db, *, deployment, workspace, instance, slot="blue", config_hash="b" * 64
):
    from lightrag.distributed import PostgresCoordinator
    from lightrag.distributed.runtime import (
        DistributedRuntime,
        operation_guard,
        finalization_guard,
        storage_write,
        physical_write,
        initialization_guard,
    )
    from lightrag.distributed.release.runtime import ReleaseRuntime
    from lightrag.distributed.release.config import ReleaseConfig

    dsn = os.environ["LIGHTRAG_RELEASE_TEST_DSN"]
    await PostgresCoordinator.migrate(dsn)
    coordinator = PostgresCoordinator(
        dsn, deployment, workspace, {"fixture": "release-runtime"}
    )
    runtime = DistributedRuntime(coordinator, workspace=workspace)

    class Storage:
        namespace = "test"

        def __init__(self):
            self._distributed_runtime = runtime
            self.workspace = workspace

        @storage_write
        async def upsert(self, data):
            from lightrag.distributed.release.commands import canonical

            async with physical_write():
                async with coordinator._pool.acquire() as c:
                    await c.execute(
                        "INSERT INTO public.release_test_effects(scope,value) VALUES($1,$2)",
                        deployment + "/" + workspace,
                        data if isinstance(data, str) else canonical(data),
                    )

        @storage_write
        async def initialize(self):
            self.initialized = True

        async def get_by_id(self, key):
            return None

    class Rag:
        def __init__(self):
            self._distributed_runtime = runtime
            self.storage = Storage()
            self.finalized = False

        @initialization_guard
        async def initialize_storages(self):
            await self.storage.initialize()

        @operation_guard(exclusive=True)
        async def acreate_entity(self, entity_name, entity_data):
            await self.storage.upsert(entity_name)
            return {"entity_name": entity_name, "entity_data": entity_data}

        @operation_guard()
        async def aquery(self, query):
            async def stream():
                yield query
                await asyncio.sleep(0.01)

            return stream()

        @finalization_guard
        async def finalize_storages(self):
            self.finalized = True

    rag = Rag()
    from lightrag.pipeline import _PipelineMixin

    Rag.apipeline_request_retry = _PipelineMixin.apipeline_request_retry
    runtime.release = ReleaseRuntime(
        runtime,
        rag,
        ReleaseConfig(
            deployment + "/" + workspace, slot, instance, config_hash, "a" * 64
        ),
    )
    await runtime.initialize()
    return rag


async def setup_scope(db):
    from lightrag.distributed.release.store import ReleaseStore
    from lightrag.distributed.release.migrations import migrate

    await migrate(db)
    await db.execute(
        "CREATE TABLE IF NOT EXISTS public.release_test_effects(scope text,value text)"
    )
    dep = "prod_" + uuid4().hex[:12]
    work = "workspace"
    await ReleaseStore(db).onboard(
        dep + "/" + work,
        active_slot="blue",
        profile_hash="a" * 64,
        config_hash="b" * 64,
        policy={
            "queue_capacity": 10,
            "request_wait_seconds": 3,
            "writers": ["sdk-test"],
        },
    )
    return dep, work


async def test_sdk_write_requires_registered_principal_and_keeps_result_semantics(
    release_db,
):
    from lightrag.distributed.release.runtime import request_context
    from lightrag.distributed.release.models import ReleaseError

    dep, work = await setup_scope(release_db)
    rag = await make_rag(release_db, deployment=dep, workspace=work, instance="pod-1")
    try:
        with pytest.raises(ReleaseError):
            await rag.acreate_entity("denied", {})
        with request_context(service_id="sdk-test", idempotency_key="same"):
            first = await rag.acreate_entity("one", {"description": "value"})
            second = await rag.acreate_entity("one", {"description": "value"})
        assert (
            first
            == second
            == {"entity_name": "one", "entity_data": {"description": "value"}}
        )
        assert (
            await release_db.fetchval(
                "SELECT count(*) FROM public.release_test_effects WHERE scope=$1",
                dep + "/" + work,
            )
            == 1
        )
    finally:
        await rag.finalize_storages()


async def test_release_queries_hold_permit_until_stream_close_without_blocking_peer_finalize(
    release_db,
):
    dep, work = await setup_scope(release_db)
    first = await make_rag(release_db, deployment=dep, workspace=work, instance="pod-1")
    second = await make_rag(
        release_db, deployment=dep, workspace=work, instance="pod-2"
    )
    stream = None
    try:
        stream = await second.aquery("still running")
        assert await anext(stream) == "still running"
        assert (
            await release_db.fetchval(
                "SELECT count(*) FROM lightrag_coordination.operations WHERE deployment_id=$1 AND state='active'",
                dep,
            )
            == 1
        )
        await asyncio.wait_for(first.finalize_storages(), 1)
        assert first.finalized
        await stream.aclose()
        assert not second._distributed_runtime.closed
    finally:
        if stream is not None:
            await stream.aclose()
        if not first._distributed_runtime.closed:
            await first.finalize_storages()
        await second.finalize_storages()


async def test_query_cache_fill_is_optional_before_send_but_never_bypasses_ticket(
    release_db,
):
    from lightrag.utils import CacheData, save_to_cache
    from lightrag.distributed.release.store import ReleaseStore
    from lightrag.distributed.release.models import ReleaseIdentity

    dep, work = await setup_scope(release_db)
    rag = await make_rag(release_db, deployment=dep, workspace=work, instance="pod-1")
    runtime = rag._distributed_runtime
    try:
        async with runtime.operation("aquery"):
            await save_to_cache(
                rag.storage,
                CacheData(
                    args_hash="one",
                    content="answer",
                    prompt="q",
                    mode="mix",
                    cache_type="query",
                ),
            )
        store = ReleaseStore(release_db)
        identity = ReleaseIdentity(
            dep + "/" + work, "r1", "v1.2.3", "c" * 40, "sha256:" + "d" * 64, "e" * 64
        )
        await store.begin(identity, expected_epoch=1)
        await store.transition(
            identity, expected_epoch=1, expected_phase="PREPARED", next_phase="DRAINING"
        )
        async with runtime.operation("aquery"):
            await save_to_cache(
                rag.storage,
                CacheData(
                    args_hash="two",
                    content="answer",
                    prompt="q",
                    mode="mix",
                    cache_type="query",
                ),
            )
        assert (
            await release_db.fetchval(
                "SELECT count(*) FROM public.release_test_effects WHERE scope=$1",
                dep + "/" + work,
            )
            == 1
        )
    finally:
        await rag.finalize_storages()


async def test_unadmitted_sdk_control_cannot_resume_production(release_db):
    from lightrag.distributed import OperationOwnershipError

    dep, work = await setup_scope(release_db)
    rag = await make_rag(release_db, deployment=dep, workspace=work, instance="pod-1")
    try:
        with pytest.raises(OperationOwnershipError):
            await rag._distributed_runtime.coordinator.pipeline_control.resume()
    finally:
        await rag.finalize_storages()


async def test_sdk_manual_retry_uses_same_durable_queue_without_extra_attempt(
    release_db,
):
    from lightrag.distributed.release.runtime import request_context

    dep, work = await setup_scope(release_db)
    rag = await make_rag(release_db, deployment=dep, workspace=work, instance="pod-1")
    try:
        with request_context(service_id="sdk-test", idempotency_key="retry-once"):
            assert await rag.apipeline_request_retry("intent-1") == "intent-1"
            assert await rag.apipeline_request_retry("intent-1") == "intent-1"
        assert (
            await release_db.fetchval(
                "SELECT count(*) FROM lightrag_coordination.pipeline_requests WHERE deployment_id=$1 AND workspace=$2",
                dep,
                work,
            )
            == 1
        )
    finally:
        await rag.finalize_storages()


async def test_cancelled_read_only_request_settles_only_after_confirmed_no_mutations(
    release_db,
):
    dep, work = await setup_scope(release_db)
    rag = await make_rag(release_db, deployment=dep, workspace=work, instance="pod-1")
    runtime = rag._distributed_runtime
    try:
        with pytest.raises(asyncio.CancelledError):
            async with runtime.operation("release_read"):
                raise asyncio.CancelledError
        assert not await release_db.fetchval(
            "SELECT fenced FROM lightrag_coordination.workspaces WHERE deployment_id=$1 AND workspace=$2",
            dep,
            work,
        )
        assert (
            await release_db.fetchval(
                "SELECT count(*) FROM lightrag_coordination.operations WHERE deployment_id=$1 AND state='active'",
                dep,
            )
            == 0
        )
    finally:
        await rag.finalize_storages()


async def test_readiness_observation_does_not_use_quarantining_transaction(
    release_db, monkeypatch
):
    from contextlib import asynccontextmanager

    dep, work = await setup_scope(release_db)
    rag = await make_rag(release_db, deployment=dep, workspace=work, instance="pod-1")
    runtime = rag._distributed_runtime

    @asynccontextmanager
    async def forbidden(**kwargs):
        raise AssertionError("Readiness must not enter a mutation transaction")
        yield

    try:
        with monkeypatch.context() as patch:
            patch.setattr(runtime.coordinator, "_transaction", forbidden)
            await runtime.release.probe()
        assert not runtime.coordinator._broken
    finally:
        await rag.finalize_storages()


async def test_unknown_read_mutation_cannot_be_settled_or_finalize_resources(
    release_db,
):
    from lightrag.distributed.release.models import ReleaseConflict

    dep, work = await setup_scope(release_db)
    rag = await make_rag(release_db, deployment=dep, workspace=work, instance="pod-1")
    runtime = rag._distributed_runtime
    try:
        with pytest.raises(asyncio.CancelledError):
            async with runtime.operation("release_read") as operation:
                await release_db.execute(
                    "INSERT INTO lightrag_coordination.mutations(id,operation_id,backend,namespace,method,state) VALUES($1,$2,'PG','synthetic','synthetic','pending')",
                    uuid4(),
                    operation.id,
                )
                raise asyncio.CancelledError
        assert await release_db.fetchval(
            "SELECT fenced FROM lightrag_coordination.workspaces WHERE deployment_id=$1 AND workspace=$2",
            dep,
            work,
        )
        with pytest.raises(ReleaseConflict):
            await rag.finalize_storages()
        assert not rag.finalized
        assert not runtime.coordinator._witness_connection.is_closed()
    finally:
        await runtime.coordinator.close()


async def test_standby_initialization_verifies_without_a_business_writer_ticket(
    release_db,
):
    dep, work = await setup_scope(release_db)
    rag = await make_rag(release_db, deployment=dep, workspace=work, instance="pod-1")
    try:
        await rag.initialize_storages()
        assert rag.storage.initialized
    finally:
        await rag._distributed_runtime.coordinator.close()


async def test_failed_verify_only_initialization_does_not_fence_blue(release_db):
    dep, work = await setup_scope(release_db)
    rag = await make_rag(release_db, deployment=dep, workspace=work, instance="pod-1")

    async def incompatible():
        raise ValueError("candidate schema incompatible")

    rag.storage.initialize = incompatible
    try:
        with pytest.raises(ValueError):
            await rag.initialize_storages()
        assert not await release_db.fetchval(
            "SELECT fenced FROM lightrag_coordination.workspaces WHERE deployment_id=$1 AND workspace=$2",
            dep,
            work,
        )
    finally:
        await rag._distributed_runtime.coordinator.close()
