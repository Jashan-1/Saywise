from datetime import datetime
from decimal import Decimal

import pytest

from app.core.constants import SHORT_CODE_ALPHABET
from app.services import sms_text
from app.services.escalation import ask_lines, counter_problem, in_contact_hours, unit_of

DEFAULT = {d: ["07:00-21:00"] for d in ("mon", "tue", "wed", "thu", "fri", "sat", "sun")}
NIGHT_SHIFT = {"mon": ["22:00-06:00"]}


def at(day: int, hhmm: str) -> datetime:
    """2026-10-19 is a Monday; day 0 = mon."""
    h, m = map(int, hhmm.split(":"))
    return datetime(2026, 10, 19 + day, h, m)


@pytest.mark.parametrize(
    ("schedule", "when", "inside"),
    [
        (None, at(0, "03:00"), True),  # null = any time
        ({}, at(0, "12:00"), False),  # no windows = never
        (DEFAULT, at(0, "07:00"), True),  # start is inclusive
        (DEFAULT, at(0, "20:59"), True),
        (DEFAULT, at(0, "21:00"), False),  # end is exclusive
        (DEFAULT, at(0, "06:59"), False),
        (NIGHT_SHIFT, at(0, "23:30"), True),  # Monday's window, Monday night
        (NIGHT_SHIFT, at(1, "05:59"), True),  # ...continues into Tuesday morning
        (NIGHT_SHIFT, at(1, "06:00"), False),
        (NIGHT_SHIFT, at(0, "05:00"), False),  # Monday 05:00 belongs to Sunday's (absent) window
        (NIGHT_SHIFT, at(1, "23:00"), False),  # Tuesday has no window of its own
        ({"sun": ["20:00-02:00"]}, at(0, "01:00"), True),  # Sunday night wraps into Monday
        ({"mon": ["09:00-12:00", "13:00-17:00"]}, at(0, "12:30"), False),
        ({"mon": ["09:00-12:00", "13:00-17:00"]}, at(0, "16:00"), True),
    ],
)
def test_in_contact_hours(schedule, when: datetime, inside: bool) -> None:
    assert in_contact_hours(schedule, when) is inside


def test_ask_lines() -> None:
    lines = ask_lines(
        "K7Z",
        "Fremont Ford",
        "Price adjustment",
        "discount",
        Decimal("50.00"),
        "Wants $800",
        "https://x/v1/approve/t",
    )
    assert lines == [
        "SaySo #K7Z - Fremont Ford - Price adjustment - discount $50",
        "Wants $800",
        "1=Yes 2=No or reply a number to counter",
        "https://x/v1/approve/t",
    ]
    assert "$" not in ask_lines("K7Z", "F", "Refund", "refund", None, None, "l")[0]


@pytest.mark.parametrize(
    ("text", "plain_text"),
    [
        ("Caf\u00e9 \u00b7 Jos\u00e9", "Cafe - Jose"),
        ("\u201cok\u201d \u2014 it\u2019s 5\u2026", '"ok" - it\'s 5...'),
        ("Handled by Raj \u2713", "Handled by Raj "),
        ("price \u20ac40 \U0001f44d", "price EUR40 "),
        ("`tick`", "'tick'"),
    ],
)
def test_plain(text: str, plain_text: str) -> None:
    assert sms_text.plain(text) == plain_text
    assert sms_text.plain(text).isascii()


@pytest.mark.parametrize(
    ("text", "length", "segments"),
    [
        ("a" * 160, 160, 1),
        ("a" * 161, 161, 2),
        ("a" * 306, 306, 2),
        ("a" * 307, 307, 3),
        ("[]" * 40, 160, 1),
        ("{" * 81, 162, 2),
    ],
)
def test_length_and_segments(text: str, length: int, segments: int) -> None:
    assert (sms_text.length(text), sms_text.segments(text)) == (length, segments)


LINK = "https://api.sayso.example/v1/approve/" + "x" * 60


@pytest.mark.parametrize("max_segments", [1, 2, 3])
@pytest.mark.parametrize("summary", ["short", "word " * 200, "\u00e9" * 400, "[" * 300])
def test_fit_trims_the_summary_to_the_budget(max_segments: int, summary: str, caplog) -> None:
    lines = [
        "SaySo #K7Z - Fremont - Refund - refund $40",
        summary,
        "1=Yes 2=No or reply a number to counter",
        LINK,
    ]
    text = sms_text.fit(lines, 1, max_segments, "dec_1", droppable=2)
    assert text.isascii() and sms_text.segments(text) <= max_segments
    assert text.endswith(LINK) and text.startswith("SaySo #K7Z")
    assert not [r for r in caplog.records if r.levelname == "ERROR"]
    if max_segments == 1:  # a link doesn't leave room for anything else
        assert text.count("\n") == 1 and "1=Yes" not in text


def test_fit_logs_an_error_when_even_the_fixed_lines_overflow(caplog) -> None:
    lines = ["H" * 150, "summary", "R", "L" * 100]
    text = sms_text.fit(lines, 1, 1, "dec_9")
    assert "summary" not in text and sms_text.segments(text) == 2  # sent anyway
    [record] = [r for r in caplog.records if r.levelname == "ERROR"]
    assert (record.msg, record.decision_id) == ("sms over max_segments", "dec_9")


def test_short_code_alphabet_avoids_lookalikes() -> None:
    assert not set("0O1I") & set(SHORT_CODE_ALPHABET) and len(set(SHORT_CODE_ALPHABET)) == 32


@pytest.mark.parametrize(
    ("value", "amount", "unit", "problem"),
    [
        ("15", "50", "discount", None),
        ("49.99", "50", "discount", None),
        ("50", "50", "discount", "Counter must be a discount under $50."),
        ("820", "50", "discount", "Counter must be a discount under $50."),
        ("0", "50", "discount", "Counter must be a discount under $50."),
        ("10", "25.50", "refund", None),
        ("30", "25.50", "refund", "Counter must be a refund under $25.50."),
        ("30", "25", "amount", "Counter must be an amount under $25."),
        ("5", None, "amount", None),  # no amount to compare with: only > 0
        ("0", None, "amount", "Counter must be more than 0."),
    ],
)
def test_counter_problem(value: str, amount: str | None, unit: str, problem: str | None) -> None:
    assert counter_problem(Decimal(value), Decimal(amount) if amount else None, unit) == problem


@pytest.mark.parametrize(
    ("body", "unit"),
    [
        ({"amount_label": "discount ($)"}, "discount"),
        ({"amount_label": "above book value ($)"}, "above book value"),
        ({"amount_label": None}, "amount"),
        ({}, "amount"),
        (None, "amount"),
    ],
)
def test_unit_of(body, unit: str) -> None:
    assert unit_of(body) == unit
