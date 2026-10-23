import json

import pytest


def config_data():
    return {
        "runtime": {
            "LIGHTRAG_QUERY_METRICS": "true",
            "ENABLE_LLM_CACHE": "true",
            "ENABLE_QUERY_ANSWER_CACHE": "false",
            "ENABLE_LLM_CACHE_FOR_EXTRACT": "true",
            "LLM_BINDING": "openai",
            "LLM_MODEL": "fixture-llm",
            "EMBEDDING_BINDING": "openai",
            "EMBEDDING_MODEL": "fixture-embedding",
            "EMBEDDING_DIM": "8",
            "EMBEDDING_TOKEN_LIMIT": "1000",
            "EMBEDDING_ASYMMETRIC": "false",
            "EMBEDDING_DOCUMENT_PREFIX": "",
            "EMBEDDING_QUERY_PREFIX": "",
            "RERANK_BINDING": "null",
            "RERANK_MODEL": "",
            "RERANK_BINDING_HOST": "",
            "POSTGRES_MAX_CONNECTIONS": "5",
            "HUGEGRAPH_MAX_CONNECTIONS": "5",
            "MAX_ASYNC": "1",
            "EMBEDDING_FUNC_MAX_ASYNC": "1",
            "MAX_PARALLEL_INSERT": "1",
            "PIPELINE_SCHEDULING_PAGE_SIZE": "16",
            "DEFAULT_PARSER_ENGINE": "native",
        },
        "onboarding": {
            "namespace_uid": "fixture-namespace",
            "kube_system_uid": "fixture-cluster",
            "server_sha256": "a" * 64,
            "runtime_role": "release_app",
            "operator_secret": "release-control",
            "control_service_account": "release-control",
            "application_service_account": "release-app",
            "image_pull_secret": "registry-prod",
            "baseline_manifest_hash": "b" * 64,
            "profile_hash": "c" * 64,
            "compatibility_evidence_id": "fixture-approval",
            "read_compatible": True,
            "write_compatible": True,
        },
        "ingress": {
            "provider": "qcloud",
            "mode": "direct",
            "region": "ap-guangzhou",
            "load_balancer_id": "lb-fixture",
            "listener_id": "lbl-fixture",
            "location_id": "loc-fixture",
            "protocol": "HTTPS",
            "certificate_id": "fixture",
            "controller_version": "2.6.0",
            "streaming_evidence_id": "fixture-sse-rehearsal",
            "backend_port": 9621,
            "path": "/",
            "force_close_disabled": True,
        },
        "network": {
            "ingress_cidrs": ["10.0.0.0/16"],
            "egress": [
                {"cidr": "10.0.0.0/16", "ports": [{"protocol": "TCP", "port": 443}]}
            ],
        },
        "version": 1,
        "enabled": False,
        "repo": "minwang/LightRAG",
        "target": {
            "cluster_id": "prod-cluster",
            "namespace": "lightrag-prod",
            "workspace": "lightrag_prod",
            "deployment_id": "lightrag_prod",
            "postgres_database": "prod",
            "hugegraph_graphspace": "prod",
            "hugegraph_graph": "prod",
            "s3_bucket": "prod",
            "s3_prefix": "prod/objects",
            "storage_class": "prod-rwx",
            "host": "rag.example.invalid",
            "ingress_class": "qcloud",
        },
        "replicas": 4,
        "workers": 1,
        "resources": {
            "cpu_request": "1",
            "cpu_limit": "2",
            "memory_request": "2Gi",
            "memory_limit": "4Gi",
            "working_storage": "40Gi",
        },
        "budget": {
            "queue_capacity": 100,
            "command_byte_limit": 1048576,
            "drain_seconds": 20,
            "cutover_seconds": 5,
            "safety_margin_seconds": 5,
            "request_wait_seconds": 60,
            "response_margin_seconds": 10,
            "proxy_timeout_seconds": 120,
            "client_timeout_seconds": 120,
            "execution_seconds": 300,
            "result_retention_seconds": 600,
            "source_retention_seconds": 1000,
        },
        "capacity": {
            "application_pods": 8,
            "pg_connections_per_pod": 10,
            "pg_connection_budget": 100,
            "other_pg_connections": 2,
            "hugegraph_connections_per_pod": 5,
            "hugegraph_connection_budget": 50,
            "model_concurrency_per_pod": 5,
            "model_concurrency_budget": 50,
        },
        "api_key_alias": "lightrag_test_api_key",
        "writers": ["api"],
        "isolation": {
            "test_scopes": ["lightrag_test_01"],
            "acl_verified": True,
            "evidence_id": "approved-offline-fixture",
        },
    }


def load(tmp_path, data):
    from scripts.ci.prod_config import load_prod_config

    p = tmp_path / "prod.json"
    p.write_text(json.dumps(data))
    return load_prod_config(p)


def test_config_preserves_four_single_worker_and_eight_pod_budget(tmp_path):
    config = load(tmp_path, config_data())
    assert config.replicas == 4 and config.workers == 1
    assert config.capacity["application_pods"] == 8
    assert config.enabled is False


@pytest.mark.parametrize(
    "field,value",
    [
        ("drain_seconds", float("inf")),
        ("queue_capacity", 0),
        ("request_wait_seconds", 10),
        ("proxy_timeout_seconds", 60),
        ("source_retention_seconds", 50),
    ],
)
def test_budget_rejects_infinite_missing_or_inverted_limits(tmp_path, field, value):
    from scripts.ci.prod_config import ProdConfigError

    data = config_data()
    data["budget"][field] = value
    with pytest.raises(ProdConfigError):
        load(tmp_path, data)


@pytest.mark.parametrize(
    "change",
    ["scope", "unknown", "capacity", "acl", "workers", "missing", "url_credentials"],
)
def test_unsafe_prod_config_is_rejected_before_mutation(tmp_path, change):
    from scripts.ci.prod_config import ProdConfigError

    data = config_data()
    if change == "scope":
        data["target"]["workspace"] = "lightrag_test_01"
    if change == "unknown":
        data["execute_shell"] = "echo unsafe"
    if change == "capacity":
        data["capacity"]["pg_connection_budget"] = 79
    if change == "acl":
        data["isolation"]["acl_verified"] = False
    if change == "workers":
        data["workers"] = 2
    if change == "missing":
        del data["target"]["storage_class"]
    if change == "url_credentials":
        data["target"]["host"] = "user:secret@host"
    with pytest.raises(ProdConfigError):
        load(tmp_path, data)


def test_environment_config_does_not_inherit_process_test_defaults(
    tmp_path, monkeypatch
):
    from scripts.ci.prod_config import ProdConfigError

    monkeypatch.setenv("WORKSPACE", "lightrag_test_01")
    data = config_data()
    del data["target"]["workspace"]
    with pytest.raises(ProdConfigError, match="workspace"):
        load(tmp_path, data)


def test_onboarding_schema_document_is_not_a_deployable_configuration():
    from pathlib import Path
    from scripts.ci.prod_config import ProdConfigError, load_prod_config

    path = Path("k8s-deploy/lightrag-kustomize/overlays/prod/onboarding.schema.json")
    with pytest.raises(ProdConfigError):
        load_prod_config(path)
