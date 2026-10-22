import httpx


async def test_healthz(client: httpx.AsyncClient) -> None:
    r = await client.get("/healthz")
    assert r.status_code == 200
    assert r.json() == {"status": "ok"}
    assert r.headers["x-request-id"]


async def test_readyz_pings_db(client: httpx.AsyncClient) -> None:
    r = await client.get("/readyz")
    assert r.status_code == 200


async def test_request_id_is_echoed(client: httpx.AsyncClient) -> None:
    r = await client.get("/healthz", headers={"x-request-id": "abc123"})
    assert r.headers["x-request-id"] == "abc123"
