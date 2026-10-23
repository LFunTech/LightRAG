"""Physical workspace registration without constructing an application or model."""

from pathlib import Path
from urllib.parse import quote
from yarl import URL

from .plan import digest
from lightrag_delivery.profile import physical_profile_hash

STORAGES = {
    "kv_storage": "PGKVStorage",
    "vector_storage": "PGVectorStorage",
    "doc_status_storage": "PGDocStatusStorage",
    "graph_storage": "HugeGraphStorage",
}


def production_manifest(environment):
    paths = {}
    for key, field in [("working", "WORKING_DIR"), ("input", "INPUT_DIR")]:
        path = Path(environment[field])
        if not path.is_absolute():
            raise ValueError("Absolute production storage paths required")
        paths[key] = str(path.resolve())
    provider = environment.get("LIGHTRAG_OBJECT_STORAGE", "").lower()
    if provider not in {"", "s3"}:
        raise ValueError("Unsupported object storage profile")
    result = {
        "profile": 1,
        "storages": dict(STORAGES),
        "workspace": environment["WORKSPACE"],
        "postgres_schema": "public",
        "postgres": {
            "host": environment["POSTGRES_HOST"],
            "port": str(int(environment["POSTGRES_PORT"])),
            "database": environment["POSTGRES_DATABASE"],
            "vector_index_type": environment.get("POSTGRES_VECTOR_INDEX_TYPE", "HNSW"),
        },
        "hugegraph": {
            "uri": str(URL(environment["HUGEGRAPH_URI"])).rstrip("/"),
            "graph_path": "/graphspaces/"
            + quote(environment["HUGEGRAPH_GRAPHSPACE"], safe="")
            + "/graphs/"
            + quote(environment["HUGEGRAPH_GRAPH"], safe=""),
        },
        "embedding": {
            "model": environment["EMBEDDING_MODEL"],
            "dimension": int(environment["EMBEDDING_DIM"]),
        },
        "paths": paths,
        "shared_filesystem_required": not provider,
        "release_protocol": 1,
        "release_physical_profile": physical_profile_hash(environment),
    }
    if provider:
        result["object_storage"] = {"provider": provider}
    return result


def manifest_hash(environment):
    return digest(production_manifest(environment))
