import logging
import re
import time
from dataclasses import dataclass
from typing import Any

import httpx
import pytest
from pydantic import SecretStr
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from twilio.request_validator import RequestValidator

from app.core.config import get_settings
from app.core.security import sign_token
from app.models import Decision, DecisionAsk, Membership, Person
from app.services import escalation
from tests.conftest import FakeChannel, Tree, auth

URL = "http://localhost:8000/v1/inbound/twilio/sms"
LEE, RAJ = "+15550000001", "+15550000002"


@dataclass
class Esc:
    tree: Tree
    lee: Membership  # location manager, limit 50
    raj: Membership  # business owner, limit 500


@pytest.fixture
async def esc(make_tree, add_member, session: AsyncSession) -> Esc:
    t: Tree = await make_tree("A")
    t.agency.settings = {"contact_hours": None}  # any time, so tests don't depend on the clock
    await session.commit()
    lee = await add_member(t.agency.id, ("location", t.location.id), LEE, "Lee", 50)
    raj = await add_member(t.agency.id, ("business", t.business.id), RAJ, "Raj", 500, role="owner")
    return Esc(t, lee, raj)


async def ask(
    client: httpx.AsyncClient, key: str, amount: float | None = 40, **kw: Any
) -> dict[str, Any]:
    body = {
        "type": "loaner_car",
        "summary": "Needs a loaner <today>",
        "subject": {"amount": amount},
        **kw,
    }
    r = await client.post("/v1/decisions", json=body, headers=auth(key))
    assert r.status_code == 200, r.text
    return r.json()


async def sms(
    client: httpx.AsyncClient, phone: str, body: str, signature: str | None = None
) -> httpx.Response:
    params = {"From": phone, "Body": body, "MessageSid": "SM123"}
    sig = (
        signature
        if signature is not None
        else RequestValidator("test-twilio-token").compute_signature(URL, params)
    )
    return await client.post(
        "/v1/inbound/twilio/sms", data=params, headers={"X-Twilio-Signature": sig}
    )


async def fetch(client: httpx.AsyncClient, key: str, decision_id: str) -> dict[str, Any]:
    return (await client.get(f"/v1/decisions/{decision_id}", headers=auth(key))).json()


def code_in(text: str) -> str:
    return re.search(r"#([A-Z0-9]{3})", text).group(1)  # type: ignore[union-attr]


def link_in(text: str) -> str:
    return re.search(r"http://localhost:8000(/v1/approve/\S+)", text).group(1)  # type: ignore[union-attr]


async def test_first_ask_goes_to_the_lowest_sufficient_authority(
    client, esc: Esc, channel: FakeChannel
) -> None:
    d = await ask(client, esc.tree.location_key, 40)
    assert d["status"] == "pending" and d["poll_after_ms"] == 1000
    [(to, text)] = channel.sent
    assert to == LEE
    lines = text.splitlines()
    assert re.fullmatch(
        r"SaySo #[2-9A-HJ-NP-Z]{3} · Location A · loaner_car · amount \$40", lines[0]
    )
    assert lines[1:3] == ["Needs a loaner <today>", "1=Yes 2=No or reply a number to counter"]
    assert "/v1/approve/" in lines[3]

    await ask(client, esc.tree.location_key, 100)  # above Lee's limit
    assert channel.sent[-1][0] == RAJ


async def test_sms_answers(client, esc: Esc, channel: FakeChannel) -> None:
    key = esc.tree.location_key
    approve = await ask(client, key, 40)
    code = code_in(channel.to(LEE)[-1])
    r = await sms(client, LEE, "yes")
    assert r.status_code == 200 and r.headers["content-type"].startswith("application/xml")
    d = await fetch(client, key, approve["id"])
    assert (d["status"], d["outcome"], d["decided_by"], d["reason"]) == (
        "decided",
        "approve",
        "human",
        "human",
    )
    assert d["approver"] == {"id": esc.lee.person_id, "name": "Lee"}
    assert d["say"] == "Good news, that's approved." and d["poll_after_ms"] is None
    assert channel.to(LEE)[-1] == f"Done: #{code} approved."

    counter = await ask(client, key, 40)
    await sms(client, LEE, "$30")
    d = await fetch(client, key, counter["id"])
    assert (d["outcome"], d["value"], d["say"]) == ("counter", 30.0, "The best we can do is 30.")

    decline = await ask(client, key, 40)
    await sms(client, LEE, "no")
    assert (await fetch(client, key, decline["id"]))["outcome"] == "decline"


async def test_authority_is_rechecked_when_answering(
    client, esc: Esc, channel: FakeChannel, session
) -> None:
    key = esc.tree.location_key
    d = await ask(client, key, 40)
    esc.lee.authority_limit = 10  # lowered after the ask went out
    await session.commit()
    await sms(client, LEE, "1")
    assert (await fetch(client, key, d["id"]))["status"] == "pending"
    assert "above your approval limit" in channel.to(LEE)[-1]
    assert channel.sent[-2][0] == RAJ  # asked next, before Lee's reply went out


async def test_counter_must_be_below_the_amount(client, esc: Esc, channel: FakeChannel) -> None:
    """D21: a counter is in amount's unit (a discount here), > 0 and < what was asked."""
    key = esc.tree.location_key
    d = await ask(client, key, 40, type="price_adjustment")  # the template escalates
    assert " · discount $40" in channel.to(LEE)[-1].splitlines()[0]  # the SMS shows the unit
    for bad in ("820", "40", "0"):
        await sms(client, LEE, bad)
        assert channel.to(LEE)[-1] == "Counter must be a discount under $40."
        assert (await fetch(client, key, d["id"]))["status"] == "pending"
    assert len(channel.sent) == 4 and channel.to(RAJ) == []  # not escalated, ask still open
    await sms(client, LEE, "15")
    assert (await fetch(client, key, d["id"]))["value"] == 15.0

    other = await ask(client, key, 40)  # no policy, so the unit is just "amount"
    await sms(client, LEE, "99")
    assert channel.to(LEE)[-1] == "Counter must be an amount under $40."
    assert (await fetch(client, key, other["id"]))["status"] == "pending"


async def test_others_hear_it_was_handled(client, esc: Esc, channel: FakeChannel, session) -> None:
    key = esc.tree.location_key
    d = await ask(client, key, 40)
    decision = await session.get(Decision, d["id"])
    await escalation.advance(session, channel, decision)  # what the worker does after the wait
    code = code_in(channel.to(RAJ)[-1])

    await sms(client, RAJ, "1")
    assert channel.to(LEE)[-1] == f"SaySo #{code} · Handled by Raj ✓"
    await sms(client, LEE, "1")  # D16: a reply to a decided decision
    assert channel.to(LEE)[-1] == f"#{code} · Handled by Raj ✓"
    assert (await fetch(client, key, d["id"]))["approver"]["name"] == "Raj"


async def test_closed_and_unknown_replies(client, esc: Esc, channel: FakeChannel, session) -> None:
    d = await ask(client, esc.tree.location_key, 40)
    row = await session.get(Decision, d["id"])
    row.status = "cancelled"
    await session.commit()
    await sms(client, LEE, "1")
    assert channel.to(LEE)[-1] == "That request has closed."
    await sms(client, LEE, "#ZZZ 1")
    assert channel.to(LEE)[-1] == "I can't find #ZZZ."

    await sms(client, LEE, "what is this")
    assert channel.to(LEE)[-1].startswith("Sorry, I didn't get that.")
    await sms(client, RAJ, "repeat customers get $75 off")  # an owner: rule text, phase 7
    assert channel.to(RAJ)[-1] == "Changing rules by text isn't available yet."

    before = len(channel.sent)
    r = await sms(client, "+15559999999", "1")  # not anyone we know
    assert r.status_code == 200 and len(channel.sent) == before


async def test_ambiguous_reply_across_two_agencies(
    client, esc: Esc, make_tree, add_member, session, channel
) -> None:
    other: Tree = await make_tree("B")
    other.agency.settings = {"contact_hours": None}
    await session.commit()
    await add_member(other.agency.id, ("agency", other.agency.id), LEE, "L. Kim", 50)
    a = await ask(client, esc.tree.location_key, 40)
    b = await ask(client, other.location_key, 40)
    code_a, code_b = code_in(channel.to(LEE)[-2]), code_in(channel.to(LEE)[-1])

    await sms(client, LEE, "1")
    assert channel.to(LEE)[-1] == f"Which one? Reply #{code_a} 1 or #{code_b} 1"
    await sms(client, LEE, f"#{code_b.lower()} 1")
    assert (await fetch(client, other.location_key, b["id"]))["approver"]["name"] == "L. Kim"
    assert (await fetch(client, esc.tree.location_key, a["id"]))["status"] == "pending"
    await sms(client, LEE, "1")  # now only one is open
    assert (await fetch(client, esc.tree.location_key, a["id"]))["approver"]["name"] == "Lee"


async def test_stop_and_start(client, esc: Esc, channel: FakeChannel, session) -> None:
    await sms(client, LEE, "STOP")
    assert (await session.scalar(select(Person).where(Person.phone == LEE))).sms_opted_out
    assert channel.sent == []  # the carrier confirms opt-outs, not us
    await ask(client, esc.tree.location_key, 40)
    assert channel.to(LEE) == [] and len(channel.to(RAJ)) == 1  # never texted while opted out
    await sms(client, LEE, "start")
    await ask(client, esc.tree.location_key, 40)
    assert len(channel.to(LEE)) == 1


async def test_nobody_eligible(client, esc: Esc, channel: FakeChannel, session) -> None:
    d = await ask(client, esc.tree.location_key, 1000)  # above everyone's limit
    assert channel.sent == []
    row = await session.get(Decision, d["id"])
    assert row.short_code and row.next_action_at == row.deadline_at  # waits for expiry (phase 4)


async def test_contact_hours_limit_who_is_asked(
    client, esc: Esc, channel: FakeChannel, session
) -> None:
    esc.lee.contact_hours = {}  # never
    esc.tree.agency.settings = {"contact_hours": {}}  # and nobody else either...
    esc.raj.contact_hours = None  # ...so Raj inherits "never" too
    await session.commit()
    await ask(client, esc.tree.location_key, 40)
    assert channel.sent == []
    esc.raj.contact_hours = {
        d: ["00:00-23:59"] for d in ("mon", "tue", "wed", "thu", "fri", "sat", "sun")
    }
    await session.commit()
    await ask(client, esc.tree.location_key, 40)
    assert [to for to, _ in channel.sent] == [RAJ]


async def test_hourly_load_cap(client, esc: Esc, channel: FakeChannel, session) -> None:
    esc.lee.max_asks_per_hour = 1
    await session.commit()
    await ask(client, esc.tree.location_key, 40)
    await ask(client, esc.tree.location_key, 40)
    assert [to for to, _ in channel.sent] == [LEE, RAJ]


async def test_signature_is_required(client, esc: Esc, channel: FakeChannel, monkeypatch) -> None:
    await ask(client, esc.tree.location_key, 40)
    for bad in ("", "nope"):
        r = await sms(client, LEE, "1", signature=bad)
        assert r.status_code == 403
    params = {"From": LEE, "Body": "1", "MessageSid": "SM123"}
    sig = RequestValidator("test-twilio-token").compute_signature(URL, params)
    tampered = await client.post(
        "/v1/inbound/twilio/sms", data={**params, "Body": "2"}, headers={"X-Twilio-Signature": sig}
    )
    assert tampered.status_code == 403
    monkeypatch.setattr(get_settings(), "twilio_auth_token", SecretStr(""))
    empty = RequestValidator("").compute_signature(URL, params)
    assert (await sms(client, LEE, "1", signature=empty)).status_code == 403
    assert len(channel.sent) == 1  # nothing was answered


async def test_approval_link(client, esc: Esc, channel: FakeChannel, session, caplog) -> None:
    d = await ask(client, esc.tree.location_key, 40)
    link = link_in(channel.to(LEE)[-1])
    with caplog.at_level(logging.INFO, logger="sayso"):
        page = await client.get(link)
    assert page.status_code == 200
    assert "Needs a loaner &lt;today&gt;" in page.text and "<today>" not in page.text
    assert "Approve" in page.text
    assert (await fetch(client, esc.tree.location_key, d["id"]))[
        "status"
    ] == "pending"  # GET is read-only
    paths = [getattr(r, "path", None) for r in caplog.records]
    assert "/v1/approve/{token}" in paths and link not in paths  # the token never reaches the logs

    bad = await client.post(link, data={"action": "counter", "value": "abc"})
    assert bad.status_code == 422
    r = await client.post(link, data={"action": "counter", "value": "$45"})
    assert "Counter must be an amount under $40." in r.text  # same rule as SMS
    r = await client.post(link, data={"action": "counter", "value": "$30"})
    assert r.status_code == 200 and "countered" in r.text
    assert (await fetch(client, esc.tree.location_key, d["id"]))["value"] == 30.0
    again = await client.get(link)
    assert "Handled by Lee" in again.text
    assert (
        "Handled by Lee" in (await client.post(link, data={"action": "approve"})).text
    )  # single use


async def test_bad_links(client, esc: Esc, channel: FakeChannel, session) -> None:
    await ask(client, esc.tree.location_key, 40)
    link = link_in(channel.to(LEE)[-1])
    ask_row = await session.scalar(select(DecisionAsk))
    secret = get_settings().signing_secret.get_secret_value().encode()
    expired = sign_token(f"{ask_row.id}:{ask_row.person_id}", int(time.time()) - 1, secret)
    wrong_person = sign_token(f"{ask_row.id}:{esc.raj.person_id}", int(time.time()) + 600, secret)
    for url in (
        link[:-2] + "xx",
        f"/v1/approve/{expired}",
        f"/v1/approve/{wrong_person}",
        "/v1/approve/junk",
    ):
        assert (await client.get(url)).status_code == 404
        assert (await client.post(url, data={"action": "approve"})).status_code == 404
