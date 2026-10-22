"""Create an agency and print an agency-scoped API key once.

python -m scripts.create_agency "Demo Agency"
"""

import asyncio
import sys

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.db import SessionLocal, engine
from app.core.security import callback_secret
from app.models import Agency, ApiKey


async def create_agency(session: AsyncSession, name: str) -> tuple[Agency, str]:
    agency = Agency(name=name)
    session.add(agency)
    await session.flush()
    row, key = ApiKey.issue(agency.id, "agency", agency.id)
    session.add(row)
    await session.commit()
    return agency, key


async def main(name: str) -> None:
    async with SessionLocal() as session:
        agency, key = await create_agency(session, name)
    await engine.dispose()
    signing = get_settings().signing_secret.get_secret_value().encode()
    print(f"agency:          {agency.id}")
    print(f"api key:         {key}")
    print(f"callback secret: {callback_secret(signing, agency.id, agency.callback_secret_version)}")
    print("Store both now. They are not shown again (rotate with POST /v1/callback_secret/rotate).")


if __name__ == "__main__":
    if len(sys.argv) != 2:
        sys.exit('usage: python -m scripts.create_agency "Agency name"')
    asyncio.run(main(sys.argv[1]))
