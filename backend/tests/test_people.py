import httpx

from tests.conftest import auth


async def add_member(
    client: httpx.AsyncClient, key: str, phone: str, scope: tuple[str, str], name: str, **kw
):
    h = auth(key)
    person = (await client.post("/v1/people", json={"phone": phone}, headers=h)).json()
    body = {
        "person_id": person["id"],
        "scope_type": scope[0],
        "scope_id": scope[1],
        "display_name": name,
        "role": "manager",
        **kw,
    }
    return person, await client.post("/v1/memberships", json=body, headers=h)


async def test_shared_person_leaks_no_names(client: httpx.AsyncClient, make_tree) -> None:
    a, b = await make_tree("A"), await make_tree("B")
    phone = "+14085550100"
    person_a, r = await add_member(
        client, a.agency_key, phone, ("business", a.business.id), "Raj (GM)", authority_limit=200
    )
    assert r.status_code == 201
    assert r.json()["authority_limit"] == 200.0

    # Agency B registers the same phone: same global person, and nothing of A's comes back.
    person_b, r = await add_member(
        client, b.agency_key, phone, ("location", b.location.id), "R. Patel"
    )
    assert r.status_code == 201
    assert person_b == person_a == {"id": person_a["id"], "phone": phone}

    for tree, own_name in ((a, "Raj (GM)"), (b, "R. Patel")):
        for key in (tree.agency_key, tree.business_key):
            members = (await client.get("/v1/memberships", headers=auth(key))).json()
            assert [m["display_name"] for m in members] == [own_name]
            people = (await client.get("/v1/people", headers=auth(key))).json()
            assert people == [person_a]

    a_membership = (await client.get("/v1/memberships", headers=auth(a.agency_key))).json()[0]["id"]
    for key in (b.agency_key, b.business_key, b.location_key):
        assert (
            await client.get(f"/v1/memberships/{a_membership}", headers=auth(key))
        ).status_code == 404
        r = await client.patch(
            f"/v1/memberships/{a_membership}", json={"display_name": "x"}, headers=auth(key)
        )
        assert r.status_code == 404
    r = await client.get(
        "/v1/memberships", params={"person_id": person_a["id"]}, headers=auth(b.agency_key)
    )
    assert [m["display_name"] for m in r.json()] == ["R. Patel"]


async def test_people_lists_follow_the_key_subtree(client: httpx.AsyncClient, make_tree) -> None:
    t = await make_tree("A")
    await add_member(client, t.agency_key, "+14085550101", ("location", t.other_location.id), "Sam")
    assert (await client.get("/v1/memberships", headers=auth(t.location_key))).json() == []
    assert (await client.get("/v1/people", headers=auth(t.location_key))).json() == []
    assert len((await client.get("/v1/memberships", headers=auth(t.business_key))).json()) == 1


async def test_membership_rules(client: httpx.AsyncClient, make_tree) -> None:
    t = await make_tree("A")
    h, loc = auth(t.agency_key), ("location", t.location.id)  # the 409 below expires `t` rows
    person, r = await add_member(client, t.agency_key, "+14085550102", loc, "Lee")
    m = r.json()
    assert (m["can_approve"], m["on_call"], m["active"], m["on_hours"]) == (True, False, True, None)

    _, dup = await add_member(client, t.agency_key, "+14085550102", loc, "Lee again")
    assert dup.status_code == 409

    _, admin = await add_member(client, t.agency_key, "+14085550103", loc, "X", role="agency_admin")
    assert admin.status_code == 422
    r = await client.patch(f"/v1/memberships/{m['id']}", json={"role": "agency_admin"}, headers=h)
    assert r.status_code == 422

    r = await client.patch(
        f"/v1/memberships/{m['id']}",
        json={"authority_limit": 150.5, "on_hours": {"mon": ["09:00-17:00"]}, "on_call": True},
        headers=h,
    )
    assert r.status_code == 200
    assert (r.json()["authority_limit"], r.json()["on_hours"], r.json()["on_call"]) == (
        150.5,
        {"mon": ["09:00-17:00"]},
        True,
    )
    r = await client.patch(f"/v1/memberships/{m['id']}", json={"on_hours": None}, headers=h)
    assert r.json()["on_hours"] is None
    for bad in (
        {"display_name": None},
        {"authority_limit": -1},
        {"on_hours": {"mon": ["9-5"]}},
        {"scope_id": "x"},
    ):
        assert (
            await client.patch(f"/v1/memberships/{m['id']}", json=bad, headers=h)
        ).status_code == 422

    r = await client.post(
        "/v1/memberships",
        json={
            **{k: m[k] for k in ("scope_type", "scope_id", "display_name", "role")},
            "person_id": "per_missing",
        },
        headers=h,
    )
    assert r.status_code == 404
    for phone in ("4085550100", "+0123", "+1 408 555 0100"):
        assert (
            await client.post("/v1/people", json={"phone": phone}, headers=h)
        ).status_code == 422
