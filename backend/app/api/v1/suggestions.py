from typing import Any, Literal

from fastapi import APIRouter, HTTPException
from fastapi.exceptions import RequestValidationError
from pydantic import ValidationError
from sqlalchemy import ColumnElement, or_, select

from app.api.deps import PrincipalDep, SessionDep, authorize
from app.api.v1.policies import new_version
from app.models import Location, Suggestion
from app.schemas import PolicyBody, PolicyOut, SuggestionOut
from app.services.scopes import Principal, agency_of, nearest_policy

router = APIRouter(tags=["suggestions"])


def in_subtree(p: Principal) -> ColumnElement[bool]:
    scope_type, scope_id = p.scope
    if scope_type == "agency":
        return Suggestion.agency_id == scope_id
    if scope_type == "business":
        locations = select(Location.id).where(Location.business_id == scope_id)
        return or_(Suggestion.scope_id == scope_id, Suggestion.scope_id.in_(locations))
    return Suggestion.scope_id == scope_id


@router.get("/suggestions")
async def list_suggestions(
    p: PrincipalDep,
    session: SessionDep,
    status: Literal["open", "adopted"] | None = "open",
) -> list[SuggestionOut]:
    q = select(Suggestion).where(Suggestion.agency_id == p.agency_id, in_subtree(p))
    if status is not None:
        q = q.where(Suggestion.status == status)
    rows = await session.scalars(q.order_by(Suggestion.created_at.desc()))
    return [SuggestionOut.model_validate(row) for row in rows]


@router.post("/suggestions/{suggestion_id}/adopt")
async def adopt_suggestion(suggestion_id: str, p: PrincipalDep, session: SessionDep) -> PolicyOut:
    """Append the suggested rule to the policy that applies at the suggestion's scope, as a new
    version at that scope. Never automatic (§12)."""
    suggestion = await session.get(Suggestion, suggestion_id)
    if suggestion is None or suggestion.agency_id != p.agency_id:
        raise HTTPException(404, "Not found")
    scope = (suggestion.scope_type, suggestion.scope_id)
    target = await authorize(session, p, *scope, "edit_rules")
    if suggestion.status != "open":
        raise HTTPException(409, "This suggestion was already adopted")
    base = await nearest_policy(session, target, suggestion.decision_type)
    body: dict[str, Any] = dict(base.body) if base else {"label": suggestion.decision_type}
    body["rules"] = [*body.get("rules", []), suggestion.rule]
    try:
        stored = PolicyBody.model_validate(body).stored()
    except ValidationError as e:  # e.g. the policy already has a rule with this id
        raise RequestValidationError(e.errors(include_url=False)) from None
    suggestion.status = "adopted"
    policy = await new_version(
        session, agency_of(target), scope, suggestion.decision_type, stored, p.key_id, "suggestion"
    )
    return PolicyOut.model_validate(policy)
