"""Explicit offline planning, operator apply, read-only verify and scope registration."""

import argparse
import asyncio
import json
import math
import os
from pathlib import Path
import sys
from urllib.parse import parse_qs, urlsplit

from .plan import build_plan, digest
from .manifest import manifest_hash


def parser():
    result = argparse.ArgumentParser(description=__doc__)
    result.add_argument("command", choices=["plan", "apply", "verify", "register"])
    result.add_argument("--environment", required=True)
    result.add_argument("--confirm-maintenance", action="store_true")
    result.add_argument("--confirm-empty-target", action="store_true")
    result.add_argument("--expected-profile-hash")
    result.add_argument("--expected-plan-hash")
    result.add_argument("--expected-manifest-hash")
    result.add_argument("--graph-wait-seconds", type=float)
    return result


def check_dsn(dsn, environment):
    try:
        from urllib.parse import unquote

        uri = urlsplit(dsn)
        query = parse_qs(uri.query, keep_blank_values=True)
        mode = query.get("sslmode", [])
        if (
            uri.scheme not in {"postgres", "postgresql"}
            or uri.hostname != environment["POSTGRES_HOST"]
            or uri.port != int(environment["POSTGRES_PORT"])
            or not uri.path.startswith("/")
            or unquote(uri.path[1:]) != environment["POSTGRES_DATABASE"]
            or not uri.username
            or unquote(uri.username) == environment["POSTGRES_USER"]
            or uri.fragment
            or set(query) - {"sslmode", "sslrootcert", "sslcert", "sslkey", "sslcrl"}
            or any(len(v) != 1 for v in query.values())
            or len(mode) != 1
            or mode[0] not in {"disable", "require", "verify-full", "verify-ca"}
        ):
            raise ValueError()
    except Exception:
        raise ValueError(
            "Dedicated operator DSN must match the target and declare its transport policy"
        ) from None


async def run(args, environment, plan, dsn):
    import asyncpg
    from .engine import apply_all, verify_all, register_workspace
    from .graph import GraphProvisioner

    readonly = args.command == "verify"
    username = (
        environment.get("HUGEGRAPH_USERNAME")
        if readonly
        else os.getenv("LIGHTRAG_GRAPH_MIGRATION_USERNAME")
    )
    password = (
        environment.get("HUGEGRAPH_PASSWORD")
        if readonly
        else os.getenv("LIGHTRAG_GRAPH_MIGRATION_PASSWORD")
    )
    if not readonly and (
        not username or username == environment.get("HUGEGRAPH_USERNAME")
    ):
        raise ValueError(
            "Dedicated graph provisioning identity must differ from runtime"
        )
    async with GraphProvisioner(
        plan,
        environment["HUGEGRAPH_URI"],
        environment["HUGEGRAPH_GRAPHSPACE"],
        environment["HUGEGRAPH_GRAPH"],
        username,
        password,
        wait_seconds=args.graph_wait_seconds,
    ) as graph:
        conn = await asyncpg.connect(
            dsn,
            timeout=10,
            command_timeout=60,
            server_settings={
                "application_name": "lightrag-explicit-provisioning",
                "search_path": "public",
                "lock_timeout": "5000",
                "statement_timeout": "60000",
                "default_transaction_read_only": "on" if readonly else "off",
            },
        )
        try:
            if readonly:
                async with conn.transaction(readonly=True, isolation="repeatable_read"):
                    await verify_all(conn, plan, graph, environment["POSTGRES_USER"])
                return {"verified": True, "version": 1}
            if args.command == "apply":
                await apply_all(
                    conn,
                    plan,
                    graph,
                    environment["POSTGRES_USER"],
                    confirm_empty=args.confirm_empty_target,
                )
                return {
                    "migrated": True,
                    "version": 1,
                    "registered": False,
                    "release_enrolled": False,
                }
            return await register_workspace(
                conn,
                plan,
                graph,
                environment,
                expected_manifest_hash=args.expected_manifest_hash,
            )
        finally:
            await conn.close(timeout=5)


def main(argv=None):
    args = parser().parse_args(argv)
    try:
        path = Path(args.environment)
        if path.stat().st_size > 1048576:
            raise ValueError("Environment exceeds limit")
        environment = json.loads(path.read_text())
        plan = build_plan(environment)
        report = plan.summary()
        report.update(
            plan_hash=digest(report), baseline_manifest_hash=manifest_hash(environment)
        )
        if args.command == "plan":
            print(json.dumps(report, sort_keys=True))
            return 0
        if args.command in {"apply", "register"} and not args.confirm_maintenance:
            raise ValueError("Explicit maintenance confirmation required")
        if args.command == "apply" and not args.confirm_empty_target:
            raise ValueError("Explicit empty-target confirmation required")
        if (
            args.expected_profile_hash != plan.profile_hash
            or args.expected_plan_hash != report["plan_hash"]
        ):
            raise ValueError("Approved migration plan or physical profile differs")
        if (
            args.command == "register"
            and args.expected_manifest_hash != report["baseline_manifest_hash"]
        ):
            raise ValueError("Approved manifest differs")
        if (
            args.graph_wait_seconds is None
            or not math.isfinite(args.graph_wait_seconds)
            or args.graph_wait_seconds <= 0
        ):
            raise ValueError("Explicit graph wait budget required")
        dsn = os.getenv("LIGHTRAG_STORAGE_MIGRATION_DSN", "")
        check_dsn(dsn, environment)
        print(
            json.dumps(asyncio.run(run(args, environment, plan, dsn)), sort_keys=True)
        )
        return 0
    except Exception as error:
        print(
            type(error).__name__
            + ": provisioning failed; no credentials or backend error body emitted",
            file=sys.stderr,
        )
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
