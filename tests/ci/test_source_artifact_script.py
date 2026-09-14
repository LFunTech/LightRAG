import shlex
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts/ci/source-artifact.sh"


def resolve_endpoint(value: str) -> str:
    command = f". {shlex.quote(str(SCRIPT))}; resolve_storage_endpoint {shlex.quote(value)}"
    result = subprocess.run(
        ["sh", "-c", command],
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()


def test_storage_endpoint_resolution_accepts_plain_host():
    assert (
        resolve_endpoint("cos.ap-guangzhou.myqcloud.com")
        == "https://cos.ap-guangzhou.myqcloud.com"
    )


def test_storage_endpoint_resolution_strips_bucket_or_path_from_secret_value():
    assert (
        resolve_endpoint("https://cos.ap-guangzhou.myqcloud.com/lightrag-artifacts")
        == "https://cos.ap-guangzhou.myqcloud.com"
    )


def test_storage_endpoint_resolution_rejects_scheme_without_host():
    command = f". {shlex.quote(str(SCRIPT))}; resolve_storage_endpoint https://"
    result = subprocess.run(
        ["sh", "-c", command],
        capture_output=True,
        text=True,
    )
    assert result.returncode != 0
    assert "storage endpoint host is empty" in result.stderr
