"""Internal tuning values. Client-facing behavior belongs in scope settings, not here."""

from datetime import timedelta

FIXED_WAIT = timedelta(seconds=20)  # retry delay after a failed step
LATE_ANSWER_WINDOW = timedelta(hours=24)  # D16
SHORT_CODE_ALPHABET = "23456789ABCDEFGHJKLMNPQRSTUVWXYZ"  # no 0/O/1/I
SHORT_CODE_LENGTH = 3
LONG_POLL_MAX_MS = 25_000
POLL_AFTER_MS = 1_000
WAIT_CHANNEL = "sayso_decisions"  # LISTEN/NOTIFY channel; payload is the decision id
WORKER_TICK_S = 1.0  # shortcut: polling granularity; move to NOTIFY wakeups for sub-second steps
WORKER_BATCH = 50
CALLBACK_MAX_ATTEMPTS = 8
CALLBACK_BACKOFF = timedelta(seconds=30)  # doubles per attempt...
CALLBACK_MAX_BACKOFF = timedelta(hours=1)  # ...up to this
CALLBACK_TIMEOUT_S = 10

# Adaptive escalation (§9.2, §9.3)
TARGET_P = 0.9  # chance that someone answers, per step
COLD_P_ANSWER = 0.5  # until an approver has STATS_MIN_ASKS asks in this hour bucket
COLD_T80 = timedelta(seconds=20)
STATS_MIN_ASKS = 5
STATS_LOOKBACK = timedelta(days=90)
STATS_CACHE_TTL = timedelta(minutes=5)
