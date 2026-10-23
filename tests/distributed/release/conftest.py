import os
from urllib.parse import urlsplit

import pytest
import pytest_asyncio


@pytest_asyncio.fixture
async def release_db():
    import asyncpg

    dsn = os.getenv("LIGHTRAG_RELEASE_TEST_DSN", "")
    target = urlsplit(dsn)
    if target.hostname not in {"127.0.0.1", "localhost"} or not target.path.startswith(
        "/release_test_"
    ):
        pytest.skip("explicit isolated loopback release_test_* database required")
    connection = await asyncpg.connect(dsn)
    try:
        yield connection
    finally:
        await connection.close()
