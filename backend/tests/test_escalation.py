from datetime import datetime
from decimal import Decimal

import pytest

from app.core.constants import SHORT_CODE_ALPHABET
from app.services.escalation import ask_text, in_contact_hours

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


def test_ask_text() -> None:
    text = ask_text(
        "K7Z",
        "Fremont Ford",
        "Price adjustment",
        Decimal("50.00"),
        "Wants $800",
        "https://x/v1/approve/t",
    )
    assert text.splitlines() == [
        "SaySo #K7Z · Fremont Ford · Price adjustment $50",
        "Wants $800",
        "1=Yes 2=No or reply a number to counter",
        "https://x/v1/approve/t",
    ]
    assert "$" not in ask_text("K7Z", "F", "Refund", None, None, "l").splitlines()[0]


def test_short_code_alphabet_avoids_lookalikes() -> None:
    assert not set("0O1I") & set(SHORT_CODE_ALPHABET) and len(set(SHORT_CODE_ALPHABET)) == 32
