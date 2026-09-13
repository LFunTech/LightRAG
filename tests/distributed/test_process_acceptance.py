"""Independent OS/Manager/pool acceptance, never a Kubernetes deployment claim."""

import asyncio
import json
import multiprocessing
import os
import signal
import sys
from uuid import uuid4

import pytest

from lightrag.base import DocStatus
from lightrag.constants import GRAPH_FIELD_SEP
from lightrag.distributed import PostgresCoordinator
from tests.distributed.process_worker import worker

pytestmark = pytest.mark.integration


class Process:
    def __init__(self, root, workspace, label, bootstrap=False, barrier=None):
        ctx = multiprocessing.get_context("spawn")
        self.pipe, child = ctx.Pipe()
        self.process = ctx.Process(
            target=worker,
            args=(
                child,
                dict(
                    root=str(root),
                    workspace=workspace,
                    label=label,
                    bootstrap=bootstrap,
                    barrier=barrier,
                ),
            ),
        )
        self.process.start()
        child.close()

    async def receive(self):
        assert await asyncio.to_thread(self.pipe.poll, 60), "worker response timed out"
        return self.pipe.recv()

    async def call(self, action, **kwargs):
        self.pipe.send(dict(action=action, **kwargs))
        return await self.receive()

    async def stop(self):
        if self.process.is_alive():
            self.pipe.send({"action": "stop_transport"})
            await self.receive()
        await asyncio.to_thread(self.process.join, 10)
        assert not self.process.is_alive()


@pytest.fixture
async def processes(monkeypatch, tmp_path):
    if not os.getenv("LIGHTRAG_COORDINATION_DSN") or not os.getenv(
        "POSTGRES_DATABASE", ""
    ).startswith("local_debug_"):
        pytest.skip("explicit isolated PG/HugeGraph environment required")
    monkeypatch.setenv("POSTGRES_PASSWORD", os.getenv("POSTGRES_PASSWORD", "test"))
    monkeypatch.setenv("LIGHTRAG_SHARED_STORAGE", "true")
    monkeypatch.setenv("LIGHTRAG_DEPLOYMENT_ID", "local_debug_process_tests")
    monkeypatch.setenv("HUGEGRAPH_GRAPH", "hugegraph")
    monkeypatch.setenv("HUGEGRAPH_GRAPHSPACE", "DEFAULT")
    await PostgresCoordinator.migrate(os.environ["LIGHTRAG_COORDINATION_DSN"])
    workspace = "local_debug_" + uuid4().hex
    monkeypatch.setenv("POSTGRES_WORKSPACE", workspace)
    clients = []
    barrier = multiprocessing.get_context("spawn").Barrier(2)

    async def start(label, bootstrap=False, expected_error=False):
        p = Process(tmp_path, workspace, label, bootstrap, barrier)
        clients.append(p)
        p.identity = await p.receive()
        if expected_error:
            assert p.identity == {"error": "WorkspaceFencedError"}, p.identity
            await asyncio.to_thread(p.process.join, 10)
            assert not p.process.is_alive()
            return p
        assert "ready" in p.identity, p.identity
        return p

    first = await start("a", True)
    second = await start("b")
    assert first.identity["ready"] != second.identity["ready"]
    assert first.identity["manager"] != second.identity["manager"]
    assert first.identity["owner"] != second.identity["owner"]
    yield first, second, start, tmp_path, workspace
    for p in clients:
        if p.process.is_alive():
            await p.stop()
        p.pipe.close()


async def file_exists(path):
    for _ in range(1000):
        if path.exists():
            return
        await asyncio.sleep(0.01)
    pytest.fail(f"worker never reached {path.name}")


async def wait_status(p, ids, expected):
    for _ in range(500):
        result = await p.call("audit", ids=ids)
        rows = result["result"]["status"]
        if set(rows) == set(ids) and all(
            row.status == expected for row in rows.values()
        ):
            return result["result"]
        await asyncio.sleep(0.02)
    pytest.fail(f"status did not converge to {expected}: {rows}")


async def test_independent_physical_writes_overlap_and_maintenance_cannot_enter(
    processes,
):
    a, b, _, root, _ = processes
    assert "result" in await a.call(
        "enqueue",
        text=["First independent research.", "Second independent research."],
        ids=["one", "two"],
    )
    a.pipe.send({"action": "process", "mode": "independent", "barrier": True})
    b.pipe.send({"action": "process", "mode": "independent", "barrier": True})
    await asyncio.gather(file_exists(root / "a.ready"), file_exists(root / "b.ready"))
    # Both claimed workers reached distinct real graph writes, despite page size 1.
    (root / "release").touch()
    assert "result" in await a.receive()
    assert "result" in await b.receive()
    times = [json.loads((root / f"{x}.commit").read_text()) for x in ("a", "b")]
    assert max(t["start"] for t in times) < min(t["end"] for t in times), times
    (root / "acceptance.json").write_text(
        json.dumps(
            {
                "processes": [a.identity, b.identity],
                "http_intervals_ns": times,
                "overlap_ns": min(t["end"] for t in times)
                - max(t["start"] for t in times),
            },
            indent=2,
        )
    )
    assert (await a.call("audit", ids=["one", "two"], name="Atlasa"))["result"]["node"]
    assert (await b.call("audit", ids=["one", "two"], name="Atlasb"))["result"]["node"]


async def test_shared_evidence_duplicate_claim_and_lost_notification(processes):
    a, b, _, _, _ = processes
    await a.call("pause")
    await b.call("poll", mode="shared")
    # Enqueue occurs in a different Manager: its notification cannot wake b.
    texts = [
        "Atlas cooperates with Borealis on one.",
        "Atlas cooperates with Borealis on two.",
    ]
    await a.call("enqueue", text=texts, ids=["one", "two"])
    await asyncio.sleep(0.1)
    await wait_status(a, ["one", "two"], DocStatus.PENDING)
    await a.call("resume")
    await a.call("process", mode="shared")
    audit = await wait_status(a, ["one", "two"], DocStatus.PROCESSED)
    await b.call("stop_poll")
    before = (
        audit["edge"]["weight"],
        set(audit["edge"]["source_id"].split(GRAPH_FIELD_SEP)),
    )
    assert len(before[1]) == 2
    assert before[0] >= 2
    assert set(audit["tracking"]["chunk_ids"]) == before[1]
    assert all(audit["anchors"].values()) and all(audit["relation_anchors"].values())
    await a.call("enqueue", text=texts, ids=["one", "two"])
    await asyncio.gather(a.call("process"), b.call("process"))
    after = (await a.call("audit", ids=["one", "two"]))["result"]
    assert (
        after["edge"]["weight"],
        set(after["edge"]["source_id"].split(GRAPH_FIELD_SEP)),
    ) == before
    state = (await a.call("inspect"))["result"]
    assert not state["claims"] and not state["fenced"]
    peer_audit = (await b.call("audit", ids=["one", "two"]))["result"]
    assert len(after["parsed"]) == len(peer_audit["parsed"]) == 1
    parsed = after["parsed"] + peer_audit["parsed"]
    assert sorted(parsed) == ["one", "two"], parsed


async def test_failed_once_only_retry_survives_request_process_restart(processes):
    a, b, start, _, _ = processes
    await a.call("enqueue", text="Atlas failure test.", ids=["failed"])
    assert "result" in await a.call("process", mode="fail_llm")
    audit = await wait_status(a, ["failed"], DocStatus.FAILED)
    original_version = audit["status"]["failed"].updated_at
    await b.call("poll", mode="fail_llm")
    await asyncio.sleep(0.15)
    assert (await a.call("audit", ids=["failed"]))["result"]["status"][
        "failed"
    ].updated_at == original_version
    await b.call("stop_poll")
    await a.call("retry", id="durable-once")
    await a.stop()
    c = await start("c")
    await c.call("process", mode="fail_llm")
    audit = await wait_status(c, ["failed"], DocStatus.FAILED)
    assert audit["status"]["failed"].updated_at != original_version
    calls = audit["calls"]
    await c.call("retry", id="durable-once")
    await c.call("poll", mode="fail_llm")
    await asyncio.sleep(0.15)
    assert (await c.call("stop_poll"))["result"]["calls"] == calls


@pytest.mark.parametrize("fault", ["kill", "ack_loss"])
async def test_commit_uncertainty_restart_audit_recover_and_real_purge(
    processes, fault
):
    a, b, start, root, workspace = processes
    await a.call(
        "enqueue", text="Atlas cooperates with Borealis with a fault.", ids=["fault"]
    )
    a.pipe.send({"action": "process", "fault": fault})
    await file_exists(root / "a.commit")
    if fault == "kill":
        # Real HugeGraph response arrived; physical pending has not been ACKed.
        busy = await b.call("maintenance")
        assert busy["error"] == "CoordinationBusyError", busy
        assert not (root / "maintenance-admitted").exists()
        os.kill(a.process.pid, signal.SIGKILL)
        await asyncio.to_thread(a.process.join, 10)
        # The killed client's Manager is an owned fixture process, not a backend.
        try:
            os.kill(a.identity["manager"], signal.SIGTERM)
        except ProcessLookupError:
            pass
    else:
        result = await a.receive()
        assert result["error"] in {
            "WorkspaceFencedError",
            "CoordinationUnavailableError",
        }, result
        await a.stop()
    denied = await b.call("new_write")
    assert denied["error"] == "WorkspaceFencedError", denied
    audit = (await b.call("audit", ids=["fault"]))["result"]
    assert audit["node"] is not None
    assert audit["anchors"]["fault"] and audit["relation_anchors"]["fault"]
    assert audit["vector"] is None  # graph commit preceded vector commit
    state = (await b.call("inspect"))["result"]
    assert any(m["state"] == "pending" for m in state["mutations"])
    await b.stop()
    import asyncpg

    audit_connection = await asyncpg.connect(os.environ["LIGHTRAG_COORDINATION_DSN"])
    try:
        for _ in range(500):
            remaining = await audit_connection.fetchval(
                "SELECT count(*) FROM pg_stat_activity WHERE application_name=$1",
                "lightrag_acceptance_" + workspace,
            )
            if remaining == 0:
                break
            await asyncio.sleep(0.01)
        assert remaining == 0, "Old business PG sessions have not finished"
    finally:
        await audit_connection.close()
    # Rebuilt coordinator, not an old pool or shared Manager. All clients stopped;
    # fault seam is AFTER completed HTTP response, so no old HG request remains.
    co = PostgresCoordinator(
        os.environ["LIGHTRAG_COORDINATION_DSN"], "local_debug_process_tests", workspace
    )
    await co.initialize(read_only=True)
    snapshot = await co.inspect()
    assert snapshot["fenced"] and snapshot["generation"] == state["generation"]
    await start("denied", expected_error=True)
    await co.close()
    proc = await asyncio.create_subprocess_exec(
        sys.executable,
        "-m",
        "lightrag.distributed",
        "recover",
        "--deployment-id",
        "local_debug_process_tests",
        "--workspace",
        workspace,
        "--expected-generation",
        str(snapshot["generation"]),
        "--actor",
        "process-acceptance",
        "--reason",
        "All owned writers stopped; completed HTTP seam and retained anchors audited; purge replay selected",
        "--confirm-writers-stopped",
        "--confirm-inflight-finished",
        "--confirm-state-audited",
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    out, err = await proc.communicate()
    assert proc.returncode == 0, err.decode()
    recovered = json.loads(out)
    assert recovered["generation"] == snapshot["generation"] + 1
    c = await start("c")
    result = await c.call("purge", id="fault")
    assert "result" in result, result
    final = (await c.call("audit", ids=["fault"]))["result"]
    assert final["node"] is None and final["edge"] is None
    assert final["anchors"]["fault"] is None
    assert not final["status"]
    history = (await c.call("inspect"))["result"]
    assert history["recovery_audit"] and not history["fenced"]


async def test_paused_retry_commit_before_response_survives_acceptor_death(processes):
    a, b, _, root, _ = processes
    await a.call("enqueue", text="Atlas retry commit boundary.", ids=["failed"])
    assert "result" in await a.call("process", mode="fail_llm")
    initial = await wait_status(a, ["failed"], DocStatus.FAILED)
    version = initial["status"]["failed"].updated_at
    await b.call("pause")
    a.pipe.send(
        {"action": "retry", "id": "commit-before-response", "stop_after_commit": True}
    )
    await file_exists(root / "a.retry-committed")
    assert not a.pipe.poll(), (
        "SDK completion must not have reached the accepting caller"
    )
    os.kill(a.process.pid, signal.SIGKILL)
    await asyncio.to_thread(a.process.join, 10)
    try:
        os.kill(a.identity["manager"], signal.SIGTERM)
    except ProcessLookupError:
        pass
    state = (await b.call("control_status"))["result"]
    assert state["pending_retries"] == 1
    assert state["paused"] is False
    assert not state["recovery_required"]
    # This is the real polling scheduler, not the explicit process/resume API.
    assert "result" in await b.call("poll_once", mode="fail_llm")
    retried = await wait_status(b, ["failed"], DocStatus.FAILED)
    assert retried["status"]["failed"].updated_at != version
    calls = retried["calls"]
    version = retried["status"]["failed"].updated_at
    assert calls > 0
    assert (await b.call("control_status"))["result"]["pending_retries"] == 0
    # Completed-request delivery is not another resume or another attempt.
    await b.call("pause")
    await b.call("retry", id="commit-before-response")
    await b.call("poll_once", mode="fail_llm")
    replay = (await b.call("audit", ids=["failed"]))["result"]
    assert replay["calls"] == calls
    assert replay["status"]["failed"].updated_at == version
    assert (await b.call("control_status"))["result"]["paused"] is True
