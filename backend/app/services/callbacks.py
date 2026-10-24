"""Signed outbound webhooks (§7, §16) through the callback_deliveries outbox. Callers get every
final state change and late answers; SaySo never messages their customers itself."""

import asyncio
import ipaddress
import json
import time
from datetime import timedelta
from typing import Any
from urllib.parse import urlsplit

import httpx
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.constants import CALLBACK_BACKOFF, CALLBACK_MAX_ATTEMPTS, CALLBACK_MAX_BACKOFF
from app.core.logging import log
from app.core.security import callback_secret, sign_payload
from app.models import Agency, CallbackDelivery, Decision, utcnow
from app.services.policy_engine import format_value


def is_public(ip: str) -> bool:
    return ipaddress.ip_address(ip).is_global


async def safe_target(url: str) -> bool:
    """SSRF guard: HTTPS to a host whose every address is public."""
    parts = urlsplit(url)
    if parts.scheme != "https" or not parts.hostname:
        return False
    try:
        infos = await asyncio.get_running_loop().getaddrinfo(parts.hostname, parts.port or 443)
    except OSError:
        return False
    # shortcut: checks the resolved addresses, not the one httpx connects to (DNS rebinding);
    # pin the checked IP in the connection if callbacks ever reach untrusted URLs at scale.
    return bool(infos) and all(is_public(str(info[4][0])) for info in infos)


def backoff(attempt: int) -> timedelta:
    return min(CALLBACK_BACKOFF * (1 << (attempt - 1)), CALLBACK_MAX_BACKOFF)  # 30 s, 60 s, ... 1 h


def enqueue(session: AsyncSession, decision: Decision, event: str) -> None:
    if decision.callback_url:
        session.add(
            CallbackDelivery(decision_id=decision.id, event=event, next_attempt_at=utcnow())
        )


def payload(decision: Decision, event: str) -> bytes:
    def money(v: Any) -> float | None:
        return None if v is None else float(format_value(v))

    body = {
        "event": event,
        "decision": {
            "id": decision.id,
            "location_id": decision.location_id,
            "type": decision.type,
            "customer_ref": decision.customer_ref,
            "status": decision.status,
            "outcome": decision.outcome,
            "value": money(decision.value),
            "decided_by": decision.decided_by,
            "say": decision.say,
            "reason": decision.reason,
            "late_outcome": decision.late_outcome,
            "late_value": money(decision.late_value),
            "decided_at": decision.decided_at.isoformat() if decision.decided_at else None,
        },
    }
    return json.dumps(body).encode()


async def deliver(
    session: AsyncSession, delivery: CallbackDelivery, client: httpx.AsyncClient
) -> None:
    """One attempt. Success marks it delivered; failure backs off, up to CALLBACK_MAX_ATTEMPTS."""
    decision = await session.get_one(Decision, delivery.decision_id)
    agency = await session.get_one(Agency, decision.agency_id)
    url = decision.callback_url or ""
    body = payload(decision, delivery.event)
    signing = get_settings().signing_secret.get_secret_value().encode()
    secret = callback_secret(signing, agency.id, agency.callback_secret_version).encode()
    delivery.attempt += 1
    ok = False
    if await safe_target(url):
        try:
            r = await client.post(
                url,
                content=body,
                headers={
                    "Content-Type": "application/json",
                    "SaySo-Event": delivery.event,
                    "SaySo-Signature": sign_payload(body, secret, int(time.time())),
                },
            )
            delivery.status_code, ok = r.status_code, r.is_success
        except httpx.HTTPError:
            pass
    else:
        log.warning("callback target refused", extra={"decision_id": decision.id})
    now = utcnow()
    if ok:
        delivery.delivered_at, delivery.next_attempt_at = now, None
    elif delivery.attempt >= CALLBACK_MAX_ATTEMPTS:
        delivery.next_attempt_at = None
        log.warning("callback gave up", extra={"decision_id": decision.id})
    else:
        delivery.next_attempt_at = now + backoff(delivery.attempt)
    await session.commit()
