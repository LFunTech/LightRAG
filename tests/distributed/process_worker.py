"""Spawn-only real backend harness; deterministic models and faults stay in tests."""

import asyncio
import json
import os
import time
from pathlib import Path
from contextlib import asynccontextmanager

import numpy as np


async def embedding(texts, **kwargs):
    return np.array([[1.0, 0.0, 0.0] for _ in texts], dtype=np.float32)


def worker(connection, config):
    # Each spawned interpreter creates its own Manager and asyncpg transport.
    from lightrag.kg import shared_storage

    shared_storage.initialize_share_data(2)
    try:
        asyncio.run(serve(connection, config, shared_storage))
    finally:
        shared_storage.finalize_share_data()
        connection.close()


async def serve(connection, config, shared_storage):
    from lightrag import LightRAG
    from lightrag.utils import EmbeddingFunc, compute_mdhash_id
    from tests.kg.hugegraph_impl.test_integration import _llm

    os.environ["POSTGRES_SERVER_SETTINGS"] = (
        "search_path=public&application_name=lightrag_acceptance_" + config["workspace"]
    )
    root = Path(config["root"])
    label = config["label"]
    state = {"mode": "", "calls": 0, "parsed": []}

    async def llm(*args, **kwargs):
        state["calls"] += 1
        if state["mode"] == "fail_llm":
            raise ValueError("deterministic extraction failure")
        if state["mode"] == "shared" and "<|#|>" in (kwargs.get("system_prompt") or ""):
            await asyncio.to_thread(config["barrier"].wait, 10)
        result = await _llm(*args, **kwargs)
        if state["mode"] == "independent":
            result = result.replace("Atlas", "Atlas" + label).replace(
                "Borealis", "Borealis" + label
            )
        return result

    rag = LightRAG(
        working_dir=str(root / "working"),
        distributed_input_dir=str(root / "inputs"),
        workspace=config["workspace"],
        distributed_writes=True,
        kv_storage="PGKVStorage",
        vector_storage="PGVectorStorage",
        doc_status_storage="PGDocStatusStorage",
        graph_storage="HugeGraphStorage",
        llm_model_func=llm,
        embedding_func=EmbeddingFunc(
            embedding_dim=3, func=embedding, model_name="runtime_test"
        ),
        max_parallel_insert=1,
        entity_extract_max_gleaning=0,
        entity_extraction_use_json=False,
    )
    rag.pipeline_scheduling_page_size = 1
    rt = rag._distributed_runtime
    rt.coordinator.wait_timeout = 0.4
    try:
        if config.get("bootstrap"):
            async with rag.distributed_maintenance():
                await rag.initialize_storages()
                await rag.check_and_migrate_data()
        else:
            await rag.initialize_storages()
    except Exception as exc:
        connection.send({"error": type(exc).__name__})
        await rt.close()
        return

    import lightrag.pipeline as pipeline

    original_get_parser = pipeline.get_parser

    def get_parser(*args, **kwargs):
        parser = original_get_parser(*args, **kwargs)
        if parser is None:
            return None

        class ObservedParser:
            async def parse(self, context):
                state["parsed"].append(context.doc_id)
                return await parser.parse(context)

        return ObservedParser()

    pipeline.get_parser = get_parser
    connection.send(
        {
            "ready": os.getpid(),
            "manager": shared_storage._manager._process.pid,
            "owner": str(rt.coordinator._owner_id),
        }
    )

    async def wait_file(path):
        for _ in range(1000):
            if path.exists():
                return
            await asyncio.sleep(0.01)
        raise TimeoutError(str(path.name))

    while True:
        cmd = await asyncio.to_thread(connection.recv)
        action = cmd["action"]
        try:
            if action == "close":
                await rag.finalize_storages()
                connection.send({"closed": True})
                return
            if action == "stop_transport":
                await rt.close()
                connection.send({"closed": True})
                return
            if action == "enqueue":
                await rag.apipeline_enqueue_documents(
                    cmd["text"],
                    ids=cmd["ids"],
                    file_paths=[f"{i}.txt" for i in cmd["ids"]],
                )
                result = True
            elif action == "process":
                state["mode"] = cmd.get("mode", "")
                original = rag.chunk_entity_relation_graph._client.request
                instrumented = False
                active_requests = 0
                freeze_requests = False

                async def tracked_request(*args, **kwargs):
                    nonlocal active_requests
                    active_requests += 1
                    try:
                        return await original(*args, **kwargs)
                    finally:
                        active_requests -= 1

                async def request(method, path, **kwargs):
                    nonlocal instrumented, freeze_requests
                    if freeze_requests:
                        await wait_file(root / "never-release")
                    if (
                        method == "POST"
                        and path.endswith("/graph/vertices/batch")
                        and not instrumented
                        and any(
                            "Atlas" in json.dumps(record)
                            for record in kwargs.get("json", [])
                        )
                    ):
                        instrumented = True
                        if cmd.get("barrier"):
                            (root / f"{label}.ready").touch()
                            await wait_file(root / "release")
                            await asyncio.to_thread(config["barrier"].wait, 10)
                        start = time.time_ns()
                        response = await tracked_request(method, path, **kwargs)
                        end = time.time_ns()
                        if cmd.get("fault"):
                            freeze_requests = True
                            while active_requests:
                                await asyncio.sleep(0.001)
                        (root / f"{label}.commit").write_text(
                            json.dumps({"start": start, "end": end})
                        )
                        if cmd.get("fault") == "kill":
                            await wait_file(root / "never-release")
                        if cmd.get("fault") == "ack_loss":
                            raise ConnectionResetError(
                                "HugeGraph committed; client ACK deliberately lost"
                            )
                        return response
                    return await tracked_request(method, path, **kwargs)

                rag.chunk_entity_relation_graph._client.request = request
                try:
                    await rag.apipeline_process_enqueue_documents()
                finally:
                    rag.chunk_entity_relation_graph._client.request = original
                result = {"calls": state["calls"]}
            elif action == "poll":
                state["mode"] = cmd.get("mode", "")
                await rag.apipeline_start_polling(interval=0.02)
                result = True
            elif action == "stop_poll":
                await rag.apipeline_stop_polling()
                result = {"calls": state["calls"]}
            elif action == "poll_once":
                from lightrag.distributed.pipeline import process

                state["mode"] = cmd.get("mode", "")
                await process(rag, resume=False)
                result = {"calls": state["calls"]}
            elif action == "retry":
                if cmd.get("stop_after_commit"):
                    original_transaction = rt.coordinator._transaction

                    @asynccontextmanager
                    async def stop_after_commit(*args, **kwargs):
                        async with original_transaction(*args, **kwargs) as transaction:
                            yield transaction
                        # Stop immediately after the FIRST real commit, before
                        # control/SDK/pipe completion or any second transaction.
                        (root / f"{label}.retry-committed").touch()
                        await asyncio.Event().wait()

                    rt.coordinator._transaction = stop_after_commit
                result = await rag.apipeline_request_retry(cmd["id"])
            elif action == "control_status":
                result = await rt.coordinator.pipeline_control.status()
            elif action == "pause":
                await rt.coordinator.pipeline_control.pause()
                result = True
            elif action == "resume":
                await rt.coordinator.pipeline_control.resume()
                result = True
            elif action == "maintenance":
                async with rag.distributed_maintenance():
                    # Marker proves there were no business/file mutations before admission.
                    (root / "maintenance-admitted").touch()
                result = True
            elif action == "inspect":
                result = await rt.coordinator.inspect()
            elif action == "audit":
                graph = rag.chunk_entity_relation_graph
                result = {
                    "node": await graph.get_node(cmd.get("name", "Atlas")),
                    "edge": await graph.get_edge("Atlas", "Borealis"),
                    "tracking": await rag.entity_chunks.get_by_id("Atlas"),
                    "anchors": {
                        i: await rag.full_entities.get_by_id(i) for i in cmd["ids"]
                    },
                    "relation_anchors": {
                        i: await rag.full_relations.get_by_id(i) for i in cmd["ids"]
                    },
                    "status": await rag.doc_status.get_full_docs_by_ids(
                        cmd["ids"], strict=True
                    ),
                    "vector": await rag.entities_vdb.get_by_id(
                        compute_mdhash_id("Atlas", prefix="ent-")
                    ),
                    "calls": state["calls"],
                    "parsed": list(state["parsed"]),
                }
            elif action == "purge":
                result = await rag.adelete_by_doc_id(cmd["id"])
            elif action == "new_write":
                await rag.acreate_entity("Denied", {"description": "must not commit"})
                result = True
            else:
                raise ValueError(action)
            connection.send({"result": result})
        except BaseException as exc:
            connection.send({"error": type(exc).__name__, "message": str(exc)[:160]})
