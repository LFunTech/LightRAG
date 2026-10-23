"""Pin local overlay identity without granting a production artifact identity."""

import hashlib
import json
from pathlib import Path
import tarfile

import pytest


IMAGE = "registry.invalid/lightrag@sha256:" + "a" * 64
COMMIT = "b" * 40


@pytest.fixture
def repo(tmp_path):
    for name in ("lightrag", "lightrag_delivery", "scripts/performance"):
        path = tmp_path / name
        path.mkdir(parents=True)
        (path / "__init__.py").write_text('VALUE = "current-uncommitted"\n')
    (tmp_path / "pyproject.toml").write_text('[project]\nname = "lightrag"\n')
    (tmp_path / ".secrets").mkdir()
    (tmp_path / ".secrets/prod.secrets").write_text("NEVER-PACK-THIS\n")
    (tmp_path / ".env").write_text("NEVER-PACK-THIS-EITHER\n")
    return tmp_path


def pack(repo, name="temp/candidate", **kwargs):
    from scripts.performance.candidate_bundle import build_bundle

    return build_bundle(
        repo, repo / name, runtime_image=IMAGE, base_commit=COMMIT, **kwargs
    )


def test_current_source_is_packaged_but_credentials_are_not(repo):
    manifest = pack(repo)
    assert manifest["source_kind"] == "working_tree_snapshot"
    assert manifest["production_eligible"] is False
    assert manifest["base_commit"] == COMMIT
    assert manifest["runtime_image"] == IMAGE
    archive = repo / "temp/candidate/bundle.tgz"
    with tarfile.open(archive, "r:gz") as reader:
        assert sorted(reader.getnames()) == [
            "lightrag/__init__.py",
            "lightrag_delivery/__init__.py",
            "pyproject.toml",
            "scripts/performance/__init__.py",
        ]
        assert reader.extractfile("lightrag/__init__.py").read() == (
            b'VALUE = "current-uncommitted"\n'
        )
    assert (
        manifest["archive_sha256"] == hashlib.sha256(archive.read_bytes()).hexdigest()
    )
    assert json.loads((archive.parent / "manifest.json").read_text()) == manifest


def test_archive_is_reproducible_and_reassembles_from_bounded_parts(repo):
    first = pack(repo)
    (repo / "lightrag/__init__.py").touch()
    second = pack(repo, "temp/second")
    assert first == second
    parts = [repo / "temp/candidate" / p["path"] for p in first["parts"]]
    assert all(p.stat().st_size <= 512 * 1024 for p in parts)
    assert (
        b"".join(p.read_bytes() for p in parts)
        == (repo / "temp/candidate/bundle.tgz").read_bytes()
    )


def test_editing_current_source_changes_the_candidate_identity(repo):
    first = pack(repo)
    (repo / "lightrag/__init__.py").write_text("VALUE = 2\n")
    second = pack(repo, "temp/second")
    assert first["source_sha256"] != second["source_sha256"]
    assert first["archive_sha256"] != second["archive_sha256"]


@pytest.mark.parametrize(
    "image",
    [
        "registry.invalid/lightrag:latest",
        "https://u:p@host/x",
        "https://registry.invalid/lightrag@sha256:" + "a" * 64,
    ],
)
def test_unpinned_or_credential_bearing_base_image_is_refused(repo, image):
    from scripts.performance.candidate_bundle import build_bundle

    with pytest.raises(ValueError, match="identity"):
        build_bundle(
            repo, repo / "temp/candidate", runtime_image=image, base_commit=COMMIT
        )
    assert not (repo / "temp/candidate").exists()


def test_symlink_is_refused_before_creating_an_output_directory(repo):
    (repo / "lightrag/linked.py").symlink_to(repo / ".secrets/prod.secrets")
    with pytest.raises(ValueError, match="symlink"):
        pack(repo)
    assert not (repo / "temp/candidate").exists()


def test_secret_file_in_a_package_is_refused_without_reading_it(repo):
    (repo / "lightrag/credential.pem").write_text("PRIVATE-MATERIAL")
    with pytest.raises(ValueError, match="file type"):
        pack(repo)
    assert not (repo / "temp/candidate").exists()


def test_incomplete_core_package_cannot_fall_back_to_the_old_runtime(repo):
    (repo / "lightrag/__init__.py").unlink()
    with pytest.raises(ValueError, match="incomplete"):
        pack(repo)
    assert not (repo / "temp/candidate").exists()


def test_existing_output_and_cross_repository_output_are_refused(repo):
    from scripts.performance.candidate_bundle import build_bundle

    pack(repo)
    with pytest.raises(FileExistsError):
        pack(repo)
    with pytest.raises(ValueError, match="repository"):
        build_bundle(
            repo, repo.parent / "outside", runtime_image=IMAGE, base_commit=COMMIT
        )
    assert not (repo.parent / "outside").exists()


def test_verification_detects_corrupt_archive_part_and_file_manifest(repo):
    from scripts.performance.candidate_bundle import verify_bundle

    pack(repo)
    output = repo / "temp/candidate"
    assert verify_bundle(output)["production_eligible"] is False
    part = output / "parts/0000.bin"
    original = part.read_bytes()
    part.write_bytes(original + b"corrupt")
    with pytest.raises(ValueError, match="part"):
        verify_bundle(output)
    part.write_bytes(original)
    manifest = json.loads((output / "manifest.json").read_text())
    manifest["files"]["lightrag/__init__.py"]["sha256"] = "0" * 64
    (output / "manifest.json").write_text(json.dumps(manifest))
    with pytest.raises(ValueError, match="[Ss]ource"):
        verify_bundle(output)


def test_size_bound_is_enforced_before_any_output(repo, monkeypatch):
    from scripts.performance import candidate_bundle

    monkeypatch.setattr(candidate_bundle, "MAX_TOTAL_BYTES", 2)
    with pytest.raises(ValueError, match="size"):
        pack(repo)
    assert not (repo / "temp/candidate").exists()


def test_same_size_concurrent_edit_is_refused_before_writing_output(repo, monkeypatch):
    path = repo / "lightrag/__init__.py"
    original_read = Path.read_bytes
    edited = False

    def concurrent_edit(self):
        nonlocal edited
        data = original_read(self)
        if self == path and not edited:
            edited = True
            self.write_bytes(data.replace(b"current", b"changed"))
        return data

    monkeypatch.setattr(Path, "read_bytes", concurrent_edit)
    with pytest.raises(ValueError, match="changed"):
        pack(repo)
    assert not (repo / "temp/candidate").exists()


def test_a_manifest_cannot_promote_an_overlay_to_a_production_artifact(repo):
    from scripts.performance.candidate_bundle import verify_bundle

    pack(repo)
    output = repo / "temp/candidate"
    manifest = json.loads((output / "manifest.json").read_text())
    manifest["production_eligible"] = True
    (output / "manifest.json").write_text(json.dumps(manifest))
    with pytest.raises(ValueError, match="non-production"):
        verify_bundle(output)


def test_cli_reports_only_metadata_and_has_no_deployment_side_effects(repo, capsys):
    from scripts.performance.candidate_bundle import main

    assert (
        main(
            [
                "--repository",
                str(repo),
                "--output",
                str(repo / "temp/cli"),
                "--runtime-image",
                IMAGE,
                "--base-commit",
                COMMIT,
            ]
        )
        == 0
    )
    report = json.loads(capsys.readouterr().out)
    assert report["production_eligible"] is False
    assert report["source_kind"] == "working_tree_snapshot"
    assert set(report) == {
        "source_kind",
        "production_eligible",
        "source_sha256",
        "archive_sha256",
        "archive_bytes",
    }
