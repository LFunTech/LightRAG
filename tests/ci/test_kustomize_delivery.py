from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[2]
OVERLAY = ROOT / "k8s-deploy/lightrag-kustomize/overlays/test"
DEPLOY_WORKFLOW = ROOT / ".woodpecker/deploy-test.yml"
RUNBOOK = ROOT / "docs/WoodpeckerTestDelivery.md"


def test_deploy_workflow_uses_kubectl_apply_k_not_helm():
    text = DEPLOY_WORKFLOW.read_text()
    assert "helm" not in text.lower()
    assert "scripts/ci/deploy-test.sh" in text
    script = (ROOT / "scripts/ci/deploy-test.sh").read_text()
    assert "kubectl apply -k" in script
    assert 'wait --for=condition=Ready pod' in script
    assert "imageID" in script
    workflow = yaml.safe_load(text)
    step = workflow["steps"]["deploy-test"]
    assert "kubeconfig_test" in text
    assert "LIGHTRAG_TEST_KUBECONFIG" in step["environment"]


def test_kustomize_overlay_declares_test_namespace_and_digest_patch():
    kustomization = yaml.safe_load((OVERLAY / "kustomization.yaml").read_text())
    assert kustomization["namespace"] == "lightrag-test"
    assert "../../base" in kustomization["resources"]
    replacements = kustomization["replacements"]
    assert any(r["source"]["fieldPath"] == "data.digest" for r in replacements)


def test_kustomize_base_keeps_service_private_and_routing_paused():
    service = yaml.safe_load((OVERLAY.parent.parent / "base/service.yaml").read_text())
    assert service["spec"]["type"] == "ClusterIP"
    assert service["spec"]["selector"] == {"lightrag.openai.com/routing-paused": "true"}


def test_kustomize_deployment_preserves_distributed_profile_without_plaintext_secrets():
    deployment = yaml.safe_load((OVERLAY.parent.parent / "base/deployment.yaml").read_text())
    assert deployment["spec"]["replicas"] == 2
    spec = deployment["spec"]["template"]["spec"]
    assert spec["securityContext"]["runAsNonRoot"] is True
    assert spec["containers"][0]["envFrom"] == [{"secretRef": {"name": "lightrag-runtime"}}]
    env = {item["name"]: item["value"] for item in spec["containers"][0]["env"]}
    assert env["WORKERS"] == "1"
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
        "LIGHTRAG_API_KEY",
        "LLM_BINDING_API_KEY",
        "LLM_BINDING_HOST",
        "EMBEDDING_BINDING_API_KEY",
        "EMBEDDING_BINDING_HOST",
    }
    assert not (forbidden & set(env))


def test_runbook_describes_kustomize_not_helm_release_state():
    text = RUNBOOK.read_text()
    assert "Kustomize" in text
    assert "kubectl apply -k" in text
    assert "Helm release" not in text
