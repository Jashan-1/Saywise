import json
from pathlib import Path
from typing import Any

import httpx
import pytest

from tests.conftest import auth

EXAMPLE = json.loads(
    (Path(__file__).parents[2] / "examples" / "policies" / "price_adjustment.json").read_text()
)


def body(label: str, **kw: Any) -> dict[str, Any]:
    return {"label": label, "rules": [], "default": "escalate", **kw}


async def test_templates_are_seeded(client: httpx.AsyncClient, make_tree) -> None:
    t = await make_tree("A")
    r = await client.get("/v1/policies/templates", headers=auth(t.location_key))
    assert r.status_code == 200
    templates = r.json()
    assert [p["decision_type"] for p in templates] == [
        "booking_exception",
        "price_adjustment",
        "refund",
        "trade_in_offer",
    ]
    assert {(p["scope_type"], p["scope_id"], p["source"], p["version"]) for p in templates} == {
        ("system", "system", "template", 1)
    }


async def test_nearest_scope_wins(client: httpx.AsyncClient, make_tree) -> None:
    t = await make_tree("A")
    h = auth(t.agency_key)

    async def label_at(key: str, scope: str | None = None) -> str:
        params = {"scope": scope} if scope else {}
        r = await client.get("/v1/policies/refund", params=params, headers=auth(key))
        assert r.status_code == 200
        return r.json()["body"]["label"]

    assert await label_at(t.location_key) == "Refund"  # system template
    for scope, label in (
        ("agency", t.agency.id),
        ("business", t.business.id),
        ("location", t.location.id),
    ):
        r = await client.put(
            "/v1/policies/refund", params={"scope": f"{scope}:{label}"}, json=body(scope), headers=h
        )
        assert r.status_code == 200, r.text
        assert await label_at(t.location_key) == scope
    assert await label_at(t.agency_key, f"location:{t.other_location.id}") == "business"
    assert await label_at(t.agency_key) == "agency"


async def test_versions_are_immutable(client: httpx.AsyncClient, make_tree) -> None:
    t = await make_tree("A")
    h, scope = auth(t.business_key), {"scope": f"business:{t.business.id}"}
    v1 = (
        await client.put("/v1/policies/refund", params=scope, json=body("first"), headers=h)
    ).json()
    v2 = (
        await client.put("/v1/policies/refund", params=scope, json=body("second"), headers=h)
    ).json()
    assert (v1["version"], v2["version"]) == (1, 2)
    assert v2["created_by"] and v2["source"] == "api"

    versions = (await client.get("/v1/policies/refund/versions", params=scope, headers=h)).json()
    assert [(v["version"], v["active"], v["body"]["label"]) for v in versions] == [
        (2, True, "second"),
        (1, False, "first"),
    ]
    assert versions[1]["id"] == v1["id"] and versions[1]["body"] == v1["body"]


async def test_clone_is_an_independent_copy(client: httpx.AsyncClient, make_tree) -> None:
    t = await make_tree("A")
    h = auth(t.agency_key)
    template = next(
        p
        for p in (await client.get("/v1/policies/templates", headers=h)).json()
        if p["decision_type"] == "price_adjustment"
    )

    r = await client.post(
        "/v1/policies/price_adjustment/clone",
        params={"to_scope": f"business:{t.business.id}"},
        headers=h,
    )
    assert r.status_code == 201
    clone = r.json()
    assert clone["body"] == template["body"] and clone["source"] == "clone"
    assert (clone["scope_type"], clone["scope_id"], clone["version"]) == (
        "business",
        t.business.id,
        1,
    )

    # Editing the clone leaves the template alone.
    edited = {**clone["body"], "label": "Our discounts"}
    await client.put(
        "/v1/policies/price_adjustment",
        params={"scope": f"business:{t.business.id}"},
        json=edited,
        headers=h,
    )
    again = next(
        p
        for p in (await client.get("/v1/policies/templates", headers=h)).json()
        if p["decision_type"] == "price_adjustment"
    )
    assert again == template

    # Cloning to the location copies the business's edited version, not the template.
    r = await client.post(
        "/v1/policies/price_adjustment/clone",
        params={"to_scope": f"location:{t.location.id}"},
        headers=h,
    )
    assert r.json()["body"]["label"] == "Our discounts"
    r = await client.post(
        "/v1/policies/price_adjustment/clone",
        params={"to_scope": f"location:{t.location.id}"},
        headers=h,
    )
    assert r.status_code == 409
    r = await client.post(
        "/v1/policies/unknown_type/clone",
        params={"to_scope": f"location:{t.location.id}"},
        headers=h,
    )
    assert r.status_code == 404


async def test_brand_new_type(client: httpx.AsyncClient, make_tree) -> None:
    t = await make_tree("A")
    h = auth(t.agency_key)
    assert (await client.get("/v1/policies/warranty_extension", headers=h)).status_code == 404
    r = await client.put("/v1/policies/warranty_extension", json=body("Warranty"), headers=h)
    assert r.status_code == 200
    assert r.json()["body"]["decision_type"] == "warranty_extension"
    assert (r.json()["scope_type"], r.json()["scope_id"]) == ("agency", t.agency.id)


BAD_BODIES = [
    body(
        "x", rules=[{"id": "r", "when": {"var": "amount", "op": "<", "value": 1}, "then": "maybe"}]
    ),
    body(
        "x",
        rules=[{"id": "r", "when": {"var": "amount", "op": "<", "value": 1}, "then": "counter"}],
    ),
    body(
        "x",
        rules=[
            {
                "id": "r",
                "when": {"var": "amount", "op": "<", "value": 1},
                "then": "approve",
                "value": 5,
            }
        ],
    ),
    body(
        "x",
        rules=[{"id": "r", "when": {"var": "amount", "op": "~", "value": 1}, "then": "approve"}],
    ),
    body(
        "x",
        rules=[
            {"id": "r", "when": {"var": "customer.name", "op": "==", "value": 1}, "then": "approve"}
        ],
    ),
    body("x", rules=[{"id": "r", "when": {"all": []}, "then": "approve"}]),
    body(
        "x",
        rules=[{"id": "r", "when": {"var": "amount", "op": "<", "value": 1}, "then": "approve"}]
        * 2,
    ),
    body("x", rules=[{"id": "r", "when": "amount < 1", "then": "approve"}]),
    body("x", default="counter"),
    body("x", overrides={"on_timeout": {"outcome": "approve"}}),  # the policy layer floor
    body("x", overrides={"deadlines": {"live_ms": 1}}),
    body("x", surprise=True),
    body("x", decision_type="refund"),  # doesn't match the URL
    {"rules": []},  # no label
]


@pytest.mark.parametrize("bad", BAD_BODIES)
async def test_invalid_policies_are_rejected(
    client: httpx.AsyncClient, make_tree, bad: dict[str, Any]
) -> None:
    t = await make_tree("A")
    r = await client.put("/v1/policies/price_adjustment", json=bad, headers=auth(t.agency_key))
    assert r.status_code == 422, bad
    assert r.headers["content-type"] == "application/problem+json"
    assert (
        await client.get("/v1/policies/price_adjustment/versions", headers=auth(t.agency_key))
    ).json() == []


async def test_example_policy_round_trips(client: httpx.AsyncClient, make_tree) -> None:
    t = await make_tree("A")
    r = await client.put("/v1/policies/price_adjustment", json=EXAMPLE, headers=auth(t.agency_key))
    assert r.status_code == 200
    assert r.json()["body"] == EXAMPLE
    resolved = await client.get(
        "/v1/settings/resolved",
        params={"location_id": t.location.id, "decision_type": "price_adjustment"},
        headers=auth(t.agency_key),
    )
    assert resolved.json()["settings"]["deadlines"]["live_ms"] == 25000
    assert resolved.json()["origins"]["after_hours"] == f"policy:{r.json()['id']}@1"


async def test_policy_scope_rules(client: httpx.AsyncClient, make_tree) -> None:
    a, b = await make_tree("A"), await make_tree("B")

    def put(key: str, scope: str):
        return client.put(
            "/v1/policies/refund", params={"scope": scope}, json=body("x"), headers=auth(key)
        )

    assert (
        await put(a.location_key, f"business:{a.business.id}")
    ).status_code == 404  # above the key
    assert (
        await put(a.location_key, f"location:{a.other_location.id}")
    ).status_code == 404  # sibling
    assert (await put(b.agency_key, f"location:{a.location.id}")).status_code == 404  # other agency
    assert (await put(a.business_key, f"location:{a.location.id}")).status_code == 200
    for bad in ("system:system", "business", "planet:earth", "business:"):
        assert (await put(a.agency_key, bad)).status_code == 422
    r = await client.put("/v1/policies/templates", json=body("x"), headers=auth(a.agency_key))
    assert r.status_code == 422
    r = await client.get("/v1/policies/Not-A-Type", headers=auth(a.agency_key))
    assert r.status_code == 422
    r = await client.get(
        "/v1/policies/refund/versions",
        params={"scope": f"location:{a.location.id}"},
        headers=auth(b.agency_key),
    )
    assert r.status_code == 404
