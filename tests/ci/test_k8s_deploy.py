import json
from pathlib import Path

import pytest
import yaml

from scripts.ci import k8s_deploy


def test_inject_digest_updates_overlay_configmap_only(tmp_path):
    overlay = tmp_path / "overlay"
    overlay.mkdir()
    release = overlay / "release-image.yaml"
    release.write_text("apiVersion: v1\nkind: ConfigMap\ndata:\n  digest: sha256:" + "0" * 64 + "\n")
    k8s_deploy.inject_digest(overlay, "sha256:" + "a" * 64)
    data = yaml.safe_load(release.read_text())
    assert data["data"]["digest"] == "sha256:" + "a" * 64
    with pytest.raises(k8s_deploy.DeploymentError, match="invalid digest"):
        k8s_deploy.inject_digest(overlay, "latest")


def test_deploy_sequence_pauses_scales_checks_applies_accepts_then_restores():
    calls = []

    def runner(args, *, input_text=None):
        calls.append(args)
        if args[:3] == ["kubectl", "get", "configmap"]:
            return json.dumps({
                "cluster": "test",
                "namespace": "lightrag-test-01",
                "initialized": True,
                "profile": {
                    "replicas": 2,
                    "workers": 1,
                    "kv_storage": "PGKVStorage",
                    "doc_status_storage": "PGDocStatusStorage",
                    "vector_storage": "PGVectorStorage",
                    "graph_storage": "HugeGraphStorage",
                    "workspace": "lightrag_test_01",
                    "postgres_workspace": "lightrag_test_01",
                },
                "storage_state": {"fenced": False, "active_operations": 0, "pending_mutations": 0, "orphaned_claims": 0},
            })
        return ""

    k8s_deploy.deploy_test(
        kubeconfig="/tmp/kube/config",
        digest="sha256:" + "a" * 64,
        overlay=Path("k8s-deploy/lightrag-kustomize/overlays/test"),
        runner=runner,
    )
    joined = [" ".join(c) for c in calls]
    assert any("patch service lightrag" in c and "routing-paused" in c for c in joined)
    assert any("scale deployment/lightrag --replicas=0" in c for c in joined)
    assert any("wait --for=delete pod" in c for c in joined)
    assert any("apply -k k8s-deploy/lightrag-kustomize/overlays/test" in c for c in joined)
    assert any("rollout status deployment/lightrag" in c for c in joined)
    assert any("-n lightrag-test-01" in c for c in joined)
    assert not any("patch service lightrag" in c and "app.kubernetes.io/name" in c for c in joined)


def test_accept_and_restore_restores_routing_only_after_valid_acceptance():
    calls = []

    def runner(args, *, input_text=None):
        calls.append(args)
        return ""

    report = {
        "pods": [
            {"metadata": {"name": "a"}, "status": {"containerStatuses": [{"imageID": "repo@sha256:" + "a" * 64}]}},
            {"metadata": {"name": "b"}, "status": {"containerStatuses": [{"imageID": "repo@sha256:" + "a" * 64}]}},
        ],
        "health": [200, 200],
        "unauthenticated_status": 403,
        "cross_pod_query_ok": True,
    }
    k8s_deploy.accept_and_restore(
        kubeconfig="/tmp/kube/config",
        digest="sha256:" + "a" * 64,
        report=report,
        runner=runner,
    )
    joined = [" ".join(c) for c in calls]
    assert any("patch service lightrag" in c and "app.kubernetes.io/name" in c for c in joined)

    calls.clear()
    bad = dict(report, cross_pod_query_ok=False)
    with pytest.raises(k8s_deploy.DeploymentError, match="cross-pod"):
        k8s_deploy.accept_and_restore(
            kubeconfig="/tmp/kube/config",
            digest="sha256:" + "a" * 64,
            report=bad,
            runner=runner,
        )
    assert not calls


def test_deploy_sequence_rejects_unsafe_storage_before_apply():
    calls = []

    def runner(args, *, input_text=None):
        calls.append(args)
        if args[:3] == ["kubectl", "get", "configmap"]:
            return json.dumps({"cluster": "test", "namespace": "lightrag-test-01", "initialized": False})
        return ""

    with pytest.raises(k8s_deploy.DeploymentError, match="initialized"):
        k8s_deploy.deploy_test(
            kubeconfig="/tmp/kube/config",
            digest="sha256:" + "a" * 64,
            overlay=Path("k8s-deploy/lightrag-kustomize/overlays/test"),
            runner=runner,
        )
    assert not any("apply" in c for call in calls for c in call)


def test_acceptance_rejects_image_mismatch_and_missing_auth():
    pods = [
        {"metadata": {"name": "a"}, "status": {"containerStatuses": [{"imageID": "repo@sha256:" + "a" * 64}]}},
        {"metadata": {"name": "b"}, "status": {"containerStatuses": [{"imageID": "repo@sha256:" + "a" * 64}]}},
    ]
    ok = {"pods": pods, "health": [200, 200], "unauthenticated_status": 403, "cross_pod_query_ok": True}
    k8s_deploy.validate_acceptance(ok, expected_digest="sha256:" + "a" * 64)
    bad = dict(ok, unauthenticated_status=200)
    with pytest.raises(k8s_deploy.DeploymentError, match="authentication"):
        k8s_deploy.validate_acceptance(bad, expected_digest="sha256:" + "a" * 64)
    bad = dict(ok, pods=[pods[0], {"metadata": {"name": "b"}, "status": {"containerStatuses": [{"imageID": "repo@sha256:" + "b" * 64}]}}])
    with pytest.raises(k8s_deploy.DeploymentError, match="imageID"):
        k8s_deploy.validate_acceptance(bad, expected_digest="sha256:" + "a" * 64)
