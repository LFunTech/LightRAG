import importlib.util
import json
from pathlib import Path
import subprocess
import sys

import pytest


def module():
    from scripts.ci import prod_config

    return prod_config


def test_prod_mapping_parses_inventory_without_executing_it(tmp_path):
    p = tmp_path / "prod.secrets"
    marker = tmp_path / "must-not-exist"
    p.write_text(
        f"""百炼\n- "API KEY": "literal-$(touch {marker})"\nAPI Host=https://model.invalid/v1\n地域：cn-example\n业务空间ID: work\nPostgreSQL\nIP: pg.invalid\n端口: 5432\n用户: app\n数据库: prod\n密码: "p:a=s s"\nlightrag_test_03_api_key: chosen\n"""
    )
    m = module()
    aliases = m.secret_aliases("prod", api_key_alias="lightrag_test_03_api_key")
    result = m.parse_secret_inventory(p, environment="prod", aliases=aliases)
    assert result["lightrag_prod_llm_api_key"].startswith("literal-$(")
    assert result["lightrag_prod_postgres_password"] == "p:a=s s"
    assert result["lightrag_prod_api_key"] == "chosen"
    assert all(k.startswith("lightrag_prod_") for k in result)
    assert not marker.exists()


def test_prod_missing_value_never_reads_test_file(tmp_path):
    (tmp_path / "test.secrets").write_text("API KEY=do-not-use")
    prod = tmp_path / "prod.secrets"
    prod.write_text("API Host=https://model.invalid/v1")
    m = module()
    with pytest.raises(m.ProdConfigError, match="llm_api_key") as caught:
        m.parse_secret_inventory(
            prod,
            environment="prod",
            aliases=m.secret_aliases("prod", api_key_alias="lightrag_test_api_key"),
            required={"llm_api_key"},
        )
    assert "do-not-use" not in str(caught.value)
    with pytest.raises(m.ProdConfigError, match="source"):
        m.parse_secret_inventory(
            tmp_path / "test.secrets", environment="prod", aliases={}
        )


@pytest.mark.parametrize(
    "body",
    ["API KEY=never-print\nAPI KEY=different-secret", 'API KEY="unterminated-secret'],
)
def test_conflicting_or_malformed_inventory_redacts_values(tmp_path, body):
    p = tmp_path / "prod.secrets"
    p.write_text(body)
    m = module()
    with pytest.raises(m.ProdConfigError) as caught:
        m.parse_secret_inventory(
            p, environment="prod", aliases={"API KEY": "llm_api_key"}
        )
    assert "never-print" not in str(caught.value)
    assert "different-secret" not in str(caught.value)
    assert "unterminated-secret" not in str(caught.value)


def test_environment_prefix_cannot_be_overridden(tmp_path):
    p = tmp_path / "prod.secrets"
    p.write_text("API KEY=a")
    m = module()
    with pytest.raises(m.ProdConfigError):
        m.parse_secret_inventory(
            p, environment="prod", aliases={"API KEY": "lightrag_test_llm_api_key"}
        )


def test_secret_apply_is_explicit_and_restricts_events_and_images(tmp_path):
    spec = importlib.util.spec_from_file_location(
        "inventory_import", Path("scripts/ci/import-environment-secrets.py")
    )
    assert spec and spec.loader
    importer = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(importer)
    calls = []

    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def read(self):
            return b"[]"

    class Opener:
        def open(self, request, timeout):
            calls.append(request)
            return Response()

    importer.apply_secrets(
        {"lightrag_prod_api_key": "hidden"},
        environment="prod",
        server="https://ci.invalid",
        repo_id=7,
        token="not-printed",
        images=["registry.invalid/tools@sha256:" + "a" * 64],
        opener=Opener(),
    )
    assert [r.method for r in calls] == ["GET", "POST"]
    data = json.loads(calls[1].data)
    assert data["events"] == ["tag"]
    assert data["images"] == ["registry.invalid/tools@sha256:" + "a" * 64]
    assert data["name"] == "lightrag_prod_api_key"
    with pytest.raises(module().ProdConfigError):
        importer.apply_secrets(
            {"lightrag_test_api_key": "hidden"},
            environment="prod",
            server="https://ci.invalid",
            repo_id=7,
            token="not-printed",
            images=["tools"],
            opener=Opener(),
        )
    assert len(calls) == 2


def test_import_cli_dry_run_never_displays_values(tmp_path):
    p = tmp_path / "prod.secrets"
    p.write_text("API KEY=hide-this-value\nlightrag_test_api_key=also-hide")
    result = subprocess.run(
        [
            sys.executable,
            "scripts/ci/import-environment-secrets.py",
            "--environment",
            "prod",
            "--input",
            str(p),
            "--api-key-alias",
            "lightrag_test_api_key",
        ],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
    assert "lightrag_prod_api_key" in result.stdout
    assert "hide-this-value" not in result.stdout + result.stderr
    assert "also-hide" not in result.stdout + result.stderr


@pytest.mark.parametrize(
    "value", ["https://[bad", "https://user:secret@host", "https://host/#secret"]
)
def test_invalid_endpoint_diagnostics_never_include_input(tmp_path, value):
    p = tmp_path / "prod.secrets"
    p.write_text("API Host=" + value)
    m = module()
    with pytest.raises(m.ProdConfigError) as caught:
        m.parse_secret_inventory(
            p, environment="prod", aliases={"API Host": "llm_binding_host"}
        )
    assert value not in str(caught.value)
