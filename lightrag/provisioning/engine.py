"""Explicit operator phases; schema provisioning never enrolls a release owner."""

from . import postgres
from .manifest import manifest_hash


async def quiescent(connection):
    if await connection.fetchval(
        "SELECT to_regclass('lightrag_coordination.operations') IS NOT NULL"
    ):
        busy = await connection.fetchval("""
            SELECT EXISTS(SELECT FROM lightrag_coordination.operations WHERE state='active')
            OR EXISTS(SELECT FROM lightrag_coordination.resource_locks)
            OR EXISTS(SELECT FROM lightrag_coordination.document_claims)
            OR EXISTS(SELECT FROM lightrag_coordination.mutations WHERE state='pending')
            OR EXISTS(SELECT FROM lightrag_coordination.pipeline_requests WHERE state<>'completed')
            OR EXISTS(SELECT FROM lightrag_coordination.pipeline_retry_targets WHERE NOT done)
            OR EXISTS(SELECT FROM lightrag_coordination.workspaces WHERE fenced)
        """)
        if busy:
            raise ValueError(
                "Provisioning requires an audited idle coordination domain"
            )
    if await connection.fetchval(
        "SELECT to_regclass('lightrag_release.instances') IS NOT NULL"
    ):
        busy = await connection.fetchval("""
            SELECT EXISTS(SELECT FROM lightrag_release.instances WHERE NOT closed)
            OR EXISTS(SELECT FROM lightrag_release.tickets WHERE state<>'COMPLETED')
            OR EXISTS(SELECT FROM lightrag_release.commands WHERE state IN ('QUEUED','EXECUTING','UNCERTAIN'))
            OR EXISTS(SELECT FROM lightrag_release.source_cleanup WHERE state='CLEANING')
            OR EXISTS(SELECT FROM lightrag_release.scopes WHERE phase NOT IN ('IDLE','COMPLETED','ABORTED','ROLLED_BACK'))
        """)
        if busy:
            raise ValueError("Provisioning requires an audited idle release domain")


async def verify_all(connection, plan, graph, runtime_role):
    from lightrag.distributed.migrations import verify as verify_coordination
    from lightrag.distributed.release.migrations import (
        verify as verify_release,
        verify_runtime_role,
    )

    await postgres.verify(connection, plan)
    await verify_coordination(connection)
    await verify_release(connection)
    await postgres.verify_runtime_access(connection, plan, runtime_role)
    await verify_runtime_role(
        connection, runtime_role, credential_exception=plan.credential_exception
    )
    await graph.verify()


async def apply_all(connection, plan, graph, runtime_role, *, confirm_empty):
    if connection.is_in_transaction():
        raise ValueError("Initialization cannot run inside a caller transaction")
    from lightrag.distributed.migrations import migrate as migrate_coordination
    from lightrag.distributed.release.migrations import (
        migrate as migrate_release,
        grant_runtime,
    )

    async with postgres.migration_lock(connection):
        if plan.credential_exception is not None:
            await plan.credential_exception.verify_initialization_state(connection)
        await postgres.audit_runtime_role(
            connection, runtime_role, credential_exception=plan.credential_exception
        )
        await quiescent(connection)
        await graph.preflight()
        async with connection.transaction():
            await postgres.migrate(
                connection, plan, runtime_role, confirm_empty=confirm_empty
            )
            await migrate_coordination(connection)
            await migrate_release(connection)
            await connection.execute(
                f'GRANT USAGE ON SCHEMA lightrag_coordination TO "{runtime_role}"'
            )
            await connection.execute(
                f'GRANT SELECT ON ALL TABLES IN SCHEMA lightrag_coordination TO "{runtime_role}"'
            )
            for name in [
                "workspaces",
                "operations",
                "resource_locks",
                "document_claims",
                "mutations",
                "recovery_audit",
                "pipeline_control",
                "pipeline_requests",
                "pipeline_retry_targets",
            ]:
                await connection.execute(
                    f'GRANT INSERT,UPDATE,DELETE ON lightrag_coordination.{name} TO "{runtime_role}"'
                )
            await grant_runtime(
                connection, runtime_role, credential_exception=plan.credential_exception
            )
        await graph.apply(connection)
        await verify_all(connection, plan, graph, runtime_role)


async def register_workspace(
    connection, plan, graph, environment, *, expected_manifest_hash
):
    if connection.is_in_transaction():
        raise ValueError(
            "Workspace registration cannot run inside a caller transaction"
        )
    if expected_manifest_hash != manifest_hash(environment):
        raise ValueError("Approved workspace manifest differs")
    async with postgres.migration_lock(connection):
        if plan.credential_exception is not None:
            await plan.credential_exception.verify_initialization_state(connection)
        await verify_all(connection, plan, graph, environment["POSTGRES_USER"])
        await quiescent(connection)
        await graph.verify_empty_scope(plan.scope[1])
        async with connection.transaction():
            if plan.credential_exception is not None:
                await plan.credential_exception.verify_initialization_state(connection)
            await quiescent(connection)
            for table in plan.tables:
                if await connection.fetchval(
                    f"SELECT EXISTS(SELECT FROM public.{table} WHERE workspace=$1 LIMIT 1)",
                    plan.scope[1],
                ):
                    raise ValueError(
                        "First registration requires an empty business workspace"
                    )
            await connection.execute(
                "INSERT INTO lightrag_coordination.workspaces(deployment_id,workspace,manifest_hash) VALUES($1,$2,$3) ON CONFLICT DO NOTHING",
                *plan.scope,
                expected_manifest_hash,
            )
            row = await connection.fetchrow(
                "SELECT manifest_hash,fenced FROM lightrag_coordination.workspaces WHERE deployment_id=$1 AND workspace=$2 FOR UPDATE",
                *plan.scope,
            )
            if row["manifest_hash"] != expected_manifest_hash or row["fenced"]:
                raise ValueError("Existing workspace registration differs")
    return {
        "registered": True,
        "manifest_hash": expected_manifest_hash,
        "release_enrolled": False,
    }
