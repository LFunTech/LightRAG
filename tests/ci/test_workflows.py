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
    for forbidden in ("COS_", "REGISTRY_PASSWORD", "KUBECONFIG", "BAILIAN", "OPENAI_API_KEY"):
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
    assert pre["when"]["ref"] == ["refs/tags/v*", "refs/tags/v*-pre", "refs/tags/v*-test"]


def test_release_secrets_do_not_appear_in_pull_request_workflow():
    release_text = "\n".join(p.read_text() for p in WOODPECKER.glob("*.yml") if p.name != "quality.yml")
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


def test_backend_quality_entry_records_junit_and_skips_external_integration():
    text = (ROOT / "scripts/ci/backend-check.sh").read_text()
    assert "--junitxml" in text
    assert "not integration" in text
    assert "libcairo2" in text
    assert "DOCX_SMART_HEADING=false" in text
    assert "lightrag-download-cache --spacy-install" not in text
    assert "ensure_faiss_importable" in text
    assert "for version in 1.13.0 1.12.0 1.11.0" in text
    assert "faiss-cpu==$version" in text
    assert 'PYTHON="$ROOT/.venv/bin/python" ./scripts/test.sh' in text


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


def test_quality_workflow_uses_available_internal_ci_images():
    text = (WOODPECKER / "quality.yml").read_text()
    assert "ghcr.io/" not in text
    assert "docker-hub.f123.pub/base/uv:python3.12-bookworm-slim-lightrag-e5b65587bce7@sha256:e5b65587bce7de595f299855d7385fe7fca39b8a74baa261ba1b7147afa78e58" in text
    assert "docker-hub.f123.pub/base/bun:1-lightrag-9114c058aeae@sha256:9114c058aeae42162ee16dd5084b95fe9473970bb6bcb5b232ab1630f0546895" in text
