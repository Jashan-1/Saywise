"""Create decisions (§7): resolve settings and policy, evaluate, persist. Writes are idempotent
per (agency, Idempotency-Key)."""

import hashlib
import json
from datetime import timedelta
from decimal import Decimal
from typing import Any

from sqlalchemy import func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.constants import WAIT_CHANNEL
from app.models import Decision, utcnow
from app.schemas import DecisionIn
from app.services.policy_engine import Verdict, evaluate
from app.services.settings import resolve as resolve_settings


class IdempotencyConflict(Exception):
    """The Idempotency-Key was already used for a different request."""


def request_hash(location_id: str, body: DecisionIn) -> str:
    canonical = json.dumps(
        {**body.model_dump(mode="json"), "location_id": location_id}, sort_keys=True
    )
    return hashlib.sha256(canonical.encode()).hexdigest()


async def _replay(
    session: AsyncSession, agency_id: str, idempotency_key: str, digest: str
) -> Decision | None:
    existing = await session.scalar(
        select(Decision).where(
            Decision.agency_id == agency_id, Decision.idempotency_key == idempotency_key
        )
    )
    if existing is not None and existing.request_hash != digest:
        raise IdempotencyConflict
    return existing


async def create(
    session: AsyncSession,
    agency_id: str,
    location_id: str,
    body: DecisionIn,
    idempotency_key: str | None,
) -> Decision:
    """Raises IdempotencyConflict, or pydantic.ValidationError if the request's tighten-only
    overrides go below a floor."""
    digest = request_hash(location_id, body)
    if idempotency_key is not None:
        existing = await _replay(session, agency_id, idempotency_key, digest)
        if existing is not None:
            return existing

    request: dict[str, dict[str, int]] = {}
    if body.deadline_ms is not None:
        request["deadlines"] = {f"{body.mode}_ms": body.deadline_ms}
    if body.max_fanout is not None:
        request["escalation"] = {"max_fanout": body.max_fanout}
    resolved = await resolve_settings(session, location_id, body.type, request)
    if resolved is None:
        raise LookupError(location_id)
    settings, policy = resolved.settings, resolved.policy

    facts: dict[str, Any] = {"type": body.type, "context": body.context, "history": {}}
    if body.subject.amount is not None:
        facts["amount"] = body.subject.amount
    verdict = evaluate(policy.body, facts) if policy else Verdict("escalate", "no_policy")

    now = utcnow()
    deadline_ms = settings.deadlines.live_ms if body.mode == "live" else settings.deadlines.async_ms
    callback_url = body.callback_url or settings.callback_url
    decision = Decision(
        agency_id=agency_id,
        location_id=location_id,
        type=body.type,
        mode=body.mode,
        summary=body.summary,
        subject=body.subject.model_dump(mode="json"),
        context=body.context,
        customer_ref=body.subject.customer_ref,
        amount=body.subject.amount,
        policy_id=policy.id if policy else None,
        policy_version=policy.version if policy else None,
        rule_id=verdict.rule_id,
        reason=verdict.reason,
        deadline_at=now + timedelta(milliseconds=deadline_ms),
        callback_url=str(callback_url) if callback_url else None,
        idempotency_key=idempotency_key,
        request_hash=digest,
    )
    if verdict.outcome == "escalate":
        decision.status = "pending"
        decision.next_action_at = now  # escalation (§9) picks it up from here
        decision.say = settings.on_timeout.hold_say
    else:
        decision.status = "decided"
        decision.outcome = verdict.outcome
        decision.value = verdict.value
        decision.say = verdict.say
        decision.decided_by = "policy"
        decision.decided_at = now
    session.add(decision)
    try:
        await session.commit()
    except IntegrityError:
        await session.rollback()
        if idempotency_key is None:
            raise
        # Lost a race with the same Idempotency-Key: answer like a replay.
        existing = await _replay(session, agency_id, idempotency_key, digest)
        if existing is None:
            raise
        return existing
    return decision


async def resolve(
    session: AsyncSession,
    decision_id: str,
    outcome: str,
    value: Decimal | None,
    person_id: str,
    say: str,
) -> Decision | None:
    """A person's answer. First valid answer wins: one conditional UPDATE, with NOTIFY in the
    same transaction so waiters wake only once it is committed. None if it was already settled."""
    decision = await session.scalar(
        update(Decision)
        .where(Decision.id == decision_id, Decision.status == "pending")
        .values(
            status="decided",
            outcome=outcome,
            value=value,
            say=say,
            reason="human",
            decided_by="human",
            decided_by_person_id=person_id,
            decided_at=utcnow(),
            next_action_at=None,
        )
        .returning(Decision)
    )
    if decision is not None:
        await session.execute(select(func.pg_notify(WAIT_CHANNEL, decision_id)))
    await session.commit()
    return decision
