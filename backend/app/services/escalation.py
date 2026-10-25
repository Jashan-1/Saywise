"""Escalation, part 1 (§9 without stats): ask eligible approvers one at a time, lowest sufficient
authority first, with a fixed wait. Also applies their answers (§9.4)."""

import secrets
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy import and_, func, or_, select, tuple_
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.channels.base import Channel
from app.core.config import get_settings
from app.core.constants import (
    LATE_ANSWER_WINDOW,
    SHORT_CODE_ALPHABET,
    SHORT_CODE_LENGTH,
    TARGET_P,
)
from app.core.logging import log
from app.core.security import sign_token
from app.models import Decision, DecisionAsk, Location, Membership, Person, utcnow
from app.services import decisions, history, scopes, sms_text
from app.services.policy_engine import format_value, render
from app.services.settings import resolve as resolve_settings
from app.services.sms_commands import Command

DAYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")
PAST_TENSE = {"approve": "approved", "decline": "declined", "counter": "countered"}


def in_contact_hours(schedule: Mapping[Any, list[str]] | None, local: datetime) -> bool:
    """`local` is in the location's timezone. None means any time. A window that ends before it
    starts crosses midnight and belongs to the day it starts (D18). Ends are exclusive."""
    if schedule is None:
        return True
    now = local.strftime("%H:%M")
    for window in schedule.get(DAYS[local.weekday()], []):
        start, end = window.split("-")
        if (start <= now < end) if start < end else now >= start:
            return True
    for window in schedule.get(DAYS[local.weekday() - 1], []):
        start, end = window.split("-")
        if end < start and now < end:
            return True
    return False


def unit_of(policy_body: Mapping[str, Any] | None) -> str:
    """What `amount` measures, from the policy's amount_label: "discount ($)" -> "discount"."""
    label = (policy_body or {}).get("amount_label") or "amount"
    return str(label).replace("($)", "").strip()


def counter_problem(value: Decimal, amount: Decimal | None, unit: str) -> str | None:
    """D21: a counter is in amount's unit, more than 0 and less than what was asked."""
    if amount is None:
        return None if value > 0 else "Counter must be more than 0."
    if 0 < value < amount:
        return None
    article = "an" if unit[:1].lower() in "aeiou" else "a"
    return f"Counter must be {article} {unit} under ${format_value(amount)}."


REPLY_HELP = "1=Yes 2=No or reply a number to counter"


def ask_lines(
    code: str,
    location: str,
    label: str,
    unit: str,
    amount: Decimal | None,
    summary: str | None,
    link: str,
) -> list[str]:
    """[head, summary, how to reply, link]; the summary is index 1, the one SMS trims."""
    head = f"SaySo #{code} - {location} - {label}"
    if amount is not None:
        head += f" - {unit} ${format_value(amount)}"
    return [head, summary or "", REPLY_HELP, link]


def approval_link(ask: DecisionAsk, deadline_at: datetime) -> str:
    settings = get_settings()
    exp = int((deadline_at + LATE_ANSWER_WINDOW).timestamp())
    secret = settings.signing_secret.get_secret_value().encode()
    # The person is signed into the token, not written in it, which keeps the link short (D23).
    token = sign_token(ask.id.removeprefix("ask_"), exp, secret, bind=ask.person_id)
    return f"{settings.public_base_url.rstrip('/')}/v1/approve/{token}"


async def _free_short_code(session: AsyncSession, now: datetime) -> str:
    """Unused by pending decisions and by expired ones still taking late answers. The partial
    unique index only covers pending (it can't use now()), so the window is checked here."""
    in_use = select(Decision.short_code).where(
        Decision.short_code.is_not(None),
        or_(
            Decision.status == "pending",
            and_(Decision.status == "expired", Decision.deadline_at > now - LATE_ANSWER_WINDOW),
        ),
    )
    used = set(await session.scalars(in_use))
    while True:
        code = "".join(secrets.choice(SHORT_CODE_ALPHABET) for _ in range(SHORT_CODE_LENGTH))
        if code not in used:
            return code


async def _assign_short_code(session: AsyncSession, decision: Decision) -> None:
    for attempt in range(3):
        decision.short_code = await _free_short_code(session, utcnow())
        try:
            await session.commit()
            return
        except IntegrityError:  # another pending decision took it first
            await session.rollback()
            await session.refresh(decision)
            if attempt == 2:
                raise


async def eligible(
    session: AsyncSession,
    decision: Decision,
    scope_hours: Mapping[Any, list[str]] | None,
    timezone: str,
    now: datetime,
    use_hours: bool = True,
) -> list[tuple[Membership, Person]]:
    """§9.1 without stats, lowest sufficient authority first, one membership per person.
    `use_hours=False` is for the after-hours on-call fallback."""
    chain = await scopes.chain(session, "location", decision.location_id) or []
    asked = select(DecisionAsk.person_id).where(DecisionAsk.decision_id == decision.id)
    q = (
        select(Membership, Person)
        .join(Person, Membership.person_id == Person.id)
        .where(
            tuple_(Membership.scope_type, Membership.scope_id).in_(chain),
            Membership.agency_id == decision.agency_id,
            Membership.active,
            Membership.can_approve,
            or_(Person.sms_opted_out.is_(False), Membership.email.is_not(None)),
            Person.id.not_in(asked),
        )
        .order_by(Membership.authority_limit, Membership.id)
    )
    if decision.amount is not None:
        q = q.where(Membership.authority_limit >= decision.amount)
    rows = (await session.execute(q)).all()
    load = dict(
        (
            await session.execute(
                select(DecisionAsk.membership_id, func.count())
                .where(
                    DecisionAsk.sent_at > now - timedelta(hours=1),
                    DecisionAsk.membership_id.in_([m.id for m, _ in rows]),
                )
                .group_by(DecisionAsk.membership_id)
            )
        ).all()
    )
    local = now.astimezone(ZoneInfo(timezone))
    chosen: list[tuple[Membership, Person]] = []
    for membership, person in rows:
        hours = scope_hours if membership.contact_hours is None else membership.contact_hours
        capped = (
            membership.max_asks_per_hour is not None
            and load.get(membership.id, 0) >= membership.max_asks_per_hour
        )
        outside = use_hours and not in_contact_hours(hours, local)
        if capped or outside or any(p is person for _, p in chosen):
            continue
        chosen.append((membership, person))
    return chosen


@dataclass(frozen=True)
class Candidate:
    key: str  # membership id
    p_answer: float
    t80: timedelta


@dataclass(frozen=True)
class Step:
    ask: list[str]  # membership ids, best first
    wait: timedelta


def plan_step(
    candidates: list[Candidate],
    remaining: timedelta,
    max_fanout: int,
    approvals_needed: int = 1,
) -> Step:
    """§9.3, pure. `candidates` are eligible and in tie-break order (lowest authority first).
    Ask the fewest, most responsive people that give TARGET_P of an answer (at least
    `approvals_needed`, at most max_fanout), then wait their slowest t80, but never longer than
    this step's share of the time left."""
    ranked = sorted(candidates, key=lambda c: -c.p_answer)  # stable: keeps the tie-break order
    tiers_left = max(1, -(-len(ranked) // max_fanout))
    window = max(remaining, timedelta(0)) / tiers_left
    chosen: list[Candidate] = []
    p_none = 1.0
    for c in ranked[:max_fanout]:
        chosen.append(c)
        p_none *= 1 - c.p_answer
        if 1 - p_none >= TARGET_P and len(chosen) >= approvals_needed:
            break
    wait = min(max((c.t80 for c in chosen), default=timedelta(0)), window)
    return Step([c.key for c in chosen], wait)


def approvals_needed(decision: Decision, two_person_over: Decimal | None) -> int:
    over = two_person_over is not None and decision.amount is not None
    return 2 if over and decision.amount > two_person_over else 1  # type: ignore[operator]


async def advance(session: AsyncSession, channel: Channel, decision: Decision) -> list[DecisionAsk]:
    """Ask the next people the planner picks. With nobody left, wait for the deadline. If nobody
    can be asked at all, after_hours decides (§9.1): defer expires now, on_call asks on-call
    members."""
    if decision.short_code is None:
        await _assign_short_code(session, decision)
    resolved = await resolve_settings(session, decision.location_id, decision.type)
    if resolved is None:
        raise LookupError(decision.location_id)
    settings = resolved.settings
    location = await session.get_one(Location, decision.location_id)
    label = resolved.policy.body.get("label", decision.type) if resolved.policy else decision.type
    unit = unit_of(resolved.policy.body if resolved.policy else None)
    now = utcnow()
    hours = settings.contact_hours
    candidates = await eligible(session, decision, hours, location.timezone, now)
    already_asked = await session.scalar(
        select(DecisionAsk.id).where(
            DecisionAsk.decision_id == decision.id, DecisionAsk.sent_at.is_not(None)
        )
    )
    if not candidates and already_asked is None:
        anyone = await eligible(session, decision, hours, location.timezone, now, use_hours=False)
        if anyone and settings.after_hours == "on_call":
            candidates = [(m, p) for m, p in anyone if m.on_call]
        if not candidates:
            reason = "after_hours" if anyone else "no_eligible_approver"
            await decisions.expire(session, decision, reason)
            await session.refresh(decision)
            return []

    by_key = {m.id: (m, p) for m, p in candidates}
    asked_total = await session.scalar(
        select(func.count()).where(DecisionAsk.decision_id == decision.id)
    )
    slots = settings.escalation.max_people - (asked_total or 0)
    remaining = decision.deadline_at - now
    needed = approvals_needed(decision, settings.escalation.two_person_over)
    max_segments = settings.sms.max_segments
    sent: list[DecisionAsk] = []
    wait = timedelta(0)
    while by_key and not sent and slots > 0:  # replan without anyone whose send failed
        stats = await history.approver_stats(
            session, [p.id for _, p in by_key.values()], remaining, now
        )
        step = plan_step(
            [Candidate(m.id, stats[p.id].p_answer, stats[p.id].t80) for m, p in by_key.values()],
            remaining,
            min(settings.escalation.max_fanout, slots),
            needed,
        )
        wait = step.wait
        for key in step.ask:
            membership, person = by_key.pop(key)
            slots -= 1
            ask = DecisionAsk(
                decision_id=decision.id,
                person_id=person.id,
                membership_id=membership.id,
                channel="sms",
            )
            session.add(ask)
            await session.flush()
            lines = ask_lines(
                decision.short_code or "",
                location.name,
                label,
                unit,
                decision.amount,
                decision.summary,
                approval_link(ask, decision.deadline_at),
            )
            sms = (
                None
                if person.sms_opted_out
                else sms_text.fit(lines, 1, max_segments, decision.id, droppable=2)
            )
            email = "\n".join(line for line in lines if line)
            used = await send_all(channel, decision.id, person.phone, membership.email, sms, email)
            if not used:
                continue  # the ask row stays, so this person isn't retried for this decision
            ask.channel, ask.sent_at = ",".join(used), now
            sent.append(ask)
    decision.next_action_at = (
        min(now + wait, decision.deadline_at) if sent else decision.deadline_at
    )
    await session.commit()
    return sent


async def decider_name(session: AsyncSession, decision: Decision) -> str | None:
    """The deciding person's name as their agency knows them."""
    if decision.decided_by_person_id is None:
        return None
    return await session.scalar(
        select(Membership.display_name)
        .join(DecisionAsk, DecisionAsk.membership_id == Membership.id)
        .where(
            DecisionAsk.decision_id == decision.id,
            DecisionAsk.person_id == decision.decided_by_person_id,
        )
    )


async def closed_reply(session: AsyncSession, decision: Decision) -> str:
    """D16: decided → "Handled by X"; anything else no longer open → closed."""
    name = await decider_name(session, decision) if decision.status == "decided" else None
    if name:
        return sms_text.plain(f"#{decision.short_code} - Handled by {name}")
    return "That request has closed."


def accepts_late_answers(decision: Decision, now: datetime) -> bool:
    """D16: expired, not yet late-answered, and inside LATE_ANSWER_WINDOW."""
    return (
        decision.status == "expired"
        and decision.late_outcome is None
        and now < decision.deadline_at + LATE_ANSWER_WINDOW
    )


async def answer(session: AsyncSession, channel: Channel, ask: DecisionAsk, cmd: Command) -> str:
    """Apply one approver's reply (SMS or link). Returns what to tell them."""
    decision = await session.get_one(Decision, ask.decision_id)
    late = accepts_late_answers(decision, utcnow())
    if (decision.status != "pending" and not late) or ask.responded_at is not None:
        return await closed_reply(session, decision)
    membership = await session.get_one(Membership, ask.membership_id)
    code = decision.short_code
    resolved = await resolve_settings(session, decision.location_id, decision.type)
    if cmd.kind == "counter" and cmd.value is not None:
        policy_body = resolved.policy.body if resolved and resolved.policy else None
        if problem := counter_problem(cmd.value, decision.amount, unit_of(policy_body)):
            return problem  # the ask stays open, so they can reply again
    two_person_over = resolved.settings.escalation.two_person_over if resolved else None
    required = approvals_needed(decision, two_person_over)
    if required > 1 and cmd.kind == "counter":
        return f"#{code} needs two approvals, so reply 1 to approve or 2 to decline."
    ask.responded_at, ask.response, ask.response_value = utcnow(), cmd.kind, cmd.value

    # Authority is re-checked now, not when the ask went out (§9.4).
    needed = cmd.value if cmd.kind == "counter" else decision.amount
    allowed = membership.active and membership.can_approve
    if cmd.kind != "decline" and needed is not None and needed > membership.authority_limit:
        allowed = False
    if not allowed:
        ask.response = "over_limit"  # never counts towards a two-person approval
    if not allowed and late:
        await session.commit()
        return f"#{code} is above your approval limit."
    if not allowed:
        await session.commit()
        await advance(session, channel, decision)
        return f"#{code} is above your approval limit, so I've asked someone else."
    if late:
        if await decisions.late_answer(session, decision.id, cmd.kind, cmd.value) is None:
            await session.refresh(decision)
            return await closed_reply(session, decision)
        return f"Thanks. #{code} had already timed out, so I've passed your answer on."

    if required > 1 and cmd.kind == "approve":
        await session.flush()
        approvals = await session.scalar(
            select(func.count()).where(
                DecisionAsk.decision_id == decision.id, DecisionAsk.response == "approve"
            )
        )
        if (approvals or 0) < required:
            await session.commit()
            return f"Got it. #{code} needs one more approval."
    template = getattr(resolved.settings.on_answer, cmd.kind) if resolved else ""
    say = render(template, {"value": cmd.value}) or ""
    if (
        await decisions.resolve(session, decision.id, cmd.kind, cmd.value, ask.person_id, say)
        is None
    ):
        await session.refresh(decision)
        return await closed_reply(session, decision)

    handled = sms_text.plain(f"SaySo #{code} - Handled by {membership.display_name}")
    await notify(session, channel, decision, handled, exclude=ask.person_id)
    return f"Done: #{code} {PAST_TENSE[cmd.kind]}."


async def send_all(
    channel: Channel,
    decision_id: str,
    phone: str,
    email: str | None,
    sms: str | None,
    email_text: str | None,
) -> list[str]:
    """Send by SMS and email where there's something to send; return the channels that worked.
    Failures are logged as errors with the decision id only, never the address or provider text."""
    used = []
    for kind, to, text in (("sms", phone, sms), ("email", email, email_text)):
        if not to or not text:
            continue
        try:
            await channel.send(to, text)
            used.append(kind)
        except Exception as e:
            log.error(
                "send failed",
                extra={"decision_id": decision_id, "channel": kind, "error": type(e).__name__},
            )
    return used


async def notify(
    session: AsyncSession,
    channel: Channel,
    decision: Decision,
    text: str,
    exclude: str | None = None,
    sms: bool = True,
) -> None:
    """Tell everyone who was asked. `sms=False` sends email only, for news nobody has to act on."""
    rows = await session.execute(
        select(Person.id, Person.phone, Person.sms_opted_out, Membership.email)
        .join(DecisionAsk, DecisionAsk.person_id == Person.id)
        .join(Membership, DecisionAsk.membership_id == Membership.id)
        .where(DecisionAsk.decision_id == decision.id, DecisionAsk.sent_at.is_not(None))
    )
    for person_id, phone, opted_out, email in rows:
        if person_id != exclude:
            sms_body = text if sms and not opted_out else None
            await send_all(channel, decision.id, phone, email, sms_body, text)


async def notify_expired(session: AsyncSession, channel: Channel, decision: Decision) -> None:
    """D24: email whoever was asked that time ran out; a late answer still gets passed on."""
    location = await session.get_one(Location, decision.location_id)
    text = sms_text.plain(
        f"SaySo #{decision.short_code} - {location.name} - expired: nobody answered in time. "
        f"The caller was told: {decision.outcome}. "
        "You can still answer within 24 hours and it will be passed on."
    )
    await notify(session, channel, decision, text, sms=False)
