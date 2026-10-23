"""Explicit initial schema phases; Kubernetes rollout remains in the CI runner."""

import argparse
import asyncio
import json
import math
import os
from pathlib import Path
import re
import sys
from types import SimpleNamespace

from lightrag_delivery.config import load_prod_config
from lightrag_delivery.credentials import for_onboarding, validate_slot
from lightrag_delivery.render import content_hash, select_prod_release
from .plan import build_plan, digest

OPERATOR_FIELDS = {
    "LIGHTRAG_STORAGE_MIGRATION_DSN",
    "LIGHTRAG_RELEASE_OPERATOR_DSN",
    "LIGHTRAG_GRAPH_MIGRATION_USERNAME",
    "LIGHTRAG_GRAPH_MIGRATION_PASSWORD",
}
SETTINGS_FIELDS = {
    "version",
    "approval_id",
    "confirm_maintenance",
    "confirm_empty_target",
    "confirm_shared_credentials",
    "expected_plan_hash",
    "graph_wait_seconds",
    "job_wait_seconds",
    "startup_wait_seconds",
}


def validate_inputs(config, identity, environment, settings):
    if config.ingress["mode"] != "kubernetes":
        raise ValueError("Initial delivery requires the Kubernetes-only profile")
    if not isinstance(settings, dict) or set(settings) != SETTINGS_FIELDS:
        raise ValueError("Complete explicit initial settings required")
    if (
        type(settings["version"]) is not int
        or settings["version"] != 1
        or not isinstance(settings["approval_id"], str)
        or not re.fullmatch(r"[A-Za-z0-9_.-]{1,128}", settings["approval_id"])
    ):
        raise ValueError("Versioned initial execution approval required")
    for field in ("confirm_maintenance", "confirm_empty_target"):
        if settings[field] is not True:
            raise ValueError(
                "Initial maintenance and empty-target confirmations required"
            )
    exception = for_onboarding(config)
    if type(settings["confirm_shared_credentials"]) is not bool or settings[
        "confirm_shared_credentials"
    ] != (exception is not None):
        raise ValueError(
            "Explicit matching shared credential execution confirmation required"
        )
    for field in ("graph_wait_seconds", "job_wait_seconds", "startup_wait_seconds"):
        value = settings[field]
        if type(value) not in (int, float) or not math.isfinite(value) or value <= 0:
            raise ValueError("Finite positive initial observation budgets required")
    if settings["job_wait_seconds"] < 7 * settings["graph_wait_seconds"] + 240:
        raise ValueError("Initial Job observation budget is insufficient")
    if settings["startup_wait_seconds"] < 60:
        raise ValueError("Explicit image/startup observation budget required")
    plan = build_plan(environment)
    if (
        settings["expected_plan_hash"] != digest(plan.summary())
        or plan.profile_hash != config.onboarding["profile_hash"]
        or identity["scope"]
        != config.target["deployment_id"] + "/" + config.target["workspace"]
        or identity["config_hash"]
        != content_hash(
            {"configuration": config.config_hash, "environment": environment}
        )
        or not select_prod_release(
            event="tag", tag=identity["tag"], activated=config.enabled
        )
    ):
        raise ValueError("Initial plan, profile or release identity differs")
    validate_slot(config, identity, "blue", environment)
    return plan


def validate_operator(environment, operator, *, confirm_shared):
    from lightrag_delivery.credentials import validate_operator_dsn as check_dsn

    if (
        not isinstance(operator, dict)
        or set(operator) != OPERATOR_FIELDS
        or any(not isinstance(v, str) or not v for v in operator.values())
    ):
        raise ValueError("Explicit complete initial operator inputs required")
    for key in ("LIGHTRAG_STORAGE_MIGRATION_DSN", "LIGHTRAG_RELEASE_OPERATOR_DSN"):
        check_dsn(operator[key], environment, confirm_shared=confirm_shared)
    graph_user = operator["LIGHTRAG_GRAPH_MIGRATION_USERNAME"]
    if confirm_shared:
        if (
            graph_user != environment["HUGEGRAPH_USERNAME"]
            or operator["LIGHTRAG_GRAPH_MIGRATION_PASSWORD"]
            != environment["HUGEGRAPH_PASSWORD"]
        ):
            raise ValueError(
                "Initial graph credentials differ from approved shared identity"
            )
    elif graph_user == environment["HUGEGRAPH_USERNAME"]:
        raise ValueError("Initial graph operator must differ from runtime")


async def run(config_path, identity_path, environment_path, settings_path):
    config = load_prod_config(Path(config_path))
    identity = json.loads(Path(identity_path).read_text())
    environment = json.loads(Path(environment_path).read_text())
    settings = json.loads(Path(settings_path).read_text())
    plan = validate_inputs(config, identity, environment, settings)
    from .manifest import manifest_hash

    if manifest_hash(environment) != config.onboarding["baseline_manifest_hash"]:
        raise ValueError("Initial baseline manifest differs")
    operator = {key: os.environ.get(key, "") for key in OPERATOR_FIELDS}
    validate_operator(
        environment, operator, confirm_shared=settings["confirm_shared_credentials"]
    )
    from .__main__ import run as provision
    from lightrag.distributed.release.__main__ import (
        run as release,
        parser as release_parser,
    )

    for phase in ("apply", "verify", "register"):
        args = SimpleNamespace(
            command=phase,
            graph_wait_seconds=settings["graph_wait_seconds"],
            confirm_shared_credentials=settings["confirm_shared_credentials"],
            confirm_maintenance=True,
            confirm_empty_target=True,
            expected_manifest_hash=config.onboarding["baseline_manifest_hash"],
        )
        await provision(
            args, environment, plan, operator["LIGHTRAG_STORAGE_MIGRATION_DSN"]
        )
    args = release_parser().parse_args(
        [
            "enroll",
            "--confirm-maintenance",
            "--config",
            str(config_path),
            "--environment",
            str(environment_path),
            "--active-slot",
            "blue",
        ]
    )
    state = await release(args)
    if (
        state["active_slot"] != "blue"
        or state["epoch"] != 1
        or state["phase"] != "IDLE"
        or not state["gate_open"]
        or state["owner"] is not None
    ):
        raise ValueError("Initial enrolled state differs")
    return {
        "phase": "INITIALIZED",
        "scope": identity["scope"],
        "release_id": identity["release_id"],
        "config_hash": identity["config_hash"],
        "plan_hash": settings["expected_plan_hash"],
        "settings_hash": content_hash(settings),
        "profile_hash": plan.profile_hash,
        "epoch": 1,
    }


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("config", "identity", "environment", "settings"):
        parser.add_argument("--" + name, required=True, type=Path)
    args = parser.parse_args(argv)
    try:
        report = asyncio.run(
            run(args.config, args.identity, args.environment, args.settings)
        )
        output = json.dumps(report, sort_keys=True)
        if len(output.encode()) > 4000:
            raise ValueError("Initial result exceeds termination message limit")
        Path("/dev/termination-log").write_text(output)
        print(output)
        return 0
    except Exception as error:
        print(
            type(error).__name__
            + ": initial provisioning stopped; no automatic recovery or replay",
            file=sys.stderr,
        )
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
