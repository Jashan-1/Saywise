"""Pure parser for approver SMS replies (§10.2)."""

import re
from dataclasses import dataclass
from decimal import Decimal
from typing import Literal

Kind = Literal["approve", "decline", "counter", "stop", "start", "rules", "text"]

CODE = re.compile(r"^#([A-Za-z0-9]{3})\s+(.+)$")
NUMBER = re.compile(r"^\$?(\d{1,9}(?:\.\d{1,2})?)$")
APPROVE = {"1", "y", "yes", "ok"}
DECLINE = {"2", "n", "no"}
STOP = {"stop", "stopall", "unsubscribe", "cancel", "end", "quit"}  # carrier opt-out words
START = {"start", "unstop"}


@dataclass(frozen=True)
class Command:
    kind: Kind
    code: str | None = None  # short code, upper-case, when the reply named one
    value: Decimal | None = None  # counter-offer

    def reply_token(self) -> str:
        """How this answer is typed, for "Which one? Reply #K7 1" prompts."""
        return {"approve": "1", "decline": "2"}.get(self.kind, str(self.value))


def parse(text: str) -> Command:
    text = " ".join(text.split())
    word = text.lower()
    if word in STOP:
        return Command("stop")
    if word in START:
        return Command("start")
    if word == "rules":
        return Command("rules")
    code = None
    if m := CODE.match(text):
        code, text = m.group(1).upper(), m.group(2)
    reply = text.lower().replace(",", "").rstrip(".!")
    if reply in APPROVE:
        return Command("approve", code)
    if reply in DECLINE:
        return Command("decline", code)
    if m := NUMBER.match(reply):
        return Command("counter", code, Decimal(m.group(1)))
    return Command("text")
