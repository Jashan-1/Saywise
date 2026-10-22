import os

# Tests never touch SAYSO_DATABASE_URL from .env (it may point at Supabase).
os.environ["SAYSO_DATABASE_URL"] = os.environ.get(
    "SAYSO_TEST_DATABASE_URL", "postgresql+asyncpg://sayso:sayso@localhost:5433/sayso_test"
)
os.environ.setdefault("SAYSO_SIGNING_SECRET", "test-signing-secret-0123456789abcdef")

from collections.abc import AsyncIterator  # noqa: E402

import httpx  # noqa: E402
import pytest  # noqa: E402

from app.main import create_app  # noqa: E402


@pytest.fixture
async def client() -> AsyncIterator[httpx.AsyncClient]:
    transport = httpx.ASGITransport(app=create_app())
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        yield c
