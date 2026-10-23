import pytest


def test_release_is_off_without_any_new_environment_dependency(monkeypatch):
    from lightrag.distributed.release.config import ReleaseConfig

    monkeypatch.delenv("LIGHTRAG_RELEASE_ENABLED", raising=False)
    monkeypatch.setenv("LIGHTRAG_RELEASE_SLOT", "invalid")
    assert ReleaseConfig.from_env() is None


@pytest.mark.parametrize("flag", ["yes", "1", "TRUE "])
def test_release_rejects_ambiguous_activation(monkeypatch, flag):
    from lightrag.distributed.release.config import ReleaseConfig

    monkeypatch.setenv("LIGHTRAG_RELEASE_ENABLED", flag)
    with pytest.raises(ValueError):
        ReleaseConfig.from_env()


def test_enabled_release_requires_explicit_scoped_single_worker_profile(monkeypatch):
    from lightrag.distributed.release.config import ReleaseConfig

    values = {
        "LIGHTRAG_RELEASE_ENABLED": "true",
        "LIGHTRAG_DISTRIBUTED_WRITES": "true",
        "LIGHTRAG_RELEASE_SLOT": "green",
        "LIGHTRAG_RELEASE_INSTANCE_ID": "pod-1",
        "LIGHTRAG_RELEASE_CONFIG_HASH": "a" * 64,
        "LIGHTRAG_RELEASE_PROFILE_HASH": "b" * 64,
        "LIGHTRAG_DEPLOYMENT_ID": "prod",
        "WORKSPACE": "prod",
        "WORKERS": "1",
    }
    for k, v in values.items():
        monkeypatch.setenv(k, v)
    config = ReleaseConfig.from_env()
    assert config.scope == "prod/prod"
    assert config.slot == "green"
    monkeypatch.setenv("WORKERS", "2")
    with pytest.raises(ValueError):
        ReleaseConfig.from_env()


def test_transition_rejects_skipped_phases_and_stale_owner():
    from lightrag.distributed.release.models import (
        ReleaseIdentity,
        validate_transition,
        ReleaseConflict,
    )

    identity = ReleaseIdentity(
        "prod/prod", "r1", "v1.2.3", "a" * 40, "sha256:" + "b" * 64, "c" * 64
    )
    row = {
        "owner": "r1",
        "epoch": 3,
        "phase": "PREPARED",
        "identity": identity.as_dict(),
    }
    assert (
        validate_transition(row, identity, expected_epoch=3, next_phase="DRAINING")
        == "DRAINING"
    )
    for epoch, phase in [(2, "DRAINING"), (3, "WRITER_SWITCHED")]:
        with pytest.raises(ReleaseConflict):
            validate_transition(row, identity, expected_epoch=epoch, next_phase=phase)
    row["owner"] = "other"
    with pytest.raises(ReleaseConflict):
        validate_transition(row, identity, expected_epoch=3, next_phase="DRAINING")


def test_release_protocol_marker_changes_only_opted_in_profile(monkeypatch, tmp_path):
    from tests.distributed.test_runtime import valid_profile
    from lightrag.distributed.runtime import configure_runtime

    rag = valid_profile(monkeypatch, tmp_path)
    monkeypatch.setenv("LIGHTRAG_RELEASE_ENABLED", "false")
    original = configure_runtime(rag)
    values = {
        "LIGHTRAG_RELEASE_ENABLED": "true",
        "LIGHTRAG_RELEASE_SLOT": "blue",
        "LIGHTRAG_RELEASE_INSTANCE_ID": "pod-1",
        "LIGHTRAG_RELEASE_CONFIG_HASH": "a" * 64,
        "LIGHTRAG_RELEASE_PROFILE_HASH": "b" * 64,
        "WORKSPACE": "test",
        "WORKERS": "1",
    }
    for key, value in values.items():
        monkeypatch.setenv(key, value)
    from tests.ci.test_release_profile import profile_env
    from lightrag_delivery.profile import physical_profile_hash

    profile = profile_env()
    profile.update(
        {
            "LIGHTRAG_DEPLOYMENT_ID": "deployment",
            "WORKSPACE": "test",
            "POSTGRES_HOST": str(original.pg_config["host"]),
            "POSTGRES_PORT": str(original.pg_config["port"]),
            "POSTGRES_DATABASE": str(original.pg_config["database"]),
            "HUGEGRAPH_URI": "http://localhost:8080",
            "HUGEGRAPH_GRAPHSPACE": "DEFAULT",
            "HUGEGRAPH_GRAPH": "hugegraph",
            "EMBEDDING_MODEL": "test",
            "EMBEDDING_DIM": "3",
        }
    )
    for key, value in profile.items():
        monkeypatch.setenv(key, value)
    monkeypatch.setenv("LIGHTRAG_RELEASE_PROFILE_HASH", physical_profile_hash(profile))
    enabled = configure_runtime(rag)
    assert original.release is None
    assert enabled.release is not None
    assert original.coordinator._manifest_hash != enabled.coordinator._manifest_hash

    monkeypatch.setenv("EMBEDDING_DOCUMENT_PREFIX", "incompatible-prefix")
    with pytest.raises(ValueError, match="physical profile"):
        configure_runtime(rag)
