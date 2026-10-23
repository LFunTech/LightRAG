"""Explicit same-principal migration keeps all non-identity safety gates."""

from dataclasses import replace
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from tests.ci.test_prod_shared_credentials import KEY, approval, shared_environment
from tests.provisioning.test_plan import build


def test_shared_plan_includes_approval_without_changing_default_plan():
    plan = build(shared_environment())
    assert plan.credential_exception.as_dict() == approval()
    assert plan.summary()["credential_exception"] == approval()
    assert "credential_exception" not in build().summary()


def test_same_dsn_requires_approved_target_and_explicit_execution_confirmation():
    from lightrag.provisioning.__main__ import check_dsn

    values = shared_environment()
    dsn = "postgresql://dedicated:synthetic@pg.invalid:5432/prod?sslmode=disable"
    check_dsn(dsn, values, confirm_shared=True)
    for bad, env, confirmed in [
        (dsn, values, False),
        (dsn, {k: v for k, v in values.items() if k != KEY}, True),
        (dsn.replace("dedicated", "other"), values, True),
        (dsn.replace("pg.invalid", "other.invalid"), values, True),
        (dsn.replace("/prod?", "/other?"), values, True),
    ]:
        with pytest.raises(ValueError):
            check_dsn(bad, env, confirm_shared=confirmed)


@pytest.mark.parametrize(
    "row",
    [
        None,
        {"principal": "other", "database": "prod", "privileged": False},
        {"principal": "dedicated", "database": "other", "privileged": False},
        {"principal": "dedicated", "database": "prod", "privileged": True},
        {"principal": "dedicated", "database": "prod", "privileged": None},
    ],
)
async def test_shared_exception_never_grants_super_or_cross_database_access(row):
    from lightrag_delivery.credentials import from_environment

    policy = from_environment(shared_environment())
    with pytest.raises(ValueError):
        await policy.verify_principal(
            SimpleNamespace(fetchrow=AsyncMock(return_value=row)), "dedicated"
        )


async def test_privileged_session_cannot_impersonate_the_approved_runtime():
    from lightrag_delivery.credentials import from_environment

    row = {
        "principal": "dedicated",
        "session_principal": "admin",
        "database": "prod",
        "privileged": False,
    }
    with pytest.raises(ValueError):
        await from_environment(shared_environment()).verify_principal(
            SimpleNamespace(fetchrow=AsyncMock(return_value=row)), "dedicated"
        )


async def test_shared_business_and_release_audits_still_verify_exact_database_identity():
    from lightrag.provisioning.postgres import audit_runtime_role
    from lightrag.distributed.release.migrations import verify_runtime_role
    from lightrag_delivery.credentials import from_environment

    policy = from_environment(shared_environment())
    connection = SimpleNamespace(
        fetchrow=AsyncMock(
            return_value={
                "principal": "dedicated",
                "session_principal": "dedicated",
                "database": "prod",
                "privileged": False,
            }
        ),
        execute=AsyncMock(),
        fetchval=AsyncMock(side_effect=AssertionError("unapproved bypass")),
    )
    await audit_runtime_role(connection, "dedicated", credential_exception=policy)
    await verify_runtime_role(connection, "dedicated", credential_exception=policy)
    assert connection.fetchrow.await_count == 2
    assert connection.execute.await_count == 0


def test_initialization_job_preserves_explicit_shared_execution_confirmation():
    from lightrag.provisioning.job import render_job
    from lightrag.provisioning.plan import digest
    from tests.provisioning.test_job import settings

    values = dict(shared_environment(), LIGHTRAG_OBJECT_STORAGE="s3")
    plan = build(values)
    configured = settings()
    configured.update(
        confirm_shared_credentials=True,
        expected_profile_hash=plan.profile_hash,
        expected_plan_hash=digest(plan.summary()),
    )
    job = render_job(configured, values)
    args = job["spec"]["template"]["spec"]["containers"][0]["args"]
    assert "--confirm-shared-credentials" in args
    assert "--confirm-maintenance" in args and "--confirm-empty-target" in args
    assert "lightrag-server" not in json.dumps(job)


def test_shared_cli_requires_extra_flag_before_dispatch(tmp_path, monkeypatch, capsys):
    from lightrag.provisioning import __main__ as cli
    from lightrag.provisioning.plan import digest

    values = dict(shared_environment(), HUGEGRAPH_PASSWORD="synthetic")
    path = tmp_path / "environment.json"
    path.write_text(json.dumps(values))
    plan = build(values)
    args = [
        "apply",
        "--environment",
        str(path),
        "--confirm-maintenance",
        "--confirm-empty-target",
        "--expected-profile-hash",
        plan.profile_hash,
        "--expected-plan-hash",
        digest(plan.summary()),
        "--graph-wait-seconds",
        "1",
    ]
    monkeypatch.setenv(
        "LIGHTRAG_STORAGE_MIGRATION_DSN",
        "postgresql://dedicated:synthetic@pg.invalid:5432/prod?sslmode=disable",
    )
    dispatched = AsyncMock(return_value={"migrated": True})
    monkeypatch.setattr(cli, "run", dispatched)
    assert cli.main(args) == 2 and dispatched.await_count == 0
    assert cli.main(args + ["--confirm-shared-credentials"]) == 0
    assert dispatched.await_count == 1
    assert dispatched.call_args.args[2].credential_exception is not None
    output = capsys.readouterr()
    assert "synthetic" not in output.out + output.err


async def test_shared_graph_cli_uses_explicit_matching_credentials_and_no_fallback(
    monkeypatch,
):
    from contextlib import asynccontextmanager
    import asyncpg
    from lightrag.provisioning import __main__ as cli
    from lightrag.provisioning import graph as graph_module, engine

    values = dict(shared_environment(), HUGEGRAPH_PASSWORD="synthetic")
    plan = build(values)
    args = SimpleNamespace(
        command="apply",
        graph_wait_seconds=1,
        confirm_shared_credentials=True,
        confirm_empty_target=True,
    )
    monkeypatch.setenv(
        "LIGHTRAG_GRAPH_MIGRATION_USERNAME", values["HUGEGRAPH_USERNAME"]
    )
    monkeypatch.setenv(
        "LIGHTRAG_GRAPH_MIGRATION_PASSWORD", values["HUGEGRAPH_PASSWORD"]
    )
    seen = []

    @asynccontextmanager
    async def fake_graph(*a, **k):
        seen.append(a[4:6])
        yield object()

    monkeypatch.setattr(graph_module, "GraphProvisioner", fake_graph)
    conn = SimpleNamespace(close=AsyncMock())
    connect = AsyncMock(return_value=conn)
    monkeypatch.setattr(asyncpg, "connect", connect)
    apply = AsyncMock()
    monkeypatch.setattr(engine, "apply_all", apply)
    result = await cli.run(args, values, plan, "explicit-dsn")
    assert result["migrated"] is True
    assert seen == [(values["HUGEGRAPH_USERNAME"], values["HUGEGRAPH_PASSWORD"])]
    assert apply.call_args.args[1].credential_exception == plan.credential_exception
    monkeypatch.delenv("LIGHTRAG_GRAPH_MIGRATION_USERNAME")
    with pytest.raises(ValueError):
        await cli.run(args, values, plan, "explicit-dsn")
    assert connect.await_count == 1


@pytest.mark.integration
async def test_real_shared_owner_migration_verify_and_registration(migration_db):
    import asyncpg
    from uuid import uuid4
    from lightrag.provisioning.engine import apply_all, verify_all, register_workspace
    from lightrag.provisioning.manifest import manifest_hash
    from lightrag.distributed.release.migrations import verify_runtime_role
    from tests.provisioning.test_graph import GraphServer
    from lightrag.provisioning.graph import GraphProvisioner
    import httpx

    role = "migration_owner_" + uuid4().hex[:10]
    db = await migration_db.fetchval("SELECT current_database()")
    await migration_db.execute(
        f'CREATE ROLE "{role}" LOGIN NOSUPERUSER NOCREATEROLE NOCREATEDB NOREPLICATION NOBYPASSRLS'
    )
    await migration_db.execute(f'GRANT CREATE ON DATABASE "{db}" TO "{role}"')
    await migration_db.execute(f'GRANT CREATE ON SCHEMA public TO "{role}"')
    await migration_db.execute("CREATE EXTENSION IF NOT EXISTS vector")
    import os
    from urllib.parse import urlsplit

    target = urlsplit(os.environ["LIGHTRAG_PROVISIONING_TEST_DSN"])
    conn = await asyncpg.connect(
        host=target.hostname, port=target.port, database=db, user=role
    )
    values = dict(
        shared_environment(),
        POSTGRES_USER=role,
        POSTGRES_DATABASE=db,
        LIGHTRAG_OBJECT_STORAGE="s3",
    )
    item = approval(values)
    item["postgres_user"] = role
    values[KEY] = json.dumps(item)
    plan = build(values)
    server = GraphServer()
    try:
        async with GraphProvisioner(
            plan,
            values["HUGEGRAPH_URI"],
            values["HUGEGRAPH_GRAPHSPACE"],
            values["HUGEGRAPH_GRAPH"],
            values["HUGEGRAPH_USERNAME"],
            "synthetic",
            wait_seconds=1,
            client=httpx.AsyncClient(transport=httpx.MockTransport(server)),
        ) as g:
            with pytest.raises(ValueError):
                await apply_all(
                    conn,
                    replace(plan, credential_exception=None),
                    g,
                    role,
                    confirm_empty=True,
                )
            await apply_all(conn, plan, g, role, confirm_empty=True)
            await apply_all(conn, plan, g, role, confirm_empty=True)
            await verify_all(conn, plan, g, role)
            result = await register_workspace(
                conn, plan, g, values, expected_manifest_hash=manifest_hash(values)
            )
            assert result["registered"] is True and result["release_enrolled"] is False
            assert len(server.posts) == 7
            async with conn.transaction(readonly=True):
                await verify_all(conn, plan, g, role)
            with pytest.raises(ValueError):
                await verify_runtime_role(conn, role)
            assert not await conn.fetchval(
                "SELECT rolcreaterole FROM pg_roles WHERE rolname=current_user"
            )
            from lightrag.distributed.release.store import ReleaseStore
            from lightrag.distributed.release.config import ReleaseConfig
            from lightrag.distributed.release.runtime import ReleaseRuntime

            store = ReleaseStore(conn)
            await store.onboard(
                "prod/prod",
                active_slot="blue",
                profile_hash=plan.profile_hash,
                config_hash="a" * 64,
                policy={
                    "queue_capacity": 10,
                    "first_release_credential_exception": plan.credential_exception.as_dict(),
                },
            )
            await apply_all(conn, plan, g, role, confirm_empty=True)
            assert len(server.posts) == 7
            runtime = SimpleNamespace(
                coordinator=SimpleNamespace(_pool=conn, _owner_id=uuid4()),
                release_require_privileges=True,
            )
            config = ReleaseConfig(
                "prod/prod",
                "blue",
                "first-pod",
                "a" * 64,
                plan.profile_hash,
                credential_exception=plan.credential_exception,
            )
            instance = ReleaseRuntime(runtime, SimpleNamespace(), config)
            await instance.initialize()
            assert instance.registered is True
            assert (
                await conn.fetchval("SELECT count(*) FROM lightrag_release.instances")
                == 1
            )
            await instance.initialize()
            await conn.execute("UPDATE lightrag_release.instances SET closed=true")
            await conn.execute(
                "UPDATE lightrag_release.scopes SET epoch=2,phase='COMPLETED',active_slot='green'"
            )
            with pytest.raises(ValueError, match="First-release"):
                await apply_all(conn, plan, g, role, confirm_empty=True)
            assert len(server.posts) == 7
            await conn.execute(
                "UPDATE lightrag_release.scopes SET epoch=1,phase='IDLE',active_slot='blue',policy=policy-'first_release_credential_exception'"
            )
            with pytest.raises(ValueError):
                await instance.refresh()
    finally:
        await conn.close()


@pytest.mark.parametrize(
    "entrypoint", ["apply", "postgres", "register", "grant", "graph"]
)
@pytest.mark.parametrize("drift", ["later_epoch", "green", "approval"])
async def test_shared_initialization_refuses_persistent_drift_before_side_effects(
    monkeypatch, entrypoint, drift
):
    from contextlib import asynccontextmanager
    from lightrag.provisioning import engine, postgres
    from lightrag.provisioning.graph import GraphProvisioner
    from lightrag.provisioning.manifest import manifest_hash

    values = shared_environment()
    plan = build(values)
    state = dict(
        scope=plan.credential_exception.scope,
        profile_hash=plan.profile_hash,
        active_slot="blue",
        epoch=1,
        phase="IDLE",
        owner=None,
        gate_open=True,
        policy={
            "first_release_credential_exception": plan.credential_exception.as_dict()
        },
    )
    if drift == "later_epoch":
        state.update(epoch=2, phase="COMPLETED")
    elif drift == "green":
        state["active_slot"] = "green"
    else:
        state["policy"]["first_release_credential_exception"]["approval_id"] = (
            "different"
        )

    async def fetchval(sql, *args):
        if (
            "pg_try_advisory_lock" in sql
            or "to_regclass('lightrag_release.scopes')" in sql
        ):
            return True
        raise AssertionError(
            "Initialization continued past invalid first-release state"
        )

    @asynccontextmanager
    async def transaction():
        yield

    connection = SimpleNamespace(
        fetchval=fetchval,
        fetchrow=AsyncMock(return_value=state),
        execute=AsyncMock(),
        transaction=transaction,
        is_in_transaction=lambda: False,
    )
    monkeypatch.setattr(postgres, "audit_runtime_role", AsyncMock())
    graph = SimpleNamespace(
        preflight=AsyncMock(), apply=AsyncMock(), verify=AsyncMock()
    )
    with pytest.raises(ValueError, match="First-release"):
        if entrypoint == "apply":
            await engine.apply_all(
                connection, plan, graph, "dedicated", confirm_empty=True
            )
        elif entrypoint == "postgres":
            await postgres.migrate(connection, plan, "dedicated", confirm_empty=True)
        elif entrypoint == "register":
            await engine.register_workspace(
                connection,
                plan,
                graph,
                values,
                expected_manifest_hash=manifest_hash(values),
            )
        elif entrypoint == "grant":
            await postgres.grant_runtime(connection, plan, "dedicated")
        else:
            graph = GraphProvisioner(
                plan,
                values["HUGEGRAPH_URI"],
                values["HUGEGRAPH_GRAPHSPACE"],
                values["HUGEGRAPH_GRAPH"],
                values["HUGEGRAPH_USERNAME"],
                "synthetic",
                wait_seconds=1,
            )
            async with graph:
                await graph.apply(connection)
    assert not any(
        any(
            keyword in call.args[0]
            for keyword in ("GRANT", "CREATE", "INSERT", "UPDATE")
        )
        for call in connection.execute.call_args_list
    )


@pytest.mark.parametrize("phase", ["apply", "register"])
@pytest.mark.parametrize("confirmation", [None, False])
def test_shared_job_cannot_infer_execution_confirmation(phase, confirmation):
    from lightrag.provisioning.job import render_job
    from lightrag.provisioning.plan import digest
    from lightrag.provisioning.manifest import manifest_hash
    from tests.provisioning.test_job import settings

    values = dict(shared_environment(), LIGHTRAG_OBJECT_STORAGE="s3")
    plan = build(values)
    configured = dict(
        settings(),
        phase=phase,
        expected_profile_hash=plan.profile_hash,
        expected_plan_hash=digest(plan.summary()),
        expected_manifest_hash=manifest_hash(values),
    )
    if confirmation is not None:
        configured["confirm_shared_credentials"] = confirmation
    with pytest.raises(ValueError, match="[Ss]hared"):
        render_job(configured, values)


@pytest.mark.parametrize("confirmation", ["true", 1, None])
def test_job_requires_boolean_shared_confirmation(confirmation):
    from lightrag.provisioning.job import render_job
    from tests.provisioning.test_job import settings

    values = dict(shared_environment(), LIGHTRAG_OBJECT_STORAGE="s3")
    configured = dict(settings(), confirm_shared_credentials=confirmation)
    with pytest.raises(ValueError, match="boolean shared"):
        render_job(configured, values)


def test_shared_verify_job_is_readonly_without_execution_confirmation():
    from lightrag.provisioning.job import render_job
    from lightrag.provisioning.plan import digest
    from tests.provisioning.test_job import settings

    values = dict(shared_environment(), LIGHTRAG_OBJECT_STORAGE="s3")
    plan = build(values)
    configured = dict(
        settings(),
        phase="verify",
        confirm_maintenance=False,
        confirm_empty_target=False,
        expected_profile_hash=plan.profile_hash,
        expected_plan_hash=digest(plan.summary()),
    )
    job = render_job(configured, values)
    args = job["spec"]["template"]["spec"]["containers"][0]["args"]
    assert "--confirm-shared-credentials" not in args
    assert "--confirm-maintenance" not in args


def test_job_cannot_confirm_an_unapproved_exception():
    from lightrag.provisioning.job import render_job
    from tests.provisioning.test_job import settings
    from tests.provisioning.test_plan import environment

    with pytest.raises(ValueError, match="approved exception"):
        render_job(
            dict(settings(), confirm_shared_credentials=True),
            dict(environment(), LIGHTRAG_OBJECT_STORAGE="s3"),
        )
