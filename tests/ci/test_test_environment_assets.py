from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[2]
BASE = ROOT / "k8s-deploy/lightrag-kustomize/base"
OVERLAY = ROOT / "k8s-deploy/lightrag-kustomize/overlays/test"
RUNBOOK = ROOT / "docs/WoodpeckerTestDelivery.md"


def test_kustomize_test_deployment_preserves_distributed_profile_without_plaintext_secrets():
    deployment = yaml.safe_load((BASE / "deployment.yaml").read_text())
    assert deployment["spec"]["replicas"] == 2
    pod = deployment["spec"]["template"]["spec"]
    assert pod["securityContext"]["runAsNonRoot"] is True
    container = pod["containers"][0]
    assert container["envFrom"] == [{"secretRef": {"name": "lightrag-runtime"}}]
    env = {item["name"]: item["value"] for item in container["env"]}
    assert env["WORKERS"] == "1"
    assert env["LIGHTRAG_DEPLOYMENT_ID"] == "lightrag_test"
    assert env["LIGHTRAG_SHARED_STORAGE"] == "false"
    assert env["LIGHTRAG_OBJECT_STORAGE"] == "s3"
    assert env["ENABLE_LOCAL_FILE_INGESTION"] == "false"
    assert env["S3_OBJECT_PREFIX"] == "lightrag/test-object-ingestion"
    assert env["S3_SCRATCH_DIR"] == "/app/data/object-scratch"
    assert env["WORKSPACE"] == "lightrag_test"
    assert env["POSTGRES_WORKSPACE"] == "lightrag_test"
    assert env["LIGHTRAG_GRAPH_STORAGE"] == "HugeGraphStorage"
    assert env["EMBEDDING_MODEL"] == "text-embedding-v4"
    assert env["EMBEDDING_DIM"] == "1024"
    forbidden = {
        "POSTGRES_HOST",
        "POSTGRES_PORT",
        "POSTGRES_DATABASE",
        "POSTGRES_USER",
        "POSTGRES_PASSWORD",
        "HUGEGRAPH_URI",
        "HUGEGRAPH_GRAPH",
        "HUGEGRAPH_GRAPHSPACE",
        "HUGEGRAPH_USERNAME",
        "HUGEGRAPH_PASSWORD",
        "LLM_BINDING_API_KEY",
        "LLM_BINDING_HOST",
        "EMBEDDING_BINDING_API_KEY",
        "EMBEDDING_BINDING_HOST",
        "LIGHTRAG_API_KEY",
        "S3_ENDPOINT_URL",
        "S3_BUCKET",
        "S3_ACCESS_KEY_ID",
        "S3_SECRET_ACCESS_KEY",
        "S3_SESSION_TOKEN",
    }
    assert not (forbidden & set(env))
    volumes = {item["name"]: item for item in pod["volumes"]}
    assert volumes["inputs"] == {"name": "inputs", "emptyDir": {"sizeLimit": "1Gi"}}
    assert volumes["object-scratch"] == {
        "name": "object-scratch",
        "emptyDir": {"sizeLimit": "5Gi"},
    }
    assert "persistentVolumeClaim" not in volumes["inputs"]


def test_kustomize_deployer_rbac_is_namespace_scoped():
    docs = list(yaml.safe_load_all((OVERLAY / "deployer-rbac.yaml").read_text()))
    assert {doc["kind"] for doc in docs} >= {"Namespace", "ServiceAccount", "Role", "RoleBinding"}
    role = next(doc for doc in docs if doc["kind"] == "Role")
    resources = {resource for rule in role["rules"] for resource in rule.get("resources", [])}
    assert "clusterroles" not in resources
    assert "secrets" in resources
    assert "ingresses" in resources


def test_kustomize_network_policy_allows_only_internal_callers_and_rke2_ingress():
    policy = yaml.safe_load((BASE / "networkpolicy.yaml").read_text())
    assert policy["kind"] == "NetworkPolicy"
    assert policy["spec"]["policyTypes"] == ["Ingress", "Egress"]
    assert policy["spec"]["ingress"]
    assert all("ipBlock" not in peer for rule in policy["spec"]["ingress"] for peer in rule.get("from", []))
    ingress_peers = [
        peer
        for rule in policy["spec"]["ingress"]
        for peer in rule.get("from", [])
        if peer.get("podSelector", {}).get("matchLabels", {}).get("app.kubernetes.io/name")
        == "rke2-ingress-nginx"
    ]
    assert ingress_peers == [
        {
            "namespaceSelector": {"matchLabels": {"kubernetes.io/metadata.name": "kube-system"}},
            "podSelector": {
                "matchLabels": {
                    "app.kubernetes.io/name": "rke2-ingress-nginx",
                    "app.kubernetes.io/component": "controller",
                }
            },
        }
    ]
    backend_rule = next(
        rule
        for rule in policy["spec"]["egress"]
        if {port["port"] for port in rule.get("ports", [])} == {5432, 8080}
    )
    assert "to" not in backend_rule


def test_kustomize_pvcs_do_not_include_shared_input_dir():
    docs = list(yaml.safe_load_all((BASE / "pvc.yaml").read_text()))
    assert {doc["metadata"]["name"] for doc in docs} == {
        "lightrag-test-working-rwx",
    }
    assert all(doc["spec"]["storageClassName"] == "syno-nfs" for doc in docs)


def test_runbook_records_pipeline_managed_initialization_and_remote_verification_boundary():
    text = RUNBOOK.read_text()
    assert "lightrag-test-01" in text
    assert "lightrag-test-05" in text
    assert "rag-test-01.f123.pub" in text
    assert "rag-test-05.f123.pub" in text
    assert "test.secrets" in text
    assert "scripts/ci/generate-test-instance-api-keys.py" in text
    assert "Woodpecker repo secrets" in text
    assert "lightrag_test_hugegraph_gremlin" in text
    assert "lightrag_test_01_api_key" in text
    assert "lightrag_test_05_api_key" in text
    assert "auth-method metadata defaults to `basic`" in text
    assert "coordination migration Job" in text
    assert "lightrag-storage-preflight" in text
    assert "storage bootstrap Job" in text
    assert "LIGHTRAG_TEST_01_PUBLIC_HOST" in text
    assert "Ingress" in text
    assert "/webui" in text
    assert "creates or updates Kubernetes runtime Secrets" in text
    assert "Remote acceptance not yet run" in text
    assert "Do not claim YAML lint, Kustomize render, or local test results as deployment success" in text
