"""Recheck queued principals against current credentials, never persisted tokens."""

from hashlib import sha256
import hmac
import time

from .commands import canonical
from .models import ReleaseError


def fingerprint(value):
    return sha256(value.encode()).hexdigest()


class AuthorizationPolicy:
    def __init__(self, *, scope, api_key, auth_handler):
        self.scope = scope
        self.api_key = api_key
        self.auth_handler = auth_handler
        if not api_key and not auth_handler.accounts:
            raise ReleaseError("Production queued writes require authentication")

    def _account_fingerprint(self, subject):
        value = self.auth_handler.accounts.get(subject)
        if value is None:
            return None
        return fingerprint(
            canonical(
                [subject, value, self.auth_handler.secret, self.auth_handler.algorithm]
            )
        )

    async def authenticate(self, headers):
        authorization = headers.get("authorization", "")
        if authorization:
            if not authorization.startswith("Bearer "):
                raise ReleaseError("AuthenticationDenied")
            try:
                info = self.auth_handler.validate_token(authorization[7:])
            except Exception:
                raise ReleaseError("AuthenticationDenied") from None
            if self.auth_handler.accounts and info.get("role") != "guest":
                subject = info["username"]
                proof = self._account_fingerprint(subject)
                if proof is None:
                    raise ReleaseError("AuthenticationDenied")
                return {
                    "id": fingerprint("account:" + subject),
                    "scheme": "account",
                    "subject": subject,
                    "credential_fingerprint": proof,
                    "expires_at": info["exp"].timestamp(),
                    "roles": [info["role"]],
                }
        key = headers.get("x-api-key", "")
        if self.api_key and hmac.compare_digest(key, self.api_key):
            return {
                "id": fingerprint("api-key:" + self.api_key),
                "scheme": "api-key",
                "credential_fingerprint": fingerprint(self.api_key),
            }
        raise ReleaseError("AuthenticationDenied")

    async def authorize(self, principal, operation, scope):
        if scope != self.scope:
            return False
        if principal.get("scheme") == "api-key":
            return (
                bool(self.api_key)
                and hmac.compare_digest(
                    principal.get("credential_fingerprint", ""),
                    fingerprint(self.api_key),
                )
                and principal.get("id") == fingerprint("api-key:" + self.api_key)
            )
        if principal.get("scheme") == "account":
            expected = self._account_fingerprint(principal.get("subject"))
            return (
                expected is not None
                and principal.get("credential_fingerprint") == expected
                and principal.get("id")
                == fingerprint("account:" + principal["subject"])
                and isinstance(principal.get("expires_at"), (int, float))
                and principal["expires_at"] > time.time()
                and isinstance(principal.get("roles"), list)
                and len(principal["roles"]) == 1
                and isinstance(principal["roles"][0], str)
                and principal["roles"][0] not in {"", "guest"}
            )
        return False

    async def execution_headers(self, principal):
        if not await self.authorize(principal, "http.execute", self.scope):
            raise ReleaseError("AuthorizationDenied")
        if principal["scheme"] == "api-key":
            return [(b"x-api-key", self.api_key.encode())]
        token = self.auth_handler.create_token(
            principal["subject"], role=principal["roles"][0]
        )
        return [(b"authorization", b"Bearer " + token.encode())]
