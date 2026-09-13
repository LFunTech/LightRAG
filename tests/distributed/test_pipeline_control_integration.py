"""Durable control intent is independent of live writer tickets."""

import pytest
from tests.distributed.test_coordinator_integration import (
    isolated_database as _isolated_database,
)

from tests.distributed.test_coordinator_integration import clients as _clients

clients = _clients

pytestmark = pytest.mark.integration


async def test_cancel_survives_reconnect_and_does_not_wait_for_writers(clients):
    a, b = clients
    assert hasattr(a, "pipeline_control"), "Durable pipeline control is missing"
    async with a.operation("pipeline"):
        await b.pipeline_control.pause()
        assert (await a.pipeline_control.status())["paused"] is True
    assert (await b.pipeline_control.status())["paused"] is True
    await a.pipeline_control.resume()
    assert (await b.pipeline_control.status())["paused"] is False


async def test_retry_acceptance_is_durable_deduplicated_and_exclusive_ack(clients):
    a, b = clients
    assert hasattr(a, "pipeline_control"), "Durable retry requests are missing"
    async with a.operation("pipeline"):
        request = await b.pipeline_control.request_retry("request-one")
        assert request["state"] == "selecting"
        assert (await a.pipeline_control.status())["pending_retries"] == 1
    async with a.operation("retry", exclusive=True) as op:
        await a.pipeline_control.add_targets(op, "request-one", {"doc": "version-one"})
        await a.pipeline_control.finish_selection(op, "request-one")
        targets = await a.pipeline_control.targets(op, "request-one", limit=1)
        assert targets == [{"doc_id": "doc", "version": "version-one"}]
        await a.pipeline_control.finish_target(op, "request-one", "doc")
        await a.pipeline_control.finish_request(op, "request-one")
    duplicate = await b.pipeline_control.request_retry("request-one")
    assert duplicate["state"] == "completed"
    assert (await b.pipeline_control.status())["pending_retries"] == 0


@pytest.mark.parametrize(
    "column,default",
    [
        ("pipeline_control.paused", "true"),
        ("pipeline_control.cancel_epoch", "1"),
        ("pipeline_requests.state", "'completed'"),
        ("pipeline_retry_targets.done", "true"),
    ],
)
async def test_new_control_defaults_are_verified_not_repaired(
    isolated_database, column, default
):
    from lightrag.distributed.migrations import verify
    from lightrag.distributed import CoordinationSchemaError

    _, db = isolated_database
    table, name = column.split(".")
    await db.execute(
        f"ALTER TABLE lightrag_coordination.{table} ALTER COLUMN {name} SET DEFAULT {default}"
    )
    with pytest.raises(CoordinationSchemaError):
        await verify(db)


isolated_database = _isolated_database


async def test_status_reports_orphan_blockage_before_another_admission(clients):
    from lightrag.distributed import CoordinationError

    a, b = clients
    context = a.operation("lost_pipeline")
    await context.__aenter__()
    await a.close()
    state = await b.pipeline_control.status()
    try:
        assert state["recovery_required"] is True
    finally:
        with pytest.raises(CoordinationError):
            await context.__aexit__(None, None, None)
