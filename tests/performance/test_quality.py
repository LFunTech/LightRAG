import httpx
import pytest


def golden():
    return {
        "version": 1,
        "cases": [
            {
                "id": "fact-v2",
                "query": "What is the current code?",
                "mode": "mix",
                "stream": False,
                "history": [],
                "actor": "reader",
                "expect": {
                    "status": 200,
                    "required_facts": [["NEW42", "new forty two"]],
                    "forbidden_facts": ["OLD17"],
                    "expected_references": ["perf/run/new.txt"],
                    "forbidden_references": ["perf/run/deleted.txt"],
                    "expected_chunks": ["chunk-new"],
                    "min_chunk_recall": 1.0,
                    "min_reference_precision": 1.0,
                },
            }
        ],
    }


def observation():
    return {
        "id": "fact-v2",
        "status": 200,
        "llm_generated": True,
        "answer": "The code is NEW42 [1].",
        "references": [{"reference_id": "1", "file_path": "perf/run/new.txt"}],
        "retrieval": {
            "status": "success",
            "data": {"chunks": [{"chunk_id": "chunk-new"}]},
        },
    }


def test_quality_checks_facts_citations_recall_and_never_outputs_content():
    from scripts.performance.quality import evaluate

    result = evaluate(golden(), [observation()])
    assert (
        result["rule_gate_passed"]
        and not result["production_accepted"]
        and not result["semantic_judgment_performed"]
    )
    assert "NEW42" not in str(result) and "perf/run/new.txt" not in str(result)
    broken = observation()
    broken["answer"] = "OLD17 [99]"
    broken["references"] = [{"reference_id": "1", "file_path": "perf/run/deleted.txt"}]
    result = evaluate(golden(), [broken])
    assert not result["rule_gate_passed"]
    assert {
        "facts_missing",
        "forbidden_fact",
        "forbidden_reference",
        "invalid_citation",
    } <= set(result["cases"][0]["failures"])


def test_quality_refuses_missing_duplicate_or_unscored_cases():
    from scripts.performance.quality import evaluate, validate_cases

    for rows in ([], [observation(), observation()]):
        with pytest.raises(ValueError):
            evaluate(golden(), rows)
    data = golden()
    data["cases"][0]["expect"]["required_facts"] = []
    with pytest.raises(ValueError):
        validate_cases(data)


def test_expected_denial_does_not_hide_leaked_content():
    from scripts.performance.quality import evaluate

    data = golden()
    case = data["cases"][0]
    case["expect"].update(
        status=403,
        required_facts=[],
        expected_references=[],
        expected_chunks=[],
        min_chunk_recall=0,
        min_reference_precision=0,
    )
    denied = observation()
    denied.update(
        status=403,
        answer="OLD17",
        references=[],
        retrieval={"_http_status": 403, "detail": "Forbidden"},
        llm_generated=False,
    )
    assert not evaluate(data, [denied])["rule_gate_passed"]
    denied["answer"] = "Forbidden"
    assert evaluate(data, [denied])["rule_gate_passed"]


@pytest.mark.parametrize("streaming", [False, True])
async def test_actual_http_collection_preserves_stream_and_retrieval_contract(
    streaming,
):
    from scripts.performance.quality import collect_case, evaluate
    from scripts.performance.http import BoundedClient

    data = golden()
    data["cases"][0]["stream"] = streaming

    async def service(request):
        if request.url.path == "/query/data":
            return httpx.Response(200, json=observation()["retrieval"])
        if streaming:
            return httpx.Response(
                200,
                headers={"content-type": "application/x-ndjson"},
                content=b'{"references":[{"reference_id":"1","file_path":"perf/run/new.txt"}]}\n{"response":"The code is "}\n{"response":"NEW42 [1]."}\n',
            )
        return httpx.Response(
            200,
            json=dict(
                response="NEW42 [1].",
                references=observation()["references"],
                llm_generated=True,
            ),
        )

    async with BoundedClient(
        "https://fixture.invalid",
        {"reader": "private-key"},
        timeout=1,
        max_bytes=10000,
        max_calls=10,
        transport=httpx.MockTransport(service),
    ) as client:
        row = await collect_case(client, data["cases"][0])
    assert evaluate(data, [row])["rule_gate_passed"]


async def test_revoked_actor_must_not_leak_retrieval_even_when_answer_route_denies():
    from scripts.performance.quality import collect_case, evaluate
    from scripts.performance.http import BoundedClient

    data = golden()
    c = data["cases"][0]
    c["expect"].update(
        status=403,
        required_facts=[],
        expected_references=[],
        expected_chunks=[],
        min_chunk_recall=0,
        min_reference_precision=0,
    )
    paths = []

    async def service(request):
        paths.append(request.url.path)
        if request.url.path == "/query":
            return httpx.Response(403, json={"detail": "Forbidden"})
        return httpx.Response(
            200, json={"status": "success", "data": {"chunks": [{"content": "OLD17"}]}}
        )

    async with BoundedClient(
        "https://fixture.invalid",
        {"reader": "private"},
        timeout=1,
        max_bytes=10000,
        max_calls=10,
        transport=httpx.MockTransport(service),
    ) as client:
        row = await collect_case(client, c)
    assert "/query/data" in paths
    assert not evaluate(data, [row])["rule_gate_passed"]


@pytest.mark.parametrize("location", ["reference", "chunk"])
async def test_success_cannot_hide_forbidden_retrieved_content(location):
    from scripts.performance.quality import evaluate

    row = observation()
    if location == "reference":
        row["references"][0]["content"] = "OLD17"
    else:
        row["retrieval"]["data"]["chunks"][0].update(
            content="OLD17", file_path="perf/run/deleted.txt"
        )
    assert not evaluate(golden(), [row])["rule_gate_passed"]
