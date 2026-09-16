"""Explicit, checksum-verified SQL migrations bundled as Python resources."""

from hashlib import sha256

from .v001 import SQL, VERSION
from . import v002, v003

CHECKSUM = sha256(SQL.encode()).hexdigest()
MIGRATIONS = [
    (VERSION, SQL, CHECKSUM),
    (v002.VERSION, v002.SQL, sha256(v002.SQL.encode()).hexdigest()),
    (v003.VERSION, v003.SQL, sha256(v003.SQL.encode()).hexdigest()),
]

# This is the runtime schema contract, not a hash of the migration source.
# Keep it aligned with versioned SQL; verify never repairs a deployed catalog.
_COLUMN_TYPES = {
    "schema_version": {
        "integer": "version",
        "text": "checksum",
        "timestamp with time zone": "applied_at",
    },
    "workspaces": {
        "text": "deployment_id workspace manifest_hash fence_reason",
        "bigint": "generation",
        "boolean": "fenced",
    },
    "operations": {
        "uuid": "id owner_id",
        "text": "deployment_id workspace kind state phase",
        "bigint": "generation witness",
        "boolean": "exclusive",
        "jsonb": "metadata",
        "timestamp with time zone": "created_at heartbeat_at finished_at",
    },
    "resource_locks": {
        "text": "deployment_id workspace resource_key",
        "uuid": "operation_id task_id",
        "integer": "depth",
    },
    "document_claims": {
        "text": "deployment_id workspace doc_id phase",
        "uuid": "operation_id",
        "timestamp with time zone": "heartbeat_at",
    },
    "mutations": {
        "uuid": "id operation_id",
        "text": "backend namespace method state",
        "timestamp with time zone": "created_at acknowledged_at recovered_at",
    },
    "recovery_audit": {
        "uuid": "id",
        "text": "deployment_id workspace actor reason",
        "bigint": "old_generation new_generation",
        "jsonb": "confirmations snapshot",
        "timestamp with time zone": "recovered_at",
    },
}
_COLUMN_TYPES.update(
    {
        "pipeline_control": {
            "text": "deployment_id workspace",
            "boolean": "paused",
            "bigint": "cancel_epoch",
        },
        "pipeline_requests": {
            "text": "deployment_id workspace request_id state",
            "timestamp with time zone": "created_at target_cutoff_at",
        },
        "pipeline_retry_targets": {
            "text": "deployment_id workspace request_id doc_id version",
            "boolean": "done",
        },
    }
)
_NULLABLE_COLUMNS = {
    "workspaces.fence_reason",
    "operations.finished_at",
    "mutations.acknowledged_at",
    "mutations.recovered_at",
}
_COLUMN_DEFAULTS = {
    "schema_version.applied_at": "clock_timestamp()",
    "workspaces.generation": "1",
    "workspaces.fenced": "false",
    "operations.state": "'active'::text",
    "operations.phase": "'admitted'::text",
    "operations.metadata": "'{}'::jsonb",
    "operations.created_at": "clock_timestamp()",
    "operations.heartbeat_at": "clock_timestamp()",
    "resource_locks.depth": "1",
    "document_claims.phase": "'claimed'::text",
    "document_claims.heartbeat_at": "clock_timestamp()",
    "mutations.state": "'pending'::text",
    "mutations.created_at": "clock_timestamp()",
    "recovery_audit.recovered_at": "clock_timestamp()",
}


_COLUMN_DEFAULTS.update(
    {
        "pipeline_control.paused": "false",
        "pipeline_control.cancel_epoch": "0",
        "pipeline_requests.state": "'selecting'::text",
        "pipeline_requests.created_at": "clock_timestamp()",
        "pipeline_requests.target_cutoff_at": "clock_timestamp()",
        "pipeline_retry_targets.done": "false",
    }
)


async def migrate(connection):
    """Apply additive SQL in one transaction, serializing concurrent migrators."""
    async with connection.transaction():
        await connection.execute("SELECT pg_advisory_xact_lock(684769932094171)")
        for version, sql, checksum in MIGRATIONS:
            await connection.execute(sql)
            existing = await connection.fetchval(
                "SELECT checksum FROM lightrag_coordination.schema_version WHERE version=$1",
                version,
            )
            if existing is not None and existing != checksum:
                raise ValueError("Coordination migration checksum mismatch")
            await connection.execute(
                "INSERT INTO lightrag_coordination.schema_version(version, checksum) VALUES ($1, $2) ON CONFLICT DO NOTHING",
                version,
                checksum,
            )
        await verify(connection)


async def verify(connection):
    """Reject absent, newer, drifted or incomplete schemas without any DDL."""
    from ..coordinator import CoordinationSchemaError

    try:
        rows = await connection.fetch(
            "SELECT version, checksum FROM lightrag_coordination.schema_version ORDER BY version"
        )
        if [(r["version"], r["checksum"]) for r in rows] != [
            (version, checksum) for version, _, checksum in MIGRATIONS
        ]:
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
            "pipeline_control": ["deployment_id", "workspace"],
            "pipeline_requests": ["deployment_id", "workspace", "request_id"],
            "pipeline_retry_targets": [
                "deployment_id",
                "workspace",
                "request_id",
                "doc_id",
            ],
        }
        found_keys = {row["relname"]: row["columns"] for row in primary_keys}
        if any(
            found_keys.get(table) != columns for table, columns in required_keys.items()
        ):
            raise CoordinationSchemaError(
                "Coordination ownership constraints are missing or drifted"
            )
        columns = await connection.fetch(
            "SELECT t.relname,a.attname,format_type(a.atttypid,a.atttypmod) AS type, "
            "a.attnotnull,pg_get_expr(d.adbin,d.adrelid) AS default_expr "
            "FROM pg_attribute a JOIN pg_class t ON t.oid=a.attrelid "
            "JOIN pg_namespace n ON n.oid=t.relnamespace "
            "LEFT JOIN pg_attrdef d ON d.adrelid=t.oid AND d.adnum=a.attnum "
            "WHERE n.nspname='lightrag_coordination' AND a.attnum>0 AND NOT a.attisdropped"
        )
        actual = {
            f"{column['relname']}.{column['attname']}": (
                column["type"],
                column["attnotnull"],
                column["default_expr"],
            )
            for column in columns
        }
        for table, groups in _COLUMN_TYPES.items():
            for data_type, names in groups.items():
                for name in names.split():
                    key = f"{table}.{name}"
                    expected = (
                        data_type,
                        key not in _NULLABLE_COLUMNS,
                        _COLUMN_DEFAULTS.get(key),
                    )
                    if actual.get(key) != expected:
                        raise CoordinationSchemaError(
                            f"Coordination column contract is missing or drifted: {key}"
                        )
    except CoordinationSchemaError:
        raise
    except Exception:
        raise CoordinationSchemaError(
            "Coordination schema is not ready; run python -m lightrag.distributed migrate"
        ) from None
