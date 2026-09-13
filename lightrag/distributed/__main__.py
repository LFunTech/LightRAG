"""Explicit coordination migration, read-only inspection and audited recovery CLI."""

import argparse
import asyncio
import json
import os
import sys

from .coordinator import CoordinationError, PostgresCoordinator


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(
        description="Durable workspace coordination (DSN from LIGHTRAG_COORDINATION_DSN only)"
    )
    commands = result.add_subparsers(dest="command", required=True)
    commands.add_parser(
        "migrate", help="Explicitly apply versioned coordination SQL migrations"
    )
    for name in ("inspect", "recover"):
        command = commands.add_parser(name)
        command.add_argument("--deployment-id", required=True)
        command.add_argument("--workspace", required=True)
        if name == "recover":
            command.add_argument("--expected-generation", required=True, type=int)
            command.add_argument("--actor", required=True)
            command.add_argument("--reason", required=True)
            command.add_argument("--confirm-writers-stopped", action="store_true")
            command.add_argument("--confirm-inflight-finished", action="store_true")
            command.add_argument("--confirm-state-audited", action="store_true")
    return result


async def run(args, dsn: str):
    if args.command == "migrate":
        await PostgresCoordinator.migrate(dsn)
        return {"migration": "verified"}
    coordinator = PostgresCoordinator(dsn, args.deployment_id, args.workspace)
    try:
        await coordinator.initialize(read_only=True)
        if args.command == "inspect":
            return await coordinator.inspect()
        return await coordinator.recover(
            expected_generation=args.expected_generation,
            actor=args.actor,
            reason=args.reason,
            writers_stopped=args.confirm_writers_stopped,
            inflight_finished=args.confirm_inflight_finished,
            state_audited=args.confirm_state_audited,
        )
    finally:
        await coordinator.close()


def main() -> int:
    cli = parser()
    args = cli.parse_args()
    if args.command == "recover" and not all(
        (
            args.confirm_writers_stopped,
            args.confirm_inflight_finished,
            args.confirm_state_audited,
        )
    ):
        cli.error(
            "Recovery requires all three --confirm-* flags after actual operational checks"
        )
    dsn = os.environ.get("LIGHTRAG_COORDINATION_DSN")
    if not dsn:
        cli.error("Set LIGHTRAG_COORDINATION_DSN in the environment")
    try:
        result = asyncio.run(run(args, dsn))
    except (CoordinationError, ValueError) as exc:
        sys.stderr.write(f"{type(exc).__name__}: {exc}\n")
        return 1
    except Exception:
        # Driver errors may contain passwords/DSNs. Never emit their text or traceback.
        sys.stderr.write("Coordination command failed; no recovery was confirmed\n")
        return 1
    sys.stdout.write(json.dumps(result, indent=2, ensure_ascii=False) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
