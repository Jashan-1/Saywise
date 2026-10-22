import httpx

from app.core.security import sign_payload, verify_payload
from tests.conftest import auth

BODY = b'{"event":"decision.late_answer"}'
NOW = 1_800_000_000


async def test_issue_list_and_revoke_keys(client: httpx.AsyncClient, make_tree) -> None:
    t = await make_tree("A")
    h = auth(t.agency_key)
    r = await client.post(
        "/v1/api_keys", json={"scope_type": "location", "scope_id": t.location.id}, headers=h
    )
    assert r.status_code == 201
    created = r.json()
    assert created["key"].startswith("sk_live_") and created["key"].startswith(created["prefix"])

    # The new key works, and only inside its location.
    new = auth(created["key"])
    assert (await client.get(f"/v1/locations/{t.location.id}", headers=new)).status_code == 200
    assert (
        await client.get(f"/v1/locations/{t.other_location.id}", headers=new)
    ).status_code == 404

    listed = (await client.get("/v1/api_keys", headers=h)).json()
    assert created["id"] in {k["id"] for k in listed}
    assert all("key" not in k and "key_hash" not in k for k in listed)

    r = await client.post(f"/v1/api_keys/{created['id']}/revoke", headers=h)
    assert r.status_code == 200 and r.json()["revoked_at"]
    assert (await client.get(f"/v1/locations/{t.location.id}", headers=new)).status_code == 401
    again = await client.post(f"/v1/api_keys/{created['id']}/revoke", headers=h)
    assert again.json()["revoked_at"] == r.json()["revoked_at"]  # idempotent


async def test_a_key_cannot_revoke_itself(client: httpx.AsyncClient, make_tree) -> None:
    t = await make_tree("A")
    h = auth(t.agency_key)
    own = next(
        k
        for k in (await client.get("/v1/api_keys", headers=h)).json()
        if k["scope_type"] == "agency"
    )
    r = await client.post(f"/v1/api_keys/{own['id']}/revoke", headers=h)
    assert r.status_code == 409
    assert (await client.get("/v1/api_keys", headers=h)).status_code == 200


async def test_key_management_is_agency_keys_only(client: httpx.AsyncClient, make_tree) -> None:
    a, b = await make_tree("A"), await make_tree("B")
    for key in (a.business_key, a.location_key):
        assert (await client.get("/v1/api_keys", headers=auth(key))).status_code == 404
        r = await client.post(
            "/v1/api_keys",
            json={"scope_type": "location", "scope_id": a.location.id},
            headers=auth(key),
        )
        assert r.status_code == 404
        assert (
            await client.post("/v1/callback_secret/rotate", headers=auth(key))
        ).status_code == 404

    # Another agency can't issue keys for, list, or revoke ours.
    r = await client.post(
        "/v1/api_keys",
        json={"scope_type": "location", "scope_id": a.location.id},
        headers=auth(b.agency_key),
    )
    assert r.status_code == 404
    a_ids = {k["id"] for k in (await client.get("/v1/api_keys", headers=auth(a.agency_key))).json()}
    b_ids = {k["id"] for k in (await client.get("/v1/api_keys", headers=auth(b.agency_key))).json()}
    assert not a_ids & b_ids
    for key_id in a_ids:
        r = await client.post(f"/v1/api_keys/{key_id}/revoke", headers=auth(b.agency_key))
        assert r.status_code == 404


async def test_callback_secret_is_per_agency_and_rotates(
    client: httpx.AsyncClient, make_tree
) -> None:
    a, b = await make_tree("A"), await make_tree("B")
    ra = (await client.post("/v1/callback_secret/rotate", headers=auth(a.agency_key))).json()
    rb = (await client.post("/v1/callback_secret/rotate", headers=auth(b.agency_key))).json()
    assert ra["version"] == rb["version"] == 2
    secret_a, secret_b = ra["secret"].encode(), rb["secret"].encode()
    assert secret_a != secret_b

    # Agency A's secret can't verify a callback signed for agency B, and vice versa.
    header_b = sign_payload(BODY, secret_b, NOW)
    assert verify_payload(BODY, header_b, secret_b, now=NOW)
    assert not verify_payload(BODY, header_b, secret_a, now=NOW)
    assert not verify_payload(BODY, sign_payload(BODY, secret_a, NOW), secret_b, now=NOW)

    # Rotating A again changes only A's secret.
    ra2 = (await client.post("/v1/callback_secret/rotate", headers=auth(a.agency_key))).json()
    assert ra2["version"] == 3 and ra2["secret"] != ra["secret"]
    assert not verify_payload(
        BODY, sign_payload(BODY, secret_a, NOW), ra2["secret"].encode(), now=NOW
    )
