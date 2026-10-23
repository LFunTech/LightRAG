"""Thin production runner: verify artifact/target, then run the pinned control Job."""

import argparse
import base64
from dataclasses import asdict
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import time

from lightrag_delivery.config import ProdConfigError, load_prod_config
from lightrag_delivery.environment import SECRET_ENV, runtime_environment
from lightrag_delivery.render import content_hash, document, select_prod_release
from scripts.ci.delivery import ReleaseIdentity, validate_source_archive_record


class Kubectl:
    def __init__(self, kubeconfig, namespace):
        self.prefix = [
            "kubectl",
            "--kubeconfig",
            str(kubeconfig),
            "--namespace",
            namespace,
        ]

    def run(self, *args, body=None):
        result = subprocess.run(
            [*self.prefix, *args],
            input=json.dumps(body) if body is not None else None,
            text=True,
            capture_output=True,
            check=False,
        )
        if result.returncode:
            raise ProdConfigError(
                "Kubernetes operation failed; protected diagnostics required"
            )
        return result.stdout

    def get(self, kind, name):
        output = self.run("get", kind, name, "--ignore-not-found", "-o", "json")
        return json.loads(output) if output.strip() else None

    def create_immutable(self, resource):
        old = self.get(resource["kind"], resource["metadata"]["name"])
        if old is None:
            self.run("create", "-f", "-", body=resource)
            return
        wanted = resource.get("data", {})
        if resource["kind"] == "Secret":
            wanted = wanted | {
                k: base64.b64encode(v.encode()).decode()
                for k, v in resource.get("stringData", {}).items()
            }
        if old.get("data", {}) != wanted or not old.get("immutable"):
            raise ProdConfigError("Existing immutable release input differs")


def verified_inputs(
    config, *, image_record, source_archive, source_record, environment
):
    event, tag, commit, pipeline, repo = (
        environment.get(k, "")
        for k in (
            "CI_PIPELINE_EVENT",
            "CI_COMMIT_TAG",
            "CI_COMMIT_SHA",
            "CI_PIPELINE_NUMBER",
            "CI_REPO",
        )
    )
    if not select_prod_release(event=event, tag=tag, activated=config.enabled):
        raise ProdConfigError("Prod is disabled or this is not a formal tag event")
    if (
        repo != config.repo
        or not re.fullmatch("[a-f0-9]{40}", commit)
        or not pipeline.isdigit()
    ):
        raise ProdConfigError("CI source identity differs")
    validate_source_archive_record(
        source_archive,
        source_record,
        expected=ReleaseIdentity(repo, tag, commit, pipeline, None),
    )
    image = image_record.get("image", "")
    digest = image_record.get("digest", "")
    if (
        image_record.get("revision") != commit
        or image_record.get("tag") != tag
        or image_record.get("platform") != "linux/amd64"
        or not re.fullmatch("sha256:[a-f0-9]{64}", digest)
        or image_record.get("image_ref") != image + "@" + digest
        or not re.fullmatch(r"[A-Za-z0-9.-]+(?::[0-9]+)?/[A-Za-z0-9_./-]+", image)
    ):
        raise ProdConfigError("Resolved image provenance differs")
    secrets = {
        key: environment.get("LIGHTRAG_PROD_" + key.upper(), "") for key in SECRET_ENV
    }
    runtime, config_hash = runtime_environment(config, secrets)
    identity = dict(
        scope=config.target["deployment_id"] + "/" + config.target["workspace"],
        release_id="prod-" + tag + "-" + commit[:12],
        tag=tag,
        commit=commit,
        digest=digest,
        config_hash=config_hash,
        protocol_version=1,
    )
    return identity, runtime, image


def render_control_job(config, identity, environment, *, image, attempt):
    ns = config.target["namespace"]
    version = content_hash(identity)[:24]
    config_name = "lightrag-release-" + version
    input_name = config_name + "-runtime"
    configmap = document(
        "ConfigMap",
        config_name,
        ns,
        immutable=True,
        data={
            "config.json": json.dumps(asdict(config), sort_keys=True),
            "identity.json": json.dumps(identity, sort_keys=True),
        },
    )
    secret = document(
        "Secret",
        input_name,
        ns,
        immutable=True,
        type="Opaque",
        stringData={"environment.json": json.dumps(environment, sort_keys=True)},
    )
    job = document(
        "Job",
        config_name + "-" + attempt,
        ns,
        spec={
            "backoffLimit": 0,
            "template": {
                "metadata": {"labels": {"lightrag.io/control": "true"}},
                "spec": {
                    "restartPolicy": "Never",
                    "serviceAccountName": config.onboarding["control_service_account"],
                    "imagePullSecrets": [
                        {"name": config.onboarding["image_pull_secret"]}
                    ],
                    "securityContext": {
                        "runAsNonRoot": True,
                        "runAsUser": 1000,
                        "runAsGroup": 1000,
                        "fsGroup": 1000,
                    },
                    "containers": [
                        {
                            "name": "control",
                            "image": image + "@" + identity["digest"],
                            "command": [
                                "python",
                                "-m",
                                "lightrag.distributed.release",
                                "deliver",
                                "--config",
                                "/run/release/config.json",
                                "--identity",
                                "/run/release/identity.json",
                                "--environment",
                                "/run/runtime/environment.json",
                                "--image",
                                image,
                            ],
                            "envFrom": [
                                {
                                    "secretRef": {
                                        "name": config.onboarding["operator_secret"]
                                    }
                                }
                            ],
                            "env": [{"name": "PYTHONDONTWRITEBYTECODE", "value": "1"}],
                            "resources": {
                                "requests": {"cpu": "100m", "memory": "128Mi"},
                                "limits": {"cpu": "1", "memory": "512Mi"},
                            },
                            "securityContext": {
                                "allowPrivilegeEscalation": False,
                                "readOnlyRootFilesystem": True,
                                "capabilities": {"drop": ["ALL"]},
                            },
                            "volumeMounts": [
                                {
                                    "name": "config",
                                    "mountPath": "/run/release",
                                    "readOnly": True,
                                },
                                {
                                    "name": "runtime",
                                    "mountPath": "/run/runtime",
                                    "readOnly": True,
                                },
                                {"name": "tmp", "mountPath": "/tmp"},
                            ],
                            "terminationMessagePolicy": "File",
                        }
                    ],
                    "volumes": [
                        {"name": "config", "configMap": {"name": config_name}},
                        {"name": "runtime", "secret": {"secretName": input_name}},
                        {"name": "tmp", "emptyDir": {}},
                    ],
                },
            },
        },
    )
    return configmap, secret, job


def validate_target(kube, config):
    raw = json.loads(kube.run("config", "view", "--minify", "-o", "json"))
    clusters = raw.get("clusters", [])
    if len(clusters) != 1:
        raise ProdConfigError("Explicit single Kubernetes context required")
    cluster = clusters[0]["cluster"]
    if (
        cluster.get("insecure-skip-tls-verify")
        or hashlib.sha256(cluster.get("server", "").encode()).hexdigest()
        != config.onboarding["server_sha256"]
    ):
        raise ProdConfigError("Kubernetes endpoint or TLS policy differs")
    for ns, key in [
        (config.target["namespace"], "namespace_uid"),
        ("kube-system", "kube_system_uid"),
    ]:
        obj = kube.get("namespace", ns)
        if obj is None or obj["metadata"]["uid"] != config.onboarding[key]:
            raise ProdConfigError("Cluster/namespace UID differs")
    for verb, resource in [
        ("create", "jobs.batch"),
        ("get", "pods"),
        ("list", "pods"),
        ("get", "jobs.batch"),
        ("get", "secrets"),
        ("create", "secrets"),
        ("get", "configmaps"),
        ("create", "configmaps"),
    ]:
        if kube.run("auth", "can-i", verb, resource).strip() != "yes":
            raise ProdConfigError("Required production delivery permission is missing")
    for kind, name in [
        ("serviceaccount", config.onboarding["control_service_account"]),
        ("secret", config.onboarding["operator_secret"]),
        ("secret", config.onboarding["image_pull_secret"]),
    ]:
        if kube.get(kind, name) is None:
            raise ProdConfigError("Production enrollment reference is missing")


def execute(kube, config, resources):
    validate_target(kube, config)
    for resource in resources[:-1]:
        kube.create_immutable(resource)
    job = resources[-1]
    name = job["metadata"]["name"]
    current = kube.get("job", name)
    if current is None:
        kube.run("create", "-f", "-", body=job)
    elif (
        current["spec"]["template"]["spec"]["containers"][0]["image"]
        != job["spec"]["template"]["spec"]["containers"][0]["image"]
    ):
        raise ProdConfigError("Control Job identity differs")
    end = (
        time.monotonic()
        + 3 * config.budget["execution_seconds"]
        + config.budget["request_wait_seconds"]
    )
    while True:
        state = kube.get("job", name)
        if state is None or state.get("status", {}).get("failed"):
            raise ProdConfigError(
                "Control Job failed; owner and unknown writes are retained"
            )
        if state.get("status", {}).get("succeeded"):
            pods = json.loads(
                kube.run("get", "pods", "-l", "job-name=" + name, "-o", "json")
            )["items"]
            if len(pods) != 1:
                raise ProdConfigError("Control Job result identity is ambiguous")
            containers = pods[0]["status"].get("containerStatuses", [])
            if len(containers) != 1:
                raise ProdConfigError("Control Job result is unavailable")
            result = json.loads(containers[0]["state"]["terminated"]["message"])
            if result.get("phase") != "COMPLETED":
                raise ProdConfigError("Control Job did not complete the release")
            return result
        if time.monotonic() >= end:
            raise ProdConfigError(
                "Control Job observation budget exceeded; Job is not killed or rolled back"
            )
        time.sleep(2)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", required=True, type=Path)
    parser.add_argument("--image-record", required=True, type=Path)
    parser.add_argument("--source-archive", required=True, type=Path)
    parser.add_argument("--source-record", required=True, type=Path)
    parser.add_argument("--kubeconfig", type=Path)
    parser.add_argument("--execute", action="store_true")
    args = parser.parse_args(argv)
    try:
        config = load_prod_config(args.config)
        identity, runtime, image = verified_inputs(
            config,
            image_record=json.loads(args.image_record.read_text()),
            source_archive=args.source_archive,
            source_record=json.loads(args.source_record.read_text()),
            environment=os.environ,
        )
        resources = render_control_job(
            config,
            identity,
            runtime,
            image=image,
            attempt=os.environ["CI_PIPELINE_NUMBER"],
        )
        if args.execute:
            if not args.kubeconfig:
                raise ProdConfigError("Explicit prod kubeconfig required")
            result = execute(
                Kubectl(args.kubeconfig, config.target["namespace"]), config, resources
            )
        else:
            result = {
                "planned": True,
                "identity": identity,
                "resources": [r["kind"] for r in resources],
            }
        print(json.dumps(result, sort_keys=True))
        return 0
    except Exception as error:
        print(
            type(error).__name__
            + ": production delivery stopped; no automatic recovery or rollback",
            file=sys.stderr,
        )
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
