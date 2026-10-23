import asyncio

import httpx
from fastapi import FastAPI, Header, HTTPException
import pytest

from .test_runtime_integration import make_rag, setup_scope

pytestmark = [pytest.mark.integration, pytest.mark.requires_db]


class Auth:
    accounts = {}


async def app_for(db):
    from lightrag.api.release_commands import ReleaseHTTPBridge
    from lightrag.api.release_lifecycle import ReleaseMiddleware

    dep, work = await setup_scope(db)
    rag = await make_rag(db, deployment=dep, workspace=work, instance="pod-1")
    app = FastAPI()

    @app.post("/graph/entity/create")
    async def create_entity(payload: dict, x_api_key: str = Header(default="")):
        if x_api_key != "synthetic-private-key":
            raise HTTPException(401)
        return await rag.acreate_entity(payload["name"], {"description": "value"})

    @app.post("/query")
    async def query_text(payload: dict):
        return {"answer": payload["query"]}

    bridge = ReleaseHTTPBridge(
        app,
        rag._distributed_runtime.release,
        api_key="synthetic-private-key",
        auth_handler=Auth(),
    )
    bridge.install()
    app.add_middleware(ReleaseMiddleware, bridge=bridge)
    await rag._distributed_runtime.release.start()
    return app, rag, dep, work


async def test_original_http_response_and_idempotency_survive_queue(release_db):
    app, rag, dep, work = await app_for(release_db)
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app), base_url="http://local"
        ) as client:
            denied = await client.post("/graph/entity/create", json={"name": "denied"})
            assert denied.status_code == 401
            headers = {"X-API-Key": "synthetic-private-key", "Idempotency-Key": "same"}
            first = await client.post(
                "/graph/entity/create", json={"name": "one"}, headers=headers
            )
            second = await client.post(
                "/graph/entity/create", json={"name": "one"}, headers=headers
            )
        assert first.status_code == second.status_code == 200
        assert (
            first.json()
            == second.json()
            == {"entity_name": "one", "entity_data": {"description": "value"}}
        )
        assert (
            await release_db.fetchval(
                "SELECT count(*) FROM public.release_test_effects WHERE scope=$1",
                dep + "/" + work,
            )
            == 1
        )
        raw = await release_db.fetchval(
            "SELECT envelope::text FROM lightrag_release.commands WHERE scope=$1 LIMIT 1",
            dep + "/" + work,
        )
        assert "synthetic-private-key" not in raw
    finally:
        await rag.finalize_storages()


async def test_query_does_not_wait_for_closed_gate_but_write_does(release_db):
    from lightrag.distributed.release.store import ReleaseStore
    from lightrag.distributed.release.models import ReleaseIdentity

    app, rag, dep, work = await app_for(release_db)
    store = ReleaseStore(release_db)
    identity = ReleaseIdentity(
        dep + "/" + work, "r1", "v1.2.3", "c" * 40, "sha256:" + "d" * 64, "e" * 64
    )
    await store.begin(identity, expected_epoch=1)
    await store.transition(
        identity, expected_epoch=1, expected_phase="PREPARED", next_phase="DRAINING"
    )
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app), base_url="http://local"
        ) as client:
            write = asyncio.create_task(
                client.post(
                    "/graph/entity/create",
                    json={"name": "queued"},
                    headers={"X-API-Key": "synthetic-private-key"},
                )
            )
            await asyncio.sleep(0.05)
            query = await asyncio.wait_for(
                client.post("/query", json={"query": "read still works"}), 0.5
            )
            assert (
                query.status_code == 200
                and query.json()["answer"] == "read still works"
            )
            assert not write.done()
            await release_db.execute(
                "UPDATE lightrag_release.scopes SET gate_open=true,phase='ABORTED',owner=NULL WHERE scope=$1",
                dep + "/" + work,
            )
            assert (await asyncio.wait_for(write, 1)).status_code == 200
    finally:
        await rag.finalize_storages()


async def test_readiness_is_not_liveness_and_command_status_is_principal_scoped(
    release_db,
):
    from lightrag.api.release_commands import register_release_endpoints
    from lightrag.distributed.release.runtime import request_context

    app, rag, dep, work = await app_for(release_db)
    bridge = rag._distributed_runtime.release.http_bridge

    @app.get("/health")
    async def health():
        return {"status": "healthy"}

    register_release_endpoints(app, bridge)
    try:
        with request_context(service_id="sdk-test", idempotency_key="private-sdk"):
            await rag.acreate_entity("sdk-private", {})
        cid = await release_db.fetchval(
            "SELECT id FROM lightrag_release.commands WHERE scope=$1 AND principal_id='sdk-test'",
            dep + "/" + work,
        )
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app), base_url="http://local"
        ) as client:
            assert (await client.get("/release/status")).status_code == 401
            authorized = {"X-API-Key": "synthetic-private-key"}
            assert (
                await client.get("/release/status", headers=authorized)
            ).status_code == 200
            assert (
                await client.get("/release/commands/" + str(cid), headers=authorized)
            ).status_code == 404
            rag._distributed_runtime.coordinator._broken = True
            assert (await client.get("/health")).status_code == 200
            ready = await client.get("/ready")
            assert ready.status_code == 503
            assert ready.json() == {"ready": False, "reason": "CoordinatorUnavailable"}
            rag._distributed_runtime.coordinator._broken = False
    finally:
        rag._distributed_runtime.coordinator._broken = False
        await rag.finalize_storages()
