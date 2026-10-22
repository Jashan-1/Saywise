import os

# Tests never touch SAYSO_DATABASE_URL from .env (it may point at Supabase).
os.environ["SAYSO_DATABASE_URL"] = os.environ.get(
    "SAYSO_TEST_DATABASE_URL", "postgresql+asyncpg://sayso:sayso@localhost:5433/sayso_test"
)
os.environ.setdefault("SAYSO_SIGNING_SECRET", "test-signing-secret-0123456789abcdef")

import asyncio  # noqa: E402
from collections.abc import AsyncIterator, Awaitable, Callable  # noqa: E402
from dataclasses import dataclass  # noqa: E402
from pathlib import Path  # noqa: E402

import httpx  # noqa: E402
import pytest  # noqa: E402
from alembic import command  # noqa: E402
from alembic.config import Config  # noqa: E402
from sqlalchemy import make_url, text  # noqa: E402
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine  # noqa: E402

from app.core.db import engine, get_session  # noqa: E402
from app.main import create_app  # noqa: E402
from app.models import Agency, ApiKey, Business, Location  # noqa: E402
from scripts.create_agency import create_agency  # noqa: E402


@pytest.fixture(scope="session", autouse=True)
def migrated_db() -> None:
    """Fresh schema from the real migrations, once per run."""
    url = make_url(os.environ["SAYSO_DATABASE_URL"])
    assert url.database and url.database.endswith("_test"), "refusing to reset a non-test db"

    async def reset() -> None:
        eng = create_async_engine(url)
        async with eng.begin() as conn:
            await conn.execute(text("DROP SCHEMA public CASCADE"))
            await conn.execute(text("CREATE SCHEMA public"))
        await eng.dispose()

    asyncio.run(reset())
    command.upgrade(Config(str(Path(__file__).parents[1] / "alembic.ini")), "head")


@pytest.fixture
async def session() -> AsyncIterator[AsyncSession]:
    """Each test runs in a transaction that is rolled back; app commits become savepoints."""
    async with engine.connect() as conn:
        trans = await conn.begin()
        s = AsyncSession(
            bind=conn, join_transaction_mode="create_savepoint", expire_on_commit=False
        )
        yield s
        await s.close()
        await trans.rollback()


@pytest.fixture
async def client(session: AsyncSession) -> AsyncIterator[httpx.AsyncClient]:
    app = create_app()
    app.dependency_overrides[get_session] = lambda: session
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


def auth(key: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {key}"}


@dataclass
class Tree:
    """agency → business → (location, other_location), with a key at each of the first three."""

    agency: Agency
    business: Business
    location: Location
    other_location: Location
    agency_key: str
    business_key: str
    location_key: str


@pytest.fixture
def make_tree(session: AsyncSession) -> Callable[[str], Awaitable[Tree]]:
    async def make(name: str) -> Tree:
        agency, agency_key = await create_agency(session, f"Agency {name}")
        business = Business(agency_id=agency.id, name=f"Business {name}")
        session.add(business)
        await session.flush()
        locations = [
            Location(agency_id=agency.id, business_id=business.id, name=n, timezone="UTC")
            for n in (f"Location {name}", f"Other {name}")
        ]
        session.add_all(locations)
        await session.flush()
        business_row, business_key = ApiKey.issue(agency.id, "business", business.id)
        location_row, location_key = ApiKey.issue(agency.id, "location", locations[0].id)
        session.add_all([business_row, location_row])
        await session.commit()
        return Tree(agency, business, *locations, agency_key, business_key, location_key)

    return make
