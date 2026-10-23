"""Credential-free immutable storage plans for the supported production profile."""

from dataclasses import dataclass
from hashlib import md5, sha256
from importlib.resources import files
import json
import re

from lightrag_delivery.profile import physical_profile_hash


def digest(value):
    return sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def resource(name):
    return files("lightrag.provisioning").joinpath("migrations", name).read_text()


def validate_graph_name(value):
    """Match the runtime graph-name rules without reading process environment."""
    if (
        not isinstance(value, str)
        or not value.strip()
        or value in {".", ".."}
        or any(ord(char) < 32 or ord(char) == 127 for char in value)
    ):
        raise ValueError(
            "Explicit graph name without dot segments or controls required"
        )


def index_name(table, suffix):
    value = f"idx_{table}_{suffix}"
    if len(value) > 63:
        value = f"idx_{md5(table.encode()).hexdigest()[:12]}_{suffix}"
    return value


@dataclass(frozen=True)
class MigrationPlan:
    profile_hash: str
    checksum: str
    graph_checksum: str
    tables: tuple[str, ...]
    statements: tuple[str, ...]
    graph: tuple
    scope: tuple[str, str]
    dimension: int
    index_type: str

    def summary(self):
        return {
            "version": 1,
            "profile_hash": self.profile_hash,
            "checksum": self.checksum,
            "graph_checksum": self.graph_checksum,
            "tables": list(self.tables),
            "graph_definitions": [v["name"] for _, v in self.graph],
            "scope": "/".join(self.scope),
            "dimension": self.dimension,
            "index_type": self.index_type,
        }


def build_plan(environment):
    profile = physical_profile_hash(environment)
    for name in ("HUGEGRAPH_GRAPHSPACE", "HUGEGRAPH_GRAPH"):
        validate_graph_name(environment[name])
    for name in ("LIGHTRAG_DEPLOYMENT_ID", "WORKSPACE"):
        if not re.fullmatch(r"[A-Za-z0-9_.-]{1,128}", environment[name]):
            raise ValueError("Invalid release scope identifier: " + name)
    model = environment["EMBEDDING_MODEL"].strip()
    if not model or model != environment["EMBEDDING_MODEL"]:
        raise ValueError("Explicit canonical embedding model required")
    kind = environment.get("POSTGRES_VECTOR_INDEX_TYPE", "HNSW")
    if kind not in {"HNSW", "HNSW_HALFVEC"}:
        raise ValueError(
            "Initial production migration supports HNSW or HNSW_HALFVEC only"
        )

    def integer(name, default, low, high):
        raw = environment.get(name, default)
        if (
            not isinstance(raw, str)
            or not re.fullmatch("[0-9]+", raw)
            or not low <= int(raw) <= high
        ):
            raise ValueError("Invalid storage parameter: " + name)
        return int(raw)

    dimension = integer(
        "EMBEDDING_DIM", "", 1, 4000 if kind == "HNSW_HALFVEC" else 2000
    )
    m = integer("POSTGRES_HNSW_M", "16", 2, 100)
    ef = integer("POSTGRES_HNSW_EF", "64", 4, 1000)
    if ef < 2 * m:
        raise ValueError("HNSW ef_construction must be at least twice m")
    suffix = re.sub("[^a-zA-Z0-9_]", "_", model.lower()) + f"_{dimension}d"
    statements, tables = [], []
    sources = [resource("001_business.sql"), resource("001_vectors.sql")]
    for source in sources:
        for sql in re.sub(r"--[^\n]*", "", source).split(";"):
            if not sql.strip():
                continue
            found = re.search(r"CREATE TABLE (LIGHTRAG_[A-Z_]+)", sql)
            if found is None:
                raise ValueError("Malformed immutable storage baseline")
            old = found[1]
            table = old.lower() + (
                "_" + suffix if old.startswith("LIGHTRAG_VDB_") else ""
            )
            if len(table.encode()) > 63 or not re.fullmatch("[a-z][a-z0-9_]*", table):
                raise ValueError("Storage identifier exceeds PostgreSQL limit")
            sql = sql.replace(old, table).replace(
                "CREATE TABLE " + table, "CREATE TABLE public." + table
            )
            sql = sql.replace(
                "VECTOR(dimension)",
                ("HALFVEC" if kind == "HNSW_HALFVEC" else "VECTOR") + f"({dimension})",
            )
            sql = re.sub(
                r"CONSTRAINT [a-zA-Z0-9_]+ PRIMARY",
                "CONSTRAINT " + index_name(table, "pk") + " PRIMARY",
                sql,
            )
            statements.append(sql.strip())
            tables.append(table)
    for table in tables:
        statements.extend(
            [
                f"CREATE INDEX {index_name(table, 'id')} ON public.{table}(id)",
                f"CREATE INDEX {index_name(table, 'workspace_id')} ON public.{table}(workspace, id)",
            ]
        )
        if table.startswith("lightrag_vdb_"):
            ops = "halfvec" if kind == "HNSW_HALFVEC" else "vector"
            statements.append(
                f"CREATE INDEX {index_name(table, kind.lower() + '_cosine')} ON public.{table} USING hnsw (content_vector {ops}_cosine_ops) WITH (m = {m}, ef_construction = {ef})"
            )
    graph = tuple(json.loads(resource("001_graph.json")))
    checksum = digest(
        {
            "version": 1,
            "sources": sources,
            "ledger": resource("001_ledger.sql"),
            "statements": statements,
        }
    )
    return MigrationPlan(
        profile,
        checksum,
        digest(graph),
        tuple(tables),
        tuple(statements),
        graph,
        (environment["LIGHTRAG_DEPLOYMENT_ID"], environment["WORKSPACE"]),
        dimension,
        kind,
    )
