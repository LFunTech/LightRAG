"""Explicit isolated mixed-write/4+4 rehearsal; default CLI is offline validation."""

import argparse
import asyncio
from collections import Counter
import hashlib
import json
import math
import os
from pathlib import Path
import re
import time
from uuid import UUID

from scripts.performance.control import PHASES, OrchestratorControl
from scripts.performance.http import (
    BoundedClient,
    read_json,
    save_json,
    validate_connection,
    NewWorkStopped,
)
from scripts.performance.dynamic_usage import DynamicUsage, validate_limits
from scripts.performance.quality import validate_cases, collect_case, evaluate
from scripts.performance.query_load import validate_plan, run_load
from scripts.performance.stream import observe_long_stream


def _identity(value):
    return isinstance(value, str) and re.fullmatch(r"[A-Za-z0-9_-]{1,64}", value)


def validate(plan, scenario):
    fields = {
        "version",
        "environment",
        "run_id",
        "approval_id",
        "release",
        "read_plan",
        "limits",
        "actions",
        "checkpoints",
        "usage",
    }
    if (
        not isinstance(plan, dict)
        or set(plan) != fields
        or type(plan["version"]) is not int
        or plan["version"] != 2
        or plan["environment"] != "isolated"
        or not _identity(plan["run_id"])
        or not _identity(plan["approval_id"])
    ):
        raise ValueError("Explicit isolated rehearsal plan required")
    validate_limits(plan["usage"])
    if not isinstance(scenario, dict) or set(scenario) != {
        "quality",
        "load_queries",
        "long_stream_case",
    }:
        raise ValueError("Explicit scenario data required")
    case_ids = validate_cases(scenario["quality"])
    if scenario["long_stream_case"] not in case_ids or not any(
        c["id"] == scenario["long_stream_case"] and c["expect"]["status"] == 200
        for c in scenario["quality"]["cases"]
    ):
        raise ValueError("Explicit long-stream case required")
    if not any(c["expect"]["status"] == 200 for c in scenario["quality"]["cases"]):
        raise ValueError("Successful query ground truth required")
    queries = scenario["load_queries"]
    if not isinstance(queries, list) or not queries or len(queries) > 10000:
        raise ValueError("Bounded load dataset required")
    # Validate the same query dataset contract before owner creation or writes.
    for q in queries:
        if (
            not isinstance(q, dict)
            or set(q) - {"query", "mode", "history"}
            or not isinstance(q.get("query"), str)
            or not q["query"].strip()
            or len(q["query"]) > 20000
        ):
            raise ValueError("Invalid load query")
        if q.get("mode", "mix") not in {"local", "global", "hybrid", "mix", "naive"}:
            raise ValueError("Invalid load mode")
        h = q.get("history", [])
        if (
            not isinstance(h, list)
            or len(h) > 100
            or any(
                not isinstance(t, dict)
                or set(t) != {"role", "content"}
                or t["role"] not in {"user", "assistant"}
                or not isinstance(t["content"], str)
                or len(t["content"]) > 20000
                for t in h
            )
        ):
            raise ValueError("Invalid load history")
        if plan["read_plan"].get("history_fraction") and not h:
            raise ValueError("Missing load history")
    load = validate_plan(plan["read_plan"])
    if not load["ready"]:
        raise ValueError("Approved complete read workload/SLO required")
    if (
        plan["read_plan"]["request_timeout_seconds"] > 600
        or plan["read_plan"]["duration_seconds"] > 86400
    ):
        raise ValueError("Read request/run lifetime exceeds rehearsal safety budget")
    r = plan["release"]
    required = {
        "scope",
        "release_id",
        "namespace",
        "namespace_uid",
        "kube_system_uid",
        "profile_hash",
        "blue_config_hash",
        "green_config_hash",
        "blue_digest",
        "green_digest",
        "epoch",
    }
    if (
        not isinstance(r, dict)
        or set(r) != required
        or not isinstance(r["scope"], str)
        or not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", r["scope"])
    ):
        raise ValueError("Explicit isolated release scope required")
    for k in ("profile_hash", "blue_config_hash", "green_config_hash"):
        if not isinstance(r[k], str) or not re.fullmatch("[a-f0-9]{64}", r[k]):
            raise ValueError("Invalid configuration hash")
    for k in ("blue_digest", "green_digest"):
        if not isinstance(r[k], str) or not re.fullmatch("sha256:[a-f0-9]{64}", r[k]):
            raise ValueError("Invalid digest")
    if (
        type(r["epoch"]) is not int
        or r["epoch"] < 1
        or any(
            not isinstance(r[k], str) or not r[k] or len(r[k]) > 128
            for k in ("release_id", "namespace", "namespace_uid", "kube_system_uid")
        )
    ):
        raise ValueError("Invalid namespace/release identity")
    isolation = plan["read_plan"]["isolation"]
    if isolation["profile_hash"] != r["profile_hash"] or not {
        r["blue_config_hash"],
        r["green_config_hash"],
    } <= set(isolation["config_hashes"]):
        raise ValueError("Read workload and release identities differ")
    limits = plan["limits"]
    if not isinstance(limits, dict) or set(limits) != {
        "timeout_seconds",
        "poll_seconds",
        "max_calls",
        "max_bytes",
        "max_write_inflight",
    }:
        raise ValueError("Explicit rehearsal budgets required")
    for k in ("timeout_seconds", "poll_seconds"):
        v = limits[k]
        if (
            isinstance(v, bool)
            or not isinstance(v, (int, float))
            or not math.isfinite(v)
            or not 0 < v <= 600
        ):
            raise ValueError("Invalid timing budget")
    if limits["poll_seconds"] >= limits["timeout_seconds"]:
        raise ValueError("Poll budget exceeds timeout")
    for k, cap in [
        ("max_calls", 100000),
        ("max_bytes", 16 * 1024**2),
        ("max_write_inflight", 16),
    ]:
        if type(limits[k]) is not int or not 1 <= limits[k] <= cap:
            raise ValueError("Invalid capacity budget")
    actions = plan["actions"]
    known = {}
    if not isinstance(actions, list) or not 1 <= len(actions) <= 100:
        raise ValueError("Bounded typed actions required")
    for action in actions:
        if (
            not isinstance(action, dict)
            or set(action) != {"id", "kind", "phase", "actor", "payload"}
            or not _identity(action["id"])
            or not _identity(action["actor"])
            or action["id"] in known
            or action["phase"] not in PHASES[:-1]
        ):
            raise ValueError("Invalid action identity")
        kind, p = action["kind"], action["payload"]
        expected = {
            "insert_text": {"text", "file_source"},
            "create_entity": {"name", "description"},
            "edit_entity": {"create_action", "description"},
            "delete_document": {"insert_action"},
        }
        if (
            kind not in expected
            or not isinstance(p, dict)
            or set(p) != expected[kind]
            or any(
                not isinstance(v, str) or not v.strip() or len(v) > 20000
                for v in p.values()
            )
        ):
            raise ValueError("Unknown or unsafe write payload")
        if kind == "insert_text" and (
            not re.fullmatch(
                "perf-"
                + re.escape(plan["run_id"])
                + r"-[A-Za-z0-9_][A-Za-z0-9_.-]{0,120}\.txt",
                p["file_source"],
            )
        ):
            raise ValueError("Write source outside this run")
        if kind == "create_entity" and not p["name"].startswith(
            "PERF_" + plan["run_id"].upper() + "_"
        ):
            raise ValueError("Entity outside this run")
        if kind in {"edit_entity", "delete_document"}:
            ref = p["create_action" if kind == "edit_entity" else "insert_action"]
            parent = known.get(ref)
            if (
                parent is None
                or parent["kind"]
                != ("create_entity" if kind == "edit_entity" else "insert_text")
                or PHASES.index(parent["phase"]) > PHASES.index(action["phase"])
                or parent["phase"] in {"DRAINING", "READ_SWITCHED"}
                and action["phase"] in {"DRAINING", "READ_SWITCHED"}
            ):
                raise ValueError("Owned executable earlier action dependency required")
        known[action["id"]] = action
    if not {"insert_text", "create_entity", "edit_entity", "delete_document"} <= {
        a["kind"] for a in actions
    }:
        raise ValueError("Mixed insertion/graph edit/deletion coverage required")
    if not any(a["phase"] in {"DRAINING", "READ_SWITCHED"} for a in actions) or not any(
        a["phase"] in {"WRITER_SWITCHED", "RETIRING"} for a in actions
    ):
        raise ValueError("Closed-gate and post-transfer write coverage required")
    checkpoints = plan["checkpoints"]
    used = set()
    ids = set()
    if not isinstance(checkpoints, list) or not 1 <= len(checkpoints) <= 50:
        raise ValueError("Quality checkpoints required")
    for c in checkpoints:
        if (
            not isinstance(c, dict)
            or set(c) != {"id", "phase", "after", "cases"}
            or not _identity(c["id"])
            or c["id"] in ids
            or c["phase"] not in PHASES
            or not isinstance(c["after"], list)
            or not isinstance(c["cases"], list)
            or not c["cases"]
            or len(set(c["cases"])) != len(c["cases"])
            or len(set(c["after"])) != len(c["after"])
            or not set(c["cases"]) <= case_ids
        ):
            raise ValueError("Invalid quality checkpoint")
        ids.add(c["id"])
        used.update(c["cases"])
        for key in c["after"]:
            if (
                key not in known
                or PHASES.index(known[key]["phase"]) > PHASES.index(c["phase"])
                or (c["phase"] in PHASES[1:3] and known[key]["phase"] in PHASES[1:3])
            ):
                raise ValueError("Checkpoint would wait on the closed write gate")
    if used != case_ids or not any(c["phase"] == "COMPLETED" for c in checkpoints):
        raise ValueError("All cases and final checkpoint required")
    return dict(
        valid=True,
        network_run=False,
        production_accepted=False,
        actions=len(actions),
        quality_cases=len(case_ids),
    )


class Journal:
    def __init__(self, path):
        self.output = os.fdopen(
            os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "w"
        )

    def record(self, **record):
        self.output.write(json.dumps(record, sort_keys=True, allow_nan=False) + "\n")
        self.output.flush()
        os.fsync(self.output.fileno())

    def close(self):
        self.output.close()


async def _track(client, track, actor, *, allow_after_stop=False):
    if not isinstance(track, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,255}", track):
        raise ValueError("Invalid track identity")
    status, _, result = await client.json(
        "GET",
        "/documents/track_status/" + track,
        actor=actor,
        allow_after_stop=allow_after_stop,
    )
    if (
        status != 200
        or not isinstance(result, dict)
        or not isinstance(result.get("documents"), list)
        or result.get("total_count") != len(result["documents"])
    ):
        raise ValueError("Document state is unknown")
    return result["documents"]


async def _action(action, plan, client, tasks, journal, stop_event):
    aid, kind, p, actor = (
        action["id"],
        action["kind"],
        action["payload"],
        action["actor"],
    )
    limits = plan["limits"]
    deadline = time.monotonic() + limits["timeout_seconds"]
    sent = False
    accepted = False
    result = dict(id=aid, state="FAILED", kind=kind)
    try:
        if stop_event.is_set():
            result["state"] = "NOT_SENT"
            return result
        parent = None
        if kind in {"edit_entity", "delete_document"}:
            ref = p["create_action" if kind == "edit_entity" else "insert_action"]
            parent = await tasks[ref]
            if stop_event.is_set():
                result["state"] = "NOT_SENT"
                return result
            if parent["state"] != "CONFIRMED":
                raise ValueError("Dependency is not confirmed")
        if kind == "insert_text":
            method, path, payload = "POST", "/documents/text", p
        elif kind == "create_entity":
            method, path, payload = (
                "POST",
                "/graph/entity/create",
                dict(
                    entity_name=p["name"],
                    entity_data={
                        "description": p["description"],
                        "entity_type": "PERFORMANCE_FIXTURE",
                    },
                ),
            )
        elif kind == "edit_entity":
            method, path, payload = (
                "POST",
                "/graph/entity/edit",
                dict(
                    entity_name=parent["name"],
                    updated_data={"description": p["description"]},
                    allow_rename=False,
                    allow_merge=False,
                ),
            )
        else:
            docs = await _track(client, parent["track"], actor)
            if (
                len(docs) != 1
                or docs[0].get("id") != parent["doc"]
                or docs[0].get("file_path") != parent["source"]
            ):
                raise ValueError("Owned document identity differs before deletion")
            method, path, payload = (
                "DELETE",
                "/documents/delete_document",
                dict(
                    doc_ids=[parent["doc"]], delete_file=False, delete_llm_cache=False
                ),
            )
        if stop_event.is_set():
            result["state"] = "NOT_SENT"
            return result
        key = "perf-" + plan["run_id"] + "-" + aid
        journal.record(
            action=aid,
            state="SENDING",
            kind=kind,
            idempotency_sha256=hashlib.sha256(key.encode()).hexdigest(),
        )
        sent = True
        status, headers, data = await client.json(
            method, path, actor=actor, payload=payload, key=key
        )
        command = headers.get("x-release-command-id")
        if command:
            result["command_id"] = str(UUID(command))
        if status >= 500 or 300 <= status < 400 or status < 200:
            raise ValueError("Write result unknown")
        if status >= 400 or not isinstance(data, dict):
            result["state"] = "FAILED"
            return result
        accepted = True
        journal.record(
            action=aid, state="ACCEPTED", command_id=result.get("command_id")
        )
        if kind in {"create_entity", "edit_entity"}:
            if (
                data.get("status") != "success"
                or data.get("operation_summary", {}).get("operation_status", "success")
                != "success"
            ):
                raise ValueError("Graph result not confirmed")
            name = p["name"] if kind == "create_entity" else parent["name"]
            code, _, graph = await client.json(
                "GET",
                "/graphs",
                actor=actor,
                params=dict(label=name, max_depth=1, max_nodes=20),
                allow_after_stop=True,
            )
            nodes = graph.get("nodes", []) if isinstance(graph, dict) else []
            if code != 200 or not any(
                n.get("id") == name
                and n.get("properties", {}).get("description") == p["description"]
                for n in nodes
                if isinstance(n, dict)
            ):
                raise ValueError("Graph read-after-write differs")
            result["name"] = name
        else:
            if data.get("status") != (
                "success" if kind == "insert_text" else "deletion_started"
            ):
                raise ValueError("Document operation not accepted")
            track = data.get("track_id") if kind == "insert_text" else parent["track"]
            while True:
                docs = await _track(client, track, actor, allow_after_stop=True)
                if kind == "delete_document" and not docs:
                    break
                if kind == "insert_text" and len(docs) == 1:
                    d = docs[0]
                    if d.get("file_path") != p["file_source"]:
                        raise ValueError("Tracked source differs")
                    if d.get("status") == "processed":
                        if not isinstance(d.get("id"), str) or not d["id"]:
                            raise ValueError("Document ID missing")
                        result.update(track=track, doc=d["id"], source=p["file_source"])
                        break
                    if d.get("status") == "failed":
                        raise ValueError("Document processing failed")
                if time.monotonic() >= deadline:
                    raise TimeoutError("Final document status unknown")
                await asyncio.sleep(limits["poll_seconds"])
        result["state"] = "CONFIRMED"
    except NewWorkStopped:
        result["state"] = (
            "ACCEPTED_UNCONFIRMED" if accepted else "UNCERTAIN" if sent else "NOT_SENT"
        )
    except (Exception, asyncio.CancelledError):
        result["state"] = (
            "ACCEPTED_UNCONFIRMED" if accepted else "UNCERTAIN" if sent else "FAILED"
        )
    finally:
        if result["state"] != "CONFIRMED":
            stop_event.set()
        try:
            journal.record(
                action=aid, state=result["state"], command_id=result.get("command_id")
            )
        except BaseException:
            stop_event.set()
            raise
    return result


async def _run_rehearsal(
    plan,
    scenario,
    *,
    base_url,
    actors,
    control,
    output_dir,
    confirmed=False,
    confirm_writes=False,
    transport=None,
    stop_event,
):
    if confirmed is not True or confirm_writes is not True:
        raise ValueError("Explicit isolated target and write confirmation required")
    validate(plan, scenario)
    control.validate(plan["release"])
    control.validate_target(base_url)
    required = {a["actor"] for a in plan["actions"]} | {
        c["actor"] for c in scenario["quality"]["cases"]
    }
    if not required <= set(actors):
        raise ValueError("Missing actor credentials")
    limits = plan["limits"]
    validate_connection(
        base_url,
        actors,
        timeout=limits["timeout_seconds"],
        max_bytes=limits["max_bytes"],
        max_calls=limits["max_calls"],
    )
    output_dir = Path(output_dir)
    os.mkdir(output_dir, 0o700)
    journal = Journal(output_dir / "attempts.jsonl")
    try:
        save_json(
            output_dir / "input-identity.json",
            {
                "run_id": plan["run_id"],
                "input_sha256": hashlib.sha256(
                    json.dumps(
                        dict(plan=plan, scenario=scenario), sort_keys=True
                    ).encode()
                ).hexdigest(),
            },
        )
    except BaseException:
        journal.close()
        raise
    client = BoundedClient(
        base_url,
        actors,
        timeout=limits["timeout_seconds"],
        max_bytes=limits["max_bytes"],
        max_calls=limits["max_calls"],
        transport=transport,
        stop_event=stop_event,
    )
    usage = DynamicUsage(
        control, plan["release"], plan["usage"], output_dir, stop_event
    )
    tasks = {}
    queued_receipts = {}
    phases = []
    quality = []
    error = None
    load_task = None
    load_report = None
    long_task = None
    long_report = None
    first_event, transferred, window_complete = (
        asyncio.Event(),
        asyncio.Event(),
        asyncio.Event(),
    )

    def ensure_running():
        if stop_event.is_set():
            raise ValueError("Rehearsal stopped; no further writes or advancement")

    async def continuous_load():
        try:
            return await run_load(
                plan["read_plan"],
                scenario["load_queries"],
                base_url=base_url,
                api_key=actors[probe["actor"]],
                confirmed=True,
                transport=transport,
                stop_event=stop_event,
                usage_monitor=usage,
            )
        finally:
            if not window_complete.is_set():
                stop_event.set()

    cases = {c["id"]: c for c in scenario["quality"]["cases"]}
    probe = next(c for c in cases.values() if c["expect"]["status"] == 200)
    try:
        await client.__aenter__()
        # Verify the public service identity before taking the durable owner.
        status, _, identity = await client.json(
            "GET", "/release/status", actor=probe["actor"]
        )
        r = plan["release"]
        if (
            status != 200
            or identity.get("profile_hash") != r["profile_hash"]
            or identity.get("config_hash") != r["blue_config_hash"]
        ):
            raise ValueError("Service identity differs")
        ensure_running()
        await usage.start()
        await control.start(r)
        usage.launch()
        ensure_running()
        load_task = asyncio.create_task(continuous_load())
        long_task = asyncio.create_task(
            observe_long_stream(
                client, cases[scenario["long_stream_case"]], first_event, transferred
            )
        )
        await asyncio.wait_for(first_event.wait(), timeout=limits["timeout_seconds"])
        if long_task.done():
            raise ValueError("Long stream ended before handover")
        for phase in PHASES:
            ensure_running()
            await usage.sample()
            deadline = time.monotonic() + limits["timeout_seconds"]
            inspections = 0
            while True:
                inspections += 1
                try:
                    snapshot = await asyncio.wait_for(
                        control.inspect(r),
                        timeout=max(0.001, deadline - time.monotonic()),
                    )
                    break
                except Exception:
                    ensure_running()
                    if inspections >= 100 or time.monotonic() >= deadline:
                        raise
                    await asyncio.sleep(limits["poll_seconds"])
            ensure_running()
            if phase == "READ_SWITCHED":
                if long_task.done() or not any(
                    p["slot"] == "blue" and p.get("lifecycle", {}).get("queries", 0) > 0
                    for p in snapshot["instances"]
                ):
                    raise ValueError("No live old-slot query across handover")
            if snapshot["phase"] != phase:
                raise ValueError("Missing release phase evidence")
            phases.append(
                dict(
                    phase=phase,
                    epoch=snapshot["epoch"],
                    instance_count=len(snapshot["instances"]),
                    route_evidence_sha256=snapshot["route_evidence_sha256"],
                )
            )
            journal.record(phase=phase, state="OBSERVED")
            for a in plan["actions"]:
                if a["phase"] == phase:
                    if (
                        sum(not t.done() for t in tasks.values())
                        >= limits["max_write_inflight"]
                    ):
                        raise ValueError("Write concurrency budget exhausted")
                    tasks[a["id"]] = asyncio.create_task(
                        _action(a, plan, client, tasks, journal, stop_event)
                    )
            # Both forms must finish normally while the write gate changes.
            for streaming in (False, True):
                row = await collect_case(client, dict(probe, stream=streaming))
                if (
                    row["status"] != 200
                    or row["retrieval"].get("_http_status") != 200
                    or row["retrieval"].get("status") != "success"
                    or row["llm_generated"] is not True
                    or not isinstance(row["answer"], str)
                    or not row["answer"].strip()
                ):
                    raise ValueError("Phase query failed")
            if phase in {"DRAINING", "READ_SWITCHED"}:
                for a in (a for a in plan["actions"] if a["phase"] == phase):
                    key = "perf-" + plan["run_id"] + "-" + a["id"]
                    principal = hashlib.sha256(
                        ("api-key:" + actors[a["actor"]]).encode()
                    ).hexdigest()
                    until = time.monotonic() + limits["timeout_seconds"]
                    while True:
                        receipt = await control.receipt(
                            r, key=key, principal_id=principal
                        )
                        if receipt is not None:
                            if receipt["state"] != "QUEUED":
                                raise ValueError("Closed-gate command was not queued")
                            queued_receipts[a["id"]] = (key, principal, receipt["id"])
                            journal.record(
                                action=a["id"], state="QUEUED", command_id=receipt["id"]
                            )
                            break
                        if tasks[a["id"]].done() or time.monotonic() >= until:
                            raise ValueError("Durable queue admission not observed")
                        await asyncio.sleep(limits["poll_seconds"])
            for cp in plan["checkpoints"]:
                if cp["phase"] != phase:
                    continue
                for aid in cp["after"]:
                    if (await tasks[aid])["state"] != "CONFIRMED":
                        raise ValueError("Checkpoint write is not confirmed")
                subset = dict(version=1, cases=[cases[k] for k in cp["cases"]])
                observations = [await collect_case(client, c) for c in subset["cases"]]
                report = evaluate(subset, observations)
                quality.append(dict(checkpoint=cp["id"], **report))
                if not report["rule_gate_passed"]:
                    raise ValueError("Quality gate failed")
            if any(
                t.done() and t.result()["state"] != "CONFIRMED" for t in tasks.values()
            ):
                raise ValueError("Unconfirmed write stops release progress")
            if load_task.done() and load_task.exception() is not None:
                raise ValueError("Background query load failed")
            ensure_running()
            if phase != "COMPLETED":
                await control.advance(r, usage_gate=usage)
                if phase == "READ_SWITCHED":
                    transferred.set()
            else:
                window_complete.set()
        results = await asyncio.gather(*tasks.values())
        load_report = await load_task
        for aid, (key, principal, command_id) in queued_receipts.items():
            receipt = await control.receipt(r, key=key, principal_id=principal)
            if (
                receipt is None
                or receipt["id"] != command_id
                or receipt["state"] != "SUCCEEDED"
            ):
                raise ValueError("Queued command terminal ACK is not confirmed")
            journal.record(
                action=aid, state="TERMINAL_CONFIRMED", command_id=command_id
            )
    except Exception as exc:
        stop_event.set()
        error = type(exc).__name__
    finally:
        # Never cancel or replay an in-flight mutation. The HTTP budget settles
        # each waiter as confirmed/unconfirmed/unknown, not as "not executed".
        settled = await asyncio.gather(*tasks.values(), return_exceptions=True)
        results = []
        for aid, row in zip(tasks, settled):
            if isinstance(row, BaseException):
                error = error or "WriteEvidenceUnavailable"
                results.append(dict(id=aid, state="UNCERTAIN"))
            else:
                results.append(row)
        if load_task is not None:
            try:
                load_report = await load_task
            except Exception:
                error = error or "LoadUnavailable"
        if long_task is not None:
            try:
                long_report = await long_task
            except Exception:
                error = error or "LongStreamUnavailable"
        await usage.finish(
            success=error is None
            and window_complete.is_set()
            and not stop_event.is_set()
        )
        if load_report is not None:
            load_report["usage"] = usage.report()
            load_report["slo_met"] = bool(
                load_report.get("client_slo_met")
                and usage.report()["verified"]
                and not stop_event.is_set()
            )
        try:
            await client.__aexit__(None, None, None)
        finally:
            journal.close()
    if stop_event.is_set():
        error = error or "RehearsalStopped"
    summary = dict(Counter(row["state"] for row in results))
    passed = (
        error is None
        and long_report is not None
        and long_report["completed_after_transfer"]
        and len(phases) == 6
        and all(row["state"] == "CONFIRMED" for row in results)
        and all(q["rule_gate_passed"] for q in quality)
        and load_report is not None
        and load_report["errors"] == 0
        and load_report["client_dropped"] == 0
        and usage.report()["verified"]
    )
    report = dict(
        rehearsal_checks_passed=passed,
        production_accepted=False,
        capacity_accepted=False,
        semantic_judgment_performed=False,
        continuous_load_covered_release=window_complete.is_set(),
        long_stream=long_report,
        phases=phases,
        quality=quality,
        write_summary=summary,
        queued_commands_confirmed=len(queued_receipts) if error is None else 0,
        error_class=error,
        load=load_report,
        usage=usage.report(),
        input_sha256=hashlib.sha256(
            json.dumps(dict(plan=plan, scenario=scenario), sort_keys=True).encode()
        ).hexdigest(),
    )
    save_json(output_dir / "report.json", report)
    return report


async def run_rehearsal(*args, **kwargs):
    """Cancellation stops new work, but never cancels an already sent mutation."""
    stop_event = asyncio.Event()
    task = asyncio.create_task(_run_rehearsal(*args, **kwargs, stop_event=stop_event))
    cancelled = False
    while True:
        try:
            result = await asyncio.shield(task)
            break
        except asyncio.CancelledError:
            stop_event.set()
            cancelled = True
            if task.cancelled():
                raise
        except Exception:
            if cancelled:
                raise asyncio.CancelledError() from None
            raise
    if cancelled:
        raise asyncio.CancelledError()
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--plan", required=True)
    parser.add_argument("--scenario", required=True)
    parser.add_argument("--output-dir", required=True)
    parser.add_argument("--run", action="store_true")
    parser.add_argument("--confirm-isolated-target", action="store_true")
    parser.add_argument("--confirm-write-rehearsal", action="store_true")
    for name in ("config", "identity", "environment", "image"):
        parser.add_argument("--delivery-" + name)
    args = parser.parse_args(argv)
    try:
        plan, scenario = read_json(args.plan), read_json(args.scenario)
        valid = validate(plan, scenario)
        if not args.run:
            os.mkdir(args.output_dir, 0o700)
            save_json(Path(args.output_dir) / "validation.json", valid)
            print("Offline rehearsal inputs valid; no network or writes executed")
            return 0
        if not args.confirm_isolated_target or not args.confirm_write_rehearsal:
            raise ValueError("Separate isolated write confirmation required")
        if any(
            not getattr(args, "delivery_" + k)
            for k in ("config", "identity", "environment", "image")
        ):
            raise ValueError("Original delivery session inputs required")
        if Path(args.output_dir).exists():
            raise FileExistsError("Existing journal cannot be replayed")
        actors = json.loads(os.environ.get("LIGHTRAG_PERF_ACTOR_KEYS", "{}"))
        base_url = os.environ.get("LIGHTRAG_PERF_BASE_URL", "")
        # Validate credential/URL shape before opening the operator session.
        validate_connection(
            base_url,
            actors,
            timeout=plan["limits"]["timeout_seconds"],
            max_bytes=plan["limits"]["max_bytes"],
            max_calls=plan["limits"]["max_calls"],
        )

        async def run():
            from lightrag.distributed.release.delivery import delivery_session

            async with delivery_session(
                config_path=args.delivery_config,
                identity_path=args.delivery_identity,
                environment_path=args.delivery_environment,
                image=args.delivery_image,
            ) as session:
                control = OrchestratorControl(
                    session.store,
                    session.transport,
                    session.identity,
                    expected_epoch=session.original_epoch,
                )
                return await run_rehearsal(
                    plan,
                    scenario,
                    base_url=base_url,
                    actors=actors,
                    control=control,
                    output_dir=args.output_dir,
                    confirmed=True,
                    confirm_writes=True,
                )

        report = asyncio.run(run())
        print(
            "Rehearsal checks "
            + ("passed" if report["rehearsal_checks_passed"] else "failed")
            + "; not production capacity acceptance"
        )
        return 0 if report["rehearsal_checks_passed"] else 2
    except Exception as error:
        print(
            type(error).__name__
            + ": rehearsal refused; inspect private evidence, do not replay unknown writes"
        )
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
