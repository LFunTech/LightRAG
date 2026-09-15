import importlib.util
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts/ci/generate-test-instance-api-keys.py"


def load_module():
    spec = importlib.util.spec_from_file_location("generate_test_instance_api_keys", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_generate_instance_api_keys_appends_missing_numbered_keys_without_overwriting(tmp_path):
    module = load_module()
    secrets = tmp_path / "test.secrets"
    existing_value = "x" * 64
    secrets.write_text(
        "# LightRAG test WebUI/API access\n"
        f"lightrag_test_01_api_key={existing_value}\n",
        encoding="utf-8",
    )

    result = module.ensure_instance_api_keys(
        secrets,
        instances=["01", "02", "05"],
        key_bytes=32,
    )

    values = module.read_key_values(secrets)
    assert values["lightrag_test_01_api_key"] == existing_value
    assert set(result.created) == {"lightrag_test_02_api_key", "lightrag_test_05_api_key"}
    assert result.existing == ["lightrag_test_01_api_key"]
    for name in result.created:
        assert len(values[name]) == 64
        assert values[name] != existing_value


def test_generate_instance_api_keys_rejects_invalid_instance_ids(tmp_path):
    module = load_module()
    secrets = tmp_path / "test.secrets"
    secrets.write_text("", encoding="utf-8")

    try:
        module.ensure_instance_api_keys(secrets, instances=["01", "../02"])
    except ValueError as exc:
        assert "invalid instance id" in str(exc)
    else:
        raise AssertionError("invalid instance id was accepted")
