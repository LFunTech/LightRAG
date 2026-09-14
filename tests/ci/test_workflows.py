import re
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[2]
WOODPECKER = ROOT / ".woodpecker"
CI_TOOLS_IMAGE = "docker-hub.f123.pub/base/ci-tools:alpine-3.22.4"
KANIKO_IMAGE = (
    "docker-hub.f123.pub/devops/kaniko:v1.14.0-debug"
    "@sha256:1b282be1c4467618e9122d2ae457ffa3776a7caa52d7539e329123283bc71f79"
)
SOURCE_ARTIFACT_PREFIX = 'lightrag/ci-source/$CI_COMMIT_SHA/$CI_PIPELINE_NUMBER'


def load(name):
    with (WOODPECKER / name).open() as fh:
        return yaml.safe_load(fh)


def test_kaniko_tool_image_is_locked_and_internal():
    lock = yaml.safe_load((ROOT / "scripts/ci/tool-images.lock.json").read_text())
    kaniko = next(item for item in lock["images"] if item["name"] == "kaniko")
    assert kaniko["source"] == "gcr.io/kaniko-project/executor:v1.14.0-debug"
    assert kaniko["source_index_digest"] == (
        "sha256:d1173d94ddd1092aaf88c929922efde0beee6b3ebfed53bbb86475128a41def9"
    )
    assert kaniko["mirror_index_digest"] == (
        "sha256:660af157e453dfd327d5ded96a652d1e279cd3b0f3e68f047482f0510c288804"
    )
    assert kaniko["platform"] == "linux/amd64"
    assert kaniko["digest"] == (
        "sha256:1b282be1c4467618e9122d2ae457ffa3776a7caa52d7539e329123283bc71f79"
    )
    assert load("build-image.yml")["steps"]["build-image"]["image"] == (
        f"{kaniko['mirror']}@{kaniko['digest']}"
    )


def test_build_image_uses_kaniko_kubernetes_resource_context():
    build = load("build-image.yml")
    step = build["steps"]["build-image"]
    kubernetes = step["backend_options"]["kubernetes"]
    assert "hostUsers" not in kubernetes
    assert "securityContext" not in kubernetes
    assert "BUILDKITD_FLAGS" not in step["environment"]
    assert step["privileged"] is False


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


def test_build_image_uses_haier_style_kaniko_builder_for_streamed_logs():
    build = load("build-image.yml")
    step = build["steps"]["build-image"]
    assert step["image"] == KANIKO_IMAGE
    assert step["environment"]["HOME"] == "/kaniko"
    assert step["environment"]["REGISTRY"] == "docker-hub.f123.pub"
    assert step["environment"]["LIGHTRAG_IMAGE_REPOSITORY"] == "lfun/lightrag"
    assert step["environment"]["LIGHTRAG_IMAGE_CACHE_REPOSITORY"] == "lfun/cache-lightrag"
    assert step["commands"] == ["sh scripts/ci/build-image.sh"]


def test_build_image_uses_redo_snapshot_mode_for_large_python_venv_layers():
    script = (ROOT / "scripts/ci/build-image.sh").read_text()
    assert "--snapshot-mode=redo" in script
    assert "--use-new-run" not in script


def test_release_source_workflow_runs_static_validation_and_uploads_source_once():
    workflow = load("validate-release.yml")
    assert "depends_on" not in workflow
    assert workflow["skip_clone"] is True
    assert "clone" not in workflow
    assert workflow["when"] == [
        {
            "event": "tag",
            "evaluate": 'CI_COMMIT_TAG matches "^v[0-9]+[.][0-9]+[.][0-9]+(-pre|-test)?$"',
        },
        {"event": "tag", "ref": "refs/tags/v*"},
    ]
    assert list(workflow["steps"]) == ["release-source"]
    source = workflow["steps"]["release-source"]
    assert source["image"] == CI_TOOLS_IMAGE
    assert "depends_on" not in source
    command_text = "\n".join(source["commands"])
    assert "scripts/ci/delivery-check.sh" in command_text
    assert "verify-source" in command_text
    assert "archive-source" in command_text
    assert "verify-archive" in command_text
    assert ". scripts/ci/source-artifact.sh" in command_text
    assert 'resolve_storage_endpoint "$STORAGE_ENDPOINT"' in command_text
    assert "mc alias set deploy" in command_text
    assert SOURCE_ARTIFACT_PREFIX in command_text
    assert "source.tar.gz" in command_text
    assert "source.tar.gz.sha256" in command_text
    assert "source-record.json" in command_text
    assert source["environment"]["STORAGE_ENDPOINT"]["from_secret"] == "cos_storage_endpoint"
    assert source["environment"]["STORAGE_BUCKET"]["from_secret"] == "cos_storage_bucket"
    assert source["environment"]["STORAGE_ACCESS_KEY"]["from_secret"] == "cos_storage_secret_id"
    assert source["environment"]["STORAGE_SECRET_KEY"]["from_secret"] == "cos_storage_secret_key"
    text = (WOODPECKER / "validate-release.yml").read_text()
    for forbidden in (
        "KUBECONFIG",
        "BAILIAN",
        "OPENAI_API_KEY",
    ):
        assert forbidden not in text


def test_release_source_workflow_uses_haier_style_single_commit_clone():
    workflow = load("validate-release.yml")
    step = workflow["steps"]["release-source"]
    assert step["image"] == CI_TOOLS_IMAGE
    assert "depends_on" not in step

    command_text = "\n".join(step["commands"])
    assert 'git init --object-format sha1 "$CLONE_DIR"' in command_text
    assert 'git fetch --no-tags --depth=1 origin "+$${CI_COMMIT_SHA}:"' in command_text
    assert 'git reset --hard -q "$${CI_COMMIT_SHA}"' in command_text
    assert "http.lowSpeedTime 300" in command_text
    assert "git lfs" not in command_text
    assert "submodule" not in command_text
    assert not re.search(r"(?<!\$)\$\{", command_text)


def test_release_source_clone_uses_container_local_temp_dir_not_nfs_workspace():
    workflow = load("validate-release.yml")
    assert list(workflow["steps"]) == ["release-source"]
    command_text = "\n".join(workflow["steps"]["release-source"]["commands"])
    assert 'CLONE_DIR="$${LIGHTRAG_RELEASE_CLONE_DIR:-/tmp/lightrag-release-source}"' in command_text
    assert 'git init --object-format sha1 "$CLONE_DIR"' in command_text
    assert 'cd "$CLONE_DIR"' in command_text
    assert 'git reset --hard -q "$${CI_COMMIT_SHA}"' in command_text
    assert "git init --object-format sha1 ." not in command_text
    assert 'git config --global --replace-all safe.directory "$${workspace_dir}"' not in command_text


def test_source_artifact_bootstrap_escapes_shell_parameter_expansion():
    for name in (
        "validate-release.yml",
        "build-image.yml",
        "pre-deploy.yml",
        "deploy-test.yml",
    ):
        workflow = load(name)
        steps = workflow["steps"]
        step_names = ["release-source"] if name == "validate-release.yml" else ["download-source"]
        for step_name in step_names:
            command_text = "\n".join(steps[step_name]["commands"])
            assert not re.search(r"(?<!\$)\$\{", command_text), (name, step_name)


def test_woodpecker_workflows_are_not_triggered_by_master_updates():
    for path in WOODPECKER.glob("*.yml"):
        text = path.read_text()
        assert "pull_request" not in text
        assert "push" not in text
        assert "branch:" not in text


def test_release_source_workflow_is_the_only_manual_clone_step():
    workflows = {path.name: load(path.name) for path in WOODPECKER.glob("*.yml")}
    assert all(workflow.get("skip_clone") is True for workflow in workflows.values())
    assert all("clone" not in workflow for workflow in workflows.values())

    manual_clone_steps = []
    for name, workflow in workflows.items():
        for step_name, step in workflow["steps"].items():
            command_text = "\n".join(step.get("commands") or [])
            if "git fetch --no-tags --depth=1" in command_text:
                manual_clone_steps.append((name, step_name))
    assert manual_clone_steps == [("validate-release.yml", "release-source")]


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
    for workflow in (validate, build, pre, deploy):
        assert isinstance(workflow["when"], list)
        assert all(entry["event"] == "tag" for entry in workflow["when"])
        assert all("push" not in entry.values() for entry in workflow["when"])
    assert build["depends_on"] == ["validate-release"]
    assert pre["depends_on"] == ["build-image"]
    assert deploy["depends_on"] == ["pre-deploy"]
    assert deploy["when"] == [
        {
            "event": "tag",
            "evaluate": 'CI_COMMIT_TAG matches "^v[0-9]+[.][0-9]+[.][0-9]+-test$"',
        },
        {"event": "tag", "ref": "refs/tags/v*-test"},
    ]
    assert pre["when"] == validate["when"]


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


def test_build_image_script_streams_kaniko_logs_and_records_digest():
    script = (ROOT / "scripts/ci/build-image.sh").read_text()
    assert script.startswith("#!/usr/bin/env sh\n")
    assert "starting Kaniko image build" in script
    assert "KANIKO_EXECUTOR" in script
    assert "/kaniko/executor" in script
    assert '--context="dir://${CONTEXT_DIR}"' in script
    assert "--dockerfile=Dockerfile" in script
    assert "--cache=true" in script
    assert "--cache-copy-layers" in script
    assert '--cache-repo="${CACHE_REPO}"' in script
    assert "--custom-platform=linux/amd64" in script
    assert "--snapshot-mode=redo" in script
    assert '--digest-file="${DIGEST_FILE}"' in script
    assert "buildctl-daemonless.sh" not in script
    assert "BUILDKIT_PROGRESS" not in script


def test_tag_delivery_workflows_are_self_contained_not_static_only():
    build = load("build-image.yml")
    pre = load("pre-deploy.yml")
    deploy = load("deploy-test.yml")

    assert build["steps"]["build-image"].get("depends_on") == ["download-source"]
    assert build["steps"]["build-image"]["image"] == KANIKO_IMAGE
    assert build["steps"]["build-image"]["commands"] == ["sh scripts/ci/build-image.sh"]

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
        ". build/release/image.env && sh scripts/ci/deploy-test.sh",
    ]


def test_deploy_test_script_uses_posix_shell_and_prints_kubernetes_status():
    script = (ROOT / "scripts/ci/deploy-test.sh").read_text()
    assert script.startswith("#!/usr/bin/env sh\n")
    assert "show_kubectl_status" in script
    assert "Deployment status" in script
    assert "Pod status" in script
    assert "Service status" in script


def test_delivery_static_uses_strict_woodpecker_lint_when_available():
    delivery_check = (ROOT / "scripts/ci/delivery-check.sh").read_text()
    assert "woodpecker-cli lint --strict .woodpecker/*.yml" in delivery_check


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
    commands = [
        command
        for step in validate["steps"].values()
        for command in step.get("commands") or []
    ]
    assert all("apt-get" not in command for command in commands)


def test_workflows_pin_linux_amd64_runner_platform():
    for path in WOODPECKER.glob("*.yml"):
        workflow = load(path.name)
        assert workflow["labels"] == {"platform": "linux/amd64"}


def test_static_delivery_workflow_uses_available_internal_ci_image():
    text = (WOODPECKER / "validate-release.yml").read_text()
    assert "ghcr.io/" not in text
    assert CI_TOOLS_IMAGE in text
    assert "docker-hub.f123.pub/base/uv:" not in text
    assert "docker-hub.f123.pub/base/bun:" not in text
