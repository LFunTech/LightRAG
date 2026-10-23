"""Separately provisioned release SQL; normal application startup only verifies."""

from hashlib import sha256
from importlib.resources import files
import json
import re

from ..models import ReleaseSchemaError


def migration_sql():
    return files(__package__).joinpath("001_release.sql").read_text()


async def structure_hash(connection):
    rows = await connection.fetch("""
        SELECT 'column' AS kind,c.relname||'.'||a.attname AS name,
               format_type(a.atttypid,a.atttypmod)||':'||a.attnotnull::text||':'||coalesce(pg_get_expr(d.adbin,d.adrelid),'') AS definition
        FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace
        JOIN pg_attribute a ON a.attrelid=c.oid AND a.attnum>0 AND NOT a.attisdropped
        LEFT JOIN pg_attrdef d ON d.adrelid=c.oid AND d.adnum=a.attnum
        WHERE n.nspname='lightrag_release' AND c.relkind='r'
        UNION ALL
        SELECT 'constraint',c.relname||'.'||k.conname,pg_get_constraintdef(k.oid)
        FROM pg_constraint k JOIN pg_class c ON c.oid=k.conrelid JOIN pg_namespace n ON n.oid=c.relnamespace WHERE n.nspname='lightrag_release'
        UNION ALL
        SELECT 'index',indexname,indexdef FROM pg_indexes WHERE schemaname='lightrag_release'
        UNION ALL
        SELECT 'function',p.proname,pg_get_functiondef(p.oid) FROM pg_proc p JOIN pg_namespace n ON n.oid=p.pronamespace WHERE n.nspname='lightrag_release'
        ORDER BY 1,2,3
    """)
    return sha256(
        json.dumps([list(row) for row in rows], separators=(",", ":")).encode()
    ).hexdigest()


async def verify(connection):
    try:
        rows = await connection.fetch(
            "SELECT version,checksum,structure_hash FROM lightrag_release.schema_migrations ORDER BY version"
        )
        if (
            len(rows) != 1
            or rows[0]["version"] != 1
            or rows[0]["checksum"] != sha256(migration_sql().encode()).hexdigest()
            or rows[0]["structure_hash"] != await structure_hash(connection)
        ):
            raise ReleaseSchemaError("Release schema is missing or drifted")
    except ReleaseSchemaError:
        raise
    except Exception:
        raise ReleaseSchemaError("Release schema verification failed") from None


async def migrate(connection):
    async with connection.transaction():
        await connection.execute("SELECT pg_advisory_xact_lock(712390816256310)")
        exists = await connection.fetchval(
            "SELECT to_regclass('lightrag_release.schema_migrations') IS NOT NULL"
        )
        if exists:
            await verify(connection)
            return
        if await connection.fetchval(
            "SELECT EXISTS(SELECT 1 FROM pg_namespace WHERE nspname='lightrag_release')"
        ):
            raise ReleaseSchemaError(
                "Unversioned release schema requires operator audit"
            )
        sql = migration_sql()
        await connection.execute(sql)
        await connection.execute(
            "INSERT INTO lightrag_release.schema_migrations(version,checksum,structure_hash) VALUES(1,$1,$2)",
            sha256(sql.encode()).hexdigest(),
            await structure_hash(connection),
        )
        await verify(connection)


async def grant_runtime(connection, role, *, credential_exception=None):
    if credential_exception is not None:
        await credential_exception.verify_principal(connection, role)
        return
    if (
        not isinstance(role, str)
        or not re.fullmatch(r"[a-z][a-z0-9_]{0,62}", role)
        or role in {"public", "postgres"}
    ):
        raise ValueError("Explicit non-privileged runtime role required")
    await verify_runtime_role(connection, role)
    privileged = await connection.fetchval(
        "SELECT rolsuper OR rolcreaterole FROM pg_roles WHERE rolname=$1", role
    )
    if privileged is not False or await connection.fetchval(
        "SELECT current_user=$1", role
    ):
        raise ValueError("Runtime role must differ from privileged provisioning role")
    await connection.execute(f'GRANT USAGE ON SCHEMA lightrag_release TO "{role}"')
    await connection.execute(
        f'GRANT SELECT ON ALL TABLES IN SCHEMA lightrag_release TO "{role}"'
    )
    await connection.execute(
        f'GRANT EXECUTE ON ALL FUNCTIONS IN SCHEMA lightrag_release TO "{role}"'
    )


async def verify_runtime_role(connection, role=None, *, credential_exception=None):
    if role is None:
        role = await connection.fetchval("SELECT current_user")
    if credential_exception is not None:
        await credential_exception.verify_principal(connection, role)
        return
    unsafe = await connection.fetchval(
        """
        SELECT EXISTS(SELECT 1 FROM pg_roles r WHERE
          (r.rolsuper OR r.rolcreaterole OR r.rolcreatedb OR r.rolbypassrls)
          AND pg_has_role($1,r.oid,'MEMBER')) OR
          has_schema_privilege($1,'lightrag_release','CREATE') OR
          EXISTS(SELECT 1 FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace
            WHERE n.nspname='lightrag_release' AND c.relkind IN ('r','S') AND
            (has_table_privilege($1,c.oid,'INSERT,UPDATE,DELETE,TRUNCATE,TRIGGER') OR
             pg_has_role($1,c.relowner,'MEMBER')))
    """,
        role,
    )
    if unsafe is not False:
        raise ValueError(
            "Runtime role has release control or privileged ownership permissions"
        )
