"""Runtime-only release functions; database grants cannot update control rows."""

import asyncio
import time
from uuid import uuid4
from lightrag.performance import measured_stage

from .commands import canonical
from .models import ReleaseConflict, ReleaseUnavailable, QueueFull
from .store import ReleaseStore, row_dict


class RuntimeStore(ReleaseStore):
    def __init__(self, database, *, scope, instance_id):
        super().__init__(database)
        self.scope = scope
        self.instance_id = instance_id

    @measured_stage("release")
    async def _call(self, function, *args):
        async with self.connection() as connection:
            try:
                return await connection.fetchval(
                    f"SELECT lightrag_release.{function}("
                    + ",".join(f"${n + 1}" for n in range(len(args)))
                    + ")",
                    *args,
                )
            except Exception as error:
                code = getattr(error, "sqlstate", None)
                if code == "P0002":
                    raise QueueFull("Release queue capacity exhausted") from None
                if code == "P0001":
                    raise ReleaseConflict(
                        "Release runtime identity or state conflict"
                    ) from None
                raise ReleaseUnavailable(
                    "Release runtime request failed; no retry of unknown writes"
                ) from None

    async def register(self, *, slot, config_hash, profile_hash, coordinator_owner):
        await self._call(
            "register_instance",
            self.scope,
            self.instance_id,
            slot,
            config_hash,
            profile_hash,
            coordinator_owner,
        )

    async def admit_writer(self, *, epoch, kind, command_id=None, parent_ticket=None):
        return await self._call(
            "admit_writer",
            self.scope,
            self.instance_id,
            epoch,
            uuid4(),
            kind,
            command_id,
            parent_ticket,
        )

    async def finish_ticket(self, ticket, *, uncertain=False):
        await self._call(
            "finish_ticket", self.scope, self.instance_id, ticket, uncertain
        )

    async def attach_ticket(self, ticket, operation_id):
        await self._call(
            "attach_ticket", self.scope, self.instance_id, ticket, operation_id
        )

    async def enqueue(self, envelope):
        if envelope.scope != self.scope:
            raise ReleaseConflict("Command scope differs from runtime")
        result = await self._call(
            "enqueue_command",
            self.scope,
            self.instance_id,
            uuid4(),
            envelope.principal["id"],
            envelope.idempotency_key,
            envelope.payload_hash,
            canonical(envelope.as_dict()),
        )
        return row_dict(result)

    async def command(self, command_id, *, principal_id):
        async with self.connection() as connection:
            row = await connection.fetchrow(
                "SELECT id,state,result,created_at,updated_at FROM lightrag_release.commands WHERE id=$1 AND scope=$2 AND principal_id=$3",
                command_id,
                self.scope,
                principal_id,
            )
            return row_dict(row) if row else None

    async def claim_next(self, *, epoch):
        result = await self._call(
            "claim_command", self.scope, self.instance_id, epoch, uuid4()
        )
        return row_dict(result) if result and result["id"] is not None else None

    async def finish_command(self, command_id, *, status, result):
        await self._call(
            "finish_command",
            self.scope,
            self.instance_id,
            command_id,
            status,
            canonical(result),
        )

    async def wait_result(self, receipt, *, principal_id, deadline):
        while True:
            result = await self.command(receipt["id"], principal_id=principal_id)
            if result is None:
                raise ReleaseUnavailable("Accepted release command cannot be read")
            if result["state"] in {"SUCCEEDED", "FAILED", "UNCERTAIN"}:
                return result
            if time.monotonic() >= deadline:
                return {"id": receipt["id"], "state": result["state"], "result": None}
            await asyncio.sleep(min(0.1, max(0, deadline - time.monotonic())))
