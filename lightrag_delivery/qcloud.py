"""Tencent CLB read-only observer. COS keys are never a credential fallback."""

import datetime
import hashlib
import hmac
import json
import time
import urllib.request


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        raise ValueError("Cloud API redirects are forbidden")


class QCloudObserver:
    SERVICE = "clb"
    VERSION = "2018-03-17"
    ACTIONS = {"DescribeTargets", "DescribeTargetHealth", "DescribeListeners"}

    def __init__(
        self, *, region, secret_id, secret_key, token=None, opener=None, edgeone=None
    ):
        if not all(isinstance(v, str) and v for v in (region, secret_id, secret_key)):
            raise ValueError("Explicit read-only CLB credentials required")
        self.region, self.secret_id, self.secret_key, self.token = (
            region,
            secret_id,
            secret_key,
            token,
        )
        self.opener = opener or urllib.request.build_opener(
            urllib.request.ProxyHandler({}), NoRedirect()
        )
        self.edgeone = edgeone

    def call(self, action, payload):
        if action not in self.ACTIONS:
            raise ValueError("Only approved read-only CLB actions are permitted")
        body = json.dumps(payload, separators=(",", ":")).encode()
        now = int(time.time())
        date = datetime.datetime.fromtimestamp(now, datetime.timezone.utc).strftime(
            "%Y-%m-%d"
        )
        host = self.SERVICE + ".tencentcloudapi.com"
        content_type = "application/json; charset=utf-8"
        canonical = (
            "POST\n/\n\ncontent-type:"
            + content_type
            + "\nhost:"
            + host
            + "\n\ncontent-type;host\n"
            + hashlib.sha256(body).hexdigest()
        )
        scope = date + "/" + self.SERVICE + "/tc3_request"
        message = (
            "TC3-HMAC-SHA256\n"
            + str(now)
            + "\n"
            + scope
            + "\n"
            + hashlib.sha256(canonical.encode()).hexdigest()
        )

        def sign(key, value):
            return hmac.new(key, value.encode(), hashlib.sha256).digest()

        key = sign(
            sign(sign(("TC3" + self.secret_key).encode(), date), self.SERVICE),
            "tc3_request",
        )
        signature = hmac.new(key, message.encode(), hashlib.sha256).hexdigest()
        headers = {
            "Content-Type": content_type,
            "Host": host,
            "Authorization": "TC3-HMAC-SHA256 Credential="
            + self.secret_id
            + "/"
            + scope
            + ", SignedHeaders=content-type;host, Signature="
            + signature,
            "X-TC-Action": action,
            "X-TC-Version": self.VERSION,
            "X-TC-Region": self.region,
            "X-TC-Timestamp": str(now),
        }
        if self.token:
            headers["X-TC-Token"] = self.token
        try:
            with self.opener.open(
                urllib.request.Request(
                    "https://" + host, data=body, headers=headers, method="POST"
                ),
                timeout=10,
            ) as response:
                raw = response.read(4 * 1024 * 1024 + 1)
                if len(raw) > 4 * 1024 * 1024:
                    raise ValueError()
                result = json.loads(raw)["Response"]
                if "Error" in result or not result.get("RequestId"):
                    raise ValueError()
                return result
        except Exception:
            raise ValueError(
                "CLB observation failed; no route proof available"
            ) from None

    def observe(self, config, *, expected_ips, port):
        edgeone_proof = (
            self.verify_edgeone(config) if config.get("mode") == "edgeone" else None
        )
        targets = self.call(
            "DescribeTargets",
            {
                "LoadBalancerId": config["load_balancer_id"],
                "ListenerIds": [config["listener_id"]],
                "Filters": [{"Name": "location-id", "Values": [config["location_id"]]}],
            },
        )
        health = self.call(
            "DescribeTargetHealth",
            {
                "LoadBalancerIds": [config["load_balancer_id"]],
                "ListenerIds": [config["listener_id"]],
            },
        )
        verify_backends(config, targets, health, expected_ips=expected_ips, port=port)
        result = {
            "provider": "qcloud",
            "request_ids": [targets["RequestId"], health["RequestId"]],
            "backend_count": len(expected_ips),
        }
        if edgeone_proof is not None:
            result["edgeone"] = edgeone_proof
        return result

    def verify_edgeone(self, config):
        if config.get("mode") != "edgeone" or self.edgeone is None:
            raise ValueError("Explicit independent EdgeOne observer is required")
        return self.edgeone.observe(config["edgeone"])


def _one(values, field, expected):
    rows = [v for v in values if v.get(field) == expected]
    if len(rows) != 1:
        raise ValueError("CLB rule identity is missing or ambiguous")
    return rows[0]


def verify_backends(config, targets, health, *, expected_ips, port):
    listener = _one(targets.get("Listeners", []), "ListenerId", config["listener_id"])
    mode, protocol = config.get("mode", "direct"), config.get("protocol", "HTTPS")
    if (mode, protocol) not in {
        ("direct", "HTTPS"),
        ("edgeone", "HTTP"),
    } or listener.get("Protocol") != protocol:
        raise ValueError(
            "Listener protocol differs from the approved cloud termination profile"
        )
    rule = _one(listener.get("Rules", []), "LocationId", config["location_id"])
    bound = {}
    for backend in rule.get("Targets", []):
        ips = backend.get("PrivateIpAddresses", [])
        if len(ips) != 1 or backend.get("Port") != port or ips[0] in bound:
            raise ValueError("CLB backend identity is unknown or duplicated")
        bound[ips[0]] = backend.get("Weight")
    if set(bound) != set(expected_ips) or any(
        type(w) is not int or w <= 0 for w in bound.values()
    ):
        raise ValueError("CLB backend propagation is incomplete")
    lb = _one(
        health.get("LoadBalancers", []), "LoadBalancerId", config["load_balancer_id"]
    )
    checks = _one(
        _one(lb.get("Listeners", []), "ListenerId", config["listener_id"]).get(
            "Rules", []
        ),
        "LocationId",
        config["location_id"],
    )
    checked = {}
    for value in checks.get("Targets", []):
        if value.get("IP") in checked:
            raise ValueError("Duplicate CLB health result")
        checked[value.get("IP")] = value
    if set(checked) != set(expected_ips) or any(
        v.get("Port") != port
        or v.get("HealthStatus") is not True
        or v.get("Weight", 0) <= 0
        for v in checked.values()
    ):
        raise ValueError("Not all expected CLB backends are healthy")
    return True


def verify_entry_response(
    response, *, slot, instance_ids, profile_hash, config_hash=None
):
    if (
        response.get("slot") != slot
        or response.get("instance_id") not in instance_ids
        or response.get("profile_hash") != profile_hash
        or response.get("readiness", {}).get("ready") is not True
        or (config_hash is not None and response.get("config_hash") != config_hash)
    ):
        raise ValueError("Public entry did not reach an admitted candidate")
    return True
