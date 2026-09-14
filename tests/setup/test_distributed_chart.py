"""Render the real Helm chart and inspect parsed Kubernetes objects."""

import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]
CHART = ROOT / "k8s-deploy/lightrag"
pytestmark = pytest.mark.skipif(not shutil.which("helm"), reason="helm required")


def render(tmp_path, values=None):
    path = tmp_path / "values.yaml"
    path.write_text(yaml.safe_dump(values or {}))
    return subprocess.run(
        ["helm", "template", "acceptance", str(CHART), "-f", str(path)],
        capture_output=True,
        text=True,
    )


def profile():
    return yaml.safe_load((CHART / "values-distributed.yaml").read_text())


def test_default_chart_remains_single_replica_rwo(tmp_path):
    result = render(tmp_path)
    assert result.returncode == 0, result.stderr
    docs = list(yaml.safe_load_all(result.stdout))
    assert next(d for d in docs if d["kind"] == "Deployment")["spec"]["replicas"] == 1
    claims = [d for d in docs if d["kind"] == "PersistentVolumeClaim"]
    assert len(claims) == 2
    assert all(d["spec"]["accessModes"] == ["ReadWriteOnce"] for d in claims)


def test_distributed_shared_claims_and_bootstrap_job(tmp_path):
    values = profile()
    values["maintenance"].update(
        enabled=True, writersStopped=True, inflightFinished=True
    )
    values["replicaCount"] = 0
    result = render(tmp_path, values)
    assert result.returncode == 0, result.stderr
    docs = list(yaml.safe_load_all(result.stdout))
    deployment = next(d for d in docs if d["kind"] == "Deployment")
    job = next(d for d in docs if d["kind"] == "Job")
    assert deployment["spec"]["replicas"] == 0
    app = deployment["spec"]["template"]["spec"]
    maintenance = job["spec"]["template"]["spec"]
    assert app["volumes"] == maintenance["volumes"]
    assert app["securityContext"]["runAsNonRoot"] is True
    assert app["containers"][0]["envFrom"] == maintenance["containers"][0]["envFrom"]
    assert app["containers"][0]["image"] == maintenance["containers"][0]["image"]
    assert "bootstrap" in maintenance["containers"][0]["command"][-1]
    assert job["spec"]["backoffLimit"] == 0
    assert not any(d["kind"] == "PersistentVolumeClaim" for d in docs)


@pytest.mark.parametrize(
    "section,key,value",
    [
        ("env", "LIGHTRAG_GRAPH_STORAGE", "NetworkXStorage"),
        ("env", "LIGHTRAG_SHARED_STORAGE", "false"),
        ("env", "POSTGRES_WORKSPACE", "wrong"),
        ("env", "LIGHTRAG_COORDINATION_POOL_MODE", "transaction"),
        ("workload", "kind", "StatefulSet"),
        ("persistence", "enabled", False),
        ("image", "tag", "latest"),
        ("env", "WORKERS", "2"),
    ],
)
def test_distributed_rejects_unsafe_profiles(tmp_path, section, key, value):
    values = profile()
    values[section][key] = value
    result = render(tmp_path, values)
    assert result.returncode != 0
    assert "distributed" in result.stderr.lower()


def test_distributed_new_claims_are_rwx(tmp_path):
    values = profile()
    for name in ("inputs", "ragStorage"):
        values["persistence"][name]["existingClaim"] = ""
    result = render(tmp_path, values)
    assert result.returncode == 0, result.stderr
    claims = [
        d
        for d in yaml.safe_load_all(result.stdout)
        if d["kind"] == "PersistentVolumeClaim"
    ]
    assert len(claims) == 2
    assert all(d["spec"]["accessModes"] == ["ReadWriteMany"] for d in claims)
    values["persistence"]["inputs"]["accessModes"] = ["ReadWriteOnce"]
    assert render(tmp_path, values).returncode != 0


def test_maintenance_requires_actual_operator_confirmations(tmp_path):
    values = profile()
    values["maintenance"]["enabled"] = True
    result = render(tmp_path, values)
    assert result.returncode != 0
    assert "writersStopped/inflightFinished" in result.stderr


@pytest.mark.parametrize("mode", ["default", "stateful", "distributed"])
def test_pod_templates_disable_service_links_that_shadow_dotenv(tmp_path, mode):
    """A postgres Service must not inject a tcp:// POSTGRES_PORT over .env."""
    values = {}
    if mode == "stateful":
        values = {"workload": {"kind": "StatefulSet"}}
    elif mode == "distributed":
        values = profile()
        values["replicaCount"] = 0
        values["maintenance"].update(
            enabled=True, writersStopped=True, inflightFinished=True
        )
    result = render(tmp_path, values)
    assert result.returncode == 0, result.stderr
    workloads = [
        doc
        for doc in yaml.safe_load_all(result.stdout)
        if doc["kind"] in {"Deployment", "StatefulSet", "Job"}
    ]
    assert len(workloads) == (2 if mode == "distributed" else 1)
    for workload in workloads:
        assert workload["spec"]["template"]["spec"].get("enableServiceLinks") is False
