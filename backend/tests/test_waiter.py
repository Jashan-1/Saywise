import asyncio
import os
import time
from datetime import timedelta

import httpx
from sqlalchemy import text

from app.core.db import SessionLocal, engine
from app.models import Agency, Business, Decision, Location, utcnow
from app.services import decisions
from app.services.waiter import Waiter
from tests.conftest import auth


async def notify(decision_id: str) -> None:
    async with engine.connect() as conn:
        await conn.execute(text("SELECT pg_notify('sayso_decisions', :id)"), {"id": decision_id})
        await conn.commit()


async def never() -> bool:
    return False


async def test_waiter_wakes_on_notify_and_times_out() -> None:
    w = Waiter()
    await w.start(os.environ["SAYSO_DATABASE_URL"])
    try:
        start = time.monotonic()
        waiting = asyncio.create_task(w.wait("dec_x", 5, never))
        await asyncio.sleep(0.1)
        await notify("dec_other")  # someone else's decision doesn't wake us
        await asyncio.sleep(0.1)
        assert not waiting.done()
        await notify("dec_x")
        await asyncio.wait_for(waiting, 2)
        assert time.monotonic() - start < 2

        start = time.monotonic()
        await w.wait("dec_y", 0.2, never)
        assert 0.2 <= time.monotonic() - start < 1

        async def already() -> bool:
            return True

        start = time.monotonic()
        await w.wait("dec_z", 5, already)  # answered before we subscribed: no wait at all
        assert time.monotonic() - start < 0.5
        assert w._futures == {}  # cleaned up
    finally:
        await w.stop()


async def test_live_request_times_out_to_pending(
    client: httpx.AsyncClient, make_tree, staff
) -> None:
    t = await make_tree("A")
    await staff(t)
    start = time.monotonic()
    r = await client.post(
        "/v1/decisions",
        json={"type": "loaner_car", "summary": "x", "wait_ms": 300},
        headers=auth(t.location_key),
    )
    assert time.monotonic() - start >= 0.3
    d = r.json()
    assert (d["status"], d["say"], d["poll_after_ms"]) == (
        "pending",
        "Let me check on that for you. One moment.",
        1000,
    )
    r = await client.get(
        f"/v1/decisions/{d['id']}", params={"wait_ms": 25_001}, headers=auth(t.location_key)
    )
    assert r.status_code == 422


async def test_racing_answers_produce_one_outcome() -> None:
    """Real, committed rows on separate connections: the conditional UPDATE lets one answer win."""
    async with SessionLocal() as s:
        agency = Agency(name="Race")
        s.add(agency)
        await s.flush()
        business = Business(agency_id=agency.id, name="B")
        s.add(business)
        await s.flush()
        location = Location(agency_id=agency.id, business_id=business.id, name="L", timezone="UTC")
        s.add(location)
        await s.flush()
        decision = Decision(
            agency_id=agency.id,
            location_id=location.id,
            type="refund",
            mode="live",
            status="pending",
            deadline_at=utcnow() + timedelta(minutes=1),
        )
        s.add(decision)
        await s.commit()

    async def answer(outcome: str) -> Decision | None:
        async with SessionLocal() as s:
            return await decisions.resolve(s, decision.id, outcome, None, None, outcome)  # type: ignore[arg-type]

    results = await asyncio.gather(*(answer(o) for o in ("approve", "decline") * 5))
    winners = [r for r in results if r is not None]
    assert len(winners) == 1
    async with SessionLocal() as s:
        final = await s.get(Decision, decision.id)
        assert final is not None and final.outcome == winners[0].outcome
