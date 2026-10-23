"""Trusted in-process adapter to the existing release owner, never a shell hook."""

import hashlib
import json
from urllib.parse import urlsplit

PHASES = (
    "PREPARED",
    "DRAINING",
    "READ_SWITCHED",
    "WRITER_SWITCHED",
    "RETIRING",
    "COMPLETED",
)


class OrchestratorControl:
    def __init__(self, store, transport, identity, *, expected_epoch):
        self.store, self.transport, self.identity, self.epoch = (
            store,
            transport,
            identity,
            expected_epoch,
        )

    def validate(self, settings):
        i = self.identity
        if (
            settings["scope"] != i.scope
            or settings["release_id"] != i.release_id
            or settings["green_config_hash"] != i.config_hash
            or settings["epoch"] != self.epoch
            or settings.get("green_digest", i.digest) != i.digest
        ):
            raise ValueError("Rehearsal release identity differs")
        config = getattr(self.transport, "config", None)
        if config is not None and (
            config.target["namespace"] != settings["namespace"]
            or config.onboarding["namespace_uid"] != settings["namespace_uid"]
            or config.onboarding["kube_system_uid"] != settings["kube_system_uid"]
            or config.onboarding["profile_hash"] != settings["profile_hash"]
            or config.replicas != 4
        ):
            raise ValueError("Isolated namespace/profile differs")

    def validate_target(self, base_url):
        config = getattr(self.transport, "config", None)
        u = urlsplit(base_url)
        if (
            config is None
            or u.scheme != "https"
            or u.hostname != config.target["host"]
            or u.port not in {None, 443}
        ):
            raise ValueError("Data origin differs from the verified qcloud entry")

    async def start(self, settings):
        self.validate(settings)
        state = await self.store.snapshot(self.identity.scope)
        if state.get("owner") is not None:
            raise ValueError(
                "Existing owner requires separate investigation; no resume"
            )
        members = await self.transport.verified_pods(
            "blue",
            config_hash=settings["blue_config_hash"],
            digest=settings["blue_digest"],
        )
        if len(members) != 4:
            raise ValueError("A complete baseline slot is required")
        row = await self.store.begin(self.identity, expected_epoch=self.epoch)
        if row["phase"] != "PREPARED":
            raise ValueError("Fresh rehearsal only; no automatic resume/replay")
        await self.store.capture_baseline(
            self.identity, expected_epoch=self.epoch, digest=settings["blue_digest"]
        )

    async def usage(self, settings):
        from scripts.performance.usage_control import collect_usage

        return await collect_usage(self, settings)

    async def advance(self, settings, *, usage_gate=None):
        self.validate(settings)
        from lightrag.distributed.release.orchestrator import ReleaseOrchestrator

        transport = self.transport
        if usage_gate is not None:
            usage_gate.ensure_running()
            transport = _UsageRetirementTransport(transport, usage_gate)
        return await ReleaseOrchestrator(self.store, transport).advance(
            self.identity, expected_epoch=self.epoch
        )

    async def receipt(self, settings, *, key, principal_id):
        self.validate(settings)
        async with self.store.connection() as db:
            rows = await db.fetch(
                "SELECT id,state FROM lightrag_release.commands WHERE scope=$1 AND principal_id=$2 AND idempotency_key=$3 LIMIT 2",
                self.identity.scope,
                principal_id,
                key,
            )
        if len(rows) > 1:
            raise ValueError("Ambiguous command identity")
        return {"id": str(rows[0]["id"]), "state": rows[0]["state"]} if rows else None

    async def inspect(self, settings):
        self.validate(settings)
        state = await self.store.snapshot(self.identity.scope)
        phase = state["phase"]
        moved = phase in PHASES[3:]
        if (
            phase not in PHASES
            or state["epoch"] != self.epoch + int(moved)
            or state["active_slot"] != ("green" if moved else "blue")
            or (phase != "COMPLETED" and state.get("owner") != self.identity.release_id)
        ):
            raise ValueError("Release phase/owner/epoch differs")
        if state.get("commands", {}).get("UNCERTAIN"):
            raise ValueError("Uncertain command prevents rehearsal")
        instances = []
        for slot in ("blue", "green"):
            count = len(await self.transport.pods(slot))
            want = 0 if phase == "COMPLETED" and slot == "blue" else 4
            if phase == "PREPARED" and slot == "green" and count == 0:
                continue
            if count != want:
                raise ValueError("A complete slot is required")
            if not want:
                continue
            options = dict(config_hash=settings[slot + "_config_hash"])
            if slot + "_digest" in settings:
                options["digest"] = settings[slot + "_digest"]
            members = await self.transport.verified_pods(slot, **options)
            if len(members) != 4:
                raise ValueError("A complete slot is required")
            for pod, status in members:
                if (
                    status["instance_id"] != pod["metadata"]["uid"]
                    or status["slot"] != slot
                    or status["profile_hash"] != settings["profile_hash"]
                    or status["config_hash"] != settings[slot + "_config_hash"]
                    or status.get("readiness", {}).get("ready") is not True
                ):
                    raise ValueError("Direct instance identity differs")
                instances.append(
                    dict(
                        uid=status["instance_id"],
                        slot=slot,
                        lifecycle=status.get("lifecycle", {}),
                    )
                )
        if len({r["uid"] for r in instances}) != len(instances):
            raise ValueError("Duplicate instance UID")
        read_slot = "green" if phase in PHASES[2:] else "blue"
        proof = await self.transport.verify_route(self.identity, read_slot)
        if not isinstance(proof, dict) or not proof:
            raise ValueError("Route evidence required")
        return dict(
            phase=phase,
            epoch=state["epoch"],
            instances=instances,
            route_evidence_sha256=hashlib.sha256(
                json.dumps(proof, sort_keys=True, default=str).encode()
            ).hexdigest(),
        )


class _UsageRetirementTransport:
    def __init__(self, transport, usage):
        self.transport, self.usage = transport, usage

    def __getattr__(self, name):
        return getattr(self.transport, name)

    async def wait_closed_and_scale(self, identity, slot):
        await self.usage.before_scale()
        self.usage.ensure_running()
        return await self.transport.wait_closed_and_scale(identity, slot)
