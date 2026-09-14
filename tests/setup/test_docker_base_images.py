"""Keep every Dockerfile's external image inputs on verified internal mirrors."""

import json
import re
import shlex
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
DOCKERFILES = sorted(path for path in ROOT.glob("Dockerfile*") if path.is_file())
LOCK = ROOT / "scripts/ci/base-images.lock.json"
pytestmark = pytest.mark.offline


def external_images(path: Path) -> list[str]:
    """Distinguish external image references from earlier build-stage aliases."""
    aliases = {"scratch"}
    images = []
    for line in path.read_text().replace("\\\n", " ").splitlines():
        syntax = re.match(r"\s*#\s*syntax\s*=\s*(\S+)", line)
        if syntax:
            images.append(syntax[1])
            continue
        words = shlex.split(line, comments=True)
        if not words:
            continue
        if words[0].upper() == "FROM":
            args = [word for word in words[1:] if not word.startswith("--")]
            if args[0].lower() not in aliases:
                images.append(args[0])
            if len(args) == 3 and args[1].upper() == "AS":
                aliases.add(args[2].lower())
        elif words[0].upper() == "COPY":
            for word in words[1:]:
                if word.startswith("--from="):
                    source = word.partition("=")[2]
                    if source.lower() not in aliases and not source.isdigit():
                        images.append(source)
    return images


@pytest.mark.parametrize("path", DOCKERFILES, ids=lambda path: path.name)
def test_external_images_are_internal_and_digest_pinned(path):
    images = external_images(path)
    assert images, f"No external image inputs found in {path.name}"
    for image in images:
        assert re.fullmatch(
            r"docker-hub\.f123\.pub/base/[a-z0-9-]+:[\w.-]+@sha256:[0-9a-f]{64}",
            image,
        ), f"Unmirrored or mutable external image in {path.name}: {image}"


def test_image_lock_covers_exactly_the_dockerfile_inputs():
    assert LOCK.is_file(), "The verified base-image inventory must be committed"
    lock = json.loads(LOCK.read_text())
    assert lock["schema_version"] == 1
    mirrors = [f"{entry['mirror']}@{entry['digest']}" for entry in lock["images"]]
    assert len(mirrors) == len(set(mirrors))
    assert set(mirrors) == {
        image for path in DOCKERFILES for image in external_images(path)
    }
    for entry in lock["images"]:
        assert entry["source"] != entry["mirror"]
        assert re.fullmatch(r"sha256:[0-9a-f]{64}", entry["digest"])
        assert "linux/amd64" in entry["platforms"]
        assert any(p.startswith("linux/arm64") for p in entry["platforms"])



def test_apt_build_stages_default_to_tsinghua_debian_mirrors():
    """Every Dockerfile apt stage rewrites Debian sources before apt-get update."""
    debian_mirror = "http://mirrors.tuna.tsinghua.edu.cn/debian"
    security_mirror = "http://mirrors.tuna.tsinghua.edu.cn/debian-security"
    for path in DOCKERFILES:
        text = path.read_text()
        if "apt-get update" not in text:
            continue
        assert f"ARG APT_DEBIAN_MIRROR={debian_mirror}" in text, path.name
        assert f"ARG APT_SECURITY_MIRROR={security_mirror}" in text, path.name
        for stage in re.split(r"(?m)^FROM ", text)[1:]:
            if "apt-get update" not in stage:
                continue
            before_update = stage[: stage.index("apt-get update")]
            assert "ARG APT_DEBIAN_MIRROR" in before_update, path.name
            assert "ARG APT_SECURITY_MIRROR" in before_update, path.name
            assert "/etc/apt/sources.list.d/debian.sources" in before_update, path.name
            assert "deb.debian.org/debian-security" in before_update, path.name
            assert "deb.debian.org/debian" in before_update, path.name
            assert "Acquire::Retries" in before_update, path.name


def test_python_dependency_installs_default_to_tsinghua_index():
    """Dockerfile uv/pip installs should not depend on public PyPI by default."""
    mirror = "https://pypi.tuna.tsinghua.edu.cn/simple"
    for path in DOCKERFILES:
        text = path.read_text()
        if "uv sync" not in text and "pip install" not in text:
            continue
        assert f"ARG PYPI_INDEX_URL={mirror}" in text, path.name
        assert "UV_DEFAULT_INDEX=${PYPI_INDEX_URL}" in text, path.name
        assert "PIP_INDEX_URL=${PYPI_INDEX_URL}" in text, path.name
        assert "pypi.org" not in text, path.name


def test_frontend_dependency_installs_default_to_npmmirror_registry():
    """Dockerfile frontend installs should use the same internal-friendly pattern as haier-demo."""
    mirror = "https://registry.npmmirror.com"
    for path in DOCKERFILES:
        text = path.read_text()
        if "bun install" not in text:
            continue
        assert f"ARG NPM_REGISTRY={mirror}" in text, path.name
        assert "NPM_CONFIG_REGISTRY=${NPM_REGISTRY}" in text, path.name


def test_rustup_install_uses_tsinghua_mirror_and_cannot_swallow_download_errors():
    """Rust bootstrap must not use curl|sh, which lets /bin/sh hide curl failures."""
    mirror = "https://mirrors.tuna.tsinghua.edu.cn/rustup"
    for path in DOCKERFILES:
        text = path.read_text()
        if "rustup" not in text and ".cargo/bin" not in text:
            continue
        assert f"ARG RUSTUP_DIST_SERVER={mirror}" in text, path.name
        assert f"ARG RUSTUP_UPDATE_ROOT={mirror}/rustup" in text, path.name
        assert f"{mirror}/rustup/dist/x86_64-unknown-linux-gnu/rustup-init" in text, (
            path.name
        )
        assert "sh.rustup.rs" not in text, path.name
        assert "| sh" not in text, path.name
