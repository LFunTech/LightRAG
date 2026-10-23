import os
from urllib.parse import urlsplit, urlunsplit
from uuid import uuid4

import pytest
import pytest_asyncio


@pytest_asyncio.fixture
async def migration_db():
    import asyncpg

    dsn = os.getenv("LIGHTRAG_PROVISIONING_TEST_DSN", "")
    target = urlsplit(dsn)
    if target.hostname not in {"127.0.0.1", "localhost"} or not target.path.startswith(
        "/migration_test_"
    ):
        pytest.skip("explicit isolated loopback migration_test_* database required")
    admin = await asyncpg.connect(dsn)
    name = "migration_test_" + uuid4().hex[:12]
    await admin.execute('CREATE DATABASE "' + name + '"')
    await admin.close()
    conn = await asyncpg.connect(urlunsplit(target._replace(path="/" + name)))
    try:
        yield conn
    finally:
        await conn.close()
