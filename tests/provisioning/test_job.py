import importlib.util
import json

import pytest

from .test_plan import environment


def settings():
    from .test_plan import build
    from lightrag.provisioning.plan import digest
    from lightrag.provisioning.manifest import manifest_hash

    return {
        "expected_profile_hash": build().profile_hash,
        "expected_plan_hash": digest(build().summary()),
        "expected_manifest_hash": manifest_hash(
            dict(environment(), LIGHTRAG_OBJECT_STORAGE="s3")
        ),
        "phase": "apply",
        "namespace": "migration-fixture",
        "name": "lightrag-init-v1",
        "image": "registry.invalid/lightrag@sha256:" + "a" * 64,
        "environment_secret": "runtime-fixture",
        "environment_key": "environment.json",
        "operator_secret": "operator-fixture",
        "operator_key": "dsn",
        "graph_secret": "graph-operator-fixture",
        "graph_user_key": "username",
        "graph_password_key": "password",
        "image_pull_secret": "existing-pull",
        "graph_wait_seconds": 2,
        "active_deadline_seconds": 120,
        "ttl_seconds": 300,
        "cpu_request": "100m",
        "cpu_limit": "1",
        "memory_request": "256Mi",
        "memory_limit": "1Gi",
        "confirm_maintenance": True,
        "confirm_empty_target": True,
    }


def render(values=None):
    assert importlib.util.find_spec("lightrag.provisioning.job"), (
        "Offline initialization Job renderer is missing"
    )
    from lightrag.provisioning.job import render_job

    return render_job(
        settings() if values is None else values,
        dict(environment(), LIGHTRAG_OBJECT_STORAGE="s3"),
    )


def test_job_uses_only_existing_refs_and_explicit_operator_phases(monkeypatch):
    import socket

    monkeypatch.setattr(
        socket.socket,
        "connect",
        lambda *a: (_ for _ in ()).throw(AssertionError("network forbidden")),
    )
    job = render()
    assert job["kind"] == "Job"
    spec = job["spec"]
    assert spec["backoffLimit"] == 0
    pod = spec["template"]["spec"]
    assert (
        pod["restartPolicy"] == "Never" and pod["automountServiceAccountToken"] is False
    )
    assert pod["imagePullSecrets"] == [{"name": "existing-pull"}]
    app = pod["containers"][0]
    assert app["command"] == ["python", "-m", "lightrag.provisioning"]
    assert (
        app["args"][0] == "apply"
        and "--confirm-maintenance" in app["args"]
        and "--confirm-empty-target" in app["args"]
    )
    assert all(
        "valueFrom" in e or e["name"] in {"PYTHON_DOTENV_DISABLED", "LOG_DIR"}
        for e in app["env"]
    )
    assert pod["volumes"][0]["secret"]["secretName"] == "runtime-fixture"
    serialized = json.dumps(job)
    assert (
        'Secret"' not in serialized
        and "lightrag-server" not in serialized
        and "kubectl" not in serialized
    )
    assert "enroll" not in serialized and "recover" not in serialized
    assert (
        "--expected-plan-hash" in app["args"]
        and "--expected-manifest-hash" not in app["args"]
    )


@pytest.mark.parametrize(
    "field,value",
    [
        ("image", "registry.invalid/lightrag:latest"),
        ("operator_secret", "same/name"),
        ("graph_wait_seconds", None),
        ("active_deadline_seconds", 1),
        ("confirm_maintenance", False),
        ("confirm_empty_target", False),
        ("expected_plan_hash", "b" * 64),
    ],
)
def test_invalid_or_unapproved_job_refuses(field, value):
    v = settings()
    v[field] = value
    with pytest.raises(ValueError):
        render(v)


def test_readonly_verify_and_registration_are_distinct_jobs():
    v = settings()
    v.update(phase="verify", confirm_maintenance=False, confirm_empty_target=False)
    job = render(v)
    args = job["spec"]["template"]["spec"]["containers"][0]["args"]
    assert args[0] == "verify" and "--confirm-maintenance" not in args
    v["phase"] = "register"
    v["confirm_maintenance"] = True
    args = render(v)["spec"]["template"]["spec"]["containers"][0]["args"]
    assert (
        args[0] == "register"
        and "--expected-manifest-hash" in args
        and "--confirm-empty-target" not in args
    )
