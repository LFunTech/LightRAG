"""Offline initialization Job renderer referencing pre-existing private Secrets."""

import argparse
import json
import math
from pathlib import Path
import re

from .manifest import manifest_hash
from .plan import build_plan, digest


def render_job(settings, environment):
    fields = {
        "phase",
        "namespace",
        "name",
        "image",
        "environment_secret",
        "environment_key",
        "operator_secret",
        "operator_key",
        "graph_secret",
        "graph_user_key",
        "graph_password_key",
        "image_pull_secret",
        "graph_wait_seconds",
        "active_deadline_seconds",
        "ttl_seconds",
        "cpu_request",
        "cpu_limit",
        "memory_request",
        "memory_limit",
        "confirm_maintenance",
        "confirm_empty_target",
        "expected_profile_hash",
        "expected_plan_hash",
        "expected_manifest_hash",
    }
    if (
        not isinstance(settings, dict)
        or set(settings) - {"confirm_shared_credentials"} != fields
    ):
        raise ValueError("Explicit complete initialization Job settings required")
    s = settings
    shared_confirmation = s.get("confirm_shared_credentials")
    if "confirm_shared_credentials" in s and type(shared_confirmation) is not bool:
        raise ValueError("Explicit boolean shared credential confirmation required")
    if s["phase"] not in {"apply", "verify", "register"}:
        raise ValueError("Explicit operator phase required")
    for key in (
        "namespace",
        "name",
        "environment_secret",
        "operator_secret",
        "graph_secret",
        "image_pull_secret",
    ):
        if (
            not isinstance(s[key], str)
            or len(s[key]) > 63
            or not re.fullmatch(r"[a-z0-9](?:[a-z0-9-]*[a-z0-9])?", s[key])
        ):
            raise ValueError("Invalid existing Kubernetes reference: " + key)
    for key in (
        "environment_key",
        "operator_key",
        "graph_user_key",
        "graph_password_key",
    ):
        if not isinstance(s[key], str) or not re.fullmatch(
            r"[A-Za-z0-9][A-Za-z0-9_.-]{0,252}", s[key]
        ):
            raise ValueError("Invalid existing Secret key reference")
    if s["operator_secret"] == s["environment_secret"]:
        raise ValueError("Operator and runtime Secret references must be separate")
    if not isinstance(s["image"], str) or not re.fullmatch(
        r"[a-zA-Z0-9][a-zA-Z0-9./:_-]*@sha256:[a-f0-9]{64}", s["image"]
    ):
        raise ValueError("Immutable image digest required")
    wait = s["graph_wait_seconds"]
    if type(wait) not in (int, float) or not math.isfinite(wait) or wait <= 0:
        raise ValueError("Explicit graph wait budget required")
    for key in ("active_deadline_seconds", "ttl_seconds"):
        if type(s[key]) is not int or s[key] <= 0:
            raise ValueError("Explicit positive Job deadline and retention required")
    if s["active_deadline_seconds"] <= wait + 10:
        raise ValueError("Job deadline must exceed the graph and connection budget")
    for key in ("cpu_request", "cpu_limit", "memory_request", "memory_limit"):
        if not isinstance(s[key], str) or not re.fullmatch(
            r"[1-9][0-9]*(?:\.[0-9]+)?(?:m|Ki|Mi|Gi)?", s[key]
        ):
            raise ValueError("Explicit positive Job resource quantities required")
    if (
        type(s["confirm_maintenance"]) is not bool
        or type(s["confirm_empty_target"]) is not bool
    ):
        raise ValueError("Explicit confirmation flags required")
    if s["phase"] != "verify" and not s["confirm_maintenance"]:
        raise ValueError("Maintenance approval required")
    if s["phase"] == "apply" and not s["confirm_empty_target"]:
        raise ValueError("Empty-target approval required")
    if environment.get("LIGHTRAG_OBJECT_STORAGE") != "s3":
        raise ValueError("Production provisioning requires the approved S3 profile")
    plan = build_plan(environment)
    if plan.credential_exception is not None and s["phase"] != "verify":
        if shared_confirmation is not True:
            raise ValueError(
                "Explicit shared first-release execution confirmation required"
            )
    elif plan.credential_exception is None and shared_confirmation is True:
        raise ValueError(
            "Shared credential confirmation requires an approved exception"
        )
    if s["expected_profile_hash"] != plan.profile_hash or s[
        "expected_plan_hash"
    ] != digest(plan.summary()):
        raise ValueError("Approved initialization plan differs")
    if s["phase"] == "register" and s["expected_manifest_hash"] != manifest_hash(
        environment
    ):
        raise ValueError("Approved workspace manifest differs")
    args = [
        s["phase"],
        "--environment",
        "/run/lightrag/environment.json",
        "--expected-profile-hash",
        s["expected_profile_hash"],
        "--expected-plan-hash",
        s["expected_plan_hash"],
        "--graph-wait-seconds",
        str(wait),
    ]
    if plan.credential_exception is not None and s["phase"] != "verify":
        args += ["--confirm-shared-credentials"]
    if s["phase"] != "verify":
        args += ["--confirm-maintenance"]
    if s["phase"] == "apply":
        args += ["--confirm-empty-target"]
    if s["phase"] == "register":
        args += ["--expected-manifest-hash", s["expected_manifest_hash"]]
    env = [
        {"name": "PYTHON_DOTENV_DISABLED", "value": "1"},
        {"name": "LOG_DIR", "value": "/tmp"},
    ]
    for name, secret, key in [
        ("LIGHTRAG_STORAGE_MIGRATION_DSN", s["operator_secret"], s["operator_key"]),
        ("LIGHTRAG_GRAPH_MIGRATION_USERNAME", s["graph_secret"], s["graph_user_key"]),
        (
            "LIGHTRAG_GRAPH_MIGRATION_PASSWORD",
            s["graph_secret"],
            s["graph_password_key"],
        ),
    ]:
        env.append(
            {"name": name, "valueFrom": {"secretKeyRef": {"name": secret, "key": key}}}
        )
    container = {
        "name": "provisioning",
        "image": s["image"],
        "imagePullPolicy": "IfNotPresent",
        "command": ["python", "-m", "lightrag.provisioning"],
        "args": args,
        "env": env,
        "resources": {
            "requests": {"cpu": s["cpu_request"], "memory": s["memory_request"]},
            "limits": {"cpu": s["cpu_limit"], "memory": s["memory_limit"]},
        },
        "securityContext": {
            "allowPrivilegeEscalation": False,
            "readOnlyRootFilesystem": True,
            "capabilities": {"drop": ["ALL"]},
        },
        "volumeMounts": [
            {"name": "environment", "mountPath": "/run/lightrag", "readOnly": True},
            {"name": "scratch", "mountPath": "/tmp"},
        ],
    }
    return {
        "apiVersion": "batch/v1",
        "kind": "Job",
        "metadata": {"name": s["name"], "namespace": s["namespace"]},
        "spec": {
            "backoffLimit": 0,
            "activeDeadlineSeconds": s["active_deadline_seconds"],
            "ttlSecondsAfterFinished": s["ttl_seconds"],
            "template": {
                "metadata": {
                    "labels": {"lightrag.io/component": "explicit-provisioning"}
                },
                "spec": {
                    "automountServiceAccountToken": False,
                    "restartPolicy": "Never",
                    "imagePullSecrets": [{"name": s["image_pull_secret"]}],
                    "securityContext": {
                        "runAsNonRoot": True,
                        "runAsUser": 1000,
                        "runAsGroup": 1000,
                        "fsGroup": 1000,
                        "seccompProfile": {"type": "RuntimeDefault"},
                    },
                    "containers": [container],
                    "volumes": [
                        {
                            "name": "environment",
                            "secret": {
                                "secretName": s["environment_secret"],
                                "defaultMode": 0o440,
                                "items": [
                                    {
                                        "key": s["environment_key"],
                                        "path": "environment.json",
                                    }
                                ],
                            },
                        },
                        {"name": "scratch", "emptyDir": {}},
                    ],
                },
            },
        },
    }


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--settings", required=True, type=Path)
    parser.add_argument("--environment", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args(argv)
    job = render_job(
        json.loads(args.settings.read_text()), json.loads(args.environment.read_text())
    )
    with args.output.open("x") as output:
        json.dump(job, output, indent=2)
        output.write("\n")
    print("Initialization Job rendered; no resources applied or Secrets created")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
