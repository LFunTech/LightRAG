"""First-cause diagnostics must not depend on the optional release protocol."""

import asyncio
from contextlib import asynccontextmanager
from types import SimpleNamespace

import pytest

from lightrag.distributed.coordinator import (
    CoordinationUnavailableError,
    PostgresCoordinator,
)
from lightrag.utils import logger

pytestmark = pytest.mark.offline


@pytest.fixture
def capture(caplog):
    logger.addHandler(caplog.handler)
    try:
        yield caplog
    finally:
        logger.removeHandler(caplog.handler)


class Transport:
    def __init__(self):
        self.terminated = False

    def get_server_pid(self):
        return 101

    def is_closed(self):
        return self.terminated

    def terminate(self):
        self.terminated = True


@pytest.mark.parametrize("release", [False, True])
@pytest.mark.parametrize("sqlstate", ["57014", "08006", "password=do-not-log"])
def test_first_failure_is_logged_once_and_transports_are_quarantined(
    monkeypatch, capture, release, sqlstate
):
    monkeypatch.setenv("POD_NAME", "candidate-local")
    coor = PostgresCoordinator(
        "postgresql://user:do-not-log@private.invalid/db", "deployment", "workspace"
    )
    if release:
        coor._release_context = SimpleNamespace(instance_id="release-pod")
    coor._pool, coor._witness_connection = Transport(), Transport()
    error = RuntimeError("private SQL body password=do-not-log")
    error.sqlstate = sqlstate
    coor._quarantine(error=error, purpose="transaction", backend_pid=202)
    coor._quarantine(error=ValueError("second secret"), purpose="witness")
    records = [r for r in capture.records if "quarantine" in r.message.lower()]
    assert len(records) == 1
    message = records[0].getMessage()
    assert "first_cause=RuntimeError" in message
    assert f"pod={'release-pod' if release else 'candidate-local'}" in message
    assert "workspace=workspace" in message and "purpose=transaction" in message
    assert "backend_pid=202" in message and "stopped_at=" in message
    assert f"sqlstate={sqlstate if len(sqlstate) == 5 else 'unknown'}" in message
    assert "do-not-log" not in message and "private SQL" not in message
    assert "second secret" not in message and not records[0].exc_info
    assert (
        coor._broken and coor._pool.terminated and coor._witness_connection.terminated
    )


@pytest.mark.parametrize("failure", [TimeoutError, asyncio.CancelledError])
async def test_transaction_failure_preserves_first_cause_and_rejects_later_work(
    monkeypatch, capture, failure
):
    monkeypatch.setenv("POD_NAME", "candidate-local")
    coor = PostgresCoordinator("not-used", "deployment", "workspace", {})

    class Connection:
        def get_server_pid(self):
            return 303

        @asynccontextmanager
        async def transaction(self):
            yield

        async def fetchrow(self, *args):
            raise failure("private SQL and payload must not escape")

    class Pool(Transport):
        acquisitions = 0

        @asynccontextmanager
        async def acquire(self):
            self.acquisitions += 1
            yield Connection()

    coor._pool, coor._witness_connection = Pool(), Transport()
    expected = (
        asyncio.CancelledError
        if failure is asyncio.CancelledError
        else CoordinationUnavailableError
    )
    with pytest.raises(expected):
        async with coor._transaction():
            pytest.fail("Failed transaction admitted business work")
    with pytest.raises(CoordinationUnavailableError):
        async with coor._transaction():
            pytest.fail("Quarantined coordinator was reused")
    assert coor._pool.acquisitions == 1
    assert coor._pool.terminated and coor._witness_connection.terminated
    records = [r for r in capture.records if "quarantine" in r.message.lower()]
    assert len(records) == 1
    record = records[0]
    assert f"first_cause={failure.__name__}" in record.getMessage()
    assert "backend_pid=303" in record.getMessage()
    assert "private SQL" not in capture.text


def test_logging_failure_cannot_leave_coordinator_or_witness_usable(monkeypatch):
    coor = PostgresCoordinator("not-used", "deployment", "workspace", {})
    coor._release_context = SimpleNamespace(instance_id="release-pod")
    coor._pool, coor._witness_connection = Transport(), Transport()

    def fail(*args, **kwargs):
        raise RuntimeError("diagnostic handler failed")

    monkeypatch.setattr(logger, "error", fail)
    with pytest.raises(RuntimeError, match="diagnostic handler failed"):
        coor._quarantine(error=TimeoutError(), purpose="transaction")
    assert (
        coor._broken and coor._pool.terminated and coor._witness_connection.terminated
    )


async def test_normal_close_does_not_invent_a_failure(capture):
    coor = PostgresCoordinator("not-used", "deployment", "workspace", {})
    coor._pool, coor._witness_connection = Transport(), Transport()
    await coor.close()
    assert (
        coor._broken and coor._pool.terminated and coor._witness_connection.terminated
    )
    assert not [r for r in capture.records if "quarantine" in r.message.lower()]


@pytest.mark.parametrize("purpose", ["transaction", "witness"])
def test_unknown_transaction_pid_is_not_substituted_by_unrelated_witness(
    capture, purpose
):
    coor = PostgresCoordinator("not-used", "deployment", "workspace", {})
    coor._pool, coor._witness_connection = Transport(), Transport()
    coor._quarantine(error=TimeoutError(), purpose=purpose)
    records = [r for r in capture.records if "quarantine" in r.message.lower()]
    assert len(records) == 1
    message = records[0].getMessage()
    expected_pid = "101" if purpose == "witness" else "None"
    assert f"backend_pid={expected_pid} witness_pid=101" in message


async def test_uncertainty_fallback_logs_the_failure_that_forced_witness_removal(
    capture,
):
    from lightrag.distributed.coordinator import OperationOwnershipError

    coor = PostgresCoordinator("not-used", "deployment", "workspace", {})
    coor._pool, coor._witness_connection = Transport(), Transport()

    @asynccontextmanager
    async def refused(**kwargs):
        raise OperationOwnershipError("private ownership details")
        yield

    coor._transaction = refused
    await coor._mark_uncertain(None, "synthetic failed ACK")
    assert (
        coor._broken and coor._pool.terminated and coor._witness_connection.terminated
    )
    records = [r for r in capture.records if "quarantine" in r.message.lower()]
    assert len(records) == 1
    assert "first_cause=OperationOwnershipError" in records[0].getMessage()
    assert "purpose=uncertain" in records[0].getMessage()
    assert "private ownership" not in capture.text


async def test_initialize_witness_failure_reports_witness_pid_not_pool_pid(
    monkeypatch, capture
):
    import asyncpg
    from lightrag.distributed import migrations

    coor = PostgresCoordinator("not-used", "deployment", "workspace", {})

    class Connection(Transport):
        @asynccontextmanager
        async def transaction(self):
            yield

        async def execute(self, *args):
            pass

        async def fetchval(self, *args):
            return coor._manifest_hash

    class Pool(Transport):
        @asynccontextmanager
        async def acquire(self):
            yield Connection()

    class Witness(Transport):
        def get_server_pid(self):
            return 404

        async def fetchval(self, *args):
            raise TimeoutError("private SQL and payload")

    pool, witness = Pool(), Witness()

    async def create_pool(*args, **kwargs):
        return pool

    async def connect(*args, **kwargs):
        return witness

    async def verify(*args):
        pass

    monkeypatch.setattr(asyncpg, "create_pool", create_pool)
    monkeypatch.setattr(asyncpg, "connect", connect)
    monkeypatch.setattr(migrations, "verify", verify)
    with pytest.raises(CoordinationUnavailableError):
        await coor.initialize()
    records = [r for r in capture.records if "quarantine" in r.message.lower()]
    assert len(records) == 1
    assert "backend_pid=404" in records[0].getMessage()
    assert "first_cause=TimeoutError" in records[0].getMessage()
    assert "purpose=initialize" in records[0].getMessage()
    assert "private SQL" not in capture.text
    assert coor._broken and pool.terminated and witness.terminated


@pytest.mark.parametrize("release", [False, True])
async def test_polling_failure_logs_stop_once_without_retry_or_private_body(
    monkeypatch, capture, release
):
    from lightrag.distributed import pipeline
    from lightrag.distributed.runtime import DistributedRuntime

    monkeypatch.setenv("POD_NAME", "candidate-local")
    coor = PostgresCoordinator("not-used", "deployment", "workspace", {})
    runtime = DistributedRuntime(coor, workspace="workspace")
    if release:
        runtime.release = SimpleNamespace(
            config=SimpleNamespace(instance_id="release-pod")
        )
    rag = SimpleNamespace(_distributed_runtime=runtime)
    calls = []

    async def fail(*args, **kwargs):
        calls.append(1)
        raise CoordinationUnavailableError("private DSN and SQL body")

    monkeypatch.setattr(pipeline, "process", fail)
    await pipeline._start_polling(rag, interval=0.001)
    await asyncio.wait_for(runtime.pipeline_task, timeout=1)
    await asyncio.sleep(0.01)
    assert calls == [1] and runtime.pipeline_error == "CoordinationUnavailableError"
    records = [r for r in capture.records if "polling stopped" in r.message.lower()]
    assert len(records) == 1
    message = records[0].getMessage()
    assert f"pod={'release-pod' if release else 'candidate-local'}" in message
    assert "workspace=workspace" in message and "deployment=deployment" in message
    assert "reason=CoordinationUnavailableError" in message and "stopped_at=" in message
    assert "private DSN" not in capture.text and not records[0].exc_info


async def test_polling_busy_is_not_logged_as_permanent_stop(monkeypatch, capture):
    from lightrag.distributed import pipeline
    from lightrag.distributed.coordinator import CoordinationBusyError
    from lightrag.distributed.runtime import DistributedRuntime

    coor = PostgresCoordinator("not-used", "deployment", "workspace", {})
    runtime = DistributedRuntime(coor, workspace="workspace")
    rag = SimpleNamespace(_distributed_runtime=runtime)
    calls = []

    async def busy_then_stop(*args, **kwargs):
        calls.append(1)
        if len(calls) == 1:
            raise CoordinationBusyError("synthetic busy admission")
        runtime.pipeline_stop.set()

    monkeypatch.setattr(pipeline, "process", busy_then_stop)
    await pipeline._start_polling(rag, interval=0.001)
    await asyncio.wait_for(runtime.pipeline_task, timeout=1)
    assert len(calls) == 2 and runtime.pipeline_error is None
    assert not [r for r in capture.records if "polling stopped" in r.message.lower()]
