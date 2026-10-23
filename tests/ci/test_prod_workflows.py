from pathlib import Path
import json
import hashlib
import yaml

ROOT = Path(__file__).resolve().parents[2]


def test_prod_workflow_is_statically_disabled_and_has_no_test_credentials():
    workflow = yaml.safe_load((ROOT / ".woodpecker/deploy-prod.yml").read_text())
    assert (
        workflow["when"][0]["event"] == "tag"
        and workflow["when"][0]["evaluate"] == "false"
    )
    assert set(workflow["when"][0]["ref"]["exclude"]) == {
        "refs/tags/*-test",
        "refs/tags/*-pre",
    }
    assert workflow["depends_on"] == ["pre-deploy"] and workflow["skip_clone"]
    secrets = [
        value["from_secret"]
        for value in workflow["steps"]["deploy-prod"]["environment"].values()
        if isinstance(value, dict)
    ]
    assert secrets and all(s.startswith("lightrag_prod_") for s in secrets)
    assert all("test" not in s for s in secrets)
    assert "pytest" not in str(workflow) and "bun test" not in str(workflow)


def test_original_delivery_files_and_test_resources_match_preimplementation_baseline():
    baseline = json.loads(
        (
            ROOT
            / "openspec/changes/add-prod-zero-downtime-delivery/verification/2026-10-21-delivery-baseline.json"
        ).read_text()
    )
    for name, digest in baseline["files"].items():
        if name.startswith(
            (
                ".woodpecker/",
                "k8s-deploy/lightrag-kustomize/base/",
                "k8s-deploy/lightrag-kustomize/overlays/test/",
            )
        ) or name in {"scripts/ci/deploy-test.sh", "scripts/ci/k8s_deploy.py"}:
            assert hashlib.sha256((ROOT / name).read_bytes()).hexdigest() == digest, (
                name
            )


def test_formal_tag_filter_survives_lifting_the_static_workflow_gate():
    import re

    workflow = yaml.safe_load((ROOT / ".woodpecker/deploy-prod.yml").read_text())
    step = workflow["steps"]["deploy-prod"]
    expression = step["when"][0]["evaluate"]
    pattern = re.fullmatch(r'CI_COMMIT_TAG matches "(.+)"', expression)[1]
    assert re.fullmatch(pattern, "v1.2.3")
    for tag in ("v1.2.3-test", "v1.2.3-pre", "v1.2.3-other", "v1.2.3trailing"):
        assert not re.fullmatch(pattern, tag)
