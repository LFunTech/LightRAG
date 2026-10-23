"""Pure renderers: no cluster access, environment fallback or mutable Blue inputs."""

import hashlib
import json
import re

SLOT_LABEL = "lightrag.io/slot"
APP_LABEL = "lightrag.io/deployment"


def select_prod_release(*, event, tag, activated):
    return (
        activated is True
        and event == "tag"
        and bool(re.fullmatch(r"v[0-9]+\.[0-9]+\.[0-9]+", tag))
    )


def document(kind, name, namespace, **body):
    version = {
        "Deployment": "apps/v1",
        "NetworkPolicy": "networking.k8s.io/v1",
        "Ingress": "networking.k8s.io/v1",
        "Role": "rbac.authorization.k8s.io/v1",
        "RoleBinding": "rbac.authorization.k8s.io/v1",
        "ClusterRole": "rbac.authorization.k8s.io/v1",
        "ClusterRoleBinding": "rbac.authorization.k8s.io/v1",
        "Job": "batch/v1",
    }.get(kind, "v1")
    result = dict(
        apiVersion=version,
        kind=kind,
        metadata=dict(name=name, namespace=namespace),
        **body,
    )
    if kind in {"ClusterRole", "ClusterRoleBinding"}:
        result["metadata"].pop("namespace")
    return result


def labels(config, slot=None):
    value = {APP_LABEL: config.target["deployment_id"]}
    if slot:
        value[SLOT_LABEL] = slot
    return value


def render_slot(config, identity, slot, *, image, environment):
    if environment.get("SSL", "false") != "false" or any(
        environment.get(field) for field in ("SSL_CERTFILE", "SSL_KEYFILE")
    ):
        raise ValueError("Application TLS is forbidden; terminate TLS in the cloud")
    if (
        slot not in {"blue", "green"}
        or not re.fullmatch(r"[a-f0-9]{64}", identity["config_hash"])
        or not re.fullmatch(r"sha256:[a-f0-9]{64}", identity["digest"])
    ):
        raise ValueError("Invalid immutable slot identity")
    ns, name = config.target["namespace"], "lightrag-" + slot
    version = identity["config_hash"][:24]
    secret_name, config_name = (
        name + "-" + version,
        name + "-config-" + content_hash(identity)[:24],
    )
    secret = document(
        "Secret",
        secret_name,
        ns,
        immutable=True,
        type="Opaque",
        stringData=dict(environment),
    )
    immutable = document(
        "ConfigMap",
        config_name,
        ns,
        immutable=True,
        data={
            "identity.json": json.dumps(identity, sort_keys=True),
        },
    )
    resources = config.resources
    container = dict(
        name="lightrag",
        image=image + "@" + identity["digest"],
        imagePullPolicy="IfNotPresent",
        command=["lightrag-server"],
        envFrom=[{"secretRef": {"name": secret_name}}],
        env=[
            {"name": key, "value": value}
            for key, value in {
                "HOST": "0.0.0.0",
                "PORT": "9621",
                "WORKERS": "1",
                "SSL": "false",
                "LIGHTRAG_RELEASE_ENABLED": "true",
                "LIGHTRAG_RELEASE_SLOT": slot,
                "LIGHTRAG_RELEASE_CONFIG_HASH": identity["config_hash"],
                "WORKING_DIR": "/app/data/rag_storage",
                "INPUT_DIR": "/app/data/inputs",
            }.items()
        ]
        + [
            {
                "name": "LIGHTRAG_RELEASE_INSTANCE_ID",
                "valueFrom": {"fieldRef": {"fieldPath": "metadata.uid"}},
            }
        ],
        ports=[{"name": "http", "containerPort": 9621}],
        readinessProbe={
            "httpGet": {"path": "/ready", "port": "http"},
            "periodSeconds": 3,
            "timeoutSeconds": 2,
            "failureThreshold": 1,
        },
        startupProbe={
            "httpGet": {"path": "/ready", "port": "http"},
            "periodSeconds": 5,
            "failureThreshold": 120,
        },
        # Liveness is intentionally absent: a quarantined writer must retain
        # evidence, not be automatically restarted as a pretend recovery.
        resources={
            "requests": {
                "cpu": resources["cpu_request"],
                "memory": resources["memory_request"],
            },
            "limits": {
                "cpu": resources["cpu_limit"],
                "memory": resources["memory_limit"],
            },
        },
        securityContext={
            "allowPrivilegeEscalation": False,
            "readOnlyRootFilesystem": True,
            "capabilities": {"drop": ["ALL"]},
        },
        volumeMounts=[
            {"name": "working", "mountPath": "/app/data"},
            {"name": "tmp", "mountPath": "/tmp"},
            {"name": "release", "mountPath": "/app/release", "readOnly": True},
        ],
    )
    if environment.get("LIGHTRAG_QUERY_METRICS") == "true":
        container["env"].append(
            dict(name="LIGHTRAG_USAGE_FINAL_PATH", value="/dev/termination-log")
        )
        container["terminationMessagePath"] = "/dev/termination-log"
        container["terminationMessagePolicy"] = "File"
    deployment = document(
        "Deployment",
        name,
        ns,
        spec={
            "replicas": config.replicas,
            "selector": {"matchLabels": labels(config, slot)},
            "strategy": {"type": "Recreate"},
            "template": {
                "metadata": {"labels": labels(config, slot)},
                "spec": {
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
                    "terminationGracePeriodSeconds": int(
                        config.budget["execution_seconds"]
                        + config.budget["request_wait_seconds"]
                        + 60
                    ),
                    "topologySpreadConstraints": [
                        {
                            "maxSkew": 1,
                            "topologyKey": "kubernetes.io/hostname",
                            "whenUnsatisfiable": "DoNotSchedule",
                            "labelSelector": {"matchLabels": labels(config, slot)},
                        }
                    ],
                    "initContainers": [
                        {
                            "name": "prepare-working-directories",
                            "image": container["image"],
                            "command": [
                                "python",
                                "-I",
                                "-c",
                                "from pathlib import Path; import sys; "
                                "[Path(p).mkdir(parents=True, exist_ok=True) for p in sys.argv[1:]]",
                                "/app/data/logs",
                                "/app/data/rag_storage",
                                "/app/data/inputs",
                                "/app/data/object-scratch",
                            ],
                            "resources": {
                                "requests": {"cpu": "10m", "memory": "32Mi"},
                                "limits": {"cpu": "100m", "memory": "128Mi"},
                            },
                            "securityContext": dict(container["securityContext"]),
                            "volumeMounts": [
                                {"name": "working", "mountPath": "/app/data"}
                            ],
                        }
                    ],
                    "containers": [container],
                    "volumes": [
                        {
                            "name": "working",
                            "persistentVolumeClaim": {"claimName": "lightrag-working"},
                        },
                        {"name": "tmp", "emptyDir": {}},
                        {"name": "release", "configMap": {"name": config_name}},
                    ],
                },
            },
        },
    )
    return [secret, immutable, deployment]


def render_entrypoint(config, slot):
    ns = config.target["namespace"]
    svc = document(
        "Service",
        "lightrag",
        ns,
        spec={
            "selector": labels(config, slot),
            "ports": [{"name": "http", "port": 9621, "targetPort": 9621}],
        },
    )
    ingress = document(
        "Ingress",
        "lightrag",
        ns,
        spec={
            "ingressClassName": "qcloud",
            "rules": [
                {
                    "host": config.target["host"],
                    "http": {
                        "paths": [
                            {
                                "path": "/",
                                "pathType": "Prefix",
                                "backend": {
                                    "service": {
                                        "name": "lightrag",
                                        "port": {"number": 9621},
                                    }
                                },
                            }
                        ]
                    },
                }
            ],
        },
    )
    ingress["metadata"]["annotations"] = {
        "kubernetes.io/ingress.existLbId": config.ingress["load_balancer_id"],
        "ingress.cloud.tencent.com/direct-access": "true",
        "ingress.cloud.tencent.com/enable-grace-shutdown": "true",
        "ingress.cloud.tencent.com/enable-grace-deletion": "true",
        "ingress.cloud.tencent.com/https-rules": json.dumps(
            [
                {
                    "host": config.target["host"],
                    "path": "/",
                    "backend": {"serviceName": "lightrag", "servicePort": 9621},
                }
            ]
        ),
        "ingress.cloud.tencent.com/certificate": json.dumps(
            [
                {
                    "hosts": [config.target["host"]],
                    "qcloud_cert_id": [config.ingress["certificate_id"]],
                }
            ]
        ),
    }
    if config.ingress["mode"] == "edgeone":
        annotations = ingress["metadata"]["annotations"]
        annotations["kubernetes.io/ingress.http-rules"] = annotations.pop(
            "ingress.cloud.tencent.com/https-rules"
        )
        del annotations["ingress.cloud.tencent.com/certificate"]
    return [svc, ingress]


def render_foundation(config):
    ns = config.target["namespace"]
    app = config.onboarding["application_service_account"]
    control = config.onboarding["control_service_account"]
    account = document("ServiceAccount", app, ns, automountServiceAccountToken=False)
    control_sa = document("ServiceAccount", control, ns)
    pvc = document(
        "PersistentVolumeClaim",
        "lightrag-working",
        ns,
        spec={
            "accessModes": ["ReadWriteMany"],
            "storageClassName": config.target["storage_class"],
            "resources": {"requests": {"storage": config.resources["working_storage"]}},
        },
    )
    policy = document(
        "NetworkPolicy",
        "lightrag",
        ns,
        spec={
            "podSelector": {"matchLabels": labels(config)},
            "policyTypes": ["Ingress", "Egress"],
            "ingress": [
                {
                    "from": [
                        {"ipBlock": {"cidr": v}}
                        for v in config.network["ingress_cidrs"]
                    ]
                    + [
                        {
                            "podSelector": {
                                "matchLabels": {"lightrag.io/control": "true"}
                            }
                        }
                    ],
                    "ports": [{"protocol": "TCP", "port": 9621}],
                }
            ],
            "egress": [
                {"to": [{"ipBlock": {"cidr": v["cidr"]}}], "ports": v["ports"]}
                for v in config.network["egress"]
            ],
        },
    )
    role = document(
        "Role",
        "lightrag-release",
        ns,
        rules=[
            {
                "apiGroups": [""],
                "resources": [
                    "pods",
                    "services",
                    "configmaps",
                    "secrets",
                    "persistentvolumeclaims",
                    "serviceaccounts",
                ],
                "verbs": ["get", "list", "create", "patch"],
            },
            {
                "apiGroups": ["apps"],
                "resources": ["deployments", "deployments/scale"],
                "verbs": ["get", "list", "create", "patch"],
            },
            {
                "apiGroups": ["discovery.k8s.io"],
                "resources": ["endpointslices"],
                "verbs": ["get", "list"],
            },
            {
                "apiGroups": ["networking.k8s.io"],
                "resources": ["ingresses", "networkpolicies"],
                "verbs": ["get", "list", "create", "patch"],
            },
        ],
    )
    binding = document(
        "RoleBinding",
        "lightrag-release",
        ns,
        roleRef={
            "apiGroup": "rbac.authorization.k8s.io",
            "kind": "Role",
            "name": "lightrag-release",
        },
        subjects=[{"kind": "ServiceAccount", "name": control, "namespace": ns}],
    )
    identity_role = document(
        "ClusterRole",
        "lightrag-release-identity-" + ns,
        ns,
        rules=[
            {
                "apiGroups": [""],
                "resources": ["namespaces"],
                "resourceNames": [ns, "kube-system"],
                "verbs": ["get"],
            }
        ],
    )
    identity_binding = document(
        "ClusterRoleBinding",
        "lightrag-release-identity-" + ns,
        ns,
        roleRef={
            "apiGroup": "rbac.authorization.k8s.io",
            "kind": "ClusterRole",
            "name": identity_role["metadata"]["name"],
        },
        subjects=[{"kind": "ServiceAccount", "name": control, "namespace": ns}],
    )
    return [
        account,
        control_sa,
        pvc,
        policy,
        role,
        binding,
        identity_role,
        identity_binding,
    ]


def content_hash(value):
    return hashlib.sha256(
        json.dumps(
            value, sort_keys=True, separators=(",", ":"), allow_nan=False
        ).encode()
    ).hexdigest()


def write_overlay(directory, resources):
    """Write a new private overlay directory; never overwrite existing output."""
    import os
    from pathlib import Path

    directory = Path(directory)
    directory.mkdir(mode=0o700)
    documents = {
        "kustomization.yaml": {
            "apiVersion": "kustomize.config.k8s.io/v1beta1",
            "kind": "Kustomization",
            "resources": ["resources.json"],
        },
        "resources.json": {"apiVersion": "v1", "kind": "List", "items": resources},
    }
    for name, body in documents.items():
        fd = os.open(directory / name, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w") as output:
            json.dump(body, output, sort_keys=True, indent=2)
            output.write("\n")
