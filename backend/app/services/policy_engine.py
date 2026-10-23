"""Pure policy evaluation (§8.2). The first matching rule wins, otherwise the policy default.
No eval and no I/O. A missing fact makes its comparison false and never raises."""

import operator
import re
from collections.abc import Callable
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

MISSING = object()

OPS: dict[str, Callable[[Any, Any], Any]] = {
    "==": operator.eq,
    "!=": operator.ne,
    "<": operator.lt,
    "<=": operator.le,
    ">": operator.gt,
    ">=": operator.ge,
    "in": lambda actual, value: actual in value,
    "not_in": lambda actual, value: actual not in value,
}
PLACEHOLDER = re.compile(r"\{([A-Za-z0-9_.]+)\}")


@dataclass(frozen=True)
class Verdict:
    outcome: str  # approve | decline | counter | escalate
    reason: str  # rule_matched | policy_default | no_policy
    rule_id: str | None = None
    value: Decimal | None = None
    say: str | None = None


def lookup(facts: dict[str, Any], path: str) -> Any:
    value: Any = facts
    for part in path.split("."):
        if not isinstance(value, dict) or part not in value:
            return MISSING
        value = value[part]
    return value


def matches(cond: dict[str, Any], facts: dict[str, Any]) -> bool:
    if "all" in cond:
        return all(matches(c, facts) for c in cond["all"])
    if "any" in cond:
        return any(matches(c, facts) for c in cond["any"])
    if "not" in cond:
        return not matches(cond["not"], facts)
    actual = lookup(facts, cond.get("var", ""))
    if cond.get("op") == "exists":
        return actual is not MISSING and actual is not None
    op = OPS.get(cond.get("op", ""))
    if op is None or actual is MISSING:
        return False
    try:
        return bool(op(actual, cond.get("value")))
    except TypeError:  # e.g. "truck" > 5, or `in` against a non-container
        return False


def format_value(value: Any) -> str:
    if value is MISSING or value is None:
        return ""
    if isinstance(value, float | Decimal):
        try:
            if value == int(value):
                return str(int(value))
        except (ValueError, OverflowError):  # NaN, infinity
            pass
    return str(value)


def render(template: str | None, facts: dict[str, Any]) -> str | None:
    """`{path}` substitution only. A missing path renders as an empty string."""
    if template is None:
        return None
    return PLACEHOLDER.sub(lambda m: format_value(lookup(facts, m.group(1))), template)


def evaluate(policy: dict[str, Any], facts: dict[str, Any]) -> Verdict:
    for rule in policy.get("rules", []):
        if matches(rule.get("when", {}), facts):
            value = rule.get("value")
            return Verdict(
                outcome=rule.get("then", "escalate"),
                reason="rule_matched",
                rule_id=rule.get("id"),
                value=None if value is None else Decimal(str(value)),
                say=render(rule.get("say"), facts),
            )
    return Verdict(outcome=policy.get("default", "escalate"), reason="policy_default")
