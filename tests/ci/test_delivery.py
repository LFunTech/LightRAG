import hashlib
import json
import subprocess
import tarfile
from pathlib import Path

import pytest

from scripts.ci import delivery


def test_release_identity_rejects_invalid_tags_and_prevents_deploy_for_pre():
    with pytest.raises(ValueError, match="unsupported release tag"):
        delivery.parse_release_tag("latest")
    assert delivery.parse_release_tag("v1.2.3-test").deploy_environment == "test"
    assert delivery.parse_release_tag("v1.2.3-pre").deploy_environment is None
    assert delivery.parse_release_tag("v1.2.3").deploy_environment is None


def test_release_source_requires_expected_repo_commit_and_master_ancestry(tmp_path):
    calls = []

    def fake_git(args):
        calls.append(args)
        if args == [
            "fetch",
            "--no-tags",
            "origin",
            "+refs/tags/v1.2.3-test:refs/tags/v1.2.3-test",
        ]:
            return ""
        if args == [
            "fetch",
            "--no-tags",
            "--unshallow",
            "origin",
            "+refs/heads/master:refs/remotes/origin/master",
        ]:
            return ""
        if args[:2] == ["rev-parse", "v1.2.3-test^{commit}"]:
            return "abc123\n"
        if args[:3] == ["merge-base", "--is-ancestor", "abc123"]:
            return ""
        raise AssertionError(args)

    identity = delivery.verify_release_source(
        tag="v1.2.3-test",
        event_commit="abc123",
        repo="minwang/LightRAG",
        allowed_repo="minwang/LightRAG",
        git=fake_git,
    )
    assert identity.commit == "abc123"
    assert identity.deploy_environment == "test"
    assert all("--depth=0" not in call for call in calls)
    assert all("--tags" not in call for call in calls if call and call[0] == "fetch")
    assert [
        "fetch",
        "--no-tags",
        "origin",
        "+refs/tags/v1.2.3-test:refs/tags/v1.2.3-test",
    ] in calls
    assert [
        "fetch",
        "--no-tags",
        "--unshallow",
        "origin",
        "+refs/heads/master:refs/remotes/origin/master",
    ] in calls


def test_release_source_rejects_moved_tag():
    def fake_git(args):
        if args[:2] == ["rev-parse", "v1.2.3-test^{commit}"]:
            return "def456\n"
        return ""

    with pytest.raises(delivery.ReleaseIdentityError, match="does not match event commit"):
        delivery.verify_release_source(
            tag="v1.2.3-test",
            event_commit="abc123",
            repo="minwang/LightRAG",
            allowed_repo="minwang/LightRAG",
            git=fake_git,
        )


def test_release_source_falls_back_to_remote_verifier_when_git_is_unavailable():
    calls = []

    def missing_git(args):
        raise FileNotFoundError("git")

    def remote_verifier(*, repo, tag, commit):
        calls.append((repo, tag, commit))

    identity = delivery.verify_release_source(
        tag="v1.2.3-test",
        event_commit="abc123",
        repo="LFunTech/LightRAG",
        allowed_repo="LFunTech/LightRAG",
        git=missing_git,
        remote_verifier=remote_verifier,
    )

    assert identity.commit == "abc123"
    assert calls == [("LFunTech/LightRAG", "v1.2.3-test", "abc123")]


def test_github_source_verifier_accepts_lightweight_tag_on_master(monkeypatch):
    responses = {
        "https://api.github.com/repos/LFunTech/LightRAG/git/ref/tags/v1.2.3-test": {
            "object": {"sha": "abc123", "type": "commit"}
        },
        "https://api.github.com/repos/LFunTech/LightRAG/compare/abc123...master": {
            "status": "ahead"
        },
    }
    requested = []

    class FakeResponse:
        headers = {}

        def __init__(self, data):
            self._data = json.dumps(data).encode()

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def read(self):
            return self._data

    def fake_open_url(request, timeout):
        requested.append(request.full_url)
        return FakeResponse(responses[request.full_url])

    monkeypatch.setattr(delivery, "_open_url", fake_open_url)

    delivery._verify_release_source_via_github(
        repo="LFunTech/LightRAG", tag="v1.2.3-test", commit="abc123"
    )

    assert requested == list(responses)


def test_http_delivery_requests_ignore_inherited_proxy_environment(monkeypatch):
    calls = []

    class FakeOpener:
        def open(self, request, timeout):
            calls.append(("open", request.full_url, timeout))
            return None

    def fake_build_opener(*handlers):
        calls.append(("handlers", handlers))
        return FakeOpener()

    monkeypatch.setenv("HTTPS_PROXY", "http://proxy.invalid:8080")
    monkeypatch.setattr(delivery.urllib.request, "build_opener", fake_build_opener)

    request = delivery.urllib.request.Request("https://api.github.com/repos/LFunTech/LightRAG")
    assert delivery._open_url(request, timeout=7) is None

    handler_call = calls[0]
    assert handler_call[0] == "handlers"
    proxy_handler = handler_call[1][0]
    assert proxy_handler.proxies == {}
    assert calls[1] == ("open", "https://api.github.com/repos/LFunTech/LightRAG", 7)


def test_source_archive_excludes_secret_and_untracked_files(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "app.py").write_text("print('ok')\n")
    (repo / ".env").write_text("SECRET=bad\n")
    (repo / "untracked.txt").write_text("ignore\n")
    out = tmp_path / "source.tar.gz"
    record = delivery.create_source_archive(
        repo,
        out,
        tracked_files=["app.py", ".env", "missing.py"],
        identity=delivery.ReleaseIdentity("minwang/LightRAG", "v1.2.3-test", "abc", "p1", "test"),
    )
    assert record["sha256"] == hashlib.sha256(out.read_bytes()).hexdigest()
    with tarfile.open(out, "r:gz") as tar:
        assert sorted(tar.getnames()) == ["app.py", "release-record.json"]


def test_source_archive_can_use_clean_worktree_snapshot_without_git(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "app.py").write_text("print('ok')\n")
    (repo / ".env").write_text("SECRET=bad\n")
    (repo / "build" / "release").mkdir(parents=True)
    (repo / "build" / "release" / "generated.json").write_text("{}\n")
    (repo / "rag_storage").mkdir()
    (repo / "rag_storage" / "data.json").write_text("{}\n")

    def missing_git(args, cwd=None):
        raise FileNotFoundError("git")

    monkeypatch.setattr(delivery, "_run_git", missing_git)
    out = tmp_path / "source.tar.gz"
    delivery.create_source_archive(
        repo,
        out,
        identity=delivery.ReleaseIdentity(
            "LFunTech/LightRAG", "v1.2.3-test", "abc", "p1", "test"
        ),
    )

    with tarfile.open(out, "r:gz") as tar:
        assert sorted(tar.getnames()) == ["app.py", "release-record.json"]


def test_safe_extract_rejects_path_traversal_and_symlink_escape(tmp_path):
    archive = tmp_path / "bad.tar.gz"
    with tarfile.open(archive, "w:gz") as tar:
        payload = tmp_path / "payload"
        payload.write_text("bad")
        info = tar.gettarinfo(str(payload), arcname="../escape")
        with payload.open("rb") as fh:
            tar.addfile(info, fh)
    with pytest.raises(delivery.ArchiveValidationError, match="unsafe archive path"):
        delivery.safe_extract_archive(archive, tmp_path / "out")

    symlink_archive = tmp_path / "bad-link.tar.gz"
    with tarfile.open(symlink_archive, "w:gz") as tar:
        info = tarfile.TarInfo("link")
        info.type = tarfile.SYMTYPE
        info.linkname = "/etc/passwd"
        tar.addfile(info)
    with pytest.raises(delivery.ArchiveValidationError, match="unsafe symlink"):
        delivery.safe_extract_archive(symlink_archive, tmp_path / "out2")


def test_image_manifest_requires_amd64_and_revision_from_config_labels():
    manifest = {
        "mediaType": "application/vnd.oci.image.manifest.v1+json",
        "config": {"digest": "sha256:" + "1" * 64},
    }
    config = {
        "config": {
            "Labels": {
                "org.opencontainers.image.revision": "abc",
                "org.opencontainers.image.source": "https://github.com/minwang/LightRAG",
            }
        }
    }
    digest = "sha256:" + "a" * 64
    record = delivery.validate_image_manifest(
        manifest, config=config, expected_commit="abc", expected_digest=digest
    )
    assert record["digest"] == digest
    assert record["platform"] == "linux/amd64"

    config["config"]["Labels"]["org.opencontainers.image.revision"] = "def"
    with pytest.raises(delivery.ImageValidationError, match="revision"):
        delivery.validate_image_manifest(
            manifest, config=config, expected_commit="abc", expected_digest=digest
        )


def test_dockerfile_defines_delivery_revision_labels():
    dockerfile = Path("Dockerfile").read_text()
    assert "ARG LIGHTRAG_IMAGE_REVISION" in dockerfile
    assert "ARG LIGHTRAG_IMAGE_SOURCE" in dockerfile
    assert "org.opencontainers.image.revision=$LIGHTRAG_IMAGE_REVISION" in dockerfile


def test_resolve_image_identity_selects_amd64_manifest_and_config_revision():
    index_digest = "sha256:" + "1" * 64
    manifest_digest = "sha256:" + "2" * 64
    config_digest = "sha256:" + "3" * 64

    class FakeRegistryClient:
        def manifest(self, repository, reference):
            assert repository == "lfun/lightrag"
            if reference == "v1.2.3-test":
                return delivery.RegistryPayload(
                    data={
                        "mediaType": "application/vnd.oci.image.index.v1+json",
                        "manifests": [
                            {
                                "digest": manifest_digest,
                                "platform": {"os": "linux", "architecture": "amd64"},
                            }
                        ],
                    },
                    digest=index_digest,
                    media_type="application/vnd.oci.image.index.v1+json",
                )
            if reference == manifest_digest:
                return delivery.RegistryPayload(
                    data={
                        "mediaType": "application/vnd.oci.image.manifest.v1+json",
                        "config": {"digest": config_digest},
                    },
                    digest=manifest_digest,
                    media_type="application/vnd.oci.image.manifest.v1+json",
                )
            raise AssertionError(reference)

        def blob(self, repository, digest):
            assert repository == "lfun/lightrag"
            assert digest == config_digest
            return delivery.RegistryPayload(
                data={
                    "config": {
                        "Labels": {"org.opencontainers.image.revision": "abc123"}
                    }
                },
                digest=config_digest,
                media_type="application/vnd.oci.image.config.v1+json",
            )

    record = delivery.resolve_image_identity(
        image="docker-hub.f123.pub/lfun/lightrag",
        tag="v1.2.3-test",
        expected_commit="abc123",
        username="user",
        password="pass",
        client=FakeRegistryClient(),
    )
    assert record["digest"] == manifest_digest
    assert record["index_digest"] == index_digest
    assert record["image_ref"] == f"docker-hub.f123.pub/lfun/lightrag@{manifest_digest}"


def test_resolve_image_env_output_exports_vars_for_deploy_child_shell(tmp_path, monkeypatch):
    digest = "sha256:" + "4" * 64
    image_ref = f"docker-hub.f123.pub/lfun/lightrag@{digest}"
    tag_ref = "docker-hub.f123.pub/lfun/lightrag:v1.2.3-test"

    def fake_resolve_image_identity(**kwargs):
        return {
            "config_digest": "sha256:" + "5" * 64,
            "digest": digest,
            "image": "docker-hub.f123.pub/lfun/lightrag",
            "image_ref": image_ref,
            "index_digest": "",
            "platform": "linux/amd64",
            "revision": "abc123",
            "tag": "v1.2.3-test",
            "tag_ref": tag_ref,
        }

    monkeypatch.setattr(delivery, "resolve_image_identity", fake_resolve_image_identity)
    env_path = tmp_path / "image.env"
    record_path = tmp_path / "image-record.json"

    delivery.main(
        [
            "resolve-image",
            "--tag",
            "v1.2.3-test",
            "--commit",
            "abc123",
            "--image",
            "docker-hub.f123.pub/lfun/lightrag",
            "--username",
            "user",
            "--password",
            "pass",
            "--record-output",
            str(record_path),
            "--env-output",
            str(env_path),
        ]
    )

    result = subprocess.run(
        [
            "sh",
            "-c",
            f'. "{env_path}"; sh -c \'test "$LIGHTRAG_IMAGE_DIGEST" = "{digest}" '
            f'&& test "$LIGHTRAG_IMAGE_REF" = "{image_ref}" '
            f'&& test "$LIGHTRAG_IMAGE_TAG_REF" = "{tag_ref}"\'',
        ],
        check=False,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr


def test_release_state_rejects_concurrent_and_older_release(tmp_path):
    store = delivery.FileReleaseStateStore(tmp_path / "state.json")
    first = delivery.ReleaseIdentity("minwang/LightRAG", "v1.2.4-test", "c2", "20", "test")
    store.acquire("lightrag-test", first, "sha256:" + "2" * 64)
    with pytest.raises(delivery.DeployStateError, match="already owned"):
        store.acquire("lightrag-test", delivery.ReleaseIdentity("minwang/LightRAG", "v1.2.5-test", "c3", "21", "test"), "sha256:" + "3" * 64)
    store.mark_success("lightrag-test", first.pipeline_id)
    with pytest.raises(delivery.DeployStateError, match="older than successful"):
        store.acquire("lightrag-test", delivery.ReleaseIdentity("minwang/LightRAG", "v1.2.3-test", "c1", "22", "test"), "sha256:" + "1" * 64)


def test_registry_auth_file_is_0600_and_masks_secret(tmp_path):
    path = delivery.write_registry_auth(tmp_path, "docker-hub.f123.pub", "user", "pass")
    assert oct(path.stat().st_mode & 0o777) == "0o600"
    assert "pass" not in delivery.mask_secret("before pass after", "pass")
    assert json.loads(path.read_text())["auths"]["docker-hub.f123.pub"]["username"] == "user"

def test_release_source_rejects_unprovable_master_ancestry():
    def fake_git(args):
        if args and args[0] == "fetch":
            raise delivery.subprocess.CalledProcessError(1, args)
        return ""

    with pytest.raises(delivery.ReleaseIdentityError, match="cannot prove"):
        delivery.verify_release_source(
            tag="v1.2.3-test",
            event_commit="abc123",
            repo="minwang/LightRAG",
            allowed_repo="minwang/LightRAG",
            git=fake_git,
        )


def test_environment_snapshot_requires_test_namespace_initialized_profile_and_no_pending_writes():
    snapshot = {
        "cluster": "test",
        "namespace": "lightrag-test-01",
        "initialized": True,
        "profile": {
            "replicas": 2,
            "workers": 1,
            "kv_storage": "PGKVStorage",
            "doc_status_storage": "PGDocStatusStorage",
            "vector_storage": "PGVectorStorage",
            "graph_storage": "HugeGraphStorage",
            "workspace": "lightrag_test_01",
            "postgres_workspace": "lightrag_test_01",
        },
        "storage_state": {"fenced": False, "active_operations": 0, "pending_mutations": 0, "orphaned_claims": 0},
    }
    assert delivery.validate_test_environment_snapshot(snapshot)["namespace"] == "lightrag-test-01"
    bad = dict(snapshot, namespace="default")
    with pytest.raises(delivery.DeployStateError, match="namespace"):
        delivery.validate_test_environment_snapshot(bad)
    bad = dict(snapshot)
    bad["profile"] = dict(snapshot["profile"], workspace="lightrag_test_02", postgres_workspace="lightrag_test_02")
    with pytest.raises(delivery.DeployStateError, match="workspace"):
        delivery.validate_test_environment_snapshot(bad)
    bad = dict(snapshot, initialized=False)
    with pytest.raises(delivery.DeployStateError, match="initialized"):
        delivery.validate_test_environment_snapshot(bad)
    bad = dict(snapshot)
    bad["storage_state"] = {"fenced": True, "active_operations": 0, "pending_mutations": 0, "orphaned_claims": 0}
    with pytest.raises(delivery.DeployStateError, match="unsafe storage"):
        delivery.validate_test_environment_snapshot(bad)


def test_delivery_module_does_not_keep_unused_buildkit_build_entrypoint():
    assert not hasattr(delivery, "buildkit_command")


def test_release_record_rejects_conflicting_version_but_accepts_same_source(tmp_path):
    store = delivery.FileReleaseRecordStore(tmp_path / "records")
    identity = delivery.ReleaseIdentity("minwang/LightRAG", "v1.2.3-test", "abc", "p1", "test")
    record = store.publish(identity, source_sha256="0" * 64, image_digest="sha256:" + "a" * 64)
    assert record["commit"] == "abc"
    assert store.publish(identity, source_sha256="0" * 64, image_digest="sha256:" + "a" * 64) == record
    with pytest.raises(delivery.ReleaseIdentityError, match="conflicting"):
        store.publish(identity, source_sha256="1" * 64, image_digest="sha256:" + "b" * 64)


def test_validate_archive_record_checks_checksum_and_identity(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "app.py").write_text("ok")
    identity = delivery.ReleaseIdentity("minwang/LightRAG", "v1.2.3-test", "abc", "p1", "test")
    archive = tmp_path / "src.tgz"
    record = delivery.create_source_archive(repo, archive, tracked_files=["app.py"], identity=identity)
    delivery.validate_source_archive_record(archive, record, expected=identity)
    bad = dict(record, commit="def")
    with pytest.raises(delivery.ArchiveValidationError, match="identity"):
        delivery.validate_source_archive_record(archive, bad, expected=identity)
    bad = dict(record, sha256="0" * 64)
    with pytest.raises(delivery.ArchiveValidationError, match="checksum"):
        delivery.validate_source_archive_record(archive, bad, expected=identity)
