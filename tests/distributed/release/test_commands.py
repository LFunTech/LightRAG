import pytest


def test_registry_refuses_unknown_operation_and_unserializable_payload():
    from lightrag.distributed.release.commands import CommandRegistry
    from lightrag.distributed.release.models import ReleaseError

    registry = CommandRegistry()
    with pytest.raises(ReleaseError):
        registry.encode(
            "arbitrary", {"x": 1}, principal={"id": "u"}, scope="prod/work", key="k"
        )
    registry.register("sdk.create", version=1, handler=lambda payload: payload)
    with pytest.raises(ReleaseError):
        registry.encode(
            "sdk.create",
            {"callback": lambda: None},
            principal={"id": "u"},
            scope="prod/work",
            key="k",
        )
    with pytest.raises(ReleaseError):
        registry.encode(
            "sdk.create",
            {"token": "secret"},
            principal={"id": "u", "raw_token": "secret"},
            scope="prod/work",
            key="k",
        )


def test_command_identity_is_canonical_and_principal_scoped():
    from lightrag.distributed.release.commands import CommandRegistry

    registry = CommandRegistry()
    registry.register("sdk.create", version=1, handler=lambda payload: payload)
    one = registry.encode(
        "sdk.create",
        {"b": 2, "a": 1},
        principal={"id": "u"},
        scope="prod/work",
        key="k",
    )
    two = registry.encode(
        "sdk.create",
        {"a": 1, "b": 2},
        principal={"id": "u"},
        scope="prod/work",
        key="k",
    )
    assert one.payload_hash == two.payload_hash
    assert one.as_dict()["version"] == 1
    assert one.as_dict()["operation"] == "sdk.create"


async def test_executor_rechecks_authorization_before_handler():
    from lightrag.distributed.release.commands import CommandRegistry
    from lightrag.distributed.release.executor import execute_command

    registry = CommandRegistry()
    calls = []

    async def handler(payload):
        calls.append(payload)
        return {"ok": True}

    registry.register("sdk.create", version=1, handler=handler)
    envelope = registry.encode(
        "sdk.create", {"a": 1}, principal={"id": "revoked"}, scope="prod/work", key="k"
    )

    async def denied(principal, operation, scope):
        return False

    result = await execute_command(registry, envelope, authorize=denied)
    assert result == {"status": "FAILED", "result": {"error": "AuthorizationDenied"}}
    assert calls == []


async def test_executor_does_not_convert_unknown_ack_to_retryable_failure():
    from lightrag.distributed.release.commands import CommandRegistry
    from lightrag.distributed.release.executor import execute_command

    registry = CommandRegistry()

    async def handler(payload):
        raise OSError("backend credentials must not be persisted")

    async def allowed(principal, operation, scope):
        return True

    registry.register("sdk.create", version=1, handler=handler)
    result = await execute_command(
        registry,
        registry.encode(
            "sdk.create", {}, principal={"id": "u"}, scope="prod/work", key="k"
        ),
        authorize=allowed,
    )
    assert result == {"status": "UNCERTAIN", "result": {"error": "OSError"}}
