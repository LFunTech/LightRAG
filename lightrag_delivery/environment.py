"""Build one production environment, never inheriting test or process defaults."""

import hashlib
from urllib.parse import quote
from .config import ProdConfigError
from .profile import physical_profile_hash
from .render import content_hash

SECRET_ENV = {
    "api_key": "LIGHTRAG_API_KEY",
    "llm_api_key": "LLM_BINDING_API_KEY",
    "llm_binding_host": "LLM_BINDING_HOST",
    "postgres_host": "POSTGRES_HOST",
    "postgres_port": "POSTGRES_PORT",
    "postgres_user": "POSTGRES_USER",
    "postgres_database": "POSTGRES_DATABASE",
    "postgres_password": "POSTGRES_PASSWORD",
    "hugegraph_uri": "HUGEGRAPH_URI",
    "hugegraph_graphspace": "HUGEGRAPH_GRAPHSPACE",
    "hugegraph_graph": "HUGEGRAPH_GRAPH",
    "hugegraph_username": "HUGEGRAPH_USERNAME",
    "hugegraph_password": "HUGEGRAPH_PASSWORD",
    "hugegraph_gremlin": "HUGEGRAPH_GREMLIN",
    "s3_access_key_id": "S3_ACCESS_KEY_ID",
    "s3_secret_access_key": "S3_SECRET_ACCESS_KEY",
    "s3_endpoint_url": "S3_ENDPOINT_URL",
    "s3_bucket": "S3_BUCKET",
    "s3_force_path_style": "S3_FORCE_PATH_STYLE",
}


def runtime_environment(config, secrets):
    if set(SECRET_ENV) - secrets.keys() or any(
        not isinstance(secrets[k], str) or not secrets[k] for k in SECRET_ENV
    ):
        raise ProdConfigError("secrets: incomplete explicit prod runtime inputs")
    for source, target in [
        ("postgres_database", "postgres_database"),
        ("hugegraph_graphspace", "hugegraph_graphspace"),
        ("hugegraph_graph", "hugegraph_graph"),
        ("s3_bucket", "s3_bucket"),
    ]:
        if secrets[source] != config.target[target]:
            raise ProdConfigError(
                "secrets: configured physical target mismatch: " + source
            )
    for name in ("llm_binding_host", "s3_endpoint_url", "hugegraph_uri"):
        from urllib.parse import urlsplit

        try:
            parsed = urlsplit(secrets[name])
            if (
                parsed.scheme not in {"https", "http"}
                or not parsed.hostname
                or parsed.username
                or parsed.password
                or parsed.fragment
            ):
                raise ValueError()
        except ValueError:
            raise ProdConfigError("secrets: invalid endpoint " + name) from None
    if (
        not secrets["postgres_port"].isdigit()
        or not 1 <= int(secrets["postgres_port"]) <= 65535
    ):
        raise ProdConfigError("secrets: invalid postgres_port")
    environment = {target: secrets[source] for source, target in SECRET_ENV.items()}
    environment.update(config.runtime)
    environment.update(
        {
            "LIGHTRAG_DISTRIBUTED_WRITES": "true",
            "LIGHTRAG_SHARED_STORAGE": "false",
            "LIGHTRAG_OBJECT_STORAGE": "s3",
            "ENABLE_LOCAL_FILE_INGESTION": "false",
            "LIGHTRAG_DEPLOYMENT_ID": config.target["deployment_id"],
            "WORKSPACE": config.target["workspace"],
            "POSTGRES_WORKSPACE": config.target["workspace"],
            "S3_OBJECT_PREFIX": config.target["s3_prefix"],
            "LIGHTRAG_COORDINATION_POOL_MODE": "direct",
            "LIGHTRAG_KV_STORAGE": "PGKVStorage",
            "LIGHTRAG_VECTOR_STORAGE": "PGVectorStorage",
            "LIGHTRAG_DOC_STATUS_STORAGE": "PGDocStatusStorage",
            "LIGHTRAG_GRAPH_STORAGE": "HugeGraphStorage",
            "HUGEGRAPH_AUTO_CREATE_SCHEMA": "false",
            "EMBEDDING_BINDING_HOST": secrets["llm_binding_host"],
            "EMBEDDING_BINDING_API_KEY": secrets["llm_api_key"],
            "LOG_DIR": "/app/data/logs",
            "WORKING_DIR": "/app/data/rag_storage",
            "INPUT_DIR": "/app/data/inputs",
            "S3_SCRATCH_DIR": "/app/data/object-scratch",
            "TOKEN_SECRET": hashlib.sha256(
                ("lightrag-token-v1:" + secrets["api_key"]).encode()
            ).hexdigest(),
        }
    )
    environment["LIGHTRAG_COORDINATION_DSN"] = (
        "postgresql://"
        + quote(secrets["postgres_user"], safe="")
        + ":"
        + quote(secrets["postgres_password"], safe="")
        + "@"
        + secrets["postgres_host"]
        + ":"
        + secrets["postgres_port"]
        + "/"
        + quote(secrets["postgres_database"], safe="")
    )
    for key in ("EMBEDDING_DOCUMENT_PREFIX", "EMBEDDING_QUERY_PREFIX"):
        if environment[key] == "":
            environment[key] = "NO_PREFIX"
    profile = physical_profile_hash(environment)
    if profile != config.onboarding["profile_hash"]:
        raise ProdConfigError("onboarding: physical profile differs from enrollment")
    environment["LIGHTRAG_RELEASE_PROFILE_HASH"] = profile
    return environment, content_hash(
        {"configuration": config.config_hash, "environment": environment}
    )
