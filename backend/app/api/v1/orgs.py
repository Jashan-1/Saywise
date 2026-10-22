from fastapi import APIRouter
from sqlalchemy import select

from app.api.deps import PrincipalDep, SessionDep, authorize
from app.models import Business, Location
from app.schemas import (
    BusinessIn,
    BusinessOut,
    BusinessPatch,
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
