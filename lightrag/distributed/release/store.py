"""Durable release owner/epoch CAS. Use operator credentials for control methods."""

from contextlib import asynccontextmanager
import asyncio
import json

from .models import (
    ReleaseConflict,
    ReleaseUnavailable,
    ReleaseBudgetExceeded,
    validate_transition,
)
from .migrations import verify
from .evidence import collect_drain_evidence


def json_value(value):
    return json.loads(value) if isinstance(value, str) else value


def row_dict(row):
    result = dict(row)
    for key in ("identity", "policy", "envelope", "result", "status", "previous"):
        if key in result and result[key] is not None:
            result[key] = json_value(result[key])
    return result


class ReleaseStore:
    def __init__(self, database):
        self.database = database
        self._connection_lock = asyncio.Lock()

    @asynccontextmanager
    async def connection(self):
        if hasattr(self.database, "acquire"):
            async with self.database.acquire() as connection:
                yield connection
        else:
            # The operator's session/advisory lock must stay on one connection.
            # Serialize its individual DB sections, not network waits or pool work.
            async with self._connection_lock:
                yield self.database

    async def verify_schema(self):
        async with self.connection() as connection:
            await verify(connection)

    async def onboard(self, scope, *, active_slot, profile_hash, config_hash, policy):
        exception = None
        if "first_release_credential_exception" in policy:
            from lightrag_delivery.credentials import CredentialException

            exception = CredentialException.parse(
                policy["first_release_credential_exception"],
                scope=scope,
                profile_hash=profile_hash,
            )
            if active_slot != "blue":
                raise ReleaseConflict("First-release credentials require initial Blue")
        if (
            active_slot not in {"blue", "green"}
            or not isinstance(policy.get("queue_capacity"), int)
            or policy["queue_capacity"] < 1
        ):
            raise ValueError("Invalid release onboarding policy")
        async with self.connection() as connection:
            async with connection.transaction():
                await connection.execute(
                    "INSERT INTO lightrag_release.scopes(scope,active_slot,profile_hash,config_hash,policy) VALUES($1,$2,$3,$4,$5::jsonb) ON CONFLICT DO NOTHING",
                    scope,
                    active_slot,
                    profile_hash,
                    config_hash,
                    json.dumps(policy, allow_nan=False),
                )
                row = await connection.fetchrow(
                    "SELECT * FROM lightrag_release.scopes WHERE scope=$1 FOR UPDATE",
                    scope,
                )
                if (
                    row["profile_hash"] != profile_hash
                    or row["config_hash"] != config_hash
                    or row["active_slot"] != active_slot
                    or json_value(row["policy"]) != policy
                ):
                    raise ReleaseConflict("Existing release enrollment differs")
                if exception is not None:
                    exception.verify_scope_state(row_dict(row))

    async def begin(self, identity, *, expected_epoch):
        async with self.connection() as connection:
            async with connection.transaction():
                row = await self._scope(connection, identity.scope, lock=True)
                if "first_release_credential_exception" in row["policy"]:
                    raise ReleaseConflict(
                        "First-release credentials cannot enter ordinary upgrade"
                    )
                existing = await connection.fetchrow(
                    "SELECT identity,phase,previous FROM lightrag_release.releases WHERE scope=$1 AND release_id=$2",
                    identity.scope,
                    identity.release_id,
                )
                if existing and json_value(existing["identity"]) != identity.as_dict():
                    raise ReleaseConflict("Release id already has a different identity")
                if existing and existing["phase"] in {
                    "COMPLETED",
                    "ABORTED",
                    "ROLLED_BACK",
                }:
                    return {
                        "owner": identity.release_id,
                        "phase": existing["phase"],
                        "identity": identity.as_dict(),
                    }
                if row["owner"] == identity.release_id:
                    if (
                        row["identity"] != identity.as_dict()
                        or json_value(existing["previous"])["epoch"] != expected_epoch
                    ):
                        raise ReleaseConflict(
                            "Release identity or original epoch differs"
                        )
                    return row
                if row["epoch"] != expected_epoch or (
                    row["owner"] is not None and row["owner"] != identity.release_id
                ):
                    raise ReleaseConflict(
                        "Release owner or epoch conflict; no automatic takeover"
                    )
                if row["owner"] == identity.release_id:
                    if row["identity"] != identity.as_dict():
                        raise ReleaseConflict("Release identity differs")
                    return row
                if not row["gate_open"] or row["phase"] not in {
                    "IDLE",
                    "COMPLETED",
                    "ABORTED",
                    "ROLLED_BACK",
                }:
                    raise ReleaseConflict("Release scope is not open for preparation")
                latest = await connection.fetchval(
                    "SELECT identity->>'tag' FROM lightrag_release.releases WHERE scope=$1 AND phase='COMPLETED' ORDER BY updated_at DESC LIMIT 1",
                    identity.scope,
                )

                def version(tag):
                    return tuple(int(part) for part in tag[1:].split("."))

                if latest and version(identity.tag) <= version(latest):
                    raise ReleaseConflict("Normal release version must advance")
                await connection.execute(
                    "INSERT INTO lightrag_release.releases(scope,release_id,identity,phase,previous) VALUES($1,$2,$3::jsonb,'PREPARED',$4::jsonb)",
                    identity.scope,
                    identity.release_id,
                    json.dumps(identity.as_dict()),
                    json.dumps(
                        {
                            key: row[key]
                            for key in ("epoch", "active_slot", "config_hash")
                        }
                    ),
                )
                await connection.execute(
                    "UPDATE lightrag_release.scopes SET owner=$2,phase='PREPARED',candidate_slot=CASE active_slot WHEN 'blue' THEN 'green' ELSE 'blue' END,identity=$3::jsonb,updated_at=clock_timestamp() WHERE scope=$1",
                    identity.scope,
                    identity.release_id,
                    json.dumps(identity.as_dict()),
                )
                return await self._scope(connection, identity.scope)

    async def transition(self, identity, *, expected_epoch, expected_phase, next_phase):
        async with self.connection() as connection:
            async with connection.transaction():
                row = await self._scope(connection, identity.scope, lock=True)
                if row["phase"] != expected_phase:
                    raise ReleaseConflict("Unexpected release phase")
                validate_transition(
                    row, identity, expected_epoch=expected_epoch, next_phase=next_phase
                )
                if next_phase == "WRITER_SWITCHED":
                    raise ReleaseConflict(
                        "Writer transfer requires confirmed drain evidence"
                    )
                if next_phase == "COMPLETED":
                    unfinished = await connection.fetchval(
                        "SELECT count(*) FROM lightrag_release.instances WHERE scope=$1 AND slot<>$2 AND NOT closed",
                        identity.scope,
                        row["active_slot"],
                    )
                    if unfinished:
                        raise ReleaseConflict("Old instances have not retired")
                await connection.execute(
                    "UPDATE lightrag_release.scopes SET phase=$2,drain_deadline=CASE WHEN $2='DRAINING' THEN clock_timestamp()+make_interval(secs=>coalesce((policy->>'drain_seconds')::double precision,30)) ELSE drain_deadline END,gate_open=CASE WHEN $2='DRAINING' THEN false ELSE gate_open END,owner=CASE WHEN $2='COMPLETED' THEN NULL ELSE owner END,updated_at=clock_timestamp() WHERE scope=$1",
                    identity.scope,
                    next_phase,
                )
                await connection.execute(
                    "UPDATE lightrag_release.releases SET phase=$3,updated_at=clock_timestamp() WHERE scope=$1 AND release_id=$2",
                    identity.scope,
                    identity.release_id,
                    next_phase,
                )
                return await self._scope(connection, identity.scope)

    async def capture_baseline(self, identity, *, expected_epoch, digest):
        import re

        if not re.fullmatch("sha256:[a-f0-9]{64}", digest):
            raise ReleaseConflict("Observed baseline digest is invalid")
        async with self.connection() as db:
            async with db.transaction():
                row = await self._scope(db, identity.scope, lock=True)
                if (
                    row["owner"] != identity.release_id
                    or row["identity"] != identity.as_dict()
                    or row["epoch"] != expected_epoch
                    or row["phase"] != "PREPARED"
                ):
                    raise ReleaseConflict("Baseline capture identity differs")
                previous = json_value(
                    await db.fetchval(
                        "SELECT previous FROM lightrag_release.releases WHERE scope=$1 AND release_id=$2 FOR UPDATE",
                        identity.scope,
                        identity.release_id,
                    )
                )
                if previous.get("digest", digest) != digest:
                    raise ReleaseConflict(
                        "Previously observed baseline artifact changed"
                    )
                previous["digest"] = digest
                await db.execute(
                    "UPDATE lightrag_release.releases SET previous=$3::jsonb WHERE scope=$1 AND release_id=$2",
                    identity.scope,
                    identity.release_id,
                    json.dumps(previous),
                )

    async def wait_for_drain(self, identity, *, expected_epoch):
        import asyncio
        from .models import ReleaseDraining

        while True:
            async with self.connection() as connection:
                async with connection.transaction():
                    row = await self._scope(connection, identity.scope, lock=True)
                    if (
                        row["owner"] != identity.release_id
                        or row["epoch"] != expected_epoch
                        or row["identity"] != identity.as_dict()
                        or row["gate_open"]
                    ):
                        raise ReleaseConflict("Drain identity or closed gate differs")
                    if await connection.fetchval(
                        "SELECT clock_timestamp()>drain_deadline FROM lightrag_release.scopes WHERE scope=$1",
                        identity.scope,
                    ):
                        raise ReleaseBudgetExceeded(
                            "Persisted writer drain budget expired"
                        )
                    evidence = await collect_drain_evidence(connection, identity.scope)
                    try:
                        evidence.require_empty()
                        return evidence
                    except ReleaseDraining:
                        pass
            await asyncio.sleep(0.1)

    async def transfer_writer(self, identity, *, expected_epoch, expected_instances):
        async with self.connection() as connection:
            async with connection.transaction():
                row = await self._scope(connection, identity.scope, lock=True)
                validate_transition(
                    row,
                    identity,
                    expected_epoch=expected_epoch,
                    next_phase="WRITER_SWITCHED",
                )
                if row["gate_open"] or not expected_instances:
                    raise ReleaseConflict(
                        "Writer transfer requires a closed gate and candidate set"
                    )
                if await connection.fetchval(
                    "SELECT clock_timestamp()>drain_deadline FROM lightrag_release.scopes WHERE scope=$1",
                    identity.scope,
                ):
                    raise ReleaseBudgetExceeded("Persisted writer drain budget expired")
                candidates = await connection.fetch(
                    "SELECT instance_id,config_hash FROM lightrag_release.instances WHERE scope=$1 AND slot=$2 AND NOT closed AND NOT retiring",
                    identity.scope,
                    row["candidate_slot"],
                )
                if {item["instance_id"] for item in candidates} != set(
                    expected_instances
                ) or any(
                    item["config_hash"] != identity.config_hash for item in candidates
                ):
                    raise ReleaseConflict(
                        "Candidate instance set or configuration changed"
                    )
                evidence = await collect_drain_evidence(connection, identity.scope)
                evidence.require_empty()
                await connection.execute(
                    "UPDATE lightrag_release.scopes SET active_slot=candidate_slot,candidate_slot=active_slot,epoch=epoch+1,config_hash=$2,gate_open=true,phase='WRITER_SWITCHED',updated_at=clock_timestamp() WHERE scope=$1",
                    identity.scope,
                    identity.config_hash,
                )
                await connection.execute(
                    "UPDATE lightrag_release.releases SET phase='WRITER_SWITCHED',updated_at=clock_timestamp() WHERE scope=$1 AND release_id=$2",
                    identity.scope,
                    identity.release_id,
                )
                return await self._scope(connection, identity.scope)

    async def begin_rollback(self, identity, *, expected_epoch, approval):
        async with self.connection() as db:
            async with db.transaction():
                row = await self._scope(db, identity.scope, lock=True)
                record = await db.fetchrow(
                    "SELECT previous,rollback_approval FROM lightrag_release.releases WHERE scope=$1 AND release_id=$2",
                    identity.scope,
                    identity.release_id,
                )
                if record is None or row["identity"] != identity.as_dict():
                    raise ReleaseConflict("Rollback release identity differs")
                previous = json_value(record["previous"])
                required = {
                    "from_digest",
                    "to_config_hash",
                    "to_digest",
                    "profile_hash",
                    "read_write_compatible",
                    "evidence_id",
                }
                if (
                    not isinstance(approval, dict)
                    or set(approval) != required
                    or approval["from_digest"] != identity.digest
                    or approval["to_config_hash"] != previous["config_hash"]
                    or approval["to_digest"] != previous.get("digest")
                    or approval["profile_hash"] != row["profile_hash"]
                    or approval["read_write_compatible"] is not True
                    or not isinstance(approval["evidence_id"], str)
                    or not approval["evidence_id"].strip()
                    or expected_epoch != previous["epoch"] + 1
                ):
                    raise ReleaseConflict(
                        "Explicit release-bound rollback compatibility is required"
                    )
                if row["phase"] in {
                    "ROLLBACK_DRAINING",
                    "ROLLBACK_WRITER_SWITCHED",
                    "ROLLBACK_RETIRING",
                    "ROLLED_BACK",
                }:
                    if json_value(record["rollback_approval"]) != approval:
                        raise ReleaseConflict(
                            "Rollback approval differs from its durable intent"
                        )
                    return row
                if (
                    row["owner"] != identity.release_id
                    or row["epoch"] != expected_epoch
                    or row["phase"] != "WRITER_SWITCHED"
                ):
                    raise ReleaseConflict(
                        "Rollback is only safe before old instances retire"
                    )
                if not await db.fetchval(
                    "SELECT EXISTS(SELECT 1 FROM lightrag_release.instances WHERE scope=$1 AND slot=$2 AND NOT closed AND NOT retiring)",
                    identity.scope,
                    previous["active_slot"],
                ):
                    raise ReleaseConflict(
                        "Old slot is no longer available for rollback"
                    )
                await db.execute(
                    "UPDATE lightrag_release.scopes SET phase='ROLLBACK_DRAINING',gate_open=false,drain_deadline=clock_timestamp()+make_interval(secs=>coalesce((policy->>'drain_seconds')::double precision,30)) WHERE scope=$1",
                    identity.scope,
                )
                await db.execute(
                    "UPDATE lightrag_release.releases SET phase='ROLLBACK_DRAINING',rollback_approval=$3::jsonb WHERE scope=$1 AND release_id=$2",
                    identity.scope,
                    identity.release_id,
                    json.dumps(approval),
                )
                return await self._scope(db, identity.scope)

    async def reverse_transfer(self, identity, *, expected_epoch, expected_instances):
        async with self.connection() as db:
            async with db.transaction():
                row = await self._scope(db, identity.scope, lock=True)
                if (
                    row["phase"] != "ROLLBACK_DRAINING"
                    or row["epoch"] != expected_epoch
                    or row["owner"] != identity.release_id
                    or row["identity"] != identity.as_dict()
                    or row["gate_open"]
                ):
                    raise ReleaseConflict("Reverse transfer identity differs")
                record = await db.fetchrow(
                    "SELECT previous,rollback_approval FROM lightrag_release.releases WHERE scope=$1 AND release_id=$2",
                    identity.scope,
                    identity.release_id,
                )
                previous = json_value(record["previous"])
                if record["rollback_approval"] is None or await db.fetchval(
                    "SELECT clock_timestamp()>drain_deadline FROM lightrag_release.scopes WHERE scope=$1",
                    identity.scope,
                ):
                    raise ReleaseConflict(
                        "Rollback approval or drain budget unavailable"
                    )
                candidates = await db.fetch(
                    "SELECT instance_id,config_hash FROM lightrag_release.instances WHERE scope=$1 AND slot=$2 AND NOT closed AND NOT retiring",
                    identity.scope,
                    previous["active_slot"],
                )
                if (
                    not expected_instances
                    or {r["instance_id"] for r in candidates} != set(expected_instances)
                    or any(
                        r["config_hash"] != previous["config_hash"] for r in candidates
                    )
                ):
                    raise ReleaseConflict("Rollback candidate set differs")
                (await collect_drain_evidence(db, identity.scope)).require_empty()
                await db.execute(
                    "UPDATE lightrag_release.scopes SET active_slot=$2,candidate_slot=active_slot,epoch=epoch+1,config_hash=$3,gate_open=true,phase='ROLLBACK_WRITER_SWITCHED' WHERE scope=$1",
                    identity.scope,
                    previous["active_slot"],
                    previous["config_hash"],
                )
                await db.execute(
                    "UPDATE lightrag_release.releases SET phase='ROLLBACK_WRITER_SWITCHED' WHERE scope=$1 AND release_id=$2",
                    identity.scope,
                    identity.release_id,
                )
                return await self._scope(db, identity.scope)

    async def finish_rollback(self, identity, *, expected_epoch):
        async with self.connection() as db:
            async with db.transaction():
                row = await self._scope(db, identity.scope, lock=True)
                if (
                    row["owner"] != identity.release_id
                    or row["epoch"] != expected_epoch
                    or row["identity"] != identity.as_dict()
                    or row["phase"]
                    not in {"ROLLBACK_WRITER_SWITCHED", "ROLLBACK_RETIRING"}
                ):
                    raise ReleaseConflict("Rollback completion identity differs")
                next_phase = (
                    "ROLLBACK_RETIRING"
                    if row["phase"] == "ROLLBACK_WRITER_SWITCHED"
                    else "ROLLED_BACK"
                )
                if next_phase == "ROLLED_BACK" and await db.fetchval(
                    "SELECT EXISTS(SELECT 1 FROM lightrag_release.instances WHERE scope=$1 AND slot<>$2 AND NOT closed)",
                    identity.scope,
                    row["active_slot"],
                ):
                    raise ReleaseConflict("Rollback instances have not retired")
                await db.execute(
                    "UPDATE lightrag_release.scopes SET phase=$2,owner=CASE WHEN $2='ROLLED_BACK' THEN NULL ELSE owner END WHERE scope=$1",
                    identity.scope,
                    next_phase,
                )
                await db.execute(
                    "UPDATE lightrag_release.releases SET phase=$3,updated_at=clock_timestamp() WHERE scope=$1 AND release_id=$2",
                    identity.scope,
                    identity.release_id,
                    next_phase,
                )
                return await self._scope(db, identity.scope)

    async def retire_old_instances(self, identity, *, expected_epoch):
        async with self.connection() as connection:
            async with connection.transaction():
                row = await self._scope(connection, identity.scope, lock=True)
                if (
                    row["owner"] != identity.release_id
                    or row["epoch"] != expected_epoch
                    or row["identity"] != identity.as_dict()
                    or row["phase"] not in {"RETIRING", "ROLLBACK_RETIRING", "ABORTING"}
                ):
                    raise ReleaseConflict("Retirement identity mismatch")
                await connection.execute(
                    "UPDATE lightrag_release.instances SET retiring=true WHERE scope=$1 AND slot<>$2 AND NOT closed",
                    identity.scope,
                    row["active_slot"],
                )

    async def abort_before_transfer(self, identity, *, expected_epoch):
        # The caller first restores and observes the old route, keeping both
        # slots alive. A route failure must never reach this transaction.
        async with self.connection() as connection:
            async with connection.transaction():
                row = await self._scope(connection, identity.scope, lock=True)
                if (
                    row["owner"] != identity.release_id
                    or row["epoch"] != expected_epoch
                    or row["identity"] != identity.as_dict()
                    or row["phase"] not in {"PREPARED", "DRAINING", "READ_SWITCHED"}
                ):
                    raise ReleaseConflict(
                        "Abort is only allowed before writer transfer"
                    )
                uncertain = await connection.fetchval(
                    "SELECT EXISTS(SELECT 1 FROM lightrag_release.tickets WHERE scope=$1 AND state='UNCERTAIN') OR EXISTS(SELECT 1 FROM lightrag_release.commands WHERE scope=$1 AND state='UNCERTAIN')",
                    identity.scope,
                )
                if uncertain:
                    raise ReleaseConflict(
                        "Unknown writes prevent reopening the old gate"
                    )
                # Active old writes may finish; they are not cancelled. Unknown
                # owners, lost witnesses and a fenced scope still fail closed.
                proof = await collect_drain_evidence(connection, identity.scope)
                if proof.unknown_owners or proof.orphans:
                    raise ReleaseConflict("Old writer identity is uncertain")
                await connection.execute(
                    "UPDATE lightrag_release.scopes SET phase='ABORTING',gate_open=true,updated_at=clock_timestamp() WHERE scope=$1",
                    identity.scope,
                )
                await connection.execute(
                    "UPDATE lightrag_release.releases SET phase='ABORTING',updated_at=clock_timestamp() WHERE scope=$1 AND release_id=$2",
                    identity.scope,
                    identity.release_id,
                )
                return await self._scope(connection, identity.scope)

    async def finish_abort(self, identity, *, expected_epoch):
        async with self.connection() as db:
            async with db.transaction():
                row = await self._scope(db, identity.scope, lock=True)
                if (
                    row["owner"] != identity.release_id
                    or row["identity"] != identity.as_dict()
                    or row["epoch"] != expected_epoch
                    or row["phase"] != "ABORTING"
                    or not row["gate_open"]
                ):
                    raise ReleaseConflict("Abort completion identity differs")
                if await db.fetchval(
                    "SELECT EXISTS(SELECT 1 FROM lightrag_release.instances WHERE scope=$1 AND slot<>$2 AND NOT closed)",
                    identity.scope,
                    row["active_slot"],
                ):
                    raise ReleaseConflict("Candidate instances have not retired")
                proof = await collect_drain_evidence(db, identity.scope)
                if (
                    proof.unknown_owners
                    or proof.orphans
                    or proof.uncertain_tickets
                    or proof.uncertain_commands
                ):
                    raise ReleaseUnavailable("Writer uncertainty appeared during abort")
                await db.execute(
                    "UPDATE lightrag_release.scopes SET phase='ABORTED',owner=NULL,updated_at=clock_timestamp() WHERE scope=$1",
                    identity.scope,
                )
                await db.execute(
                    "UPDATE lightrag_release.releases SET phase='ABORTED',updated_at=clock_timestamp() WHERE scope=$1 AND release_id=$2",
                    identity.scope,
                    identity.release_id,
                )
                return await self._scope(db, identity.scope)

    async def record_evidence(self, identity, report):
        async with self.connection() as connection:
            row = await self._scope(connection, identity.scope)
            if row["identity"] != identity.as_dict():
                raise ReleaseConflict("Evidence identity mismatch")
            await connection.execute(
                "UPDATE lightrag_release.releases SET evidence=evidence||$3::jsonb WHERE scope=$1 AND release_id=$2",
                identity.scope,
                identity.release_id,
                json.dumps(report, allow_nan=False),
            )

    async def _scope(self, connection, scope, *, lock=False):
        row = await connection.fetchrow(
            "SELECT * FROM lightrag_release.scopes WHERE scope=$1"
            + (" FOR UPDATE" if lock else ""),
            scope,
        )
        if row is None:
            raise ReleaseUnavailable("Production scope is not enrolled")
        return row_dict(row)

    async def snapshot(self, scope):
        async with self.connection() as connection:
            async with connection.transaction(
                isolation="repeatable_read", readonly=True
            ):
                row = await self._scope(connection, scope)
                commands = await connection.fetch(
                    "SELECT state,count(*) AS count FROM lightrag_release.commands WHERE scope=$1 AND state IN ('QUEUED','EXECUTING','UNCERTAIN') GROUP BY state",
                    scope,
                )
                tickets = await connection.fetchval(
                    "SELECT count(*) FROM lightrag_release.tickets WHERE scope=$1 AND state<>'COMPLETED'",
                    scope,
                )
                return {
                    key: row[key]
                    for key in (
                        "scope",
                        "owner",
                        "epoch",
                        "phase",
                        "active_slot",
                        "candidate_slot",
                        "gate_open",
                        "config_hash",
                        "profile_hash",
                        "identity",
                    )
                } | {
                    "commands": {r["state"]: r["count"] for r in commands},
                    "unfinished_tickets": tickets,
                }
