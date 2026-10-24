import json
from datetime import timedelta

import httpx
import pytest
from sqlalchemy import select, update

from app.core.config import get_settings
from app.core.db import SessionLocal
from app.core.security import callback_secret, verify_payload
from app.models import CallbackDelivery, utcnow
from app.services.callbacks import backoff, is_public, safe_target
from app.worker import deliver_callbacks, step_decisions
from tests.conftest import FakeChannel
from tests.test_worker import PAST, get, new_decision, shift, world


@pytest.mark.parametrize(
    ("ip", "public"),
    [
        ("8.8.8.8", True),
        ("2606:4700:4700::1111", True),
        ("127.0.0.1", False),
        ("10.1.2.3", False),
        ("172.16.0.1", False),
        ("192.168.1.1", False),
        ("169.254.169.254", False),  # cloud metadata
        ("100.64.0.1", False),  # carrier-grade NAT
        ("0.0.0.0", False),  # noqa: S104
        ("::1", False),
        ("fd00::1", False),
        ("::ffff:127.0.0.1", False),
    ],
)
def test_is_public(ip: str, public: bool) -> None:
    assert is_public(ip) is public


@pytest.mark.parametrize(
    ("url", "ok"),
    [
        ("https://8.8.8.8/hook", True),
        ("http://8.8.8.8/hook", False),  # https only
        ("https://127.0.0.1/hook", False),
        ("https://[::1]/hook", False),
        ("https://169.254.169.254/latest/meta-data", False),
        ("https://localhost/hook", False),
        ("https://nothing-here.invalid/hook", False),
        ("not a url", False),
    ],
)
async def test_safe_target(url: str, ok: bool) -> None:
    assert await safe_target(url) is ok


@pytest.mark.parametrize(
    ("attempt", "seconds"), [(1, 30), (2, 60), (3, 120), (7, 1920), (8, 3600), (20, 3600)]
)
def test_backoff(attempt: int, seconds: int) -> None:
    assert backoff(attempt) == timedelta(seconds=seconds)


async def expired_with_callback(url: str) -> tuple[str, str]:
    w, ch = await world(), FakeChannel()
    d = await new_decision(w, ch, callback_url=url)
    await shift(d, deadline_at=PAST, next_action_at=PAST)
    await step_decisions(FakeChannel())
    return w.agency_id, d


def recorder(status: int) -> tuple[list[httpx.Request], httpx.AsyncClient]:
    seen: list[httpx.Request] = []

    def handle(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(status)

    return seen, httpx.AsyncClient(transport=httpx.MockTransport(handle))


async def delivery_for(decision_id: str) -> CallbackDelivery:
    async with SessionLocal() as s:
        row = await s.scalar(
            select(CallbackDelivery).where(CallbackDelivery.decision_id == decision_id)
        )
        assert row is not None
        return row


async def test_callbacks_are_signed_with_the_agency_secret() -> None:
    agency_id, d = await expired_with_callback("https://8.8.8.8/hook")
    other_agency, _ = await expired_with_callback("https://8.8.8.8/other")
    seen, client = recorder(200)
    await deliver_callbacks(client)
    [request] = [r for r in seen if json.loads(r.content)["decision"]["id"] == d]
    assert request.headers["SaySo-Event"] == "decision.expired"
    body = json.loads(request.content)
    assert body["decision"]["status"] == "expired" and body["decision"]["outcome"] == "defer"

    signing = get_settings().signing_secret.get_secret_value().encode()
    ours = callback_secret(signing, agency_id, 1).encode()
    theirs = callback_secret(signing, other_agency, 1).encode()
    assert verify_payload(request.content, request.headers["SaySo-Signature"], ours)
    assert not verify_payload(request.content, request.headers["SaySo-Signature"], theirs)
    row = await delivery_for(d)
    assert row.delivered_at is not None and (row.attempt, row.status_code) == (1, 200)


async def test_failed_callbacks_back_off_then_give_up() -> None:
    _, d = await expired_with_callback("https://8.8.8.8/flaky")
    _, client = recorder(500)
    await deliver_callbacks(client)
    row = await delivery_for(d)
    assert (row.attempt, row.status_code, row.delivered_at) == (1, 500, None)
    assert row.next_attempt_at is not None
    assert timedelta(seconds=25) < row.next_attempt_at - utcnow() <= timedelta(seconds=30)
    for _ in range(10):
        async with SessionLocal() as s:
            await s.execute(
                update(CallbackDelivery)
                .where(CallbackDelivery.id == row.id, CallbackDelivery.next_attempt_at.is_not(None))
                .values(next_attempt_at=utcnow() + PAST)
            )
            await s.commit()
        await deliver_callbacks(client)
    row = await delivery_for(d)
    assert (row.attempt, row.next_attempt_at, row.delivered_at) == (8, None, None)


async def test_ssrf_targets_are_never_called() -> None:
    _, d = await expired_with_callback("https://10.0.0.5/hook")  # a valid https URL, private host
    seen, client = recorder(200)
    await deliver_callbacks(client)
    assert all("10.0.0.5" not in str(r.url) for r in seen)
    row = await delivery_for(d)
    assert (row.attempt, row.delivered_at, row.status_code) == (1, None, None)
    assert (await get(d)).status == "expired"
