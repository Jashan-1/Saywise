from typing import Annotated

from fastapi import Depends, HTTPException
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.db import get_session
from app.core.security import key_prefix, verify_key
from app.models import ApiKey, utcnow
from app.services import scopes
from app.services.scopes import Action, Principal, Scope

SessionDep = Annotated[AsyncSession, Depends(get_session)]
bearer = HTTPBearer(auto_error=False)


async def get_principal(
    session: SessionDep,
    creds: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer)],
) -> Principal:
    key = creds.credentials if creds else ""
    row = None
    if key:
        row = await session.scalar(
            select(ApiKey).where(ApiKey.prefix == key_prefix(key), ApiKey.revoked_at.is_(None))
        )
    if row is None or not verify_key(key, row.key_hash):
        raise HTTPException(401, "Invalid or missing API key", {"WWW-Authenticate": "Bearer"})
    # shortcut: one UPDATE per request; throttle to once a minute if it shows up in profiles.
    row.last_used_at = utcnow()
    await session.commit()
    return Principal(agency_id=row.agency_id, scope=(row.scope_type, row.scope_id))


PrincipalDep = Annotated[Principal, Depends(get_principal)]


async def authorize(
    session: AsyncSession, p: Principal, scope_type: str, scope_id: str, action: Action
) -> list[Scope]:
    """Return the target's scope chain. 404 if it is missing or outside the caller's subtree."""
    target = await scopes.chain(session, scope_type, scope_id)
    if target is None or not scopes.reaches(p, target):
        raise HTTPException(404, "Not found")
    if not scopes.can(p, action, target):
        raise HTTPException(403, "This key is not allowed to do that")
    return target
