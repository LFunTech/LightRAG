import pytest
from tests.api.test_health_auth import (
    _FakeLightRAG,
    _build_client,
    _set_auth_mode,
    _isolate_env,
)


@pytest.fixture(autouse=True)
def isolate(monkeypatch):
    yield from _isolate_env.__wrapped__(monkeypatch)


def test_query_metrics_remain_behind_existing_health_credentials(monkeypatch):
    from lightrag.performance import QueryMetrics

    monkeypatch.setenv("LIGHTRAG_QUERY_METRICS", "true")
    monkeypatch.setattr(_FakeLightRAG, "query_metrics_enabled", True, raising=False)
    monkeypatch.setattr(
        _FakeLightRAG, "query_metrics", QueryMetrics(enabled=True), raising=False
    )
    client = _build_client(monkeypatch, api_key="synthetic-private")
    _set_auth_mode(monkeypatch, auth_configured=True)
    assert "query_performance" not in client.get("/health").json()
    result = client.get("/health", headers={"X-API-Key": "synthetic-private"})
    assert (
        result.status_code == 200
        and result.json()["query_performance"]["enabled"] is True
    )
