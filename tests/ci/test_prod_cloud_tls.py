"""Production TLS stays in the cloud; application Kubernetes backends are HTTP."""

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from tests.ci.test_prod_config import config_data, load
from tests.ci.test_release_profile import profile_env


def identity():
    return {
        "scope": "prod/prod",
        "release_id": "fixture",
        "tag": "v1.0.0",
        "commit": "a" * 40,
        "digest": "sha256:" + "b" * 64,
        "config_hash": "c" * 64,
    }


def test_cloud_certificate_configuration_needs_no_kubernetes_tls_secret(tmp_path):
    data = config_data()
    data["target"].pop("tls_secret", None)
    config = load(tmp_path, data)
    assert config.ingress["protocol"] == "HTTPS"
    assert config.ingress["certificate_id"] == "fixture"
    assert "tls_secret" not in config.target


def test_legacy_kubernetes_tls_secret_input_is_rejected(tmp_path):
    from lightrag_delivery.config import ProdConfigError

    data = config_data()
    data["target"]["tls_secret"] = "forbidden-certificate"
    with pytest.raises(ProdConfigError, match="target"):
        load(tmp_path, data)


@pytest.mark.parametrize(
    "change",
    [
        {"certificate_id": ""},
        {"protocol": "HTTP"},
        {"force_close_disabled": False},
    ],
)
def test_current_approved_clb_profile_is_not_relaxed_by_http_pods(tmp_path, change):
    from lightrag_delivery.config import ProdConfigError

    data = config_data()
    data["ingress"].update(change)
    with pytest.raises(ProdConfigError):
        load(tmp_path, data)


def test_onboarding_schema_does_not_request_a_kubernetes_tls_secret():
    schema = json.loads(
        Path(
            "k8s-deploy/lightrag-kustomize/overlays/prod/onboarding.schema.json"
        ).read_text()
    )
    target = schema["properties"]["target"]
    assert "tls_secret" not in target["properties"]
    assert "tls_secret" not in target["required"]
    assert target["additionalProperties"] is False


@pytest.mark.parametrize(
    "environment",
    [
        {"SSL": "true"},
        {"SSL_CERTFILE": "/certs/server.crt"},
        {"SSL_KEYFILE": "/certs/server.key"},
    ],
)
def test_slot_render_rejects_application_tls_before_producing_manifests(
    tmp_path, environment
):
    from lightrag_delivery.render import render_slot

    config = load(tmp_path, config_data())
    with pytest.raises(ValueError, match="TLS"):
        render_slot(
            config,
            identity(),
            "blue",
            image="registry.invalid/rag",
            environment=environment,
        )


def test_all_application_resources_use_http_without_certificate_secrets(tmp_path):
    from lightrag_delivery.render import (
        render_entrypoint,
        render_foundation,
        render_slot,
    )

    config = load(tmp_path, config_data())
    resources = render_foundation(config) + render_entrypoint(config, "blue")
    for slot in ("blue", "green"):
        resources += render_slot(
            config, identity(), slot, image="registry.invalid/rag", environment={}
        )
    assert all(r.get("type") != "kubernetes.io/tls" for r in resources)
    ingress = next(r for r in resources if r["kind"] == "Ingress")
    assert not ingress["spec"].get("tls")
    assert json.loads(
        ingress["metadata"]["annotations"]["ingress.cloud.tencent.com/certificate"]
    ) == [{"hosts": ["rag.example.invalid"], "qcloud_cert_id": ["fixture"]}]
    for deploy in (r for r in resources if r["kind"] == "Deployment"):
        pod = deploy["spec"]["template"]["spec"]
        assert not any(v.get("secret") for v in pod["volumes"])
        container = pod["containers"][0]
        env = {v["name"]: v.get("value") for v in container["env"]}
        assert env["SSL"] == "false"
        assert "SSL_CERTFILE" not in env and "SSL_KEYFILE" not in env
        assert container["ports"] == [{"name": "http", "containerPort": 9621}]
        for name in ("readinessProbe", "startupProbe"):
            assert container[name]["httpGet"].get("scheme", "HTTP") == "HTTP"


@pytest.mark.parametrize(
    "change",
    [
        {"ssl": True},
        {"ssl_certfile": "/certs/server.crt"},
        {"ssl_keyfile": "/certs/server.key"},
    ],
)
def test_server_profile_rejects_local_tls_even_when_embedding_matches(change):
    from lightrag_delivery.profile import verify_server_profile

    env = profile_env()
    args = SimpleNamespace(
        **{
            key.lower(): value
            for key, value in env.items()
            if key.startswith("EMBEDDING_")
        }
    )
    for key, value in change.items():
        setattr(args, key, value)
    with pytest.raises(ValueError, match="TLS"):
        verify_server_profile(args, env)


def test_http_backend_keeps_existing_physical_profile_verification():
    from lightrag_delivery.profile import verify_server_profile

    env = profile_env()
    args = SimpleNamespace(
        **{
            key.lower(): value
            for key, value in env.items()
            if key.startswith("EMBEDDING_")
        },
        ssl=False,
        ssl_certfile=None,
        ssl_keyfile=None,
    )
    verify_server_profile(args, env)
    args.embedding_model = "different-model"
    with pytest.raises(ValueError, match="embedding_model"):
        verify_server_profile(args, env)
