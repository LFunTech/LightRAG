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
    assert env["LIGHTRAG_GRAPH_STORAGE"] == "HugeGraphStorage"
    assert env["EMBEDDING_MODEL"] == "text-embedding-v4"
    assert env["EMBEDDING_DIM"] == "1024"
    forbidden = {"POSTGRES_PASSWORD", "HUGEGRAPH_PASSWORD", "LLM_BINDING_API_KEY", "EMBEDDING_BINDING_API_KEY", "LIGHTRAG_API_KEY"}
    assert not (forbidden & set(env))


def test_kustomize_deployer_rbac_is_namespace_scoped():
    docs = list(yaml.safe_load_all((OVERLAY / "deployer-rbac.yaml").read_text()))
    assert {doc["kind"] for doc in docs} >= {"Namespace", "ServiceAccount", "Role", "RoleBinding"}
    role = next(doc for doc in docs if doc["kind"] == "Role")
    resources = {resource for rule in role["rules"] for resource in rule.get("resources", [])}
    assert "clusterroles" not in resources
    assert "secrets" in resources
    assert "ingresses" not in resources


def test_kustomize_network_policy_keeps_application_private_with_explicit_internal_callers():
    policy = yaml.safe_load((BASE / "networkpolicy.yaml").read_text())
    assert policy["kind"] == "NetworkPolicy"
    assert policy["spec"]["policyTypes"] == ["Ingress", "Egress"]
    assert policy["spec"]["ingress"]
    assert all("ipBlock" not in peer for rule in policy["spec"]["ingress"] for peer in rule.get("from", []))


def test_runbook_records_manual_initialization_and_remote_verification_boundary():
    text = RUNBOOK.read_text()
    assert "lightrag-test" in text
    assert "not part of every tag deployment" in text
    assert "Remote acceptance not yet run" in text
    assert "Do not claim YAML lint, Kustomize render, or local test results as deployment success" in text
