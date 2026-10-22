import httpx
import pytest
from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import ApiKey, utcnow
from tests.conftest import auth


async def test_bad_or_missing_key_is_401(client: httpx.AsyncClient, make_tree) -> None:
    t = await make_tree("A")
    for headers in (
        {},
        auth("sk_live_nope"),
        auth(t.agency_key + "x"),
        {"Authorization": t.agency_key},
    ):
        r = await client.get("/v1/businesses", headers=headers)
        assert r.status_code == 401
        assert r.json()["title"] == "Unauthorized"


async def test_revoked_key_is_401(
    client: httpx.AsyncClient, session: AsyncSession, make_tree
) -> None:
    t = await make_tree("A")
    await session.execute(update(ApiKey).values(revoked_at=utcnow()))
    await session.commit()
    assert (await client.get("/v1/businesses", headers=auth(t.agency_key))).status_code == 401


async def test_agency_key_builds_org(client: httpx.AsyncClient, make_tree) -> None:
    t = await make_tree("A")
    h = auth(t.agency_key)
    r = await client.post("/v1/businesses", json={"name": "Demo Motors"}, headers=h)
    assert r.status_code == 201
    bus = r.json()
    assert bus["id"].startswith("bus_") and bus["agency_id"] == t.agency.id

    r = await client.post(
        "/v1/locations",
        json={"business_id": bus["id"], "name": "Fremont", "timezone": "America/Los_Angeles"},
        headers=h,
    )
    assert r.status_code == 201 and r.json()["id"].startswith("loc_")
    r = await client.patch(
        f"/v1/locations/{r.json()['id']}", json={"name": "Fremont Ford"}, headers=h
    )
    assert r.json()["name"] == "Fremont Ford"

    for tz in ("Mars/Olympus", "America", "", "../etc/passwd"):
        r = await client.post(
            "/v1/locations", json={"business_id": bus["id"], "name": "x", "timezone": tz}, headers=h
        )
        assert r.status_code == 422, tz
    r = await client.patch(f"/v1/businesses/{bus['id']}", json={"name": None}, headers=h)
    assert r.status_code == 422


@pytest.mark.parametrize(
    "path",
    [
        "/v1/businesses/{a.business.id}",
        "/v1/locations/{a.location.id}",
        "/v1/settings/agency/{a.agency.id}",
        "/v1/settings/business/{a.business.id}",
        "/v1/settings/location/{a.location.id}",
        "/v1/settings/resolved?location_id={a.location.id}",
    ],
)
async def test_other_agency_gets_404(client: httpx.AsyncClient, make_tree, path: str) -> None:
    a, b = await make_tree("A"), await make_tree("B")
    url = path.format(a=a)
    assert (await client.get(url, headers=auth(a.agency_key))).status_code == 200
    for key in (b.agency_key, b.business_key, b.location_key):
        r = await client.get(url, headers=auth(key))
        assert r.status_code == 404
        assert r.json() == {
            "type": "about:blank",
            "title": "Not Found",
            "status": 404,
            "detail": "Not found",
        }
    r = await client.patch(url.split("?")[0], json={"name": "pwned"}, headers=auth(b.agency_key))
    assert r.status_code in (404, 405)


async def test_other_agency_cannot_create_under_us(client: httpx.AsyncClient, make_tree) -> None:
    a, b = await make_tree("A"), await make_tree("B")
    r = await client.post(
        "/v1/locations",
        json={"business_id": a.business.id, "name": "x", "timezone": "UTC"},
        headers=auth(b.agency_key),
    )
    assert r.status_code == 404
    assert (await client.get("/v1/businesses", headers=auth(b.agency_key))).json()[0][
        "id"
    ] == b.business.id
    assert {
        x["id"] for x in (await client.get("/v1/locations", headers=auth(b.agency_key))).json()
    } == {
        b.location.id,
        b.other_location.id,
    }


async def test_outside_subtree_is_404(client: httpx.AsyncClient, make_tree) -> None:
    t = await make_tree("A")
    loc_key, bus_key = auth(t.location_key), auth(t.business_key)

    # A location key sees only its own location.
    assert (await client.get(f"/v1/locations/{t.location.id}", headers=loc_key)).status_code == 200
    assert (
        await client.get(f"/v1/locations/{t.other_location.id}", headers=loc_key)
    ).status_code == 404
    assert (await client.get(f"/v1/businesses/{t.business.id}", headers=loc_key)).status_code == 404
    assert (
        await client.get(f"/v1/settings/agency/{t.agency.id}", headers=loc_key)
    ).status_code == 404
    assert (await client.get("/v1/businesses", headers=loc_key)).json() == []
    assert [x["id"] for x in (await client.get("/v1/locations", headers=loc_key)).json()] == [
        t.location.id
    ]
    r = await client.patch(
        f"/v1/settings/location/{t.other_location.id}", json={"locale": "en"}, headers=loc_key
    )
    assert r.status_code == 404

    # A business key sees its business and everything under it, but not the agency.
    assert (
        await client.get(f"/v1/locations/{t.other_location.id}", headers=bus_key)
    ).status_code == 200
    assert (
        await client.get(f"/v1/settings/agency/{t.agency.id}", headers=bus_key)
    ).status_code == 404
    r = await client.patch(
        f"/v1/settings/business/{t.business.id}", json={"locale": "en"}, headers=bus_key
    )
    assert r.status_code == 200


async def test_only_agency_keys_shape_the_org(client: httpx.AsyncClient, make_tree) -> None:
    t = await make_tree("A")
    bus_key = auth(t.business_key)
    r = await client.post("/v1/businesses", json={"name": "x"}, headers=bus_key)
    assert r.status_code == 404  # the agency is outside a business key's subtree
    r = await client.post(
        "/v1/locations",
        json={"business_id": t.business.id, "name": "x", "timezone": "UTC"},
        headers=bus_key,
    )
    assert r.status_code == 403
    r = await client.patch(
        f"/v1/locations/{t.location.id}", json={"name": "x"}, headers=auth(t.location_key)
    )
    assert r.status_code == 403


async def test_key_scope_id_from_another_agency_grants_nothing(
    client: httpx.AsyncClient, session: AsyncSession, make_tree
) -> None:
    a, b = await make_tree("A"), await make_tree("B")
    location_id = a.location.id
    # A malformed key row: agency B's key pointing at agency A's location. The key's own
    # agency must also be on the target's chain, so it reaches nothing.
    row, key = ApiKey.issue(b.agency.id, "location", location_id)
    session.add(row)
    await session.commit()
    assert (await client.get("/v1/businesses", headers=auth(key))).json() == []
    assert (await client.get("/v1/locations", headers=auth(key))).json() == []
    r = await client.get(f"/v1/locations/{location_id}", headers=auth(key))
    assert r.status_code == 404
