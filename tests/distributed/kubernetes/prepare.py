"""Generate isolated Kind acceptance fixtures; never apply resources automatically.

Run from the repository root. Use a fresh directory, cluster and workspace for
an independent run. Generated credentials must remain outside the repository.
"""

import argparse
from pathlib import Path
import secrets

import yaml

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument("--directory", required=True)
parser.add_argument("--image-tag", required=True)
parser.add_argument("--workspace", required=True)
args = parser.parse_args()
root = Path(args.directory).expanduser().resolve()
repository = Path(__file__).resolve().parents[3]
if root == repository or repository in root.parents:
    raise ValueError("Credentials must be generated outside the repository")
root.mkdir(parents=True, exist_ok=True)
if any(root.iterdir()):
    raise ValueError(
        "Use an empty directory; existing credentials are never overwritten"
    )
if not args.workspace.startswith("local_debug_"):
    raise ValueError("Only a local_debug_ workspace is allowed")
root.chmod(0o700)
ns = "lightrag-concurrency-test"
password = secrets.token_hex(24)
hg_password = secrets.token_hex(24)
api_key = secrets.token_hex(24)
env = {
    "POSTGRES_PASSWORD": password,
    "LIGHTRAG_COORDINATION_DSN": f"postgresql://postgres:{password}@postgres:5432/local_debug_k8s",
    "HUGEGRAPH_USERNAME": "admin",
    "HUGEGRAPH_PASSWORD": hg_password,
    "LIGHTRAG_API_KEY": api_key,
    "LLM_BINDING_API_KEY": "isolated-test-fixture",
    "EMBEDDING_BINDING_API_KEY": "isolated-test-fixture",
}
p = root / "credentials.env"
with p.open("x") as handle:
    p.chmod(0o600)
    handle.write("\n".join(f"{k}={v}" for k, v in env.items()) + "\n")
objects = [
    {
        "apiVersion": "v1",
        "kind": "Namespace",
        "metadata": {
            "name": ns,
            "labels": {"purpose": "lightrag-concurrency-acceptance"},
        },
    }
]
for key, subdir in [("inputs", "inputs"), ("working", "working")]:
    name = f"lightrag-test-{key}"
    objects.extend(
        [
            {
                "apiVersion": "v1",
                "kind": "PersistentVolume",
                "metadata": {"name": name},
                "spec": {
                    "capacity": {"storage": "2Gi"},
                    "accessModes": ["ReadWriteMany"],
                    "persistentVolumeReclaimPolicy": "Retain",
                    "storageClassName": "",
                    "hostPath": {
                        "path": f"/lightrag-shared/{subdir}",
                        "type": "Directory",
                    },
                    "claimRef": {"namespace": ns, "name": name},
                },
            },
            {
                "apiVersion": "v1",
                "kind": "PersistentVolumeClaim",
                "metadata": {"name": name, "namespace": ns},
                "spec": {
                    "accessModes": ["ReadWriteMany"],
                    "storageClassName": "",
                    "volumeName": name,
                    "resources": {"requests": {"storage": "2Gi"}},
                },
            },
        ]
    )
objects.append(
    {
        "apiVersion": "v1",
        "kind": "ConfigMap",
        "metadata": {"name": "test-bootstrap-sql", "namespace": ns},
        "data": {"init.sql": "CREATE EXTENSION IF NOT EXISTS vector;\n"},
    }
)
objects.append(
    {
        "apiVersion": "v1",
        "kind": "ConfigMap",
        "metadata": {"name": "acceptance-gateway", "namespace": ns},
        "data": {
            "gateway.py": Path("tests/distributed/kubernetes/gateway.py").read_text()
        },
    }
)
for name, image, port in [
    ("postgres", "pgvector/pgvector:pg17", 5432),
    ("hugegraph", "hugegraph/hugegraph:1.7.0", 8080),
    ("gateway", "python:3.12-slim", 8090),
]:
    container = {
        "name": name,
        "image": image,
        "imagePullPolicy": "IfNotPresent",
        "ports": [{"containerPort": port}],
        "resources": {
            "requests": {"cpu": "100m", "memory": "256Mi"},
            "limits": {"cpu": "2", "memory": "3Gi"},
        },
        "readinessProbe": {
            "tcpSocket": {"port": port},
            "initialDelaySeconds": 5,
            "periodSeconds": 3,
        },
    }
    spec = {"containers": [container]}
    if name == "postgres":
        container["env"] = [
            {"name": "POSTGRES_DB", "value": "local_debug_k8s"},
            {
                "name": "POSTGRES_PASSWORD",
                "valueFrom": {
                    "secretKeyRef": {
                        "name": "lightrag-test-credentials",
                        "key": "POSTGRES_PASSWORD",
                    }
                },
            },
        ]
        container["volumeMounts"] = [
            {"name": "data", "mountPath": "/var/lib/postgresql/data"},
            {"name": "init", "mountPath": "/docker-entrypoint-initdb.d"},
        ]
        spec["volumes"] = [
            {
                "name": "data",
                "persistentVolumeClaim": {"claimName": "test-postgres-data"},
            },
            {"name": "init", "configMap": {"name": "test-bootstrap-sql"}},
        ]
        objects.append(
            {
                "apiVersion": "v1",
                "kind": "PersistentVolumeClaim",
                "metadata": {"name": "test-postgres-data", "namespace": ns},
                "spec": {
                    "accessModes": ["ReadWriteOnce"],
                    "resources": {"requests": {"storage": "2Gi"}},
                },
            }
        )
    elif name == "hugegraph":
        container["env"] = [
            {
                "name": "PASSWORD",
                "valueFrom": {
                    "secretKeyRef": {
                        "name": "lightrag-test-credentials",
                        "key": "HUGEGRAPH_PASSWORD",
                    }
                },
            }
        ]
    else:
        container["command"] = ["python", "/fixture/gateway.py"]
        container["volumeMounts"] = [
            {"name": "fixture", "mountPath": "/fixture", "readOnly": True}
        ]
        spec["volumes"] = [
            {"name": "fixture", "configMap": {"name": "acceptance-gateway"}}
        ]
    objects.extend(
        [
            {
                "apiVersion": "apps/v1",
                "kind": "Deployment",
                "metadata": {"name": name, "namespace": ns},
                "spec": {
                    "replicas": 1,
                    "selector": {"matchLabels": {"test-component": name}},
                    "template": {
                        "metadata": {"labels": {"test-component": name}},
                        "spec": spec,
                    },
                },
            },
            {
                "apiVersion": "v1",
                "kind": "Service",
                "metadata": {"name": name, "namespace": ns},
                "spec": {
                    "selector": {"test-component": name},
                    "ports": [{"port": port, "targetPort": port}],
                },
            },
        ]
    )
(root / "infra.yaml").write_text(yaml.safe_dump_all(objects, sort_keys=False))
values = {
    "replicaCount": 0,
    "image": {"repository": "lightrag", "tag": args.image_tag},
    "maintenance": {
        "enabled": True,
        "actor": "local-k8s-acceptance",
        "writersStopped": True,
        "inflightFinished": True,
    },
    "envFrom": {"secrets": [{"name": "lightrag-test-credentials"}]},
    "persistence": {
        "inputs": {"existingClaim": "lightrag-test-inputs"},
        "ragStorage": {"existingClaim": "lightrag-test-working"},
    },
    "resources": {
        "requests": {"cpu": "250m", "memory": "512Mi"},
        "limits": {"cpu": "2", "memory": "3Gi"},
    },
    "env": {
        "WORKSPACE": args.workspace,
        "POSTGRES_WORKSPACE": args.workspace,
        "LIGHTRAG_DEPLOYMENT_ID": args.workspace + "_deployment",
        "POSTGRES_HOST": "postgres",
        "POSTGRES_DATABASE": "local_debug_k8s",
        "POSTGRES_USER": "postgres",
        "HUGEGRAPH_URI": "http://gateway:8090",
        "HUGEGRAPH_AUTO_CREATE_SCHEMA": "true",
        # Two Pods can still overlap; stay below the small fixture server budget.
        "HUGEGRAPH_MAX_CONNECTIONS": "1",
        "LLM_BINDING": "openai",
        "LLM_MODEL": "k8s-fixture",
        "LLM_BINDING_HOST": "http://gateway:8090/v1",
        "EMBEDDING_BINDING": "openai",
        "EMBEDDING_MODEL": "k8s-fixture",
        "EMBEDDING_DIM": "8",
        "EMBEDDING_BINDING_HOST": "http://gateway:8090/v1",
        "ENTITY_EXTRACTION_USE_JSON": "false",
        "MAX_GLEANING": "0",
        "MAX_PARALLEL_INSERT": "1",
        "LIGHTRAG_DISTRIBUTED_POLL_INTERVAL": "0.2",
        "RERANK_BINDING": "null",
        "LLM_TIMEOUT": "180",
    },
}
(root / "values.yaml").write_text(yaml.safe_dump(values, sort_keys=False))
print(
    "Prepared isolated fixtures and protected credential file; no credential values printed."
)

for subdir in ("inputs", "working"):
    path = root / "shared" / subdir
    path.mkdir(parents=True)
    # Disposable local hostPath only; production must provision proper RWX ACLs.
    path.chmod(0o777)
node_mount = [{"hostPath": str(root / "shared"), "containerPath": "/lightrag-shared"}]
(root / "kind.yaml").write_text(
    yaml.safe_dump(
        {
            "kind": "Cluster",
            "apiVersion": "kind.x-k8s.io/v1alpha4",
            "nodes": [
                {"role": "control-plane"},
                {"role": "worker", "extraMounts": node_mount},
                {"role": "worker", "extraMounts": node_mount},
            ],
        },
        sort_keys=False,
    )
)
(root / "writers-patch.yaml").write_text(
    yaml.safe_dump(
        {
            "spec": {
                "template": {
                    "metadata": {"labels": {"lightrag-test-writer": "true"}},
                    "spec": {
                        "affinity": {
                            "podAntiAffinity": {
                                "requiredDuringSchedulingIgnoredDuringExecution": [
                                    {
                                        "labelSelector": {
                                            "matchLabels": {
                                                "lightrag-test-writer": "true"
                                            }
                                        },
                                        "topologyKey": "kubernetes.io/hostname",
                                    }
                                ],
                            }
                        }
                    },
                }
            },
        },
        sort_keys=False,
    )
)
