from datetime import datetime
from decimal import Decimal
from typing import Annotated, Any, ClassVar, Literal, Self, get_args
from zoneinfo import ZoneInfo

from pydantic import (
    AfterValidator,
    AnyUrl,
    BaseModel,
    ConfigDict,
    Field,
    PlainSerializer,
    StringConstraints,
    UrlConstraints,
    model_validator,
)

ScopeType = Literal["agency", "business", "location"]
Role = Literal["agency_admin", "owner", "manager", "approver"]
Money = Annotated[
    Decimal,
    Field(ge=0, max_digits=12, decimal_places=2),
    PlainSerializer(float, return_type=float, when_used="json"),
]
Name = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=200)]
E164 = Annotated[str, StringConstraints(pattern=r"^\+[1-9]\d{6,14}$")]
HttpsUrl = Annotated[AnyUrl, UrlConstraints(allowed_schemes=["https"], max_length=2083)]

Day = Literal["mon", "tue", "wed", "thu", "fri", "sat", "sun"]


def _distinct_ends(v: str) -> str:
    if v[:5] == v[6:]:
        raise ValueError("a window's start and end must differ")
    return v


# "HH:MM-HH:MM" in the location's timezone. An end before the start crosses midnight, and the
# window belongs to the day it starts on (D18).
TimeRange = Annotated[
    str,
    StringConstraints(pattern=r"^([01]\d|2[0-3]):[0-5]\d-([01]\d|2[0-3]):[0-5]\d$"),
    AfterValidator(_distinct_ends),
]
WeeklySchedule = dict[Day, list[TimeRange]]
DEFAULT_CONTACT_HOURS: dict[Day, list[str]] = {day: ["07:00-21:00"] for day in get_args(Day)}


def _known_timezone(v: str) -> str:
    try:
        ZoneInfo(v)
    except (KeyError, ValueError, OSError):  # not found, malformed, or a tz directory
        raise ValueError(f"unknown timezone {v!r}") from None
    return v


Timezone = Annotated[str, AfterValidator(_known_timezone)]


class _In(BaseModel):
    model_config = ConfigDict(extra="forbid")


class _Patch(_In):
    """Omit a field to leave it alone. Only fields in NULLABLE may be set to null."""

    NULLABLE: ClassVar[frozenset[str]] = frozenset()

    @model_validator(mode="after")
    def _no_nulls(self) -> Self:
        bad = sorted(
            k for k in self.model_fields_set if getattr(self, k) is None and k not in self.NULLABLE
        )
        if bad:
            raise ValueError(f"{', '.join(bad)} cannot be null")
        return self


class _Out(BaseModel):
    model_config = ConfigDict(from_attributes=True)


# --- client settings (§6.2) -------------------------------------------------------------------


class Deadlines(_In):
    live_ms: int = Field(30_000, ge=5_000, le=120_000)
    async_ms: int = Field(3_600_000, ge=60_000, le=7 * 24 * 3_600_000)


class Escalation(_In):
    max_fanout: int = Field(3, ge=1, le=10)
    two_person_over: Money | None = None


class OnTimeout(_In):
    outcome: Literal["defer", "decline"] = "defer"  # fixed floor: a timeout never approves
    say: str = Field(
        "I wasn't able to confirm that right now. I'll check and get back to you shortly.",
        max_length=300,
    )
    hold_say: str = Field("Let me check on that for you. One moment.", max_length=300)


class Ai(_In):
    summaries: bool = True
    recommendations: bool = True
    rule_drafting: bool = True


class Retention(_In):
    context_days: int = Field(90, ge=1, le=3650)


class ClientSettings(_In):
    deadlines: Deadlines = Field(default_factory=Deadlines)
    escalation: Escalation = Field(default_factory=Escalation)
    # When people may be texted; null means any time (D18).
    contact_hours: WeeklySchedule | None = Field(
        default_factory=lambda: dict(DEFAULT_CONTACT_HOURS)
    )
    after_hours: Literal["defer", "on_call"] = "defer"
    on_timeout: OnTimeout = Field(default_factory=OnTimeout)
    ai: Ai = Field(default_factory=Ai)
    retention: Retention = Field(default_factory=Retention)
    locale: Literal["en"] = "en"
    callback_url: HttpsUrl | None = None


class ResolvedSettingsOut(BaseModel):
    settings: dict[str, Any]
    origins: dict[str, str]  # dotted key -> "default" | "<scope_type>:<id>" | "policy:<id>@<v>"


# --- org structure ----------------------------------------------------------------------------


class BusinessIn(_In):
    name: Name


class BusinessPatch(_Patch):
    name: Name | None = None


class BusinessOut(_Out):
    id: str
    agency_id: str
    name: str


class LocationIn(_In):
    business_id: str
    name: Name
    timezone: Timezone


class LocationPatch(_Patch):
    name: Name | None = None
    timezone: Timezone | None = None


class LocationOut(_Out):
    id: str
    agency_id: str
    business_id: str
    name: str
    timezone: str


# --- people -----------------------------------------------------------------------------------


class PersonIn(_In):
    phone: E164


class PersonOut(_Out):
    id: str
    phone: str


class MembershipIn(_In):
    person_id: str
    scope_type: ScopeType
    scope_id: str
    display_name: Name
    role: Role
    authority_limit: Money = Decimal(0)
    can_approve: bool = True
    on_call: bool = False
    contact_hours: WeeklySchedule | None = None  # null: use the scope's contact_hours
    max_asks_per_hour: int | None = Field(None, ge=1)


class MembershipPatch(_Patch):
    NULLABLE = frozenset({"contact_hours", "max_asks_per_hour"})

    display_name: Name | None = None
    role: Role | None = None
    authority_limit: Money | None = None
    can_approve: bool | None = None
    on_call: bool | None = None
    contact_hours: WeeklySchedule | None = None
    max_asks_per_hour: int | None = Field(None, ge=1)
    active: bool | None = None


class MembershipOut(_Out):
    id: str
    person_id: str
    agency_id: str
    scope_type: str
    scope_id: str
    display_name: str
    role: str
    authority_limit: Money
    can_approve: bool
    on_call: bool
    contact_hours: dict[str, Any] | None
    max_asks_per_hour: int | None
    active: bool


# --- API keys and callback secrets -----------------------------------------------------------


class ApiKeyIn(_In):
    scope_type: ScopeType
    scope_id: str


class ApiKeyOut(_Out):
    id: str
    scope_type: str
    scope_id: str
    prefix: str
    created_at: datetime
    last_used_at: datetime | None
    revoked_at: datetime | None


class ApiKeyCreated(ApiKeyOut):
    key: str  # shown once


class CallbackSecretOut(BaseModel):
    version: int
    secret: str  # shown once
