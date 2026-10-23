"""Read-only EO origin proof; reviewed configuration hashes are not semantic audits."""

import ipaddress
import os
import re

from .config import ProdConfigError, _keys, _text
from .qcloud import QCloudObserver
from .render import content_hash

PROFILE_FIELDS = {
    "zone_id",
    "host",
    "origin_address",
    "origin_protocol",
    "origin_port",
    "origin_host",
    "domain_hash",
    "settings_hash",
    "rules_hash",
    "review_evidence_id",
}
DOMAIN_FIELDS = {
    "ZoneId",
    "DomainName",
    "DomainStatus",
    "IdentificationStatus",
    "OriginDetail",
    "Certificate",
    "Cname",
}
RULE_LIMIT = 1000


def validate_profile(profile, *, host):
    """Require reviewed, explicit HTTP origin bindings; never infer live values."""
    _keys(profile, PROFILE_FIELDS, "ingress.edgeone")
    if not isinstance(profile["zone_id"], str) or not re.fullmatch(
        r"zone-[a-z0-9]+", profile["zone_id"]
    ):
        raise ProdConfigError("ingress.edgeone.zone_id: explicit zone ID required")
    if profile["host"] != host or profile["origin_host"] != host:
        raise ProdConfigError("ingress.edgeone: origin and public hosts must match")
    try:
        address = ipaddress.IPv4Address(profile["origin_address"])
        if (
            address.is_unspecified
            or address.is_loopback
            or address.is_multicast
            or address.is_link_local
        ):
            raise ValueError()
    except (ValueError, TypeError):
        raise ProdConfigError(
            "ingress.edgeone.origin_address: explicit CLB IPv4 required"
        ) from None
    if (
        profile["origin_protocol"] != "http"
        or type(profile["origin_port"]) is not int
        or profile["origin_port"] != 80
    ):
        raise ProdConfigError("ingress.edgeone: reviewed HTTP/80 origin required")
    for key in ("domain_hash", "settings_hash", "rules_hash"):
        if not isinstance(profile[key], str) or not re.fullmatch(
            "[a-f0-9]{64}", profile[key]
        ):
            raise ProdConfigError(
                "ingress.edgeone." + key + ": approved SHA256 required"
            )
    _text(profile["review_evidence_id"], "ingress.edgeone.review_evidence_id")


class EdgeOneObserver(QCloudObserver):
    SERVICE = "teo"
    VERSION = "2022-09-01"
    ACTIONS = {
        "DescribeAccelerationDomains",
        "DescribeL7AccSetting",
        "DescribeL7AccRules",
    }

    def observe(self, profile):
        """Prove current bindings and approved snapshots, not effective rule semantics."""
        validate_profile(profile, host=profile["host"])
        domain_response = self.call(
            "DescribeAccelerationDomains",
            {
                "ZoneId": profile["zone_id"],
                "Offset": 0,
                "Limit": 2,
                "Filters": [
                    {"Name": "domain-name", "Values": [profile["host"]], "Fuzzy": False}
                ],
            },
        )
        domains = domain_response.get("AccelerationDomains")
        if (
            type(domain_response.get("TotalCount")) is not int
            or domain_response["TotalCount"] != 1
            or not isinstance(domains, list)
            or len(domains) != 1
        ):
            raise ValueError("EdgeOne domain is unavailable or ambiguous")
        domain = domains[0]
        if not isinstance(domain, dict) or not DOMAIN_FIELDS.issubset(domain):
            raise ValueError("EdgeOne domain metadata is incomplete")
        origin, certificate = domain["OriginDetail"], domain["Certificate"]
        if (
            domain["ZoneId"] != profile["zone_id"]
            or domain["DomainName"] != profile["host"]
            or domain["DomainStatus"] != "online"
            or domain["IdentificationStatus"] != "finished"
            or not isinstance(origin, dict)
            or origin.get("OriginType") != "IP_DOMAIN"
            or origin.get("Origin") != profile["origin_address"]
            or origin.get("BackupOrigin")
            or not isinstance(certificate, dict)
            or certificate.get("Mode") not in {"sslcert", "eofreecert"}
        ):
            raise ValueError(
                "EdgeOne HTTPS domain or origin does not match the approved profile"
            )
        contract = {key: domain[key] for key in DOMAIN_FIELDS}
        if content_hash(contract) != profile["domain_hash"]:
            raise ValueError("EdgeOne reviewed domain configuration drifted")
        settings_response = self.call(
            "DescribeL7AccSetting", {"ZoneId": profile["zone_id"]}
        )
        settings = settings_response.get("ZoneSetting")
        if (
            not isinstance(settings, dict)
            or not settings
            or content_hash(settings) != profile["settings_hash"]
        ):
            raise ValueError(
                "EdgeOne reviewed global settings are unavailable or drifted"
            )
        rules_response = self.call(
            "DescribeL7AccRules",
            {"ZoneId": profile["zone_id"], "Limit": RULE_LIMIT, "Offset": 0},
        )
        rules, count = rules_response.get("Rules"), rules_response.get("TotalCount")
        if (
            type(count) is not int
            or not 0 <= count <= RULE_LIMIT
            or not isinstance(rules, list)
            or count != len(rules)
            or content_hash(rules) != profile["rules_hash"]
        ):
            raise ValueError("EdgeOne reviewed ordered rules are incomplete or drifted")
        ids = [
            response.get("RequestId")
            for response in (domain_response, settings_response, rules_response)
        ]
        if not all(isinstance(value, str) and value for value in ids):
            raise ValueError("EdgeOne observation request identity is unavailable")
        return {
            "provider": "edgeone",
            "zone_id": profile["zone_id"],
            "host": profile["host"],
            "request_ids": ids,
            **{
                key: profile[key]
                for key in ("domain_hash", "settings_hash", "rules_hash")
            },
        }


def cloud_observer(ingress):
    """Keep EO and CLB observation identities explicit and outside application Pods."""
    edgeone = None
    if ingress["mode"] == "edgeone":
        edgeone = EdgeOneObserver(
            region=ingress["region"],
            secret_id=os.environ.get("LIGHTRAG_PROD_EDGEONE_SECRET_ID", ""),
            secret_key=os.environ.get("LIGHTRAG_PROD_EDGEONE_SECRET_KEY", ""),
            token=os.environ.get("LIGHTRAG_PROD_EDGEONE_TOKEN"),
        )
    return QCloudObserver(
        region=ingress["region"],
        secret_id=os.environ.get("LIGHTRAG_PROD_CLB_SECRET_ID", ""),
        secret_key=os.environ.get("LIGHTRAG_PROD_CLB_SECRET_KEY", ""),
        token=os.environ.get("LIGHTRAG_PROD_CLB_TOKEN"),
        edgeone=edgeone,
    )
