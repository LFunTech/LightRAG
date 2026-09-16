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
    assert 'kubectl -n "$NAMESPACE" apply -k "$OVERLAY"' in script
    assert "wait --for=condition=Ready pod" in script
    assert "imageID" in script
    workflow = yaml.safe_load(text)
    step = workflow["steps"]["deploy-test"]
    assert "kubeconfig_test" in text
    assert "LIGHTRAG_TEST_KUBECONFIG" in step["environment"]
    assert step["environment"]["LIGHTRAG_TEST_INSTANCES"] == "01,02,03,04,05"
    assert "LIGHTRAG_TEST_PUBLIC_HOST" not in step["environment"]
    assert (
        step["environment"]["LIGHTRAG_TEST_S3_ENDPOINT_URL"]["from_secret"]
        == "lightrag_cos_storage_endpoint"
    )
    assert (
        step["environment"]["LIGHTRAG_TEST_S3_ACCESS_KEY_ID"]["from_secret"]
        == "lightrag_cos_storage_secret_id"
    )
    assert "verify_public_ingress" in script
    assert '"/health"' in script
    assert "/webui" not in script
    assert "wait_for_pvc_bound lightrag-test-inputs-rwx" not in script
    for forbidden in (
        '"/documents/uploads/presign"',
        '"/documents/uploads/complete"',
        '"/documents/reprocess_failed"',
        '"/documents/delete_document"',
        '"/query"',
    ):
        assert forbidden not in script
    assert "LIGHTRAG_TEST_S3_ENDPOINT_URL" in script


def test_kustomize_overlay_declares_test_namespace_and_digest_patch():
    kustomization = yaml.safe_load((OVERLAY / "kustomization.yaml").read_text())
    assert kustomization["namespace"] == "lightrag-test-01"
    assert "../../base" in kustomization["resources"]
    assert "workspace-profile.yaml" in kustomization["resources"]
    replacements = kustomization["replacements"]
    assert any(r["source"]["fieldPath"] == "data.digest" for r in replacements)
    assert any(
        r["source"]["fieldPath"] == "data.host"
        and any(
            "Ingress" == target["select"]["kind"]
            and "spec.rules.0.host" in target["fieldPaths"]
            for target in r["targets"]
        )
        for r in replacements
    )
    assert any(
        r["source"]["fieldPath"] == "data.ingressClassName"
        and any(
            "Ingress" == target["select"]["kind"]
            and "spec.ingressClassName" in target["fieldPaths"]
            for target in r["targets"]
        )
        for r in replacements
    )

    assert any(
        r["source"]["fieldPath"] == "data.workspace"
        and any(
            "Deployment" == target["select"]["kind"]
            and "spec.template.spec.containers.[name=lightrag].env.[name=WORKSPACE].value"
            in target["fieldPaths"]
            for target in r["targets"]
        )
        for r in replacements
    )
    assert any(
        r["source"]["fieldPath"] == "data.s3ObjectPrefix"
        and any(
            "Deployment" == target["select"]["kind"]
            and "spec.template.spec.containers.[name=lightrag].env.[name=S3_OBJECT_PREFIX].value"
            in target["fieldPaths"]
            for target in r["targets"]
        )
        for r in replacements
    )


def test_kustomize_base_keeps_service_private_and_routing_paused():
    service = yaml.safe_load((OVERLAY.parent.parent / "base/service.yaml").read_text())
    assert service["spec"]["type"] == "ClusterIP"
    assert service["spec"]["selector"] == {"lightrag.openai.com/routing-paused": "true"}


def test_kustomize_base_exposes_test_api_and_webui_through_ingress():
    ingress = yaml.safe_load((OVERLAY.parent.parent / "base/ingress.yaml").read_text())
    assert ingress["kind"] == "Ingress"
    assert ingress["metadata"]["name"] == "lightrag"
    assert ingress["spec"]["ingressClassName"] == "nginx"
    paths = ingress["spec"]["rules"][0]["http"]["paths"]
    assert paths == [
        {
            "path": "/",
            "pathType": "Prefix",
            "backend": {
                "service": {
                    "name": "lightrag",
                    "port": {"name": "http"},
                }
            },
        }
    ]
    assert (
        ingress["metadata"]["annotations"][
            "nginx.ingress.kubernetes.io/proxy-body-size"
        ]
        == "50m"
    )
    assert (
        ingress["metadata"]["annotations"][
            "nginx.ingress.kubernetes.io/proxy-buffering"
        ]
        == "off"
    )
    assert (
        ingress["metadata"]["annotations"][
            "nginx.ingress.kubernetes.io/configuration-snippet"
        ]
        == 'more_set_headers "X-Accel-Buffering: no";'
    )


def test_kustomize_deployment_preserves_distributed_profile_without_plaintext_secrets():
    deployment = yaml.safe_load(
        (OVERLAY.parent.parent / "base/deployment.yaml").read_text()
    )
    assert deployment["spec"]["replicas"] == 2
    spec = deployment["spec"]["template"]["spec"]
    assert spec["securityContext"]["runAsNonRoot"] is True
    assert spec["containers"][0]["envFrom"] == [
        {"secretRef": {"name": "lightrag-runtime"}}
    ]
    env = {item["name"]: item["value"] for item in spec["containers"][0]["env"]}
    assert env["WORKERS"] == "1"
    assert env["LIGHTRAG_SHARED_STORAGE"] == "false"
    assert env["LIGHTRAG_OBJECT_STORAGE"] == "s3"
    assert env["ENABLE_LOCAL_FILE_INGESTION"] == "false"
    assert env["S3_OBJECT_PREFIX"] == "lightrag/test-object-ingestion"
    assert env["S3_SCRATCH_DIR"] == "/app/data/object-scratch"
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
        "S3_ENDPOINT_URL",
        "S3_BUCKET",
        "S3_ACCESS_KEY_ID",
        "S3_SECRET_ACCESS_KEY",
        "S3_SESSION_TOKEN",
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
