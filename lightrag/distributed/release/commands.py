"""Explicit versioned command codecs; never persist callbacks or bearer tokens."""

from dataclasses import asdict, dataclass, field
from hashlib import sha256
import json
from typing import Any

from .models import ReleaseError


def canonical(value):
    try:
        return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)
    except (TypeError, ValueError):
        raise ReleaseError("Command payload must be versioned JSON data") from None


@dataclass(frozen=True)
class CommandEnvelope:
    operation: str
    version: int
    scope: str
    principal: dict[str, Any]
    idempotency_key: str
    payload_hash: str
    payload: dict[str, Any]
    source_refs: list[str] = field(default_factory=list)

    def as_dict(self):
        return asdict(self)


class CommandRegistry:
    def __init__(self):
        self.handlers = {}

    def register(self, name, *, version, handler):
        if (
            (name, version) in self.handlers
            or type(version) is not int
            or version != 1
            or not callable(handler)
        ):
            raise ReleaseError("Invalid or duplicate command registration")
        self.handlers[name, version] = handler

    def encode(self, operation, payload, *, principal, scope, key, source_refs=None):
        if (operation, 1) not in self.handlers:
            raise ReleaseError("Unregistered writer operation")
        allowed = {
            "id",
            "scheme",
            "subject",
            "credential_fingerprint",
            "expires_at",
            "roles",
            "authorization_version",
        }
        if (
            not isinstance(principal, dict)
            or set(principal) - allowed
            or not isinstance(principal.get("id"), str)
            or not principal["id"]
            or not isinstance(payload, dict)
            or not isinstance(key, str)
            or not 1 <= len(key) <= 200
        ):
            raise ReleaseError("Invalid command principal or idempotency identity")
        if source_refs is not None and (
            not isinstance(source_refs, list)
            or len(source_refs) > 16
            or any(
                not isinstance(ref, str) or not ref or len(ref) > 4096
                for ref in source_refs
            )
        ):
            raise ReleaseError("Invalid command source references")
        normalized = json.loads(canonical(payload))
        material = {
            "operation": operation,
            "version": 1,
            "scope": scope,
            "payload": normalized,
            "source_refs": source_refs or [],
        }
        digest = sha256(canonical(material).encode()).hexdigest()
        canonical(principal)
        return CommandEnvelope(
            operation, 1, scope, principal, key, digest, normalized, source_refs or []
        )

    def handler(self, envelope):
        try:
            return self.handlers[envelope.operation, envelope.version]
        except KeyError:
            raise ReleaseError("Unsupported persisted command version") from None
