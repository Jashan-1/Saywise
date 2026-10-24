from datetime import timedelta

import httpx
import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.constants import COLD_P_ANSWER, COLD_T80
from app.models import Decision, DecisionAsk, utcnow
from app.services import history
from app.services.escalation import Candidate, plan_step
from tests.conftest import FakeChannel, Tree, auth

S = timedelta(seconds=1)


def c(key: str, p: float, t80: int = 20) -> Candidate:
    return Candidate(key, p, t80 * S)


COLD = [c(k, COLD_P_ANSWER, int(COLD_T80 / S)) for k in "abcde"]


@pytest.mark.parametrize(
    ("candidates", "remaining", "fanout", "needed", "ask", "wait"),
    [
        # Cold start: 0.5 each needs 4 for 0.9, so fan out to the cap (3), wait the cold t80.
        (COLD, 30 * S, 3, 1, ["a", "b", "c"], 15 * S),  # 5 people / 3 per step = 2 tiers: 15 s
        (COLD, 120 * S, 3, 1, ["a", "b", "c"], 20 * S),
        (COLD, 120 * S, 5, 1, ["a", "b", "c", "d"], 20 * S),  # 1 - 0.5^4 >= 0.9: stop at 4
        # A strong approver alone is enough.
        ([c("a", 0.5), c("b", 0.95, 8)], 30 * S, 3, 1, ["b"], 8 * S),
        # Two decent ones: 1 - 0.3 * 0.3 = 0.91.
        ([c("a", 0.7, 12), c("b", 0.7, 25), c("z", 0.1)], 60 * S, 3, 1, ["a", "b"], 25 * S),
        # Ranked by p_answer; ties keep the given (authority) order.
        ([c("low", 0.6), c("high", 0.6), c("best", 0.99, 5)], 60 * S, 3, 1, ["best"], 5 * S),
        ([c("low", 0.6), c("high", 0.6)], 60 * S, 1, 1, ["low"], 20 * S),
        # max_fanout caps it even when the target isn't reached.
        ([c("a", 0.2), c("b", 0.2), c("c", 0.2)], 60 * S, 2, 1, ["a", "b"], 20 * S),
        # The window shrinks near the deadline: wait never exceeds this step's share.
        ([c("a", 0.95, 40)], 10 * S, 3, 1, ["a"], 10 * S),
        ([c("a", 0.5), c("b", 0.5), c("c", 0.5), c("d", 0.5)], 8 * S, 1, 1, ["a"], 2 * S),
        ([c("a", 0.95)], -5 * S, 3, 1, ["a"], 0 * S),  # past the deadline: no waiting
        # Two-person rule: at least two asked even when one would answer reliably.
        ([c("a", 0.99, 5), c("b", 0.5, 30)], 60 * S, 3, 2, ["a", "b"], 30 * S),
        (
            [c("a", 0.99, 5)],
            60 * S,
            3,
            2,
            ["a"],
            5 * S,
        ),  # only one exists: ask them, find more later
        ([c("a", 0.99), c("b", 0.99), c("c", 0.99)], 60 * S, 1, 2, ["a"], 20 * S),  # cap still wins
        ([], 60 * S, 3, 1, [], 0 * S),
    ],
)
def test_plan_step(candidates, remaining, fanout, needed, ask, wait) -> None:
    step = plan_step(candidates, remaining, fanout, needed)
    assert (step.ask, step.wait) == (ask, wait)


async def test_stats_come_from_answered_asks(session: AsyncSession, make_tree, add_member) -> None:
    t: Tree = await make_tree("A")
    fast = await add_member(t.agency.id, ("agency", t.agency.id), "+15550002001", "Fast")
    new = await add_member(t.agency.id, ("agency", t.agency.id), "+15550002002", "New")
    now = utcnow()
    for i, took in enumerate([5, 6, 7, 8, 30, None]):  # 6 asks this hour, 5 answered
        d = Decision(
            agency_id=t.agency.id,
            location_id=t.location.id,
            type="refund",
            mode="live",
            status="decided",
            deadline_at=now,
        )
        session.add(d)
        await session.flush()
        sent = now - timedelta(days=1 + i)  # same hour of day, earlier days
        session.add(
            DecisionAsk(
                decision_id=d.id,
                person_id=fast.person_id,
                membership_id=fast.id,
                channel="sms",
                sent_at=sent,
                responded_at=None if took is None else sent + took * S,
            )
        )
    await session.flush()
    history._cache.clear()
    stats = await history.approver_stats(session, [fast.person_id, new.person_id], 10 * S, now)
    assert stats[fast.person_id].p_answer == pytest.approx(4 / 6)  # within 10 s
    assert 8 * S < stats[fast.person_id].t80 < 30 * S
    assert stats[new.person_id] == history.COLD  # too few asks: cold start


async def two_person_tree(client, make_tree, add_member, session) -> tuple[Tree, list[str]]:
    t: Tree = await make_tree("A")
    t.agency.settings = {"contact_hours": None, "escalation": {"two_person_over": 100}}
    await session.commit()
    phones = ["+15550003001", "+15550003002", "+15550003003"]
    for i, phone in enumerate(phones):
        await add_member(t.agency.id, ("agency", t.agency.id), phone, f"P{i}", 1000)
    return t, phones


async def post(client: httpx.AsyncClient, key: str, amount: int) -> dict:
    body = {"type": "loaner_car", "summary": "x", "subject": {"amount": amount}}
    return (await client.post("/v1/decisions", json=body, headers=auth(key))).json()


async def test_fan_out_on_cold_start(
    client, make_tree, add_member, session, channel: FakeChannel
) -> None:
    t, phones = await two_person_tree(client, make_tree, add_member, session)
    await post(client, t.location_key, 50)  # under the two-person line
    assert sorted(to for to, _ in channel.sent) == sorted(phones)  # 3 = default max_fanout


async def test_two_person_rule(
    client, make_tree, add_member, session, channel: FakeChannel
) -> None:
    from tests.test_inbound import sms

    t, (a, b, c_) = await two_person_tree(client, make_tree, add_member, session)
    d = await post(client, t.location_key, 500)
    await sms(client, a, "1")
    assert channel.to(a)[-1].endswith("needs one more approval.")
    await sms(client, a, "1")  # the same person twice doesn't count
    r = (await client.get(f"/v1/decisions/{d['id']}", headers=auth(t.location_key))).json()
    assert r["status"] == "pending"
    await sms(client, b, "300")
    assert "needs two approvals" in channel.to(b)[-1]
    await sms(client, b, "yes")
    r = (await client.get(f"/v1/decisions/{d['id']}", headers=auth(t.location_key))).json()
    assert (r["status"], r["outcome"], r["approver"]["name"]) == ("decided", "approve", "P1")

    # One decline ends it.
    d2 = await post(client, t.location_key, 500)
    await sms(client, c_, "2")
    r = (await client.get(f"/v1/decisions/{d2['id']}", headers=auth(t.location_key))).json()
    assert (r["status"], r["outcome"]) == ("decided", "decline")
