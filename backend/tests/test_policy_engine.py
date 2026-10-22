import json
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from app.schemas import PolicyBody
from app.services.policy_engine import evaluate, matches, render

FACTS: dict[str, Any] = {
    "type": "price_adjustment",
    "amount": Decimal("50"),
    "context": {
        "requested": 800,
        "quoted": 850.0,
        "vehicle": "truck",
        "nested": {"tier": "gold"},
        "note": None,
        "price": Decimal("800.50"),
    },
    "history": {},
}


def cmp(var: str, op: str, value: Any = None) -> dict[str, Any]:
    return {"var": var, "op": op, "value": value}


AMOUNT_OK = cmp("amount", "<=", 50)
MISSING = cmp("context.color", "==", "red")

CONDITIONS = [
    (AMOUNT_OK, True),
    (cmp("amount", "<", 50), False),
    (cmp("amount", "==", 50), True),  # Decimal vs int
    (cmp("amount", "!=", 50), False),
    (cmp("amount", ">", 49.5), True),  # Decimal vs float
    (cmp("amount", ">=", 51), False),
    (cmp("type", "==", "price_adjustment"), True),
    (cmp("context.vehicle", "in", ["truck", "suv"]), True),
    (cmp("context.vehicle", "not_in", ["truck"]), False),
    (cmp("context.nested.tier", "==", "gold"), True),
    (MISSING, False),
    (cmp("context.color", "!=", "red"), False),  # a missing fact is false, even for !=
    (cmp("history.approved_count", ">=", 3), False),
    (cmp("context.vehicle", "exists"), True),
    (cmp("context.note", "exists"), False),  # null counts as absent
    (cmp("context.nope", "exists"), False),
    (cmp("context.vehicle", ">", 5), False),  # type mismatch never raises
    (cmp("context.vehicle.length", "==", 5), False),  # path through a non-object
    (cmp("context.vehicle", "in", 5), False),  # `in` against a non-container
    (cmp("amount", "~=", 50), False),  # unknown operator
    ({"all": [AMOUNT_OK, cmp("context.vehicle", "==", "truck")]}, True),
    ({"all": [AMOUNT_OK, MISSING]}, False),
    ({"any": [MISSING, AMOUNT_OK]}, True),
    ({"any": [MISSING, cmp("amount", ">", 100)]}, False),
    ({"not": MISSING}, True),
    ({"not": AMOUNT_OK}, False),
    ({"all": [{"any": [MISSING, AMOUNT_OK]}, {"not": cmp("context.vehicle", "==", "suv")}]}, True),
    ({}, False),
]


@pytest.mark.parametrize(("cond", "expected"), CONDITIONS)
def test_matches(cond: dict[str, Any], expected: bool) -> None:
    assert matches(cond, FACTS) is expected


@pytest.mark.parametrize(
    ("template", "expected"),
    [
        ("We can do {context.requested}.", "We can do 800."),
        ("Best is {context.quoted}.", "Best is 850."),  # 850.0 reads as 850
        ("Price {context.price}", "Price 800.50"),
        ("Off {amount} for your {context.vehicle}", "Off 50 for your truck"),
        ("Hi {context.name}!", "Hi !"),  # missing renders empty
        ("Literal {not a path} stays", "Literal {not a path} stays"),
        (None, None),
    ],
)
def test_render(template: str | None, expected: str | None) -> None:
    assert render(template, FACTS) == expected


def rule(rule_id: str, when: dict[str, Any], then: str, **kw: Any) -> dict[str, Any]:
    return {"id": rule_id, "when": when, "then": then, **kw}


@pytest.mark.parametrize(
    ("policy", "outcome", "reason", "rule_id", "value", "say"),
    [
        (
            {
                "rules": [
                    rule("a", AMOUNT_OK, "approve", say="ok {context.requested}"),
                    rule("b", AMOUNT_OK, "decline"),
                ]
            },
            "approve",
            "rule_matched",
            "a",
            None,
            "ok 800",
        ),  # first match wins
        (
            {"rules": [rule("a", MISSING, "approve")], "default": "escalate"},
            "escalate",
            "policy_default",
            None,
            None,
            None,
        ),
        ({"rules": [], "default": "decline"}, "decline", "policy_default", None, None, None),
        (
            {"rules": [rule("c", AMOUNT_OK, "counter", value=820)]},
            "counter",
            "rule_matched",
            "c",
            Decimal("820"),
            None,
        ),
        (
            {"rules": [rule("e", AMOUNT_OK, "escalate")]},
            "escalate",
            "rule_matched",
            "e",
            None,
            None,
        ),
        ({}, "escalate", "policy_default", None, None, None),
        (
            {"rules": [{"id": "broken", "when": AMOUNT_OK}]},
            "escalate",
            "rule_matched",
            "broken",
            None,
            None,
        ),
    ],
)
def test_evaluate(
    policy: dict[str, Any],
    outcome: str,
    reason: str,
    rule_id: str | None,
    value: Decimal | None,
    say: str | None,
) -> None:
    v = evaluate(policy, FACTS)
    assert (v.outcome, v.reason, v.rule_id, v.value, v.say) == (
        outcome,
        reason,
        rule_id,
        value,
        say,
    )


TEMPLATES = sorted(
    (Path(__file__).parents[1] / "app" / "templates" / "decision_types").glob("*.json")
)
EXAMPLE = Path(__file__).parents[2] / "examples" / "policies" / "price_adjustment.json"


@pytest.mark.parametrize("path", [*TEMPLATES, EXAMPLE], ids=lambda p: p.stem)
def test_shipped_policies_are_valid(path: Path) -> None:
    body = PolicyBody.model_validate(json.loads(path.read_text()))
    assert body.decision_type == path.stem


@pytest.mark.parametrize("path", TEMPLATES, ids=lambda p: p.stem)
def test_templates_are_safe_defaults(path: Path) -> None:
    raw = json.loads(path.read_text())
    # System overrides would beat every agency's own settings, and a placeholder can render empty.
    assert "overrides" not in raw
    assert all("{" not in r.get("say", "") for r in raw["rules"])
    assert len(TEMPLATES) == 4
