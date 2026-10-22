from fastapi import APIRouter, HTTPException
from sqlalchemy import select, update

from app.api.deps import PrincipalDep, SessionDep, authorize
from app.core.config import get_settings
from app.core.security import callback_secret
from app.models import Agency, ApiKey, Business, Location, utcnow
from app.schemas import (
    ApiKeyCreated,
    ApiKeyIn,
    ApiKeyOut,
    BusinessIn,
    BusinessOut,
    BusinessPatch,
    CallbackSecretOut,
    LocationIn,
    LocationOut,
    LocationPatch,
)

router = APIRouter(tags=["orgs"])


@router.post("/businesses", status_code=201)
async def create_business(body: BusinessIn, p: PrincipalDep, session: SessionDep) -> BusinessOut:
    await authorize(session, p, "agency", p.agency_id, "manage_org")
    business = Business(agency_id=p.agency_id, name=body.name)
    session.add(business)
    await session.commit()
    return BusinessOut.model_validate(business)


@router.get("/businesses")
async def list_businesses(p: PrincipalDep, session: SessionDep) -> list[BusinessOut]:
    q = select(Business).where(Business.agency_id == p.agency_id).order_by(Business.id)
    if p.scope[0] == "business":
        q = q.where(Business.id == p.scope[1])
    elif p.scope[0] == "location":
        return []
    return [BusinessOut.model_validate(b) for b in await session.scalars(q)]


@router.get("/businesses/{business_id}")
async def get_business(business_id: str, p: PrincipalDep, session: SessionDep) -> BusinessOut:
    await authorize(session, p, "business", business_id, "read")
    return BusinessOut.model_validate(await session.get_one(Business, business_id))


@router.patch("/businesses/{business_id}")
async def update_business(
    business_id: str, body: BusinessPatch, p: PrincipalDep, session: SessionDep
) -> BusinessOut:
    await authorize(session, p, "business", business_id, "manage_org")
    business = await session.get_one(Business, business_id)
    for field, value in body.model_dump(exclude_unset=True).items():
        setattr(business, field, value)
    await session.commit()
    return BusinessOut.model_validate(business)


@router.post("/locations", status_code=201)
async def create_location(body: LocationIn, p: PrincipalDep, session: SessionDep) -> LocationOut:
    await authorize(session, p, "business", body.business_id, "manage_org")
    location = Location(agency_id=p.agency_id, **body.model_dump())
    session.add(location)
    await session.commit()
    return LocationOut.model_validate(location)


@router.get("/locations")
async def list_locations(p: PrincipalDep, session: SessionDep) -> list[LocationOut]:
    q = select(Location).where(Location.agency_id == p.agency_id).order_by(Location.id)
    if p.scope[0] == "business":
        q = q.where(Location.business_id == p.scope[1])
    elif p.scope[0] == "location":
        q = q.where(Location.id == p.scope[1])
    return [LocationOut.model_validate(loc) for loc in await session.scalars(q)]


@router.get("/locations/{location_id}")
async def get_location(location_id: str, p: PrincipalDep, session: SessionDep) -> LocationOut:
    await authorize(session, p, "location", location_id, "read")
    return LocationOut.model_validate(await session.get_one(Location, location_id))


@router.patch("/locations/{location_id}")
async def update_location(
    location_id: str, body: LocationPatch, p: PrincipalDep, session: SessionDep
) -> LocationOut:
    await authorize(session, p, "location", location_id, "manage_org")
    location = await session.get_one(Location, location_id)
    for field, value in body.model_dump(exclude_unset=True).items():
        setattr(location, field, value)
    await session.commit()
    return LocationOut.model_validate(location)


# --- agency administration (agency keys only) ------------------------------------------------


@router.post("/api_keys", status_code=201)
async def create_api_key(body: ApiKeyIn, p: PrincipalDep, session: SessionDep) -> ApiKeyCreated:
    await authorize(session, p, "agency", p.agency_id, "manage_org")
    await authorize(session, p, body.scope_type, body.scope_id, "manage_org")
    row, key = ApiKey.issue(p.agency_id, body.scope_type, body.scope_id)
    session.add(row)
    await session.commit()
    return ApiKeyCreated(**ApiKeyOut.model_validate(row).model_dump(), key=key)


@router.get("/api_keys")
async def list_api_keys(p: PrincipalDep, session: SessionDep) -> list[ApiKeyOut]:
    await authorize(session, p, "agency", p.agency_id, "manage_org")
    rows = await session.scalars(
        select(ApiKey).where(ApiKey.agency_id == p.agency_id).order_by(ApiKey.id)
    )
    return [ApiKeyOut.model_validate(row) for row in rows]


@router.post("/api_keys/{key_id}/revoke")
async def revoke_api_key(key_id: str, p: PrincipalDep, session: SessionDep) -> ApiKeyOut:
    await authorize(session, p, "agency", p.agency_id, "manage_org")
    row = await session.get(ApiKey, key_id)
    if row is None or row.agency_id != p.agency_id:
        raise HTTPException(404, "Not found")
    if row.id == p.key_id:
        raise HTTPException(409, "A key cannot revoke itself")
    row.revoked_at = row.revoked_at or utcnow()
    await session.commit()
    return ApiKeyOut.model_validate(row)


@router.post("/callback_secret/rotate")
async def rotate_callback_secret(p: PrincipalDep, session: SessionDep) -> CallbackSecretOut:
    """Invalidate the agency's callback secret and return the new one, once (D17)."""
    await authorize(session, p, "agency", p.agency_id, "manage_org")
    version = await session.scalar(
        update(Agency)
        .where(Agency.id == p.agency_id)
        .values(callback_secret_version=Agency.callback_secret_version + 1)
        .returning(Agency.callback_secret_version)
    )
    if version is None:
        raise HTTPException(404, "Not found")
    await session.commit()
    signing = get_settings().signing_secret.get_secret_value().encode()
    return CallbackSecretOut(version=version, secret=callback_secret(signing, p.agency_id, version))
