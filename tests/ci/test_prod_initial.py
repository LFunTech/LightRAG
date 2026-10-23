"""Initial Kubernetes delivery is not a cloud-entry or capacity certification."""

from dataclasses import asdict
import importlib.util
import json

import pytest

from tests.ci.test_prod_config import config_data, load


def kubernetes_data():
    data = config_data()
    data["ingress"] = dict(
        provider="qcloud",
        mode="kubernetes",
        load_balancer_id="lb-fixture",
        backend_port=9621,
        path="/",
    )
    data["target"]["storage_class"] = None
    data["isolation"].update(acl_verified=False, evidence_id="not-remotely-verified")
    return data


def initial_inputs(tmp_path):
    from tests.ci.test_prod_inputs import inputs
    from lightrag_delivery.environment import runtime_environment, SECRET_ENV
    from lightrag.provisioning.plan import build_plan, digest
    from lightrag.provisioning.manifest import manifest_hash

    base, image, archive, record, env = inputs(tmp_path)
    data = asdict(base)
    example = kubernetes_data()
    for key in ("ingress", "isolation"):
        data[key] = example[key]
    data["target"]["storage_class"] = None
    config = load(tmp_path, data)
    secrets = {k: env["LIGHTRAG_PROD_" + k.upper()] for k in SECRET_ENV}
    runtime, _ = runtime_environment(config, secrets)
    data["onboarding"]["baseline_manifest_hash"] = manifest_hash(runtime)
    config = load(tmp_path, data)
    plan = build_plan(runtime)
    settings = dict(
        version=1,
        approval_id="first-k8s-deployment",
        confirm_maintenance=True,
        confirm_empty_target=True,
        confirm_shared_credentials=False,
        expected_plan_hash=digest(plan.summary()),
        graph_wait_seconds=2,
        job_wait_seconds=600,
        startup_wait_seconds=600,
    )
    return config, image, archive, record, env, runtime, settings


def module():
    assert importlib.util.find_spec("scripts.ci.prod_initial"), (
        "Initial Kubernetes delivery runner is missing"
    )
    from scripts.ci import prod_initial

    return prod_initial


def test_kubernetes_profile_needs_no_cloud_or_acl_certification(tmp_path):
    config = load(tmp_path, kubernetes_data())
    assert config.ingress["mode"] == "kubernetes"
    assert config.isolation["acl_verified"] is False
    assert "edgeone" not in config.ingress and "certificate_id" not in config.ingress


def test_kubernetes_profile_renders_http_and_bounded_per_pod_scratch(tmp_path):
    from lightrag_delivery.render import (
        render_slot,
        render_entrypoint,
        render_foundation,
    )

    config = load(tmp_path, kubernetes_data())
    identity = dict(config_hash="a" * 64, digest="sha256:" + "b" * 64)
    values = dict(
        LIGHTRAG_OBJECT_STORAGE="s3",
        ENABLE_LOCAL_FILE_INGESTION="false",
        LIGHTRAG_KV_STORAGE="PGKVStorage",
        LIGHTRAG_VECTOR_STORAGE="PGVectorStorage",
        LIGHTRAG_DOC_STATUS_STORAGE="PGDocStatusStorage",
        LIGHTRAG_GRAPH_STORAGE="HugeGraphStorage",
    )
    slot = render_slot(
        config, identity, "blue", image="registry.invalid/rag", environment=values
    )
    volume = next(
        v
        for v in slot[-1]["spec"]["template"]["spec"]["volumes"]
        if v["name"] == "working"
    )
    assert volume == {
        "name": "working",
        "emptyDir": {"sizeLimit": config.resources["working_storage"]},
    }
    assert not any(
        r["kind"] == "PersistentVolumeClaim" for r in render_foundation(config)
    )
    ingress = render_entrypoint(config, "blue")[1]
    assert not ingress["spec"].get("tls")
    assert not any(
        "https" in k or "certificate" in k for k in ingress["metadata"]["annotations"]
    )


@pytest.mark.parametrize(
    "field,value",
    [
        ("LIGHTRAG_OBJECT_STORAGE", ""),
        ("ENABLE_LOCAL_FILE_INGESTION", "true"),
        ("LIGHTRAG_KV_STORAGE", "JsonKVStorage"),
    ],
)
def test_ephemeral_working_volume_refuses_non_durable_profiles(tmp_path, field, value):
    from lightrag_delivery.render import render_slot

    config = load(tmp_path, kubernetes_data())
    env = dict(
        LIGHTRAG_OBJECT_STORAGE="s3",
        ENABLE_LOCAL_FILE_INGESTION="false",
        LIGHTRAG_KV_STORAGE="PGKVStorage",
        LIGHTRAG_VECTOR_STORAGE="PGVectorStorage",
        LIGHTRAG_DOC_STATUS_STORAGE="PGDocStatusStorage",
        LIGHTRAG_GRAPH_STORAGE="HugeGraphStorage",
    )
    env[field] = value
    with pytest.raises(ValueError):
        render_slot(
            config,
            dict(config_hash="a" * 64, digest="sha256:" + "b" * 64),
            "blue",
            image="registry.invalid/rag",
            environment=env,
        )


def test_initial_inputs_reuse_verified_artifacts_without_an_old_blue(tmp_path):
    mod = module()
    config, image, archive, record, ci, _, settings = initial_inputs(tmp_path)
    identity, runtime, repo = mod.verified_initial_inputs(
        config,
        settings=settings,
        image_record=image,
        source_archive=archive,
        source_record=record,
        environment=ci,
    )
    assert identity["digest"] == image["digest"] and identity["tag"] == "v1.0.0"
    assert runtime["LIGHTRAG_OBJECT_STORAGE"] == "s3" and repo == image["image"]


@pytest.mark.parametrize(
    "field,value",
    [
        ("confirm_maintenance", False),
        ("confirm_empty_target", False),
        ("expected_plan_hash", "a" * 64),
        ("job_wait_seconds", 1),
        ("startup_wait_seconds", False),
    ],
)
def test_initial_settings_refuse_unconfirmed_or_ambiguous_execution(
    tmp_path, field, value
):
    mod = module()
    config, image, archive, record, ci, _, settings = initial_inputs(tmp_path)
    settings[field] = value
    with pytest.raises(ValueError):
        mod.verified_initial_inputs(
            config,
            settings=settings,
            image_record=image,
            source_archive=archive,
            source_record=record,
            environment=ci,
        )


def test_initial_job_is_pinned_has_no_cloud_creds_and_separates_operator(tmp_path):
    mod = module()
    config, image, archive, record, ci, _, settings = initial_inputs(tmp_path)
    identity, runtime, repo = mod.verified_initial_inputs(
        config,
        settings=settings,
        image_record=image,
        source_archive=archive,
        source_record=record,
        environment=ci,
    )
    operator = dict(
        LIGHTRAG_STORAGE_MIGRATION_DSN="postgresql://operator:synthetic@pg.invalid:5432/prod?sslmode=require",
        LIGHTRAG_RELEASE_OPERATOR_DSN="postgresql://operator:synthetic@pg.invalid:5432/prod?sslmode=require",
        LIGHTRAG_GRAPH_MIGRATION_USERNAME="graph_operator",
        LIGHTRAG_GRAPH_MIGRATION_PASSWORD="synthetic",
    )
    resources = mod.render_initial_job(
        config, identity, runtime, settings, operator, image=repo, attempt="12"
    )
    job = resources[-1]
    pod = job["spec"]["template"]["spec"]
    container = pod["containers"][0]
    assert container["image"] == image["image_ref"] and job["spec"]["backoffLimit"] == 0
    assert (
        pod["automountServiceAccountToken"] is False and pod["restartPolicy"] == "Never"
    )
    assert "lightrag.provisioning.initial" in container["command"]
    assert "activeDeadlineSeconds" not in job["spec"]
    assert not any(
        "CLB" in k or "EDGEONE" in k for r in resources for k in r.get("stringData", {})
    )
    assert "operator" not in json.dumps(runtime)
    assert all(r["kind"] in {"Secret", "ConfigMap", "Job"} for r in resources)


def test_ordinary_upgrade_does_not_guess_first_deployment(tmp_path):
    from scripts.ci.prod_deploy import verified_inputs

    config = load(tmp_path, kubernetes_data())
    with pytest.raises(ValueError, match="[Ii]nitial|[Ff]irst"):
        verified_inputs(
            config,
            image_record={},
            source_archive=None,
            source_record={},
            environment={},
        )


def prepared_initial(tmp_path):
    mod = module()
    config, image, archive, record, ci, runtime, settings = initial_inputs(tmp_path)
    identity, runtime, repo = mod.verified_initial_inputs(
        config,
        settings=settings,
        image_record=image,
        source_archive=archive,
        source_record=record,
        environment=ci,
    )
    operator = dict(
        LIGHTRAG_STORAGE_MIGRATION_DSN="postgresql://operator:synthetic@pg.invalid:5432/prod?sslmode=require",
        LIGHTRAG_RELEASE_OPERATOR_DSN="postgresql://operator:synthetic@pg.invalid:5432/prod?sslmode=require",
        LIGHTRAG_GRAPH_MIGRATION_USERNAME="graph_operator",
        LIGHTRAG_GRAPH_MIGRATION_PASSWORD="synthetic",
    )
    resources = mod.render_initial_job(
        config, identity, runtime, settings, operator, image=repo, attempt="12"
    )
    return mod, config, identity, runtime, settings, repo, resources


class FakeKube:
    def __init__(
        self,
        config,
        identity,
        settings,
        image,
        *,
        failed=False,
        existing=False,
        wrong_image=False,
    ):
        self.config, self.identity, self.settings, self.image = (
            config,
            identity,
            settings,
            image,
        )
        self.failed, self.existing, self.wrong_image = failed, existing, wrong_image
        self.objects = {}
        self.created = []

    def get(self, kind, name, **kwargs):
        if kind == "Deployment" and self.existing:
            return {"metadata": {"name": name}, "spec": {}}
        value = self.objects.get((kind, name))
        if value and kind == "Job":
            value["metadata"]["uid"] = "job-uid"
            value["status"] = {"failed": 1} if self.failed else {"succeeded": 1}
        if value and kind == "Deployment":
            value["metadata"]["generation"] = 1
            value["status"] = dict(
                observedGeneration=1, updatedReplicas=4, availableReplicas=4
            )
        return value

    def create_immutable(self, resource, **kwargs):
        self.run("create", "-f", "-", body=resource)

    def run(self, *args, body=None, **kwargs):
        if args[0] == "create":
            self.created.append(body["kind"])
            self.objects[body["kind"], body["metadata"]["name"]] = body
            return ""
        assert args[:2] == ("get", "pods")
        selector = args[args.index("-l") + 1]
        if selector.startswith("job-name="):
            report = dict(
                phase="INITIALIZED",
                scope=self.identity["scope"],
                release_id=self.identity["release_id"],
                config_hash=self.identity["config_hash"],
                plan_hash=self.settings["expected_plan_hash"],
                settings_hash=__import__(
                    "lightrag_delivery.render", fromlist=["content_hash"]
                ).content_hash(self.settings),
                profile_hash=self.config.onboarding["profile_hash"],
                epoch=1,
            )
            return json.dumps(
                {
                    "items": [
                        {
                            "metadata": {
                                "ownerReferences": [{"kind": "Job", "uid": "job-uid"}]
                            },
                            "status": {
                                "containerStatuses": [
                                    {
                                        "state": {
                                            "terminated": {
                                                "exitCode": 0,
                                                "message": json.dumps(report),
                                            }
                                        }
                                    }
                                ]
                            },
                        }
                    ]
                }
            )
        if not self.objects.get(("Deployment", "lightrag-blue")):
            return json.dumps({"items": []})
        digest = "sha256:" + "0" * 64 if self.wrong_image else self.identity["digest"]
        return json.dumps(
            {
                "items": [
                    dict(
                        metadata={"uid": "pod-" + str(i)},
                        spec={"containers": [{"image": self.image + "@" + digest}]},
                        status={
                            "phase": "Running",
                            "conditions": [{"type": "Ready", "status": "True"}],
                            "containerStatuses": [
                                {"ready": True, "imageID": "containerd://" + digest}
                            ],
                        },
                    )
                    for i in range(4)
                ]
            }
        )


def test_initial_execution_deploys_in_order_without_any_cloud_calls(
    tmp_path, monkeypatch
):
    mod, config, identity, runtime, settings, image, resources = prepared_initial(
        tmp_path
    )
    monkeypatch.setattr(mod, "validate_initial_target", lambda *a: None)
    import lightrag_delivery.edgeone as eo

    monkeypatch.setattr(
        eo, "cloud_observer", lambda *a: pytest.fail("Cloud API constructed")
    )
    kube = FakeKube(config, identity, settings, image)
    result = mod.execute_initial(
        kube, config, identity, runtime, settings, resources, image=image
    )
    assert (
        result["phase"] == "KUBERNETES_READY"
        and result["external_entry_verified"] is False
    )
    assert (
        kube.created.index("Job")
        < kube.created.index("Deployment")
        < kube.created.index("Service")
        < kube.created.index("Ingress")
    )
    assert (
        "PersistentVolumeClaim" not in kube.created
        and "ClusterRole" not in kube.created
    )


@pytest.mark.parametrize("failure", ["job", "existing", "image"])
def test_initial_failure_stops_without_cloud_or_replacement(
    tmp_path, monkeypatch, failure
):
    mod, config, identity, runtime, settings, image, resources = prepared_initial(
        tmp_path
    )
    monkeypatch.setattr(mod, "validate_initial_target", lambda *a: None)
    kube = FakeKube(
        config,
        identity,
        settings,
        image,
        failed=failure == "job",
        existing=failure == "existing",
        wrong_image=failure == "image",
    )
    with pytest.raises(ValueError):
        mod.execute_initial(
            kube, config, identity, runtime, settings, resources, image=image
        )
    assert "Service" not in kube.created and "Ingress" not in kube.created
    if failure in {"existing", "job"}:
        assert "Deployment" not in kube.created
    if failure == "existing":
        assert not kube.created


def test_real_runner_routes_explicit_initial_settings_without_cloud(
    tmp_path, monkeypatch, capsys
):
    from scripts.ci import prod_deploy

    mod, config, identity, runtime, settings, _, _ = prepared_initial(tmp_path)
    _, image, archive, record, ci, _, _ = initial_inputs(tmp_path)
    for name, value in [("image", image), ("record", record), ("settings", settings)]:
        (tmp_path / (name + ".json")).write_text(json.dumps(value))
    operator = dict(
        LIGHTRAG_STORAGE_MIGRATION_DSN="postgresql://operator:synthetic@pg.invalid:5432/prod?sslmode=require",
        LIGHTRAG_RELEASE_OPERATOR_DSN="postgresql://operator:synthetic@pg.invalid:5432/prod?sslmode=require",
        LIGHTRAG_GRAPH_MIGRATION_USERNAME="graph_operator",
        LIGHTRAG_GRAPH_MIGRATION_PASSWORD="synthetic",
    )
    for key, value in ci.items():
        monkeypatch.setenv(key, value)
    monkeypatch.setenv("LIGHTRAG_PROD_OPERATOR_JSON", json.dumps(operator))
    result = prod_deploy.main(
        [
            "--config",
            str(tmp_path / "prod.json"),
            "--image-record",
            str(tmp_path / "image.json"),
            "--source-archive",
            str(archive),
            "--source-record",
            str(tmp_path / "record.json"),
            "--initial-settings",
            str(tmp_path / "settings.json"),
        ]
    )
    assert result == 0
    output = capsys.readouterr()
    assert (
        '"planned": true' in output.out and "synthetic" not in output.out + output.err
    )


def test_initial_ci_planning_needs_only_the_standard_library(tmp_path):
    import subprocess
    import sys

    _, _, _, runtime, _, _, _ = prepared_initial(tmp_path)
    payload = dict(
        environment=runtime,
        operator=dict(
            LIGHTRAG_STORAGE_MIGRATION_DSN="postgresql://operator:synthetic@pg.invalid:5432/prod?sslmode=require",
            LIGHTRAG_RELEASE_OPERATOR_DSN="postgresql://operator:synthetic@pg.invalid:5432/prod?sslmode=require",
            LIGHTRAG_GRAPH_MIGRATION_USERNAME="graph_operator",
            LIGHTRAG_GRAPH_MIGRATION_PASSWORD="synthetic",
        ),
    )
    path = tmp_path / "payload.json"
    path.write_text(json.dumps(payload))
    code = "import json,sys; from lightrag.provisioning.initial import validate_operator; p=json.load(open(sys.argv[1])); validate_operator(p['environment'],p['operator'],confirm_shared=False)"
    result = subprocess.run(
        [sys.executable, "-S", "-c", code, str(path)], capture_output=True, text=True
    )
    assert result.returncode == 0, result.stderr


def test_initial_runtime_preserves_explicit_pg_transport_and_cos_region(tmp_path):
    from tests.ci.test_prod_inputs import inputs
    from lightrag_delivery.environment import runtime_environment, SECRET_ENV

    config, _, _, _, ci = inputs(tmp_path)
    data = asdict(config)
    data["runtime"].update(POSTGRES_SSL_MODE="disable", S3_REGION="ap-shanghai")
    config = load(tmp_path, data)
    secrets = {k: ci["LIGHTRAG_PROD_" + k.upper()] for k in SECRET_ENV}
    values, _ = runtime_environment(config, secrets)
    assert values["S3_REGION"] == "ap-shanghai"
    assert values["POSTGRES_SSL_MODE"] == "disable"
    assert values["LIGHTRAG_COORDINATION_DSN"].endswith("?sslmode=disable")


def test_schema_accepts_only_explicit_kubernetes_boundary(tmp_path):
    jsonschema = pytest.importorskip(
        "jsonschema", reason="optional JSON Schema tooling"
    )
    from pathlib import Path

    schema = json.loads(
        Path(
            "k8s-deploy/lightrag-kustomize/overlays/prod/onboarding.schema.json"
        ).read_text()
    )
    data = kubernetes_data()
    jsonschema.validate(data, schema)
    data["network"] = {"ingress_cidrs": [], "egress": []}
    jsonschema.validate(data, schema)
    data["network"]["ingress_cidrs"] = ["10.0.0.0/8"]
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(data, schema)
    legacy = config_data()
    legacy["network"] = {"ingress_cidrs": [], "egress": []}
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(legacy, schema)
    legacy = config_data()
    legacy["isolation"]["acl_verified"] = False
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(legacy, schema)


def test_kubernetes_profile_can_leave_existing_namespace_network_policy_unchanged(
    tmp_path,
):
    from lightrag_delivery.render import render_foundation

    data = kubernetes_data()
    data["network"] = {"ingress_cidrs": [], "egress": []}
    config = load(tmp_path, data)
    assert not any(r["kind"] == "NetworkPolicy" for r in render_foundation(config))


def test_initial_job_identity_is_stable_across_pipeline_attempts(tmp_path):
    mod, config, identity, runtime, settings, image, first = prepared_initial(tmp_path)
    operator = next(
        r["stringData"]
        for r in first
        if r["kind"] == "Secret"
        and "LIGHTRAG_RELEASE_OPERATOR_DSN" in r.get("stringData", {})
    )
    second = mod.render_initial_job(
        config, identity, runtime, settings, operator, image=image, attempt="13"
    )
    assert first[-1]["metadata"]["name"] == second[-1]["metadata"]["name"]
    assert first[0]["metadata"]["name"] == second[0]["metadata"]["name"]


def test_initial_scope_claim_refuses_changed_settings_before_a_second_job(
    tmp_path, monkeypatch
):
    mod, config, identity, runtime, settings, image, first = prepared_initial(tmp_path)
    monkeypatch.setattr(mod, "validate_initial_target", lambda *a: None)
    kube = FakeKube(config, identity, settings, image, failed=True)
    with pytest.raises(ValueError):
        mod.execute_initial(
            kube, config, identity, runtime, settings, first, image=image
        )
    changed = dict(settings, approval_id="different-execution")
    operator = next(
        r["stringData"]
        for r in first
        if r["kind"] == "Secret"
        and "LIGHTRAG_RELEASE_OPERATOR_DSN" in r.get("stringData", {})
    )
    second = mod.render_initial_job(
        config, identity, runtime, changed, operator, image=image, attempt="13"
    )
    with pytest.raises(ValueError):
        mod.execute_initial(
            kube, config, identity, runtime, changed, second, image=image
        )
    assert kube.created.count("Job") == 1


def test_initial_scope_claim_never_recreates_a_missing_unknown_job(
    tmp_path, monkeypatch
):
    mod, config, identity, runtime, settings, image, resources = prepared_initial(
        tmp_path
    )
    monkeypatch.setattr(mod, "validate_initial_target", lambda *a: None)
    kube = FakeKube(config, identity, settings, image, failed=True)
    with pytest.raises(ValueError):
        mod.execute_initial(
            kube, config, identity, runtime, settings, resources, image=image
        )
    del kube.objects["Job", resources[-1]["metadata"]["name"]]
    with pytest.raises(ValueError):
        mod.execute_initial(
            kube, config, identity, runtime, settings, resources, image=image
        )
    assert kube.created.count("Job") == 1


def test_success_returned_after_initial_observation_deadline_does_not_deploy(
    tmp_path, monkeypatch
):
    mod, config, identity, runtime, settings, image, resources = prepared_initial(
        tmp_path
    )
    monkeypatch.setattr(mod, "validate_initial_target", lambda *a: None)
    kube = FakeKube(config, identity, settings, image)
    clock = [0.0]
    monkeypatch.setattr(mod.time, "monotonic", lambda: clock[0])
    original = kube.get

    def get(kind, name, **kwargs):
        result = original(kind, name)
        if kind == "Job" and result:
            clock[0] += settings["job_wait_seconds"] + 1
        return result

    kube.get = get
    with pytest.raises(ValueError):
        mod.execute_initial(
            kube, config, identity, runtime, settings, resources, image=image
        )
    assert "Deployment" not in kube.created


def test_kubectl_applies_request_and_subprocess_timeouts(monkeypatch):
    from scripts.ci.prod_deploy import Kubectl
    from types import SimpleNamespace
    import subprocess

    observed = []

    def run(args, **kwargs):
        observed.append((args, kwargs))
        return SimpleNamespace(returncode=0, stdout="{}")

    monkeypatch.setattr(subprocess, "run", run)
    Kubectl("explicit-config", "prod").get("Job", "first", timeout=2)
    assert observed[0][1]["timeout"] <= 2
    assert any(
        x.startswith("--request-timeout=") and x != "--request-timeout=0s"
        for x in observed[0][0]
    )


def test_immutable_creation_does_not_write_after_its_read_budget(tmp_path, monkeypatch):
    from scripts.ci.prod_deploy import Kubectl
    from unittest.mock import Mock
    import scripts.ci.prod_deploy as runner

    clock = [0.0]
    monkeypatch.setattr(runner.time, "monotonic", lambda: clock[0])
    kube = Kubectl("explicit-config", "prod")

    def get(*args, **kwargs):
        clock[0] = 3
        return None

    monkeypatch.setattr(kube, "get", get)
    write = Mock()
    monkeypatch.setattr(kube, "run", write)
    with pytest.raises(ValueError):
        kube.create_immutable(
            {"kind": "ConfigMap", "metadata": {"name": "initial"}}, timeout=2
        )
    assert write.call_count == 0


def test_success_returned_after_startup_deadline_does_not_publish_entrypoint(
    tmp_path, monkeypatch
):
    mod, config, identity, runtime, settings, image, resources = prepared_initial(
        tmp_path
    )
    monkeypatch.setattr(mod, "validate_initial_target", lambda *a: None)
    kube = FakeKube(config, identity, settings, image)
    clock = [0.0]
    monkeypatch.setattr(mod.time, "monotonic", lambda: clock[0])
    original = kube.get

    def get(kind, name, **kwargs):
        value = original(kind, name)
        if kind == "Deployment" and value:
            clock[0] += settings["startup_wait_seconds"] + 1
        return value

    kube.get = get
    with pytest.raises(ValueError):
        mod.execute_initial(
            kube, config, identity, runtime, settings, resources, image=image
        )
    assert "Service" not in kube.created and "Ingress" not in kube.created


@pytest.mark.parametrize("failure", ["timeout", "forbidden"])
def test_kubernetes_failure_reports_safe_operation_without_credentials(
    monkeypatch, capsys, failure
):
    import subprocess
    from types import SimpleNamespace
    from scripts.ci.prod_deploy import Kubectl

    secret_value = "synthetic-private-body-do-not-log"

    def run(args, **kwargs):
        if failure == "timeout":
            raise subprocess.TimeoutExpired(
                args, 3, output=secret_value, stderr=secret_value
            )
        return SimpleNamespace(
            returncode=1,
            stdout=secret_value,
            stderr="Error from server (Forbidden): " + secret_value,
        )

    monkeypatch.setattr(subprocess, "run", run)
    with pytest.raises(ValueError):
        Kubectl("explicit-config", "prod").run(
            "create",
            "-f",
            "-",
            body={"kind": "Secret", "stringData": {"password": secret_value}},
            timeout=3,
        )
    captured = capsys.readouterr()
    assert secret_value not in captured.out + captured.err
    report = json.loads(captured.err)
    assert report["event"] == "kubernetes_request_failed"
    assert report["operation"] == "create"
    assert report["resource_kind"] == "Secret"
    assert report["reason"] == (
        "CLIENT_TIMEOUT" if failure == "timeout" else "Forbidden"
    )
    assert report["remote_outcome"] == "unknown"
    assert report["timeout_seconds"] == 3
