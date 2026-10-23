"""One durable phase per call. Unknown side effects are observed on reentry."""

from .models import ReleaseConflict, ReleaseBudgetExceeded


class ReleaseOrchestrator:
    def __init__(self, store, transport):
        self.store = store
        self.transport = transport

    async def advance(self, identity, *, expected_epoch):
        row = await self.store.begin(identity, expected_epoch=expected_epoch)
        phase, epoch = row["phase"], row.get("epoch", expected_epoch)
        if phase in {"COMPLETED", "ABORTED", "ROLLED_BACK"}:
            return row
        if phase == "ABORTING":
            return await self.abort_before_transfer(
                identity, expected_epoch=expected_epoch
            )
        target = (
            row["candidate_slot"]
            if phase in {"PREPARED", "DRAINING", "READ_SWITCHED"}
            else row["active_slot"]
        )
        if phase == "PREPARED":
            await self.transport.prepare(identity, target)
            await self.transport.verify_candidate(identity, target)
            await self.store.transition(
                identity,
                expected_epoch=epoch,
                expected_phase=phase,
                next_phase="DRAINING",
            )
        elif phase == "DRAINING":
            await self.transport.verify_candidate(identity, target)
            await self.transport.switch_read_route(identity, target)
            proof = await self.transport.verify_route(identity, target)
            await self.store.record_evidence(identity, {"route": proof})
            await self.store.transition(
                identity,
                expected_epoch=epoch,
                expected_phase=phase,
                next_phase="READ_SWITCHED",
            )
        elif phase == "READ_SWITCHED":
            await self.store.wait_for_drain(identity, expected_epoch=epoch)
            instances = await self.transport.verify_candidate(identity, target)
            await self.transport.verify_route(identity, target)
            await self.store.transfer_writer(
                identity, expected_epoch=epoch, expected_instances=instances
            )
        elif phase == "WRITER_SWITCHED":
            await self.transport.verify_route(identity, target)
            await self.store.transition(
                identity,
                expected_epoch=epoch,
                expected_phase=phase,
                next_phase="RETIRING",
            )
        elif phase == "RETIRING":
            await self.transport.verify_route(identity, target)
            await self.store.retire_old_instances(identity, expected_epoch=epoch)
            await self.transport.wait_closed_and_scale(identity, row["candidate_slot"])
            await self.store.transition(
                identity,
                expected_epoch=epoch,
                expected_phase=phase,
                next_phase="COMPLETED",
            )
        else:
            raise ReleaseConflict("Release is not resumable in its current phase")
        report = await self.store.snapshot(identity.scope)
        await self.store.record_evidence(identity, report)
        return report

    async def run(self, identity, *, expected_epoch):
        while True:
            try:
                report = await self.advance(identity, expected_epoch=expected_epoch)
            except ReleaseBudgetExceeded:
                snapshot = await self.store.snapshot(identity.scope)
                if snapshot["phase"] not in {"DRAINING", "READ_SWITCHED"}:
                    raise
                report = await self.abort_before_transfer(
                    identity, expected_epoch=expected_epoch
                )
            if report["phase"] in {"COMPLETED", "ABORTED", "ROLLED_BACK"}:
                return report

    async def abort_before_transfer(self, identity, *, expected_epoch):
        row = await self.store.begin(identity, expected_epoch=expected_epoch)
        if row["phase"] == "ABORTED":
            return row
        if row["phase"] not in {"PREPARED", "DRAINING", "READ_SWITCHED", "ABORTING"}:
            raise ReleaseConflict("Abort is not allowed after writer transfer")
        if row["phase"] != "ABORTING":
            await self.transport.switch_read_route(identity, row["active_slot"])
            await self.transport.verify_route(identity, row["active_slot"])
            row = await self.store.abort_before_transfer(
                identity, expected_epoch=row["epoch"]
            )
        else:
            await self.transport.verify_route(identity, row["active_slot"])
        await self.store.retire_old_instances(identity, expected_epoch=row["epoch"])
        await self.transport.wait_closed_and_scale(identity, row["candidate_slot"])
        await self.transport.verify_route(identity, row["active_slot"])
        result = await self.store.finish_abort(identity, expected_epoch=row["epoch"])
        report = await self.store.snapshot(identity.scope)
        await self.store.record_evidence(identity, report)
        return result

    async def rollback_after_transfer(
        self, identity, *, expected_epoch, approved_compatibility
    ):
        row = await self.store.begin_rollback(
            identity, expected_epoch=expected_epoch, approval=approved_compatibility
        )
        if row["phase"] == "ROLLED_BACK":
            return await self.store.snapshot(identity.scope)
        if row["phase"] == "ROLLBACK_DRAINING":
            await self.store.wait_for_drain(identity, expected_epoch=row["epoch"])
            target = row["candidate_slot"]
            instances = await self.transport.verify_rollback_candidate(
                identity, target, approved_compatibility
            )
            await self.transport.switch_read_route(identity, target)
            await self.transport.verify_route(identity, target)
            await self.store.reverse_transfer(
                identity, expected_epoch=row["epoch"], expected_instances=instances
            )
        elif row["phase"] == "ROLLBACK_WRITER_SWITCHED":
            await self.transport.verify_route(identity, row["active_slot"])
            await self.store.finish_rollback(identity, expected_epoch=row["epoch"])
        elif row["phase"] == "ROLLBACK_RETIRING":
            await self.transport.verify_route(identity, row["active_slot"])
            await self.store.retire_old_instances(identity, expected_epoch=row["epoch"])
            await self.transport.wait_closed_and_scale(identity, row["candidate_slot"])
            await self.store.finish_rollback(identity, expected_epoch=row["epoch"])
        return await self.store.snapshot(identity.scope)
