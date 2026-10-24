from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import pytest
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Decision, utcnow
from tests.conftest import Tree, auth

POLICY = {
    "label": "Price adjustment",
    "rules": [
        {
            "id": "small",
            "when": {"var": "amount", "op": "<=", "value": 50},
            "then": "approve",
            "say": "Good news, we can do {context.requested}.",
        },
        {
            "id": "big",
            "when": {"var": "amount", "op": ">", "value": 150},
            "then": "decline",
            "say": "The best I can do is {context.quoted}.",
        },
        {
            "id": "meet",
            "when": {
                "all": [
                    {"var": "amount", "op": ">", "value": 100},
                    {"var": "context.quoted", "op": "exists"},
                ]
            },
            "then": "counter",
            "value": 820,
            "say": "How about 820?",
        },
    ],
    "default": "escalate",
}


def decision(amount: float | None = 40, **kw: Any) -> dict[str, Any]:
    subject = {"customer_ref": "ghl:contact_123"} | (
        {"amount": amount} if amount is not None else {}
    )
    return {
        "type": "price_adjustment",
        "summary": "Customer asks $800 instead of $850",
        "subject": subject,
        "context": {"quoted": 850, "requested": 800},
        **kw,
    }


@pytest.fixture
async def tree(client: httpx.AsyncClient, make_tree, staff) -> Tree:
    t: Tree = await make_tree("A")
    await staff(t)
    r = await client.put(
        "/v1/policies/price_adjustment",
        params={"scope": f"business:{t.business.id}"},
        json=POLICY,
        headers=auth(t.agency_key),
    )
    assert r.status_code == 200
    return t


async def post(
    client: httpx.AsyncClient, key: str, body: dict[str, Any], idem: str | None = None
) -> httpx.Response:
    headers = auth(key) | ({"Idempotency-Key": idem} if idem else {})
    return await client.post("/v1/decisions", json=body, headers=headers)


@pytest.mark.parametrize(
    ("amount", "status", "outcome", "value", "rule_id", "reason", "say"),
    [
        (40, "decided", "approve", None, "small", "rule_matched", "Good news, we can do 800."),
        (200, "decided", "decline", None, "big", "rule_matched", "The best I can do is 850."),
        (120, "decided", "counter", 820.0, "meet", "rule_matched", "How about 820?"),
        (
            80,
            "pending",
            None,
            None,
            None,
            "policy_default",
            "Let me check on that for you. One moment.",
        ),
        (
            None,
            "pending",
            None,
            None,
            None,
            "policy_default",
            "Let me check on that for you. One moment.",
        ),
    ],
)
async def test_policy_decisions(
    client: httpx.AsyncClient, tree: Tree, amount, status, outcome, value, rule_id, reason, say
) -> None:
    r = await post(client, tree.location_key, decision(amount))
    assert r.status_code == 200, r.text
    d = r.json()
    assert (d["status"], d["outcome"], d["value"], d["reason"], d["say"]) == (
        status,
        outcome,
        value,
        reason,
        say,
    )
    assert d["decided_by"] == ("policy" if status == "decided" else None)
    assert d["policy"]["version"] == 1 and d["policy"]["rule_id"] == rule_id
    assert d["location_id"] == tree.location.id and d["id"].startswith("dec_")
    assert (d["approver"], d["ai_recommendation"]) == (None, None)
    assert d["poll_after_ms"] == (1000 if status == "pending" else None)


async def test_no_policy_escalates_with_reason(client: httpx.AsyncClient, tree: Tree) -> None:
    r = await post(client, tree.location_key, decision(type="loaner_car"))
    assert r.status_code == 200
    d = r.json()
    assert (d["status"], d["reason"], d["policy"], d["decided_by"]) == (
        "pending",
        "no_policy",
        None,
        None,
    )


async def test_templates_never_decide_on_their_own(
    client: httpx.AsyncClient, make_tree, staff
) -> None:
    t = await make_tree("B")
    await staff(t)
    for decision_type in ("price_adjustment", "booking_exception", "trade_in_offer", "refund"):
        d = (await post(client, t.location_key, decision(1, type=decision_type))).json()
        assert (d["status"], d["reason"], d["decided_by"]) == ("pending", "policy_default", None)
        assert d["policy"]["version"] == 2 and d["policy"]["rule_id"] is None


async def test_location_resolution(client: httpx.AsyncClient, tree: Tree, make_tree) -> None:
    other = await make_tree("B")
    r = await post(client, tree.agency_key, decision())
    assert r.status_code == 422 and "location_id" in r.json()["detail"]
    r = await post(client, tree.agency_key, decision(location_id=tree.other_location.id))
    assert r.status_code == 200 and r.json()["location_id"] == tree.other_location.id
    r = await post(client, tree.business_key, decision(location_id=tree.location.id))
    assert r.status_code == 200
    assert (
        await post(client, tree.location_key, decision(location_id=tree.other_location.id))
    ).status_code == 404
    assert (
        await post(client, other.agency_key, decision(location_id=tree.location.id))
    ).status_code == 404
    assert (
        await post(client, tree.agency_key, decision(location_id="loc_missing"))
    ).status_code == 404


async def test_idempotency(client: httpx.AsyncClient, tree: Tree, make_tree) -> None:
    first = await post(client, tree.location_key, decision(), idem="call_8f2e:discount:1")
    replay = await post(client, tree.location_key, decision(), idem="call_8f2e:discount:1")
    assert replay.status_code == 200 and replay.json() == first.json()

    conflict = await post(client, tree.location_key, decision(45), idem="call_8f2e:discount:1")
    assert conflict.status_code == 409
    assert conflict.headers["content-type"] == "application/problem+json"
    assert set(conflict.json()) == {"type", "title", "status", "detail"}
    assert conflict.json()["status"] == 409 and conflict.json()["title"] == "Conflict"

    # Keys are per agency, and requests without a key are never deduplicated.
    other = await make_tree("B")
    assert (
        await post(client, other.location_key, decision(), idem="call_8f2e:discount:1")
    ).status_code == 200
    a = (await post(client, tree.location_key, decision())).json()["id"]
    b = (await post(client, tree.location_key, decision())).json()["id"]
    assert a != b


async def test_deadlines_follow_settings_and_only_tighten(
    client: httpx.AsyncClient, tree: Tree
) -> None:
    def seconds(d: dict[str, Any]) -> float:
        return (
            datetime.fromisoformat(d["deadline_at"]) - datetime.fromisoformat(d["created_at"])
        ).total_seconds()

    assert seconds((await post(client, tree.location_key, decision(80))).json()) == pytest.approx(
        30, abs=1
    )
    assert seconds(
        (await post(client, tree.location_key, decision(80, deadline_ms=10_000))).json()
    ) == pytest.approx(10, abs=1)
    assert seconds(
        (await post(client, tree.location_key, decision(80, deadline_ms=99_000))).json()
    ) == pytest.approx(30, abs=1)
    assert seconds(
        (await post(client, tree.location_key, decision(80, mode="async"))).json()
    ) == pytest.approx(3600, abs=1)
    r = await post(client, tree.location_key, decision(80, deadline_ms=100))
    assert r.status_code == 422  # below the 5 s floor


async def test_read_and_list_respect_scope(
    client: httpx.AsyncClient, tree: Tree, make_tree
) -> None:
    other = await make_tree("B")
    mine = (await post(client, tree.location_key, decision(80))).json()
    sibling = (
        await post(client, tree.agency_key, decision(40, location_id=tree.other_location.id))
    ).json()
    theirs = (await post(client, other.location_key, decision(40))).json()

    assert (
        await client.get(f"/v1/decisions/{mine['id']}", headers=auth(tree.location_key))
    ).json() == mine
    assert (
        await client.get(f"/v1/decisions/{sibling['id']}", headers=auth(tree.location_key))
    ).status_code == 404
    assert (
        await client.get(f"/v1/decisions/{theirs['id']}", headers=auth(tree.agency_key))
    ).status_code == 404
    assert (
        await client.get("/v1/decisions/dec_missing", headers=auth(tree.agency_key))
    ).status_code == 404

    async def ids(key: str, **params: Any) -> list[str]:
        r = await client.get("/v1/decisions", params=params, headers=auth(key))
        assert r.status_code == 200, r.text
        return [d["id"] for d in r.json()]

    assert await ids(tree.agency_key) == [sibling["id"], mine["id"]]  # newest first
    assert await ids(tree.business_key) == [sibling["id"], mine["id"]]
    assert await ids(tree.location_key) == [mine["id"]]
    assert await ids(tree.agency_key, status="pending") == [mine["id"]]
    assert await ids(tree.agency_key, location_id=tree.other_location.id) == [sibling["id"]]
    assert await ids(tree.agency_key, limit=1) == [sibling["id"]]
    future = (datetime.now(UTC) + timedelta(hours=1)).isoformat()
    assert await ids(tree.agency_key, **{"from": future}) == []
    assert await ids(other.agency_key) == [theirs["id"]]
    r = await client.get(
        "/v1/decisions", params={"location_id": tree.location.id}, headers=auth(other.agency_key)
    )
    assert r.status_code == 404
    r = await client.get(
        "/v1/decisions", params={"from": "2026-01-01T00:00:00"}, headers=auth(tree.agency_key)
    )
    assert r.status_code == 422  # timestamps need a timezone


@pytest.mark.parametrize(
    "fields",
    [
        {"decided_by": "timeout", "outcome": "approve", "status": "expired"},
        {"decided_by": "ai", "outcome": "approve", "status": "decided"},
        {"reason": "because"},
    ],
)
async def test_database_enforces_the_floor(
    session: AsyncSession, make_tree, fields: dict[str, str]
) -> None:
    t = await make_tree("A")
    row = Decision(
        agency_id=t.agency.id,
        location_id=t.location.id,
        type="refund",
        mode="live",
        deadline_at=utcnow(),
        **fields,
    )
    session.add(row)
    with pytest.raises(IntegrityError):
        await session.flush()
    await session.rollback()


async def test_cancel(client: httpx.AsyncClient, tree: Tree, make_tree) -> None:
    other = await make_tree("B")
    d = (await post(client, tree.location_key, decision(80))).json()
    assert d["status"] == "pending"
    url = f"/v1/decisions/{d['id']}/cancel"
    assert (await client.post(url, headers=auth(other.agency_key))).status_code == 404
    r = await client.post(url, headers=auth(tree.location_key))
    assert r.status_code == 200
    assert (r.json()["status"], r.json()["reason"], r.json()["outcome"]) == (
        "cancelled",
        "cancelled",
        None,
    )
    again = await client.post(url, headers=auth(tree.location_key))
    assert again.status_code == 409 and again.headers["content-type"] == "application/problem+json"
    decided = (await post(client, tree.location_key, decision(40))).json()
    r = await client.post(f"/v1/decisions/{decided['id']}/cancel", headers=auth(tree.location_key))
    assert r.status_code == 409


async def test_async_mode_returns_202_without_waiting(
    client: httpx.AsyncClient, tree: Tree
) -> None:
    r = await post(client, tree.location_key, decision(80, mode="async", wait_ms=5000))
    assert r.status_code == 202 and r.json()["status"] == "pending"
    r = await post(client, tree.location_key, decision(40, mode="async"))
    assert r.status_code == 202 and r.json()["outcome"] == "approve"  # rules still answer at once
