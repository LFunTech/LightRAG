"""Bounded live evidence; never accept an operator-supplied empty-work snapshot."""

from dataclasses import asdict, dataclass
from .models import ReleaseDraining, ReleaseUnavailable

READ_KINDS = (
    "aquery",
    "aquery_data",
    "aquery_llm",
    "release_read",
    "initialize",
    "initialize_upload_sessions",
)


@dataclass(frozen=True)
class DrainEvidence:
    tickets: int
    uncertain_tickets: int
    writers: int
    unknown_owners: int
    orphans: int
    pending: int
    claims: int
    locks: int
    uncertain_commands: int
    cleaning_sources: int

    @property
    def empty(self):
        return not any(asdict(self).values())

    def require_empty(self):
        if (
            self.unknown_owners
            or self.orphans
            or self.uncertain_commands
            or self.uncertain_tickets
        ):
            raise ReleaseUnavailable(
                "Uncertain writer state requires an operator audit"
            )
        if not self.empty:
            raise ReleaseDraining("Writer drain has unfinished or uncertain work")


async def collect_drain_evidence(connection, scope):
    """Caller holds the release scope row lock; active indexes bound history reads."""
    deployment, workspace = scope.split("/")
    row = await connection.fetchrow(
        "SELECT fenced FROM lightrag_coordination.workspaces WHERE deployment_id=$1 AND workspace=$2",
        deployment,
        workspace,
    )
    if row is None or row["fenced"]:
        raise ReleaseUnavailable("Coordination workspace is missing or fenced")
    # No scan of the historical mutations table: the operation-state index and
    # per-operation mutation index select only currently active operations.
    counts = await connection.fetchrow(
        """
        WITH active AS MATERIALIZED (
          SELECT o.* FROM lightrag_coordination.operations o
          WHERE deployment_id=$1 AND workspace=$2 AND state='active'
        )
        SELECT
          (SELECT count(*) FROM active WHERE kind<>ALL($3::text[])) AS writers,
          (SELECT count(*) FROM active o WHERE NOT EXISTS (
            SELECT 1 FROM lightrag_release.instances i WHERE i.scope=$4 AND
            i.coordinator_owner=o.owner_id AND NOT i.closed)) AS unknown_owners,
          (SELECT count(*) FROM active o WHERE NOT EXISTS (
            SELECT 1 FROM pg_locks l WHERE l.locktype='advisory' AND l.granted
            AND l.objsubid=1 AND l.database=(SELECT oid FROM pg_database WHERE datname=current_database())
            AND l.classid::bigint=(o.witness >> 32) AND l.objid::bigint=(o.witness & 4294967295))) AS orphans,
          (SELECT count(*) FROM active o JOIN lightrag_coordination.mutations m ON m.operation_id=o.id WHERE m.state='pending') AS pending,
          (SELECT count(*) FROM lightrag_coordination.document_claims WHERE deployment_id=$1 AND workspace=$2) AS claims,
          (SELECT count(*) FROM lightrag_coordination.resource_locks WHERE deployment_id=$1 AND workspace=$2) AS locks,
          (SELECT count(*) FROM lightrag_release.tickets WHERE scope=$4 AND state<>'COMPLETED') AS tickets,
          (SELECT count(*) FROM lightrag_release.tickets WHERE scope=$4 AND state='UNCERTAIN') AS uncertain_tickets,
          (SELECT count(*) FROM lightrag_release.commands WHERE scope=$4 AND state='UNCERTAIN') AS uncertain_commands,
          (SELECT count(*) FROM lightrag_release.source_cleanup WHERE scope=$4 AND state='CLEANING') AS cleaning_sources
    """,
        deployment,
        workspace,
        list(READ_KINDS),
        scope,
    )
    return DrainEvidence(**dict(counts))
