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
