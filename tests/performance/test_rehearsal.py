import json

import httpx
import pytest

from tests.performance.test_load import plan as read_plan
from tests.performance.test_quality import golden


def plan():
    p = read_plan()
    p.update(target_qps=100, duration_seconds=0.3, max_requests=30)
    p["isolation"]["config_hashes"] = ["b" * 64, "c" * 64]
    return dict(
        version=2,
        usage=dict(
            poll_seconds=0.001,
            sample_timeout_seconds=0.2,
            transition_timeout_seconds=0.2,
            max_samples=3000,
            max_evidence_bytes=4 * 1024**2,
        ),
        environment="isolated",
        run_id="run1",
        approval_id="isolated-review",
        release=dict(
            scope="isolation/run",
            release_id="release1",
            namespace="isolated-run",
            namespace_uid="uid-ns",
            kube_system_uid="uid-cluster",
            profile_hash="a" * 64,
            blue_config_hash="b" * 64,
            green_config_hash="c" * 64,
            blue_digest="sha256:" + "d" * 64,
            green_digest="sha256:" + "e" * 64,
            epoch=1,
        ),
        read_plan=p,
        limits=dict(
            timeout_seconds=1,
            poll_seconds=0.001,
            max_calls=200,
            max_bytes=10000,
            max_write_inflight=4,
        ),
        actions=[
            dict(
                id="insert",
                phase="PREPARED",
                actor="reader",
                kind="insert_text",
                payload=dict(text="NEW42", file_source="perf-run1-new.txt"),
            ),
            dict(
                id="create",
                phase="PREPARED",
                actor="reader",
                kind="create_entity",
                payload=dict(name="PERF_RUN1_NODE", description="before"),
            ),
            dict(
                id="edit",
                phase="DRAINING",
                actor="reader",
                kind="edit_entity",
                payload=dict(create_action="create", description="after"),
            ),
            dict(
                id="delete",
                phase="WRITER_SWITCHED",
                actor="reader",
                kind="delete_document",
                payload=dict(insert_action="insert"),
            ),
        ],
        checkpoints=[
            dict(
                id="before",
                phase="PREPARED",
                after=["insert", "create"],
                cases=["fact-v2"],
            ),
            dict(
                id="after",
                phase="COMPLETED",
                after=["edit", "delete"],
                cases=["fact-v2"],
            ),
        ],
    )


def scenario():
    return dict(
        quality=golden(),
        load_queries=[{"query": "synthetic"}],
        long_stream_case="fact-v2",
    )


class Control:
    def __init__(self):
        self.queued = False
        self.long_active = False
        self.phase = 0
        self.edited = False
        self.count = 0
        from lightrag.performance import QueryMetrics

        self.metrics = {
            slot + str(i): QueryMetrics(enabled=True)
            for slot in ("blue", "green")
            for i in range(4)
        }
        self.finals = {}

    def validate(self, settings):
        pass

    def validate_target(self, base_url):
        pass

    async def start(self, settings):
        self.count += 1

    async def inspect(self, settings):
        from scripts.performance.control import PHASES

        phase = PHASES[self.phase]
        return dict(
            phase=phase,
            epoch=1 + int(self.phase >= 3),
            instances=[
                dict(
                    uid=s + str(i),
                    slot=s,
                    lifecycle={"queries": int(s == "blue" and self.long_active)},
                )
                for s in (["green"] if phase == "COMPLETED" else ["blue", "green"])
                for i in range(4)
            ],
            route_evidence_sha256="f" * 64,
        )

    async def receipt(self, settings, *, key, principal_id):
        return (
            dict(
                id="22222222-2222-2222-2222-222222222222",
                state="SUCCEEDED" if self.phase >= 3 else "QUEUED",
            )
            if self.queued
            else None
        )

    async def usage(self, settings):
        from lightrag.distributed.release.usage import compact_usage
        from tests.performance.test_dynamic_usage import member
        from scripts.performance.control import PHASES

        phase = PHASES[self.phase]
        members = []
        for slot in ["green"] if phase == "COMPLETED" else ["blue", "green"]:
            for i in range(4):
                uid = slot + str(i)
                row = member(slot, i)
                row["usage"] = compact_usage(self.metrics[uid])
                if slot == "blue" and phase == "RETIRING" and self.finals:
                    row = self.finals[uid]
                members.append(row)
        return dict(phase=phase, epoch=1 + int(self.phase >= 3), members=members)

    async def advance(self, settings, *, usage_gate=None):
        if self.phase == 4 and usage_gate is not None:
            frame = await self.usage(settings)
            self.finals = {
                r["uid"]: dict(r, state="final")
                for r in frame["members"]
                if r["slot"] == "blue"
            }
            await usage_gate.before_scale()
        self.phase += 1


def service(control, calls, *, unknown=False):
    deleted = False

    async def handle(request):
        nonlocal deleted
        path = request.url.path
        calls.append((request.method, path))
        if (
            path == "/query/stream"
            and request.headers.get("x-performance-rehearsal") == "long-stream"
        ):

            class Body(httpx.AsyncByteStream):
                async def __aiter__(self):
                    import asyncio

                    control.long_active = True
                    try:
                        yield b'{"response":"first"}\n'
                        while control.phase < 3:
                            await asyncio.sleep(0.001)
                        yield b'{"response":"last"}\n'
                    finally:
                        control.long_active = False

            return httpx.Response(
                200, headers={"content-type": "application/x-ndjson"}, stream=Body()
            )
        if path == "/release/status":
            return httpx.Response(
                200,
                json=dict(
                    profile_hash="a" * 64,
                    config_hash="b" * 64 if control.phase < 3 else "c" * 64,
                ),
            )
        if path == "/documents/text":
            return httpx.Response(
                200,
                json=dict(status="success", track_id="track1"),
                headers={
                    "X-Release-Command-Id": "11111111-1111-1111-1111-111111111111"
                },
            )
        if path == "/documents/track_status/track1":
            docs = (
                []
                if deleted
                else [
                    dict(
                        id="doc-owned",
                        file_path="perf-run1-new.txt",
                        status="processed",
                    )
                ]
            )
            return httpx.Response(200, json=dict(total_count=len(docs), documents=docs))
        if path == "/graph/entity/create":
            return httpx.Response(200, json=dict(status="success"))
        if path == "/graph/entity/edit":
            control.queued = True
            if unknown:
                raise httpx.ReadTimeout("private backend error")
            # A queued request may only finish after actual writer transfer.
            import asyncio

            while control.phase < 3:
                await asyncio.sleep(0.001)
            control.edited = True
            return httpx.Response(
                200,
                json=dict(status="success"),
                headers={
                    "X-Release-Command-Id": "22222222-2222-2222-2222-222222222222"
                },
            )
        if path == "/graphs":
            return httpx.Response(
                200,
                json=dict(
                    nodes=[
                        dict(
                            id="PERF_RUN1_NODE",
                            properties={
                                "description": "after" if control.edited else "before"
                            },
                        )
                    ]
                ),
            )
        if path == "/documents/delete_document":
            payload = json.loads(request.content)
            assert payload == dict(
                doc_ids=["doc-owned"], delete_file=False, delete_llm_cache=False
            )
            deleted = True
            return httpx.Response(200, json=dict(status="deletion_started"))
        if path == "/query/data":
            return httpx.Response(
                200,
                json=dict(
                    status="success", data={"chunks": [{"chunk_id": "chunk-new"}]}
                ),
            )
        if path == "/query/stream":
            return httpx.Response(
                200,
                headers={"content-type": "application/x-ndjson"},
                content=b'{"references":[{"reference_id":"1","file_path":"perf/run/new.txt"}]}\n{"response":"NEW42 [1]."}\n',
            )
        if path == "/query":
            return httpx.Response(
                200,
                json=dict(
                    response="NEW42 [1].",
                    llm_generated=True,
                    references=[dict(reference_id="1", file_path="perf/run/new.txt")],
                ),
            )
        raise AssertionError(path)

    return httpx.MockTransport(handle)


async def test_missing_confirmation_or_invalid_plan_never_opens_target(tmp_path):
    from scripts.performance.rehearsal import run_rehearsal

    c = Control()
    with pytest.raises(ValueError, match="confirmation"):
        await run_rehearsal(
            plan(),
            scenario(),
            base_url="https://fixture.invalid",
            actors={"reader": "private"},
            control=c,
            output_dir=tmp_path / "run",
        )
    assert c.count == 0 and not (tmp_path / "run").exists()


async def test_mixed_writes_cross_gate_and_verify_final_state_without_fake_success(
    tmp_path,
):
    from scripts.performance.rehearsal import run_rehearsal

    c, calls = Control(), []
    data = plan()
    result = await run_rehearsal(
        data,
        scenario(),
        base_url="https://fixture.invalid",
        actors={"reader": "private-key"},
        control=c,
        output_dir=tmp_path / "run",
        confirmed=True,
        confirm_writes=True,
        transport=service(c, calls),
    )
    assert result["rehearsal_checks_passed"] and result["write_summary"] == {
        "CONFIRMED": 4
    }
    assert (
        len(result["phases"]) == 6
        and result["phases"][1]["instance_count"] == 8
        and c.edited
    )
    assert not result["production_accepted"] and not result["capacity_accepted"]
    assert calls.count(("POST", "/graph/entity/edit")) == 1
    assert (
        "private-key" not in str(result)
        and "NEW42" not in str(result)
        and "doc-owned" not in str(result)
    )
    assert (tmp_path / "run" / "attempts.jsonl").stat().st_mode & 0o777 == 0o600


async def test_unknown_write_ack_is_not_replayed_or_reported_as_completed(tmp_path):
    from scripts.performance.rehearsal import run_rehearsal

    c, calls = Control(), []
    result = await run_rehearsal(
        plan(),
        scenario(),
        base_url="https://fixture.invalid",
        actors={"reader": "private"},
        control=c,
        output_dir=tmp_path / "run",
        confirmed=True,
        confirm_writes=True,
        transport=service(c, calls, unknown=True),
    )
    assert (
        not result["rehearsal_checks_passed"]
        and result["write_summary"].get("UNCERTAIN") == 1
    )
    assert calls.count(("POST", "/graph/entity/edit")) == 1
    with pytest.raises(FileExistsError):
        await run_rehearsal(
            plan(),
            scenario(),
            base_url="https://fixture.invalid",
            actors={"reader": "private"},
            control=c,
            output_dir=tmp_path / "run",
            confirmed=True,
            confirm_writes=True,
            transport=service(c, calls),
        )


def test_destructive_scope_and_callback_payloads_are_refused():
    from scripts.performance.rehearsal import validate

    data = plan()
    data["actions"][0]["payload"]["file_source"] = "existing/document.txt"
    with pytest.raises(ValueError):
        validate(data, scenario())
    data = plan()
    data["actions"][3]["payload"] = {"doc_ids": ["arbitrary-existing-doc"]}
    with pytest.raises(ValueError):
        validate(data, scenario())


async def test_control_failure_drains_inflight_write_before_closing_http(tmp_path):
    import asyncio
    from scripts.performance.rehearsal import run_rehearsal

    started = asyncio.Event()

    class Broken(Control):
        async def advance(self, settings, *, usage_gate=None):
            if self.phase == 1:
                await started.wait()
                raise OSError("control unavailable")
            self.phase += 1

    c, calls = Broken(), []
    underlying = service(c, calls)

    async def handle(request):
        if request.url.path == "/graph/entity/edit":
            started.set()
            await asyncio.sleep(0.02)
            c.edited = True
            return httpx.Response(200, json={"status": "success"})
        return await underlying.handle_async_request(request)

    report = await run_rehearsal(
        plan(),
        scenario(),
        base_url="https://fixture.invalid",
        actors={"reader": "private"},
        control=c,
        output_dir=tmp_path / "run",
        confirmed=True,
        confirm_writes=True,
        transport=httpx.MockTransport(handle),
    )
    assert not report["rehearsal_checks_passed"]
    assert report["write_summary"] == {"CONFIRMED": 3}


async def test_full_rehearsal_drives_the_existing_orchestrator_not_a_phase_stub(
    tmp_path,
):
    from scripts.performance.rehearsal import run_rehearsal
    from scripts.performance.control import PHASES, OrchestratorControl
    from lightrag.distributed.release.models import ReleaseIdentity
    from tests.distributed.release.test_orchestrator import (
        Store,
        Transport as BaseTransport,
    )
    from tests.performance.test_control import Transport as PodTransport

    class ActualStore(Store):
        def __init__(self):
            super().__init__()
            self.state["owner"] = None

        async def begin(self, identity, expected_epoch):
            if self.state["phase"] != "COMPLETED":
                self.state["owner"] = identity.release_id
            return self.state.copy()

        async def capture_baseline(self, identity, **kwargs):
            assert kwargs["digest"] == "sha256:" + "d" * 64

        async def transfer_writer(
            self, identity, *, expected_epoch, expected_instances
        ):
            assert expected_instances == ["green0", "green1", "green2", "green3"]
            self.calls.append("TRANSFER")
            self.state.update(
                phase="WRITER_SWITCHED",
                epoch=2,
                active_slot="green",
                candidate_slot="blue",
            )

    store = ActualStore()

    class BackendState:
        edited = False
        long_active = False

        @property
        def phase(self):
            return PHASES.index(store.state["phase"])

    backend = BackendState()
    backend.queued = False

    class T(BaseTransport, PodTransport):
        def __init__(self):
            BaseTransport.__init__(self)
            self.counts = {"blue": 4, "green": 4}
            from types import SimpleNamespace

            self.config = SimpleNamespace(
                target={"host": "fixture.invalid", "namespace": "isolated-run"},
                onboarding={
                    "namespace_uid": "uid-ns",
                    "kube_system_uid": "uid-cluster",
                    "profile_hash": "a" * 64,
                },
                replicas=4,
            )

        async def verify_candidate(self, *args):
            return ["green0", "green1", "green2", "green3"]

        async def pods(self, slot):
            return (
                []
                if slot == "blue" and store.state["phase"] == "COMPLETED"
                else [object()] * 4
            )

        async def verified_pods(self, slot, **kwargs):
            rows = await PodTransport.verified_pods(self, slot, **kwargs)
            for _, s in rows:
                s["lifecycle"] = {
                    "queries": int(slot == "blue" and backend.long_active)
                }
            return rows

    identity = ReleaseIdentity(
        "isolation/run", "release1", "v1.0.0", "d" * 40, "sha256:" + "e" * 64, "c" * 64
    )

    class NativeControl(OrchestratorControl):
        async def usage(self, settings):
            from tests.performance.test_dynamic_usage import member

            if not hasattr(self, "_usage_rows"):
                self._usage_rows = [
                    member(s, i) for s in ("blue", "green") for i in range(4)
                ]
            rows = self._usage_rows
            if "RETIRE" in store.calls:
                for row in rows:
                    if row["slot"] == "blue":
                        row["state"] = "final"
            return dict(
                phase=store.state["phase"],
                epoch=store.state["epoch"],
                members=[
                    r
                    for r in rows
                    if r["slot"] == "green" or store.state["phase"] != "COMPLETED"
                ],
            )

        async def receipt(self, settings, *, key, principal_id):
            return (
                dict(
                    id="22222222-2222-2222-2222-222222222222",
                    state="SUCCEEDED" if backend.phase >= 3 else "QUEUED",
                )
                if backend.queued
                else None
            )

    c = NativeControl(store, T(), identity, expected_epoch=1)
    calls = []
    result = await run_rehearsal(
        plan(),
        scenario(),
        base_url="https://fixture.invalid",
        actors={"reader": "private"},
        control=c,
        output_dir=tmp_path / "run",
        confirmed=True,
        confirm_writes=True,
        transport=service(backend, calls),
    )
    assert (
        result["rehearsal_checks_passed"]
        and result["long_stream"]["completed_after_transfer"]
    )
    assert store.calls.count("TRANSFER") == 1 and store.state["phase"] == "COMPLETED"


def test_cli_default_validates_offline_and_run_without_confirmation_is_refused(
    tmp_path,
):
    from scripts.performance.rehearsal import main

    p, s = tmp_path / "plan.json", tmp_path / "scenario.json"
    p.write_text(json.dumps(plan()))
    s.write_text(json.dumps(scenario()))
    args = [
        "--plan",
        str(p),
        "--scenario",
        str(s),
        "--output-dir",
        str(tmp_path / "validation"),
    ]
    assert main(args) == 0
    assert (
        json.loads((tmp_path / "validation" / "validation.json").read_text())[
            "network_run"
        ]
        is False
    )
    assert main([*args[:-1], str(tmp_path / "blocked"), "--run"]) == 2
    assert not (tmp_path / "blocked").exists()


@pytest.mark.parametrize("stale", [False, True])
async def test_checkpoint_ground_truth_tracks_revision_and_rejects_stale_answers(
    tmp_path, stale
):
    import copy
    from scripts.performance.rehearsal import run_rehearsal

    c, calls = Control(), []
    p, s = plan(), scenario()
    stable = s["quality"]["cases"][0]
    old = copy.deepcopy(stable)
    old.update(id="old", query="version-sensitive")
    old["expect"].update(
        required_facts=[["OLD17"]],
        forbidden_facts=["NEW42"],
        expected_chunks=["chunk-old"],
    )
    new = copy.deepcopy(stable)
    new.update(id="new", query="version-sensitive")
    s["quality"]["cases"] = [stable, old, new]
    p["checkpoints"][0]["cases"] = ["fact-v2", "old"]
    p["checkpoints"][1]["cases"] = ["new"]
    underlying = service(c, calls)

    async def handle(request):
        if (
            request.method == "POST"
            and request.url.path in {"/query", "/query/data"}
            and json.loads(request.content)["query"] == "version-sensitive"
        ):
            current = c.edited and not stale
            if request.url.path == "/query/data":
                return httpx.Response(
                    200,
                    json=dict(
                        status="success",
                        data={
                            "chunks": [
                                {"chunk_id": "chunk-new" if current else "chunk-old"}
                            ]
                        },
                    ),
                )
            return httpx.Response(
                200,
                json=dict(
                    response=("NEW42" if current else "OLD17") + " [1].",
                    llm_generated=True,
                    references=[dict(reference_id="1", file_path="perf/run/new.txt")],
                ),
            )
        return await underlying.handle_async_request(request)

    report = await run_rehearsal(
        p,
        s,
        base_url="https://fixture.invalid",
        actors={"reader": "private"},
        control=c,
        output_dir=tmp_path / "run",
        confirmed=True,
        confirm_writes=True,
        transport=httpx.MockTransport(handle),
    )
    assert report["rehearsal_checks_passed"] is (not stale)
    if stale:
        assert {"facts_missing", "forbidden_fact", "chunk_recall"} <= set(
            report["quality"][-1]["cases"][0]["failures"]
        )


def test_rehearsal_refuses_a_matrix_without_closed_gate_and_post_transfer_writes():
    from scripts.performance.rehearsal import validate

    p = plan()
    for a in p["actions"]:
        a["phase"] = "PREPARED"
    with pytest.raises(ValueError, match="gate"):
        validate(p, scenario())


def test_owned_source_survives_native_insert_request_normalization():
    from tests.api.routes.test_insert_text_whitespace import InsertTextRequest
    from scripts.performance.rehearsal import validate

    p = plan()
    source = p["actions"][0]["payload"]["file_source"]
    validate(p, scenario())
    assert InsertTextRequest(**p["actions"][0]["payload"]).file_source == source


async def test_background_load_error_stops_before_further_release_advance(tmp_path):
    import asyncio
    from scripts.performance.rehearsal import run_rehearsal

    class Slow(Control):
        async def inspect(self, settings):
            await asyncio.sleep(0.02)
            return await super().inspect(settings)

    c, calls = Slow(), []
    underlying = service(c, calls)

    async def handle(request):
        if (
            request.url.path == "/query"
            and json.loads(request.content)["query"] == "synthetic"
        ):
            return httpx.Response(500, json={"error": "unavailable"})
        return await underlying.handle_async_request(request)

    r = await run_rehearsal(
        plan(),
        scenario(),
        base_url="https://fixture.invalid",
        actors={"reader": "private"},
        control=c,
        output_dir=tmp_path / "run",
        confirmed=True,
        confirm_writes=True,
        transport=httpx.MockTransport(handle),
    )
    assert not r["rehearsal_checks_passed"] and r["error_class"] is not None
    assert c.phase == 0
    assert not any(path.startswith("/graph/entity/") for _, path in calls)


async def test_failed_query_freezes_dependency_write_not_yet_sent(tmp_path):
    import asyncio
    from scripts.performance.rehearsal import run_rehearsal

    c, calls = Control(), []
    started = asyncio.Event()
    p = plan()
    p["actions"][2]["phase"] = "PREPARED"
    p["actions"][0]["phase"] = "DRAINING"
    p["checkpoints"][0]["after"] = []
    underlying = service(c, calls)

    async def handle(request):
        if request.url.path == "/graph/entity/create":
            started.set()
            await asyncio.sleep(0.03)
        if (
            request.url.path == "/query"
            and json.loads(request.content)["query"] != "synthetic"
        ):
            await asyncio.wait_for(started.wait(), 1)
            return httpx.Response(503, json={"detail": "unavailable"})
        return await underlying.handle_async_request(request)

    r = await run_rehearsal(
        p,
        scenario(),
        base_url="https://fixture.invalid",
        actors={"reader": "private"},
        control=c,
        output_dir=tmp_path / "run",
        confirmed=True,
        confirm_writes=True,
        transport=httpx.MockTransport(handle),
    )
    assert not r["rehearsal_checks_passed"]
    assert ("POST", "/graph/entity/create") in calls
    assert ("POST", "/graph/entity/edit") not in calls


async def test_cancellation_preserves_sent_write_outcome_and_closes_all_resources(
    tmp_path, monkeypatch
):
    import asyncio
    from scripts.performance import rehearsal as m

    c, calls = Control(), []
    started, finish = asyncio.Event(), asyncio.Event()
    underlying = service(c, calls)
    journal_closed, clients_closed = [], []
    original_close, original_exit = m.Journal.close, m.BoundedClient.__aexit__

    def close(self):
        journal_closed.append(True)
        original_close(self)

    async def exit_client(self, *args):
        clients_closed.append(True)
        await original_exit(self, *args)

    monkeypatch.setattr(m.Journal, "close", close)
    monkeypatch.setattr(m.BoundedClient, "__aexit__", exit_client)

    async def handle(request):
        if request.url.path == "/documents/text":
            started.set()
            await finish.wait()
        return await underlying.handle_async_request(request)

    task = asyncio.create_task(
        m.run_rehearsal(
            plan(),
            scenario(),
            base_url="https://fixture.invalid",
            actors={"reader": "private"},
            control=c,
            output_dir=tmp_path / "run",
            confirmed=True,
            confirm_writes=True,
            transport=httpx.MockTransport(handle),
        )
    )
    await asyncio.wait_for(started.wait(), 1)
    task.cancel()
    await asyncio.sleep(0)
    task.cancel()
    finish.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert journal_closed and clients_closed
    events = [
        json.loads(line)
        for line in (tmp_path / "run" / "attempts.jsonl").read_text().splitlines()
    ]
    assert [e["state"] for e in events if e.get("action") == "insert"][
        -1
    ] == "CONFIRMED"
    assert (tmp_path / "run" / "report.json").exists()
    assert not c.long_active


async def test_redirected_write_is_unknown_not_confirmed_or_retried(tmp_path):
    from scripts.performance.rehearsal import run_rehearsal

    c, calls = Control(), []
    underlying = service(c, calls)
    sends = []

    async def handle(request):
        if request.url.path == "/documents/text":
            sends.append(True)
            return httpx.Response(302, json={"status": "success", "track_id": "track1"})
        return await underlying.handle_async_request(request)

    report = await run_rehearsal(
        plan(),
        scenario(),
        base_url="https://fixture.invalid",
        actors={"reader": "private"},
        control=c,
        output_dir=tmp_path / "run",
        confirmed=True,
        confirm_writes=True,
        transport=httpx.MockTransport(handle),
    )
    assert (
        not report["rehearsal_checks_passed"]
        and report["write_summary"].get("UNCERTAIN") == 1
    )
    assert len(sends) == 1


def test_closed_gate_dependency_is_refused_before_owner_acquisition():
    from scripts.performance.rehearsal import validate

    p = plan()
    p["actions"][1]["phase"] = "DRAINING"
    p["checkpoints"][0]["after"] = ["insert"]
    with pytest.raises(ValueError, match="dependency"):
        validate(p, scenario())


async def test_exhausted_load_budget_cannot_pass_as_continuous_release_coverage(
    tmp_path,
):
    import asyncio
    from scripts.performance.rehearsal import run_rehearsal

    class Slow(Control):
        async def inspect(self, settings):
            await asyncio.sleep(0.035)
            return await super().inspect(settings)

    c, calls = Slow(), []
    p = plan()
    p["read_plan"].update(target_qps=1000, duration_seconds=0.001, max_requests=1)
    r = await run_rehearsal(
        p,
        scenario(),
        base_url="https://fixture.invalid",
        actors={"reader": "private"},
        control=c,
        output_dir=tmp_path / "run",
        confirmed=True,
        confirm_writes=True,
        transport=service(c, calls),
    )
    assert not r["rehearsal_checks_passed"] and r["error_class"] is not None
    assert c.phase == 0


async def test_journal_io_failure_still_closes_http_and_drains_children(
    tmp_path, monkeypatch
):
    from scripts.performance import rehearsal as m

    c, calls = Control(), []
    closed = []
    original_record, original_exit = m.Journal.record, m.BoundedClient.__aexit__

    def record(self, **row):
        if row.get("action") == "insert" and row.get("state") == "CONFIRMED":
            raise OSError("disk full")
        return original_record(self, **row)

    async def exit(self, *args):
        closed.append(True)
        return await original_exit(self, *args)

    monkeypatch.setattr(m.Journal, "record", record)
    monkeypatch.setattr(m.BoundedClient, "__aexit__", exit)
    report = await m.run_rehearsal(
        plan(),
        scenario(),
        base_url="https://fixture.invalid",
        actors={"reader": "private"},
        control=c,
        output_dir=tmp_path / "run",
        confirmed=True,
        confirm_writes=True,
        transport=service(c, calls),
    )
    assert not report["rehearsal_checks_passed"] and report["error_class"]
    assert closed and not c.long_active


@pytest.mark.parametrize(
    "field,value", [("request_timeout_seconds", 1e20), ("duration_seconds", 1e20)]
)
def test_mixed_runner_rejects_unbounded_read_lifetimes(field, value):
    from scripts.performance.rehearsal import validate

    p = plan()
    p["read_plan"][field] = value
    if field == "duration_seconds":
        p["read_plan"]["target_qps"] = 1e-20
    with pytest.raises(ValueError, match="lifetime"):
        validate(p, scenario())


def test_duplicate_checkpoint_cases_are_refused_before_any_write():
    from scripts.performance.rehearsal import validate

    p = plan()
    p["checkpoints"][0]["cases"] *= 2
    with pytest.raises(ValueError, match="checkpoint"):
        validate(p, scenario())
