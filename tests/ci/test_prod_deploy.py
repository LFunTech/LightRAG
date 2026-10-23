import copy
import pytest
from tests.ci.test_prod_config import config_data, load


def test_prod_requires_qcloud_specific_propagation_contract(tmp_path):
    from scripts.ci.prod_config import ProdConfigError

    data = config_data()
    data["target"]["ingress_class"] = "nginx"
    with pytest.raises(ProdConfigError):
        load(tmp_path, data)


def test_renderer_has_disjoint_slots_pinned_immutable_configuration(tmp_path):
    from lightrag_delivery.render import render_slot, render_entrypoint

    config = load(tmp_path, config_data())
    identity = dict(
        scope="lightrag_prod/lightrag_prod",
        release_id="r1",
        tag="v1.0.0",
        commit="a" * 40,
        digest="sha256:" + "b" * 64,
        config_hash="c" * 64,
    )
    env = {"LIGHTRAG_API_KEY": "synthetic-private"}
    blue = render_slot(
        config, identity, "blue", image="registry.invalid/rag", environment=env
    )
    green = render_slot(
        config, identity, "green", image="registry.invalid/rag", environment=env
    )
    for resources, slot in [(blue, "blue"), (green, "green")]:
        deploy = next(item for item in resources if item["kind"] == "Deployment")
        assert deploy["spec"]["replicas"] == 4
        pod = deploy["spec"]["template"]["spec"]
        assert pod["containers"][0]["image"].endswith("@" + identity["digest"])
        values = {v["name"]: v.get("value") for v in pod["containers"][0]["env"]}
        assert values["WORKERS"] == "1"
        assert pod["containers"][0]["readinessProbe"]["httpGet"]["path"] == "/ready"
        assert deploy["spec"]["selector"]["matchLabels"]["lightrag.io/slot"] == slot
        assert not pod["automountServiceAccountToken"]
        assert next(r for r in resources if r["kind"] == "Secret")["immutable"]
    assert copy.deepcopy(blue) == blue
    entry = render_entrypoint(config, "blue")
    ingress = next(r for r in entry if r["kind"] == "Ingress")
    assert ingress["spec"]["ingressClassName"] == "qcloud"
    assert not any("nginx" in k for k in ingress["metadata"]["annotations"])


def test_prod_selection_is_formal_tag_and_explicitly_activated_only():
    from lightrag_delivery.render import select_prod_release

    for event, tag, active in [
        ("push", "v1.0.0", True),
        ("tag", "v1.0.0-test", True),
        ("tag", "v1.0.0-pre", True),
        ("tag", "v1.0.0", False),
    ]:
        assert not select_prod_release(event=event, tag=tag, activated=active)
    assert select_prod_release(event="tag", tag="v1.0.0", activated=True)


def test_foundation_gives_control_only_exact_namespace_identity_reads(tmp_path):
    from lightrag_delivery.render import render_foundation

    config = load(tmp_path, config_data())
    resources = render_foundation(config)
    role = next(r for r in resources if r["kind"] == "ClusterRole")
    assert role["rules"] == [
        {
            "apiGroups": [""],
            "resources": ["namespaces"],
            "resourceNames": [config.target["namespace"], "kube-system"],
            "verbs": ["get"],
        }
    ]
    binding = next(r for r in resources if r["kind"] == "ClusterRoleBinding")
    assert binding["subjects"] == [
        {
            "kind": "ServiceAccount",
            "name": config.onboarding["control_service_account"],
            "namespace": config.target["namespace"],
        }
    ]
    assert "namespace" not in role["metadata"]


def test_reusing_a_slot_with_same_config_has_a_new_immutable_identity_configmap(
    tmp_path,
):
    from lightrag_delivery.render import render_slot

    config = load(tmp_path, config_data())
    identity = dict(
        scope="lightrag_prod/lightrag_prod",
        release_id="r1",
        tag="v1.0.0",
        commit="a" * 40,
        digest="sha256:" + "b" * 64,
        config_hash="c" * 64,
    )
    first = render_slot(
        config, identity, "green", image="registry.invalid/rag", environment={}
    )
    second = render_slot(
        config,
        dict(identity, release_id="r2", tag="v1.0.2", digest="sha256:" + "d" * 64),
        "green",
        image="registry.invalid/rag",
        environment={},
    )
    first_map = next(v for v in first if v["kind"] == "ConfigMap")
    second_map = next(v for v in second if v["kind"] == "ConfigMap")
    assert first_map["metadata"]["name"] != second_map["metadata"]["name"]
