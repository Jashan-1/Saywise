from typing import Annotated, Any

from fastapi import APIRouter, Body, HTTPException
from fastapi.exceptions import RequestValidationError
from pydantic import ValidationError

from app.api.deps import PrincipalDep, SessionDep, authorize
from app.models import Agency, Business, Location, ScopedSettings
from app.schemas import ResolvedSettingsOut, ScopeType
from app.services import settings as settings_service

router = APIRouter(tags=["settings"])

SCOPE_MODELS: dict[str, type[ScopedSettings]] = {
    "agency": Agency,
    "business": Business,
    "location": Location,
}


@router.get("/settings/resolved")
async def resolved_settings(
    location_id: str, p: PrincipalDep, session: SessionDep, decision_type: str | None = None
) -> ResolvedSettingsOut:
    await authorize(session, p, "location", location_id, "read")
    resolved = await settings_service.resolve(session, location_id, decision_type)
    if resolved is None:
        raise HTTPException(404, "Not found")
    return ResolvedSettingsOut(
        settings=resolved.settings.model_dump(mode="json"), origins=resolved.origins
    )


@router.get("/settings/{scope_type}/{scope_id}")
async def get_scope_settings(
    scope_type: ScopeType, scope_id: str, p: PrincipalDep, session: SessionDep
) -> dict[str, Any]:
    await authorize(session, p, scope_type, scope_id, "read")
    row = await session.get_one(SCOPE_MODELS[scope_type], scope_id)
    return row.settings


@router.patch("/settings/{scope_type}/{scope_id}")
async def patch_scope_settings(
    scope_type: ScopeType,
    scope_id: str,
    patch: Annotated[dict[str, Any], Body()],
    p: PrincipalDep,
    session: SessionDep,
) -> dict[str, Any]:
    """JSON merge patch over this scope's partial overrides; null removes one (inherit)."""
    await authorize(session, p, scope_type, scope_id, "manage_settings")
    row = await session.get_one(SCOPE_MODELS[scope_type], scope_id)
    updated = settings_service.merge_patch(row.settings, patch)
    try:
        settings_service.validate_layer(updated)
    except ValidationError as e:
        raise RequestValidationError(e.errors(include_url=False)) from None
    row.settings = updated
    await session.commit()
    return updated
