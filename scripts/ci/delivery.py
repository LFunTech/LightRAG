"""CI delivery helpers for LightRAG Woodpecker release workflows."""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import re
import subprocess
import tarfile
import tempfile
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Callable, Iterable

RELEASE_TAG_RE = re.compile(r"^v(?P<version>\d+\.\d+\.\d+)(?P<suffix>-test|-pre)?$")
DIGEST_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
EXCLUDED_ARCHIVE_NAMES = {
    ".env",
    ".env.local",
    ".secrets",
    "id_rsa",
    "id_ed25519",
}
EXCLUDED_ARCHIVE_PREFIXES = (
    "rag_storage/",
    "inputs/",
    "temp/",
    ".git/",
    "node_modules/",
    "lightrag_webui/node_modules/",
)


class ReleaseIdentityError(RuntimeError):
    pass


class ArchiveValidationError(RuntimeError):
    pass


class ImageValidationError(RuntimeError):
    pass


class DeployStateError(RuntimeError):
    pass


@dataclass(frozen=True)
class ReleaseIdentity:
    repo: str
    tag: str
    commit: str
    pipeline_id: str
    deploy_environment: str | None = None


def parse_release_tag(tag: str) -> ReleaseIdentity:
    match = RELEASE_TAG_RE.fullmatch(tag or "")
    if not match:
        raise ValueError(f"unsupported release tag: {tag!r}")
    suffix = match.group("suffix")
    return ReleaseIdentity(
        repo="",
        tag=tag,
        commit="",
        pipeline_id="",
        deploy_environment="test" if suffix == "-test" else None,
    )


def _run_git(args: list[str], cwd: Path | None = None) -> str:
    result = subprocess.run(
        ["git", *args], cwd=cwd, check=True, capture_output=True, text=True
    )
    return result.stdout


def verify_release_source(
    *,
    tag: str,
    event_commit: str,
    repo: str,
    allowed_repo: str,
    git: Callable[[list[str]], str] | None = None,
    pipeline_id: str | None = None,
) -> ReleaseIdentity:
    parsed = parse_release_tag(tag)
    if repo != allowed_repo:
        raise ReleaseIdentityError(f"repository {repo!r} is not allowed")
    git = git or _run_git
    try:
        git(["fetch", "--depth=0", "origin", "master", "--tags"])
        tag_commit = git(["rev-parse", f"{tag}^{{commit}}"]).strip()
        if tag_commit != event_commit:
            raise ReleaseIdentityError(
                f"tag commit {tag_commit} does not match event commit {event_commit}"
            )
        git(["merge-base", "--is-ancestor", tag_commit, "origin/master"])
    except subprocess.CalledProcessError as exc:
        raise ReleaseIdentityError("cannot prove tag belongs to origin/master") from exc
    return ReleaseIdentity(
        repo=repo,
        tag=tag,
        commit=event_commit,
        pipeline_id=pipeline_id or os.getenv("CI_PIPELINE_NUMBER", "unknown"),
        deploy_environment=parsed.deploy_environment,
    )


def _archive_allowed(name: str) -> bool:
    clean = name.replace("\\", "/").lstrip("/")
    if clean in EXCLUDED_ARCHIVE_NAMES or Path(clean).name in EXCLUDED_ARCHIVE_NAMES:
        return False
    return not any(clean.startswith(prefix) for prefix in EXCLUDED_ARCHIVE_PREFIXES)


def _git_tracked_files(repo: Path) -> list[str]:
    out = _run_git(["ls-files", "-z"], cwd=repo)
    return [p for p in out.split("\0") if p]


def create_source_archive(
    repo: Path,
    output: Path,
    *,
    tracked_files: Iterable[str] | None = None,
    identity: ReleaseIdentity,
) -> dict[str, Any]:
    repo = repo.resolve()
    files = list(tracked_files) if tracked_files is not None else _git_tracked_files(repo)
    output.parent.mkdir(parents=True, exist_ok=True)
    record = {"schema_version": 1, **asdict(identity)}
    with tarfile.open(output, "w:gz") as tar:
        for rel in sorted(files):
            if not _archive_allowed(rel):
                continue
            path = (repo / rel).resolve()
            if not path.is_file() or repo not in path.parents:
                continue
            tar.add(path, arcname=rel, recursive=False)
        data = json.dumps(record, sort_keys=True, indent=2).encode()
        info = tarfile.TarInfo("release-record.json")
        info.size = len(data)
        info.mode = 0o644
        import io

        tar.addfile(info, io.BytesIO(data))
    digest = hashlib.sha256(output.read_bytes()).hexdigest()
    return {**record, "archive": str(output), "sha256": digest}


def safe_extract_archive(archive: Path, destination: Path) -> None:
    destination.mkdir(parents=True, exist_ok=True)
    dest = destination.resolve()
    with tarfile.open(archive, "r:gz") as tar:
        for member in tar.getmembers():
            target = (dest / member.name).resolve()
            if target != dest and dest not in target.parents:
                raise ArchiveValidationError(f"unsafe archive path: {member.name}")
            if member.issym() or member.islnk():
                link_target = Path(member.linkname)
                resolved = link_target if link_target.is_absolute() else (target.parent / link_target)
                resolved = resolved.resolve()
                if resolved != dest and dest not in resolved.parents:
                    raise ArchiveValidationError(f"unsafe symlink: {member.name}")
        tar.extractall(dest)


def validate_image_manifest(
    manifest: dict[str, Any], *, expected_commit: str, expected_digest: str
) -> dict[str, str]:
    if not DIGEST_RE.fullmatch(expected_digest):
        raise ImageValidationError(f"invalid digest: {expected_digest}")
    annotations = manifest.get("annotations") or {}
    revision = annotations.get("org.opencontainers.image.revision")
    if revision != expected_commit:
        raise ImageValidationError("image revision does not match expected commit")
    media_type = manifest.get("mediaType", "")
    if media_type.endswith("image.index.v1+json") or media_type.endswith(
        "manifest.list.v2+json"
    ):
        manifests = manifest.get("manifests") or []
        has_amd64 = any(
            (m.get("platform") or {}).get("os") == "linux"
            and (m.get("platform") or {}).get("architecture") == "amd64"
            for m in manifests
        )
    elif media_type.endswith("image.manifest.v1+json") or media_type.endswith(
        "manifest.v2+json"
    ):
        config = manifest.get("config") or {}
        platform = manifest.get("platform") or {}
        has_amd64 = platform in ({}, {"os": "linux", "architecture": "amd64"}) or (
            platform.get("os") == "linux" and platform.get("architecture") == "amd64"
        )
        if config.get("digest") and not DIGEST_RE.fullmatch(config["digest"]):
            raise ImageValidationError("image config digest has unknown format")
    else:
        raise ImageValidationError(f"unsupported manifest mediaType: {media_type!r}")
    if not has_amd64:
        raise ImageValidationError("image manifest does not contain linux/amd64")
    return {"digest": expected_digest, "revision": expected_commit, "platform": "linux/amd64"}


class FileReleaseStateStore:
    def __init__(self, path: Path):
        self.path = path

    def _read(self) -> dict[str, Any]:
        if not self.path.exists():
            return {"environments": {}}
        return json.loads(self.path.read_text())

    def _write(self, state: dict[str, Any]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=str(self.path.parent), prefix=self.path.name, text=True)
        with os.fdopen(fd, "w") as fh:
            json.dump(state, fh, sort_keys=True, indent=2)
        os.replace(tmp, self.path)

    def acquire(self, environment: str, identity: ReleaseIdentity, digest: str) -> None:
        if not DIGEST_RE.fullmatch(digest):
            raise DeployStateError("invalid image digest")
        state = self._read()
        envs = state.setdefault("environments", {})
        current = envs.get(environment, {})
        lock = current.get("lock")
        if lock and lock.get("pipeline_id") != identity.pipeline_id:
            raise DeployStateError(f"environment {environment} already owned")
        success = current.get("success")
        if success and _version_key(identity.tag) < _version_key(success["tag"]):
            raise DeployStateError("candidate release is older than successful release")
        current["lock"] = {**asdict(identity), "digest": digest}
        envs[environment] = current
        self._write(state)

    def mark_success(self, environment: str, pipeline_id: str) -> None:
        state = self._read()
        current = state.setdefault("environments", {}).setdefault(environment, {})
        lock = current.get("lock")
        if not lock or lock.get("pipeline_id") != pipeline_id:
            raise DeployStateError("release lock is not owned by this pipeline")
        current["success"] = lock
        current.pop("lock", None)
        self._write(state)


def _version_key(tag: str) -> tuple[int, int, int, int]:
    match = RELEASE_TAG_RE.fullmatch(tag)
    if not match:
        return (0, 0, 0, 0)
    nums = tuple(int(part) for part in match.group("version").split("."))
    suffix_rank = {"-test": 0, "-pre": 1, None: 2}[match.group("suffix")]
    return (*nums, suffix_rank)


def write_registry_auth(directory: Path, registry: str, username: str, password: str) -> Path:
    if not registry or not username or not password:
        raise ValueError("registry credentials must be non-empty")
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / "config.json"
    token = base64.b64encode(f"{username}:{password}".encode()).decode()
    data = {"auths": {registry: {"username": username, "password": password, "auth": token}}}
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as fh:
        json.dump(data, fh)
    os.chmod(path, 0o600)
    return path


def mask_secret(text: str, secret: str) -> str:
    return text.replace(secret, "***") if secret else text




def validate_source_archive_record(
    archive: Path, record: dict[str, Any], *, expected: ReleaseIdentity
) -> None:
    checksum = hashlib.sha256(archive.read_bytes()).hexdigest()
    if record.get("sha256") != checksum:
        raise ArchiveValidationError("source archive checksum mismatch")
    for key in ("repo", "tag", "commit", "pipeline_id", "deploy_environment"):
        if record.get(key) != getattr(expected, key):
            raise ArchiveValidationError(f"source archive identity mismatch: {key}")


class FileReleaseRecordStore:
    """Append-by-version release records with idempotent same-source retries."""

    def __init__(self, directory: Path):
        self.directory = directory

    def publish(
        self, identity: ReleaseIdentity, *, source_sha256: str, image_digest: str
    ) -> dict[str, Any]:
        if not re.fullmatch(r"[0-9a-f]{64}", source_sha256):
            raise ReleaseIdentityError("invalid source archive sha256")
        if not DIGEST_RE.fullmatch(image_digest):
            raise ReleaseIdentityError("invalid image digest")
        self.directory.mkdir(parents=True, exist_ok=True)
        path = self.directory / f"{identity.tag}.json"
        record = {
            "schema_version": 1,
            **asdict(identity),
            "source_sha256": source_sha256,
            "image_digest": image_digest,
        }
        if path.exists():
            existing = json.loads(path.read_text())
            if existing != record:
                raise ReleaseIdentityError("conflicting release record already exists")
            return existing
        path.write_text(json.dumps(record, sort_keys=True, indent=2) + "\n")
        return record


def buildkit_command(
    *, tag: str, commit: str, auth_file: Path, image: str, cache_ref: str
) -> list[str]:
    parse_release_tag(tag)
    if not auth_file.is_file():
        raise FileNotFoundError(auth_file)
    return [
        "buildctl-daemonless.sh",
        "build",
        "--frontend",
        "dockerfile.v0",
        "--local",
        "context=.",
        "--local",
        "dockerfile=.",
        "--opt",
        "platform=linux/amd64",
        "--opt",
        f"label:org.opencontainers.image.source=https://github.com/{os.getenv('CI_REPO', 'minwang/LightRAG')}",
        "--opt",
        f"label:org.opencontainers.image.revision={commit}",
        "--export-cache",
        f"type=registry,ref={cache_ref},mode=max",
        "--import-cache",
        f"type=registry,ref={cache_ref}",
        "--output",
        f"type=image,name={image}:{tag},push=true,registry.config={auth_file}",
    ]

def validate_test_environment_snapshot(snapshot: dict[str, Any]) -> dict[str, Any]:
    """Validate pre-deploy facts collected from the test namespace.

    The caller supplies already-sanitized facts from kubectl/Kustomize/storage checks;
    this function only decides whether an automatic tag deployment may proceed.
    """
    if snapshot.get("cluster") != "test":
        raise DeployStateError("target cluster must be test")
    if snapshot.get("namespace") != "lightrag-test":
        raise DeployStateError("target namespace must be lightrag-test")
    if snapshot.get("initialized") is not True:
        raise DeployStateError("test environment is not initialized")
    profile = snapshot.get("profile") or {}
    required_profile = {
        "replicas": 2,
        "workers": 1,
        "kv_storage": "PGKVStorage",
        "doc_status_storage": "PGDocStatusStorage",
        "vector_storage": "PGVectorStorage",
        "graph_storage": "HugeGraphStorage",
    }
    for key, expected in required_profile.items():
        if profile.get(key) != expected:
            raise DeployStateError(f"incompatible profile: {key}")
    if not profile.get("workspace") or profile.get("workspace") != profile.get(
        "postgres_workspace"
    ):
        raise DeployStateError("workspace and postgres workspace must match")
    storage = snapshot.get("storage_state") or {}
    unsafe = {
        "fenced": bool(storage.get("fenced")),
        "active_operations": int(storage.get("active_operations", 0) or 0) > 0,
        "pending_mutations": int(storage.get("pending_mutations", 0) or 0) > 0,
        "orphaned_claims": int(storage.get("orphaned_claims", 0) or 0) > 0,
    }
    if any(unsafe.values()):
        raise DeployStateError("unsafe storage state prevents automatic deployment")
    return snapshot

def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="cmd", required=True)

    v = sub.add_parser("verify-source")
    v.add_argument("--tag", required=True)
    v.add_argument("--commit", required=True)
    v.add_argument("--repo", required=True)
    v.add_argument("--allowed-repo", required=True)
    v.add_argument("--pipeline-id", required=True)

    archive = sub.add_parser("archive-source")
    archive.add_argument("--repo", type=Path, default=Path("."))
    archive.add_argument("--output", type=Path, required=True)
    archive.add_argument("--identity", type=Path, required=True)
    archive.add_argument("--record-output", type=Path, required=True)

    verify_archive = sub.add_parser("verify-archive")
    verify_archive.add_argument("--archive", type=Path, required=True)
    verify_archive.add_argument("--record", type=Path, required=True)
    verify_archive.add_argument("--identity", type=Path, required=True)
    verify_archive.add_argument("--extract-to", type=Path, required=True)

    build = sub.add_parser("build-image")
    build.add_argument("--tag", required=True)
    build.add_argument("--commit", required=True)
    build.add_argument("--image", default="docker-hub.f123.pub/lfun/lightrag")
    build.add_argument("--cache-ref", default="docker-hub.f123.pub/lfun/lightrag:buildcache")
    build.add_argument("--registry", default="docker-hub.f123.pub")
    build.add_argument("--username", default=os.getenv("REGISTRY_USERNAME"))
    build.add_argument("--password", default=os.getenv("REGISTRY_PASSWORD"))

    validate_image = sub.add_parser("validate-image")
    validate_image.add_argument("--manifest", type=Path, required=True)
    validate_image.add_argument("--commit", required=True)
    validate_image.add_argument("--digest", required=True)
    validate_image.add_argument("--record-output", type=Path, required=True)

    args = parser.parse_args(argv)
    if args.cmd == "verify-source":
        ident = verify_release_source(
            tag=args.tag,
            event_commit=args.commit,
            repo=args.repo,
            allowed_repo=args.allowed_repo,
            pipeline_id=args.pipeline_id,
        )
        print(json.dumps(asdict(ident), sort_keys=True))
    elif args.cmd == "archive-source":
        identity = ReleaseIdentity(**json.loads(args.identity.read_text()))
        record = create_source_archive(args.repo, args.output, identity=identity)
        args.record_output.write_text(json.dumps(record, sort_keys=True, indent=2) + "\n")
        print(json.dumps(record, sort_keys=True))
    elif args.cmd == "verify-archive":
        identity = ReleaseIdentity(**json.loads(args.identity.read_text()))
        record = json.loads(args.record.read_text())
        validate_source_archive_record(args.archive, record, expected=identity)
        safe_extract_archive(args.archive, args.extract_to)
        print(json.dumps({"verified": True, "extract_to": str(args.extract_to)}, sort_keys=True))
    elif args.cmd == "build-image":
        if not args.username or not args.password:
            raise ValueError("registry credentials must be provided")
        with tempfile.TemporaryDirectory() as tmp:
            auth = write_registry_auth(Path(tmp), args.registry, args.username, args.password)
            subprocess.run(
                buildkit_command(
                    tag=args.tag,
                    commit=args.commit,
                    auth_file=auth,
                    image=args.image,
                    cache_ref=args.cache_ref,
                ),
                check=True,
            )
    elif args.cmd == "validate-image":
        record = validate_image_manifest(
            json.loads(args.manifest.read_text()),
            expected_commit=args.commit,
            expected_digest=args.digest,
        )
        args.record_output.write_text(json.dumps(record, sort_keys=True, indent=2) + "\n")
        print(json.dumps(record, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
