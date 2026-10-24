"""Plain, segment-bounded SMS text (D23). Carriers bill per segment: 160 GSM-7 characters for a
single message, 153 per segment once split. One non-GSM character (emoji, curly quote, ·)
would switch the whole message to UCS-2 at 70 per segment, so everything is made plain first."""

import math
import unicodedata

from app.core.logging import log

SINGLE, PER_SEGMENT = 160, 153
GSM_EXTENDED = set("^{}\\[]~|")  # in GSM-7 but count as two characters
REPLACE = {
    "‘": "'",
    "’": "'",
    "“": '"',
    "”": '"',
    "–": "-",
    "—": "-",
    "−": "-",
    "·": "-",
    "…": "...",
    "`": "'",
    "€": "EUR",
    "✓": "",
}


def plain(text: str) -> str:
    """ASCII only: known punctuation mapped, accents stripped, anything else dropped."""
    text = "".join(REPLACE.get(ch, ch) for ch in text)
    return unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode()


def length(text: str) -> int:
    """GSM-7 characters used by plain text."""
    return len(text) + sum(ch in GSM_EXTENDED for ch in text)


def segments(text: str) -> int:
    n = length(text)
    return 1 if n <= SINGLE else math.ceil(n / PER_SEGMENT)


def budget(max_segments: int) -> int:
    return SINGLE if max_segments == 1 else PER_SEGMENT * max_segments


def fit(
    lines: list[str],
    trim: int,
    max_segments: int,
    decision_id: str | None = None,
    droppable: int | None = None,
) -> str:
    """Join plain lines so the message fits max_segments: shorten lines[trim] (the summary)
    first, then drop lines[droppable] if that's still not enough. If it still doesn't fit it is
    sent anyway, and the overrun is logged as an error."""
    lines = [plain(line) for line in lines]
    room = budget(max_segments)

    def rest(skip: set[int]) -> int:
        return length("\n".join(line for i, line in enumerate(lines) if i not in skip and line))

    if length("\n".join(line for line in lines if line)) > room:
        space = room - rest({trim}) - 1  # the newline before the trimmed line
        cut = lines[trim]
        while cut and length(cut) + 3 > space:
            cut = cut[:-1]
        lines[trim] = cut.rstrip() + "..." if cut.strip() else ""
    if droppable is not None and rest(set()) > room:
        lines[droppable] = ""
    text = "\n".join(line for line in lines if line)
    if segments(text) > max_segments:
        log.error("sms over max_segments", extra={"decision_id": decision_id, "channel": "sms"})
    return text
