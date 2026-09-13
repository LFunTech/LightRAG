"""Offline contract tests for the opt-in coordination boundary."""

import asyncio
import importlib.util
import os
from pathlib import Path
import subprocess
import sys

import pytest

from lightrag.distributed import (
    CoordinationUnavailableError,
    PostgresCoordinator,
)

pytestmark = pytest.mark.offline


@pytest.mark.parametrize("field", ["deployment_id", "workspace"])
def test_empty_scope_is_rejected_before_connect(field):
    kwargs = dict(dsn="not-used", deployment_id="deployment", workspace="workspace")
    kwargs[field] = " "
    with pytest.raises(ValueError):
        PostgresCoordinator(**kwargs)


@pytest.mark.parametrize(
    "kwargs",
    [
        {"wait_timeout": -1},
        {"poll_interval": 0},
        {"command_timeout": 0},
        {"pool_size": 0},
    ],
)
def test_unsafe_timeouts_or_pool_size_are_rejected(kwargs):
    with pytest.raises(ValueError):
        PostgresCoordinator("not-used", "deployment", "workspace", {}, **kwargs)


async def test_uninitialized_or_closed_coordinator_never_yields_business_permit():
    coordinator = PostgresCoordinator("not-used", "deployment", "workspace", {})
    for closed in (False, True):
        if closed:
            await coordinator.close()
        with pytest.raises(CoordinationUnavailableError):
            async with coordinator.operation("write"):
                pytest.fail("Unavailable coordination yielded a business permit")
        with pytest.raises(CoordinationUnavailableError):
            await coordinator.inspect()


async def test_connection_errors_do_not_disclose_dsn_or_driver_details(monkeypatch):
    asyncpg = pytest.importorskip("asyncpg")
    secret = "postgresql://user:never-print-this@invalid/db"

    async def fail(*args, **kwargs):
        raise RuntimeError(secret)

    monkeypatch.setattr(asyncpg, "create_pool", fail)
    coordinator = PostgresCoordinator(secret, "deployment", "workspace", {})
    with pytest.raises(CoordinationUnavailableError) as caught:
        await coordinator.initialize()
    assert secret not in str(caught.value)
    assert "never-print-this" not in repr(coordinator)
    assert caught.value.__suppress_context__


async def test_wait_timeout_and_cancellation_do_not_quarantine_unadmitted_client():
    coordinator = PostgresCoordinator("not-used", "deployment", "workspace", {})
    from lightrag.distributed import CoordinationBusyError

    async def busy():
        return None

    with pytest.raises(CoordinationBusyError):
        await coordinator._wait(busy, 0)
    task = asyncio.create_task(coordinator._wait(busy, 100))
    await asyncio.sleep(0)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert not coordinator._broken


@pytest.mark.parametrize(
    "missing", ["writers_stopped", "inflight_finished", "state_audited"]
)
async def test_recovery_refuses_missing_confirmation_without_a_database(missing):
    coordinator = PostgresCoordinator("not-used", "deployment", "workspace", {})
    assert hasattr(coordinator, "recover"), "Recovery prerequisite validation missing"
    confirmations = dict(
        writers_stopped=True, inflight_finished=True, state_audited=True
    )
    confirmations[missing] = False
    with pytest.raises(ValueError):
        await coordinator.recover(
            expected_generation=1, actor="operator", reason="audit", **confirmations
        )


def run_cli(*args):
    return subprocess.run(
        [sys.executable, "-m", "lightrag.distributed", *args],
        cwd=Path(__file__).parents[2],
        env={
            **os.environ,
            "LIGHTRAG_COORDINATION_DSN": "postgresql://user:never-print-this@invalid/db",
        },
        capture_output=True,
        text=True,
        timeout=15,
    )


def test_cli_recovery_checks_confirmations_before_connection():
    assert importlib.util.find_spec("lightrag.distributed.__main__"), (
        "Recovery CLI missing"
    )
    result = run_cli(
        "recover",
        "--deployment-id",
        "deployment",
        "--workspace",
        "workspace",
        "--expected-generation",
        "1",
        "--actor",
        "operator",
        "--reason",
        "audit",
    )
    assert result.returncode == 2
    assert "confirm" in result.stderr.lower()
    assert "never-print-this" not in result.stderr + result.stdout


def test_cli_has_no_inline_dsn_option_and_keeps_driver_errors_private():
    assert importlib.util.find_spec("lightrag.distributed.__main__"), (
        "Recovery CLI missing"
    )
    result = run_cli("--help")
    assert result.returncode == 0
    assert "--dsn " not in result.stdout
    result = run_cli(
        "inspect", "--deployment-id", "deployment", "--workspace", "workspace"
    )
    assert result.returncode != 0
    assert "never-print-this" not in result.stderr + result.stdout
    assert "Traceback" not in result.stderr
