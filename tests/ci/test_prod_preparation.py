"""Production input preparation never changes test mappings or starts services."""

import json
from pathlib import Path
import subprocess
import sys

from tests.ci.test_prod_config import config_data, load


def test_prod_s3_labels_match_the_provided_inventory_without_test_fallback(tmp_path):
    from lightrag_delivery.config import parse_secret_inventory, secret_aliases

    source = tmp_path / "prod.secrets"
    source.write_text("S3_ENDPOINT_URL: https://objects.invalid\nS3_BUCKET: prod\n")
    values = parse_secret_inventory(
        source,
        environment="prod",
        aliases=secret_aliases("prod", api_key_alias="lightrag_test_api_key"),
        required={"s3_endpoint_url", "s3_bucket"},
    )
    assert values == {
        "lightrag_prod_s3_endpoint_url": "https://objects.invalid",
        "lightrag_prod_s3_bucket": "prod",
    }
    test_aliases = secret_aliases("test", api_key_alias="lightrag_test_api_key")
    assert "S3_ENDPOINT_URL" not in test_aliases and "S3_BUCKET" not in test_aliases


def test_prod_initial_working_directories_are_created_before_application_start(
    tmp_path,
):
    from lightrag_delivery.render import render_slot

    config = load(tmp_path, config_data())
    resources = render_slot(
        config,
        {"config_hash": "a" * 64, "digest": "sha256:" + "b" * 64},
        "blue",
        image="registry.invalid/rag",
        environment={},
    )
    pod = resources[-1]["spec"]["template"]["spec"]
    init = pod["initContainers"][0]
    assert init["image"] == pod["containers"][0]["image"]
    assert init["securityContext"]["readOnlyRootFilesystem"] is True
    assert init.get("envFrom", []) == []
    assert init["volumeMounts"] == [{"name": "working", "mountPath": "/app/data"}]
    command = init["command"]
    assert command[:3] == ["python", "-I", "-c"]
    paths = [str(tmp_path / Path(p).relative_to("/app/data")) for p in command[4:]]
    result = subprocess.run(
        [sys.executable, "-I", "-c", command[3], *paths], capture_output=True, text=True
    )
    assert result.returncode == 0, result.stderr
    assert all(Path(p).is_dir() for p in paths)
    assert {Path(p).name for p in paths} == {
        "logs",
        "rag_storage",
        "inputs",
        "object-scratch",
    }
    again = subprocess.run(
        [sys.executable, "-I", "-c", command[3], *paths], capture_output=True, text=True
    )
    assert again.returncode == 0
    assert "lightrag" not in command[3].lower()
    assert "secret" not in json.dumps(init).lower()
