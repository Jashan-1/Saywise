"""Rule suggestions from history (§12). A nightly job proposes approve rules for amount ranges
people almost always approve. Nothing is applied until someone adopts it."""

from collections import defaultdict
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.constants import (
    AMOUNT_BUCKETS,
    SUGGEST_LOOKBACK,
    SUGGEST_MIN_APPROVE_RATE,
    SUGGEST_MIN_DECISIONS,
)
from app.models import Decision, Location, Suggestion, utcnow


@dataclass(frozen=True)
class Found:
    agency_id: str
    business_id: str
    decision_type: str
    lo: int
    hi: int
    n: int
    approved: int


def bucket(amount: Decimal) -> tuple[int, int] | None:
    for lo, hi in zip(AMOUNT_BUCKETS, AMOUNT_BUCKETS[1:], strict=False):
        if lo < amount <= hi:
            return lo, hi
    return None  # zero, or above the top edge


def find(rows: list[tuple[str, str, str, Decimal | None, str | None]]) -> list[Found]:
    """Pure. rows are human decisions: (agency, business, type, amount, outcome)."""
    groups: dict[tuple[str, str, str, int, int], list[str | None]] = defaultdict(list)
    for agency_id, business_id, decision_type, amount, outcome in rows:
        if amount is not None and (edges := bucket(amount)):
            groups[(agency_id, business_id, decision_type, *edges)].append(outcome)
    found = []
    for (agency_id, business_id, decision_type, lo, hi), outcomes in groups.items():
        n, approved = len(outcomes), outcomes.count("approve")
        if n >= SUGGEST_MIN_DECISIONS and approved / n >= SUGGEST_MIN_APPROVE_RATE:
            found.append(Found(agency_id, business_id, decision_type, lo, hi, n, approved))
    return found


def rule_for(lo: int, hi: int) -> dict[str, Any]:
    conditions = [{"var": "amount", "op": "<=", "value": hi}]
    if lo > 0:
        conditions.insert(0, {"var": "amount", "op": ">", "value": lo})
    return {"id": f"suggested_{lo}_{hi}", "when": {"all": conditions}, "then": "approve"}


async def run(session: AsyncSession) -> int:
    """The nightly job. Doesn't commit; the job runner does. Returns how many it created."""
    since = utcnow() - SUGGEST_LOOKBACK
    # shortcut: groups in Python; move the bucketing into SQL when human decisions exceed ~100k
    # per 60 days.
    rows = (
        await session.execute(
            select(
                Decision.agency_id,
                Location.business_id,
                Decision.type,
                Decision.amount,
                Decision.outcome,
            )
            .join(Location, Decision.location_id == Location.id)
            .where(Decision.decided_by == "human", Decision.decided_at >= since)
        )
    ).all()
    created = 0
    for f in find([tuple(r) for r in rows]):  # type: ignore[misc]
        rule = rule_for(f.lo, f.hi)
        exists = await session.scalar(
            select(Suggestion.id).where(
                Suggestion.scope_type == "business",
                Suggestion.scope_id == f.business_id,
                Suggestion.decision_type == f.decision_type,
                Suggestion.rule == rule,
            )
        )
        if exists is None:
            session.add(
                Suggestion(
                    agency_id=f.agency_id,
                    scope_type="business",
                    scope_id=f.business_id,
                    decision_type=f.decision_type,
                    rule=rule,
                    evidence={
                        "decisions": f.n,
                        "approved": f.approved,
                        "approve_rate": round(f.approved / f.n, 3),
                        "amount_range": [f.lo, f.hi],
                        "since": since.isoformat(),
                    },
                )
            )
            created += 1
    return created
