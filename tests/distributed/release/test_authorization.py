import time

import pytest


class Auth:
    accounts = {"alice": "approved-password-hash"}
    secret = "private-issuer-key"
    algorithm = "HS256"

    def validate_token(self, token):
        from datetime import datetime, timezone

        if token != "valid":
            raise ValueError("invalid")
        return {
            "username": "alice",
            "role": "user",
            "exp": datetime.fromtimestamp(time.time() + 60, timezone.utc),
        }


async def test_key_revocation_is_checked_without_persisting_key():
    from lightrag.distributed.release.authorization import AuthorizationPolicy

    policy = AuthorizationPolicy(
        scope="prod/work", api_key="private-api-key", auth_handler=Auth()
    )
    principal = await policy.authenticate({"x-api-key": "private-api-key"})
    assert "private-api-key" not in str(principal)
    assert await policy.authorize(principal, "http.write", "prod/work")
    policy.api_key = "rotated"
    assert not await policy.authorize(principal, "http.write", "prod/work")


async def test_token_expiry_and_account_revocation_are_checked_again():
    from lightrag.distributed.release.authorization import AuthorizationPolicy

    auth = Auth()
    auth.accounts = dict(auth.accounts)
    policy = AuthorizationPolicy(
        scope="prod/work", api_key="private", auth_handler=auth
    )
    principal = await policy.authenticate({"authorization": "Bearer valid"})
    assert "valid" not in str(principal)
    assert await policy.authorize(principal, "http.write", "prod/work")
    principal["expires_at"] = time.time() - 1
    assert not await policy.authorize(principal, "http.write", "prod/work")
    principal["expires_at"] = time.time() + 60
    del auth.accounts["alice"]
    assert not await policy.authorize(principal, "http.write", "prod/work")


async def test_invalid_bearer_is_not_bypassed_by_api_key_or_guest():
    from lightrag.distributed.release.authorization import AuthorizationPolicy
    from lightrag.distributed.release.models import ReleaseError

    policy = AuthorizationPolicy(
        scope="prod/work", api_key="private", auth_handler=Auth()
    )
    with pytest.raises(ReleaseError):
        await policy.authenticate(
            {"authorization": "Bearer invalid", "x-api-key": "private"}
        )
    with pytest.raises(ReleaseError):
        await policy.authenticate({})


async def test_original_non_guest_account_role_is_preserved_at_execution():
    from lightrag.distributed.release.authorization import AuthorizationPolicy

    class RoleAuth(Auth):
        def validate_token(self, token):
            return dict(super().validate_token(token), role="admin")

        def create_token(self, subject, role):
            assert role == "admin"
            return "signed-in-memory"

    policy = AuthorizationPolicy(scope="prod/work", api_key="", auth_handler=RoleAuth())
    principal = await policy.authenticate({"authorization": "Bearer valid"})
    assert await policy.authorize(principal, "http.write", "prod/work")
    assert await policy.execution_headers(principal) == [
        (b"authorization", b"Bearer signed-in-memory")
    ]
