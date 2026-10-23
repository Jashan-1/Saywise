from typing import Annotated, Literal

from fastapi import APIRouter, Header, HTTPException, Query
from fastapi.exceptions import RequestValidationError
from pydantic import AwareDatetime, ValidationError
from sqlalchemy import select

from app.api.deps import ChannelDep, PrincipalDep, SessionDep, authorize
from app.core.constants import LONG_POLL_MAX_MS, POLL_AFTER_MS
from app.models import Decision, Location, utcnow
from app.schemas import DecisionIn, DecisionOut, PolicyRef
from app.services import decisions, escalation
from app.services.waiter import waiter

router = APIRouter(tags=["decisions"])


async def wait_for(session: SessionDep, decision: Decision, wait_ms: int) -> None:
    """Long-poll until the decision settles, `wait_ms` passes, or its deadline does."""
    remaining_s = (decision.deadline_at - utcnow()).total_seconds()

    async def settled() -> bool:
        status = await session.scalar(select(Decision.status).where(Decision.id == decision.id))
        await session.commit()  # don't hold a pooled connection while waiting
        return status != "pending"

    await waiter.wait(decision.id, max(0.0, min(wait_ms / 1000, remaining_s)), settled)
    await session.refresh(decision)


async def to_out(session: SessionDep, d: Decision) -> DecisionOut:
    policy = None
    if d.policy_id is not None and d.policy_version is not None:
        policy = PolicyRef(id=d.policy_id, version=d.policy_version, rule_id=d.rule_id)
    approver = None
    if d.decided_by_person_id is not None:
        name = await escalation.decider_name(session, d)
        approver = {"id": d.decided_by_person_id, "name": name or ""}
    return DecisionOut(
        id=d.id,
        location_id=d.location_id,
        type=d.type,
        mode=d.mode,
        status=d.status,
        outcome=d.outcome,
        value=d.value,
        decided_by=d.decided_by,
        approver=approver,
        say=d.say,
        reason=d.reason,
        policy=policy,
        deadline_at=d.deadline_at,
        poll_after_ms=POLL_AFTER_MS if d.status == "pending" else None,
        created_at=d.created_at,
    )


@router.post("/decisions")
async def create_decision(
    body: DecisionIn,
    p: PrincipalDep,
    session: SessionDep,
    channel: ChannelDep,
    idempotency_key: Annotated[str | None, Header(max_length=255)] = None,
) -> DecisionOut:
    location_id = body.location_id or (p.scope[1] if p.scope[0] == "location" else None)
    if location_id is None:
        raise HTTPException(422, "location_id is required unless the key is location-scoped")
    await authorize(session, p, "location", location_id, "decide")
    try:
        decision = await decisions.create(session, p.agency_id, location_id, body, idempotency_key)
    except decisions.IdempotencyConflict:
        raise HTTPException(
            409, "This Idempotency-Key was already used with a different request"
        ) from None
    except ValidationError as e:
        raise RequestValidationError(e.errors(include_url=False)) from None
    if decision.status == "pending" and decision.short_code is None:  # new: first ask, inline
        await escalation.advance(session, channel, decision)
    if body.wait_ms and decision.status == "pending":
        await wait_for(session, decision, body.wait_ms)
    return await to_out(session, decision)


@router.get("/decisions/{decision_id}")
async def get_decision(
    decision_id: str,
    p: PrincipalDep,
    session: SessionDep,
    wait_ms: Annotated[int, Query(ge=0, le=LONG_POLL_MAX_MS)] = 0,
) -> DecisionOut:
    decision = await session.get(Decision, decision_id)
    if decision is None:
        raise HTTPException(404, "Not found")
    await authorize(session, p, "location", decision.location_id, "read")
    if wait_ms and decision.status == "pending":
        await wait_for(session, decision, wait_ms)
    return await to_out(session, decision)


@router.get("/decisions")
async def list_decisions(
    p: PrincipalDep,
    session: SessionDep,
    location_id: str | None = None,
    status: Literal["pending", "decided", "expired", "cancelled"] | None = None,
    from_: Annotated[AwareDatetime | None, Query(alias="from")] = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
) -> list[DecisionOut]:
    """Newest first, for audit."""
    q = select(Decision).where(Decision.agency_id == p.agency_id)
    if location_id is not None:
        await authorize(session, p, "location", location_id, "read")
        q = q.where(Decision.location_id == location_id)
    elif p.scope[0] == "business":
        in_business = select(Location.id).where(Location.business_id == p.scope[1])
        q = q.where(Decision.location_id.in_(in_business))
    elif p.scope[0] == "location":
        q = q.where(Decision.location_id == p.scope[1])
    if status is not None:
        q = q.where(Decision.status == status)
    if from_ is not None:
        q = q.where(Decision.created_at >= from_)
    # shortcut: newest `limit` rows only; add cursor pagination when audits need more than 200.
    q = q.order_by(Decision.created_at.desc(), Decision.id.desc()).limit(limit)
    # shortcut: one name lookup per human-decided row; batch it if audit pages get slow.
    return [await to_out(session, d) for d in (await session.scalars(q)).all()]
