"""Public backend selection and environment validation for HugeGraph."""

import pytest

from lightrag.kg import verify_storage_implementation
from lightrag.kg.factory import get_storage_class
from lightrag.utils import check_storage_env_vars

pytestmark = pytest.mark.offline


def test_hugegraph_is_selectable_only_as_graph_storage() -> None:
    verify_storage_implementation("GRAPH_STORAGE", "HugeGraphStorage")
    with pytest.raises(ValueError, match="not compatible"):
        verify_storage_implementation("KV_STORAGE", "HugeGraphStorage")


def test_hugegraph_required_environment_is_uri_only(monkeypatch) -> None:
    for key in (
        "HUGEGRAPH_URI",
        "HUGEGRAPH_USERNAME",
        "HUGEGRAPH_PASSWORD",
        "HUGEGRAPH_TOKEN",
    ):
        monkeypatch.delenv(key, raising=False)
    with pytest.raises(ValueError, match="HUGEGRAPH_URI"):
        check_storage_env_vars("HugeGraphStorage")
    monkeypatch.setenv("HUGEGRAPH_URI", "http://localhost:8080")
    check_storage_env_vars("HugeGraphStorage")


def test_factory_loads_hugegraph_backend() -> None:
    storage_class = get_storage_class("HugeGraphStorage")
    assert storage_class.__module__ == "lightrag.kg.hugegraph_impl"
    assert storage_class.__name__ == "HugeGraphStorage"
