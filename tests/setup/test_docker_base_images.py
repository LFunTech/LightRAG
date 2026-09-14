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


def test_python_dependency_installs_default_to_internal_pypi_index():
    """Dockerfile uv/pip installs should not depend on public PyPI by default."""
    mirror = "https://mirror.f123.pub/repository/pypi/simple"
    for path in DOCKERFILES:
        text = path.read_text()
        if "uv sync" not in text and "pip install" not in text:
            continue
        assert f"ARG PYPI_INDEX_URL={mirror}" in text, path.name
        assert "UV_DEFAULT_INDEX=${PYPI_INDEX_URL}" in text, path.name
        assert "PIP_INDEX_URL=${PYPI_INDEX_URL}" in text, path.name
        assert "pypi.org" not in text, path.name


def test_uv_lock_uses_internal_pypi_urls_for_frozen_sync():
    """Frozen uv sync should not download locked artifacts from public PyPI."""
    text = (ROOT / "uv.lock").read_text()
    assert "https://mirror.f123.pub/repository/pypi/simple" in text
    assert "https://mirror.f123.pub/repository/pypi/packages/" in text
    assert "https://pypi.org/simple" not in text
    assert "https://files.pythonhosted.org/" not in text


def test_frontend_dependency_installs_default_to_npmmirror_registry():
    """Dockerfile frontend installs should use the same internal-friendly pattern as haier-demo."""
    mirror = "https://registry.npmmirror.com"
    for path in DOCKERFILES:
        text = path.read_text()
        if "bun install" not in text:
            continue
        assert f"ARG NPM_REGISTRY={mirror}" in text, path.name
        assert "NPM_CONFIG_REGISTRY=${NPM_REGISTRY}" in text, path.name


def test_release_dockerfiles_do_not_install_rust_toolchain():
    """The amd64 release build uses locked wheels and must not bootstrap Cargo."""
    forbidden = (
        "rustup",
        "RUSTUP_",
        ".cargo/bin",
        "build-essential",
        "pkg-config",
    )
    for path in (ROOT / "Dockerfile", ROOT / "Dockerfile.lite"):
        text = path.read_text()
        for marker in forbidden:
            assert marker not in text, f"{path.name} still contains {marker}"


def test_release_dockerfiles_do_not_download_spacy_or_network_cache_helpers_during_build():
    """CI image builds must not block on GitHub/spaCy cache downloads."""
    for path in (ROOT / "Dockerfile", ROOT / "Dockerfile.lite"):
        text = path.read_text()
        assert "lightrag-download-cache" not in text, path.name
        assert "spacy_models" not in text, path.name


def test_release_dockerfiles_bake_committed_tiktoken_cache_for_startup():
    """The API constructs the default tokenizer at startup, so it must not fetch BPE files at runtime."""
    for path in (ROOT / "Dockerfile", ROOT / "Dockerfile.lite"):
        text = path.read_text()
        assert "COPY docker/tiktoken-cache/ /app/tiktoken_cache/" in text, path.name
        assert "TIKTOKEN_CACHE_DIR=/app/tiktoken_cache" in text, path.name
        assert "tiktoken.encoding_for_model(\"gpt-4o-mini\")" in text, path.name
        assert "tiktoken.get_encoding(\"cl100k_base\")" in text, path.name


def test_committed_tiktoken_cache_matches_tiktoken_expected_files():
    """The committed BPE cache uses tiktoken's URL-derived cache keys and expected hashes."""
    import hashlib

    cache_dir = ROOT / "docker" / "tiktoken-cache"
    expected = {
        "fb374d419588a4632f3f557e76b4b70aebbca790": "446a9538cb6c348e3516120d7c08b09f57c36495e2acfffe59a5bf8b0cfb1a2d",
        "9b5ad71b2ce5302211f9c61530b329a4922fc6a4": "223921b76ee99bde995b7ff738513eef100fb51d18c93597a113bcffe865b2a7",
    }
    cache_files = [
        path.name
        for path in cache_dir.iterdir()
        if path.is_file() and not path.name.endswith(".md")
    ]
    assert sorted(cache_files) == sorted(expected)
    for name, digest in expected.items():
        assert hashlib.sha256((cache_dir / name).read_bytes()).hexdigest() == digest


def test_release_dockerfiles_install_python_dependencies_in_final_stage_only():
    """Kaniko must not cross-stage save or copy a large Python virtualenv."""
    for path in (ROOT / "Dockerfile", ROOT / "Dockerfile.lite"):
        text = path.read_text()
        assert " AS builder" not in text, path.name
        assert "--from=builder" not in text, path.name
        assert "COPY --from=builder /app/.venv" not in text, path.name
        assert "COPY --from=builder /root/.local" not in text, path.name


def test_dockerfiles_are_kaniko_compatible_without_buildkit_run_mounts():
    """Woodpecker builds with Kaniko, so Dockerfiles must avoid BuildKit-only syntax."""
    for path in DOCKERFILES:
        text = path.read_text()
        assert "RUN --mount=" not in text, path.name
        assert "$BUILDPLATFORM" not in text, path.name


def test_dockerfiles_do_not_keep_unused_buildkit_parser_frontend():
    """Kaniko ignores BuildKit parser directives, so do not retain unused image inputs."""
    for path in DOCKERFILES:
        text = path.read_text()
        assert "# syntax=" not in text, path.name
