import pytest

from app.services.scopes import SYSTEM, Principal, can

LOC = [("location", "loc_1"), ("business", "bus_1"), ("agency", "agy_1"), SYSTEM]
BUS = LOC[1:]
AGY = LOC[2:]
SIBLING = [("location", "loc_2"), ("business", "bus_1"), ("agency", "agy_1"), SYSTEM]
OTHER_AGENCY = [("location", "loc_9"), ("business", "bus_9"), ("agency", "agy_9"), SYSTEM]


def member(role: str, scope: tuple[str, str] = ("agency", "agy_1"), **kw: bool) -> Principal:
    return Principal(agency_id="agy_1", scope=scope, role=role, **kw)


def key(scope: tuple[str, str]) -> Principal:
    return Principal(agency_id="agy_1", scope=scope)


ACTIONS = ("approve", "edit_rules", "manage_people")

# §5.3, one row per role: which of (approve, edit rules, manage people) it may do.
ROLE_MATRIX = {
    "agency_admin": (True, True, True),
    "owner": (True, True, True),
    "manager": (True, False, False),
    "approver": (True, False, False),
}


@pytest.mark.parametrize("role", ROLE_MATRIX)
@pytest.mark.parametrize("i", range(len(ACTIONS)))
def test_role_matrix(role: str, i: int) -> None:
    assert can(member(role), ACTIONS[i], LOC) is ROLE_MATRIX[role][i]  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("principal", "action", "target", "allowed"),
    [
        # Roles only reach their own scope and below.
        (member("owner", ("business", "bus_1")), "edit_rules", LOC, True),
        (member("owner", ("business", "bus_1")), "edit_rules", AGY, False),
        (member("owner", ("location", "loc_1")), "manage_people", SIBLING, False),
        (member("manager", ("business", "bus_1")), "approve", SIBLING, True),
        (member("agency_admin"), "approve", OTHER_AGENCY, False),
        # can_approve=False removes approval but nothing else.
        (member("owner", can_approve=False), "approve", LOC, False),
        (member("owner", can_approve=False), "edit_rules", LOC, True),
        # Roles never get settings or org management; those are API-key actions.
        (member("agency_admin"), "manage_settings", LOC, False),
        (member("agency_admin"), "manage_org", AGY, False),
        # API keys: anything in their subtree, but only agency keys shape the org.
        (key(("agency", "agy_1")), "manage_org", BUS, True),
        (key(("business", "bus_1")), "manage_org", BUS, False),
        (key(("business", "bus_1")), "manage_settings", LOC, True),
        (key(("business", "bus_1")), "read", AGY, False),
        (key(("location", "loc_1")), "read", LOC, True),
        (key(("location", "loc_1")), "read", SIBLING, False),
        (key(("agency", "agy_1")), "read", OTHER_AGENCY, False),
        # A key whose own agency isn't on the target chain reaches nothing, even "in scope".
        (Principal(agency_id="agy_9", scope=("location", "loc_1")), "read", LOC, False),
    ],
)
def test_can(
    principal: Principal, action: str, target: list[tuple[str, str]], allowed: bool
) -> None:
    assert can(principal, action, target) is allowed  # type: ignore[arg-type]
