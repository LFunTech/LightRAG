"""Pinned-image control Job; runtime Pods never receive operator credentials."""

import json
from contextlib import asynccontextmanager
from dataclasses import dataclass
import os
from pathlib import Path

from lightrag_delivery.config import load_prod_config
from lightrag_delivery.kubernetes import Kubernetes
from lightrag_delivery.edgeone import cloud_observer
from lightrag_delivery.render import content_hash, select_prod_release
from .models import ReleaseConflict, ReleaseIdentity
from .orchestrator import ReleaseOrchestrator
from .store import ReleaseStore
from .transport import ProductionTransport


@dataclass(frozen=True)
class DeliverySession:
    config: object
    identity: ReleaseIdentity
    store: ReleaseStore
    transport: ProductionTransport
    original_epoch: int


@asynccontextmanager
async def delivery_session(*, config_path, identity_path, environment_path, image):
    """Hold the original operator connection/lock until the caller finishes."""
    config = load_prod_config(Path(config_path))
    identity = ReleaseIdentity(**json.loads(Path(identity_path).read_text()))
    environment = json.loads(Path(environment_path).read_text())
    actual_hash = content_hash(
        {"configuration": config.config_hash, "environment": environment}
    )
    if (
        identity.config_hash != actual_hash
        or identity.scope
        != config.target["deployment_id"] + "/" + config.target["workspace"]
        or not select_prod_release(
            event="tag", tag=identity.tag, activated=config.enabled
        )
    ):
        raise ReleaseConflict("Release input identity or activation differs")
    import asyncpg

    dsn = os.environ.get("LIGHTRAG_RELEASE_OPERATOR_DSN")
    if not dsn:
        raise ValueError("Separate operator DSN is required")
    connection = await asyncpg.connect(dsn, command_timeout=15)
    try:
        # Session lock serializes even two retries of the SAME durable owner.
        # It is not ownership: losing this connection never clears the owner.
        locked = await connection.fetchval(
            "SELECT pg_try_advisory_lock(hashtextextended($1,71239081))", identity.scope
        )
        if not locked:
            raise ReleaseConflict("Another runner is active; durable owner is retained")
        from .migrations import verify_runtime_role

        runtime_check = await asyncpg.connect(
            environment["LIGHTRAG_COORDINATION_DSN"], command_timeout=15
        )
        try:
            if (
                await runtime_check.fetchval("SELECT current_user")
                != config.onboarding["runtime_role"]
            ):
                raise ReleaseConflict("Runtime database principal differs")
            await verify_runtime_role(runtime_check)
        finally:
            await runtime_check.close()
        store = ReleaseStore(connection)
        await store.verify_schema()
        cloud = cloud_observer(config.ingress)
        transport = ProductionTransport(
            config=config,
            kubernetes=Kubernetes.in_cluster(config.target["namespace"]),
            cloud=cloud,
            store=store,
            image=image,
            environment=environment,
        )
        state = await transport.preflight(identity)
        existing = await connection.fetchval(
            "SELECT previous FROM lightrag_release.releases WHERE scope=$1 AND release_id=$2",
            identity.scope,
            identity.release_id,
        )
        original_epoch = (
            json.loads(existing)["epoch"]
            if isinstance(existing, str)
            else existing["epoch"]
            if existing
            else state["epoch"]
        )
        yield DeliverySession(config, identity, store, transport, original_epoch)

    finally:
        await connection.close()


async def deliver(
    *,
    config_path,
    identity_path,
    environment_path,
    image,
    action="advance",
    approval_path=None,
):
    async with delivery_session(
        config_path=config_path,
        identity_path=identity_path,
        environment_path=environment_path,
        image=image,
    ) as session:
        store, transport, identity, original_epoch = (
            session.store,
            session.transport,
            session.identity,
            session.original_epoch,
        )
        runner = ReleaseOrchestrator(store, transport)
        if action == "abort":
            return await runner.abort_before_transfer(
                identity, expected_epoch=original_epoch
            )
        if action == "rollback":
            if not approval_path:
                raise ReleaseConflict("Explicit rollback approval input is required")
            approval = json.loads(Path(approval_path).read_text())
            while True:
                report = await runner.rollback_after_transfer(
                    identity,
                    expected_epoch=original_epoch + 1,
                    approved_compatibility=approval,
                )
                if report["phase"] == "ROLLED_BACK":
                    return report
        return await runner.run(identity, expected_epoch=original_epoch)
