"""Build a local, non-production overlay without Git or registry publication.

Only the declared source roots are inputs. An overlay is not an OCI image or a
verified commit; deployment must separately verify the base image, dependencies,
mounted bytes and isolated target before starting a candidate.
"""

import argparse
import gzip
import hashlib
import io
import json
import os
from pathlib import Path, PurePosixPath
import re
import tarfile

from scripts.performance.http import save_json


ROOTS = ("lightrag", "lightrag_delivery", "scripts/performance")
MAX_TOTAL_BYTES = 32 * 1024**2
MAX_ARCHIVE_BYTES = 8 * 1024**2
MAX_FILES = 2048
PART_BYTES = 512 * 1024
EXTENSIONS = {
    ".py",
    ".sql",
    ".json",
    ".html",
    ".css",
    ".js",
    ".mjs",
    ".map",
    ".svg",
    ".png",
    ".ico",
    ".ttf",
    ".woff",
    ".woff2",
    ".md",
    ".txt",
    ".yaml",
    ".yml",
    ".toml",
}


def _hash(data):
    return hashlib.sha256(data).hexdigest()


def _canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode()


def _safe_name(name):
    p = PurePosixPath(name)
    if (
        not isinstance(name, str)
        or p.is_absolute()
        or "\\" in name
        or any(x in {"", ".", ".."} or x.startswith(".") for x in p.parts)
        or (
            name != "pyproject.toml"
            and not any(name.startswith(r + "/") for r in ROOTS)
        )
        or "__pycache__" in p.parts
        or "secret" in p.name.lower()
        or "credential" in p.name.lower()
        or (p.suffix not in EXTENSIONS and p.name != "LICENSE")
    ):
        raise ValueError("Source path or file type refused")


def _identity(runtime_image, base_commit):
    if (
        not isinstance(runtime_image, str)
        or "://" in runtime_image
        or not re.fullmatch(
            r"[A-Za-z0-9][A-Za-z0-9._:/-]*@sha256:[a-f0-9]{64}", runtime_image
        )
        or not isinstance(base_commit, str)
        or not re.fullmatch(r"[a-f0-9]{40}", base_commit)
    ):
        raise ValueError("Explicit pinned runtime and base commit identity required")


def _source(root):
    paths = [root / "pyproject.toml"]
    for name in ROOTS:
        directory = root / name
        if directory.is_symlink() or not directory.is_dir():
            raise ValueError("Source roots must be real repository directories")
        paths.extend(sorted(directory.rglob("*")))
    payloads, size = {}, 0
    for path in paths:
        if path.is_symlink():
            raise ValueError("Source symlink refused")
        rel = path.relative_to(root)
        if any(x.startswith(".") or x == "__pycache__" for x in rel.parts):
            continue
        if path.is_dir() or path.suffix in {".pyc", ".pyo"}:
            continue
        name = rel.as_posix()
        _safe_name(name)
        size += path.stat().st_size
        if size > MAX_TOTAL_BYTES or len(payloads) >= MAX_FILES:
            raise ValueError("Source size or file count exceeds bounds")
        data = path.read_bytes()
        if len(data) != path.stat().st_size:
            raise ValueError("Source changed while reading")
        payloads[name] = data
    if (
        not {
            "pyproject.toml",
            "lightrag/__init__.py",
            "lightrag_delivery/__init__.py",
        }
        <= payloads.keys()
    ):
        raise ValueError("Source package is incomplete")
    return dict(sorted(payloads.items()))


def _write(path, data):
    with os.fdopen(
        os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "wb"
    ) as f:
        f.write(data)
        f.flush()
        os.fsync(f.fileno())


def build_bundle(repository, output, *, runtime_image, base_commit):
    _identity(runtime_image, base_commit)
    root, output = Path(repository).resolve(strict=True), Path(output)
    if not output.resolve().is_relative_to(root) or output.resolve() == root:
        raise ValueError("Output must remain inside the repository")
    if output.exists() or output.is_symlink():
        raise FileExistsError("Existing candidate output cannot be overwritten")
    payloads = _source(root)
    raw = io.BytesIO()
    with tarfile.open(fileobj=raw, mode="w", format=tarfile.USTAR_FORMAT) as writer:
        for name, data in payloads.items():
            item = tarfile.TarInfo(name)
            item.size, item.mode, item.mtime = len(data), 0o644, 0
            writer.addfile(item, io.BytesIO(data))
    compressed = io.BytesIO()
    with gzip.GzipFile(fileobj=compressed, mode="wb", filename="", mtime=0) as writer:
        writer.write(raw.getvalue())
    archive = compressed.getvalue()
    if len(archive) > MAX_ARCHIVE_BYTES:
        raise ValueError("Archive size exceeds bounds")
    if _source(root) != payloads:
        raise ValueError("Source changed while packaging; create a fresh snapshot")
    files = {
        name: {"bytes": len(data), "sha256": _hash(data)}
        for name, data in payloads.items()
    }
    manifest = dict(
        format="lightrag-working-tree-overlay/v1",
        source_kind="working_tree_snapshot",
        production_eligible=False,
        runtime_image=runtime_image,
        base_commit=base_commit,
        files=files,
        source_sha256=_hash(_canonical(files)),
        archive_bytes=len(archive),
        archive_sha256=_hash(archive),
        parts=[],
    )
    output.mkdir(mode=0o700, parents=True)
    (output / "parts").mkdir(mode=0o700)
    _write(output / "bundle.tgz", archive)
    for i, start in enumerate(range(0, len(archive), PART_BYTES)):
        data, name = archive[start : start + PART_BYTES], f"parts/{i:04d}.bin"
        _write(output / name, data)
        manifest["parts"].append(dict(path=name, bytes=len(data), sha256=_hash(data)))
    save_json(output / "manifest.json", manifest)
    verify_bundle(output)
    return manifest


def verify_bundle(output):
    output = Path(output)
    for name in ("manifest.json", "bundle.tgz"):
        path = output / name
        limit = MAX_ARCHIVE_BYTES if name.endswith("tgz") else 2 * 1024**2
        if path.is_symlink() or path.stat().st_size > limit:
            raise ValueError("Bundle file or size refused")
    manifest = json.loads((output / "manifest.json").read_text())
    expected = {
        "format",
        "source_kind",
        "production_eligible",
        "runtime_image",
        "base_commit",
        "files",
        "source_sha256",
        "archive_bytes",
        "archive_sha256",
        "parts",
    }
    if (
        set(manifest) != expected
        or manifest["format"] != "lightrag-working-tree-overlay/v1"
        or manifest["source_kind"] != "working_tree_snapshot"
        or manifest["production_eligible"] is not False
    ):
        raise ValueError("Bundle is not an isolated non-production overlay")
    _identity(manifest["runtime_image"], manifest["base_commit"])
    files = manifest["files"]
    if not isinstance(files, dict) or not 1 <= len(files) <= MAX_FILES:
        raise ValueError("Invalid source manifest")
    if (
        not {
            "pyproject.toml",
            "lightrag/__init__.py",
            "lightrag_delivery/__init__.py",
        }
        <= files.keys()
    ):
        raise ValueError("Source package is incomplete")
    if _hash(_canonical(files)) != manifest["source_sha256"]:
        raise ValueError("Source manifest digest differs")
    archive = (output / "bundle.tgz").read_bytes()
    if (
        len(archive) != manifest["archive_bytes"]
        or _hash(archive) != manifest["archive_sha256"]
    ):
        raise ValueError("Archive digest differs")
    parts = manifest["parts"]
    if (
        not isinstance(parts, list)
        or not 1 <= len(parts) <= MAX_ARCHIVE_BYTES // PART_BYTES
    ):
        raise ValueError("Invalid archive parts")
    rebuilt = bytearray()
    for i, item in enumerate(parts):
        if (
            set(item) != {"path", "bytes", "sha256"}
            or item["path"] != f"parts/{i:04d}.bin"
        ):
            raise ValueError("Archive part path differs")
        path = output / item["path"]
        if (
            path.is_symlink()
            or not path.resolve().is_relative_to(output.resolve())
            or path.stat().st_size > PART_BYTES
        ):
            raise ValueError("Archive part size or path refused")
        data = path.read_bytes()
        if len(data) != item["bytes"] or _hash(data) != item["sha256"]:
            raise ValueError("Archive part digest differs")
        rebuilt.extend(data)
    if rebuilt != archive:
        raise ValueError("Archive parts do not reassemble")
    with gzip.GzipFile(fileobj=io.BytesIO(archive)) as stream:
        raw = stream.read(MAX_TOTAL_BYTES + MAX_FILES * 1024 + 10241)
    if len(raw) > MAX_TOTAL_BYTES + MAX_FILES * 1024 + 10240:
        raise ValueError("Expanded archive size exceeds bounds")
    seen, total = set(), 0
    with tarfile.open(fileobj=io.BytesIO(raw), mode="r:") as reader:
        for member in reader:
            _safe_name(member.name)
            if not member.isfile() or member.name in seen or member.name not in files:
                raise ValueError("Archive contains an undeclared or unsafe source")
            seen.add(member.name)
            total += member.size
            if total > MAX_TOTAL_BYTES or len(seen) > MAX_FILES:
                raise ValueError("Source size or count exceeds bounds")
            data = reader.extractfile(member).read()
            if files[member.name] != {"bytes": len(data), "sha256": _hash(data)}:
                raise ValueError("Archive source differs from manifest")
    if seen != set(files):
        raise ValueError("Archive source is incomplete")
    return manifest


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repository", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--runtime-image", required=True)
    parser.add_argument("--base-commit", required=True)
    args = parser.parse_args(argv)
    try:
        manifest = build_bundle(
            args.repository,
            args.output,
            runtime_image=args.runtime_image,
            base_commit=args.base_commit,
        )
        print(
            json.dumps(
                {
                    k: manifest[k]
                    for k in (
                        "source_kind",
                        "production_eligible",
                        "source_sha256",
                        "archive_sha256",
                        "archive_bytes",
                    )
                }
            )
        )
        return 0
    except Exception as error:
        print(
            type(error).__name__ + ": candidate bundle refused; no deployment performed"
        )
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
