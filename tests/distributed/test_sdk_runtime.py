"""Real SDK entry points must acquire durable permits before business work."""

from contextlib import asynccontextmanager
from dataclasses import asdict
from unittest.mock import AsyncMock

import pytest

from lightrag import LightRAG
from lightrag.utils import EmbeddingFunc
from lightrag.distributed import CoordinationBusyError, OperationOwnershipError
from lightrag.distributed.runtime import DistributedRuntime
from tests.distributed.test_runtime import Coordinator

pytestmark = pytest.mark.offline


@pytest.fixture
def rag(tmp_path):
    return LightRAG(
        working_dir=str(tmp_path),
        workspace="sdk-test",
        llm_model_func=AsyncMock(),
        embedding_func=EmbeddingFunc(
            embedding_dim=3, func=AsyncMock(), model_name="test"
        ),
    )


@pytest.mark.parametrize(
    "name,exclusive",
    [
        ("ainsert", False),
        ("ainsert_custom_chunks", True),
        ("arollback_failed_custom_chunk_patches", True),
        ("ainsert_custom_kg", True),
        ("aquery", False),
        ("aquery_data", False),
        ("aquery_llm", False),
        ("aclear_cache", True),
        ("adelete_by_doc_id", True),
        ("adelete_by_entity", True),
        ("adelete_by_relation", True),
        ("aedit_entity", True),
        ("aedit_relation", True),
        ("acreate_entity", True),
        ("acreate_relation", True),
        ("amerge_entities", True),
    ],
)
async def test_sdk_admission_precedes_every_business_body(rag, name, exclusive):
    c = Coordinator()

    @asynccontextmanager
    async def refuse(kind, **kwargs):
        assert kwargs["exclusive"] is exclusive
        raise CoordinationBusyError("test admission refused")
        yield

    c.operation = refuse
    rag._distributed_runtime = DistributedRuntime(c, workspace=rag.workspace)
    with pytest.raises(CoordinationBusyError, match="test admission refused"):
        await getattr(rag, name)()


async def test_startup_migration_is_verify_only_without_explicit_maintenance(rag):
    c = Coordinator()
    rt = DistributedRuntime(c, workspace=rag.workspace)
    rag._distributed_runtime = rt
    rag._verify_distributed_data = AsyncMock()
    rag.chunk_entity_relation_graph.get_all_labels = AsyncMock(
        side_effect=AssertionError("automatic migration ran")
    )
    await rag.check_and_migrate_data()
    rag._verify_distributed_data.assert_awaited_once()


def test_runtime_credentials_are_not_dataclass_or_exported_config(rag):
    rag._distributed_runtime = DistributedRuntime(
        Coordinator(), workspace=rag.workspace
    )
    rag._distributed_runtime.secret = "never-print-this"
    assert "_distributed_runtime" not in asdict(rag)
    assert "never-print-this" not in repr(rag._build_global_config())


async def test_explicit_maintenance_available_before_storage_initialization(rag):
    c = Coordinator()
    c.initialize = AsyncMock()
    rt = DistributedRuntime(c, workspace=rag.workspace)
    rag._distributed_runtime = rt
    assert hasattr(rag, "distributed_maintenance"), (
        "Public maintenance bootstrap is missing"
    )
    async with rag.distributed_maintenance():
        assert rt.permit(maintenance=True).exclusive
    c.initialize.assert_awaited_once()


async def test_unapproved_migration_helper_cannot_write(rag):
    rt = DistributedRuntime(Coordinator(), workspace=rag.workspace)
    rag._distributed_runtime = rt
    async with rt.operation("ingest"):
        with pytest.raises(OperationOwnershipError):
            await rag._migrate_chunk_tracking_storage()


async def test_stale_maintenance_ticket_cannot_silently_claim_migration_success(rag):
    import asyncio

    rt = DistributedRuntime(Coordinator(), workspace=rag.workspace)
    rag._distributed_runtime = rt
    rag._verify_distributed_data = AsyncMock()
    released = asyncio.Event()

    async def late_migration():
        await released.wait()
        with pytest.raises(OperationOwnershipError):
            await rag.check_and_migrate_data()

    async with rt.operation("maintenance", exclusive=True, maintenance=True):
        child = asyncio.create_task(late_migration())
    released.set()
    await child
