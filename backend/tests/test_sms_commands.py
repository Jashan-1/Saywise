from decimal import Decimal

import pytest

from app.services.sms_commands import Command, parse


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("1", Command("approve")),
        ("y", Command("approve")),
        ("Yes", Command("approve")),
        (" OK! ", Command("approve")),
        ("2", Command("decline")),
        ("n", Command("decline")),
        ("No.", Command("decline")),
        ("820", Command("counter", value=Decimal("820"))),
        ("$1,200.50", Command("counter", value=Decimal("1200.50"))),
        ("#K7Z 1", Command("approve", "K7Z")),
        ("#K7 1", Command("text")),  # codes are 3 characters (D16)
        ("#k7x 2", Command("decline", "K7X")),
        ("#M2Q 450", Command("counter", "M2Q", Decimal("450"))),
        ("#M2Q   yes", Command("approve", "M2Q")),
        ("STOP", Command("stop")),
        ("unsubscribe", Command("stop")),
        ("Start", Command("start")),
        ("RULES", Command("rules")),
        ("repeat customers can get $75 off", Command("text")),
        ("#K7Q hello", Command("text")),  # a code with an unknown answer is just text
        ("#K7Q", Command("text")),
        ("12.345", Command("text")),  # at most two decimals
        ("-5", Command("text")),
        ("", Command("text")),
        ("1 2", Command("text")),
    ],
)
def test_parse(text: str, expected: Command) -> None:
    assert parse(text) == expected


@pytest.mark.parametrize(
    ("cmd", "token"),
    [
        (Command("approve"), "1"),
        (Command("decline"), "2"),
        (Command("counter", value=Decimal("45")), "45"),
    ],
)
def test_reply_token(cmd: Command, token: str) -> None:
    assert cmd.reply_token() == token
