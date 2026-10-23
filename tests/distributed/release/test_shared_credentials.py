"""First Blue accepts audited ownership, never another slot or release."""

from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from tests.ci.test_prod_shared_credentials import shared_environment


def policy():
    from lightrag_delivery.credentials import from_environment

    return from_environment(shared_environment())


def initial_state():
    item = policy()
    return dict(
        scope=item.scope,
        profile_hash=item.profile_hash,
        active_slot="blue",
        epoch=1,
        phase="IDLE",
        owner=None,
        gate_open=True,
        policy={
            "queue_capacity": 10,
            "first_release_credential_exception": item.as_dict(),
        },
    )


def runtime_environment():
    return dict(
        shared_environment(),
        LIGHTRAG_RELEASE_ENABLED="true",
        LIGHTRAG_DISTRIBUTED_WRITES="true",
        LIGHTRAG_RELEASE_SLOT="blue",
        LIGHTRAG_RELEASE_INSTANCE_ID="pod-1",
        WORKERS="1",
        LIGHTRAG_RELEASE_CONFIG_HASH="a" * 64,
        LIGHTRAG_RELEASE_PROFILE_HASH=policy().profile_hash,
    )


def test_release_configuration_accepts_only_pinned_first_blue(monkeypatch):
    from lightrag.distributed.release.config import ReleaseConfig

    for k, v in runtime_environment().items():
        monkeypatch.setenv(k, v)
    assert ReleaseConfig.from_env().credential_exception == policy()
    monkeypatch.setenv("LIGHTRAG_RELEASE_SLOT", "green")
    with pytest.raises(ValueError):
        ReleaseConfig.from_env()


@pytest.mark.parametrize(
    "field,value",
    [
        ("active_slot", "green"),
        ("epoch", 2),
        ("phase", "PREPARED"),
        ("owner", "another"),
        ("gate_open", False),
        ("policy", {}),
        ("scope", "other/prod"),
        ("profile_hash", "c" * 64),
    ],
)
async def test_shared_runtime_refuses_before_registering_if_persistent_approval_differs(
    monkeypatch, field, value
):
    from lightrag.distributed.release import runtime as module

    state = initial_state()
    state[field] = value

    @asynccontextmanager
    async def connection():
        yield SimpleNamespace()

    store = SimpleNamespace(
        verify_schema=AsyncMock(),
        connection=connection,
        _scope=AsyncMock(return_value=state),
        register=AsyncMock(),
    )
    monkeypatch.setattr(module, "RuntimeStore", lambda *a, **k: store)
    base = SimpleNamespace(
        coordinator=SimpleNamespace(_pool=object(), _owner_id=object()),
        release_require_privileges=True,
    )
    config = SimpleNamespace(
        scope=policy().scope,
        instance_id="pod-1",
        slot="blue",
        config_hash="a" * 64,
        profile_hash=policy().profile_hash,
        credential_exception=policy(),
    )
    instance = module.ReleaseRuntime(base, SimpleNamespace(), config)
    with pytest.raises(ValueError):
        await instance.initialize()
    assert store.register.await_count == 0


async def test_raw_release_store_refuses_upgrade_of_shared_owner_baseline():
    from lightrag.distributed.release.store import ReleaseStore
    from lightrag.distributed.release.models import ReleaseConflict, ReleaseIdentity

    @asynccontextmanager
    async def transaction():
        yield

    connection = SimpleNamespace(
        transaction=transaction,
        execute=AsyncMock(),
        fetchrow=AsyncMock(
            side_effect=AssertionError("upgrade continued past shared scope")
        ),
    )
    store = ReleaseStore(connection)
    store._scope = AsyncMock(return_value=initial_state())
    identity = ReleaseIdentity(
        policy().scope, "next", "v1.5.10", "a" * 40, "sha256:" + "b" * 64, "c" * 64
    )
    with pytest.raises(ReleaseConflict):
        await store.begin(identity, expected_epoch=1)
    assert connection.execute.await_count == 0


def test_shared_scope_policy_requires_first_epoch_even_on_direct_enrollment():
    item = policy()
    item.verify_scope_state(initial_state())
    changed = initial_state()
    changed["policy"]["first_release_credential_exception"]["approval_id"] = "different"
    with pytest.raises(ValueError):
        item.verify_scope_state(changed)


@pytest.mark.parametrize("changed", ["host", "port"])
async def test_enrollment_checks_physical_operator_target_before_connecting(
    monkeypatch, tmp_path, changed
):
    import json
    import asyncpg
    from lightrag.distributed.release import __main__ as cli
    from tests.ci.test_prod_shared_credentials import configured_shared
    from lightrag_delivery.profile import physical_profile_hash
    from dataclasses import replace
    from lightrag_delivery.credentials import ENV_KEY
    import lightrag_delivery.config as config_module

    values = shared_environment()
    config, _ = configured_shared(tmp_path)
    item = policy().as_dict()
    config = replace(
        config,
        target=dict(
            config.target,
            deployment_id="prod",
            workspace="prod",
            postgres_database="prod",
        ),
        onboarding=dict(
            config.onboarding,
            runtime_role="dedicated",
            profile_hash=physical_profile_hash(values),
            first_release_credential_exception=item,
        ),
    )
    monkeypatch.setattr(config_module, "load_prod_config", lambda path: config)
    assert json.loads(values[ENV_KEY]) == item
    path = tmp_path / "environment.json"
    path.write_text(json.dumps(values))
    dsn = "postgresql://dedicated:synthetic@pg.invalid:5432/prod?sslmode=disable"
    dsn = (
        dsn.replace("pg.invalid", "other.invalid")
        if changed == "host"
        else dsn.replace(":5432/", ":5433/")
    )
    monkeypatch.setenv("LIGHTRAG_RELEASE_OPERATOR_DSN", dsn)
    connect = AsyncMock(side_effect=AssertionError("Wrong target connection attempted"))
    monkeypatch.setattr(asyncpg, "connect", connect)
    args = cli.parser().parse_args(
        [
            "enroll",
            "--config",
            "unused",
            "--environment",
            str(path),
            "--active-slot",
            "blue",
            "--confirm-maintenance",
        ]
    )
    with pytest.raises(ValueError):
        await cli.run(args)
    assert connect.await_count == 0


async def test_enrollment_persists_approved_policy_on_the_exact_operator_target(
    monkeypatch, tmp_path
):
    import json
    import asyncpg
    from dataclasses import replace
    from lightrag.distributed.release import __main__ as cli
    import lightrag_delivery.config as config_module
    from tests.ci.test_prod_shared_credentials import configured_shared

    values = shared_environment()
    config, _ = configured_shared(tmp_path)
    item = policy().as_dict()
    config = replace(
        config,
        target=dict(
            config.target,
            deployment_id="prod",
            workspace="prod",
            postgres_database="prod",
        ),
        onboarding=dict(
            config.onboarding,
            profile_hash=item["profile_hash"],
            first_release_credential_exception=item,
        ),
    )
    monkeypatch.setattr(config_module, "load_prod_config", lambda path: config)
    path = tmp_path / "environment.json"
    path.write_text(json.dumps(values))
    dsn = "postgresql://dedicated:synthetic@pg.invalid:5432/prod?sslmode=disable"
    monkeypatch.setenv("LIGHTRAG_RELEASE_OPERATOR_DSN", dsn)

    @asynccontextmanager
    async def transaction():
        yield

    async def fetchrow(sql, *args):
        if "current_user AS principal" in sql:
            return dict(
                principal="dedicated",
                session_principal="dedicated",
                database="prod",
                privileged=False,
            )
        assert "SELECT manifest_hash,fenced" in sql
        return dict(
            manifest_hash=config.onboarding["baseline_manifest_hash"], fenced=False
        )

    connection = SimpleNamespace(
        transaction=transaction,
        fetchrow=fetchrow,
        fetchval=AsyncMock(return_value=False),
        close=AsyncMock(),
    )
    connect = AsyncMock(return_value=connection)
    monkeypatch.setattr(asyncpg, "connect", connect)
    store = SimpleNamespace(
        verify_schema=AsyncMock(),
        onboard=AsyncMock(),
        snapshot=AsyncMock(return_value={"enrolled": True}),
    )
    monkeypatch.setattr(cli, "ReleaseStore", lambda conn: store)
    args = cli.parser().parse_args(
        [
            "enroll",
            "--config",
            "unused",
            "--environment",
            str(path),
            "--active-slot",
            "blue",
            "--confirm-maintenance",
        ]
    )
    assert await cli.run(args) == {"enrolled": True}
    assert connect.call_args.args == (dsn,)
    assert (
        store.onboard.call_args.kwargs["policy"]["first_release_credential_exception"]
        == item
    )
    assert connection.close.await_count == 1
