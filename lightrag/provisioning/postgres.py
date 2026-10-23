"""Transactional initial storage DDL and read-only schema verification.

Never adopts unversioned business tables. Graph mutations are journalled separately;
see docs/ProdDelivery.md for the non-transactional provisioning contract.
"""

from contextlib import asynccontextmanager
import re

from .plan import digest, resource

LOCK = 713908122390164


@asynccontextmanager
async def migration_lock(connection):
    if not await connection.fetchval("SELECT pg_try_advisory_lock($1)", LOCK):
        raise ValueError("Another storage migrator is active")
    try:
        yield
    finally:
        await connection.execute("SELECT pg_advisory_unlock($1)", LOCK)


async def audit_runtime_role(connection, role, *, operator=True):
    if not isinstance(role, str) or not re.fullmatch("[a-z][a-z0-9_]{0,62}", role):
        raise ValueError("Explicit existing runtime role required")
    row = await connection.fetchrow(
        """
        SELECT r.rolsuper OR r.rolcreatedb OR r.rolcreaterole OR r.rolreplication OR r.rolbypassrls AS privileged,
               has_database_privilege(r.oid,current_database(),'CREATE') AS database_create,
               has_schema_privilege(r.oid,'public','CREATE') AS schema_create,
               current_user=r.rolname AS same_operator,
               EXISTS(SELECT FROM pg_roles p WHERE (p.rolsuper OR p.rolcreatedb OR p.rolcreaterole OR p.rolbypassrls)
                      AND pg_has_role(r.oid,p.oid,'MEMBER')) AS inherited
        FROM pg_roles r WHERE r.rolname=$1
    """,
        role,
    )
    if (
        not row
        or any(
            row[k]
            for k in ("privileged", "database_create", "schema_create", "inherited")
        )
        or (operator and row["same_operator"])
    ):
        raise ValueError(
            "Runtime role must be separate and have no schema provisioning privileges"
        )
    if await connection.fetchval(
        """
        SELECT EXISTS(SELECT FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace
        WHERE (n.nspname IN ('lightrag_storage','lightrag_release','lightrag_coordination') OR (n.nspname='public' AND c.relname LIKE 'lightrag%'))
        AND pg_has_role($1::text,c.relowner,'MEMBER'))
    """,
        role,
    ):
        raise ValueError("Runtime role must not own migration or business objects")
    unsafe = await connection.fetchval(
        """
        SELECT EXISTS(SELECT FROM pg_namespace n
          WHERE n.nspname IN ('lightrag_storage','lightrag_coordination')
          AND (has_schema_privilege($1::text,n.oid,'CREATE') OR pg_has_role($1::text,n.nspowner,'MEMBER')))
        OR EXISTS(SELECT FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace
          WHERE c.relkind='r' AND (n.nspname='lightrag_storage' OR
            (n.nspname='lightrag_coordination' AND c.relname='schema_version'))
          AND has_table_privilege($1::text,c.oid,'INSERT,UPDATE,DELETE,TRUNCATE,TRIGGER,REFERENCES'))
    """,
        role,
    )
    if unsafe:
        raise ValueError("Runtime role has migration control permissions")


async def verify_runtime_access(connection, plan, role):
    """Check every required grant independently; privilege lists mean ANY in PG."""
    await audit_runtime_role(connection, role, operator=False)
    for table in plan.tables:
        for privilege in ("SELECT", "INSERT", "UPDATE", "DELETE"):
            if not await connection.fetchval(
                "SELECT has_table_privilege($1::text,$2::text,$3::text)",
                role,
                "public." + table,
                privilege,
            ):
                raise ValueError("Runtime business storage permissions are incomplete")
    for name in ("migrations", "graph_steps"):
        if not await connection.fetchval(
            "SELECT has_table_privilege($1::text,$2::text,'SELECT')",
            role,
            "lightrag_storage." + name,
        ):
            raise ValueError(
                "Runtime migration verification permissions are incomplete"
            )


async def snapshot(connection, plan):
    unsupported = await connection.fetchval(
        """
        WITH owned AS (
          SELECT c.oid,c.relrowsecurity,c.relforcerowsecurity FROM pg_class c
          JOIN pg_namespace n ON n.oid=c.relnamespace WHERE c.relkind='r' AND
          ((n.nspname='public' AND c.relname=ANY($1::text[])) OR n.nspname='lightrag_storage')
        )
        SELECT EXISTS(SELECT FROM owned WHERE relrowsecurity OR relforcerowsecurity)
        OR EXISTS(SELECT FROM pg_policy p JOIN owned o ON o.oid=p.polrelid)
        OR EXISTS(SELECT FROM pg_trigger t JOIN owned o ON o.oid=t.tgrelid WHERE NOT t.tgisinternal)
        """,
        list(plan.tables),
    )
    if unsupported:
        raise ValueError("Unsupported storage RLS, policy or user-trigger drift")
    rows = await connection.fetch(
        """
        WITH owned AS (
            SELECT c.oid,c.relname,n.nspname FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace
            WHERE c.relkind='r' AND ((n.nspname='public' AND c.relname=ANY($1::text[])) OR n.nspname='lightrag_storage')
        )
        SELECT 'column' AS kind,o.nspname||'.'||o.relname||'.'||a.attname AS name,
               format_type(a.atttypid,a.atttypmod)||':'||a.attnotnull::text||':'||coalesce(pg_get_expr(d.adbin,d.adrelid),'') AS definition
        FROM owned o JOIN pg_attribute a ON a.attrelid=o.oid AND a.attnum>0 AND NOT a.attisdropped
        LEFT JOIN pg_attrdef d ON d.adrelid=o.oid AND d.adnum=a.attnum
        UNION ALL SELECT 'constraint',o.nspname||'.'||o.relname||'.'||k.conname,pg_get_constraintdef(k.oid)
        FROM owned o JOIN pg_constraint k ON k.conrelid=o.oid
        UNION ALL SELECT 'index',o.nspname||'.'||o.relname||'.'||c.relname,
               pg_get_indexdef(i.indexrelid)||':'||i.indisvalid::text||':'||i.indisready::text
        FROM owned o JOIN pg_index i ON i.indrelid=o.oid JOIN pg_class c ON c.oid=i.indexrelid
        ORDER BY 1,2,3
    """,
        list(plan.tables),
    )
    names = await connection.fetch(
        "SELECT tablename FROM pg_tables WHERE schemaname='public' AND tablename=ANY($1::text[]) ORDER BY tablename",
        list(plan.tables),
    )
    if [r["tablename"] for r in names] != sorted(plan.tables) or not rows:
        raise ValueError("Storage tables are missing")
    return digest([list(row) for row in rows])


async def verify(connection, plan, *, require_graph=True):
    try:
        rows = await connection.fetch(
            "SELECT version,profile_hash,checksum,structure_hash,graph_checksum,graph_complete FROM lightrag_storage.migrations ORDER BY version"
        )
        if len(rows) != 1:
            raise ValueError("Storage migration history differs")
        row = rows[0]
        if (
            row["version"],
            row["profile_hash"],
            row["checksum"],
            row["graph_checksum"],
        ) != (1, plan.profile_hash, plan.checksum, plan.graph_checksum):
            raise ValueError("Storage migration version/checksum/profile differs")
        if row["structure_hash"] != await snapshot(connection, plan):
            raise ValueError("Storage structure has drifted")
        version = await connection.fetchval(
            "SELECT extversion FROM pg_extension WHERE extname='vector'"
        )
        if (
            not isinstance(version, str)
            or not re.fullmatch(r"\d+\.\d+\.\d+", version)
            or tuple(map(int, version.split("."))) < (0, 7, 0)
        ):
            raise ValueError("Supported pgvector extension is not provisioned")
        if require_graph:
            if row["graph_complete"] is not True:
                raise ValueError("Graph migration is not complete")
            steps = await connection.fetch(
                "SELECT name,payload_hash,state FROM lightrag_storage.graph_steps ORDER BY name"
            )
            expected = sorted(
                (v["name"], digest([kind, v]), "CONFIRMED") for kind, v in plan.graph
            )
            if [tuple(r) for r in steps] != expected:
                raise ValueError("Graph migration evidence is missing or drifted")
    except ValueError:
        raise
    except Exception:
        raise ValueError("Storage migration is absent or cannot be verified") from None


async def grant_runtime(connection, plan, role):
    await audit_runtime_role(connection, role)
    await connection.execute(
        f'GRANT USAGE ON SCHEMA public,lightrag_storage TO "{role}"'
    )
    await connection.execute(
        f'GRANT SELECT ON ALL TABLES IN SCHEMA lightrag_storage TO "{role}"'
    )
    for table in plan.tables:
        await connection.execute(
            f'GRANT SELECT,INSERT,UPDATE,DELETE ON public.{table} TO "{role}"'
        )


async def migrate(connection, plan, runtime_role, *, confirm_empty=False):
    await audit_runtime_role(connection, runtime_role)
    async with migration_lock(connection):
        async with connection.transaction():
            await connection.execute("SET LOCAL lock_timeout='5s'")
            await connection.execute("SET LOCAL statement_timeout='60s'")
            exists = await connection.fetchval(
                "SELECT to_regclass('lightrag_storage.migrations') IS NOT NULL"
            )
            if exists:
                await verify(connection, plan, require_graph=False)
            else:
                if not confirm_empty or await connection.fetchval(
                    "SELECT to_regnamespace('lightrag_storage') IS NOT NULL OR EXISTS(SELECT FROM pg_tables WHERE schemaname='public' AND tablename ILIKE 'lightrag%')"
                ):
                    raise ValueError(
                        "Confirmed empty versioned target required; legacy storage is not adopted"
                    )
                if not await connection.fetchval(
                    "SELECT EXISTS(SELECT FROM pg_extension WHERE extname='vector')"
                ):
                    await connection.execute("CREATE EXTENSION vector")
                await connection.execute(resource("001_ledger.sql"))
                for sql in plan.statements:
                    await connection.execute(sql)
                await connection.execute(
                    "INSERT INTO lightrag_storage.migrations(version,profile_hash,checksum,structure_hash,graph_checksum) VALUES(1,$1,$2,$3,$4)",
                    plan.profile_hash,
                    plan.checksum,
                    await snapshot(connection, plan),
                    plan.graph_checksum,
                )
                await verify(connection, plan, require_graph=False)
            await grant_runtime(connection, plan, runtime_role)
