"""Read-only migration gate before a production coordinator can register."""

import asyncpg

from .postgres import verify, verify_runtime_access


async def verify_runtime_storage(runtime):
    from lightrag.kg.postgres_impl import PostgreSQLDB

    config = runtime.pg_config
    db = PostgreSQLDB(config)
    ssl = db._create_ssl_context()
    if ssl is None and db.ssl_mode:
        if db.ssl_mode.lower() in {"require", "prefer"}:
            ssl = True
        elif db.ssl_mode.lower() == "disable":
            ssl = False
    conn = await asyncpg.connect(
        host=db.host,
        port=db.port,
        database=db.database,
        user=db.user,
        password=db.password,
        ssl=ssl,
        timeout=10,
        command_timeout=30,
        server_settings={
            "search_path": "public",
            "default_transaction_read_only": "on",
            "application_name": "lightrag-runtime-schema-verify",
            "lock_timeout": "5000",
        },
    )
    try:
        async with conn.transaction(readonly=True, isolation="repeatable_read"):
            await verify(conn, runtime.storage_migration_plan)
            await verify_runtime_access(conn, runtime.storage_migration_plan, db.user)
            row = await conn.fetchrow(
                "SELECT manifest_hash,fenced FROM lightrag_coordination.workspaces "
                "WHERE deployment_id=$1 AND workspace=$2",
                *runtime.coordinator._scope,
            )
            if (
                not row
                or row["manifest_hash"] != runtime.coordinator._manifest_hash
                or row["fenced"]
            ):
                raise ValueError("Explicit compatible workspace registration required")
    finally:
        await conn.close(timeout=5)
