import pytest


def test_selector_cas_expands_before_narrowing_and_never_pauses_all():
    from lightrag_delivery.kubernetes import selector_patch
    from lightrag_delivery.render import APP_LABEL, SLOT_LABEL

    old = {APP_LABEL: "prod", SLOT_LABEL: "blue"}
    both = {APP_LABEL: "prod"}
    patch = selector_patch("12", old, both)
    assert patch[0] == {
        "op": "test",
        "path": "/metadata/resourceVersion",
        "value": "12",
    }
    assert patch[1]["value"] == old and patch[2]["value"] == both
    with pytest.raises(ValueError):
        selector_patch("12", old, {"paused": "true"})


def test_public_route_proof_binds_actual_candidate_instance_and_profile():
    from lightrag_delivery.qcloud import verify_entry_response

    expected = {"green-1", "green-2"}
    response = {
        "instance_id": "green-1",
        "slot": "green",
        "profile_hash": "a" * 64,
        "readiness": {"ready": True},
    }
    assert verify_entry_response(
        response, slot="green", instance_ids=expected, profile_hash="a" * 64
    )
    with pytest.raises(ValueError):
        verify_entry_response(
            dict(response, instance_id="old-blue"),
            slot="green",
            instance_ids=expected,
            profile_hash="a" * 64,
        )
