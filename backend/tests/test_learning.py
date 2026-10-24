"""Phase 6: customer history in rules, rule suggestions, usage and retention."""

import asyncio
from datetime import timedelta
from decimal import Decimal
from typing import Any

import httpx
import pytest
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.db import SessionLocal
from app.models import (
    AiCall,
    Business,
    Decision,
    DecisionAsk,
    Location,
    Suggestion,
    WorkerJob,
    utcnow,
)
from app.services import history, retention, suggestions
from app.services.suggestions import bucket, find
from app.worker import run_job
from tests.conftest import Tree, auth

REPEAT_POLICY = {
    "label": "Price adjustment",
    "rules": [
        {
            "id": "repeat",
            "when": {
                "all": [
                    {"var": "history.approved_count", "op": ">=", "value": 3},
                    {"var": "amount", "op": "<=", "value": 75},
                ]
            },
            "then": "approve",
        }
    ],
}


def past(t: Tree, location_id: str | None = None, **kw: Any) -> Decision:
    fields: dict[str, Any] = {
        "agency_id": t.agency.id,
        "location_id": location_id or t.location.id,
        "type": "price_adjustment",
        "mode": "live",
        "status": "decided",
        "deadline_at": utcnow(),
        "decided_by": "human",
        "decided_at": utcnow(),
        "outcome": "approve",
        "amount": Decimal(40),
        **kw,
    }
    return Decision(**fields)


# --- customer history -----------------------------------------------------------------------


async def other_business(session: AsyncSession, t: Tree) -> str:
    """A second business in the same agency, with one location."""
    b = Business(agency_id=t.agency.id, name="Other business")
    session.add(b)
    await session.flush()
    loc = Location(agency_id=t.agency.id, business_id=b.id, name="Elsewhere", timezone="UTC")
    session.add(loc)
    await session.flush()
    return loc.id


async def test_customer_facts_stay_within_the_business(session: AsyncSession, make_tree) -> None:
    t: Tree = await make_tree("A")
    elsewhere = await other_business(session, t)
    session.add_all(
        [
            past(t, customer_ref="c1", amount=Decimal(40)),
            past(t, location_id=t.other_location.id, customer_ref="c1", amount=Decimal(60)),
            past(t, customer_ref="c1", outcome="decline", amount=Decimal(500)),
            past(t, customer_ref="c2"),
            past(t, location_id=elsewhere, customer_ref="c1"),  # another business: never counted
        ]
    )
    await session.flush()
    facts = await history.customer_facts(session, t.agency.id, t.location.id, "c1")
    assert facts == {
        "decision_count": 3,
        "approved_count": 2,
        "declined_count": 1,
        "last_outcome": facts["last_outcome"],
        "total_approved_amount": Decimal(100),
    }
    assert facts["last_outcome"] in ("approve", "decline")
    assert await history.customer_facts(session, t.agency.id, t.location.id, None) == {}
    other = await history.customer_facts(session, t.agency.id, elsewhere, "c1")
    assert other["decision_count"] == 1


async def test_repeat_customer_is_approved_by_a_history_rule(
    client: httpx.AsyncClient, session: AsyncSession, make_tree, staff
) -> None:
    t: Tree = await make_tree("A")
    await staff(t)
    elsewhere = await other_business(session, t)
    h = auth(t.agency_key)
    await client.put("/v1/policies/price_adjustment", json=REPEAT_POLICY, headers=h)

    async def ask(location_id: str, customer: str) -> dict[str, Any]:
        body = {
            "location_id": location_id,
            "type": "price_adjustment",
            "summary": "x",
            "subject": {"customer_ref": customer, "amount": 70},
        }
        return (await client.post("/v1/decisions", json=body, headers=h)).json()

    session.add_all([past(t, customer_ref="loyal") for _ in range(3)])
    session.add_all([past(t, location_id=elsewhere, customer_ref="stranger") for _ in range(3)])
    await session.commit()
    loyal = await ask(t.other_location.id, "loyal")  # same business, different location
    assert (loyal["outcome"], loyal["policy"]["rule_id"]) == ("approve", "repeat")
    stranger = await ask(t.location.id, "stranger")  # their history is in another business
    assert stranger["status"] == "pending"


# --- suggestions ----------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("amount", "edges"),
    [
        (Decimal(1), (0, 25)),
        (Decimal(25), (0, 25)),
        (Decimal("25.01"), (25, 50)),
        (Decimal(10000), (5000, 10000)),
        (Decimal(10001), None),
        (Decimal(0), None),
    ],
)
def test_bucket(amount: Decimal, edges) -> None:
    assert bucket(amount) == edges


def rows(n: int, approved: int, amount: int = 40, business: str = "bus_1") -> list[tuple]:
    return [
        ("agy", business, "refund", Decimal(amount), "approve" if i < approved else "decline")
        for i in range(n)
    ]


@pytest.mark.parametrize(
    ("data", "expected"),
    [
        (rows(10, 10), [(25, 50, 10, 10)]),
        (rows(10, 9), [(25, 50, 10, 9)]),  # exactly 0.9
        (rows(10, 8), []),
        (rows(9, 9), []),  # too few
        (rows(20, 19), [(25, 50, 20, 19)]),
        (rows(5, 5, 40) + rows(5, 5, 60), []),  # different buckets don't add up
        (rows(10, 10, business="a") + rows(10, 10, business="b"), [(25, 50, 10, 10)] * 2),
        ([("agy", "bus_1", "refund", None, "approve")] * 12, []),  # no amount
    ],
)
def test_find(data: list[tuple], expected: list[tuple]) -> None:
    assert [(f.lo, f.hi, f.n, f.approved) for f in find(data)] == expected


async def test_nightly_job_suggests_and_adopting_writes_a_version(
    client: httpx.AsyncClient, session: AsyncSession, make_tree, staff
) -> None:
    t: Tree = await make_tree("A")
    await staff(t)
    other: Tree = await make_tree("B")
    session.add_all([past(t, type="refund", amount=Decimal(30)) for _ in range(10)])
    session.add_all(
        [past(t, type="refund", amount=Decimal(30), decided_by="policy") for _ in range(10)]
    )
    await session.flush()
    assert await suggestions.run(session) >= 1
    await suggestions.run(session)  # a rerun must not duplicate
    mine = list(
        await session.scalars(select(Suggestion).where(Suggestion.agency_id == t.agency.id))
    )
    assert len(mine) == 1  # policy decisions don't count, and reruns don't duplicate
    await session.commit()

    listed = (await client.get("/v1/suggestions", headers=auth(t.business_key))).json()
    assert [s["id"] for s in listed] == [mine[0].id]
    assert listed[0]["evidence"]["decisions"] == 10 and listed[0]["rule"]["then"] == "approve"
    assert (await client.get("/v1/suggestions", headers=auth(t.location_key))).json() == []
    assert (await client.get("/v1/suggestions", headers=auth(other.agency_key))).json() == []
    assert (
        await client.post(f"/v1/suggestions/{mine[0].id}/adopt", headers=auth(other.agency_key))
    ).status_code == 404

    # Before adopting, a $30 refund escalates (the template has no rules).
    body = {"type": "refund", "summary": "x", "subject": {"amount": 30}}
    assert (await client.post("/v1/decisions", json=body, headers=auth(t.location_key))).json()[
        "status"
    ] == "pending"
    r = await client.post(f"/v1/suggestions/{mine[0].id}/adopt", headers=auth(t.business_key))
    assert r.status_code == 200
    policy = r.json()
    assert (policy["source"], policy["scope_type"], policy["scope_id"], policy["version"]) == (
        "suggestion",
        "business",
        t.business.id,
        1,
    )
    assert policy["body"]["rules"][-1]["id"] == "suggested_25_50"
    assert (
        await client.post(f"/v1/suggestions/{mine[0].id}/adopt", headers=auth(t.business_key))
    ).status_code == 409
    d = (await client.post("/v1/decisions", json=body, headers=auth(t.location_key))).json()
    assert (d["outcome"], d["policy"]["rule_id"]) == ("approve", "suggested_25_50")


async def test_jobs_run_once_per_period_across_workers() -> None:
    async with SessionLocal() as s:
        await s.execute(delete(WorkerJob).where(WorkerJob.name == "probe"))
        await s.commit()
    runs = 0

    async def job(session: AsyncSession) -> int:
        nonlocal runs
        runs += 1
        await asyncio.sleep(0.2)
        return 0

    results = await asyncio.gather(*(run_job("probe", timedelta(hours=24), job) for _ in range(3)))
    assert sorted(results) == [False, False, True] and runs == 1
    assert await run_job("probe", timedelta(hours=24), job) is False  # ran recently
    assert await run_job("probe", timedelta(0), job) is True


# --- retention ------------------------------------------------------------------------------


async def test_retention_scrubs_only_expired_context(
    session: AsyncSession, make_tree, add_member
) -> None:
    t: Tree = await make_tree("A")
    t.other_location.settings = {"retention": {"context_days": 30}}
    old, recent, pending, short = (
        utcnow() - timedelta(days=100),
        utcnow() - timedelta(days=10),
        utcnow() - timedelta(days=100),
        utcnow() - timedelta(days=40),
    )
    rows = {
        "old": past(
            t, created_at=old, context={"phone": "x"}, summary="s", say="y", customer_ref="c"
        ),
        "recent": past(t, created_at=recent, context={"phone": "x"}, summary="s", say="y"),
        "pending": past(
            t, created_at=pending, status="pending", decided_by=None, outcome=None, context={"a": 1}
        ),
        "short": past(
            t, location_id=t.other_location.id, created_at=short, context={"a": 1}, summary="s"
        ),
    }
    session.add_all(rows.values())
    await session.flush()
    m = await add_member(t.agency.id, ("agency", t.agency.id), "+15550004444", "Ann")
    session.add(
        DecisionAsk(
            decision_id=rows["old"].id,
            person_id=m.person_id,
            membership_id=m.id,
            channel="sms",
            ai_recommendation="approve",
        )
    )
    await session.flush()
    assert await retention.run(session) == 2
    for row in rows.values():
        await session.refresh(row)
    assert (rows["old"].context, rows["old"].summary, rows["old"].say) == (None, None, None)
    assert (rows["old"].outcome, rows["old"].amount, rows["old"].customer_ref) == (
        "approve",
        Decimal(40),
        "c",
    )
    assert rows["old"].context_scrubbed_at is not None
    assert rows["short"].context is None  # its location keeps 30 days
    assert rows["recent"].context == {"phone": "x"}  # inside 90 days
    assert rows["pending"].context == {"a": 1}  # never while pending
    ask = await session.scalar(select(DecisionAsk).where(DecisionAsk.decision_id == rows["old"].id))
    assert ask is not None and ask.ai_recommendation is None
    assert await retention.run(session) == 0  # already scrubbed


# --- usage ----------------------------------------------------------------------------------


async def test_usage_per_scope_and_month(
    client: httpx.AsyncClient, session: AsyncSession, make_tree
) -> None:
    t: Tree = await make_tree("A")
    other: Tree = await make_tree("B")
    oct_ = utcnow().replace(year=2026, month=10, day=5)
    sep = oct_.replace(month=9)
    session.add_all(
        [
            past(t, created_at=oct_),
            past(t, created_at=oct_),
            past(t, created_at=sep),
            past(t, location_id=t.other_location.id, created_at=oct_),
            past(other, created_at=oct_),
        ]
    )
    session.add_all(
        [
            AiCall(
                agency_id=t.agency.id,
                location_id=t.location.id,
                feature="summary",
                model="m",
                tokens_in=100,
                tokens_out=20,
                latency_ms=300,
                ok=True,
                created_at=oct_,
            ),
            AiCall(
                agency_id=t.agency.id,
                location_id=None,
                feature="draft_rule",
                model="m",
                tokens_in=50,
                tokens_out=10,
                latency_ms=300,
                ok=True,
                created_at=oct_,
            ),
        ]
    )
    await session.flush()

    async def get(key: str, **params: str) -> list[dict[str, Any]]:
        r = await client.get("/v1/usage", params=params, headers=auth(key))
        assert r.status_code == 200, r.text
        return r.json()

    agency = await get(t.agency_key)
    assert [(m["month"], m["decisions"], m["ai_calls"], m["ai_tokens_in"]) for m in agency] == [
        ("2026-09", 1, 0, 0),
        ("2026-10", 3, 2, 150),
    ]
    location = await get(t.location_key)
    assert [(m["month"], m["decisions"], m["ai_calls"]) for m in location] == [
        ("2026-09", 1, 0),
        ("2026-10", 2, 1),
    ]
    assert [
        m["decisions"] for m in await get(t.agency_key, scope=f"location:{t.other_location.id}")
    ] == [1]
    assert await get(t.agency_key, **{"from": "2026-10-01T00:00:00Z"}) == [agency[1]]
    r = await client.get(
        "/v1/usage", params={"scope": f"agency:{t.agency.id}"}, headers=auth(other.agency_key)
    )
    assert r.status_code == 404
