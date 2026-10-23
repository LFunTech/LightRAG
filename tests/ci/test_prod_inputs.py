import hashlib
import json

import pytest

from tests.ci.test_prod_config import config_data, load


def inputs(tmp_path):
    from lightrag_delivery.environment import SECRET_ENV
    from lightrag_delivery.profile import physical_profile_hash

    data = config_data()
    data["enabled"] = True
    secrets = {k: "synthetic-private" for k in SECRET_ENV}
    secrets.update(
        postgres_host="pg.invalid",
        postgres_port="5432",
        postgres_user="release_app",
        postgres_database="prod",
        hugegraph_uri="http://graph.invalid:8080",
        hugegraph_graphspace="prod",
        hugegraph_graph="prod",
        s3_bucket="prod",
        s3_endpoint_url="https://objects.invalid",
        s3_force_path_style="true",
        llm_binding_host="https://model.invalid/v1",
    )
    physical = {target: secrets[key] for key, target in SECRET_ENV.items()}
    physical.update(data["runtime"])
    physical.update(
        LIGHTRAG_DEPLOYMENT_ID="lightrag_prod",
        WORKSPACE="lightrag_prod",
        S3_OBJECT_PREFIX="prod/objects",
        EMBEDDING_BINDING_HOST=secrets["llm_binding_host"],
    )
    data["onboarding"]["profile_hash"] = physical_profile_hash(physical)
    config = load(tmp_path, data)
    archive = tmp_path / "source.tar.gz"
    archive.write_bytes(b"synthetic-source-only")
    record = dict(
        repo=config.repo,
        tag="v1.0.0",
        commit="a" * 40,
        pipeline_id="12",
        deploy_environment=None,
        sha256=hashlib.sha256(archive.read_bytes()).hexdigest(),
    )
    image = dict(
        image="registry.invalid/rag",
        digest="sha256:" + "b" * 64,
        revision="a" * 40,
        tag="v1.0.0",
        platform="linux/amd64",
        image_ref="registry.invalid/rag@sha256:" + "b" * 64,
    )
    env = {
        "CI_PIPELINE_EVENT": "tag",
        "CI_COMMIT_TAG": "v1.0.0",
        "CI_COMMIT_SHA": "a" * 40,
        "CI_PIPELINE_NUMBER": "12",
        "CI_REPO": config.repo,
    }
    env.update(
        {"LIGHTRAG_PROD_" + key.upper(): value for key, value in secrets.items()}
    )
    return config, image, archive, record, env


def test_real_inputs_bind_archive_image_environment_and_control_job(tmp_path):
    from scripts.ci.prod_deploy import verified_inputs, render_control_job

    config, image, archive, record, env = inputs(tmp_path)
    identity, runtime, repository = verified_inputs(
        config,
        image_record=image,
        source_archive=archive,
        source_record=record,
        environment=env,
    )
    resources = render_control_job(
        config, identity, runtime, image=repository, attempt="12"
    )
    job = resources[-1]
    assert (
        job["spec"]["backoffLimit"] == 0 and "activeDeadlineSeconds" not in job["spec"]
    )
    container = job["spec"]["template"]["spec"]["containers"][0]
    assert container["image"].endswith(identity["digest"])
    assert container["envFrom"] == [
        {"secretRef": {"name": config.onboarding["operator_secret"]}}
    ]
    assert resources[0]["immutable"] and resources[1]["immutable"]
    assert "synthetic-private" not in json.dumps(resources[0])
    assert "LIGHTRAG_RELEASE_OPERATOR_DSN" not in runtime


@pytest.mark.parametrize("failure", ["archive", "image", "tag", "secrets", "physical"])
def test_invalid_inputs_fail_before_any_kubernetes_operation(tmp_path, failure):
    from scripts.ci.prod_deploy import verified_inputs

    config, image, archive, record, env = inputs(tmp_path)
    if failure == "archive":
        record["pipeline_id"] = "old"
    if failure == "image":
        image["revision"] = "c" * 40
    if failure == "tag":
        env["CI_COMMIT_TAG"] = "v1.0.0-test"
    if failure == "secrets":
        del env["LIGHTRAG_PROD_POSTGRES_PASSWORD"]
    if failure == "physical":
        env["LIGHTRAG_PROD_S3_BUCKET"] = "test"
    from scripts.ci.delivery import ArchiveValidationError

    with pytest.raises((ValueError, ArchiveValidationError)):
        verified_inputs(
            config,
            image_record=image,
            source_archive=archive,
            source_record=record,
            environment=env,
        )
