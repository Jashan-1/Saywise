from typing import Any

import httpx
import pytest
from pydantic import ValidationError
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Policy
from app.services.settings import apply_patch, merge, resolve, tighten, validate_layer
from tests.conftest import auth


@pytest.mark.parametrize(
    ("layers", "path", "value", "origin"),
    [
        ([], "deadlines.live_ms", 30000, "default"),
        ([("agency:a", {"deadlines": {"live_ms": 20000}})], "deadlines.live_ms", 20000, "agency:a"),
        # Nearest layer wins.
        (
            [
                ("agency:a", {"deadlines": {"live_ms": 20000}}),
                ("location:l", {"deadlines": {"live_ms": 9000}}),
            ],
            "deadlines.live_ms",
            9000,
            "location:l",
        ),
        # Sections merge key by key: a later layer setting live_ms leaves async_ms inherited.
        (
            [
                ("agency:a", {"deadlines": {"async_ms": 120000}}),
                ("business:b", {"deadlines": {"live_ms": 9000}}),
            ],
            "deadlines.async_ms",
            120000,
            "agency:a",
        ),
        # Leaves are replaced whole, even when they are dicts.
        (
            [
                ("agency:a", {"contact_hours": {"mon": ["22:00-07:00"]}}),
                ("business:b", {"contact_hours": {"sun": ["00:00-23:59"]}}),
            ],
            "contact_hours",
            {"sun": ["00:00-23:59"]},
            "business:b",
        ),
        ([("policy:p@2", {"after_hours": "on_call"})], "after_hours", "on_call", "policy:p@2"),
    ],
)
def test_merge(layers: list[Any], path: str, value: Any, origin: str) -> None:
    merged, origins = merge(layers)
    section, _, key = path.partition(".")
    assert (merged[section][key] if key else merged[section]) == value
    assert origins[path] == origin


@pytest.mark.parametrize(
    ("partial", "ok"),
    [
        ({}, True),
        ({"on_timeout": {"outcome": "decline"}}, True),
        ({"on_timeout": {"outcome": "approve"}}, False),  # fixed floor
        ({"deadlines": {"live_ms": 4999}}, False),
        ({"deadlines": {"live_ms": 120001}}, False),
        ({"deadlines": {"async_ms": 7 * 24 * 3_600_000 + 1}}, False),
        ({"escalation": {"max_fanout": 0}}, False),
        ({"escalation": {"max_fanout": 11}}, False),
        ({"escalation": {"max_people": 10}}, True),
        ({"escalation": {"max_people": 11}}, False),
        ({"escalation": {"max_people": 0}}, False),
        ({"escalation": {"two_person_over": -1}}, False),
        ({"escalation": {"two_person_over": 500}}, True),
        ({"on_timeout": {"say": "x" * 301}}, False),
        ({"retention": {"context_days": 0}}, False),
        ({"locale": "fr"}, False),
        ({"after_hours": "ask_everyone"}, False),
        ({"callback_url": "http://example.com/hook"}, False),  # https only
        ({"callback_url": "https://example.com/hook"}, True),
        ({"contact_hours": {"mon": ["22:00-07:00"]}}, True),
        ({"contact_hours": {"mon": ["25:00-07:00"]}}, False),
        ({"contact_hours": {"someday": ["09:00-17:00"]}}, False),
        ({"contact_hours": {"mon": ["09:00-09:00"]}}, False),  # ambiguous: zero or 24 h
        ({"contact_hours": None}, True),  # any time
        ({"contact_hours": {}}, True),  # never
        ({"deadlines": {"live_ms": None}}, False),
        ({"unknown_knob": 1}, False),
        ({"deadlines": {"unknown": 1}}, False),
        ({"deadlines": 5}, False),
    ],
)
def test_validate_layer(partial: dict[str, Any], ok: bool) -> None:
    if ok:
        validate_layer(partial)
    else:
        with pytest.raises(ValidationError):
            validate_layer(partial)


@pytest.mark.parametrize(
    ("stored", "patch", "expected"),
    [
        ({}, {"after_hours": "on_call"}, {"after_hours": "on_call"}),
        ({}, {"contact_hours": None}, {"contact_hours": None}),  # null is a value: any time
        (
            {"escalation": {"two_person_over": 500}},
            {"escalation": {"two_person_over": None}},
            {"escalation": {"two_person_over": None}},
        ),
        (
            {"deadlines": {"live_ms": 9000}},
            {"deadlines": {"async_ms": 60000}},
            {"deadlines": {"live_ms": 9000, "async_ms": 60000}},
        ),
        (
            {"contact_hours": {"mon": ["22:00-07:00"]}},
            {"contact_hours": {"tue": ["22:00-07:00"]}},
            {"contact_hours": {"tue": ["22:00-07:00"]}},
        ),
    ],
)
def test_apply_patch(
    stored: dict[str, Any], patch: dict[str, Any], expected: dict[str, Any]
) -> None:
    assert apply_patch(stored, patch) == expected


def test_request_can_only_tighten() -> None:
    merged, origins = merge([("location:l", {"deadlines": {"live_ms": 20000}})])
    tighten(merged, origins, {"deadlines": {"live_ms": 8000}, "escalation": {"max_fanout": 9}})
    assert merged["deadlines"]["live_ms"] == 8000
    assert origins["deadlines.live_ms"] == "request"
    assert merged["escalation"]["max_fanout"] == 3  # raising is ignored
    assert origins["escalation.max_fanout"] == "default"


@pytest.mark.parametrize(
    "request_override",
    [{"on_timeout": {"outcome": "approve"}}, {"after_hours": {"x": 1}}, {"deadlines": {"foo": 1}}],
)
def test_request_cannot_touch_other_settings(request_override: dict[str, Any]) -> None:
    merged, origins = merge([])
    with pytest.raises(ValueError):
        tighten(merged, origins, request_override)


async def test_resolve_full_chain_with_policy_and_request(session: AsyncSession, make_tree) -> None:
    t = await make_tree("A")
    t.agency.settings = {
        "deadlines": {"live_ms": 25000, "async_ms": 120000},
        "after_hours": "on_call",
    }
    t.business.settings = {"deadlines": {"live_ms": 20000}}
    t.location.settings = {"escalation": {"max_fanout": 2}}
    policy = Policy(
        agency_id=t.agency.id,
        scope_type="business",
        scope_id=t.business.id,
        decision_type="refund",
        version=3,
        body={"rules": [], "default": "escalate", "overrides": {"after_hours": "defer"}},
        source="api",
    )
    session.add(policy)
    await session.flush()

    r = await resolve(session, t.location.id, "refund", {"deadlines": {"live_ms": 10000}})
    assert r is not None and r.policy is not None and r.policy.id == policy.id
    s = r.settings
    assert (s.deadlines.live_ms, s.deadlines.async_ms, s.escalation.max_fanout) == (
        10000,
        120000,
        2,
    )
    assert s.after_hours == "defer"
    assert r.origins["deadlines.live_ms"] == "request"
    assert r.origins["deadlines.async_ms"] == f"agency:{t.agency.id}"
    assert r.origins["escalation.max_fanout"] == f"location:{t.location.id}"
    assert r.origins["after_hours"] == f"policy:{policy.id}@3"
    assert r.origins["on_timeout.outcome"] == "default"


async def test_resolve_request_below_floor_is_rejected(session: AsyncSession, make_tree) -> None:
    t = await make_tree("A")
    with pytest.raises(ValidationError):
        await resolve(session, t.location.id, request={"deadlines": {"live_ms": 100}})


async def test_resolve_unknown_location(session: AsyncSession) -> None:
    assert await resolve(session, "loc_missing") is None


@pytest.mark.parametrize("layer", ["agency", "business", "location"])
async def test_patch_rejects_timeout_approve_at_every_scope(
    client: httpx.AsyncClient, make_tree, layer: str
) -> None:
    t = await make_tree("A")
    scope_id = {"agency": t.agency.id, "business": t.business.id, "location": t.location.id}[layer]
    r = await client.patch(
        f"/v1/settings/{layer}/{scope_id}",
        json={"on_timeout": {"outcome": "approve"}},
        headers=auth(t.agency_key),
    )
    assert r.status_code == 422
    assert r.headers["content-type"] == "application/problem+json"
    assert r.json()["errors"][0]["loc"] == ["on_timeout", "outcome"]
    assert (
        await client.get(f"/v1/settings/{layer}/{scope_id}", headers=auth(t.agency_key))
    ).json() == {}


def test_policy_override_layer_rejects_timeout_approve() -> None:
    with pytest.raises(ValidationError):
        validate_layer({"on_timeout": {"outcome": "approve"}})  # the check policy writes will use


async def test_settings_api_patch_and_resolved_origins(
    client: httpx.AsyncClient, make_tree
) -> None:
    t = await make_tree("A")
    h = auth(t.agency_key)
    r = await client.patch(
        f"/v1/settings/agency/{t.agency.id}", json={"deadlines": {"live_ms": 20000}}, headers=h
    )
    assert r.status_code == 200 and r.json() == {"deadlines": {"live_ms": 20000}}
    r = await client.patch(
        f"/v1/settings/location/{t.location.id}",
        json={"deadlines": {"live_ms": 15000}, "after_hours": "on_call"},
        headers=auth(t.location_key),
    )
    assert r.status_code == 200

    r = await client.get("/v1/settings/resolved", params={"location_id": t.location.id}, headers=h)
    body = r.json()
    assert body["settings"]["deadlines"] == {"live_ms": 15000, "async_ms": 3600000}
    assert body["origins"]["deadlines.live_ms"] == f"location:{t.location.id}"
    assert body["origins"]["deadlines.async_ms"] == "default"
    assert body["origins"]["after_hours"] == f"location:{t.location.id}"

    # The sibling location still inherits the agency value.
    r = await client.get(
        "/v1/settings/resolved", params={"location_id": t.other_location.id}, headers=h
    )
    assert r.json()["settings"]["deadlines"]["live_ms"] == 20000
    assert r.json()["origins"]["deadlines.live_ms"] == f"agency:{t.agency.id}"

    # null is a value: this location may text people at any time.
    r = await client.patch(
        f"/v1/settings/location/{t.location.id}", json={"contact_hours": None}, headers=h
    )
    assert r.json()["contact_hours"] is None
    r = await client.get("/v1/settings/resolved", params={"location_id": t.location.id}, headers=h)
    assert r.json()["settings"]["contact_hours"] is None
    assert r.json()["origins"]["contact_hours"] == f"location:{t.location.id}"

    # PUT replaces the overrides; whatever it leaves out inherits again.
    r = await client.put(
        f"/v1/settings/location/{t.location.id}", json={"after_hours": "on_call"}, headers=h
    )
    assert r.status_code == 200 and r.json() == {"after_hours": "on_call"}
    r = await client.get("/v1/settings/resolved", params={"location_id": t.location.id}, headers=h)
    assert r.json()["settings"]["deadlines"]["live_ms"] == 20000
    assert r.json()["settings"]["contact_hours"]["mon"] == ["07:00-21:00"]
    assert r.json()["origins"]["contact_hours"] == "default"
    r = await client.put(
        f"/v1/settings/location/{t.location.id}",
        json={"on_timeout": {"outcome": "approve"}},
        headers=h,
    )
    assert r.status_code == 422
