"""The approved first-release exception is narrow, explicit and immutable."""

import json

import pytest

from tests.ci.test_prod_config import config_data, load
from tests.provisioning.test_plan import environment


KEY = "LIGHTRAG_FIRST_RELEASE_CREDENTIAL_EXCEPTION"


def approval(values=None):
    from lightrag_delivery.profile import physical_profile_hash

    values = environment() if values is None else values
    return {
        "version": 1,
        "approval_id": "first-release-approval",
        "scope": values["LIGHTRAG_DEPLOYMENT_ID"] + "/" + values["WORKSPACE"],
        "profile_hash": physical_profile_hash(values),
        "release_tag": "v1.5.9",
        "postgres_user": "dedicated",
        "postgres_database": values["POSTGRES_DATABASE"],
        "hugegraph_user": "dedicated_graph",
        "accepted_risk": True,
    }


def shared_environment():
    values = dict(
        environment(),
        POSTGRES_USER="dedicated",
        HUGEGRAPH_USERNAME="dedicated_graph",
        LIGHTRAG_RELEASE_TAG="v1.5.9",
    )
    values[KEY] = json.dumps(approval(values))
    return values


def test_exception_is_opt_in_and_exactly_bound():
    from lightrag_delivery.credentials import from_environment

    assert from_environment(environment()) is None
    policy = from_environment(shared_environment())
    assert policy.as_dict() == approval()
    assert "password" not in json.dumps(policy.as_dict())


@pytest.mark.parametrize(
    "field,value",
    [
        ("version", True),
        ("version", 2),
        ("accepted_risk", False),
        ("accepted_risk", "true"),
        ("scope", "other/prod"),
        ("scope", "test/test"),
        ("profile_hash", "a" * 64),
        ("release_tag", "v1.5.9-test"),
        ("approval_id", ""),
        ("postgres_user", "other"),
        ("postgres_database", "other"),
        ("hugegraph_user", "other"),
    ],
)
def test_exception_rejects_incomplete_or_cross_target_authorization(field, value):
    from lightrag_delivery.credentials import from_environment

    values = shared_environment()
    item = json.loads(values[KEY])
    item[field] = value
    values[KEY] = json.dumps(item)
    with pytest.raises(ValueError):
        from_environment(values)


@pytest.mark.parametrize("value", ["", "true", "[]", "{}", "null"])
def test_malformed_exception_is_not_a_disabled_exception(value):
    from lightrag_delivery.credentials import from_environment

    with pytest.raises(ValueError):
        from_environment(dict(shared_environment(), **{KEY: value}))


def configured_shared(tmp_path):
    data = config_data()
    item = approval()
    item.update(
        scope=data["target"]["deployment_id"] + "/" + data["target"]["workspace"],
        profile_hash=data["onboarding"]["profile_hash"],
        postgres_database=data["target"]["postgres_database"],
    )
    data["onboarding"]["runtime_role"] = item["postgres_user"]
    data["onboarding"]["first_release_credential_exception"] = item
    return load(tmp_path, data), item


def test_approved_config_records_exception_without_changing_default_hash(tmp_path):
    original = load(tmp_path, config_data())
    configured, item = configured_shared(tmp_path)
    assert configured.onboarding["first_release_credential_exception"] == item
    assert configured.config_hash != original.config_hash


def test_schema_describes_optional_exception_without_relaxing_existing_fields():
    from pathlib import Path

    schema = json.loads(
        Path(
            "k8s-deploy/lightrag-kustomize/overlays/prod/onboarding.schema.json"
        ).read_text()
    )
    node = schema["properties"]["onboarding"]
    policy = node["properties"]["first_release_credential_exception"]
    assert policy["additionalProperties"] is False
    assert set(policy["required"]) == set(approval())
    assert "first_release_credential_exception" not in node["required"]


@pytest.mark.parametrize("slot,tag", [("green", "v1.5.9"), ("blue", "v1.5.10")])
def test_exception_cannot_render_green_or_a_later_tag(tmp_path, slot, tag):
    from lightrag_delivery.render import render_slot

    config, item = configured_shared(tmp_path)
    with pytest.raises(ValueError):
        render_slot(
            config,
            {"config_hash": "a" * 64, "digest": "sha256:" + "b" * 64, "tag": tag},
            slot,
            image="registry.invalid/rag",
            environment={KEY: json.dumps(item)},
        )


def test_exception_renders_only_the_pinned_first_blue(tmp_path):
    from lightrag_delivery.render import render_slot

    config, item = configured_shared(tmp_path)
    values = {KEY: json.dumps(item), "LIGHTRAG_RELEASE_TAG": item["release_tag"]}
    result = render_slot(
        config,
        {
            "config_hash": "a" * 64,
            "digest": "sha256:" + "b" * 64,
            "tag": item["release_tag"],
        },
        "blue",
        image="registry.invalid/rag",
        environment=values,
    )
    assert result[-1]["spec"]["replicas"] == 4


async def test_ordinary_deliver_rejects_exception_before_opening_connections(
    tmp_path, monkeypatch
):
    import asyncpg
    from lightrag.distributed.release.delivery import delivery_session

    config, _ = configured_shared(tmp_path)
    # Loading the approved exception must precede all identity/network work.
    monkeypatch.setattr(
        asyncpg, "connect", lambda *a, **k: pytest.fail("network started")
    )
    with pytest.raises(ValueError, match="first|First"):
        async with delivery_session(
            config_path=tmp_path / "prod.json",
            identity_path="absent",
            environment_path="absent",
            image="registry.invalid/rag",
        ):
            pytest.fail("ordinary delivery accepted")


def test_ordinary_pipeline_refuses_shared_profile_before_artifact_or_cluster_work(
    tmp_path,
):
    from scripts.ci.prod_deploy import verified_inputs, render_control_job

    config, _ = configured_shared(tmp_path)
    with pytest.raises(ValueError, match="first|First"):
        verified_inputs(
            config,
            image_record={},
            source_archive=None,
            source_record={},
            environment={},
        )
    with pytest.raises(ValueError, match="first|First"):
        render_control_job(
            config,
            {"digest": "sha256:" + "a" * 64},
            {},
            image="registry.invalid/rag",
            attempt="1",
        )


def test_runtime_environment_carries_only_the_explicit_approved_policy(tmp_path):
    from dataclasses import replace
    from lightrag_delivery.environment import runtime_environment
    from tests.ci.test_prod_inputs import inputs
    from lightrag_delivery.environment import SECRET_ENV

    config, _, _, _, ci = inputs(tmp_path)
    secrets = {key: ci["LIGHTRAG_PROD_" + key.upper()] for key in SECRET_ENV}
    original, original_hash = runtime_environment(config, secrets)
    item = approval(original)
    item.update(
        postgres_user=secrets["postgres_user"],
        hugegraph_user=secrets["hugegraph_username"],
    )
    config = replace(
        config,
        onboarding=dict(config.onboarding, first_release_credential_exception=item),
    )
    values, hashed = runtime_environment(config, secrets)
    assert json.loads(values[KEY]) == item
    assert hashed != original_hash and KEY not in original
    secrets["postgres_user"] = "different"
    with pytest.raises(ValueError):
        runtime_environment(config, secrets)
