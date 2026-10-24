"""The worker runs on its own sessions, so these tests use committed rows (each test makes its
own agency, so they don't see each other's data)."""

import asyncio
import uuid
from dataclasses import dataclass
from datetime import timedelta
from decimal import Decimal
from typing import Any

import pytest
from sqlalchemy import func, select, update

from app.core.db import SessionLocal
from app.models import (
    Agency,
    Business,
    CallbackDelivery,
    Decision,
    DecisionAsk,
    Location,
    Membership,
    Person,
    utcnow,
)
from app.schemas import DecisionIn
from app.services import decisions as decision_service
from app.services import escalation
from app.services.sms_commands import Command
from app.worker import step_decisions
from tests.conftest import FakeChannel

PAST = timedelta(seconds=-1)


@dataclass
class World:
    agency_id: str
    location_id: str
    lee: str  # location manager, limit 50
    raj: str  # business owner, limit 500


def phone() -> str:
    return f"+1777{uuid.uuid4().int % 10**7:07d}"


async def world(agency_settings: dict[str, Any] | None = None, raj_on_call: bool = False) -> World:
    async with SessionLocal() as s:
        one_at_a_time = {"contact_hours": None, "escalation": {"max_fanout": 1}}
        agency = Agency(name="W", settings={**one_at_a_time, **(agency_settings or {})})
        s.add(agency)
        await s.flush()
        business = Business(agency_id=agency.id, name="B")
        s.add(business)
        await s.flush()
        location = Location(agency_id=agency.id, business_id=business.id, name="L", timezone="UTC")
        s.add(location)
        lee, raj = Person(phone=phone()), Person(phone=phone())
        s.add_all([lee, raj])
        await s.flush()
        s.add_all(
            [
                Membership(
                    person_id=lee.id,
                    agency_id=agency.id,
                    scope_type="location",
                    scope_id=location.id,
                    display_name="Lee",
                    role="manager",
                    authority_limit=Decimal(50),
                ),
                Membership(
                    person_id=raj.id,
                    agency_id=agency.id,
                    scope_type="business",
                    scope_id=business.id,
                    display_name="Raj",
                    role="owner",
                    authority_limit=Decimal(500),
                    on_call=raj_on_call,
                ),
            ]
        )
        await s.commit()
        return World(agency.id, location.id, lee.phone, raj.phone)


async def new_decision(w: World, channel: FakeChannel, amount: int = 40, **kw: Any) -> str:
    """What POST /v1/decisions does: create, then the first ask inline."""
    async with SessionLocal() as s:
        body = DecisionIn(type="loaner_car", summary="x", subject={"amount": amount}, **kw)  # type: ignore[arg-type]
        d = await decision_service.create(s, w.agency_id, w.location_id, body, None)
        if d.status == "pending":
            await escalation.advance(s, channel, d)
        return d.id


async def get(decision_id: str) -> Decision:
    async with SessionLocal() as s:
        return await s.get_one(Decision, decision_id)


async def shift(decision_id: str, **deltas: timedelta) -> None:
    """Move timestamps relative to now, e.g. next_action_at=PAST."""
    async with SessionLocal() as s:
        values = {k: utcnow() + v for k, v in deltas.items()}
        await s.execute(update(Decision).where(Decision.id == decision_id).values(**values))
        await s.commit()


async def asks(decision_id: str) -> int:
    async with SessionLocal() as s:
        q = (
            select(func.count())
            .select_from(DecisionAsk)
            .where(DecisionAsk.decision_id == decision_id)
        )
        return await s.scalar(q) or 0


async def test_moves_to_the_next_approver_after_the_wait() -> None:
    w, ch = await world(), FakeChannel()
    d = await new_decision(w, ch)
    assert [to for to, _ in ch.sent] == [w.lee]
    await step_decisions(ch)
    assert ch.to(w.raj) == []  # the wait hasn't run out
    await shift(d, next_action_at=PAST)
    await step_decisions(ch)
    assert len(ch.to(w.raj)) == 1
    await shift(d, next_action_at=PAST)
    await step_decisions(ch)  # nobody left: waits for the deadline, nobody asked twice
    assert await asks(d) == 2 and len(ch.sent) == 2
    assert (await get(d)).next_action_at == (await get(d)).deadline_at


@pytest.mark.parametrize(
    ("location_settings", "outcome"),
    [
        ({}, "defer"),
        ({"on_timeout": {"outcome": "decline", "say": "We can't do that today."}}, "decline"),
        ({"on_timeout": {"outcome": "approve"}}, "defer"),  # written behind the API's back
    ],
)
async def test_expiry_yields_defer_or_decline_never_approve(location_settings, outcome) -> None:
    w, ch = await world(), FakeChannel()
    d = await new_decision(w, ch)
    async with SessionLocal() as s:
        await s.execute(
            update(Location).where(Location.id == w.location_id).values(settings=location_settings)
        )
        await s.commit()
    await shift(d, deadline_at=PAST, next_action_at=PAST)
    await step_decisions(ch)
    row = await get(d)
    assert (row.status, row.outcome, row.decided_by, row.reason) == (
        "expired",
        outcome,
        "timeout",
        "timeout",
    )
    default_say = "I wasn't able to confirm that right now. I'll check and get back to you shortly."
    assert row.say == ("We can't do that today." if outcome == "decline" else default_say)


async def test_after_hours_defer_expires_at_once() -> None:
    w, ch = (
        await world({"contact_hours": {}}),
        FakeChannel(),
    )  # nobody may be texted, after_hours = defer
    row = await get(await new_decision(w, ch))
    assert (row.status, row.outcome, row.reason) == ("expired", "defer", "after_hours")
    assert ch.sent == []


async def test_after_hours_on_call_asks_on_call_members() -> None:
    w, ch = (
        await world({"contact_hours": {}, "after_hours": "on_call"}, raj_on_call=True),
        FakeChannel(),
    )
    row = await get(await new_decision(w, ch))
    assert row.status == "pending" and [to for to, _ in ch.sent] == [w.raj]

    w2, ch2 = (
        await world({"contact_hours": {}, "after_hours": "on_call"}),
        FakeChannel(),
    )  # nobody on call
    row = await get(await new_decision(w2, ch2))
    assert (row.status, row.reason) == ("expired", "after_hours")


async def test_late_answer_is_stored_and_called_back() -> None:
    w, ch = await world(), FakeChannel()
    d = await new_decision(w, ch, callback_url="https://8.8.8.8/hook")
    await shift(d, deadline_at=PAST, next_action_at=PAST)
    await step_decisions(ch)
    async with SessionLocal() as s:
        ask = await s.scalar(select(DecisionAsk).where(DecisionAsk.decision_id == d))
        assert ask is not None
        reply = await escalation.answer(s, ch, ask, Command("approve"))
    assert "already timed out" in reply
    row = await get(d)
    assert (row.status, row.outcome, row.late_outcome) == ("expired", "defer", "approve")
    async with SessionLocal() as s:
        events = list(
            await s.scalars(select(CallbackDelivery.event).where(CallbackDelivery.decision_id == d))
        )
        assert sorted(events) == ["decision.expired", "decision.late_answer"]
        again = await escalation.answer(s, ch, ask, Command("decline"))
    assert again == "That request has closed."


async def test_answers_past_the_window_change_nothing() -> None:
    w, ch = await world(), FakeChannel()
    d = await new_decision(w, ch)
    await shift(d, deadline_at=PAST, next_action_at=PAST)
    await step_decisions(ch)
    await shift(d, deadline_at=-timedelta(hours=25))
    async with SessionLocal() as s:
        ask = await s.scalar(select(DecisionAsk).where(DecisionAsk.decision_id == d))
        assert ask is not None
        assert await escalation.answer(s, ch, ask, Command("approve")) == "That request has closed."
    assert (await get(d)).late_outcome is None


class SlowChannel(FakeChannel):
    async def send(self, to: str, text: str) -> str:
        await asyncio.sleep(0.2)
        return await super().send(to, text)


async def test_two_workers_never_process_the_same_row() -> None:
    w, ch = await world(), FakeChannel()
    d = await new_decision(w, ch)
    await shift(d, next_action_at=PAST)
    slow = SlowChannel()
    await asyncio.gather(step_decisions(slow), step_decisions(slow))
    assert slow.to(w.raj) and len(slow.to(w.raj)) == 1
    assert await asks(d) == 2


async def test_expiry_is_emailed_to_whoever_was_asked() -> None:
    w, ch = await world(), FakeChannel()
    d = await new_decision(w, ch)
    async with SessionLocal() as s:
        await s.execute(
            update(Membership)
            .where(Membership.agency_id == w.agency_id)
            .values(email="team@dealer.example")
        )
        await s.commit()
    await shift(d, deadline_at=PAST, next_action_at=PAST)
    sms_before = len(ch.to(w.lee))
    await step_decisions(ch)
    [notice] = ch.to("team@dealer.example")  # only Lee was asked, so one email
    assert "expired: nobody answered in time" in notice and "defer" in notice
    assert len(ch.to(w.lee)) == sms_before  # no SMS for news nobody has to act on
