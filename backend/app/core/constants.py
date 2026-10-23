"""Internal tuning values. Client-facing behavior belongs in scope settings, not here."""

from datetime import timedelta

FIXED_WAIT = timedelta(seconds=20)  # wait per approver until adaptive timing (phase 5)
LATE_ANSWER_WINDOW = timedelta(hours=24)  # D16
SHORT_CODE_ALPHABET = "23456789ABCDEFGHJKLMNPQRSTUVWXYZ"  # no 0/O/1/I
SHORT_CODE_LENGTH = 3
LONG_POLL_MAX_MS = 25_000
POLL_AFTER_MS = 1_000
WAIT_CHANNEL = "sayso_decisions"  # LISTEN/NOTIFY channel; payload is the decision id
