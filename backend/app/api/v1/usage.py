from datetime import datetime
from typing import Annotated, Any

from fastapi import APIRouter, Query
from pydantic import AwareDatetime
from sqlalchemy import func, select

from app.api.deps import PrincipalDep, SessionDep, authorize
from app.api.v1.policies import parse_scope
from app.models import AiCall, Decision, DecisionAsk, Location
from app.schemas import UsageMonth

router = APIRouter(tags=["usage"])


def at_scope(location_col: Any, agency_col: Any, scope: tuple[str, str]) -> Any:
    scope_type, scope_id = scope
    if scope_type == "agency":
        return agency_col == scope_id
    if scope_type == "business":
        return location_col.in_(select(Location.id).where(Location.business_id == scope_id))
    return location_col == scope_id


@router.get("/usage")
async def usage(
    p: PrincipalDep,
    session: SessionDep,
    scope: str | None = None,
    from_: Annotated[AwareDatetime | None, Query(alias="from")] = None,
    to: AwareDatetime | None = None,
) -> list[UsageMonth]:
    """Counts per calendar month (UTC) for a scope and everything under it (D10). No metering
    table until pricing is decided. AI calls with no location count at agency scope only."""
    at = parse_scope(p, scope)
    await authorize(session, p, *at, "read")

    async def monthly(created: Any, where: Any, *columns: Any) -> dict[datetime, Any]:
        month = func.date_trunc("month", func.timezone("UTC", created))
        q = select(month, func.count(), *columns).where(where).group_by(month)
        if from_ is not None:
            q = q.where(created >= from_)
        if to is not None:
            q = q.where(created < to)
        return {row[0]: row[1:] for row in await session.execute(q)}

    decisions = await monthly(
        Decision.created_at, at_scope(Decision.location_id, Decision.agency_id, at)
    )
    asks = await monthly(
        DecisionAsk.sent_at,
        DecisionAsk.decision_id.in_(
            select(Decision.id).where(at_scope(Decision.location_id, Decision.agency_id, at))
        ),
    )
    ai = await monthly(
        AiCall.created_at,
        at_scope(AiCall.location_id, AiCall.agency_id, at),
        func.coalesce(func.sum(AiCall.tokens_in), 0),
        func.coalesce(func.sum(AiCall.tokens_out), 0),
    )
    return [
        UsageMonth(
            month=month.strftime("%Y-%m"),
            decisions=decisions.get(month, (0,))[0],
            asks=asks.get(month, (0,))[0],
            ai_calls=ai.get(month, (0, 0, 0))[0],
            ai_tokens_in=ai.get(month, (0, 0, 0))[1],
            ai_tokens_out=ai.get(month, (0, 0, 0))[2],
        )
        for month in sorted({*decisions, *asks, *ai})
    ]
