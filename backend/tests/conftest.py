import os

# Tests never touch SAYSO_DATABASE_URL from .env (it may point at Supabase).
os.environ["SAYSO_DATABASE_URL"] = os.environ.get(
    "SAYSO_TEST_DATABASE_URL", "postgresql+asyncpg://sayso:sayso@localhost:5433/sayso_test"
)
os.environ.setdefault("SAYSO_SIGNING_SECRET", "test-signing-secret-0123456789abcdef")
os.environ["SAYSO_TWILIO_AUTH_TOKEN"] = "test-twilio-token"
os.environ["SAYSO_PUBLIC_BASE_URL"] = "http://localhost:8000"

import asyncio  # noqa: E402
from collections.abc import AsyncIterator, Awaitable, Callable  # noqa: E402
from dataclasses import dataclass, field  # noqa: E402
from decimal import Decimal  # noqa: E402
from pathlib import Path  # noqa: E402

import httpx  # noqa: E402
import pytest  # noqa: E402
from alembic import command  # noqa: E402
from alembic.config import Config  # noqa: E402
from sqlalchemy import make_url, select, text  # noqa: E402
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine  # noqa: E402

from app.api.deps import get_channel  # noqa: E402
from app.core.db import engine, get_session  # noqa: E402
from app.main import create_app  # noqa: E402
from app.models import Agency, ApiKey, Business, Location, Membership, Person  # noqa: E402
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


@dataclass
class FakeChannel:
    sent: list[tuple[str, str]] = field(default_factory=list)

    async def send(self, to: str, text: str) -> str:
        self.sent.append((to, text))
        return f"SM{len(self.sent)}"

    def to(self, phone: str) -> list[str]:
        return [text for to, text in self.sent if to == phone]


@pytest.fixture
def channel() -> FakeChannel:
    return FakeChannel()


@pytest.fixture
async def client(session: AsyncSession, channel: FakeChannel) -> AsyncIterator[httpx.AsyncClient]:
    app = create_app()
    app.dependency_overrides[get_session] = lambda: session
    app.dependency_overrides[get_channel] = lambda: channel
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


@pytest.fixture
def add_member(session: AsyncSession) -> Callable[..., Awaitable[Membership]]:
    """Give a phone a membership; the person is shared across agencies by phone."""

    async def add(
        agency_id: str,
        scope: tuple[str, str],
        phone: str,
        name: str,
        limit: int = 100,
        **kw: object,
    ) -> Membership:
        person = await session.scalar(select(Person).where(Person.phone == phone))
        if person is None:
            person = Person(phone=phone)
            session.add(person)
            await session.flush()
        kw.setdefault("role", "manager")
        membership = Membership(
            person_id=person.id,
            agency_id=agency_id,
            scope_type=scope[0],
            scope_id=scope[1],
            display_name=name,
            authority_limit=Decimal(limit),
            **kw,
        )
        session.add(membership)
        await session.commit()
        return membership

    return add
