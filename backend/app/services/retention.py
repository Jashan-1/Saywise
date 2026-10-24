"""Retention (§12, D14): a daily job scrubs call context once it is older than the location's
retention.context_days. Outcomes, amounts, timings and customer_ref stay, so history and
suggestions keep working."""

from datetime import timedelta

from pydantic import ValidationError
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.logging import log
from app.models import Decision, DecisionAsk, utcnow
from app.services.settings import resolve


async def run(session: AsyncSession) -> int:
    """Doesn't commit; the job runner does. Returns how many decisions it scrubbed."""
    now = utcnow()
    unscrubbed = (Decision.context_scrubbed_at.is_(None), Decision.status != "pending")
    locations = await session.scalars(
        select(Decision.location_id)
        .where(*unscrubbed, Decision.created_at < now - timedelta(days=1))  # 1 day is the floor
        .distinct()
    )
    scrubbed = 0
    for location_id in locations.all():
        try:
            resolved = await resolve(session, location_id)
        except ValidationError:
            log.warning("retention skipped: invalid settings", extra={"location_id": location_id})
            continue
        if resolved is None:
            continue
        cutoff = now - timedelta(days=resolved.settings.retention.context_days)
        expired = select(Decision.id).where(
            Decision.location_id == location_id, *unscrubbed, Decision.created_at < cutoff
        )
        await session.execute(
            update(DecisionAsk)
            .where(DecisionAsk.decision_id.in_(expired))
            .values(ai_recommendation=None)
        )
        result = await session.execute(
            update(Decision)
            .where(Decision.id.in_(expired))
            .values(context=None, summary=None, say=None, context_scrubbed_at=now)
        )
        scrubbed += result.rowcount  # type: ignore[attr-defined]
    return scrubbed
