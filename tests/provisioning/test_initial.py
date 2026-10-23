"""The initial Job executes explicit phases once and preserves unknown outcomes."""

import json
from unittest.mock import AsyncMock

import pytest

from tests.ci.test_prod_initial import prepared_initial


@pytest.mark.parametrize("failed_phase", [None, "apply", "verify", "register"])
async def test_initial_job_calls_phases_once_in_order(
    tmp_path, monkeypatch, failed_phase
):
    from lightrag.provisioning import initial, __main__ as provision
    from lightrag.distributed.release import __main__ as release

    _, config, identity, runtime, settings, _, _ = prepared_initial(tmp_path)
    paths = []
    for name, value in [
        ("config", config.__dict__),
        ("identity", identity),
        ("environment", runtime),
        ("settings", settings),
    ]:
        p = tmp_path / (name + ".json")
        p.write_text(json.dumps(value))
        paths.append(p)
    for key, value in dict(
        LIGHTRAG_STORAGE_MIGRATION_DSN="postgresql://operator:synthetic@pg.invalid:5432/prod?sslmode=require",
        LIGHTRAG_RELEASE_OPERATOR_DSN="postgresql://operator:synthetic@pg.invalid:5432/prod?sslmode=require",
        LIGHTRAG_GRAPH_MIGRATION_USERNAME="graph_operator",
        LIGHTRAG_GRAPH_MIGRATION_PASSWORD="synthetic",
    ).items():
        monkeypatch.setenv(key, value)
    phases = []

    async def invoke(args, *a):
        phases.append(args.command)
        if args.command == failed_phase:
            raise ValueError("Uncertain fixture result")
        return {}

    monkeypatch.setattr(provision, "run", invoke)
    enroll = AsyncMock(
        return_value=dict(
            active_slot="blue", epoch=1, phase="IDLE", gate_open=True, owner=None
        )
    )
    monkeypatch.setattr(release, "run", enroll)
    if failed_phase:
        with pytest.raises(ValueError):
            await initial.run(*paths)
        assert (
            phases
            == ["apply", "verify", "register"][
                : ["apply", "verify", "register"].index(failed_phase) + 1
            ]
        )
        assert enroll.await_count == 0
    else:
        result = await initial.run(*paths)
        assert phases == ["apply", "verify", "register"] and enroll.await_count == 1
        assert (
            result["phase"] == "INITIALIZED"
            and result["config_hash"] == identity["config_hash"]
        )


@pytest.mark.integration
async def test_initial_job_real_pg_runs_all_four_phases_and_repeats_safely(
    migration_db, tmp_path, monkeypatch
):
    from dataclasses import replace
    from uuid import uuid4
    from urllib.parse import urlsplit
    import os
    import httpx
    from lightrag.provisioning import initial, graph as graph_module
    from lightrag.provisioning.plan import build_plan, digest
    from lightrag.provisioning.manifest import manifest_hash
    from lightrag_delivery.render import content_hash
    from lightrag_delivery.profile import physical_profile_hash
    from lightrag_delivery.credentials import ENV_KEY
    import lightrag_delivery.config as config_module
    from tests.provisioning.test_shared_credentials import shared_environment, approval
    from tests.provisioning.test_graph import GraphServer

    _, base, _, _, _, _, _ = prepared_initial(tmp_path)
    role = "migration_initial_" + uuid4().hex[:10]
    database = await migration_db.fetchval("SELECT current_database()")
    await migration_db.execute(
        f'CREATE ROLE "{role}" LOGIN NOSUPERUSER NOCREATEROLE NOCREATEDB NOREPLICATION NOBYPASSRLS'
    )
    await migration_db.execute(f'GRANT CREATE ON DATABASE "{database}" TO "{role}"')
    await migration_db.execute(f'GRANT CREATE ON SCHEMA public TO "{role}"')
    await migration_db.execute("CREATE EXTENSION IF NOT EXISTS vector")
    target = urlsplit(os.environ["LIGHTRAG_PROVISIONING_TEST_DSN"])
    values = dict(
        shared_environment(),
        **base.runtime,
        POSTGRES_HOST=target.hostname,
        POSTGRES_PORT=str(target.port),
        POSTGRES_DATABASE=database,
        POSTGRES_USER=role,
        HUGEGRAPH_PASSWORD="synthetic",
        LIGHTRAG_OBJECT_STORAGE="s3",
    )
    item = approval(values)
    item["postgres_user"] = role
    values[ENV_KEY] = json.dumps(item)
    profile = physical_profile_hash(values)
    config = replace(
        base,
        target=dict(
            base.target,
            deployment_id="prod",
            workspace="prod",
            postgres_database=database,
        ),
        onboarding=dict(
            base.onboarding,
            runtime_role=role,
            profile_hash=profile,
            baseline_manifest_hash=manifest_hash(values),
            first_release_credential_exception=item,
        ),
    )
    identity = dict(
        scope="prod/prod",
        release_id="initial-pg-fixture",
        tag=item["release_tag"],
        config_hash=content_hash(
            {"configuration": config.config_hash, "environment": values}
        ),
    )
    plan = build_plan(values)
    settings = dict(
        version=1,
        approval_id="local-initial-fixture",
        confirm_maintenance=True,
        confirm_empty_target=True,
        confirm_shared_credentials=True,
        expected_plan_hash=digest(plan.summary()),
        graph_wait_seconds=1,
        job_wait_seconds=600,
        startup_wait_seconds=600,
    )
    paths = []
    for name, value in [
        ("config", config.__dict__),
        ("identity", identity),
        ("environment", values),
        ("settings", settings),
    ]:
        p = tmp_path / (name + ".json")
        p.write_text(json.dumps(value))
        paths.append(p)
    # Keep the real CLI/engine paths; only permit the guarded local fixture DB name.
    monkeypatch.setattr(initial, "load_prod_config", lambda path: config)
    monkeypatch.setattr(config_module, "load_prod_config", lambda path: config)
    server = GraphServer()
    graph_class = graph_module.GraphProvisioner

    def graph(*args, **kwargs):
        return graph_class(
            *args,
            **kwargs,
            client=httpx.AsyncClient(transport=httpx.MockTransport(server)),
        )

    monkeypatch.setattr(graph_module, "GraphProvisioner", graph)
    dsn = f"postgresql://{role}@{target.hostname}:{target.port}/{database}?sslmode=disable"
    for key, value in dict(
        LIGHTRAG_STORAGE_MIGRATION_DSN=dsn,
        LIGHTRAG_RELEASE_OPERATOR_DSN=dsn,
        LIGHTRAG_GRAPH_MIGRATION_USERNAME=values["HUGEGRAPH_USERNAME"],
        LIGHTRAG_GRAPH_MIGRATION_PASSWORD="synthetic",
    ).items():
        monkeypatch.setenv(key, value)
    result = await initial.run(*paths)
    assert result["phase"] == "INITIALIZED" and result["epoch"] == 1
    state = await migration_db.fetchrow(
        "SELECT active_slot,epoch,config_hash,policy FROM lightrag_release.scopes WHERE scope='prod/prod'"
    )
    assert (
        state["active_slot"] == "blue"
        and state["config_hash"] == identity["config_hash"]
    )
    assert json.loads(state["policy"])["first_release_credential_exception"] == item
    assert await initial.run(*paths) == result
    assert len(server.posts) == 7
