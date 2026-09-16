"""Real existing pipeline and PG/HugeGraph writes, deterministic local LLM only."""

import asyncio
import pytest
from tests.distributed.test_runtime_integration import runtime_rags as _runtime_rags
from tests.kg.hugegraph_impl.test_integration import _llm
from lightrag.base import DocStatus
from lightrag.constants import GRAPH_FIELD_SEP

runtime_rags = _runtime_rags

pytestmark = pytest.mark.integration


async def configure(rags):
    for rag in rags:
        rag.max_parallel_insert = 1
        rag.pipeline_scheduling_page_size = 1
        rag.entity_extract_max_gleaning = 0
        rag.entity_extraction_use_json = False
        for role in ["extract"]:
            await rag.aupdate_llm_role_config(role, model_func=_llm)


async def test_real_pipeline_parallel_documents_and_shared_evidence(runtime_rags):
    rag, peer = runtime_rags
    await configure(runtime_rags)
    await rag.apipeline_enqueue_documents(
        [
            "Atlas cooperates with Borealis on project one.",
            "Atlas cooperates with Borealis on project two.",
        ],
        ids=["one", "two"],
        file_paths=["one.txt", "two.txt"],
    )
    await asyncio.wait_for(
        asyncio.gather(
            rag.apipeline_process_enqueue_documents(),
            peer.apipeline_process_enqueue_documents(),
        ),
        30,
    )
    rows = await rag.doc_status.get_full_docs_by_ids(["one", "two"], strict=True)
    assert {row.status for row in rows.values()} == {DocStatus.PROCESSED}
    graph = rag.chunk_entity_relation_graph
    node = await graph.get_node("Atlas")
    edge = await graph.get_edge("Atlas", "Borealis")
    assert len(set(node["source_id"].split(GRAPH_FIELD_SEP))) == 2
    assert len(set(edge["source_id"].split(GRAPH_FIELD_SEP))) == 2
    assert edge["weight"] >= 2
    state = await rag._distributed_runtime.coordinator.inspect()
    assert not state["claims"]
    assert not state["fenced"]


async def test_polling_finds_lost_notification_but_pause_does_not_resume(runtime_rags):
    rag, peer = runtime_rags
    await configure(runtime_rags)
    await peer._distributed_runtime.coordinator.pipeline_control.pause()
    await rag.apipeline_enqueue_documents(
        "Atlas cooperates with Borealis.", ids=["lost"], file_paths=["lost.txt"]
    )
    await peer.apipeline_start_polling(interval=0.02)
    await asyncio.sleep(0.1)
    row = await rag.doc_status.get_by_id("lost")
    assert row["status"] == DocStatus.PENDING
    await rag._distributed_runtime.coordinator.pipeline_control.resume()
    for _ in range(200):
        rows = await rag.doc_status.get_full_docs_by_ids(["lost"], strict=True)
        if rows["lost"].status == DocStatus.PROCESSED:
            break
        await asyncio.sleep(0.02)
    await peer.apipeline_stop_polling()
    assert rows["lost"].status == DocStatus.PROCESSED


async def test_background_upload_cannot_undo_durable_pause(
    runtime_rags, tmp_path, monkeypatch
):
    import sys

    monkeypatch.setattr(sys, "argv", ["test"])
    from lightrag.api.routers.document_routes import pipeline_index_file

    rag, peer = runtime_rags
    await configure(runtime_rags)
    await rag._distributed_runtime.coordinator.pipeline_control.pause()
    source = tmp_path / "paused.txt"
    source.write_text("Atlas cooperates with Borealis while paused.")
    await pipeline_index_file(rag, source)
    assert (await peer._distributed_runtime.coordinator.pipeline_control.status())[
        "paused"
    ] is True
    rows = await rag.doc_status.get_docs_by_statuses([DocStatus.PENDING], strict=True)
    assert len(rows) == 1


async def test_retry_replay_skips_a_new_failure_version(runtime_rags):
    from lightrag.distributed.pipeline import reset_requests

    rag, peer = runtime_rags
    await configure(runtime_rags)
    await rag.apipeline_enqueue_documents(
        "Retry version.", ids=["retry"], file_paths=["retry.txt"]
    )
    rt = rag._distributed_runtime
    async with rt.operation("seed"):
        await rag.doc_status.update_doc_status_fields(
            "retry",
            {"status": DocStatus.FAILED, "updated_at": "2026-01-01T00:00:00+00:00"},
        )
    await rag.apipeline_request_retry("request")
    control = rt.coordinator.pipeline_control
    async with rt.operation("interrupted_reset", exclusive=True) as op:
        rows = await rag.doc_status.get_full_docs_by_ids(["retry"], strict=True)
        old = rows["retry"]
        await control.add_targets(op, "request", {"retry": str(old.updated_at)})
        await control.finish_selection(op, "request")
        update, _, _ = rag._build_pending_reset_update(
            old, await rag.full_docs.get_by_id_strict("retry")
        )
        await rag.doc_status.upsert({"retry": update})
        # Simulates a retry completing/failing before durable target ACK.
        await rag.doc_status.update_doc_status_fields(
            "retry", {"status": DocStatus.FAILED}
        )
    before = await rag.doc_status.get_full_docs_by_ids(["retry"], strict=True)
    await reset_requests(peer)
    after = await rag.doc_status.get_full_docs_by_ids(["retry"], strict=True)
    assert after["retry"].status == DocStatus.FAILED
    assert after["retry"].updated_at == before["retry"].updated_at
    assert (await control.status())["pending_retries"] == 0


async def test_manual_retry_selection_uses_application_cutoff_not_db_clock(
    runtime_rags,
):
    from datetime import timedelta

    from lightrag.distributed.pipeline import reset_requests

    rag, peer = runtime_rags
    await configure(runtime_rags)
    await rag.apipeline_enqueue_documents(
        "Retry clock skew.", ids=["retry-clock"], file_paths=["retry-clock.txt"]
    )
    rt = rag._distributed_runtime
    async with rt.operation("seed-clock-skew-failure"):
        # The target DB may be behind the app nodes. This row is deliberately
        # newer than the request's DB-side created_at, but still existed before
        # the app published the retry request.
        db_now = await rt.coordinator._pool.fetchval("SELECT clock_timestamp()")
        failed_at = db_now + timedelta(seconds=5)
        cutoff_at = failed_at + timedelta(seconds=5)
        await rag.doc_status.update_doc_status_fields(
            "retry-clock",
            {
                "status": DocStatus.FAILED,
                "updated_at": failed_at.isoformat(),
            },
        )
    control = rt.coordinator.pipeline_control
    await control.request_retry("clock-cutoff", target_cutoff_at=cutoff_at)
    await reset_requests(peer)
    after = await rag.doc_status.get_full_docs_by_ids(["retry-clock"], strict=True)
    assert after["retry-clock"].status == DocStatus.PENDING
    assert (await control.status())["pending_retries"] == 0


async def test_global_cancel_reaches_two_active_processing_owners(runtime_rags):
    rag, peer = runtime_rags
    await configure(runtime_rags)
    entered = [asyncio.Event(), asyncio.Event()]
    release = asyncio.Event()
    for index, instance in enumerate(runtime_rags):

        async def blocked_llm(*args, _index=index, **kwargs):
            entered[_index].set()
            await release.wait()
            return await _llm(*args, **kwargs)

        await instance.aupdate_llm_role_config("extract", model_func=blocked_llm)
    await rag.apipeline_enqueue_documents(
        ["Atlas works on first cancellation.", "Atlas works on second cancellation."],
        ids=["cancel-one", "cancel-two"],
        file_paths=["cancel-one.txt", "cancel-two.txt"],
    )
    tasks = [
        asyncio.create_task(instance.apipeline_process_enqueue_documents())
        for instance in runtime_rags
    ]
    await asyncio.wait_for(asyncio.gather(*(event.wait() for event in entered)), 10)
    await peer._distributed_runtime.coordinator.pipeline_control.pause()
    release.set()
    await asyncio.wait_for(asyncio.gather(*tasks), 15)
    rows = await rag.doc_status.get_full_docs_by_ids(
        ["cancel-one", "cancel-two"], strict=True
    )
    assert {row.status for row in rows.values()} == {DocStatus.FAILED}
    assert (await peer._distributed_runtime.coordinator.pipeline_control.status())[
        "paused"
    ]
    assert not (await peer._distributed_runtime.coordinator.inspect())["fenced"]
    await rag.apipeline_start_polling(interval=0.01)
    await asyncio.sleep(0.03)
    await rag.apipeline_stop_polling()
    await rag.apipeline_start_polling(interval=0.01)
    await asyncio.sleep(0.03)
    await rag.apipeline_stop_polling()
    rows = await rag.doc_status.get_full_docs_by_ids(
        ["cancel-one", "cancel-two"], strict=True
    )
    assert {row.status for row in rows.values()} == {DocStatus.FAILED}


async def test_claimed_pipeline_never_uses_best_effort_full_doc_reads(
    runtime_rags, monkeypatch
):
    rag, _ = runtime_rags
    await configure(runtime_rags)
    await rag.apipeline_enqueue_documents(
        "Atlas cooperates with Borealis strictly.",
        ids=["strict"],
        file_paths=["strict.txt"],
    )

    async def forbidden(*args, **kwargs):
        raise AssertionError("Claimed pipeline used a best-effort point read")

    monkeypatch.setattr(rag.full_docs, "get_by_id_strict", rag.full_docs.get_by_id)
    monkeypatch.setattr(rag.full_docs, "get_by_id", forbidden)
    await rag.apipeline_process_enqueue_documents()
    rows = await rag.doc_status.get_full_docs_by_ids(["strict"], strict=True)
    assert rows["strict"].status == DocStatus.PROCESSED


async def test_duplicate_enqueue_cannot_overwrite_a_claimed_document(runtime_rags):
    rag, peer = runtime_rags
    await configure(runtime_rags)
    original = "Atlas cooperates with Borealis with original evidence."
    await rag.apipeline_enqueue_documents(
        original, ids=["owned"], file_paths=["owned.txt"]
    )
    entered, release = asyncio.Event(), asyncio.Event()
    run = rag._run_pipeline_batch

    async def blocked(*args, **kwargs):
        entered.set()
        await release.wait()
        await run(*args, **kwargs)

    rag._run_pipeline_batch = blocked
    processing = asyncio.create_task(rag.apipeline_process_enqueue_documents())
    await entered.wait()
    await peer.apipeline_enqueue_documents(
        "A replacement must not land.", ids=["owned"], file_paths=["owned.txt"]
    )
    assert (await peer.full_docs.get_by_id_strict("owned"))["content"] == original
    release.set()
    await processing
    rows = await peer.doc_status.get_full_docs_by_ids(["owned"], strict=True)
    assert rows["owned"].status == DocStatus.PROCESSED
    assert not (await rag._distributed_runtime.coordinator.inspect())["fenced"]


async def test_finalize_refusal_keeps_runtime_open_and_discovery_stopped(runtime_rags):
    from contextvars import Context
    from lightrag.distributed import CoordinationBusyError

    rag, peer = runtime_rags
    await rag._distributed_runtime.coordinator.pipeline_control.pause()
    await rag.apipeline_start_polling(interval=0.01)
    rag._distributed_runtime.coordinator.wait_timeout = 0.03
    async with peer._distributed_runtime.operation("peer_writer"):
        with pytest.raises(CoordinationBusyError):
            await Context().run(asyncio.create_task, rag.finalize_storages())
    runtime = rag._distributed_runtime
    assert not runtime.closed
    assert runtime.pipeline_stop.is_set()
    assert runtime.pipeline_task is None
    assert (await runtime.coordinator.pipeline_control.status())["paused"]
    await rag.apipeline_start_polling(interval=0.01)
    await asyncio.sleep(0.02)
    assert (await runtime.coordinator.pipeline_control.status())["paused"]
    await rag.apipeline_stop_polling()


async def test_error_enqueue_cannot_overwrite_claimed_custom_id(runtime_rags):
    from contextvars import Context
    from lightrag.utils import compute_mdhash_id

    rag, peer = runtime_rags
    doc_id = compute_mdhash_id("owned.txt-File extraction failed", prefix="error-")
    await rag.apipeline_enqueue_documents(
        "Owned document content.", ids=[doc_id], file_paths=["owned.txt"]
    )
    async with rag._distributed_runtime.operation("pipeline") as operation:
        assert await rag._distributed_runtime.coordinator.try_claim_document(
            operation, doc_id
        )
        await Context().run(
            asyncio.create_task,
            peer.apipeline_enqueue_error_documents([{"file_path": "owned.txt"}]),
        )
        row = await peer.doc_status.get_by_id_strict(doc_id)
        assert row["status"] == DocStatus.PENDING
        await rag._distributed_runtime.coordinator.release_claim(operation, doc_id)


async def test_analyze_worker_strict_content_failure_is_failed_not_skipped(
    runtime_rags, tmp_path, monkeypatch
):
    from dataclasses import replace
    import lightrag.pipeline as pipeline

    rag, _ = runtime_rags
    await configure(runtime_rags)
    blocks = tmp_path / "multimodal.blocks.jsonl"
    blocks.write_text('{"type":"text","text":"Atlas cooperates with Borealis."}\n')
    await rag.apipeline_enqueue_documents(
        "Atlas cooperates with Borealis.",
        ids=["multimodal"],
        file_paths=["multimodal.txt"],
        process_options="ite",
    )
    original_parser = pipeline.get_parser

    class WithSidecar:
        def __init__(self, parser):
            self.parser = parser

        def __getattr__(self, name):
            return getattr(self.parser, name)

        async def parse(self, context):
            # Keep the real parser and only attach an existing sidecar fixture.
            return replace(await self.parser.parse(context), blocks_path=str(blocks))

    monkeypatch.setattr(
        pipeline, "get_parser", lambda *a, **kw: WithSidecar(original_parser(*a, **kw))
    )
    original_read = rag.full_docs.get_by_id_strict
    failed = False

    async def strict_read(doc_id):
        nonlocal failed
        row = await rag.doc_status.get_by_id_strict(doc_id)
        if not failed and row and row["status"] == DocStatus.ANALYZING:
            failed = True
            raise OSError("strict multimodal content read failed")
        return await original_read(doc_id)

    monkeypatch.setattr(rag.full_docs, "get_by_id_strict", strict_read)
    await asyncio.wait_for(rag.apipeline_process_enqueue_documents(), 20)
    assert failed, "Fault must reach the real claimed analyze worker"
    row = await rag.doc_status.get_by_id_strict("multimodal")
    assert row["status"] == DocStatus.FAILED
    assert not row["metadata"].get("analyzing_stage_skipped")
    assert "strict multimodal content read failed" in row["error_msg"]
    assert await rag.chunk_entity_relation_graph.get_node("Atlas") is None


async def test_pending_retry_replay_and_poll_preserve_later_pause(runtime_rags):
    from lightrag.distributed.pipeline import process

    rag, peer = runtime_rags
    await configure(runtime_rags)
    await rag.apipeline_enqueue_documents(
        "Atlas cooperates with Borealis after explicit retry.",
        ids=["paused-retry"],
        file_paths=["paused-retry.txt"],
    )
    async with rag._distributed_runtime.operation("seed-failure"):
        await rag.doc_status.update_doc_status_fields(
            "paused-retry", {"status": DocStatus.FAILED}
        )
    before = await rag.doc_status.get_full_docs_by_ids(["paused-retry"], strict=True)
    await rag.apipeline_request_retry("pending-once")
    control = peer._distributed_runtime.coordinator.pipeline_control
    await control.pause()
    await rag.apipeline_request_retry("pending-once")
    await process(peer, resume=False)
    state = await control.status()
    after = await peer.doc_status.get_full_docs_by_ids(["paused-retry"], strict=True)
    assert state["paused"]
    assert state["pending_retries"] == 1
    assert after["paused-retry"].status == DocStatus.FAILED
    assert after["paused-retry"].updated_at == before["paused-retry"].updated_at
    # A new explicit process call, unlike replay or polling, may resume it.
    await peer.apipeline_process_enqueue_documents()
    after = await peer.doc_status.get_full_docs_by_ids(["paused-retry"], strict=True)
    assert after["paused-retry"].status == DocStatus.PROCESSED
    assert (await control.status())["pending_retries"] == 0
