"""Operator bootstrap uses the real server constructor, never app lifespan."""

import argparse
from contextlib import asynccontextmanager
import types
from unittest.mock import AsyncMock, Mock

import pytest

from lightrag.distributed import __main__ as cli


async def test_bootstrap_runs_preinit_maintenance(monkeypatch):
    events = []

    @asynccontextmanager
    async def maintenance(**kwargs):
        events.append(("admit", kwargs))
        yield
        events.append("released")

    rag = Mock()
    rag.distributed_writes = True
    rag.distributed_maintenance = maintenance
    rag.initialize_storages = AsyncMock(side_effect=lambda: events.append("initialize"))
    rag.check_and_migrate_data = AsyncMock(side_effect=lambda: events.append("migrate"))
    rag.finalize_storages = AsyncMock(side_effect=lambda: events.append("finalize"))
    monkeypatch.setattr(cli, "configured_rag", lambda: rag, raising=False)
    monkeypatch.setattr(
        cli,
        "preflight_bootstrap_storage",
        AsyncMock(side_effect=lambda rag: events.append("preflight")),
        raising=False,
    )
    args = argparse.Namespace(
        command="bootstrap",
        actor="operator",
        confirm_writers_stopped=True,
        confirm_inflight_finished=True,
    )
    result = await cli.run(args, "secret")
    assert events[0] == "preflight"
    assert events[1][0] == "admit"
    assert events[2:] == ["initialize", "migrate", "released", "finalize"]
    assert result == {"bootstrap": "verified"}


async def test_bootstrap_preflight_failure_does_not_admit_maintenance(monkeypatch):
    rag = Mock()
    rag.distributed_writes = True
    rag.distributed_maintenance.side_effect = AssertionError("must not admit")
    rag.initialize_storages = AsyncMock()
    monkeypatch.setattr(cli, "configured_rag", lambda: rag, raising=False)
    monkeypatch.setattr(
        cli,
        "preflight_bootstrap_storage",
        AsyncMock(side_effect=ValueError("pgvector preflight failed")),
        raising=False,
    )
    args = argparse.Namespace(command="bootstrap", actor="operator")
    with pytest.raises(ValueError, match="pgvector preflight failed"):
        await cli.run(args, "secret")
    rag.initialize_storages.assert_not_called()


async def test_pgvector_preflight_rejects_missing_extension_without_privilege(monkeypatch):
    class InsufficientPrivilegeError(Exception):
        pass

    class FakeTransaction:
        def __init__(self):
            self.rolled_back = False

        async def start(self):
            pass

        async def rollback(self):
            self.rolled_back = True

    class FakeConnection:
        def __init__(self):
            self.transaction_obj = FakeTransaction()
            self.closed = False

        async def fetchval(self, query):
            assert "pg_extension" in query
            return False

        def transaction(self):
            return self.transaction_obj

        async def execute(self, query):
            if query == "CREATE EXTENSION IF NOT EXISTS vector":
                raise InsufficientPrivilegeError

        async def close(self):
            self.closed = True

    fake_connection = FakeConnection()
    fake_asyncpg = types.SimpleNamespace(
        connect=AsyncMock(return_value=fake_connection),
        exceptions=types.SimpleNamespace(
            InsufficientPrivilegeError=InsufficientPrivilegeError
        ),
    )
    monkeypatch.setitem(__import__("sys").modules, "asyncpg", fake_asyncpg)

    with pytest.raises(ValueError, match="cannot create"):
        await cli._preflight_pgvector_extension(
            {
                "user": "app",
                "password": "secret",
                "database": "db",
                "host": "db.example",
                "port": 5432,
            }
        )
    assert fake_connection.transaction_obj.rolled_back is True
    assert fake_connection.closed is True


def test_bootstrap_requires_stopped_and_quiescent_confirmations(monkeypatch):
    monkeypatch.setattr(
        "sys.argv", ["coordination", "bootstrap", "--actor", "operator"]
    )
    with pytest.raises(SystemExit) as error:
        cli.main()
    assert error.value.code == 2


async def test_bootstrap_rejects_local_configuration_before_io(monkeypatch):
    rag = Mock(distributed_writes=False)
    monkeypatch.setattr(cli, "configured_rag", lambda: rag, raising=False)
    args = argparse.Namespace(command="bootstrap", actor="operator")
    with pytest.raises(ValueError, match="distributed"):
        await cli.run(args, "secret")
    rag.initialize_storages.assert_not_called()


@pytest.mark.integration
async def test_real_bootstrap_cli_from_server_environment(tmp_path, monkeypatch):
    import asyncio
    import os
    import sys
    from pathlib import Path
    from uuid import uuid4

    if not os.getenv("LIGHTRAG_COORDINATION_DSN") or not os.getenv(
        "POSTGRES_DATABASE", ""
    ).startswith("local_debug_"):
        pytest.skip("explicit isolated backend environment required")
    env = dict(os.environ)
    env.setdefault("POSTGRES_PASSWORD", "test")
    workspace = "local_debug_bootstrap_" + uuid4().hex
    env.update(
        {
            "PYTHONPATH": str(Path(__file__).resolve().parents[2]),
            "LIGHTRAG_DISTRIBUTED_WRITES": "true",
            "LIGHTRAG_SHARED_STORAGE": "true",
            "LIGHTRAG_DEPLOYMENT_ID": "local_debug_bootstrap_cli",
            "WORKSPACE": workspace,
            "POSTGRES_WORKSPACE": workspace,
            "WORKING_DIR": str(tmp_path / "working"),
            "INPUT_DIR": str(tmp_path / "inputs"),
            "LIGHTRAG_KV_STORAGE": "PGKVStorage",
            "LIGHTRAG_DOC_STATUS_STORAGE": "PGDocStatusStorage",
            "LIGHTRAG_VECTOR_STORAGE": "PGVectorStorage",
            "LIGHTRAG_GRAPH_STORAGE": "HugeGraphStorage",
            "LLM_BINDING": "openai",
            "LLM_MODEL": "fixture-not-called",
            "LLM_BINDING_API_KEY": "fixture-not-called",
            "EMBEDDING_BINDING": "openai",
            "EMBEDDING_MODEL": "runtime_test",
            "EMBEDDING_DIM": "3",
            "EMBEDDING_BINDING_API_KEY": "fixture-not-called",
            "HUGEGRAPH_GRAPH": "hugegraph",
            "HUGEGRAPH_GRAPHSPACE": "DEFAULT",
            "HUGEGRAPH_AUTO_CREATE_SCHEMA": "false",
            "WORKERS": "1",
        }
    )
    (tmp_path / ".env").write_text(
        "WORKSPACE=wrong\nEMBEDDING_DIM=9\nPOSTGRES_PASSWORD=\n"
    )
    migration = await asyncio.create_subprocess_exec(
        sys.executable,
        "-m",
        "lightrag.distributed",
        "migrate",
        cwd=tmp_path,
        env=env,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    _, migration_error = await migration.communicate()
    assert migration.returncode == 0, migration_error.decode()
    # Two real executions prove explicit first bootstrap and idempotent rerun;
    # constructor/lifecycle are production and no model request is made.
    for _ in range(2):
        process = await asyncio.create_subprocess_exec(
            sys.executable,
            "-m",
            "lightrag.distributed",
            "bootstrap",
            "--actor",
            "acceptance",
            "--confirm-writers-stopped",
            "--confirm-inflight-finished",
            cwd=tmp_path,
            env=env,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        out, err = await process.communicate()
        assert process.returncode == 0, err.decode()[-3000:]
        assert '"bootstrap": "verified"' in out.decode()
        assert env["LIGHTRAG_COORDINATION_DSN"] not in (out + err).decode()
