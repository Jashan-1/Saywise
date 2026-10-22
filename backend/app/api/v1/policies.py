from typing import Annotated, Any

from fastapi import APIRouter, HTTPException, Path
from sqlalchemy import func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import PrincipalDep, SessionDep, authorize
from app.models import Policy
from app.schemas import DECISION_TYPE_PATTERN, PolicyBody, PolicyOut
from app.services.scopes import Principal, Scope, agency_of, nearest_policy

router = APIRouter(tags=["policies"])

TypePath = Annotated[str, Path(pattern=DECISION_TYPE_PATTERN)]


def parse_scope(p: Principal, scope: str | None) -> Scope:
    """`business:bus_...`; defaults to the key's own scope. System scope is read-only."""
    if scope is None:
        return p.scope
    scope_type, sep, scope_id = scope.partition(":")
    if not sep or not scope_id or scope_type not in ("agency", "business", "location"):
        raise HTTPException(422, "scope must look like 'business:bus_...'")
    return scope_type, scope_id


def writable_type(decision_type: str) -> None:
    if decision_type == "templates":  # would collide with GET /v1/policies/templates
        raise HTTPException(422, "'templates' is reserved")


async def new_version(
    session: AsyncSession,
    agency_id: str,
    scope: Scope,
    decision_type: str,
    body: dict[str, Any],
    created_by: str | None,
    source: str,
) -> Policy:
    """Insert the next version at `scope` and make it the active one. Old rows stay untouched
    apart from `active`."""
    at_scope = (
        Policy.scope_type == scope[0],
        Policy.scope_id == scope[1],
        Policy.decision_type == decision_type,
    )
    latest = await session.scalar(select(func.max(Policy.version)).where(*at_scope))
    await session.execute(update(Policy).where(*at_scope, Policy.active).values(active=False))
    policy = Policy(
        agency_id=agency_id,
        scope_type=scope[0],
        scope_id=scope[1],
        decision_type=decision_type,
        version=(latest or 0) + 1,
        body=body,
        created_by=created_by,
        source=source,
    )
    session.add(policy)
    try:
        await session.commit()
    except IntegrityError:
        await session.rollback()
        raise HTTPException(409, "Another change to this policy landed first; retry") from None
    return policy


@router.get("/policies/templates")
async def list_templates(p: PrincipalDep, session: SessionDep) -> list[PolicyOut]:
    rows = await session.scalars(
        select(Policy)
        .where(Policy.scope_type == "system", Policy.active)
        .order_by(Policy.decision_type)
    )
    return [PolicyOut.model_validate(row) for row in rows]


@router.get("/policies/{decision_type}")
async def get_policy(
    decision_type: TypePath, p: PrincipalDep, session: SessionDep, scope: str | None = None
) -> PolicyOut:
    """The policy that applies at `scope`: the nearest active one, up to the system template."""
    target = await authorize(session, p, *parse_scope(p, scope), "read")
    policy = await nearest_policy(session, target, decision_type)
    if policy is None:
        raise HTTPException(404, "No policy for this type at or above that scope")
    return PolicyOut.model_validate(policy)


@router.put("/policies/{decision_type}")
async def put_policy(
    decision_type: TypePath,
    body: PolicyBody,
    p: PrincipalDep,
    session: SessionDep,
    scope: str | None = None,
) -> PolicyOut:
    """Replace the policy at `scope` with a new version. Brand-new types are created this way."""
    writable_type(decision_type)
    if body.decision_type not in (None, decision_type):
        raise HTTPException(422, "body.decision_type does not match the URL")
    at = parse_scope(p, scope)
    target = await authorize(session, p, *at, "edit_rules")
    stored = {**body.stored(), "decision_type": decision_type}
    policy = await new_version(
        session, agency_of(target), at, decision_type, stored, p.key_id, "api"
    )
    return PolicyOut.model_validate(policy)


@router.post("/policies/{decision_type}/clone", status_code=201)
async def clone_policy(
    decision_type: TypePath, to_scope: str, p: PrincipalDep, session: SessionDep
) -> PolicyOut:
    """Copy the template or the nearest parent policy down to `to_scope`, for editing there."""
    writable_type(decision_type)
    at = parse_scope(p, to_scope)
    target = await authorize(session, p, *at, "edit_rules")
    if await nearest_policy(session, target[:1], decision_type) is not None:
        raise HTTPException(409, "That scope already has its own policy; PUT to change it")
    source = await nearest_policy(session, target[1:], decision_type)
    if source is None:
        raise HTTPException(404, "No template or parent policy to clone")
    policy = await new_version(
        session, agency_of(target), at, decision_type, dict(source.body), p.key_id, "clone"
    )
    return PolicyOut.model_validate(policy)


@router.get("/policies/{decision_type}/versions")
async def list_versions(
    decision_type: TypePath, p: PrincipalDep, session: SessionDep, scope: str | None = None
) -> list[PolicyOut]:
    """Every version stored at `scope`, newest first."""
    at = parse_scope(p, scope)
    await authorize(session, p, *at, "read")
    rows = await session.scalars(
        select(Policy)
        .where(
            Policy.scope_type == at[0],
            Policy.scope_id == at[1],
            Policy.decision_type == decision_type,
        )
        .order_by(Policy.version.desc())
    )
    return [PolicyOut.model_validate(row) for row in rows]
