"""Credential-free physical profile and immutable runtime configuration hashes."""

from .render import content_hash

PROFILE_FIELDS = {
    "LIGHTRAG_DEPLOYMENT_ID",
    "WORKSPACE",
    "POSTGRES_HOST",
    "POSTGRES_PORT",
    "POSTGRES_DATABASE",
    "HUGEGRAPH_URI",
    "HUGEGRAPH_GRAPHSPACE",
    "HUGEGRAPH_GRAPH",
    "EMBEDDING_BINDING",
    "EMBEDDING_BINDING_HOST",
    "EMBEDDING_MODEL",
    "EMBEDDING_DIM",
    "EMBEDDING_ASYMMETRIC",
    "EMBEDDING_DOCUMENT_PREFIX",
    "EMBEDDING_QUERY_PREFIX",
    "S3_ENDPOINT_URL",
    "S3_BUCKET",
    "S3_OBJECT_PREFIX",
}


def physical_profile_hash(environment):
    values = {}
    for field in sorted(PROFILE_FIELDS):
        value = environment.get(field)
        if not isinstance(value, str) or (not value and not field.endswith("_PREFIX")):
            raise ValueError("Missing physical profile field: " + field)
        values[field] = (
            ""
            if field in {"EMBEDDING_DOCUMENT_PREFIX", "EMBEDDING_QUERY_PREFIX"}
            and value == "NO_PREFIX"
            else value
        )
    if values["EMBEDDING_ASYMMETRIC"] not in {"true", "false"}:
        raise ValueError("Explicit embedding asymmetry required")
    return content_hash({"version": 1, "fields": values})


def verify_server_profile(args, environment):
    for name in (
        "embedding_binding",
        "embedding_binding_host",
        "embedding_model",
        "embedding_dim",
        "embedding_asymmetric",
        "embedding_document_prefix",
        "embedding_query_prefix",
    ):
        actual = getattr(args, name, None)
        if isinstance(actual, bool):
            actual = str(actual).lower()
        if actual is None and name.endswith("_prefix"):
            actual = ""
        expected = environment.get(name.upper())
        if name.endswith("_prefix") and expected == "NO_PREFIX":
            expected = ""
        if str(actual) != expected:
            raise ValueError(
                "Server arguments differ from the enrolled physical profile: " + name
            )
