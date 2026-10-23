import pytest


@pytest.mark.parametrize("enabled", [False, True])
def test_only_production_metrics_profile_opts_into_native_final_file(tmp_path, enabled):
    from tests.ci.test_prod_config import config_data, load
    from lightrag_delivery.render import render_slot

    c = load(tmp_path, config_data())
    objects = render_slot(
        c,
        dict(config_hash="c" * 64, digest="sha256:" + "d" * 64),
        "green",
        image="registry.invalid/rag",
        environment={"LIGHTRAG_QUERY_METRICS": str(enabled).lower()},
    )
    container = objects[-1]["spec"]["template"]["spec"]["containers"][0]
    env = {r["name"]: r.get("value") for r in container["env"]}
    if enabled:
        assert env["LIGHTRAG_USAGE_FINAL_PATH"] == "/dev/termination-log"
        assert container["terminationMessagePath"] == "/dev/termination-log"
        assert container["terminationMessagePolicy"] == "File"
    else:
        assert "LIGHTRAG_USAGE_FINAL_PATH" not in env
