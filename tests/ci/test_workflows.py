from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[2]
WOODPECKER = ROOT / ".woodpecker"


def load(name):
    with (WOODPECKER / name).open() as fh:
        return yaml.safe_load(fh)


def test_quality_workflow_has_no_release_or_deploy_secrets():
    workflow = load("quality.yml")
    assert workflow["when"] == [
        {"event": ["push", "pull_request"], "branch": ["master"]},
        {"event": ["tag"], "ref": ["refs/tags/v*", "refs/tags/v*-pre", "refs/tags/v*-test"]},
    ]
    text = (WOODPECKER / "quality.yml").read_text()
    for forbidden in (
        "COS_",
        "REGISTRY_PASSWORD",
        "KUBECONFIG",
        "BAILIAN",
        "OPENAI_API_KEY",
    ):
        assert forbidden not in text


def test_release_workflows_are_tag_only_and_depend_on_quality():
    validate = load("validate-release.yml")
    build = load("build-image.yml")
    pre = load("pre-deploy.yml")
    deploy = load("deploy-test.yml")
    assert validate["when"]["event"] == ["tag"]
    assert build["depends_on"] == ["validate-release"]
    assert pre["depends_on"] == ["build-image"]
    assert deploy["depends_on"] == ["pre-deploy"]
    assert deploy["when"]["ref"] == ["refs/tags/v*-test"]
    assert pre["when"]["ref"] == [
        "refs/tags/v*",
        "refs/tags/v*-pre",
        "refs/tags/v*-test",
    ]


def test_release_secrets_do_not_appear_in_pull_request_workflow():
    release_text = "\n".join(
        p.read_text() for p in WOODPECKER.glob("*.yml") if p.name != "quality.yml"
    )
    assert "from_secret" in release_text
    assert "kubeconfig_test" in release_text
    assert "DOCKER_PASSWORD" in release_text
    assert "cos_storage_secret_key" in release_text


def test_release_workflows_reference_existing_global_or_org_secrets():
    release_text = "\n".join(
        p.read_text() for p in WOODPECKER.glob("*.yml") if p.name != "quality.yml"
    )
    for expected in (
        "DOCKER_USERNAME",
        "DOCKER_PASSWORD",
        "cos_storage_endpoint",
        "cos_storage_bucket",
        "cos_storage_secret_id",
        "cos_storage_secret_key",
        "kubeconfig_test",
    ):
        assert expected in release_text
    for old_name in (
        "registry_username",
        "registry_password",
        "lightrag_cos_access_key",
        "lightrag_cos_secret_key",
        "lightrag_test_kubeconfig",
    ):
        assert old_name not in release_text


def test_release_workflows_delegate_complex_steps_to_tested_scripts():
    build = (WOODPECKER / "build-image.yml").read_text()
    pre = (WOODPECKER / "pre-deploy.yml").read_text()
    deploy = (WOODPECKER / "deploy-test.yml").read_text()
    assert "scripts/ci/build-image.sh" in build
    assert "python -m scripts.ci.delivery build-image" not in build
    assert "buildctl-daemonless.sh build" not in build
    assert "python -m scripts.ci.delivery resolve-image" in pre
    assert "LIGHTRAG_IMAGE_DIGEST" not in pre
    assert "scripts/ci/deploy-test.sh" in deploy
    assert "python -m scripts.ci.k8s_deploy deploy-test" not in deploy
    assert "acceptance-report.json" not in deploy
    assert "sed -i" not in deploy


def test_tag_delivery_workflows_are_self_contained_not_static_only():
    build = load("build-image.yml")
    pre = load("pre-deploy.yml")
    deploy = load("deploy-test.yml")

    assert build["steps"]["build-image"]["image"].startswith("moby/buildkit:")
    assert build["steps"]["build-image"]["commands"] == ["scripts/ci/build-image.sh"]

    assert "resolve-image" in " ".join(pre["steps"]["verify-image"]["commands"])
    assert "LIGHTRAG_IMAGE_DIGEST" not in pre["steps"]["verify-image"].get("environment", {})

    assert set(deploy["steps"]) == {"resolve-image", "deploy-test"}
    assert deploy["steps"]["resolve-image"]["image"].startswith("docker-hub.f123.pub/base/uv:")
    assert deploy["steps"]["deploy-test"]["image"].startswith("docker-hub.f123.pub/base/ci-tools:")
    assert deploy["steps"]["deploy-test"].get("depends_on") == ["resolve-image"]
    assert deploy["steps"]["deploy-test"]["commands"] == [
        "test -s build/release/image.env",
        ". build/release/image.env && scripts/ci/deploy-test.sh",
    ]


def test_woodpecker_quality_does_not_run_repository_test_suites():
    workflow_text = (WOODPECKER / "quality.yml").read_text()
    delivery_check = (ROOT / "scripts/ci/delivery-check.sh").read_text()
    forbidden_in_workflow = (
        "scripts/ci/backend-check.sh",
        "scripts/ci/frontend-check.sh",
        "./scripts/test.sh",
        "pytest",
        "bun test",
        "bunx tsc",
        "bun run lint",
    )
    for forbidden in forbidden_in_workflow:
        assert forbidden not in workflow_text

    for forbidden in (
        "./scripts/test.sh",
        "pytest",
        "bun test",
        "bunx tsc",
        "backend-check.sh",
        "frontend-check.sh",
    ):
        assert forbidden not in delivery_check
    assert "py_compile" in delivery_check
    assert "json.tool" in delivery_check
    assert "bash -n" in delivery_check


def test_local_check_scripts_are_not_invoked_by_woodpecker():
    workflow_text = "\n".join(p.read_text() for p in WOODPECKER.glob("*.yml"))
    assert "backend-check.sh" not in workflow_text
    assert "frontend-check.sh" not in workflow_text

    backend = (ROOT / "scripts/ci/backend-check.sh").read_text()
    frontend = (ROOT / "scripts/ci/frontend-check.sh").read_text()
    marker = "Local-only helper. Woodpecker must not invoke this script"
    assert marker in backend
    assert marker in frontend


def test_validate_release_creates_source_archive_and_record_before_build():
    text = (WOODPECKER / "validate-release.yml").read_text()
    assert '--allowed-repo "LFunTech/LightRAG"' in text
    assert "archive-source" in text
    assert "verify-archive" in text
    assert "release-identity.json" in text
    assert "source-record.json" in text
    assert "source.tar.gz" in text


def test_workflows_pin_linux_amd64_runner_platform():
    for path in WOODPECKER.glob("*.yml"):
        workflow = load(path.name)
        assert workflow["labels"] == {"platform": "linux/amd64"}


def test_quality_workflow_uses_available_internal_ci_image():
    text = (WOODPECKER / "quality.yml").read_text()
    assert "ghcr.io/" not in text
    assert (
        "docker-hub.f123.pub/base/uv:python3.12-bookworm-slim-lightrag-e5b65587bce7"
        "@sha256:e5b65587bce7de595f299855d7385fe7fca39b8a74baa261ba1b7147afa78e58"
        in text
    )
    assert "docker-hub.f123.pub/base/bun:" not in text
