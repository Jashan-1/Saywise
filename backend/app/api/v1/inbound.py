"""Approver replies: the Twilio SMS webhook and the one-tap approval page (§10)."""

import html
from decimal import Decimal, InvalidOperation
from urllib.parse import parse_qsl

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse, Response
from sqlalchemy import and_, or_, select
from twilio.request_validator import RequestValidator

from app.api.deps import ChannelDep, SessionDep
from app.channels.base import Channel
from app.core.config import get_settings
from app.core.constants import LATE_ANSWER_WINDOW
from app.core.logging import log
from app.core.security import verify_token
from app.models import Decision, DecisionAsk, Location, Membership, Person, utcnow
from app.services import escalation
from app.services.sms_commands import Command, parse

router = APIRouter(tags=["inbound"], include_in_schema=False)

EMPTY_TWIML = '<?xml version="1.0" encoding="UTF-8"?><Response/>'
HELP = "Sorry, I didn't get that. Reply 1 for yes, 2 for no, or a number to counter."
NOT_YET = "Changing rules by text isn't available yet."


async def form(request: Request) -> dict[str, str]:
    """urlencoded body; parsed with the stdlib so python-multipart isn't needed."""
    return dict(parse_qsl((await request.body()).decode(), keep_blank_values=True))


async def handle_sms(session: SessionDep, channel: Channel, phone: str, body: str) -> str | None:
    """Returns the reply for the sender, if any."""
    person = await session.scalar(select(Person).where(Person.phone == phone))
    if person is None:
        return None
    cmd = parse(body)
    if cmd.kind in ("stop", "start"):  # honoured before anything else; the carrier confirms
        person.sms_opted_out = cmd.kind == "stop"
        await session.commit()
        return None
    if cmd.kind in ("rules", "text"):
        is_owner = await session.scalar(
            select(Membership.id).where(
                Membership.person_id == person.id,
                Membership.active,
                Membership.role.in_(("owner", "agency_admin")),
            )
        )
        return NOT_YET if is_owner else HELP

    # A reply is matched against this phone's open asks in every agency (shared number, §10.2).
    rows = (
        await session.execute(
            select(DecisionAsk, Decision)
            .join(Decision, DecisionAsk.decision_id == Decision.id)
            .where(
                DecisionAsk.person_id == person.id,
                DecisionAsk.sent_at.is_not(None),
                DecisionAsk.responded_at.is_(None),
                or_(
                    Decision.status == "pending",
                    and_(  # still taking late answers (D16)
                        Decision.status == "expired",
                        Decision.late_outcome.is_(None),
                        Decision.deadline_at > utcnow() - LATE_ANSWER_WINDOW,
                    ),
                ),
            )
            .order_by(Decision.created_at)
        )
    ).all()
    open_asks = [(a, d) for a, d in rows if cmd.code in (None, d.short_code)]
    if len(open_asks) > 1:
        options = " or ".join(f"#{d.short_code} {cmd.reply_token()}" for _, d in open_asks)
        return f"Which one? Reply {options}"
    if open_asks:
        return await escalation.answer(session, channel, open_asks[0][0], cmd)

    # Nothing open: say why, for the decision they most likely meant.
    q = (
        select(Decision)
        .join(DecisionAsk, DecisionAsk.decision_id == Decision.id)
        .where(DecisionAsk.person_id == person.id)
        .order_by(Decision.created_at.desc())
        .limit(1)
    )
    if cmd.code is not None:
        q = q.where(Decision.short_code == cmd.code)
    latest = await session.scalar(q)
    if latest is None:
        return f"I can't find #{cmd.code}." if cmd.code else None
    return await escalation.closed_reply(session, latest)


@router.post("/inbound/twilio/sms")
async def twilio_sms(request: Request, session: SessionDep, channel: ChannelDep) -> Response:
    settings = get_settings()
    params = await form(request)
    token = settings.twilio_auth_token.get_secret_value()
    url = f"{settings.public_base_url.rstrip('/')}/v1/inbound/twilio/sms"
    signature = request.headers.get("X-Twilio-Signature", "")
    # An empty auth token would let anyone compute a valid signature.
    if not token or not RequestValidator(token).validate(url, params, signature):
        raise HTTPException(403, "Invalid Twilio signature")
    phone = params.get("From", "")
    reply = await handle_sms(session, channel, phone, params.get("Body", ""))
    if reply:
        try:
            await channel.send(phone, reply)
        except Exception:
            log.warning("sms reply failed")
    return Response(EMPTY_TWIML, media_type="application/xml")


# --- one-tap approval page ------------------------------------------------------------------


def page(title: str, body: str, status: int = 200) -> HTMLResponse:
    return HTMLResponse(
        "<!doctype html><html lang=en><meta charset=utf-8>"
        '<meta name=viewport content="width=device-width,initial-scale=1">'
        f"<title>SaySo · {html.escape(title)}</title>"
        "<body style='font:16px system-ui;max-width:28rem;margin:2rem auto;padding:0 1rem'>"
        f"<h1 style='font-size:1.25rem'>{html.escape(title)}</h1>{body}</body></html>",
        status_code=status,
    )


async def ask_for(session: SessionDep, token: str) -> DecisionAsk | None:
    secret = get_settings().signing_secret.get_secret_value().encode()
    data = verify_token(token, secret)
    if data is None:
        return None
    ask_id, _, person_id = data.partition(":")
    ask = await session.get(DecisionAsk, ask_id)
    return ask if ask is not None and ask.person_id == person_id else None


@router.get("/approve/{token}")
async def approval_page(token: str, session: SessionDep) -> HTMLResponse:
    """Read-only, so link previews that fetch it can't answer anything."""
    ask = await ask_for(session, token)
    if ask is None:
        return page("Link not valid", "<p>This link has expired or is not valid.</p>", 404)
    decision = await session.get_one(Decision, ask.decision_id)
    late = escalation.accepts_late_answers(decision, utcnow())
    if (decision.status != "pending" and not late) or ask.responded_at is not None:
        reply = await escalation.closed_reply(session, decision)
        return page(f"#{decision.short_code}", f"<p>{html.escape(reply)}</p>")
    location = await session.get_one(Location, decision.location_id)
    amount = "" if decision.amount is None else f" · ${decision.amount}"
    button = "style='font:inherit;padding:.6rem 1rem;margin:.25rem 0;width:100%'"
    return page(
        f"#{decision.short_code} · {location.name}",
        f"<p><strong>{html.escape(decision.type)}{html.escape(amount)}</strong></p>"
        f"<p>{html.escape(decision.summary or '')}</p>"
        "<form method=post>"
        f"<button name=action value=approve {button}>Approve</button>"
        f"<button name=action value=decline {button}>Decline</button>"
        "<label>Counter-offer <input name=value inputmode=decimal required "
        "style='font:inherit;width:100%;padding:.5rem'></label>"
        f"<button name=action value=counter {button}>Send counter-offer</button>"
        "</form>",
    )


@router.post("/approve/{token}")
async def approval_submit(
    token: str, request: Request, session: SessionDep, channel: ChannelDep
) -> HTMLResponse:
    ask = await ask_for(session, token)
    if ask is None:
        return page("Link not valid", "<p>This link has expired or is not valid.</p>", 404)
    fields = await form(request)
    action = fields.get("action")
    value = None
    if action == "counter":
        try:
            value = Decimal(fields.get("value", "").strip().lstrip("$").replace(",", ""))
        except InvalidOperation:
            value = None
        if value is None or not value.is_finite() or value < 0:
            return page("Not a number", "<p>Enter the counter-offer as a number.</p>", 422)
    elif action not in ("approve", "decline"):
        return page("Unknown action", "<p>Choose approve, decline or a counter-offer.</p>", 422)
    cmd = (
        Command("counter", value=value)
        if value is not None
        else Command("approve" if action == "approve" else "decline")
    )
    reply = await escalation.answer(session, channel, ask, cmd)
    return page("Thanks", f"<p>{html.escape(reply)}</p>")
