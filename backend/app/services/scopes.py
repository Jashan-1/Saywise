from dataclasses import dataclass
from typing import Literal

from sqlalchemy import select, tuple_
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Agency, Business, Location, Policy

Scope = tuple[str, str]  # (scope_type, scope_id)
SYSTEM: Scope = ("system", "system")

Action = Literal["read", "approve", "edit_rules", "manage_people", "manage_settings", "manage_org"]

# §5.3. Scope ("agency subtree" / "own scope") comes from where the membership sits.
ROLE_ACTIONS: dict[str, frozenset[str]] = {
    "agency_admin": frozenset({"approve", "edit_rules", "manage_people"}),
    "owner": frozenset({"approve", "edit_rules", "manage_people"}),
    "manager": frozenset({"approve"}),
    "approver": frozenset({"approve"}),
}


@dataclass(frozen=True)
class Principal:
    """Who is acting: an API key (role=None) or a person through one membership."""

    agency_id: str
    scope: Scope
    role: str | None = None
    can_approve: bool = True
    key_id: str | None = None


def reaches(p: Principal, target: list[Scope]) -> bool:
    """`target` is a scope chain. True if it is the caller's own scope or below it, inside the
    caller's own agency."""
    return p.scope in target and ("agency", p.agency_id) in target


def can(p: Principal, action: Action, target: list[Scope]) -> bool:
    if not reaches(p, target):
        return False
    if p.role is None:
        return action != "manage_org" or p.scope[0] == "agency"
    if action == "approve" and not p.can_approve:
        return False
    return action in ROLE_ACTIONS[p.role]


async def chain(session: AsyncSession, scope_type: str, scope_id: str) -> list[Scope] | None:
    """Nearest first, e.g. [location, business, agency, system]. None if the scope doesn't exist."""
    if scope_type == "location":
        row = (
            await session.execute(
                select(Location.business_id, Location.agency_id).where(Location.id == scope_id)
            )
        ).one_or_none()
        if row is None:
            return None
        return [
            ("location", scope_id),
            ("business", row.business_id),
            ("agency", row.agency_id),
            SYSTEM,
        ]
    if scope_type == "business":
        agency_id = await session.scalar(select(Business.agency_id).where(Business.id == scope_id))
        if agency_id is None:
            return None
        return [("business", scope_id), ("agency", agency_id), SYSTEM]
    if scope_type == "agency":
        found = await session.scalar(select(Agency.id).where(Agency.id == scope_id))
        return None if found is None else [("agency", scope_id), SYSTEM]
    return None


def agency_of(scopes: list[Scope]) -> str:
    return next(scope_id for scope_type, scope_id in scopes if scope_type == "agency")


async def nearest_policy(
    session: AsyncSession, scopes: list[Scope], decision_type: str
) -> Policy | None:
    """The active policy for `decision_type` closest to the start of the chain."""
    rows = await session.scalars(
        select(Policy).where(
            Policy.active,
            Policy.decision_type == decision_type,
            tuple_(Policy.scope_type, Policy.scope_id).in_(scopes),
        )
    )
    rank = {scope: i for i, scope in enumerate(scopes)}
    return min(rows, key=lambda p: rank[(p.scope_type, p.scope_id)], default=None)
