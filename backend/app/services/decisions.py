"""Create decisions (§7): resolve settings and policy, evaluate, persist. Writes are idempotent
per (agency, Idempotency-Key)."""

import hashlib
import json
from datetime import timedelta
from decimal import Decimal
from typing import Any

from pydantic import ValidationError
from sqlalchemy import and_, func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.constants import WAIT_CHANNEL
from app.models import Decision, utcnow
from app.schemas import DecisionIn, OnTimeout
from app.services import callbacks, history
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

    customer = await history.customer_facts(
        session, agency_id, location_id, body.subject.customer_ref
    )
    facts: dict[str, Any] = {"type": body.type, "context": body.context, "history": customer}
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
        await session.flush()
        if decision.status == "decided":
            callbacks.enqueue(session, decision, "decision.decided")
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


async def _transition(
    session: AsyncSession, decision_id: str, where: Any, values: dict[str, Any], event: str
) -> Decision | None:
    """Every state change is one conditional UPDATE, so the first one wins (§7.3). NOTIFY and the
    callback ride in the same transaction. None if the condition no longer held."""
    decision = await session.scalar(
        update(Decision)
        .where(Decision.id == decision_id, where)
        .values(**values)
        .returning(Decision)
    )
    if decision is not None:
        await session.execute(select(func.pg_notify(WAIT_CHANNEL, decision_id)))
        callbacks.enqueue(session, decision, event)
    await session.commit()
    return decision


async def resolve(
    session: AsyncSession,
    decision_id: str,
    outcome: str,
    value: Decimal | None,
    person_id: str,
    say: str,
) -> Decision | None:
    """A person's answer while the decision is pending."""
    return await _transition(
        session,
        decision_id,
        Decision.status == "pending",
        {
            "status": "decided",
            "outcome": outcome,
            "value": value,
            "say": say,
            "reason": "human",
            "decided_by": "human",
            "decided_by_person_id": person_id,
            "decided_at": utcnow(),
            "next_action_at": None,
        },
        "decision.decided",
    )


async def expire(session: AsyncSession, decision: Decision, reason: str) -> Decision | None:
    """Nobody answered in time (or nobody can): the configured defer/decline, never approve."""
    try:
        resolved = await resolve_settings(session, decision.location_id, decision.type)
        on_timeout = resolved.settings.on_timeout if resolved else OnTimeout()
    except ValidationError:  # stored settings went bad: fall back to the floor, not a crash
        on_timeout = OnTimeout()
    return await _transition(
        session,
        decision.id,
        Decision.status == "pending",
        {
            "status": "expired",
            "outcome": on_timeout.outcome,
            "say": on_timeout.say,
            "reason": reason,
            "decided_by": "timeout",
            "decided_at": utcnow(),
            "next_action_at": None,
        },
        "decision.expired",
    )


async def cancel(session: AsyncSession, decision_id: str) -> Decision | None:
    return await _transition(
        session,
        decision_id,
        Decision.status == "pending",
        {"status": "cancelled", "reason": "cancelled", "next_action_at": None},
        "decision.cancelled",
    )


async def late_answer(
    session: AsyncSession, decision_id: str, outcome: str, value: Decimal | None
) -> Decision | None:
    """D16: an answer after expiry, inside the window, is passed on but changes nothing else."""
    return await _transition(
        session,
        decision_id,
        and_(Decision.status == "expired", Decision.late_outcome.is_(None)),
        {"late_outcome": outcome, "late_value": value},
        "decision.late_answer",
    )
