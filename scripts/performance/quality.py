"""Deterministic golden-fact, citation and retrieval checks; not semantic adjudication."""

import argparse
import json
import re
import unicodedata

from scripts.performance.http import read_json, save_json


def text(value):
    return unicodedata.normalize("NFKC", value).casefold()


def strings(values, *, empty=True):
    if (
        not isinstance(values, list)
        or len(values) > 200
        or (not empty and not values)
        or any(not isinstance(v, str) or not v.strip() or len(v) > 4096 for v in values)
    ):
        raise ValueError("Bounded nonempty strings required")


def validate_cases(data):
    if (
        not isinstance(data, dict)
        or set(data) != {"version", "cases"}
        or type(data["version"]) is not int
        or data["version"] != 1
        or not isinstance(data["cases"], list)
        or not 1 <= len(data["cases"]) <= 200
    ):
        raise ValueError("Versioned golden cases required")
    ids = set()
    for case in data["cases"]:
        if not isinstance(case, dict) or set(case) != {
            "id",
            "query",
            "mode",
            "stream",
            "history",
            "actor",
            "expect",
        }:
            raise ValueError("Explicit quality case fields required")
        for name in ("id", "actor"):
            if not isinstance(case[name], str) or not re.fullmatch(
                r"[A-Za-z0-9_-]{1,64}", case[name]
            ):
                raise ValueError("Invalid case or actor identity")
        if case["id"] in ids:
            raise ValueError("Duplicate case identity")
        ids.add(case["id"])
        if (
            not isinstance(case["query"], str)
            or not case["query"].strip()
            or len(case["query"]) > 20000
            or case["mode"] not in {"mix", "local", "global", "hybrid", "naive"}
            or type(case["stream"]) is not bool
        ):
            raise ValueError("Invalid query configuration")
        if not isinstance(case["history"], list) or len(case["history"]) > 100:
            raise ValueError("Invalid history")
        for turn in case["history"]:
            if (
                not isinstance(turn, dict)
                or set(turn) != {"role", "content"}
                or turn["role"] not in {"user", "assistant"}
                or not isinstance(turn["content"], str)
                or len(turn["content"]) > 20000
            ):
                raise ValueError("Invalid history")
        e = case["expect"]
        fields = {
            "status",
            "required_facts",
            "forbidden_facts",
            "expected_references",
            "forbidden_references",
            "expected_chunks",
            "min_chunk_recall",
            "min_reference_precision",
        }
        if (
            not isinstance(e, dict)
            or set(e) != fields
            or type(e["status"]) is not int
            or e["status"] not in {200, 401, 403}
        ):
            raise ValueError("Explicit success or denial rubric required")
        if not isinstance(e["required_facts"], list) or len(e["required_facts"]) > 100:
            raise ValueError("Fact rubric required")
        for variants in e["required_facts"]:
            strings(variants, empty=False)
        for name in (
            "forbidden_facts",
            "expected_references",
            "forbidden_references",
            "expected_chunks",
        ):
            strings(e[name])
            if len(set(e[name])) != len(e[name]):
                raise ValueError("Duplicate rubric item")
        for name in ("min_chunk_recall", "min_reference_precision"):
            v = e[name]
            if (
                isinstance(v, bool)
                or not isinstance(v, (float, int))
                or not 0 <= v <= 1
            ):
                raise ValueError("Invalid quality threshold")
        if e["status"] == 200 and (
            not e["required_facts"]
            or not e["expected_references"]
            or not e["expected_chunks"]
        ):
            raise ValueError(
                "Successful cases require facts, references and chunk ground truth"
            )
    return ids


def evaluate(data, observations):
    expected = validate_cases(data)
    if (
        not isinstance(observations, list)
        or len(observations) != len(expected)
        or any(not isinstance(r, dict) for r in observations)
    ):
        raise ValueError("Exactly one observation per case required")
    rows = {r.get("id"): r for r in observations}
    if set(rows) != expected or len(rows) != len(observations):
        raise ValueError("Missing or duplicate observation")
    reports = []
    for case in data["cases"]:
        row, e = rows[case["id"]], case["expect"]
        if not isinstance(row.get("answer"), str) or not isinstance(
            row.get("references"), list
        ):
            raise ValueError("Invalid observation shape")
        answer = text(row["answer"])
        failures = []
        if row.get("status") != e["status"]:
            failures.append("status")
        if any(
            not any(text(v) in answer for v in variants)
            for variants in e["required_facts"]
        ):
            failures.append("facts_missing")
        if any(text(v) in answer for v in e["forbidden_facts"]):
            failures.append("forbidden_fact")
        refs = row["references"]
        if any(
            not isinstance(r, dict)
            or not isinstance(r.get("file_path"), str)
            or not isinstance(r.get("reference_id"), str)
            for r in refs
        ):
            raise ValueError("Invalid references")
        paths = {r["file_path"] for r in refs}
        ref_ids = {r["reference_id"] for r in refs}
        if len(ref_ids) != len(refs):
            failures.append("duplicate_reference")
        if paths & set(e["forbidden_references"]):
            failures.append("forbidden_reference")
        exposed = text(
            json.dumps(
                {"references": refs, "retrieval": row.get("retrieval")},
                ensure_ascii=False,
            )
        )
        if any(text(v) in exposed for v in e["forbidden_facts"]):
            failures.append("forbidden_context_fact")
        if any(text(v) in exposed for v in e["forbidden_references"]):
            failures.append("forbidden_context_reference")
        precision = (
            len(paths & set(e["expected_references"])) / len(paths) if paths else 0.0
        )
        citations = set(re.findall(r"\[([0-9]+)\]", row["answer"]))
        if citations - ref_ids:
            failures.append("invalid_citation")
        recall = 0.0
        if e["status"] == 200:
            if row.get("llm_generated") is not True or not row["answer"].strip():
                failures.append("no_generation")
            retrieval = row.get("retrieval")
            if (
                not isinstance(retrieval, dict)
                or retrieval.get("status") != "success"
                or not isinstance(retrieval.get("data", {}).get("chunks"), list)
            ):
                failures.append("retrieval_unconfirmed")
            else:
                chunks = {
                    r.get("chunk_id", r.get("id"))
                    for r in retrieval["data"]["chunks"]
                    if isinstance(r, dict)
                }
                recall = len(chunks & set(e["expected_chunks"])) / len(
                    e["expected_chunks"]
                )
            if recall < e["min_chunk_recall"]:
                failures.append("chunk_recall")
            if (
                not set(e["expected_references"]) <= paths
                or precision < e["min_reference_precision"]
            ):
                failures.append("reference_quality")
            if not citations or not citations <= ref_ids:
                failures.append("citation_missing_or_invalid")
        else:
            retrieval = row.get("retrieval")
            if (
                refs
                or row.get("llm_generated") is True
                or not isinstance(retrieval, dict)
                or retrieval.get("_http_status") != e["status"]
            ):
                failures.append("denial_leak_or_unverified_retrieval")
            exposed = text(json.dumps(retrieval, ensure_ascii=False))
            if any(
                text(v) in exposed
                for v in e["forbidden_facts"] + e["forbidden_references"]
            ):
                failures.append("denial_retrieval_leak")
        reports.append(
            dict(
                id=case["id"],
                passed=not failures,
                failures=failures,
                chunk_recall=recall,
                reference_precision=precision,
            )
        )
    return dict(
        rule_gate_passed=all(r["passed"] for r in reports),
        cases=reports,
        production_accepted=False,
        semantic_judgment_performed=False,
    )


async def collect_case(client, case):
    payload = dict(
        query=case["query"],
        mode=case["mode"],
        stream=case["stream"],
        conversation_history=case["history"],
        include_references=True,
        include_chunk_content=True,
    )
    status, headers, body = await client.request(
        "POST",
        "/query/stream" if case["stream"] else "/query",
        actor=case["actor"],
        payload=payload,
    )
    row = dict(
        id=case["id"],
        status=status,
        answer="",
        references=[],
        retrieval=None,
        llm_generated=False,
    )
    retrieval_payload = {
        k: payload[k] for k in ("query", "mode", "conversation_history")
    }
    code, _, retrieval = await client.json(
        "POST", "/query/data", actor=case["actor"], payload=retrieval_payload
    )
    row["retrieval"] = (
        dict(retrieval, _http_status=code)
        if isinstance(retrieval, dict)
        else {"_http_status": code, "unparsed": retrieval}
    )
    if status != 200:
        row["answer"] = body.decode("utf-8")
        return row
    ct = headers.get("content-type", "").split(";", 1)[0].strip().lower()
    if case["stream"]:
        if ct != "application/x-ndjson":
            raise ValueError("NDJSON required")
        parts = []
        generated = True
        seen_refs = False
        for line in body.splitlines():
            if not line.strip():
                continue
            event = json.loads(line)
            if not isinstance(event, dict) or event.get("error") is not None:
                raise ValueError("Stream error")
            if "llm_generated" in event and event["llm_generated"] is not True:
                generated = False
            if "references" in event:
                if seen_refs:
                    raise ValueError("Repeated references")
                row["references"] = event["references"]
                seen_refs = True
            if "response" in event:
                if not isinstance(event["response"], str):
                    raise ValueError("Invalid response content")
                parts.append(event["response"])
        row.update(answer="".join(parts), llm_generated=generated and bool(parts))
    else:
        if ct != "application/json":
            raise ValueError("JSON required")
        value = json.loads(body)
        row.update(
            answer=value.get("response"),
            references=value.get("references") or [],
            llm_generated=value.get("llm_generated") is True,
        )
    return row


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cases", required=True)
    parser.add_argument("--observations")
    parser.add_argument("--output", required=True)
    args = parser.parse_args(argv)
    try:
        cases = read_json(args.cases)
        validate_cases(cases)
        report = (
            evaluate(cases, read_json(args.observations))
            if args.observations
            else {"valid": True, "network_run": False, "production_accepted": False}
        )
        save_json(args.output, report)
        return 0 if report.get("rule_gate_passed", report.get("valid")) else 2
    except Exception as error:
        print(type(error).__name__ + ": quality check refused; inspect private inputs")
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
