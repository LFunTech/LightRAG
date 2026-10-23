"""Explicit EO HTTPS termination with verified HTTP CLB/Pod origins."""

from copy import deepcopy
import json
from pathlib import Path

import pytest

from lightrag_delivery.render import content_hash
from tests.ci.test_prod_config import config_data, load


def cloud_records():
    domain = {
        "ZoneId": "zone-fixture",
        "DomainName": "rag.example.invalid",
        "DomainStatus": "online",
        "IdentificationStatus": "finished",
        "OriginDetail": {
            "OriginType": "IP_DOMAIN",
            "Origin": "203.0.113.10",
            "BackupOrigin": "",
        },
        "Certificate": {"Mode": "sslcert", "List": [{"CertId": "fixture-cloud-cert"}]},
        "Cname": "fixture.eo.example.invalid",
    }
    settings = {
        "ZoneName": "example.invalid",
        "ZoneConfig": {"Cache": {"NoCache": {"Switch": "on"}}},
    }
    return domain, settings, []


def edgeone_data():
    data = config_data()
    domain, settings, rules = cloud_records()
    data["ingress"].update(
        mode="edgeone",
        protocol="HTTP",
        certificate_id="",
        edgeone={
            "zone_id": "zone-fixture",
            "host": data["target"]["host"],
            "origin_address": "203.0.113.10",
            "origin_protocol": "http",
            "origin_port": 80,
            "origin_host": data["target"]["host"],
            "domain_hash": content_hash(domain),
            "settings_hash": content_hash(settings),
            "rules_hash": content_hash(rules),
            "review_evidence_id": "fixture-ops-review",
        },
    )
    return data


def test_explicit_edgeone_profile_accepts_http_origin_without_clb_certificate(tmp_path):
    config = load(tmp_path, edgeone_data())
    assert config.ingress["mode"] == "edgeone"
    assert config.ingress["protocol"] == "HTTP"
    assert config.ingress["certificate_id"] == ""


@pytest.mark.parametrize(
    "field,value",
    [
        ("zone_id", ""),
        ("host", "other.invalid"),
        ("origin_address", "0.0.0.0"),
        ("origin_address", "127.0.0.1"),
        ("origin_address", "origin.invalid"),
        ("origin_protocol", "follow"),
        ("origin_port", 443),
        ("origin_port", True),
        ("origin_host", "other.invalid"),
        ("domain_hash", ""),
        ("settings_hash", "x" * 64),
        ("rules_hash", None),
        ("review_evidence_id", ""),
    ],
)
def test_edgeone_profile_rejects_missing_or_ambiguous_approval(tmp_path, field, value):
    data = edgeone_data()
    data["ingress"]["edgeone"][field] = value
    with pytest.raises(ValueError):
        load(tmp_path, data)


def test_http_is_not_an_implicit_fallback_for_direct_profile(tmp_path):
    data = config_data()
    data["ingress"]["protocol"] = "HTTP"
    with pytest.raises(ValueError):
        load(tmp_path, data)
    data = edgeone_data()
    data["ingress"]["certificate_id"] = "not-an-eo-certificate-binding"
    with pytest.raises(ValueError):
        load(tmp_path, data)


def test_edgeone_ingress_is_only_http_and_has_no_certificate_metadata(tmp_path):
    from lightrag_delivery.render import render_entrypoint

    config = load(tmp_path, edgeone_data())
    service, ingress = render_entrypoint(config, "blue")
    assert service["spec"]["ports"][0]["targetPort"] == 9621
    assert not ingress["spec"].get("tls")
    annotations = ingress["metadata"]["annotations"]
    assert not any(
        "https" in key.lower() or "certificate" in key.lower() for key in annotations
    )
    rules = json.loads(annotations["kubernetes.io/ingress.http-rules"])
    assert rules == [
        {
            "host": config.target["host"],
            "path": "/",
            "backend": {"serviceName": "lightrag", "servicePort": 9621},
        }
    ]


def observer(records=None):
    from lightrag_delivery.edgeone import EdgeOneObserver

    domain, settings, rules = cloud_records()
    records = records or {
        "DescribeAccelerationDomains": {
            "TotalCount": 1,
            "AccelerationDomains": [domain],
            "RequestId": "domain-read",
        },
        "DescribeL7AccSetting": {"ZoneSetting": settings, "RequestId": "settings-read"},
        "DescribeL7AccRules": {
            "TotalCount": len(rules),
            "Rules": rules,
            "RequestId": "rules-read",
        },
    }
    result = EdgeOneObserver(
        region="ap-shanghai", secret_id="fixture-eo-id", secret_key="fixture-eo-key"
    )
    result.call = lambda action, payload: deepcopy(records[action])
    return result, records


def test_observer_proves_exact_domain_origin_and_reviewed_configuration():
    cloud, _ = observer()
    result = cloud.observe(edgeone_data()["ingress"]["edgeone"])
    assert result["provider"] == "edgeone"
    assert result["host"] == "rag.example.invalid"
    assert result["request_ids"] == ["domain-read", "settings-read", "rules-read"]
    assert "OriginDetail" not in json.dumps(result)
    assert "fixture-cloud-cert" not in json.dumps(result)


@pytest.mark.parametrize(
    "field,value",
    [
        ("DomainName", "old.invalid"),
        ("ZoneId", "zone-other"),
        ("DomainStatus", "offline"),
        ("IdentificationStatus", "pending"),
        ("OriginDetail", {"OriginType": "IP_DOMAIN", "Origin": "203.0.113.99"}),
        (
            "OriginDetail",
            {
                "OriginType": "IP_DOMAIN",
                "Origin": "203.0.113.10",
                "BackupOrigin": "other.invalid",
            },
        ),
        ("Certificate", {"Mode": "none"}),
    ],
)
def test_observer_rejects_wrong_origin_or_unavailable_https(field, value):
    cloud, records = observer()
    records["DescribeAccelerationDomains"]["AccelerationDomains"][0][field] = value
    with pytest.raises(ValueError):
        cloud.observe(edgeone_data()["ingress"]["edgeone"])


@pytest.mark.parametrize("component", ["domain", "settings", "rules"])
def test_observer_rejects_even_re_reviewable_configuration_drift(component):
    cloud, records = observer()
    if component == "domain":
        records["DescribeAccelerationDomains"]["AccelerationDomains"][0]["Certificate"][
            "List"
        ][0]["CertId"] = "changed"
    elif component == "settings":
        records["DescribeL7AccSetting"]["ZoneSetting"]["ZoneConfig"]["Cache"][
            "NoCache"
        ]["Switch"] = "off"
    else:
        records["DescribeL7AccRules"].update(
            TotalCount=1, Rules=[{"RuleId": "changed"}]
        )
    with pytest.raises(ValueError):
        cloud.observe(edgeone_data()["ingress"]["edgeone"])


@pytest.mark.parametrize(
    "response",
    [
        {"TotalCount": 1, "Rules": []},
        {"TotalCount": 0, "Rules": None},
        {"TotalCount": True, "Rules": [{}]},
        {"TotalCount": 1001, "Rules": []},
    ],
)
def test_partial_or_unknown_rules_are_not_an_empty_reviewed_configuration(response):
    cloud, records = observer()
    records["DescribeL7AccRules"] = dict(response, RequestId="rules-read")
    with pytest.raises(ValueError):
        cloud.observe(edgeone_data()["ingress"]["edgeone"])


def test_edgeone_api_has_distinct_credentials_and_signed_read_only_endpoint():
    from lightrag_delivery.edgeone import EdgeOneObserver

    seen = []

    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def read(self, size):
            return b'{"Response":{"RequestId":"fixture-request","TotalCount":0,"Rules":[]}}'

    class Opener:
        def open(self, request, timeout):
            seen.append(request)
            return Response()

    cloud = EdgeOneObserver(
        region="ap-shanghai",
        secret_id="fixture-eo-id",
        secret_key="fixture-eo-key",
        opener=Opener(),
    )
    assert (
        cloud.call("DescribeL7AccRules", {"ZoneId": "zone-fixture"})["RequestId"]
        == "fixture-request"
    )
    assert seen[0].full_url == "https://teo.tencentcloudapi.com"
    assert seen[0].headers["X-tc-version"] == "2022-09-01"
    assert "fixture-eo-id/" in seen[0].headers["Authorization"]
    assert "fixture-eo-key" not in str(seen[0].headers)
    for action in ("ModifyAccelerationDomain", "DescribeListeners"):
        with pytest.raises(ValueError):
            cloud.call(action, {})
    assert len(seen) == 1


def test_missing_edgeone_identity_never_uses_clb_or_cos_credentials(monkeypatch):
    from lightrag_delivery.edgeone import cloud_observer

    monkeypatch.setenv("LIGHTRAG_PROD_CLB_SECRET_ID", "fixture-clb-id")
    monkeypatch.setenv("LIGHTRAG_PROD_CLB_SECRET_KEY", "fixture-clb-key")
    monkeypatch.setenv("COS_SECRET_KEY", "fixture-cos-key")
    monkeypatch.delenv("LIGHTRAG_PROD_EDGEONE_SECRET_ID", raising=False)
    monkeypatch.delenv("LIGHTRAG_PROD_EDGEONE_SECRET_KEY", raising=False)
    with pytest.raises(ValueError):
        cloud_observer(edgeone_data()["ingress"])
    assert cloud_observer(config_data()["ingress"]).edgeone is None


def test_onboarding_schema_documents_only_explicit_edgeone_http_profile():
    schema = json.loads(
        Path(
            "k8s-deploy/lightrag-kustomize/overlays/prod/onboarding.schema.json"
        ).read_text()
    )
    ingress = schema["properties"]["ingress"]
    edgeone = ingress["properties"]["edgeone"]
    assert edgeone["additionalProperties"] is False
    assert set(edgeone["required"]) == set(edgeone_data()["ingress"]["edgeone"])
    assert ingress["allOf"]


@pytest.mark.parametrize(
    "mode,protocol,listener_protocol,accepted",
    [
        ("edgeone", "HTTP", "HTTP", True),
        ("edgeone", "HTTP", "HTTPS", False),
        ("direct", "HTTPS", "HTTP", False),
        ("unknown", "HTTP", "HTTP", False),
    ],
)
def test_http_clb_backends_are_admitted_only_for_explicit_edgeone(
    mode, protocol, listener_protocol, accepted
):
    from lightrag_delivery.qcloud import verify_backends

    config = {
        "mode": mode,
        "protocol": protocol,
        "load_balancer_id": "lb-a",
        "listener_id": "lbl-a",
        "location_id": "loc-a",
    }
    targets = {
        "Listeners": [
            {
                "ListenerId": "lbl-a",
                "Protocol": listener_protocol,
                "Rules": [
                    {
                        "LocationId": "loc-a",
                        "Targets": [
                            {
                                "PrivateIpAddresses": ["10.1.0.1"],
                                "Port": 9621,
                                "Weight": 10,
                            }
                        ],
                    }
                ],
            }
        ]
    }
    health = {
        "LoadBalancers": [
            {
                "LoadBalancerId": "lb-a",
                "Listeners": [
                    {
                        "ListenerId": "lbl-a",
                        "Rules": [
                            {
                                "LocationId": "loc-a",
                                "Targets": [
                                    {
                                        "IP": "10.1.0.1",
                                        "Port": 9621,
                                        "HealthStatus": True,
                                        "Weight": 10,
                                    }
                                ],
                            }
                        ],
                    }
                ],
            }
        ]
    }
    if accepted:
        assert verify_backends(
            config, targets, health, expected_ips={"10.1.0.1"}, port=9621
        )
    else:
        with pytest.raises(ValueError):
            verify_backends(
                config, targets, health, expected_ips={"10.1.0.1"}, port=9621
            )


@pytest.mark.parametrize("ingress", [None, [], "not-an-object"])
def test_malformed_ingress_remains_a_field_only_config_error(tmp_path, ingress):
    from lightrag_delivery.config import ProdConfigError

    data = config_data()
    data["ingress"] = ingress
    with pytest.raises(ProdConfigError, match="ingress"):
        load(tmp_path, data)


@pytest.mark.parametrize("field", ["mode", "protocol"])
@pytest.mark.parametrize("value", [[], {}, None, True, 1])
def test_ingress_discriminators_reject_non_strings_without_type_errors(
    tmp_path, field, value
):
    from lightrag_delivery.config import ProdConfigError

    data = config_data()
    data["ingress"][field] = value
    with pytest.raises(ProdConfigError, match="ingress"):
        load(tmp_path, data)


@pytest.mark.parametrize("mode", ["direct", "edgeone"])
async def test_transport_refuses_kubernetes_tls_even_with_matching_cloud_route(
    tmp_path, mode
):
    from contextlib import asynccontextmanager
    from types import SimpleNamespace
    from lightrag.distributed.release.transport import ProductionTransport
    from lightrag.distributed.release.models import ReleaseConflict

    config = load(tmp_path, edgeone_data() if mode == "edgeone" else config_data())

    class DB:
        async def fetchval(self, *args):
            return config.onboarding["baseline_manifest_hash"]

    class Store:
        @asynccontextmanager
        async def connection(self):
            yield DB()

        async def _scope(self, *args):
            return {
                "profile_hash": config.onboarding["profile_hash"],
                "policy": dict(config.budget, writers=config.writers),
            }

    class Kube:
        def namespace_uid(self, name):
            return config.onboarding[
                "kube_system_uid" if name == "kube-system" else "namespace_uid"
            ]

        def get(self, *args):
            return {
                "metadata": {
                    "annotations": {
                        "ingress.cloud.tencent.com/direct-access": "true",
                        "kubernetes.io/ingress.qcloud-loadbalance-id": config.ingress[
                            "load_balancer_id"
                        ],
                    }
                },
                "spec": {
                    "ingressClassName": "qcloud",
                    "tls": [{"secretName": "forbidden"}],
                },
            }

    transport = ProductionTransport(
        config=config,
        kubernetes=Kube(),
        cloud=object(),
        store=Store(),
        image="registry.invalid/rag",
        environment={},
    )
    with pytest.raises(ReleaseConflict, match="TLS"):
        await transport.preflight(SimpleNamespace(scope="prod/prod"))
