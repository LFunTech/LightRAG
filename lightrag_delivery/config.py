"""Strict, non-executable production onboarding and environment secret inputs."""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import math
from pathlib import Path
import re
from typing import Literal, Mapping
from urllib.parse import urlsplit


class ProdConfigError(ValueError):
    """A field-only diagnostic safe to display in delivery logs."""


TARGET_FIELDS = {
    "cluster_id",
    "namespace",
    "workspace",
    "deployment_id",
    "postgres_database",
    "hugegraph_graphspace",
    "hugegraph_graph",
    "s3_bucket",
    "s3_prefix",
    "storage_class",
    "host",
    "tls_secret",
    "ingress_class",
}
RESOURCE_FIELDS = {
    "cpu_request",
    "cpu_limit",
    "memory_request",
    "memory_limit",
    "working_storage",
}
BUDGET_FIELDS = {
    "queue_capacity",
    "command_byte_limit",
    "drain_seconds",
    "cutover_seconds",
    "safety_margin_seconds",
    "request_wait_seconds",
    "response_margin_seconds",
    "proxy_timeout_seconds",
    "client_timeout_seconds",
    "execution_seconds",
    "result_retention_seconds",
    "source_retention_seconds",
}
CAPACITY_FIELDS = {
    "application_pods",
    "pg_connections_per_pod",
    "pg_connection_budget",
    "other_pg_connections",
    "hugegraph_connections_per_pod",
    "hugegraph_connection_budget",
    "model_concurrency_per_pod",
    "model_concurrency_budget",
}
CONFIG_FIELDS = {
    "version",
    "enabled",
    "repo",
    "target",
    "replicas",
    "workers",
    "resources",
    "budget",
    "capacity",
    "api_key_alias",
    "writers",
    "isolation",
    "runtime",
    "onboarding",
    "ingress",
    "network",
}
SECRET_FIELDS = {
    "llm_api_key",
    "llm_binding_host",
    "bailian_region",
    "dashscope_workspace_id",
    "api_key",
    "postgres_host",
    "postgres_port",
    "postgres_user",
    "postgres_database",
    "postgres_password",
    "hugegraph_uri",
    "hugegraph_graphspace",
    "hugegraph_graph",
    "hugegraph_username",
    "hugegraph_password",
    "hugegraph_gremlin",
    "hugegraph_gremlin_http",
    "hugegraph_graph_binding",
    "hugegraph_traversal_binding",
    "s3_access_key_id",
    "s3_secret_access_key",
    "s3_endpoint_url",
    "s3_bucket",
    "s3_force_path_style",
}


def _keys(value, expected: set[str], field: str) -> None:
    if not isinstance(value, dict):
        raise ProdConfigError(f"{field}: object required")
    missing = sorted(expected - value.keys())
    if missing:
        raise ProdConfigError(f"{field}: missing {','.join(missing)}")
    if value.keys() - expected:
        raise ProdConfigError(f"{field}: unknown fields")


def _text(value, field: str) -> str:
    if (
        not isinstance(value, str)
        or not value.strip()
        or any(ord(c) < 32 for c in value)
    ):
        raise ProdConfigError(f"{field}: non-empty text required")
    return value


def _positive(value, field: str, *, integer=False) -> None:
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(value)
        or value <= 0
        or (integer and not isinstance(value, int))
    ):
        raise ProdConfigError(
            f"{field}: finite positive {'integer' if integer else 'number'} required"
        )


def validate_budget(config: ProdConfig) -> None:
    b = config.budget
    for field, value in b.items():
        _positive(
            value,
            f"budget.{field}",
            integer=field in {"queue_capacity", "command_byte_limit"},
        )
    if (
        b["drain_seconds"] + b["cutover_seconds"] + b["safety_margin_seconds"]
        >= b["request_wait_seconds"]
    ):
        raise ProdConfigError("budget: drain/cutover exceeds request wait")
    if b["request_wait_seconds"] + b["response_margin_seconds"] >= min(
        b["proxy_timeout_seconds"], b["client_timeout_seconds"]
    ):
        raise ProdConfigError("budget: proxy/client timeout margin insufficient")
    minimum = b["request_wait_seconds"] + b["execution_seconds"]
    if (
        b["result_retention_seconds"] < minimum
        or b["source_retention_seconds"] < b["result_retention_seconds"]
    ):
        raise ProdConfigError("budget: source/result retention insufficient")


@dataclass(frozen=True, repr=False)
class ProdConfig:
    version: int
    enabled: bool
    repo: str
    target: dict
    replicas: int
    workers: int
    resources: dict
    budget: dict
    capacity: dict
    api_key_alias: str
    writers: list[str]
    isolation: dict
    runtime: dict
    onboarding: dict
    ingress: dict
    network: dict

    @property
    def config_hash(self) -> str:
        return hashlib.sha256(
            json.dumps(
                self.__dict__, sort_keys=True, separators=(",", ":"), allow_nan=False
            ).encode()
        ).hexdigest()


def load_prod_config(path: Path) -> ProdConfig:
    try:
        data = json.loads(path.read_text())
    except (OSError, ValueError):
        raise ProdConfigError("config: unreadable JSON") from None
    _keys(data, CONFIG_FIELDS, "config")
    for field, fields in [
        ("target", TARGET_FIELDS),
        ("resources", RESOURCE_FIELDS),
        ("budget", BUDGET_FIELDS),
        ("capacity", CAPACITY_FIELDS),
        ("isolation", {"test_scopes", "acl_verified", "evidence_id"}),
    ]:
        _keys(data[field], fields, field)
    if (
        data["version"] != 1
        or type(data["version"]) is not int
        or type(data["enabled"]) is not bool
    ):
        raise ProdConfigError("config: unsupported version or activation flag")
    if (
        type(data["replicas"]) is not int
        or data["replicas"] < 4
        or data["workers"] != 1
        or type(data["workers"]) is not int
    ):
        raise ProdConfigError("config: at least four single-worker replicas required")
    if not re.fullmatch(
        r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", _text(data["repo"], "repo")
    ):
        raise ProdConfigError("repo: invalid identity")
    for field, value in data["target"].items():
        _text(value, f"target.{field}")
        if not re.fullmatch(r"[A-Za-z0-9_.\-/]+", value) or ".." in value.split("/"):
            raise ProdConfigError(f"target.{field}: invalid identifier")
        if re.search(r"(^|[_\-/])test([_\-/]|$)", value, re.I):
            raise ProdConfigError(f"target.{field}: test target forbidden")
    for field in ("namespace", "tls_secret", "ingress_class", "storage_class"):
        if not re.fullmatch(r"[a-z0-9](?:[a-z0-9.-]*[a-z0-9])?", data["target"][field]):
            raise ProdConfigError(f"target.{field}: invalid Kubernetes name")
    if not re.fullmatch(r"[a-z0-9](?:[a-z0-9.-]*[a-z0-9])?", data["target"]["host"]):
        raise ProdConfigError("target.host: invalid DNS host")
    for field, value in data["resources"].items():
        if not isinstance(value, str) or not re.fullmatch(
            r"[1-9][0-9]*(?:\.[0-9]+)?(?:m|Ki|Mi|Gi|Ti)?", value
        ):
            raise ProdConfigError(
                f"resources.{field}: explicit positive quantity required"
            )
    isolation = data["isolation"]
    if (
        isolation["acl_verified"] is not True
        or not isinstance(isolation["test_scopes"], list)
        or not isolation["test_scopes"]
    ):
        raise ProdConfigError(
            "isolation: approved ACL and test-scope evidence required"
        )
    _text(isolation["evidence_id"], "isolation.evidence_id")
    for scope in isolation["test_scopes"]:
        _text(scope, "isolation.test_scopes")
        if scope in data["target"].values():
            raise ProdConfigError("isolation: target overlaps test scope")
    if (
        not isinstance(data["writers"], list)
        or not data["writers"]
        or any(
            not isinstance(x, str) or not re.fullmatch(r"[A-Za-z0-9_.-]+", x)
            for x in data["writers"]
        )
        or len(set(data["writers"])) != len(data["writers"])
    ):
        raise ProdConfigError("writers: unique explicit participants required")
    if not re.fullmatch(
        r"[A-Za-z_][A-Za-z0-9_]*", _text(data["api_key_alias"], "api_key_alias")
    ):
        raise ProdConfigError("api_key_alias: invalid alias")
    for field, value in data["capacity"].items():
        _positive(value, f"capacity.{field}", integer=True)
    c = data["capacity"]
    if c["other_pg_connections"] < 2:
        raise ProdConfigError(
            "capacity: include operator/probe and all non-Pod PG users"
        )
    pods = data["replicas"] * 2
    if c["application_pods"] < pods:
        raise ProdConfigError("capacity: two full slots required")
    for resource, per, total in [
        ("PG", "pg_connections_per_pod", "pg_connection_budget"),
        ("HugeGraph", "hugegraph_connections_per_pod", "hugegraph_connection_budget"),
        ("model", "model_concurrency_per_pod", "model_concurrency_budget"),
    ]:
        extra = c["other_pg_connections"] if resource == "PG" else 0
        if c["application_pods"] * c[per] + extra > c[total]:
            raise ProdConfigError(f"capacity: {resource} aggregate budget insufficient")
    validate_delivery_profile(data)
    result = ProdConfig(**data)
    validate_budget(result)
    return result


def secret_aliases(
    environment: Literal["prod", "test"], *, api_key_alias: str
) -> dict[str, str]:
    if environment not in {"prod", "test"} or not re.fullmatch(
        r"lightrag_(?:prod|test)(?:_[0-9]{2})?_api_key", api_key_alias
    ):
        raise ProdConfigError("environment or api_key_alias: unsupported")
    return {
        "API KEY": "llm_api_key",
        "API Host": "llm_binding_host",
        "地域": "bailian_region",
        "业务空间ID": "dashscope_workspace_id",
        "IP": "postgres_host",
        "端口": "postgres_port",
        "用户": "postgres_user",
        "数据库": "postgres_database",
        "密码": "postgres_password",
        "HUGEGRAPH_ENDPOINT": "hugegraph_uri",
        "HUGEGRAPH_GRAPHSPACE": "hugegraph_graphspace",
        "HUGEGRAPH_GRAPH": "hugegraph_graph",
        "HUGEGRAPH_USERNAME": "hugegraph_username",
        "HUGEGRAPH_PASSWORD": "hugegraph_password",
        "HUGEGRAPH_GREMLIN_ENDPOINT": "hugegraph_gremlin",
        "HUGEGRAPH_GREMLIN_HTTP_ENDPOINT": "hugegraph_gremlin_http",
        "HUGEGRAPH_GREMLIN_GRAPH_BINDING": "hugegraph_graph_binding",
        "HUGEGRAPH_GREMLIN_TRAVERSAL_BINDING": "hugegraph_traversal_binding",
        "REST": "hugegraph_uri",
        "Gremlin": "hugegraph_gremlin",
        "graphspace": "hugegraph_graphspace",
        "graph": "hugegraph_graph",
        "AWS_ACCESS_KEY_ID": "s3_access_key_id",
        "AWS_SECRET_ACCESS_KEY": "s3_secret_access_key",
        "AWS_ENDPOINT_URL_S3": "s3_endpoint_url",
        "AWS_BUCKET": "s3_bucket",
        "AWS_S3_FORCE_PATH_STYLE": "s3_force_path_style",
        api_key_alias: "api_key",
    }


def _unquote(value: str, field: str) -> str:
    value = value.strip()
    if value.startswith(('"', "'")):
        if len(value) < 2 or value[-1] != value[0]:
            raise ProdConfigError(f"{field}: malformed quoted value")
        value = value[1:-1]
    return _text(value, field)


def parse_secret_inventory(
    path: Path,
    *,
    environment: Literal["prod", "test"],
    aliases: Mapping[str, str],
    required: set[str] | None = None,
) -> dict[str, str]:
    if environment not in {"prod", "test"} or path.name != f"{environment}.secrets":
        raise ProdConfigError(
            "source: explicit matching environment inventory required"
        )
    if set(aliases.values()) - SECRET_FIELDS:
        raise ProdConfigError("aliases: destination outside environment whitelist")
    try:
        lines = path.read_text().splitlines()
    except OSError:
        raise ProdConfigError("source: inventory unreadable") from None
    result = {}
    seen = set()
    for line in lines:
        match = re.match(
            r"^\s*(?:[-*]\s+|\d+[.)]\s+)?[\"\']?([A-Za-z_\u4e00-\u9fff][A-Za-z0-9_ \u4e00-\u9fff]*?)[\"\']?\s*[:=：]\s*(.*)$",
            line,
        )
        if not match or match[1].strip() not in aliases:
            continue
        field = aliases[match[1].strip()]
        if field in seen:
            raise ProdConfigError(f"{field}: duplicate or conflicting input")
        seen.add(field)
        value = _unquote(match[2], field)
        if field.endswith(("_uri", "_host", "_url", "_http")) and ("://" in value):
            try:
                parsed = urlsplit(value)
                parsed.port
            except ValueError:
                raise ProdConfigError(f"{field}: invalid endpoint") from None
            if (
                parsed.scheme not in {"http", "https", "ws", "wss"}
                or not parsed.hostname
                or parsed.username
                or parsed.password
                or parsed.fragment
            ):
                raise ProdConfigError(f"{field}: invalid endpoint")
        if field == "postgres_port" and (
            not value.isdigit() or not 1 <= int(value) <= 65535
        ):
            raise ProdConfigError(f"{field}: invalid port")
        if field == "s3_force_path_style" and value.lower() not in {"true", "false"}:
            raise ProdConfigError(f"{field}: invalid boolean")
        result[f"lightrag_{environment}_{field}"] = value
    missing = (required or set()) - {
        key.removeprefix(f"lightrag_{environment}_") for key in result
    }
    if missing:
        raise ProdConfigError("inventory: missing " + ",".join(sorted(missing)))
    return result


RUNTIME_FIELDS = {
    "LIGHTRAG_QUERY_METRICS",
    "ENABLE_LLM_CACHE",
    "ENABLE_QUERY_ANSWER_CACHE",
    "ENABLE_LLM_CACHE_FOR_EXTRACT",
    "LLM_BINDING",
    "LLM_MODEL",
    "EMBEDDING_BINDING",
    "EMBEDDING_MODEL",
    "EMBEDDING_DIM",
    "EMBEDDING_TOKEN_LIMIT",
    "EMBEDDING_ASYMMETRIC",
    "EMBEDDING_DOCUMENT_PREFIX",
    "EMBEDDING_QUERY_PREFIX",
    "RERANK_BINDING",
    "RERANK_MODEL",
    "RERANK_BINDING_HOST",
    "POSTGRES_MAX_CONNECTIONS",
    "HUGEGRAPH_MAX_CONNECTIONS",
    "MAX_ASYNC",
    "EMBEDDING_FUNC_MAX_ASYNC",
    "MAX_PARALLEL_INSERT",
    "PIPELINE_SCHEDULING_PAGE_SIZE",
    "DEFAULT_PARSER_ENGINE",
}
ONBOARDING_FIELDS = {
    "namespace_uid",
    "kube_system_uid",
    "server_sha256",
    "runtime_role",
    "operator_secret",
    "control_service_account",
    "application_service_account",
    "image_pull_secret",
    "baseline_manifest_hash",
    "profile_hash",
    "compatibility_evidence_id",
    "read_compatible",
    "write_compatible",
}
INGRESS_FIELDS = {
    "provider",
    "mode",
    "region",
    "load_balancer_id",
    "listener_id",
    "location_id",
    "protocol",
    "certificate_id",
    "controller_version",
    "streaming_evidence_id",
    "backend_port",
    "path",
    "force_close_disabled",
}


def validate_delivery_profile(data):
    import ipaddress

    _keys(data["runtime"], RUNTIME_FIELDS, "runtime")
    _keys(data["onboarding"], ONBOARDING_FIELDS, "onboarding")
    _keys(data["ingress"], INGRESS_FIELDS, "ingress")
    _keys(data["network"], {"ingress_cidrs", "egress"}, "network")
    if data["target"]["ingress_class"] != "qcloud":
        raise ProdConfigError("target.ingress_class: qcloud required")
    i, o, r = data["ingress"], data["onboarding"], data["runtime"]
    if (
        i["provider"] != "qcloud"
        or i["mode"] != "direct"
        or i["protocol"] != "HTTPS"
        or i["backend_port"] != 9621
        or i["path"] != "/"
        or i["force_close_disabled"] is not True
    ):
        raise ProdConfigError(
            "ingress: explicit HTTPS/direct streaming contract required"
        )
    for field, prefix in [
        ("load_balancer_id", "lb-"),
        ("listener_id", "lbl-"),
        ("location_id", "loc-"),
    ]:
        if not isinstance(i[field], str) or not re.fullmatch(
            prefix + r"[a-z0-9]+", i[field]
        ):
            raise ProdConfigError("ingress." + field + ": invalid identifier")
    for field in (
        "region",
        "certificate_id",
        "controller_version",
        "streaming_evidence_id",
    ):
        _text(i[field], "ingress." + field)
    for field, value in o.items():
        if field in {"read_compatible", "write_compatible"}:
            if value is not True:
                raise ProdConfigError(
                    "onboarding: explicit compatibility approval required"
                )
        elif field.endswith("hash") or field == "server_sha256":
            if not isinstance(value, str) or not re.fullmatch("[a-f0-9]{64}", value):
                raise ProdConfigError("onboarding." + field + ": SHA256 required")
        elif not re.fullmatch(r"[A-Za-z0-9_.-]+", _text(value, "onboarding." + field)):
            raise ProdConfigError("onboarding." + field + ": invalid identifier")
    for field, value in r.items():
        if not isinstance(value, str) or any(ord(c) < 32 for c in value):
            raise ProdConfigError("runtime." + field + ": string required")
    if (
        r["LLM_BINDING"] != "openai"
        or r["EMBEDDING_BINDING"] != "openai"
        or r["DEFAULT_PARSER_ENGINE"] != "native"
    ):
        raise ProdConfigError(
            "runtime: only the verified OpenAI-compatible/native profile is admitted"
        )
    if r["EMBEDDING_ASYMMETRIC"] not in {"true", "false"}:
        raise ProdConfigError("runtime: explicit embedding asymmetry required")
    for field in ("LIGHTRAG_QUERY_METRICS", "ENABLE_LLM_CACHE", "ENABLE_QUERY_ANSWER_CACHE", "ENABLE_LLM_CACHE_FOR_EXTRACT"):
        if r[field] not in {"true", "false"}:
            raise ProdConfigError("runtime." + field + ": explicit boolean required")
    if r["ENABLE_QUERY_ANSWER_CACHE"] != "false" or r["ENABLE_LLM_CACHE_FOR_EXTRACT"] != "true":
        raise ProdConfigError("runtime: answer reuse is unverified; preserve extraction recovery cache")
    if r["LIGHTRAG_QUERY_METRICS"] != "true":
        raise ProdConfigError("runtime: production query metrics are required")
    if r["EMBEDDING_ASYMMETRIC"] == "true" and not all(
        r[k] for k in ("EMBEDDING_DOCUMENT_PREFIX", "EMBEDDING_QUERY_PREFIX")
    ):
        raise ProdConfigError("runtime: both asymmetric prefixes required")
    for field in ("LLM_MODEL", "EMBEDDING_MODEL"):
        _text(r[field], "runtime." + field)
    for field in (
        "EMBEDDING_DIM",
        "EMBEDDING_TOKEN_LIMIT",
        "POSTGRES_MAX_CONNECTIONS",
        "HUGEGRAPH_MAX_CONNECTIONS",
        "MAX_ASYNC",
        "EMBEDDING_FUNC_MAX_ASYNC",
        "MAX_PARALLEL_INSERT",
        "PIPELINE_SCHEDULING_PAGE_SIZE",
    ):
        if not re.fullmatch(r"[1-9][0-9]*", r[field]):
            raise ProdConfigError("runtime." + field + ": positive integer required")
    c = data["capacity"]
    if (
        int(r["POSTGRES_MAX_CONNECTIONS"]) + 5 > c["pg_connections_per_pod"]
        or int(r["HUGEGRAPH_MAX_CONNECTIONS"]) > c["hugegraph_connections_per_pod"]
        or 4 * int(r["MAX_ASYNC"]) + int(r["EMBEDDING_FUNC_MAX_ASYNC"])
        > c["model_concurrency_per_pod"]
    ):
        raise ProdConfigError(
            "capacity: actual pools including coordinator/witness exceed per-Pod budget"
        )
    if r["RERANK_BINDING"] != "null":
        raise ProdConfigError(
            "runtime: reranker requires a separately budgeted verified profile"
        )
    for cidr in data["network"]["ingress_cidrs"]:
        try:
            net = ipaddress.ip_network(cidr, strict=True)
            if net.prefixlen == 0:
                raise ValueError()
        except ValueError:
            raise ProdConfigError(
                "network: explicit restricted ingress CIDR required"
            ) from None
    if not data["network"]["ingress_cidrs"] or not data["network"]["egress"]:
        raise ProdConfigError("network: explicit ingress and egress required")
    for rule in data["network"]["egress"]:
        _keys(rule, {"cidr", "ports"}, "network.egress")
        try:
            net = ipaddress.ip_network(rule["cidr"], strict=True)
            if net.prefixlen == 0:
                raise ValueError()
        except ValueError:
            raise ProdConfigError("network: restricted egress CIDR required") from None
        if not rule["ports"]:
            raise ProdConfigError("network: explicit egress ports required")
        for port in rule["ports"]:
            _keys(port, {"protocol", "port"}, "network.egress.ports")
            if (
                port["protocol"] not in {"TCP", "UDP"}
                or type(port["port"]) is not int
                or not 1 <= port["port"] <= 65535
            ):
                raise ProdConfigError("network: invalid egress port")
