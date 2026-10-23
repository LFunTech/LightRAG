"""First Kubernetes-only rollout, reusing the existing immutable artifact chain."""

from dataclasses import asdict
import json
import re
import time

from lightrag_delivery.config import ProdConfigError
from lightrag_delivery.render import (
    content_hash,
    document,
    labels,
    render_slot,
    render_entrypoint,
    render_foundation,
)
from lightrag.provisioning.initial import validate_inputs, validate_operator
from scripts.ci.prod_deploy import verified_artifact_inputs


def verified_initial_inputs(
    config, *, settings, image_record, source_archive, source_record, environment
):
    identity, runtime, image = verified_artifact_inputs(
        config,
        image_record=image_record,
        source_archive=source_archive,
        source_record=source_record,
        environment=environment,
    )
    validate_inputs(config, identity, runtime, settings)
    return identity, runtime, image


def render_initial_job(
    config, identity, runtime, settings, operator, *, image, attempt
):
    validate_inputs(config, identity, runtime, settings)
    validate_operator(
        runtime, operator, confirm_shared=settings["confirm_shared_credentials"]
    )
    if not isinstance(attempt, str) or not re.fullmatch(r"[0-9]{1,12}", attempt):
        raise ProdConfigError("Explicit initial pipeline attempt required")
    ns = config.target["namespace"]
    name = (
        "lightrag-initial-"
        + content_hash({"identity": identity, "settings": settings})[:20]
    )
    runtime_name, config_name = name + "-runtime", name + "-config"
    op_name = config.onboarding["operator_secret"]
    scope_name = "lightrag-initial-" + content_hash({"scope": identity["scope"]})[:20]
    resources = [
        document(
            "ConfigMap",
            config_name,
            ns,
            immutable=True,
            data={
                "config.json": json.dumps(asdict(config), sort_keys=True),
                "identity.json": json.dumps(identity, sort_keys=True),
                "settings.json": json.dumps(settings, sort_keys=True),
            },
        ),
        document(
            "Secret",
            runtime_name,
            ns,
            immutable=True,
            type="Opaque",
            stringData={"environment.json": json.dumps(runtime, sort_keys=True)},
        ),
        document(
            "Secret", op_name, ns, immutable=True, type="Opaque", stringData=operator
        ),
    ]
    job = document(
        "Job",
        scope_name,
        ns,
        spec={
            "backoffLimit": 0,
            "template": {
                "metadata": {"labels": {"lightrag.io/component": "initialization"}},
                "spec": {
                    "restartPolicy": "Never",
                    "automountServiceAccountToken": False,
                    "serviceAccountName": config.onboarding[
                        "application_service_account"
                    ],
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
                            "name": "initialization",
                            "image": image + "@" + identity["digest"],
                            "command": [
                                "python",
                                "-m",
                                "lightrag.provisioning.initial",
                            ],
                            "args": [
                                "--config",
                                "/run/release/config.json",
                                "--identity",
                                "/run/release/identity.json",
                                "--settings",
                                "/run/release/settings.json",
                                "--environment",
                                "/run/runtime/environment.json",
                            ],
                            "envFrom": [{"secretRef": {"name": op_name}}],
                            "env": [
                                {"name": "PYTHON_DOTENV_DISABLED", "value": "1"},
                                {"name": "PYTHONDONTWRITEBYTECODE", "value": "1"},
                                {"name": "LOG_DIR", "value": "/tmp"},
                            ],
                            "resources": {
                                "requests": {"cpu": "100m", "memory": "256Mi"},
                                "limits": {"cpu": "1", "memory": "1Gi"},
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
                        {"name": "runtime", "secret": {"secretName": runtime_name}},
                        {"name": "tmp", "emptyDir": {}},
                    ],
                },
            },
        },
    )
    marker = document(
        "ConfigMap",
        scope_name + "-execution",
        ns,
        immutable=True,
        data={
            "binding": content_hash({"identity": identity, "settings": settings}),
            "job_name": scope_name,
        },
    )
    return [marker, *resources, job]


def validate_initial_target(kube, config):
    from scripts.ci.prod_deploy import validate_cluster_identity

    validate_cluster_identity(kube, config)
    permissions = [
        ("create", "jobs.batch"),
        ("get", "jobs.batch"),
        ("list", "pods"),
        ("get", "secrets"),
        ("create", "secrets"),
        ("get", "configmaps"),
        ("create", "configmaps"),
        ("get", "deployments.apps"),
        ("create", "deployments.apps"),
        ("get", "services"),
        ("create", "services"),
        ("get", "ingresses.networking.k8s.io"),
        ("create", "ingresses.networking.k8s.io"),
        ("get", "serviceaccounts"),
        ("create", "serviceaccounts"),
    ]
    if config.network != {"ingress_cidrs": [], "egress": []}:
        permissions += [
            ("get", "networkpolicies.networking.k8s.io"),
            ("create", "networkpolicies.networking.k8s.io"),
        ]
    if config.target["storage_class"] is not None:
        permissions += [
            ("get", "persistentvolumeclaims"),
            ("create", "persistentvolumeclaims"),
        ]
    for verb, resource in permissions:
        if kube.run("auth", "can-i", verb, resource).strip() != "yes":
            raise ProdConfigError("Initial Kubernetes delivery permission is missing")
    if kube.get("secret", config.onboarding["image_pull_secret"]) is None:
        raise ProdConfigError("Existing production image pull reference is missing")


def managed_equal(actual, expected):
    if isinstance(expected, dict):
        return isinstance(actual, dict) and all(
            k in actual and managed_equal(actual[k], v) for k, v in expected.items()
        )
    if isinstance(expected, list):
        return (
            isinstance(actual, list)
            and len(actual) == len(expected)
            and all(managed_equal(a, b) for a, b in zip(actual, expected))
        )
    return actual == expected


def remaining_timeout(deadline, request_budget=15):
    remaining = deadline - time.monotonic()
    if remaining <= 0.01:
        raise ProdConfigError(
            "Initial observation budget exhausted; remote resources retained"
        )
    return min(request_budget, remaining)


def claim_execution(kube, marker, job):
    old = kube.get("ConfigMap", marker["metadata"]["name"])
    if old is None:
        kube.run("create", "-f", "-", body=marker)
    elif old.get("immutable") is not True or old.get("data") != marker["data"]:
        raise ProdConfigError(
            "Initial scope already belongs to another approved execution"
        )
    elif kube.get("Job", job["metadata"]["name"]) is None:
        raise ProdConfigError(
            "Existing initial execution has a missing Job; audit required, no replay"
        )


def create_exact(
    kube, resource, *, deadline=None, request_budget=15, resource_check=None
):
    timeout = (
        remaining_timeout(deadline, request_budget) if deadline is not None else 30
    )
    old = kube.get(resource["kind"], resource["metadata"]["name"], timeout=timeout)
    if old is None:
        kube.run(
            "create",
            "-f",
            "-",
            body=resource,
            timeout=remaining_timeout(deadline, request_budget)
            if deadline is not None
            else 30,
        )
    elif resource_check is not None:
        if not resource_check(old, resource):
            raise ProdConfigError(
                "Existing application resource differs; no replacement"
            )
    elif (
        not managed_equal(old.get("spec", {}), resource.get("spec", {}))
        or not managed_equal(
            old.get("metadata", {}).get("annotations", {}),
            resource.get("metadata", {}).get("annotations", {}),
        )
        or (resource["kind"] == "Ingress" and old.get("spec", {}).get("tls"))
        or (
            resource["kind"] == "ServiceAccount"
            and old.get("automountServiceAccountToken") is not False
        )
    ):
        raise ProdConfigError(
            "Existing initial resource differs; no replacement allowed"
        )


def wait_initialized(kube, job, identity, settings, config, *, deadline=None):
    end = (
        deadline
        if deadline is not None
        else time.monotonic() + settings["job_wait_seconds"]
    )
    name = job["metadata"]["name"]
    while True:
        state = kube.get("Job", name, timeout=remaining_timeout(end))
        remaining_timeout(end)
        if state is None or state.get("status", {}).get("failed"):
            raise ProdConfigError(
                "Initial Job failed; retained without retry or recovery"
            )
        if not managed_equal(state["spec"]["template"], job["spec"]["template"]):
            raise ProdConfigError("Initial Job identity differs")
        if state.get("status", {}).get("succeeded"):
            pods = json.loads(
                kube.run(
                    "get",
                    "pods",
                    "-l",
                    "job-name=" + name,
                    "-o",
                    "json",
                    timeout=remaining_timeout(end),
                )
            )["items"]
            remaining_timeout(end)
            if len(pods) != 1 or not any(
                r.get("kind") == "Job" and r.get("uid") == state["metadata"]["uid"]
                for r in pods[0]["metadata"].get("ownerReferences", [])
            ):
                raise ProdConfigError("Initial Job result is ambiguous")
            containers = pods[0].get("status", {}).get("containerStatuses", [])
            if len(containers) != 1:
                raise ProdConfigError("Initial Job result is unavailable")
            terminated = containers[0].get("state", {}).get("terminated", {})
            raw = terminated.get("message", "")
            if terminated.get("exitCode") != 0 or len(raw.encode()) > 4000:
                raise ProdConfigError("Initial Job did not succeed")
            result = json.loads(raw)
            expected = dict(
                phase="INITIALIZED",
                scope=identity["scope"],
                release_id=identity["release_id"],
                config_hash=identity["config_hash"],
                plan_hash=settings["expected_plan_hash"],
                settings_hash=content_hash(settings),
                profile_hash=config.onboarding["profile_hash"],
                epoch=1,
            )
            if result != expected:
                raise ProdConfigError("Initial result does not match this release")
            remaining_timeout(end)
            return result
        if time.monotonic() >= end:
            raise ProdConfigError(
                "Initial Job observation timed out; Job and unknown outcomes retained"
            )
        time.sleep(2)


def wait_ready(
    kube,
    config,
    identity,
    settings,
    *,
    image,
    deadline=None,
    request_budget=15,
    fail_fast=False,
    snapshot_check=None,
):
    end = (
        deadline
        if deadline is not None
        else time.monotonic() + settings["startup_wait_seconds"]
    )
    selector = ",".join(k + "=" + v for k, v in sorted(labels(config, "blue").items()))
    expected_image = image + "@" + identity["digest"]
    while True:
        deployment = kube.get(
            "Deployment",
            "lightrag-blue",
            timeout=remaining_timeout(end, request_budget),
        )
        remaining_timeout(end, request_budget)
        if deployment is None:
            raise ProdConfigError("Initial deployment disappeared")
        pods = json.loads(
            kube.run(
                "get",
                "pods",
                "-l",
                selector,
                "-o",
                "json",
                timeout=remaining_timeout(end, request_budget),
            )
        )["items"]
        remaining_timeout(end, request_budget)
        if snapshot_check is not None:
            snapshot_check(deployment, pods, end)
            remaining_timeout(end, request_budget)
        if fail_fast:
            for pod in pods:
                statuses = pod.get("status", {}).get("containerStatuses", []) + pod.get(
                    "status", {}
                ).get("initContainerStatuses", [])
                if pod.get("status", {}).get("phase") == "Failed" or any(
                    item.get("restartCount", 0) > 0
                    or item.get("state", {}).get("waiting", {}).get("reason")
                    in {
                        "CrashLoopBackOff",
                        "ErrImagePull",
                        "ImagePullBackOff",
                        "CreateContainerConfigError",
                        "CreateContainerError",
                        "RunContainerError",
                        "InvalidImageName",
                    }
                    or item.get("state", {}).get("terminated", {}).get("exitCode", 0)
                    != 0
                    for item in statuses
                ):
                    raise ProdConfigError(
                        "Application Pod failed; resume stopped without restart"
                    )
        ready = True
        for pod in pods:
            containers = pod["spec"].get("containers", [])
            statuses = pod.get("status", {}).get("containerStatuses", [])
            if len(containers) != 1 or containers[0].get("image") != expected_image:
                raise ProdConfigError(
                    "Initial Pod image differs from the pinned release"
                )
            if (
                pod["metadata"].get("deletionTimestamp")
                or len(statuses) != 1
                or statuses[0].get("ready") is not True
            ):
                ready = False
                continue
            actual = statuses[0].get("imageID", "")
            if actual != "containerd://" + identity["digest"] and not actual.endswith(
                "@" + identity["digest"]
            ):
                raise ProdConfigError("Initial running image digest differs")
            ready = ready and any(
                c.get("type") == "Ready" and c.get("status") == "True"
                for c in pod.get("status", {}).get("conditions", [])
            )
        state = deployment.get("status", {})
        if (
            ready
            and len(pods) == config.replicas
            and state.get("observedGeneration") == deployment["metadata"]["generation"]
            and state.get("updatedReplicas") == config.replicas
            and state.get("availableReplicas") == config.replicas
        ):
            remaining_timeout(end, request_budget)
            return [p["metadata"]["uid"] for p in pods]
        if time.monotonic() >= end:
            raise ProdConfigError(
                "Initial Pod readiness timed out; instances retained without restart"
            )
        time.sleep(2)


def execute_initial(kube, config, identity, runtime, settings, resources, *, image):
    validate_inputs(config, identity, runtime, settings)
    validate_initial_target(kube, config)
    for kind, name in [
        ("Deployment", "lightrag-blue"),
        ("Deployment", "lightrag-green"),
        ("Service", "lightrag"),
        ("Ingress", "lightrag"),
    ]:
        if kube.get(kind, name) is not None:
            raise ProdConfigError(
                "Existing application requires separate review; initial deployment never replaces it"
            )
    selector = ",".join(k + "=" + v for k, v in sorted(labels(config).items()))
    if json.loads(kube.run("get", "pods", "-l", selector, "-o", "json"))["items"]:
        raise ProdConfigError("Existing application Pods prevent initial deployment")
    claim_execution(kube, resources[0], resources[-1])
    for resource in render_foundation(config):
        if resource["kind"] in {"NetworkPolicy", "PersistentVolumeClaim"} or (
            resource["kind"] == "ServiceAccount"
            and resource["metadata"]["name"]
            == config.onboarding["application_service_account"]
        ):
            create_exact(kube, resource)
    for resource in resources[1:-1]:
        kube.create_immutable(resource)
    create_exact(kube, resources[-1])
    job_deadline = time.monotonic() + settings["job_wait_seconds"]
    wait_initialized(
        kube, resources[-1], identity, settings, config, deadline=job_deadline
    )
    remaining_timeout(job_deadline)
    startup_deadline = time.monotonic() + settings["startup_wait_seconds"]
    slot = render_slot(config, identity, "blue", image=image, environment=runtime)
    for resource in slot[:-1]:
        kube.create_immutable(resource, timeout=remaining_timeout(startup_deadline))
    create_exact(kube, slot[-1], deadline=startup_deadline)
    pods = wait_ready(
        kube, config, identity, settings, image=image, deadline=startup_deadline
    )
    for resource in render_entrypoint(config, "blue"):
        create_exact(kube, resource, deadline=startup_deadline)
    remaining_timeout(startup_deadline)
    return dict(
        phase="KUBERNETES_READY",
        boundary="kubernetes",
        identity=identity,
        replicas=len(pods),
        pod_uids=pods,
        external_entry_verified=False,
        capacity_accepted=False,
    )
