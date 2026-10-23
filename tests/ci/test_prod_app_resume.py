"""Application-only resume consumes proven success without repeating initialization."""

import base64
from copy import deepcopy
import importlib.util
import json
from pathlib import Path

import pytest

from lightrag_delivery.render import content_hash, render_slot
from tests.ci.test_prod_initial import prepared_initial, FakeKube


def module():
    assert importlib.util.find_spec("scripts.ci.prod_app_resume"), (
        "Application-only resume is missing"
    )
    from scripts.ci import prod_app_resume

    return prod_app_resume


def fixture(tmp_path, monkeypatch):
    mod = module()
    initial, config, identity, runtime, settings, image, resources = prepared_initial(
        tmp_path
    )
    monkeypatch.setattr(mod, "validate_cluster_identity", lambda *a: None)
    kube = FakeKube(config, identity, settings, image)
    for obj in resources:
        obj = deepcopy(obj)
        if obj["kind"] == "Secret":
            obj["data"] = {
                k: base64.b64encode(v.encode()).decode()
                for k, v in obj.pop("stringData").items()
            }
        kube.objects[obj["kind"], obj["metadata"]["name"]] = obj
    job = kube.get("Job", resources[-1]["metadata"]["name"])
    pods = json.loads(
        kube.run(
            "get", "pods", "-l", "job-name=" + job["metadata"]["name"], "-o", "json"
        )
    )
    pod = pods["items"][0]
    pod["metadata"]["uid"] = "original-pod"
    pod["spec"] = deepcopy(job["spec"]["template"]["spec"])
    pod["status"]["containerStatuses"][0].update(
        ready=False, restartCount=0, imageID=image + "@" + identity["digest"]
    )
    approval = dict(
        version=1,
        approval_id="user-approved-app-only",
        source_pipeline=76,
        namespace=config.target["namespace"],
        configmap=resources[1]["metadata"]["name"],
        initial_job=job["metadata"]["name"],
        initial_job_uid="job-uid",
        initial_job_spec_hash=content_hash(job["spec"]),
        initial_pod_uid="original-pod",
        onboarding_hash=config.config_hash,
        settings_hash=content_hash(settings),
        identity=identity,
        image=image,
        request_timeout_seconds=60,
    )
    slot = render_slot(config, identity, "blue", image=image, environment=runtime)
    kube.objects["Secret", slot[0]["metadata"]["name"]] = dict(
        slot[0],
        data={k: base64.b64encode(v.encode()).decode() for k, v in runtime.items()},
    )
    kube.objects["ServiceAccount", config.onboarding["application_service_account"]] = {
        "automountServiceAccountToken": False
    }
    kube.objects["Secret", config.onboarding["image_pull_secret"]] = {
        "type": "kubernetes.io/dockerconfigjson"
    }
    original_get = kube.get

    def get(kind, name, **kwargs):
        if kind == "Job":
            return kube.objects.get((kind, name))
        if kind == "ReplicaSet" and name == "blue-rs":
            return {
                "metadata": {
                    "uid": "blue-rs-uid",
                    "ownerReferences": [
                        {
                            "kind": "Deployment",
                            "uid": "blue-deployment-uid",
                            "controller": True,
                        }
                    ],
                }
            }
        value = original_get(kind, name, **kwargs)
        if kind == "Deployment" and value is not None:
            value["metadata"]["uid"] = "blue-deployment-uid"
        return value

    kube.get = get
    original_run = kube.run

    def run(*args, body=None, **kwargs):
        if args[:2] == ("get", "pods") and "job-name=" in args[args.index("-l") + 1]:
            return json.dumps(pods)
        if args[0] == "create":
            assert body["kind"] in {"ConfigMap", "Deployment", "Service", "Ingress"}, (
                "Forbidden initialization/credential write"
            )
        result = original_run(*args, body=body, **kwargs)
        if args[:2] == ("get", "pods") and kube.objects.get(
            ("Deployment", "lightrag-blue")
        ):
            data = json.loads(result)
            for pod in data["items"]:
                original_image = pod["spec"]["containers"][0]["image"]
                pod["spec"] = deepcopy(
                    kube.objects["Deployment", "lightrag-blue"]["spec"]["template"][
                        "spec"
                    ]
                )
                pod["spec"]["containers"][0]["image"] = original_image
                pod["metadata"]["ownerReferences"] = [
                    {
                        "kind": "ReplicaSet",
                        "name": "blue-rs",
                        "uid": "blue-rs-uid",
                        "controller": True,
                    }
                ]
            result = json.dumps(data)
        return result

    kube.run = run
    from types import MethodType
    from scripts.ci.prod_deploy import Kubectl

    kube.create_immutable = MethodType(Kubectl.create_immutable, kube)
    return mod, kube, approval, job, pods, config, identity, runtime, settings, slot


def test_resume_creates_only_missing_application_resources(tmp_path, monkeypatch):
    mod, kube, a, job, pods, c, i, e, s, slot = fixture(tmp_path, monkeypatch)
    original = deepcopy(job)
    result = mod.execute_resume(kube, a, confirm=a["approval_id"])
    assert result["phase"] == "KUBERNETES_READY" and result["replicas"] == 4
    assert result["identity"] == i and result["source_pipeline"] == 76
    assert kube.created == ["ConfigMap", "Deployment", "Service", "Ingress"]
    assert job == original
    assert (
        result["external_entry_verified"] is False
        and result["capacity_accepted"] is False
    )
    assert not kube.objects["Ingress", "lightrag"]["spec"].get("tls")


@pytest.mark.parametrize(
    "change",
    [
        "job_uid",
        "job_spec",
        "pod_uid",
        "pod_owner",
        "pod_image",
        "pod_restarted",
        "receipt",
        "claim",
        "runtime",
        "config",
        "settings",
        "app_secret",
        "green",
        "foreign_deployment",
    ],
)
def test_resume_drift_stops_before_writes(tmp_path, monkeypatch, change):
    mod, kube, a, job, pods, c, i, e, s, slot = fixture(tmp_path, monkeypatch)
    pod = pods["items"][0]
    if change == "job_uid":
        job["metadata"]["uid"] = "different"
    elif change == "job_spec":
        job["spec"]["backoffLimit"] = 2
    elif change == "pod_uid":
        pod["metadata"]["uid"] = "different"
    elif change == "pod_owner":
        pod["metadata"]["ownerReferences"][0]["uid"] = "different"
    elif change == "pod_image":
        pod["status"]["containerStatuses"][0]["imageID"] = (
            "registry.invalid/wrong@sha256:" + "0" * 64
        )
    elif change == "pod_restarted":
        pod["status"]["containerStatuses"][0]["restartCount"] = 1
    elif change == "receipt":
        pod["status"]["containerStatuses"][0]["state"]["terminated"]["message"] = "{}"
    elif change == "claim":
        next(
            o
            for (k, n), o in kube.objects.items()
            if k == "ConfigMap" and n.endswith("-execution")
        )["data"]["binding"] = "different"
    elif change == "runtime":
        next(
            o
            for (k, n), o in kube.objects.items()
            if k == "Secret" and n.endswith("-runtime")
        )["data"]["environment.json"] = base64.b64encode(b"{}").decode()
    elif change == "config":
        kube.objects["ConfigMap", a["configmap"]]["immutable"] = False
    elif change == "settings":
        kube.objects["ConfigMap", a["configmap"]]["data"]["settings.json"] = "{}"
    elif change == "app_secret":
        kube.objects["Secret", slot[0]["metadata"]["name"]]["data"]["WORKSPACE"] = (
            base64.b64encode(b"different").decode()
        )
    elif change == "green":
        kube.objects["Deployment", "lightrag-green"] = {"metadata": {}, "spec": {}}
    elif change == "foreign_deployment":
        kube.existing = True
    with pytest.raises(ValueError):
        mod.execute_resume(kube, a, confirm=a["approval_id"])
    assert kube.created == []


def test_missing_confirmation_does_not_contact_cluster(tmp_path, monkeypatch):
    mod, kube, a, *_ = fixture(tmp_path, monkeypatch)
    monkeypatch.setattr(
        kube, "get", lambda *a, **k: pytest.fail("unapproved cluster access")
    )
    with pytest.raises(ValueError):
        mod.execute_resume(kube, a, confirm="")


def test_unknown_ack_stops_without_cleanup_or_initialization(tmp_path, monkeypatch):
    mod, kube, a, *_ = fixture(tmp_path, monkeypatch)
    original = kube.run

    def run(*args, body=None, **kwargs):
        if args[0] == "create":
            original(*args, body=body, **kwargs)
            raise ValueError("unknown acknowledgement")
        return original(*args, body=body, **kwargs)

    kube.run = run
    with pytest.raises(ValueError):
        mod.execute_resume(kube, a, confirm=a["approval_id"])
    assert kube.created == ["ConfigMap"]
    assert not any(k == "Deployment" for k, n in kube.objects)


def test_resume_workflow_is_manual_only_and_does_not_build_or_migrate():
    import yaml

    path = Path(".woodpecker/resume-prod-app.yml")
    assert path.is_file(), "Resume workflow missing"
    data = yaml.safe_load(path.read_text())
    assert data["when"] == [{"event": "manual", "branch": "master"}]
    assert not data.get("depends_on")
    assert list(data["steps"]) == ["resume-prod-app"]
    step = data["steps"]["resume-prod-app"]
    assert step["environment"]["LIGHTRAG_PROD_KUBECONFIG"] == {
        "from_secret": "lightrag_prod_kubeconfig"
    }
    assert set(step["environment"]) == {"LIGHTRAG_PROD_KUBECONFIG"}
    assert "kaniko" not in path.read_text().lower()


def test_matching_existing_resources_are_observed_without_replacement(
    tmp_path, monkeypatch
):
    mod, kube, a, *_ = fixture(tmp_path, monkeypatch)
    first = mod.execute_resume(kube, a, confirm=a["approval_id"])
    created = list(kube.created)
    second = mod.execute_resume(kube, a, confirm=a["approval_id"])
    assert second["identity"] == first["identity"] and kube.created == created


@pytest.mark.parametrize(
    "event,branch", [("tag", "master"), ("manual", "main"), ("push", "master")]
)
def test_cli_refuses_wrong_event_or_branch_before_cluster_access(
    tmp_path, monkeypatch, event, branch
):
    mod = module()
    monkeypatch.setenv("CI_PIPELINE_EVENT", event)
    monkeypatch.setenv("CI_COMMIT_BRANCH", branch)
    monkeypatch.setenv("CI_REPO", "LFunTech/LightRAG")
    monkeypatch.setenv("CI_COMMIT_SHA", "a" * 40)
    monkeypatch.setattr(
        mod, "Kubectl", lambda *a: pytest.fail("unapproved cluster access")
    )
    assert mod.main(["--kubeconfig", "unused", "--approval", "unused"]) == 2


@pytest.mark.parametrize("field", ["restart", "image_pull", "crash"])
def test_resume_pod_failure_stops_before_entrypoint(tmp_path, monkeypatch, field):
    mod, kube, a, *_ = fixture(tmp_path, monkeypatch)
    original = kube.run

    def run(*args, body=None, **kwargs):
        value = original(*args, body=body, **kwargs)
        if (
            args[:2] == ("get", "pods")
            and "job-name=" not in args[args.index("-l") + 1]
            and kube.objects.get(("Deployment", "lightrag-blue"))
        ):
            data = json.loads(value)
            item = data["items"][0]["status"]["containerStatuses"][0]
            if field == "restart":
                item["restartCount"] = 1
            elif field == "image_pull":
                item["state"] = {"waiting": {"reason": "ImagePullBackOff"}}
            else:
                item["state"] = {"terminated": {"exitCode": 2}}
            return json.dumps(data)
        return value

    kube.run = run
    with pytest.raises(ValueError, match="Pod failed"):
        mod.execute_resume(kube, a, confirm=a["approval_id"])
    assert "Service" not in kube.created and "Ingress" not in kube.created


def test_changed_existing_ingress_binding_is_not_accepted(tmp_path, monkeypatch):
    mod, kube, a, job, pods, c, *_ = fixture(tmp_path, monkeypatch)
    from lightrag_delivery.render import render_entrypoint

    ingress = deepcopy(render_entrypoint(c, "blue")[1])
    key = next(iter(ingress["metadata"]["annotations"]))
    ingress["metadata"]["annotations"][key] = "different"
    kube.objects["Ingress", "lightrag"] = ingress
    with pytest.raises(ValueError):
        mod.execute_resume(kube, a, confirm=a["approval_id"])
    assert kube.created == []


def test_manual_cli_reports_executor_separately_from_original_image_commit(
    tmp_path, monkeypatch, capsys
):
    mod, kube, a, *_ = fixture(tmp_path, monkeypatch)
    for key, value in dict(
        CI_PIPELINE_EVENT="manual",
        CI_COMMIT_BRANCH="master",
        CI_REPO="LFunTech/LightRAG",
        CI_COMMIT_SHA="c" * 40,
        LIGHTRAG_PROD_APP_RESUME_CONFIRM=a["approval_id"],
    ).items():
        monkeypatch.setenv(key, value)
    path = tmp_path / "approval.json"
    path.write_text(json.dumps(a))
    monkeypatch.setattr(mod, "Kubectl", lambda *a: kube)
    assert mod.main(["--kubeconfig", "existing", "--approval", str(path)]) == 0
    result = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert result["executor_commit"] == "c" * 40
    assert result["identity"]["commit"] == a["identity"]["commit"]
    assert result["identity"]["digest"] == a["identity"]["digest"]
    assert kube.created == ["ConfigMap", "Deployment", "Service", "Ingress"]


@pytest.mark.parametrize("extra", ["args", "livenessProbe", "envFrom", "hostNetwork"])
def test_existing_deployment_extra_behavior_is_not_adopted(
    tmp_path, monkeypatch, extra
):
    mod, kube, a, job, pods, c, i, e, s, slot = fixture(tmp_path, monkeypatch)
    deployment = deepcopy(slot[2])
    pod = deployment["spec"]["template"]["spec"]
    if extra == "args":
        pod["containers"][0]["args"] = ["--workers", "2"]
    elif extra == "livenessProbe":
        pod["containers"][0]["livenessProbe"] = {
            "httpGet": {"path": "/health", "port": "http"}
        }
    elif extra == "envFrom":
        pod["initContainers"][0]["envFrom"] = [{"secretRef": {"name": "other"}}]
    else:
        pod["hostNetwork"] = True
    kube.objects["Deployment", "lightrag-blue"] = deployment
    with pytest.raises(ValueError):
        mod.execute_resume(kube, a, confirm=a["approval_id"])
    assert kube.created == []


@pytest.mark.parametrize(
    "drift", ["owner_missing", "rs_owner", "pod_runtime", "pod_extra_args"]
)
def test_foreign_or_changed_app_pod_cannot_be_ready_evidence(
    tmp_path, monkeypatch, drift
):
    mod, kube, a, *_ = fixture(tmp_path, monkeypatch)
    original = kube.run

    def run(*args, body=None, **kwargs):
        result = original(*args, body=body, **kwargs)
        if (
            args[:2] == ("get", "pods")
            and "job-name=" not in args[args.index("-l") + 1]
            and kube.objects.get(("Deployment", "lightrag-blue"))
        ):
            data = json.loads(result)
            pod = data["items"][0]
            if drift == "owner_missing":
                pod["metadata"]["ownerReferences"] = []
            elif drift == "rs_owner":
                pod["metadata"]["ownerReferences"] = [
                    {
                        "kind": "ReplicaSet",
                        "name": "foreign",
                        "uid": "foreign-uid",
                        "controller": True,
                    }
                ]
            elif drift == "pod_runtime":
                pod["spec"]["containers"][0]["envFrom"] = [
                    {"secretRef": {"name": "wrong-runtime"}}
                ]
            else:
                pod["spec"]["containers"][0]["args"] = ["--workers", "2"]
            return json.dumps(data)
        return result

    kube.run = run
    with pytest.raises(ValueError):
        mod.execute_resume(kube, a, confirm=a["approval_id"])
    assert "Service" not in kube.created and "Ingress" not in kube.created


def test_standard_kubernetes_defaults_do_not_count_as_behavior_drift(
    tmp_path, monkeypatch
):
    mod, kube, a, job, pods, c, i, e, s, slot = fixture(tmp_path, monkeypatch)
    deployment = deepcopy(slot[2])
    deployment["spec"].update(progressDeadlineSeconds=600, revisionHistoryLimit=10)
    tpl = deployment["spec"]["template"]
    tpl["metadata"]["creationTimestamp"] = None
    spec = tpl["spec"]
    spec.update(
        restartPolicy="Always",
        dnsPolicy="ClusterFirst",
        schedulerName="default-scheduler",
        enableServiceLinks=True,
        serviceAccount=spec["serviceAccountName"],
    )
    for container in spec["containers"] + spec["initContainers"]:
        container.setdefault("imagePullPolicy", "IfNotPresent")
        container.setdefault("terminationMessagePath", "/dev/termination-log")
        container.setdefault("terminationMessagePolicy", "File")
        for port in container.get("ports", []):
            port.setdefault("protocol", "TCP")
        for name in ["startupProbe", "readinessProbe"]:
            if name in container:
                container[name].setdefault("timeoutSeconds", 1)
                container[name].setdefault("initialDelaySeconds", 0)
                container[name].setdefault("successThreshold", 1)
                container[name]["httpGet"].setdefault("scheme", "HTTP")
    for volume in spec["volumes"]:
        if "configMap" in volume:
            volume["configMap"]["defaultMode"] = 420
    kube.objects["Deployment", "lightrag-blue"] = deployment
    result = mod.execute_resume(kube, a, confirm=a["approval_id"])
    assert result["phase"] == "KUBERNETES_READY" and "Deployment" not in kube.created
    from scripts.ci.prod_app_contract import pod_spec

    bound = deepcopy(spec)
    bound.update(
        nodeName="node-1",
        priority=0,
        preemptionPolicy="PreemptLowerPriority",
        tolerations=[
            dict(
                effect="NoExecute",
                key="node.kubernetes.io/not-ready",
                operator="Exists",
                tolerationSeconds=300,
            )
        ],
    )
    assert pod_spec(bound, bound=True) == pod_spec(slot[2]["spec"]["template"]["spec"])


def test_creation_recheck_rejects_a_newly_drifted_deployment(tmp_path, monkeypatch):
    mod, kube, a, job, pods, c, i, e, s, slot = fixture(tmp_path, monkeypatch)
    original = kube.get
    reads = [0]

    def get(kind, name, **kwargs):
        if kind == "Deployment" and name == "lightrag-blue":
            reads[0] += 1
            if reads[0] == 3:
                drift = deepcopy(slot[2])
                drift["spec"]["template"]["spec"]["containers"][0]["args"] = [
                    "--workers",
                    "2",
                ]
                return drift
        return original(kind, name, **kwargs)

    kube.get = get
    with pytest.raises(ValueError):
        mod.execute_resume(kube, a, confirm=a["approval_id"])
    assert kube.created == ["ConfigMap"]


def test_observed_tke_defaults_preserve_the_approved_pod_runtime(tmp_path, monkeypatch):
    mod, kube, a, job, pods, c, i, e, s, slot = fixture(tmp_path, monkeypatch)
    from scripts.ci.prod_app_contract import application_matches, pod_spec

    observed = deepcopy(slot[2])
    spec = observed["spec"]["template"]["spec"]
    for variable in spec["containers"][0]["env"]:
        if "fieldRef" in variable.get("valueFrom", {}):
            variable["valueFrom"]["fieldRef"]["apiVersion"] = "v1"
    assert application_matches(observed, slot[2])
    bound = deepcopy(spec)
    for kind in ["requests", "limits"]:
        bound["containers"][0]["resources"][kind]["tke.cloud.tencent.com/eni-ip"] = "1"
    assert pod_spec(bound, bound=True) == pod_spec(slot[2]["spec"]["template"]["spec"])
    bound["containers"][0]["resources"]["limits"]["tke.cloud.tencent.com/eni-ip"] = "2"
    assert pod_spec(bound, bound=True) != pod_spec(slot[2]["spec"]["template"]["spec"])


def test_cli_reports_safe_validation_reason_but_not_arbitrary_exception(
    tmp_path, monkeypatch, capsys
):
    mod = module()
    from lightrag_delivery.config import ProdConfigError

    a = json.loads(
        Path(
            "k8s-deploy/lightrag-kustomize/overlays/prod/app-resume-76.json"
        ).read_text()
    )
    for key, value in dict(
        CI_PIPELINE_EVENT="manual",
        CI_COMMIT_BRANCH="master",
        CI_REPO="LFunTech/LightRAG",
        CI_COMMIT_SHA="c" * 40,
        LIGHTRAG_PROD_APP_RESUME_CONFIRM=a["approval_id"],
    ).items():
        monkeypatch.setenv(key, value)
    path = tmp_path / "approval.json"
    path.write_text(json.dumps(a))
    monkeypatch.setattr(mod, "Kubectl", lambda *a: object())
    for error, expected in [
        (
            ProdConfigError("Observed application Deployment differs"),
            "Observed application Deployment differs",
        ),
        (RuntimeError("synthetic-private-error-do-not-log"), "RuntimeError"),
    ]:

        def execute(*args, **kwargs):
            raise error

        monkeypatch.setattr(mod, "execute_resume", execute)
        assert mod.main(["--kubeconfig", "unused", "--approval", str(path)]) == 2
        output = capsys.readouterr().err
        assert expected in output and "synthetic-private-error-do-not-log" not in output
