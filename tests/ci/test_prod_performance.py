import pytest
from tests.ci.test_prod_config import config_data, load


def policy(data):
    data["runtime"].update(
        LIGHTRAG_QUERY_METRICS="true",
        ENABLE_LLM_CACHE="true",
        ENABLE_QUERY_ANSWER_CACHE="false",
        ENABLE_LLM_CACHE_FOR_EXTRACT="true",
    )
    data["capacity"].update(model_concurrency_per_pod=5, model_concurrency_budget=50)
    return data


def test_prod_has_explicit_safe_cache_policy_and_model_budget(tmp_path):
    value = load(tmp_path, policy(config_data()))
    assert value.runtime["ENABLE_QUERY_ANSWER_CACHE"] == "false"
    assert value.runtime["ENABLE_LLM_CACHE_FOR_EXTRACT"] == "true"


def test_independent_role_queues_must_all_fit_the_model_budget(tmp_path):
    from lightrag_delivery.config import ProdConfigError

    data = policy(config_data())
    data["capacity"]["model_concurrency_per_pod"] = 2
    with pytest.raises(ProdConfigError, match="budget"):
        load(tmp_path, data)


@pytest.mark.parametrize(
    "field,value",
    [
        ("ENABLE_QUERY_ANSWER_CACHE", "true"),
        ("ENABLE_LLM_CACHE_FOR_EXTRACT", "false"),
        ("LIGHTRAG_QUERY_METRICS", "maybe"),
    ],
)
def test_unverified_or_ambiguous_prod_cache_policy_is_refused(tmp_path, field, value):
    from lightrag_delivery.config import ProdConfigError

    data = policy(config_data())
    data["runtime"][field] = value
    with pytest.raises(ProdConfigError):
        load(tmp_path, data)
