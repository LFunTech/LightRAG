import json
import shutil
import subprocess

import pytest

from tests.ci.test_prod_config import config_data, load


def test_full_generated_production_overlay_is_valid_kustomize_without_live_defaults(
    tmp_path,
):
    from lightrag_delivery.render import (
        render_entrypoint,
        render_foundation,
        render_slot,
        write_overlay,
    )

    if not shutil.which("kubectl"):
        pytest.skip("kubectl is required for local offline Kustomize validation")
    config = load(tmp_path, config_data())
    identity = {
        "scope": "lightrag_prod/lightrag_prod",
        "release_id": "fixture",
        "tag": "v1.0.0",
        "commit": "a" * 40,
        "digest": "sha256:" + "b" * 64,
        "config_hash": "c" * 64,
    }
    resources = render_foundation(config) + render_entrypoint(config, "blue")
    resources += render_slot(
        config,
        identity,
        "blue",
        image="registry.invalid/rag",
        environment={"LIGHTRAG_API_KEY": "synthetic-private"},
    )
    resources += render_slot(
        config,
        identity,
        "green",
        image="registry.invalid/rag",
        environment={"LIGHTRAG_API_KEY": "synthetic-private"},
    )
    overlay = tmp_path / "generated"
    write_overlay(overlay, resources)
    result = subprocess.run(
        ["kubectl", "kustomize", str(overlay)],
        text=True,
        capture_output=True,
        check=True,
    )
    import yaml

    rendered = list(yaml.safe_load_all(result.stdout))
    deployments = [v for v in rendered if v["kind"] == "Deployment"]
    assert (
        len(deployments) == 2 and sum(v["spec"]["replicas"] for v in deployments) == 8
    )
    assert "nginx.ingress.kubernetes.io" not in result.stdout
    assert "lightrag_test" not in result.stdout
    assert "syno-nfs" not in result.stdout
    assert json.loads((overlay / "kustomization.yaml").read_text())["resources"] == [
        "resources.json"
    ]
    assert (overlay / "resources.json").stat().st_mode & 0o777 == 0o600
