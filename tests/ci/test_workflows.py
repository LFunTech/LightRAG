from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[2]
WOODPECKER = ROOT / ".woodpecker"


def load(name):
    with (WOODPECKER / name).open() as fh:
        return yaml.safe_load(fh)


def test_quality_workflow_has_no_release_or_deploy_secrets():
    workflow = load("quality.yml")
    assert workflow["when"]["event"] == ["push", "pull_request", "tag"]
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
    assert "lightrag_test_kubeconfig" in release_text
    assert "registry_password" in release_text


def test_release_workflows_delegate_complex_steps_to_tested_scripts():
    build = (WOODPECKER / "build-image.yml").read_text()
    pre = (WOODPECKER / "pre-deploy.yml").read_text()
    deploy = (WOODPECKER / "deploy-test.yml").read_text()
    assert "python -m scripts.ci.delivery build-image" in build
    assert "buildctl-daemonless.sh build" not in build
    assert "python -m scripts.ci.delivery validate-image" in pre
    assert "verify image manifest" not in pre
    assert "python -m scripts.ci.k8s_deploy deploy-test" in deploy
    assert "sed -i" not in deploy


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
