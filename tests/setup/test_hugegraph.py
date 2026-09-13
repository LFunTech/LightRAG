"""HugeGraph wizard registration, external-service configuration, and validation."""

from __future__ import annotations

import shlex
from pathlib import Path

import pytest
import yaml
from dotenv import dotenv_values

from tests.setup._helpers import REPO_ROOT, run_bash, run_bash_lines, write_text_lines

pytestmark = pytest.mark.offline


def test_storage_wizard_selects_external_hugegraph_and_preserves_other_settings(
    tmp_path: Path,
) -> None:
    """A graph selection must dispatch the external collector without creating services."""
    write_text_lines(
        tmp_path / ".env",
        [
            "LIGHTRAG_KV_STORAGE=JsonKVStorage",
            "LIGHTRAG_VECTOR_STORAGE=NanoVectorDBStorage",
            "LIGHTRAG_GRAPH_STORAGE=NetworkXStorage",
            "LIGHTRAG_DOC_STATUS_STORAGE=JsonDocStatusStorage",
            "HUGEGRAPH_URI=http://localhost:18080",
            "HUGEGRAPH_GRAPH=private_graph",
            "HUGEGRAPH_GRAPHSPACE=TEAM",
            "HUGEGRAPH_TIMEOUT=12.5",
            "HUGEGRAPH_BATCH_SIZE=27",
            "HUGEGRAPH_MAX_CONNECTIONS=3",
            "HUGEGRAPH_RETRIES=0",
            "HUGEGRAPH_AUTO_CREATE_SCHEMA=false",
            "LLM_BINDING=openai",
            "LLM_MODEL=preserve-me",
        ],
    )
    (tmp_path / "env.example").write_text(
        (REPO_ROOT / "env.example").read_text(encoding="utf-8"), encoding="utf-8"
    )
    values = run_bash_lines(f"""
set -euo pipefail
source "{REPO_ROOT}/scripts/setup/setup.sh"
REPO_ROOT="{tmp_path}"
prompt_choice() {{
  if [[ "$1" == "Graph storage" ]]; then
    shift 2
    for option in "$@"; do
      if [[ "$option" == "HugeGraphStorage" ]]; then
        printf '%s' "$option"
        return
      fi
    done
    printf 'HugeGraphStorage is not selectable' >&2
    return 1
  fi
  printf '%s' "$2"
}}
prompt_with_default() {{ printf '%s' "$2"; }}
prompt_secret_with_default() {{ printf '%s' "$2"; }}
confirm_required_yes_no() {{ return 0; }}
confirm_default_no() {{ return 1; }}
env_storage_flow
printf 'SERVICE_COUNT=%s\\n' "${{#DOCKER_SERVICES[@]}}"
""")
    config = dotenv_values(tmp_path / ".env")
    assert config["LIGHTRAG_GRAPH_STORAGE"] == "HugeGraphStorage"
    assert config["HUGEGRAPH_URI"] == "http://localhost:18080"
    assert config["HUGEGRAPH_GRAPH"] == "private_graph"
    assert config["HUGEGRAPH_GRAPHSPACE"] == "TEAM"
    assert config["HUGEGRAPH_TIMEOUT"] == "12.5"
    assert config["HUGEGRAPH_BATCH_SIZE"] == "27"
    assert config["HUGEGRAPH_MAX_CONNECTIONS"] == "3"
    assert config["HUGEGRAPH_RETRIES"] == "0"
    assert config["HUGEGRAPH_AUTO_CREATE_SCHEMA"] == "false"
    assert config["LLM_MODEL"] == "preserve-me"
    assert values["SERVICE_COUNT"] == "0"
    assert not (tmp_path / "docker-compose.final.yml").exists()


@pytest.mark.parametrize(
    ("auth_mode", "expected_auth"),
    [
        (
            "none",
            {"HUGEGRAPH_USERNAME": "", "HUGEGRAPH_PASSWORD": "", "HUGEGRAPH_TOKEN": ""},
        ),
        (
            "basic",
            {
                "HUGEGRAPH_USERNAME": "operator",
                "HUGEGRAPH_PASSWORD": "private-password",
                "HUGEGRAPH_TOKEN": "",
            },
        ),
        (
            "token",
            {
                "HUGEGRAPH_USERNAME": "",
                "HUGEGRAPH_PASSWORD": "",
                "HUGEGRAPH_TOKEN": "private-token",
            },
        ),
    ],
)
def test_hugegraph_collector_switches_auth_without_stale_credentials(
    auth_mode: str, expected_auth: dict[str, str]
) -> None:
    """Switching auth mode must remove the competing credentials from the saved env."""
    values = run_bash_lines(f"""
set -euo pipefail
source "{REPO_ROOT}/scripts/setup/setup.sh"
reset_state
ENV_VALUES[HUGEGRAPH_USERNAME]=operator
ENV_VALUES[HUGEGRAPH_PASSWORD]=private-password
ENV_VALUES[HUGEGRAPH_TOKEN]=private-token
prompt_choice() {{
  if [[ "$1" == "HugeGraph authentication" ]]; then printf '%s' '{auth_mode}';
  else printf '%s' "$2"; fi
}}
prompt_with_default() {{ printf '%s' "$2"; }}
prompt_secret_with_default() {{ printf '%s' "$2"; }}
collect_database_config hugegraph yes
for key in HUGEGRAPH_URI HUGEGRAPH_GRAPH HUGEGRAPH_GRAPHSPACE HUGEGRAPH_USERNAME HUGEGRAPH_PASSWORD HUGEGRAPH_TOKEN HUGEGRAPH_TIMEOUT HUGEGRAPH_BATCH_SIZE HUGEGRAPH_MAX_CONNECTIONS HUGEGRAPH_RETRIES HUGEGRAPH_AUTO_CREATE_SCHEMA; do
  printf '%s=%s\\n' "$key" "${{ENV_VALUES[$key]}}"
done
printf 'SERVICE_COUNT=%s\\n' "${{#DOCKER_SERVICES[@]}}"
""")
    assert {key: values[key] for key in expected_auth} == expected_auth
    assert values["HUGEGRAPH_URI"] == "http://localhost:8080"
    assert values["HUGEGRAPH_GRAPH"] == "hugegraph"
    assert values["HUGEGRAPH_GRAPHSPACE"] == "DEFAULT"
    assert values["HUGEGRAPH_TIMEOUT"] == "30"
    assert values["HUGEGRAPH_BATCH_SIZE"] == "100"
    assert values["HUGEGRAPH_MAX_CONNECTIONS"] == "10"
    assert values["HUGEGRAPH_RETRIES"] == "2"
    assert values["HUGEGRAPH_AUTO_CREATE_SCHEMA"] == "true"
    assert values["SERVICE_COUNT"] == "0"


@pytest.mark.parametrize(
    ("extra", "valid"),
    [
        ([], True),
        (["HUGEGRAPH_USERNAME=operator", "HUGEGRAPH_PASSWORD=private"], True),
        (["HUGEGRAPH_TOKEN=private-token"], True),
        (["HUGEGRAPH_TIMEOUT=0.5", "HUGEGRAPH_RETRIES=0"], True),
        (["HUGEGRAPH_TIMEOUT=1e2", "HUGEGRAPH_AUTO_CREATE_SCHEMA=FALSE"], True),
        (["HUGEGRAPH_URI=http://[::1]:8080/proxy"], True),
        (["HUGEGRAPH_URI=http://localhost:99999"], False),
        (["HUGEGRAPH_URI=http:///missing-host"], False),
        (["HUGEGRAPH_GRAPH=.."], False),
        (["HUGEGRAPH_GRAPHSPACE=   "], False),
        (["HUGEGRAPH_URI="], False),
        (["HUGEGRAPH_URI=bolt://localhost:8080"], False),
        (["HUGEGRAPH_URI=http://user:password@localhost:8080"], False),
        (["HUGEGRAPH_URI=http://localhost:8080/?token=private"], False),
        (["HUGEGRAPH_USERNAME=operator"], False),
        (["HUGEGRAPH_PASSWORD=private"], False),
        (
            [
                "HUGEGRAPH_USERNAME=operator",
                "HUGEGRAPH_PASSWORD=private",
                "HUGEGRAPH_TOKEN=private-token",
            ],
            False,
        ),
        (["HUGEGRAPH_TIMEOUT=0"], False),
        (["HUGEGRAPH_TIMEOUT=nan"], False),
        (["HUGEGRAPH_BATCH_SIZE=0"], False),
        (["HUGEGRAPH_MAX_CONNECTIONS=-1"], False),
        (["HUGEGRAPH_RETRIES=-1"], False),
        (["HUGEGRAPH_RETRIES=11"], False),
        (["HUGEGRAPH_TIMEOUT=1e309"], False),
        (["HUGEGRAPH_AUTO_CREATE_SCHEMA=perhaps"], False),
    ],
)
def test_validate_env_file_checks_selected_hugegraph(
    tmp_path: Path, extra: list[str], valid: bool
) -> None:
    """The same setup validation entry point must reject invalid HugeGraph settings."""
    write_text_lines(
        tmp_path / ".env",
        [
            "LIGHTRAG_KV_STORAGE=JsonKVStorage",
            "LIGHTRAG_VECTOR_STORAGE=NanoVectorDBStorage",
            "LIGHTRAG_GRAPH_STORAGE=HugeGraphStorage",
            "LIGHTRAG_DOC_STATUS_STORAGE=JsonDocStatusStorage",
            "HUGEGRAPH_URI=https://graphs.example.com/proxy",
            *extra,
        ],
    )
    values = run_bash_lines(f"""
set -euo pipefail
source "{REPO_ROOT}/scripts/setup/setup.sh"
REPO_ROOT="{tmp_path}"
if validate_env_file; then printf 'VALID=yes\\n'; else printf 'VALID=no\\n'; fi
""")
    assert values["VALID"] == ("yes" if valid else "no")


def test_hugegraph_external_uri_survives_compose_generation(tmp_path: Path) -> None:
    """A remote HugeGraph selection must not inject a nonexistent managed endpoint."""
    compose_path = tmp_path / "docker-compose.final.yml"
    env_path = tmp_path / ".env"
    run_bash(f"""
set -euo pipefail
source "{REPO_ROOT}/scripts/setup/setup.sh"
reset_state
ENV_VALUES[HUGEGRAPH_URI]=https://graphs.example.com/proxy
prompt_choice() {{ printf '%s' "$2"; }}
prompt_with_default() {{ printf '%s' "$2"; }}
prompt_secret_with_default() {{ printf '%s' "$2"; }}
collect_database_config hugegraph yes
prepare_compose_env_overrides
generate_env_file "$REPO_ROOT/env.example" {shlex.quote(str(env_path))}
generate_docker_compose {shlex.quote(str(compose_path))}
""")
    assert (
        dotenv_values(env_path)["HUGEGRAPH_URI"] == "https://graphs.example.com/proxy"
    )
    compose = yaml.safe_load(compose_path.read_text(encoding="utf-8"))
    assert set(compose["services"]) == {"lightrag"}
    assert "HUGEGRAPH_URI" not in compose["services"]["lightrag"].get("environment", {})


def test_hugegraph_collector_keeps_verify_only_with_uppercase_saved_boolean() -> None:
    """An accepted FALSE setting must remain a selectable false default on rerun."""
    values = run_bash_lines(f"""
set -euo pipefail
source "{REPO_ROOT}/scripts/setup/setup.sh"
reset_state
ENV_VALUES[HUGEGRAPH_AUTO_CREATE_SCHEMA]=FALSE
prompt_with_default() {{ printf '%s' "$2"; }}
prompt_choice() {{
  local default="$2"
  shift 2
  for option in "$@"; do
    if [[ "$option" == "$default" ]]; then printf '%s' "$option"; return 0; fi
  done
  printf 'INVALID_DEFAULT'
}}
collect_database_config hugegraph
printf 'AUTO_CREATE=%s\\n' "${{ENV_VALUES[HUGEGRAPH_AUTO_CREATE_SCHEMA]}}"
""")
    assert values["AUTO_CREATE"] == "false"
