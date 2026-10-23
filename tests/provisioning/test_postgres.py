import importlib.util

import pytest
from .test_plan import build


def module():
    assert importlib.util.find_spec("lightrag.provisioning.postgres"), (
        "Transactional storage migration is missing"
    )
    from lightrag.provisioning import postgres

    return postgres


@pytest.mark.integration
async def test_fresh_migration_creates_versioned_tables_and_second_apply_only_verifies(
    migration_db,
):
    m = module()
    plan = build()
    await m.migrate(migration_db, plan, "migration_test_app", confirm_empty=True)
    assert (
        await migration_db.fetchval(
            "SELECT count(*) FROM pg_tables WHERE schemaname='public' AND tablename LIKE 'lightrag%'"
        )
        == 12
    )
    assert (
        await migration_db.fetchval("SELECT version FROM lightrag_storage.migrations")
        == 1
    )
    before = await migration_db.fetchval(
        "SELECT structure_hash FROM lightrag_storage.migrations"
    )
    await m.migrate(migration_db, plan, "migration_test_app", confirm_empty=True)
    await m.verify(migration_db, plan, require_graph=False)
    assert (
        await migration_db.fetchval(
            "SELECT structure_hash FROM lightrag_storage.migrations"
        )
        == before
    )
    assert not await migration_db.fetchval(
        "SELECT has_schema_privilege('migration_test_app','public','CREATE')"
    )


@pytest.mark.integration
@pytest.mark.parametrize(
    "mutation",
    [
        "ALTER TABLE public.lightrag_doc_full ADD COLUMN unexpected text",
        "DROP INDEX public.idx_lightrag_doc_full_id",
        "UPDATE lightrag_storage.migrations SET checksum='wrong'",
    ],
)
async def test_migration_drift_is_rejected_without_repairing(migration_db, mutation):
    m = module()
    plan = build()
    await m.migrate(migration_db, plan, "migration_test_app", confirm_empty=True)
    await migration_db.execute(mutation)
    with pytest.raises(ValueError):
        await m.migrate(migration_db, plan, "migration_test_app", confirm_empty=True)


@pytest.mark.integration
async def test_unversioned_existing_business_tables_are_not_adopted(migration_db):
    m = module()
    await migration_db.execute("CREATE TABLE public.lightrag_doc_full(id text)")
    with pytest.raises(ValueError):
        await m.migrate(migration_db, build(), "migration_test_app", confirm_empty=True)
    assert (
        await migration_db.fetchval("SELECT to_regnamespace('lightrag_storage')")
        is None
    )


@pytest.mark.integration
async def test_failed_ddl_rolls_back_ledger_and_business_tables(migration_db):
    from dataclasses import replace

    m = module()
    plan = build()
    broken = replace(
        plan, statements=plan.statements + ("SELECT definitely_missing_function()",)
    )
    with pytest.raises(Exception):
        await m.migrate(migration_db, broken, "migration_test_app", confirm_empty=True)
    assert (
        await migration_db.fetchval("SELECT to_regnamespace('lightrag_storage')")
        is None
    )
    assert (
        await migration_db.fetchval("SELECT to_regclass('public.lightrag_doc_full')")
        is None
    )


@pytest.mark.integration
async def test_runtime_role_with_create_or_same_operator_identity_is_refused_before_ddl(
    migration_db,
):
    m = module()
    await migration_db.execute("GRANT CREATE ON SCHEMA public TO migration_test_app")
    with pytest.raises(ValueError):
        await m.migrate(migration_db, build(), "migration_test_app", confirm_empty=True)
    assert (
        await migration_db.fetchval("SELECT to_regnamespace('lightrag_storage')")
        is None
    )


@pytest.mark.integration
async def test_wrong_profile_and_unfinished_graph_refuse_runtime_verify(migration_db):
    from .test_plan import environment

    m = module()
    plan = build()
    await m.migrate(migration_db, plan, "migration_test_app", confirm_empty=True)
    with pytest.raises(ValueError):
        await m.verify(migration_db, plan)
    other = build(dict(environment(), EMBEDDING_DIM="16"))
    with pytest.raises(ValueError):
        await m.verify(migration_db, other, require_graph=False)


@pytest.mark.integration
@pytest.mark.parametrize(
    "mutation",
    [
        "ALTER TABLE public.lightrag_doc_full ENABLE ROW LEVEL SECURITY",
        "ALTER TABLE public.lightrag_doc_full FORCE ROW LEVEL SECURITY",
        "CREATE POLICY hidden ON public.lightrag_doc_full USING (false)",
        """CREATE FUNCTION public.migration_fixture_trigger() RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN RETURN NULL; END $$;
       CREATE TRIGGER migration_fixture_trigger BEFORE INSERT ON public.lightrag_doc_full
       FOR EACH ROW EXECUTE FUNCTION public.migration_fixture_trigger()""",
    ],
)
async def test_runtime_semantics_drift_is_rejected_without_repairing(
    migration_db, mutation
):
    m = module()
    plan = build()
    await m.migrate(migration_db, plan, "migration_test_app", confirm_empty=True)
    await migration_db.execute(mutation)
    with pytest.raises(ValueError):
        await m.verify(migration_db, plan, require_graph=False)
