"""What SaySo has learned from past decisions: approver response stats (§9.2) and, from phase 6,
customer facts."""

from dataclasses import dataclass
from datetime import datetime, timedelta

from sqlalchemy import extract, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.constants import (
    COLD_P_ANSWER,
    COLD_T80,
    STATS_CACHE_TTL,
    STATS_LOOKBACK,
    STATS_MIN_ASKS,
)
from app.models import DecisionAsk


@dataclass(frozen=True)
class Stats:
    p_answer: float  # share of asks answered within the window
    t80: timedelta  # 80th-percentile response time


COLD = Stats(COLD_P_ANSWER, COLD_T80)

# shortcut: in-process TTL cache; move to a materialized view when asks exceed ~1M rows.
_cache: dict[tuple[str, int, int], tuple[datetime, Stats]] = {}


async def approver_stats(
    session: AsyncSession, person_ids: list[str], window: timedelta, now: datetime
) -> dict[str, Stats]:
    """Per person, from their asks in the same UTC hour of day over STATS_LOOKBACK. Fewer than
    STATS_MIN_ASKS asks means cold-start values."""
    hour, window_s = now.hour, int(window.total_seconds())
    out: dict[str, Stats] = {}
    for pid in person_ids:
        hit = _cache.get((pid, hour, window_s))
        if hit and hit[0] > now:
            out[pid] = hit[1]
    missing = [pid for pid in person_ids if pid not in out]
    if missing:
        took = func.extract("epoch", DecisionAsk.responded_at - DecisionAsk.sent_at)
        rows = await session.execute(
            select(
                DecisionAsk.person_id,
                func.count(),
                func.count().filter(took <= window_s),
                func.percentile_cont(0.8).within_group(took).filter(took.is_not(None)),
            )
            .where(
                DecisionAsk.person_id.in_(missing),
                DecisionAsk.sent_at > now - STATS_LOOKBACK,
                extract("hour", func.timezone("UTC", DecisionAsk.sent_at)) == hour,
            )
            .group_by(DecisionAsk.person_id)
        )
        found = {pid: (n, answered, t80) for pid, n, answered, t80 in rows}
        for pid in missing:
            n, answered, t80 = found.get(pid, (0, 0, None))
            stats = COLD
            if n >= STATS_MIN_ASKS:
                seconds = COLD_T80 if t80 is None else timedelta(seconds=float(t80))
                stats = Stats(answered / n, seconds)
            _cache[(pid, hour, window_s)] = (now + STATS_CACHE_TTL, stats)
            out[pid] = stats
    return out
