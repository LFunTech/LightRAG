"""Small in-cluster API client with explicit target and conditional route writes."""

import base64
import json
from pathlib import Path
import ssl
import urllib.error
import urllib.parse
import urllib.request
from .qcloud import NoRedirect
from .render import APP_LABEL, SLOT_LABEL

RESOURCE_PATHS = {
    "Pod": ("/api/v1", "pods"),
    "Service": ("/api/v1", "services"),
    "Secret": ("/api/v1", "secrets"),
    "ConfigMap": ("/api/v1", "configmaps"),
    "PersistentVolumeClaim": ("/api/v1", "persistentvolumeclaims"),
    "ServiceAccount": ("/api/v1", "serviceaccounts"),
    "Deployment": ("/apis/apps/v1", "deployments"),
    "EndpointSlice": ("/apis/discovery.k8s.io/v1", "endpointslices"),
    "Ingress": ("/apis/networking.k8s.io/v1", "ingresses"),
    "NetworkPolicy": ("/apis/networking.k8s.io/v1", "networkpolicies"),
}


def selector_patch(version, old, target):
    if (
        not version
        or not old.get(APP_LABEL)
        or target.get(APP_LABEL) != old[APP_LABEL]
        or set(old) - {APP_LABEL, SLOT_LABEL}
        or set(target) - {APP_LABEL, SLOT_LABEL}
        or target.get(SLOT_LABEL) not in {None, "blue", "green"}
    ):
        raise ValueError("Unsafe route selector change")
    return [
        dict(op="test", path="/metadata/resourceVersion", value=version),
        dict(op="test", path="/spec/selector", value=old),
        dict(op="replace", path="/spec/selector", value=target),
    ]


class Kubernetes:
    def __init__(self, *, server, namespace, token, ca_file, opener=None):
        parsed = urllib.parse.urlsplit(server)
        if (
            parsed.scheme != "https"
            or not parsed.hostname
            or parsed.username
            or parsed.password
            or parsed.path not in {"", "/"}
        ):
            raise ValueError("Explicit HTTPS Kubernetes server required")
        self.server = server.rstrip("/")
        self.namespace = namespace
        self.token = token
        self.opener = opener or urllib.request.build_opener(
            urllib.request.ProxyHandler({}),
            NoRedirect(),
            urllib.request.HTTPSHandler(
                context=ssl.create_default_context(cafile=ca_file)
            ),
        )

    def request(
        self, method, path, body=None, *, content_type="application/json", missing=False
    ):
        headers = {
            "Authorization": "Bearer " + self.token,
            "Content-Type": content_type,
            "Accept": "application/json",
        }
        encoded = (
            json.dumps(body, separators=(",", ":")).encode()
            if body is not None
            else None
        )
        try:
            with self.opener.open(
                urllib.request.Request(
                    self.server + path, data=encoded, headers=headers, method=method
                ),
                timeout=15,
            ) as response:
                raw = response.read(8 * 1024 * 1024 + 1)
                if len(raw) > 8 * 1024 * 1024:
                    raise ValueError("Kubernetes response exceeds bounded size")
                return json.loads(raw)
        except urllib.error.HTTPError as error:
            if missing and error.code == 404:
                return None
            raise ValueError("Kubernetes request failed: " + str(error.code)) from None
        except Exception:
            raise ValueError(
                "Kubernetes request outcome is unavailable; observe before retrying"
            ) from None

    def path(self, kind, name=None):
        base, resource = RESOURCE_PATHS[kind]
        return (
            base
            + "/namespaces/"
            + urllib.parse.quote(self.namespace, safe="")
            + "/"
            + resource
            + ("/" + urllib.parse.quote(name, safe="") if name else "")
        )

    def get(self, kind, name):
        return self.request("GET", self.path(kind, name), missing=True)

    def list(self, kind, selector):
        query = urllib.parse.urlencode(
            {
                "labelSelector": ",".join(
                    k + "=" + v for k, v in sorted(selector.items())
                ),
                "limit": 100,
            }
        )
        result = self.request("GET", self.path(kind) + "?" + query)
        if result.get("metadata", {}).get("continue"):
            raise ValueError(
                "Unexpected participants exceed the approved deployment bound"
            )
        return result["items"]

    def put_immutable(self, resource):
        kind = resource["kind"]
        name = resource["metadata"]["name"]
        if resource["metadata"]["namespace"] != self.namespace:
            raise ValueError("Cross-namespace mutation refused")
        old = self.get(kind, name)
        if old is None:
            return self.request("POST", self.path(kind), resource)
        if kind in {"Secret", "ConfigMap"}:
            desired = resource.get("data", {})
            if kind == "Secret":
                desired = desired | {
                    k: base64.b64encode(v.encode()).decode()
                    for k, v in resource.get("stringData", {}).items()
                }
            if old.get("data", {}) != desired or not old.get("immutable"):
                raise ValueError("Immutable release input differs")
            return old
        raise ValueError("Use a checked, resource-version conditional update")

    def patch(self, kind, name, patch):
        return self.request(
            "PATCH",
            self.path(kind, name),
            patch,
            content_type="application/json-patch+json",
        )

    def namespace_uid(self, name):
        return self.request(
            "GET", "/api/v1/namespaces/" + urllib.parse.quote(name, safe="")
        )["metadata"]["uid"]

    @classmethod
    def in_cluster(cls, namespace):
        import os

        root = Path("/var/run/secrets/kubernetes.io/serviceaccount")
        host = os.environ["KUBERNETES_SERVICE_HOST"]
        if ":" in host:
            host = "[" + host + "]"
        return cls(
            server="https://"
            + host
            + ":"
            + os.environ["KUBERNETES_SERVICE_PORT_HTTPS"],
            namespace=namespace,
            token=(root / "token").read_text().strip(),
            ca_file=str(root / "ca.crt"),
        )
