"""Explicit, checksum-verified SQL migrations bundled as Python resources."""

from hashlib import sha256

from .v001 import SQL, VERSION

CHECKSUM = sha256(SQL.encode()).hexdigest()


async def migrate(connection):
    """Apply additive SQL in one transaction, serializing concurrent migrators."""
    async with connection.transaction():
        await connection.execute("SELECT pg_advisory_xact_lock(684769932094171)")
        await connection.execute(SQL)
        checksum = await connection.fetchval(
            "SELECT checksum FROM lightrag_coordination.schema_version WHERE version=$1",
            VERSION,
        )
        if checksum is not None and checksum != CHECKSUM:
            raise ValueError("Coordination migration checksum mismatch")
        await connection.execute(
            "INSERT INTO lightrag_coordination.schema_version(version, checksum) "
            "VALUES ($1, $2) ON CONFLICT DO NOTHING",
            VERSION,
            CHECKSUM,
        )
        await verify(connection)


async def verify(connection):
    """Reject absent, newer, drifted or incomplete schemas without any DDL."""
    from ..coordinator import CoordinationSchemaError

    try:
        rows = await connection.fetch(
            "SELECT version, checksum FROM lightrag_coordination.schema_version ORDER BY version"
        )
        if [(r["version"], r["checksum"]) for r in rows] != [(VERSION, CHECKSUM)]:
            raise CoordinationSchemaError(
                "Coordination schema version/checksum mismatch"
            )
        primary_keys = await connection.fetch(
            "SELECT t.relname, array_agg(a.attname::text ORDER BY k.ordinality) AS columns "
            "FROM pg_constraint c JOIN pg_class t ON t.oid=c.conrelid "
            "JOIN pg_namespace n ON n.oid=t.relnamespace "
            "CROSS JOIN LATERAL unnest(c.conkey) WITH ORDINALITY k(attnum,ordinality) "
            "JOIN pg_attribute a ON a.attrelid=t.oid AND a.attnum=k.attnum "
            "WHERE n.nspname='lightrag_coordination' AND c.contype='p' GROUP BY t.relname"
        )
        required_keys = {
            "schema_version": ["version"],
            "workspaces": ["deployment_id", "workspace"],
            "operations": ["id"],
            "resource_locks": ["deployment_id", "workspace", "resource_key"],
            "document_claims": ["deployment_id", "workspace", "doc_id"],
            "mutations": ["id"],
            "recovery_audit": ["id"],
        }
        found_keys = {row["relname"]: row["columns"] for row in primary_keys}
        if any(
            found_keys.get(table) != columns for table, columns in required_keys.items()
        ):
            raise CoordinationSchemaError(
                "Coordination ownership constraints are missing or drifted"
            )
        # Resolve every runtime column without reading or changing stored rows.
        for table, columns in {
            "workspaces": "deployment_id,workspace,manifest_hash,generation,fenced,fence_reason",
            "operations": "id,deployment_id,workspace,generation,owner_id,witness,kind,exclusive,state,phase,metadata,created_at,heartbeat_at,finished_at",
            "resource_locks": "deployment_id,workspace,resource_key,operation_id,task_id,depth",
            "document_claims": "deployment_id,workspace,doc_id,operation_id,phase,heartbeat_at",
            "mutations": "id,operation_id,backend,namespace,method,state,created_at,acknowledged_at,recovered_at",
            "recovery_audit": "id,deployment_id,workspace,old_generation,new_generation,actor,reason,confirmations,snapshot,recovered_at",
        }.items():
            await connection.execute(
                f"SELECT {columns} FROM lightrag_coordination.{table} LIMIT 0"
            )
    except CoordinationSchemaError:
        raise
    except Exception:
        raise CoordinationSchemaError(
            "Coordination schema is not ready; run python -m lightrag.distributed migrate"
        ) from None
