import pytest
from .test_plan import build, environment
from .test_graph import GraphServer, graph


@pytest.mark.integration
async def test_complete_initialization_is_versioned_but_does_not_register_or_enroll(
    migration_db,
):
    from lightrag.provisioning.engine import apply_all, verify_all

    server = GraphServer()
    async with graph(server) as g:
        await apply_all(
            migration_db, build(), g, "migration_test_app", confirm_empty=True
        )
        await apply_all(
            migration_db, build(), g, "migration_test_app", confirm_empty=True
        )
        await verify_all(migration_db, build(), g, "migration_test_app")
        assert (
            await migration_db.fetchval(
                "SELECT count(*) FROM lightrag_coordination.workspaces"
            )
            == 0
        )
        assert (
            await migration_db.fetchval("SELECT count(*) FROM lightrag_release.scopes")
            == 0
        )
        assert len(server.posts) == 7
        await migration_db.execute("SET ROLE migration_test_app")
        try:
            await verify_all(migration_db, build(), g, "migration_test_app")
            assert not await migration_db.fetchval(
                "SELECT has_table_privilege(current_user,'lightrag_storage.migrations','UPDATE')"
            )
        finally:
            await migration_db.execute("RESET ROLE")


@pytest.mark.integration
async def test_runtime_ledger_write_privilege_is_rejected(migration_db):
    from lightrag.provisioning.postgres import migrate, audit_runtime_role

    await migrate(migration_db, build(), "migration_test_app", confirm_empty=True)
    await migration_db.execute(
        "GRANT UPDATE ON lightrag_storage.migrations TO migration_test_app"
    )
    with pytest.raises(ValueError):
        await audit_runtime_role(migration_db, "migration_test_app", operator=False)


@pytest.mark.integration
async def test_scope_registration_is_separate_and_never_waits_for_graph_in_pg_transaction(
    migration_db,
):
    from lightrag.provisioning.engine import apply_all, register_workspace
    from lightrag.provisioning.manifest import manifest_hash

    server = GraphServer()
    values = dict(
        environment(), POSTGRES_USER="migration_test_app", LIGHTRAG_OBJECT_STORAGE="s3"
    )
    async with graph(server) as g:
        await apply_all(
            migration_db, build(), g, "migration_test_app", confirm_empty=True
        )

        verify_empty = g.verify_empty_scope

        async def empty(scope):
            assert scope == "prod"
            assert not migration_db.is_in_transaction(), (
                "Graph I/O must precede the registration transaction"
            )
            await verify_empty(scope)

        g.verify_empty_scope = empty
        result = await register_workspace(
            migration_db,
            build(),
            g,
            values,
            expected_manifest_hash=manifest_hash(values),
        )
        assert result["registered"] is True and result["release_enrolled"] is False
        assert await migration_db.fetchval(
            "SELECT manifest_hash FROM lightrag_coordination.workspaces"
        ) == manifest_hash(values)
        assert (
            await migration_db.fetchval("SELECT count(*) FROM lightrag_release.scopes")
            == 0
        )


@pytest.mark.integration
@pytest.mark.parametrize(
    "grant",
    [
        "GRANT CREATE ON SCHEMA lightrag_storage TO migration_test_app",
        "GRANT UPDATE ON lightrag_coordination.schema_version TO migration_test_app",
        "REVOKE DELETE ON public.lightrag_doc_status FROM migration_test_app",
    ],
)
async def test_verification_rejects_unsafe_or_missing_runtime_permissions(
    migration_db, grant
):
    from lightrag.provisioning.engine import apply_all, verify_all

    async with graph(GraphServer()) as g:
        await apply_all(
            migration_db, build(), g, "migration_test_app", confirm_empty=True
        )
        await migration_db.execute(grant)
        with pytest.raises(ValueError):
            await verify_all(migration_db, build(), g, "migration_test_app")


@pytest.mark.integration
async def test_second_migration_connection_refuses_active_migrator(migration_db):
    import asyncpg
    from lightrag.provisioning.postgres import migration_lock

    # Use the exact temporary connection target, never a default DSN.
    import os
    from urllib.parse import urlsplit, urlunsplit

    target = urlsplit(os.environ["LIGHTRAG_PROVISIONING_TEST_DSN"])
    target = target._replace(
        path="/" + await migration_db.fetchval("SELECT current_database()")
    )
    peer = await asyncpg.connect(urlunsplit(target))
    try:
        async with migration_lock(migration_db):
            with pytest.raises(ValueError, match="active"):
                async with migration_lock(peer):
                    pass
        async with migration_lock(peer):
            pass
    finally:
        await peer.close()


@pytest.mark.integration
async def test_real_runtime_identity_requires_registered_baseline_and_does_only_reads(
    migration_db,
):
    from types import SimpleNamespace
    import os
    from urllib.parse import urlsplit
    from lightrag.provisioning.engine import apply_all, register_workspace
    from lightrag.provisioning.manifest import manifest_hash
    from lightrag.provisioning.runtime import verify_runtime_storage
    from lightrag.kg.postgres_impl import ClientManager

    target = urlsplit(os.environ["LIGHTRAG_PROVISIONING_TEST_DSN"])
    database = await migration_db.fetchval("SELECT current_database()")
    values = dict(
        environment(), POSTGRES_USER="migration_test_app", LIGHTRAG_OBJECT_STORAGE="s3"
    )
    config = ClientManager.get_config(vector_storage="PGVectorStorage")
    config.update(
        user="migration_test_app",
        password="unused-in-loopback-trust",
        host=target.hostname,
        port=target.port,
        database=database,
        ssl_mode="disable",
    )
    runtime = SimpleNamespace(
        pg_config=config,
        storage_migration_plan=build(),
        coordinator=SimpleNamespace(
            _scope=build().scope, _manifest_hash=manifest_hash(values)
        ),
    )
    async with graph(GraphServer()) as g:
        await apply_all(
            migration_db, build(), g, "migration_test_app", confirm_empty=True
        )
        with pytest.raises(ValueError, match="registration"):
            await verify_runtime_storage(runtime)

        await register_workspace(
            migration_db,
            build(),
            g,
            values,
            expected_manifest_hash=manifest_hash(values),
        )
        await verify_runtime_storage(runtime)
    assert (
        await migration_db.fetchval(
            "SELECT count(*) FROM lightrag_coordination.operations"
        )
        == 0
    )
    assert (
        await migration_db.fetchval("SELECT count(*) FROM lightrag_release.scopes") == 0
    )
