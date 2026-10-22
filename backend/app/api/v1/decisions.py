from typing import Annotated, Literal

from fastapi import APIRouter, Header, HTTPException, Query
from fastapi.exceptions import RequestValidationError
from pydantic import AwareDatetime, ValidationError
from sqlalchemy import select

from app.api.deps import PrincipalDep, SessionDep, authorize
from app.models import Decision, Location
from app.schemas import DecisionIn, DecisionOut, PolicyRef
from app.services import decisions

router = APIRouter(tags=["decisions"])


def to_out(d: Decision) -> DecisionOut:
    policy = None
    if d.policy_id is not None and d.policy_version is not None:
        policy = PolicyRef(id=d.policy_id, version=d.policy_version, rule_id=d.rule_id)
    return DecisionOut(
        id=d.id,
        location_id=d.location_id,
        type=d.type,
        mode=d.mode,
        status=d.status,
        outcome=d.outcome,
        value=d.value,
        decided_by=d.decided_by,
        say=d.say,
        reason=d.reason,
        policy=policy,
        deadline_at=d.deadline_at,
        created_at=d.created_at,
    )


@router.post("/decisions")
async def create_decision(
    body: DecisionIn,
    p: PrincipalDep,
    session: SessionDep,
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
    return to_out(decision)


@router.get("/decisions/{decision_id}")
async def get_decision(decision_id: str, p: PrincipalDep, session: SessionDep) -> DecisionOut:
    decision = await session.get(Decision, decision_id)
    if decision is None:
        raise HTTPException(404, "Not found")
    await authorize(session, p, "location", decision.location_id, "read")
    return to_out(decision)


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
    return [to_out(d) for d in await session.scalars(q)]
