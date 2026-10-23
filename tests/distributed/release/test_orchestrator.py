import pytest


class Store:
    def __init__(self):
        self.state = dict(
            phase="PREPARED", epoch=1, active_slot="blue", candidate_slot="green"
        )
        self.calls = []

    async def begin(self, identity, expected_epoch):
        return self.state.copy()

    async def snapshot(self, scope):
        return self.state.copy()

    async def transition(self, identity, *, expected_epoch, expected_phase, next_phase):
        assert self.state["phase"] == expected_phase
        self.calls.append(next_phase)
        self.state["phase"] = next_phase
        return self.state.copy()

    async def wait_for_drain(self, identity, expected_epoch):
        self.calls.append("DRAIN_OBSERVED")

    async def transfer_writer(self, identity, *, expected_epoch, expected_instances):
        assert expected_instances == ["g1"]
        self.calls.append("TRANSFER")
        self.state.update(
            phase="WRITER_SWITCHED", epoch=2, active_slot="green", candidate_slot="blue"
        )
        return self.state.copy()

    async def retire_old_instances(self, identity, expected_epoch):
        self.calls.append("RETIRE")

    async def record_evidence(self, identity, report):
        self.report = report


class Transport:
    def __init__(self):
        self.calls = []
        self.route = "blue"
        self.fail_route = False

    async def prepare(self, identity, slot):
        self.calls.append("prepare:" + slot)

    async def verify_candidate(self, identity, slot):
        self.calls.append("verify:" + slot)
        return ["g1"]

    async def switch_read_route(self, identity, target):
        self.route = target
        self.calls.append("route:" + target)
        if self.fail_route:
            self.fail_route = False
            raise OSError("route ACK lost after switching")
        return {"provider": "qcloud", "target": target}

    async def verify_route(self, identity, target):
        assert self.route == target
        self.calls.append("observe:" + target)
        return {"provider": "qcloud", "target": target}

    async def wait_closed_and_scale(self, identity, old_slot):
        self.calls.append("scale:" + old_slot)


async def test_reentry_observes_route_after_lost_ack_before_transfer():
    from lightrag.distributed.release.orchestrator import ReleaseOrchestrator
    from lightrag.distributed.release.models import ReleaseIdentity

    store, transport = Store(), Transport()
    identity = ReleaseIdentity(
        "prod/work", "r1", "v1.0.0", "a" * 40, "sha256:" + "b" * 64, "c" * 64
    )
    runner = ReleaseOrchestrator(store, transport)
    await runner.advance(identity, expected_epoch=1)
    assert store.state["phase"] == "DRAINING"
    transport.fail_route = True
    with pytest.raises(OSError):
        await runner.advance(identity, expected_epoch=1)
    assert store.state["phase"] == "DRAINING"
    assert "TRANSFER" not in store.calls
    for _ in range(5):
        await runner.advance(identity, expected_epoch=1)
    assert store.state["phase"] == "COMPLETED"
    assert store.calls.count("TRANSFER") == 1
    assert transport.calls.index("observe:green") < transport.calls.index("scale:blue")


async def test_known_drain_budget_expiry_restores_blue_and_retires_only_green():
    from lightrag.distributed.release.orchestrator import ReleaseOrchestrator
    from lightrag.distributed.release.models import (
        ReleaseIdentity,
        ReleaseBudgetExceeded,
    )

    class BudgetStore(Store):
        async def wait_for_drain(self, identity, expected_epoch):
            raise ReleaseBudgetExceeded("known budget exhausted")

        async def abort_before_transfer(self, identity, expected_epoch):
            assert transport.route == "blue"
            self.state.update(phase="ABORTING", gate_open=True)
            self.calls.append("BLUE_GATE_OPEN")
            return self.state.copy()

        async def finish_abort(self, identity, expected_epoch):
            assert "scale:green" in transport.calls
            self.state["phase"] = "ABORTED"
            return self.state.copy()

    store, transport = BudgetStore(), Transport()
    store.state["phase"] = "READ_SWITCHED"
    transport.route = "green"
    identity = ReleaseIdentity(
        "prod/work", "r1", "v1.0.0", "a" * 40, "sha256:" + "b" * 64, "c" * 64
    )
    report = await ReleaseOrchestrator(store, transport).run(identity, expected_epoch=1)
    assert report["phase"] == "ABORTED" and report["gate_open"]
    assert "TRANSFER" not in store.calls and "scale:blue" not in transport.calls
