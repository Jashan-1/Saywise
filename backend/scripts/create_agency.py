"""Create an agency and print an agency-scoped API key once.

python -m scripts.create_agency "Demo Agency"
"""

import asyncio
import sys

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.db import SessionLocal, engine
from app.core.security import generate_api_key, hash_key, key_prefix
from app.models import Agency, ApiKey


def issue_key(session: AsyncSession, agency_id: str, scope_type: str, scope_id: str) -> str:
    """Add a key row to the session and return the plaintext key. Only its hash is stored."""
    key = generate_api_key()
    session.add(
        ApiKey(
            agency_id=agency_id,
            scope_type=scope_type,
            scope_id=scope_id,
            prefix=key_prefix(key),
            key_hash=hash_key(key),
        )
    )
    return key


async def create_agency(session: AsyncSession, name: str) -> tuple[Agency, str]:
    agency = Agency(name=name)
    session.add(agency)
    await session.flush()
    key = issue_key(session, agency.id, "agency", agency.id)
    await session.commit()
    return agency, key


async def main(name: str) -> None:
    async with SessionLocal() as session:
        agency, key = await create_agency(session, name)
    await engine.dispose()
    print(f"agency:  {agency.id}")
    print(f"api key: {key}")
    print("Store the key now. It is not shown again.")


if __name__ == "__main__":
    if len(sys.argv) != 2:
        sys.exit('usage: python -m scripts.create_agency "Agency name"')
    asyncio.run(main(sys.argv[1]))
