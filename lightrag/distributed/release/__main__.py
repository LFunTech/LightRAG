"""Explicit release schema provisioning and credential-free read-only status."""

import argparse
import asyncio
import json
import os
import re
import sys

from .migrations import grant_runtime, migrate
from .store import ReleaseStore


def parser():
    result = argparse.ArgumentParser(description=__doc__)
    commands = result.add_subparsers(dest="command", required=True)
    provision = commands.add_parser("migrate")
    provision.add_argument("--confirm-maintenance", action="store_true")
    provision.add_argument("--runtime-role")
    enrollment = commands.add_parser("enroll")
    enrollment.add_argument("--confirm-maintenance", action="store_true")
    enrollment.add_argument("--config", required=True)
    enrollment.add_argument("--environment", required=True)
    enrollment.add_argument("--active-slot", choices=["blue", "green"], required=True)
    status = commands.add_parser("status")
    status.add_argument("--scope", required=True)
    delivery = commands.add_parser("deliver")
    delivery.add_argument("--config", required=True)
    delivery.add_argument("--identity", required=True)
    delivery.add_argument("--environment", required=True)
    delivery.add_argument("--image", required=True)
    delivery.add_argument(
        "--action", choices=["advance", "abort", "rollback"], default="advance"
    )
    delivery.add_argument("--approval")
    delivery.add_argument("--result", default="/dev/termination-log")
    return result


async def run(args):
    if args.command == "deliver":
        from .delivery import deliver

        return await deliver(
            config_path=args.config,
            identity_path=args.identity,
            environment_path=args.environment,
            image=args.image,
            action=args.action,
            approval_path=args.approval,
        )
    dsn = os.getenv("LIGHTRAG_RELEASE_OPERATOR_DSN", "")
    if not dsn:
        raise ValueError(
            "LIGHTRAG_RELEASE_OPERATOR_DSN is required; no business/test credential fallback"
        )
    if args.command in {"migrate", "enroll"} and not args.confirm_maintenance:
        raise ValueError("Explicit maintenance confirmation is required")
    if args.command == "status" and not re.fullmatch(
        r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", args.scope
    ):
        raise ValueError("Invalid release scope")
    import asyncpg

    connection = None
    try:
        connection = await asyncpg.connect(dsn, command_timeout=15)
        if args.command == "migrate":
            async with connection.transaction():
                await migrate(connection)
                if args.runtime_role:
                    await grant_runtime(connection, args.runtime_role)
            return {"schema": "lightrag_release", "version": 1, "verified": True}
        store = ReleaseStore(connection)
        await store.verify_schema()
        if args.command == "enroll":
            from pathlib import Path
            from lightrag_delivery.config import load_prod_config
            from lightrag_delivery.profile import physical_profile_hash
            from lightrag_delivery.render import content_hash

            config = load_prod_config(Path(args.config))
            environment = json.loads(Path(args.environment).read_text())
            scope = config.target["deployment_id"] + "/" + config.target["workspace"]
            if physical_profile_hash(environment) != config.onboarding["profile_hash"]:
                raise ValueError("Physical profile differs")
            async with connection.transaction():
                baseline = await connection.fetchrow(
                    "SELECT manifest_hash,fenced FROM lightrag_coordination.workspaces WHERE deployment_id=$1 AND workspace=$2 FOR UPDATE",
                    *scope.split("/"),
                )
                active = await connection.fetchval(
                    "SELECT EXISTS(SELECT 1 FROM lightrag_coordination.operations WHERE deployment_id=$1 AND workspace=$2 AND state='active')",
                    *scope.split("/"),
                )
                if (
                    baseline is None
                    or baseline["manifest_hash"]
                    != config.onboarding["baseline_manifest_hash"]
                    or baseline["fenced"]
                    or active
                ):
                    raise ValueError("Audited quiescent protocol baseline is required")
                policy = dict(config.budget, writers=config.writers)
                await store.onboard(
                    scope,
                    active_slot=args.active_slot,
                    profile_hash=config.onboarding["profile_hash"],
                    config_hash=content_hash(
                        {
                            "configuration": config.config_hash,
                            "environment": environment,
                        }
                    ),
                    policy=policy,
                )
            return await store.snapshot(scope)
        return await store.snapshot(args.scope)
    finally:
        if connection is not None:
            await connection.close()


def main(argv=None):
    args = parser().parse_args(argv)
    try:
        if not os.getenv("LIGHTRAG_RELEASE_OPERATOR_DSN"):
            print("LIGHTRAG_RELEASE_OPERATOR_DSN is required", file=sys.stderr)
            return 2
        if args.command in {"migrate", "enroll"} and not args.confirm_maintenance:
            print("Explicit maintenance confirmation is required", file=sys.stderr)
            return 2
        report = json.dumps(asyncio.run(run(args)), separators=(",", ":"))
        if args.command == "deliver":
            from pathlib import Path

            if len(report.encode()) > 4000:
                raise ValueError("Result exceeds termination-message bound")
            Path(args.result).write_text(report)
        print(report)
        return 0
    except Exception as error:
        print(
            type(error).__name__
            + ": release operation failed; inspect protected operator diagnostics",
            file=sys.stderr,
        )
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
