"""Resume only application resources from an explicitly approved successful receipt."""

import argparse
import base64
import json
import math
import os
from pathlib import Path
import re
import sys
import tempfile
import time

from lightrag_delivery.config import ProdConfigError, load_prod_config
from lightrag_delivery.render import (
    content_hash,
    labels,
    render_slot,
    render_entrypoint,
)
from lightrag.provisioning.initial import validate_inputs
from scripts.ci.prod_deploy import Kubectl, validate_cluster_identity
from scripts.ci.prod_initial import managed_equal, create_exact, wait_ready
from scripts.ci.prod_app_contract import application_matches, pod_spec

FIELDS = {
    "version",
    "approval_id",
    "source_pipeline",
    "namespace",
    "configmap",
    "initial_job",
    "initial_job_uid",
    "initial_job_spec_hash",
    "initial_pod_uid",
    "onboarding_hash",
    "settings_hash",
    "identity",
    "image",
    "request_timeout_seconds",
}


def validate_approval(approval, confirm):
    if not isinstance(approval, dict) or set(approval) != FIELDS:
        raise ProdConfigError("Complete application resume approval required")
    if type(approval["version"]) is not int or approval["version"] != 1:
        raise ProdConfigError("Unsupported application resume approval")
    for key in FIELDS - {
        "version",
        "source_pipeline",
        "identity",
        "request_timeout_seconds",
        "image",
    }:
        if not isinstance(approval[key], str) or not re.fullmatch(
            r"[A-Za-z0-9_.-]{1,128}", approval[key]
        ):
            raise ProdConfigError("Invalid application resume approval field")
    if confirm != approval["approval_id"]:
        raise ProdConfigError(
            "Explicit matching application-only confirmation required"
        )
    if type(approval["source_pipeline"]) is not int or approval["source_pipeline"] <= 0:
        raise ProdConfigError("Original pipeline identity required")
    timeout = approval["request_timeout_seconds"]
    if (
        type(timeout) not in (int, float)
        or not math.isfinite(timeout)
        or not 1 <= timeout <= 120
    ):
        raise ProdConfigError("Bounded Kubernetes request budget required")
    for key in ("initial_job_spec_hash", "onboarding_hash", "settings_hash"):
        if not re.fullmatch(r"[a-f0-9]{64}", approval[key]):
            raise ProdConfigError("Original receipt hashes required")
    identity = approval["identity"]
    if not isinstance(identity, dict) or set(identity) != {
        "scope",
        "release_id",
        "tag",
        "commit",
        "digest",
        "config_hash",
        "protocol_version",
    }:
        raise ProdConfigError("Complete original release identity required")
    if not all(
        isinstance(identity[k], str) for k in identity if k != "protocol_version"
    ) or (
        not re.fullmatch(r"v[0-9]+\.[0-9]+\.[0-9]+", identity["tag"])
        or not re.fullmatch(r"[a-f0-9]{40}", identity["commit"])
        or not re.fullmatch(r"sha256:[a-f0-9]{64}", identity["digest"])
        or not re.fullmatch(r"[a-f0-9]{64}", identity["config_hash"])
        or type(identity["protocol_version"]) is not int
        or identity["protocol_version"] != 1
        or identity["release_id"]
        != "prod-" + identity["tag"] + "-" + identity["commit"][:12]
    ):
        raise ProdConfigError("Original immutable release identity differs")
    if not re.fullmatch(
        r"[A-Za-z0-9.-]+(?::[0-9]+)?/[A-Za-z0-9_./-]+", approval["image"]
    ):
        raise ProdConfigError("Original image repository required")


def immutable_data(obj, *, keys=None):
    if (
        obj is None
        or obj.get("immutable") is not True
        or not isinstance(obj.get("data"), dict)
    ):
        raise ProdConfigError("Original immutable input is unavailable")
    if keys is not None and set(obj["data"]) != set(keys):
        raise ProdConfigError("Original immutable input fields differ")
    return obj["data"]


def load_receipt(kube, approval):
    cm = kube.get("ConfigMap", approval["configmap"])
    data = immutable_data(cm, keys={"config.json", "identity.json", "settings.json"})
    with tempfile.TemporaryDirectory(prefix="lightrag-resume-config-") as directory:
        path = Path(directory) / "config.json"
        path.write_text(data["config.json"])
        config = load_prod_config(path)
    identity = json.loads(data["identity.json"])
    settings = json.loads(data["settings.json"])
    if (
        config.config_hash != approval["onboarding_hash"]
        or config.target["namespace"] != approval["namespace"]
        or identity != approval["identity"]
        or content_hash(settings) != approval["settings_hash"]
    ):
        raise ProdConfigError("Original approved configuration differs")
    validate_cluster_identity(kube, config)
    job = kube.get("Job", approval["initial_job"])
    if (
        job is None
        or job["metadata"].get("uid") != approval["initial_job_uid"]
        or job["metadata"].get("deletionTimestamp")
        or content_hash(job["spec"]) != approval["initial_job_spec_hash"]
        or job.get("status", {}).get("succeeded") != 1
        or job["status"].get("failed")
        or job["status"].get("active")
    ):
        raise ProdConfigError("Original initialization Job has not proven success")
    template = job["spec"]["template"]["spec"]
    volumes = {v["name"]: v for v in template["volumes"]}
    if volumes["config"]["configMap"]["name"] != approval["configmap"]:
        raise ProdConfigError("Initialization configuration reference differs")
    secret = kube.get("Secret", volumes["runtime"]["secret"]["secretName"])
    encoded = immutable_data(secret, keys={"environment.json"})["environment.json"]
    runtime = json.loads(base64.b64decode(encoded, validate=True))
    validate_inputs(config, identity, runtime, settings)
    marker = kube.get("ConfigMap", approval["initial_job"] + "-execution")
    if immutable_data(marker) != dict(
        binding=content_hash({"identity": identity, "settings": settings}),
        job_name=approval["initial_job"],
    ):
        raise ProdConfigError("Original initialization execution claim differs")
    pods = json.loads(
        kube.run(
            "get", "pods", "-l", "job-name=" + approval["initial_job"], "-o", "json"
        )
    )["items"]
    if (
        len(pods) != 1
        or pods[0]["metadata"].get("uid") != approval["initial_pod_uid"]
        or pods[0]["metadata"].get("deletionTimestamp")
        or not any(
            r.get("kind") == "Job" and r.get("uid") == approval["initial_job_uid"]
            for r in pods[0]["metadata"].get("ownerReferences", [])
        )
    ):
        raise ProdConfigError("Original initialization Pod identity differs")
    pod = pods[0]
    if not managed_equal(pod["spec"], template):
        raise ProdConfigError("Original initialization Pod template differs")
    statuses = pod.get("status", {}).get("containerStatuses", [])
    if len(statuses) != 1:
        raise ProdConfigError("Original initialization termination result unavailable")
    status = statuses[0]
    terminated = status.get("state", {}).get("terminated", {})
    image = approval["image"] + "@" + identity["digest"]
    actual = status.get("imageID", "")
    if (
        template["containers"][0]["image"] != image
        or status.get("restartCount", 0) != 0
        or terminated.get("exitCode") != 0
        or (
            actual != "containerd://" + identity["digest"]
            and not actual.endswith("@" + identity["digest"])
        )
    ):
        raise ProdConfigError("Original initialization outcome or digest differs")
    raw = terminated.get("message", "")
    if len(raw.encode()) > 4000 or json.loads(raw) != dict(
        phase="INITIALIZED",
        scope=identity["scope"],
        release_id=identity["release_id"],
        config_hash=identity["config_hash"],
        plan_hash=settings["expected_plan_hash"],
        settings_hash=approval["settings_hash"],
        profile_hash=config.onboarding["profile_hash"],
        epoch=1,
    ):
        raise ProdConfigError("Original initialization receipt differs")
    return config, identity, runtime, settings


def immutable_matches(actual, expected):
    wanted = expected.get("data", {}) | {
        k: base64.b64encode(v.encode()).decode()
        for k, v in expected.get("stringData", {}).items()
    }
    return immutable_data(actual) == wanted


def execute_resume(kube, approval, *, confirm):
    validate_approval(approval, confirm)
    config, identity, runtime, settings = load_receipt(kube, approval)
    print(
        json.dumps(
            dict(
                event="application_resume",
                phase="RECEIPT_VERIFIED",
                source_pipeline=approval["source_pipeline"],
                tag=identity["tag"],
                digest=identity["digest"],
            ),
            sort_keys=True,
        ),
        flush=True,
    )
    image = approval["image"]
    slot = render_slot(config, identity, "blue", image=image, environment=runtime)
    if not immutable_matches(kube.get("Secret", slot[0]["metadata"]["name"]), slot[0]):
        raise ProdConfigError(
            "Existing application runtime differs; no credential rewrite"
        )
    sa = kube.get("ServiceAccount", config.onboarding["application_service_account"])
    if (
        sa is None
        or sa.get("automountServiceAccountToken") is not False
        or kube.get("Secret", config.onboarding["image_pull_secret"]) is None
    ):
        raise ProdConfigError("Existing application references are unavailable")
    if kube.get("Deployment", "lightrag-green") is not None:
        raise ProdConfigError("Existing Green prevents initial application resume")
    entry = render_entrypoint(config, "blue")
    for resource in [slot[1], slot[2], *entry]:
        old = kube.get(resource["kind"], resource["metadata"]["name"])
        if old is None:
            continue
        if resource["kind"] == "ConfigMap":
            if not immutable_matches(old, resource):
                raise ProdConfigError("Existing application configuration differs")
        elif not application_matches(old, resource):
            raise ProdConfigError(
                "Existing application resource differs; no replacement"
            )
    selector = ",".join(k + "=" + v for k, v in sorted(labels(config).items()))
    current = json.loads(kube.run("get", "pods", "-l", selector, "-o", "json"))["items"]
    pinned_uid = None

    def check_snapshot(deployment, pods, end=None):
        nonlocal pinned_uid

        def remaining_request():
            if end is None:
                return approval["request_timeout_seconds"]
            left = end - time.monotonic()
            if left <= 0.01:
                raise ProdConfigError("Application resume observation budget exhausted")
            return min(left, approval["request_timeout_seconds"])

        if deployment is None:
            if pods:
                raise ProdConfigError(
                    "Unowned existing application Pods prevent resume"
                )
            return
        if not application_matches(deployment, slot[2]):
            raise ProdConfigError("Observed application Deployment differs")
        uid = deployment["metadata"].get("uid")
        if not uid or (pinned_uid is not None and uid != pinned_uid):
            raise ProdConfigError("Application Deployment UID changed")
        pinned_uid = uid
        replicasets = {}
        for pod in pods:
            if pod_spec(pod["spec"], bound=True) != pod_spec(
                slot[2]["spec"]["template"]["spec"]
            ):
                raise ProdConfigError(
                    "Application Pod runtime differs from approved template"
                )
            owners = [
                r
                for r in pod["metadata"].get("ownerReferences", [])
                if r.get("controller") is True and r.get("kind") == "ReplicaSet"
            ]
            if len(owners) != 1:
                raise ProdConfigError(
                    "Application Pod controller is not the approved Blue"
                )
            owner = owners[0]
            if owner["name"] not in replicasets:
                replicasets[owner["name"]] = kube.get(
                    "ReplicaSet", owner["name"], timeout=remaining_request()
                )
            rs = replicasets[owner["name"]]
            if (
                rs is None
                or rs["metadata"].get("uid") != owner.get("uid")
                or not any(
                    r.get("controller") is True
                    and r.get("kind") == "Deployment"
                    and r.get("uid") == uid
                    for r in rs["metadata"].get("ownerReferences", [])
                )
            ):
                raise ProdConfigError("Application Pod controller UID chain differs")

    check_snapshot(kube.get("Deployment", "lightrag-blue"), current)
    deadline = time.monotonic() + settings["startup_wait_seconds"]
    request_budget = approval["request_timeout_seconds"]

    def remaining():
        value = deadline - time.monotonic()
        if value <= 0.01:
            raise ProdConfigError(
                "Application resume observation budget exhausted; resources retained"
            )
        return min(value, request_budget)

    print(
        json.dumps(
            dict(
                event="application_resume", phase="CREATE_MISSING_APPLICATION_RESOURCES"
            )
        ),
        flush=True,
    )
    kube.create_immutable(slot[1], timeout=remaining())
    create_exact(
        kube,
        slot[2],
        deadline=deadline,
        request_budget=request_budget,
        resource_check=application_matches,
    )
    pod_uids = wait_ready(
        kube,
        config,
        identity,
        settings,
        image=image,
        deadline=deadline,
        request_budget=request_budget,
        fail_fast=True,
        snapshot_check=check_snapshot,
    )
    for resource in entry:
        create_exact(
            kube,
            resource,
            deadline=deadline,
            request_budget=request_budget,
            resource_check=application_matches,
        )
    remaining()
    return dict(
        phase="KUBERNETES_READY",
        boundary="kubernetes",
        source_pipeline=approval["source_pipeline"],
        identity=identity,
        replicas=len(pod_uids),
        pod_uids=pod_uids,
        external_entry_verified=False,
        capacity_accepted=False,
    )


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--kubeconfig", required=True, type=Path)
    parser.add_argument("--approval", required=True, type=Path)
    args = parser.parse_args(argv)
    try:
        if (
            os.environ.get("CI_PIPELINE_EVENT") != "manual"
            or os.environ.get("CI_COMMIT_BRANCH") != "master"
            or os.environ.get("CI_REPO") != "LFunTech/LightRAG"
            or not re.fullmatch(r"[a-f0-9]{40}", os.environ.get("CI_COMMIT_SHA", ""))
        ):
            raise ProdConfigError(
                "Application resume requires the approved manual master pipeline"
            )
        approval = json.loads(args.approval.read_text())
        confirm = os.environ.get("LIGHTRAG_PROD_APP_RESUME_CONFIRM", "")
        validate_approval(approval, confirm)
        result = execute_resume(
            Kubectl(args.kubeconfig, approval["namespace"]), approval, confirm=confirm
        )
        result["executor_commit"] = os.environ["CI_COMMIT_SHA"]
        print(json.dumps(result, sort_keys=True))
        return 0
    except Exception as error:
        detail = (
            str(error) if isinstance(error, ProdConfigError) else type(error).__name__
        )
        print(
            detail + ": application resume stopped; resources retained without retry",
            file=sys.stderr,
        )
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
