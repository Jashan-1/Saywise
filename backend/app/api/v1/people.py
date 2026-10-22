from fastapi import APIRouter, HTTPException
from sqlalchemy import ColumnElement, and_, or_, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.exc import IntegrityError

from app.api.deps import PrincipalDep, SessionDep, authorize
from app.models import Location, Membership, Person
from app.schemas import MembershipIn, MembershipOut, MembershipPatch, PersonIn, PersonOut
from app.services.scopes import Action, Principal, agency_of

router = APIRouter(tags=["people"])


def in_subtree(p: Principal) -> ColumnElement[bool]:
    """Memberships at the caller's scope or below it."""
    scope_type, scope_id = p.scope
    if scope_type == "agency":
        return Membership.agency_id == scope_id
    if scope_type == "business":
        return or_(
            and_(Membership.scope_type == "business", Membership.scope_id == scope_id),
            and_(
                Membership.scope_type == "location",
                Membership.scope_id.in_(
                    select(Location.id).where(Location.business_id == scope_id)
                ),
            ),
        )
    return and_(Membership.scope_type == "location", Membership.scope_id == scope_id)


def check_role(role: str, scope_type: str) -> None:
    if role == "agency_admin" and scope_type != "agency":
        raise HTTPException(422, "agency_admin memberships must be at agency scope")


@router.post("/people", status_code=201)
async def create_person(body: PersonIn, p: PrincipalDep, session: SessionDep) -> PersonOut:
    """Get-or-create by phone. Always 201, so callers can't tell whether another agency has
    this number."""
    await session.execute(
        insert(Person).values(phone=body.phone).on_conflict_do_nothing(index_elements=["phone"])
    )
    person = await session.scalar(select(Person).where(Person.phone == body.phone))
    await session.commit()
    return PersonOut.model_validate(person)


@router.get("/people")
async def list_people(p: PrincipalDep, session: SessionDep) -> list[PersonOut]:
    members = select(Membership.person_id).where(in_subtree(p))
    q = select(Person).where(Person.id.in_(members)).order_by(Person.id)
    return [PersonOut.model_validate(person) for person in await session.scalars(q)]


@router.post("/memberships", status_code=201)
async def create_membership(
    body: MembershipIn, p: PrincipalDep, session: SessionDep
) -> MembershipOut:
    target = await authorize(session, p, body.scope_type, body.scope_id, "manage_people")
    check_role(body.role, body.scope_type)
    if await session.get(Person, body.person_id) is None:
        raise HTTPException(404, "Person not found")
    membership = Membership(agency_id=agency_of(target), **body.model_dump())
    session.add(membership)
    try:
        await session.commit()
    except IntegrityError:
        await session.rollback()
        raise HTTPException(409, "This person already has a membership at that scope") from None
    return MembershipOut.model_validate(membership)


@router.get("/memberships")
async def list_memberships(
    p: PrincipalDep, session: SessionDep, person_id: str | None = None
) -> list[MembershipOut]:
    q = select(Membership).where(in_subtree(p)).order_by(Membership.id)
    if person_id is not None:
        q = q.where(Membership.person_id == person_id)
    return [MembershipOut.model_validate(m) for m in await session.scalars(q)]


async def _membership(
    session: SessionDep, p: Principal, membership_id: str, action: Action
) -> Membership:
    membership = await session.get(Membership, membership_id)
    if membership is None:
        raise HTTPException(404, "Not found")
    await authorize(session, p, membership.scope_type, membership.scope_id, action)
    return membership


@router.get("/memberships/{membership_id}")
async def get_membership(membership_id: str, p: PrincipalDep, session: SessionDep) -> MembershipOut:
    return MembershipOut.model_validate(await _membership(session, p, membership_id, "read"))


@router.patch("/memberships/{membership_id}")
async def update_membership(
    membership_id: str, body: MembershipPatch, p: PrincipalDep, session: SessionDep
) -> MembershipOut:
    membership = await _membership(session, p, membership_id, "manage_people")
    changes = body.model_dump(exclude_unset=True)
    if "role" in changes:
        check_role(changes["role"], membership.scope_type)
    for field, value in changes.items():
        setattr(membership, field, value)
    await session.commit()
    return MembershipOut.model_validate(membership)
