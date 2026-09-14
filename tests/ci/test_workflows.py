import re
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[2]
WOODPECKER = ROOT / ".woodpecker"
CI_TOOLS_IMAGE = "docker-hub.f123.pub/base/ci-tools:alpine-3.22.2"
BUILDKIT_IMAGE = (
    "docker-hub.f123.pub/base/buildkit:"
    "v0.24.0-rootless-amd64-lightrag-ea4fedf4f72d"
    "@sha256:ea4fedf4f72d34133f43236149d961cdb6197735069711a3d3131c76a1ca314e"
)
SOURCE_ARTIFACT_PREFIX = 'lightrag/ci-source/$CI_COMMIT_SHA/$CI_PIPELINE_NUMBER'


def load(name):
    with (WOODPECKER / name).open() as fh:
        return yaml.safe_load(fh)


def test_buildkit_tool_image_is_locked_and_internal():
    lock = yaml.safe_load((ROOT / "scripts/ci/tool-images.lock.json").read_text())
    buildkit = next(
        item for item in lock["images"] if item["name"] == "rootless-buildkit"
    )
    assert buildkit["source"] == "docker.io/moby/buildkit:v0.24.0-rootless"
    assert buildkit["source_index_digest"] == (
        "sha256:995077ff90af1afff56ff23018699d7511d122b2b111041f2011bd12afd5c0fe"
    )
    assert buildkit["platform"] == "linux/amd64"
    assert buildkit["digest"] == (
        "sha256:ea4fedf4f72d34133f43236149d961cdb6197735069711a3d3131c76a1ca314e"
    )
    assert load("build-image.yml")["steps"]["build-image"]["image"] == (
        f"{buildkit['mirror']}@{buildkit['digest']}"
    )


def test_build_image_uses_rootless_kubernetes_security_context():
    build = load("build-image.yml")
    kubernetes = build["steps"]["build-image"]["backend_options"]["kubernetes"]
    security_context = kubernetes["securityContext"]
    assert security_context["runAsUser"] == 1000
    assert security_context["runAsGroup"] == 1000
    assert security_context["seccompProfile"] == {"type": "Unconfined"}
    assert security_context["apparmorProfile"] == {"type": "Unconfined"}
    assert build["steps"]["build-image"]["environment"]["BUILDKITD_FLAGS"] == (
        "--oci-worker-no-process-sandbox"
    )


def test_build_image_declares_kubernetes_resource_budget():
    build = load("build-image.yml")
    resources = build["steps"]["build-image"]["backend_options"]["kubernetes"][
        "resources"
    ]
    assert resources == {
        "requests": {
            "cpu": "1000m",
            "memory": "2Gi",
            "ephemeral-storage": "20Gi",
        },
        "limits": {
            "cpu": "4000m",
            "memory": "8Gi",
            "ephemeral-storage": "40Gi",
        },
    }


def test_release_source_workflow_runs_static_validation_and_uploads_source_once():
    workflow = load("validate-release.yml")
    assert "depends_on" not in workflow
    assert workflow.get("skip_clone") is not True
    assert workflow["when"] == {
        "event": ["tag"],
        "ref": ["refs/tags/v*", "refs/tags/v*-pre", "refs/tags/v*-test"],
    }
    assert set(workflow["steps"]) == {"delivery-static", "validate-release", "upload-source"}
    assert workflow["steps"]["validate-release"]["depends_on"] == ["delivery-static"]
    assert workflow["steps"]["upload-source"]["depends_on"] == ["validate-release"]
    assert workflow["steps"]["upload-source"]["image"] == CI_TOOLS_IMAGE
    upload_text = "\n".join(workflow["steps"]["upload-source"]["commands"])
    assert ". scripts/ci/source-artifact.sh" in upload_text
    assert 'resolve_storage_endpoint "$STORAGE_ENDPOINT"' in upload_text
    assert "mc alias set deploy" in upload_text
    assert SOURCE_ARTIFACT_PREFIX in upload_text
    assert "source.tar.gz" in upload_text
    assert "source.tar.gz.sha256" in upload_text
    assert "source-record.json" in upload_text
    text = (WOODPECKER / "validate-release.yml").read_text()
    for forbidden in (
        "KUBECONFIG",
        "BAILIAN",
        "OPENAI_API_KEY",
    ):
        assert forbidden not in text


def test_source_artifact_bootstrap_escapes_shell_parameter_expansion():
    for name in (
        "validate-release.yml",
        "build-image.yml",
        "pre-deploy.yml",
        "deploy-test.yml",
    ):
        workflow = load(name)
        steps = workflow["steps"]
        step_names = ["upload-source"] if name == "validate-release.yml" else ["download-source"]
        for step_name in step_names:
            command_text = "\n".join(steps[step_name]["commands"])
            assert not re.search(r"(?<!\$)\$\{", command_text), (name, step_name)


def test_woodpecker_workflows_are_not_triggered_by_master_updates():
    for path in WOODPECKER.glob("*.yml"):
        text = path.read_text()
        assert "pull_request" not in text
        assert "push" not in text
        assert "branch:" not in text


def test_only_release_source_workflow_uses_default_clone():
    workflows = {path.name: load(path.name) for path in WOODPECKER.glob("*.yml")}
    cloned = [name for name, workflow in workflows.items() if workflow.get("skip_clone") is not True]
    assert cloned == ["validate-release.yml"]


def test_downstream_workflows_skip_clone_and_download_source_from_minio():
    for name in ("build-image.yml", "pre-deploy.yml", "deploy-test.yml"):
        workflow = load(name)
        assert workflow["skip_clone"] is True
        assert "download-source" in workflow["steps"]
        download = workflow["steps"]["download-source"]
        assert download["image"] == CI_TOOLS_IMAGE
        assert download["environment"]["STORAGE_ENDPOINT"]["from_secret"] == "cos_storage_endpoint"
        assert download["environment"]["STORAGE_BUCKET"]["from_secret"] == "cos_storage_bucket"
        assert download["environment"]["STORAGE_ACCESS_KEY"]["from_secret"] == "cos_storage_secret_id"
        assert download["environment"]["STORAGE_SECRET_KEY"]["from_secret"] == "cos_storage_secret_key"
        command_text = "\n".join(download["commands"])
        assert ". scripts/ci/source-artifact.sh" not in command_text
        assert "resolve_storage_endpoint() {" in command_text
        assert 'resolve_storage_endpoint "$STORAGE_ENDPOINT"' in command_text
        assert "mc cp" in command_text
        assert SOURCE_ARTIFACT_PREFIX in command_text
        assert "sha256sum -c source.tar.gz.sha256" in command_text
        assert "find . -mindepth 1 -maxdepth 1 -exec rm -rf {} +" in command_text
        assert "tar -xzf /tmp/lightrag-source/source.tar.gz -C ." in command_text


def test_release_workflows_are_tag_only_and_ordered_from_source_artifact():
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
    release_text = "\n".join(p.read_text() for p in WOODPECKER.glob("*.yml"))
    assert "from_secret" in release_text
    assert "kubeconfig_test" in release_text
    assert "DOCKER_PASSWORD" in release_text
    assert "cos_storage_secret_key" in release_text


def test_release_workflows_reference_existing_global_or_org_secrets():
    release_text = "\n".join(p.read_text() for p in WOODPECKER.glob("*.yml"))
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


def test_build_image_script_streams_plain_buildkit_progress():
    script = (ROOT / "scripts/ci/build-image.sh").read_text()
    assert script.startswith("#!/usr/bin/env sh\n")
    assert "starting rootless BuildKit image build" in script
    assert "BUILDKIT_PROGRESS=plain" in script
    assert "--progress=plain" in script
    assert 'METADATA_FILE="${METADATA_FILE:-/tmp/lightrag-build-metadata.json}"' in script
    assert 'mkdir -p "$DOCKER_CONFIG_DIR" "$(dirname "$METADATA_FILE")"' in script
    assert 'mkdir -p "$DOCKER_CONFIG_DIR" build/release' not in script


def test_tag_delivery_workflows_are_self_contained_not_static_only():
    build = load("build-image.yml")
    pre = load("pre-deploy.yml")
    deploy = load("deploy-test.yml")

    assert build["steps"]["build-image"].get("depends_on") == ["download-source"]
    assert build["steps"]["build-image"]["image"] == BUILDKIT_IMAGE
    assert build["steps"]["build-image"]["commands"] == ["scripts/ci/build-image.sh"]

    assert pre["steps"]["verify-image"].get("depends_on") == ["download-source"]
    assert "resolve-image" in " ".join(pre["steps"]["verify-image"]["commands"])
    assert "LIGHTRAG_IMAGE_DIGEST" not in pre["steps"]["verify-image"].get("environment", {})

    assert set(deploy["steps"]) == {"download-source", "resolve-image", "deploy-test"}
    assert deploy["steps"]["resolve-image"].get("depends_on") == ["download-source"]
    assert deploy["steps"]["resolve-image"]["image"].startswith("docker-hub.f123.pub/base/uv:")
    assert deploy["steps"]["deploy-test"]["image"].startswith("docker-hub.f123.pub/base/ci-tools:")
    assert deploy["steps"]["deploy-test"].get("depends_on") == ["resolve-image"]
    assert deploy["steps"]["deploy-test"]["commands"] == [
        "test -s build/release/image.env",
        ". build/release/image.env && scripts/ci/deploy-test.sh",
    ]


def test_woodpecker_static_delivery_does_not_run_repository_test_suites():
    workflow_text = "\n".join(p.read_text() for p in WOODPECKER.glob("*.yml"))
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


def test_validate_release_does_not_install_packages_in_ci():
    validate = load("validate-release.yml")
    commands = validate["steps"]["validate-release"]["commands"]
    assert all("apt-get" not in command for command in commands)


def test_workflows_pin_linux_amd64_runner_platform():
    for path in WOODPECKER.glob("*.yml"):
        workflow = load(path.name)
        assert workflow["labels"] == {"platform": "linux/amd64"}


def test_static_delivery_workflow_uses_available_internal_ci_image():
    text = (WOODPECKER / "validate-release.yml").read_text()
    assert "ghcr.io/" not in text
    assert (
        "docker-hub.f123.pub/base/uv:python3.12-bookworm-slim-lightrag-e5b65587bce7"
        "@sha256:e5b65587bce7de595f299855d7385fe7fca39b8a74baa261ba1b7147afa78e58"
        in text
    )
    assert "docker-hub.f123.pub/base/bun:" not in text
