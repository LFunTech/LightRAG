"""Conservative Kustomize deployment helpers for the LightRAG test namespace."""

from __future__ import annotations

import argparse
import json
import re
import subprocess
from pathlib import Path
from typing import Any, Callable

import yaml

from scripts.ci.delivery import DeployStateError, validate_test_environment_snapshot

DIGEST_RE = re.compile(r"^sha256:[0-9a-f]{64}$")


class DeploymentError(RuntimeError):
    pass


def _run(args: list[str], *, input_text: str | None = None) -> str:
    result = subprocess.run(
        args, input=input_text, check=True, capture_output=True, text=True
    )
    return result.stdout


def inject_digest(overlay: Path, digest: str) -> None:
    if not DIGEST_RE.fullmatch(digest):
        raise DeploymentError("invalid digest")
    path = overlay / "release-image.yaml"
    data = yaml.safe_load(path.read_text())
    data.setdefault("data", {})["digest"] = digest
    path.write_text(yaml.safe_dump(data, sort_keys=False))


def _kubectl(kubeconfig: str, *args: str) -> list[str]:
    return ["kubectl", *args, "--kubeconfig", kubeconfig, "-n", "lightrag-test"]


def _restore_selector_patch() -> str:
    return json.dumps(
        {
            "spec": {
                "selector": {
                    "app.kubernetes.io/name": "lightrag",
                    "app.kubernetes.io/instance": "lightrag",
                }
            }
        }
    )


def _pause_selector_patch() -> str:
    return json.dumps({"spec": {"selector": {"lightrag.openai.com/routing-paused": "true"}}})


def collect_environment_snapshot(
    *, kubeconfig: str, runner: Callable[..., str] = _run
) -> dict[str, Any]:
    raw = runner(
        _kubectl(
            kubeconfig,
            "get",
            "configmap",
            "lightrag-test-environment",
            "-o",
            "jsonpath={.data.snapshot}",
        )
    )
    return json.loads(raw)


def validate_acceptance(report: dict[str, Any], *, expected_digest: str) -> None:
    if not DIGEST_RE.fullmatch(expected_digest):
        raise DeploymentError("invalid expected digest")
    pods = report.get("pods") or []
    if len(pods) != 2:
        raise DeploymentError("acceptance requires exactly two pods")
    for pod in pods:
        statuses = (pod.get("status") or {}).get("containerStatuses") or []
        image_id = statuses[0].get("imageID") if statuses else ""
        if expected_digest not in image_id:
            raise DeploymentError("pod imageID does not match verified digest")
    if report.get("health") != [200, 200]:
        raise DeploymentError("health acceptance failed")
    if report.get("unauthenticated_status") not in (401, 403):
        raise DeploymentError("authentication acceptance failed")
    if report.get("cross_pod_query_ok") is not True:
        raise DeploymentError("cross-pod ingestion/query acceptance failed")


def deploy_test(
    *,
    kubeconfig: str,
    digest: str,
    overlay: Path,
    runner: Callable[..., str] = _run,
) -> None:
    if not DIGEST_RE.fullmatch(digest):
        raise DeploymentError("invalid digest")
    # Stop new business traffic before touching writers.
    runner(_kubectl(kubeconfig, "patch", "service", "lightrag", "--type=merge", "-p", _pause_selector_patch()))
    # Gracefully drain old writers; do not force delete on timeout.
    runner(_kubectl(kubeconfig, "scale", "deployment/lightrag", "--replicas=0"))
    runner(
        _kubectl(
            kubeconfig,
            "wait",
            "--for=delete",
            "pod",
            "-l",
            "app.kubernetes.io/name=lightrag,app.kubernetes.io/instance=lightrag",
            "--timeout=600s",
        )
    )
    try:
        validate_test_environment_snapshot(
            collect_environment_snapshot(kubeconfig=kubeconfig, runner=runner)
        )
    except DeployStateError as exc:
        raise DeploymentError(str(exc)) from exc
    inject_digest(overlay, digest)
    runner(["kubectl", "apply", "-k", str(overlay), "--kubeconfig", kubeconfig])
    runner(_kubectl(kubeconfig, "rollout", "status", "deployment/lightrag", "--timeout=600s"))
    runner(_kubectl(kubeconfig, "get", "pods", "-l", "app.kubernetes.io/name=lightrag", "-o", "json"))


def accept_and_restore(
    *,
    kubeconfig: str,
    digest: str,
    report: dict[str, Any],
    runner: Callable[..., str] = _run,
) -> None:
    validate_acceptance(report, expected_digest=digest)
    runner(_kubectl(kubeconfig, "patch", "service", "lightrag", "--type=merge", "-p", _restore_selector_patch()))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="cmd", required=True)
    deploy = sub.add_parser("deploy-test")
    deploy.add_argument("--kubeconfig", required=True)
    deploy.add_argument("--digest", required=True)
    deploy.add_argument(
        "--overlay",
        type=Path,
        default=Path("k8s-deploy/lightrag-kustomize/overlays/test"),
    )
    accept = sub.add_parser("accept-and-restore")
    accept.add_argument("--kubeconfig", required=True)
    accept.add_argument("--digest", required=True)
    accept.add_argument("--report", type=Path, required=True)

    args = parser.parse_args(argv)
    if args.cmd == "deploy-test":
        deploy_test(kubeconfig=args.kubeconfig, digest=args.digest, overlay=args.overlay)
    elif args.cmd == "accept-and-restore":
        accept_and_restore(
            kubeconfig=args.kubeconfig,
            digest=args.digest,
            report=json.loads(args.report.read_text()),
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
