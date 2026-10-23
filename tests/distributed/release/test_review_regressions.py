import asyncio
from uuid import uuid4

import pytest

from .test_runtime_integration import make_rag, setup_scope

pytestmark = [pytest.mark.integration, pytest.mark.requires_db]


async def test_detached_background_owns_an_independent_ticket_after_command_finishes(
    release_db,
):
    from lightrag.api.distributed import background_operation
    from lightrag.distributed.release.commands import CommandRegistry

    dep, work = await setup_scope(release_db)
    rag = await make_rag(release_db, deployment=dep, workspace=work, instance="blue-1")
    release = rag._distributed_runtime.release
    ready, proceed = asyncio.Event(), asyncio.Event()

    @background_operation(exclusive=True)
    async def background_delete_documents(rag):
        await proceed.wait()
        await rag.storage.upsert("confirmed-background-effect")

    registry = CommandRegistry()
    registry.register("http.delete", version=1, handler=lambda payload: payload)
    receipt = await release.store.enqueue(
        registry.encode(
            "http.delete",
            {},
            principal={"id": "u"},
            scope=dep + "/" + work,
            key=uuid4().hex,
        )
    )
    command = await release.store.claim_next(epoch=1)
    task = None
    try:
        async with release.use_command(command):
            task = asyncio.create_task(
                background_delete_documents(rag, _distributed_started=ready)
            )
            await asyncio.wait_for(ready.wait(), 1)
        child = await release_db.fetchrow(
            "SELECT id,command_id FROM lightrag_release.tickets WHERE scope=$1 AND id<>$2 AND state='ACTIVE'",
            dep + "/" + work,
            command["ticket_id"],
        )
        assert child is not None and child["command_id"] == receipt["id"]
        await release.store.finish_command(
            receipt["id"], status="SUCCEEDED", result={"accepted": True}
        )
        proceed.set()
        await task
        assert (
            await release_db.fetchval(
                "SELECT count(*) FROM public.release_test_effects WHERE scope=$1",
                dep + "/" + work,
            )
            == 1
        )
    finally:
        proceed.set()
        if task and not task.done():
            await asyncio.gather(task, return_exceptions=True)
        await rag._distributed_runtime.close()


async def test_real_text_handler_waits_for_durable_enqueue_before_reporting_success(
    release_db, monkeypatch
):
    import sys

    monkeypatch.setattr(sys, "argv", ["lightrag-server"])
    from types import SimpleNamespace, MethodType
    from fastapi import FastAPI
    import httpx
    from lightrag.api.routers import document_routes as routes
    from lightrag.api.release_commands import install_release_api
    from lightrag.distributed.runtime import operation_guard
    from lightrag.chunker import chunking_by_token_size
    from .test_authorization import Auth

    dep, work = await setup_scope(release_db)
    rag = await make_rag(release_db, deployment=dep, workspace=work, instance="blue-1")
    entered, accept = asyncio.Event(), asyncio.Event()

    class Status:
        async def get_doc_by_file_basename(self, name):
            return None

    @operation_guard()
    async def apipeline_enqueue_documents(self, **kwargs):
        entered.set()
        await accept.wait()
        await self.storage.upsert(kwargs["track_id"])
        return kwargs["track_id"]

    async def noop(*args, **kwargs):
        return None

    rag.apipeline_enqueue_documents = MethodType(apipeline_enqueue_documents, rag)
    rag.doc_status = Status()
    rag.workspace = work
    rag.addon_params = {}
    rag.chunking_func = chunking_by_token_size
    for name in ["_reserve_enqueue_slot", "_release_enqueue_slot", "drive_pipeline"]:
        monkeypatch.setattr(routes, name, noop)
    app = FastAPI()
    app.state.background_tasks = set()
    app.include_router(
        routes.create_document_routes(
            rag, SimpleNamespace(), api_key="synthetic-private"
        )
    )
    release = rag._distributed_runtime.release
    install_release_api(app, release, api_key="synthetic-private", auth_handler=Auth())
    await release.start()
    request = None
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://fixture"
        ) as client:
            request = asyncio.create_task(
                client.post(
                    "/documents/text",
                    json={"text": "synthetic document", "file_source": "source.txt"},
                    headers={
                        "X-API-Key": "synthetic-private",
                        "Idempotency-Key": "accepted-one",
                    },
                )
            )
            await asyncio.wait_for(entered.wait(), 2)
            await asyncio.sleep(0.05)
            assert not request.done(), (
                "HTTP reported success before the original doc-status acceptance point"
            )
            assert (
                await release_db.fetchval(
                    "SELECT state FROM lightrag_release.commands WHERE scope=$1",
                    dep + "/" + work,
                )
                == "EXECUTING"
            )
            accept.set()
            response = await asyncio.wait_for(request, 2)
            assert response.status_code == 200, response.text
            assert (
                await release_db.fetchval(
                    "SELECT count(*) FROM public.release_test_effects WHERE scope=$1",
                    dep + "/" + work,
                )
                == 1
            )
        await asyncio.gather(*app.state.background_tasks)
    finally:
        accept.set()
        if request:
            await asyncio.gather(request, return_exceptions=True)
        await asyncio.gather(*app.state.background_tasks, return_exceptions=True)
        await release.close_instance(lambda *args: noop(), rag, (), {})


async def test_route_proof_rejects_post_transfer_candidate_image_drift(
    release_db, tmp_path, monkeypatch
):
    import json
    from types import SimpleNamespace
    from .test_handover_integration import prepare
    from tests.ci.test_prod_config import config_data, load
    from lightrag.distributed.release.transport import ProductionTransport
    from lightrag.distributed.release.models import ReleaseConflict

    store, identity, blue, green = await prepare(release_db)
    await store.transfer_writer(
        identity, expected_epoch=1, expected_instances=["green-1"]
    )
    data = config_data()
    data["replicas"] = 4
    data["target"]["deployment_id"] = identity.scope.split("/")[0]
    data["target"]["workspace"] = identity.scope.split("/")[1]
    data["onboarding"]["profile_hash"] = "a" * 64
    config = load(tmp_path, data)
    # Four otherwise healthy participants report the intended profile/config,
    # but one Pod actually runs a different digest after the transfer.
    statuses = {
        f"green-{i}": {
            "instance_id": f"green-{i}",
            "slot": "green",
            "profile_hash": "a" * 64,
            "config_hash": identity.config_hash,
            "protocol_version": 1,
            "shutdown_ready": True,
            "readiness": {"ready": True},
            "command_formats_hash": "formats",
            "authorization_version": "auth",
        }
        for i in range(1, 5)
    }
    pods = [
        {
            "metadata": {"uid": uid},
            "spec": {
                "containers": [
                    {
                        "image": "registry.invalid/rag@"
                        + (identity.digest if i else "sha256:" + "f" * 64)
                    }
                ]
            },
            "status": {
                "podIP": f"10.0.0.{i + 1}",
                "containerStatuses": [
                    {
                        "ready": True,
                        "imageID": "registry.invalid/rag@"
                        + (identity.digest if i else "sha256:" + "f" * 64),
                    }
                ],
            },
        }
        for i, uid in enumerate(statuses)
    ]

    class Kube:
        def get(self, kind, name):
            return {
                "spec": {
                    "selector": {
                        "lightrag.io/deployment": config.target["deployment_id"],
                        "lightrag.io/slot": "green",
                    }
                }
            }

        def list(self, kind, selector):
            if kind == "Pod":
                return pods
            return [
                {
                    "endpoints": [
                        {
                            "targetRef": {"uid": p["metadata"]["uid"]},
                            "conditions": {"ready": True},
                            "addresses": [p["status"]["podIP"]],
                        }
                        for p in pods
                    ]
                }
            ]

    cloud_calls = []

    class Cloud:
        def observe(self, *args, **kwargs):
            cloud_calls.append(True)
            return {"provider": "qcloud"}

    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def read(self, *args):
            return json.dumps(statuses["green-1"]).encode()

    monkeypatch.setattr(
        "urllib.request.build_opener",
        lambda *args: SimpleNamespace(open=lambda *args, **kwargs: Response()),
    )
    transport = ProductionTransport(
        config=config,
        kubernetes=Kube(),
        cloud=Cloud(),
        store=store,
        image="registry.invalid/rag",
        environment={"LIGHTRAG_API_KEY": "synthetic"},
    )

    async def status(pod):
        return statuses[pod["metadata"]["uid"]]

    transport.local_status = status
    try:
        with pytest.raises(ReleaseConflict):
            await transport.verify_route(identity, "green")
        assert not cloud_calls
        assert (await store.snapshot(identity.scope))["phase"] == "WRITER_SWITCHED"
    finally:
        await blue.finalize_storages()
        await green.finalize_storages()
