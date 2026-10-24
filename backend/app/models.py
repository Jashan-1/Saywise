from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    MetaData,
    Numeric,
    Text,
    UniqueConstraint,
    false,
    func,
    text,
    true,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, MappedColumn, mapped_column
from ulid import ULID

from app.core.security import generate_api_key, hash_key, key_prefix

Json = dict[str, Any]


def new_id(prefix: str) -> str:
    return f"{prefix}_{ULID()}"


def utcnow() -> datetime:
    return datetime.now(UTC)


def pk(prefix: str) -> MappedColumn[str]:
    return mapped_column(primary_key=True, default=lambda: new_id(prefix))


class Base(DeclarativeBase):
    metadata = MetaData(
        naming_convention={
            "ix": "ix_%(table_name)s_%(column_0_N_name)s",
            "uq": "uq_%(table_name)s_%(column_0_N_name)s",
            "ck": "ck_%(table_name)s_%(constraint_name)s",
            "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
            "pk": "pk_%(table_name)s",
        }
    )
    type_annotation_map = {
        Json: JSONB,
        datetime: DateTime(timezone=True),
        Decimal: Numeric(12, 2),
        str: Text,
    }


class Timestamps:
    created_at: Mapped[datetime] = mapped_column(default=utcnow, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        default=utcnow, onupdate=utcnow, server_default=func.now()
    )


class ScopedSettings(Timestamps):
    """Agencies, businesses and locations each hold partial settings overrides (§6.1)."""

    settings: Mapped[Json] = mapped_column(default=dict, server_default=text("'{}'::jsonb"))


class Agency(ScopedSettings, Base):
    __tablename__ = "agencies"
    id: Mapped[str] = pk("agy")
    name: Mapped[str]
    callback_secret_version: Mapped[int] = mapped_column(default=1, server_default="1")


class Business(ScopedSettings, Base):
    __tablename__ = "businesses"
    id: Mapped[str] = pk("bus")
    agency_id: Mapped[str] = mapped_column(ForeignKey("agencies.id"), index=True)
    name: Mapped[str]


class Location(ScopedSettings, Base):
    __tablename__ = "locations"
    id: Mapped[str] = pk("loc")
    agency_id: Mapped[str] = mapped_column(ForeignKey("agencies.id"), index=True)
    business_id: Mapped[str] = mapped_column(ForeignKey("businesses.id"), index=True)
    name: Mapped[str]
    timezone: Mapped[str]


class ApiKey(Timestamps, Base):
    __tablename__ = "api_keys"
    id: Mapped[str] = pk("key")
    agency_id: Mapped[str] = mapped_column(ForeignKey("agencies.id"), index=True)
    scope_type: Mapped[str]
    scope_id: Mapped[str]
    prefix: Mapped[str] = mapped_column(unique=True)
    key_hash: Mapped[str]
    last_used_at: Mapped[datetime | None]
    revoked_at: Mapped[datetime | None]

    @classmethod
    def issue(cls, agency_id: str, scope_type: str, scope_id: str) -> tuple["ApiKey", str]:
        """A new key row plus the plaintext key, which is never stored."""
        key = generate_api_key()
        row = cls(
            agency_id=agency_id,
            scope_type=scope_type,
            scope_id=scope_id,
            prefix=key_prefix(key),
            key_hash=hash_key(key),
        )
        return row, key


class Person(Timestamps, Base):
    """Global and keyed by phone: the shared SMS number means one person can serve many agencies.
    Names live on memberships so agencies never see each other's labels."""

    __tablename__ = "people"
    id: Mapped[str] = pk("per")
    phone: Mapped[str] = mapped_column(unique=True)
    sms_opted_out: Mapped[bool] = mapped_column(default=False, server_default=false())


class Membership(Timestamps, Base):
    __tablename__ = "memberships"
    __table_args__ = (
        UniqueConstraint("person_id", "scope_type", "scope_id"),
        Index(None, "scope_type", "scope_id"),
    )
    id: Mapped[str] = pk("mem")
    person_id: Mapped[str] = mapped_column(ForeignKey("people.id"))
    agency_id: Mapped[str] = mapped_column(ForeignKey("agencies.id"), index=True)
    scope_type: Mapped[str]
    scope_id: Mapped[str]
    display_name: Mapped[str]
    email: Mapped[str | None]  # per agency, like the name (D24)
    role: Mapped[str]
    authority_limit: Mapped[Decimal] = mapped_column(default=Decimal(0), server_default="0")
    can_approve: Mapped[bool] = mapped_column(default=True, server_default=true())
    on_call: Mapped[bool] = mapped_column(default=False, server_default=false())
    contact_hours: Mapped[Json | None]  # null: use the scope's contact_hours
    max_asks_per_hour: Mapped[int | None]
    active: Mapped[bool] = mapped_column(default=True, server_default=true())


class Policy(Timestamps, Base):
    """One row per version. Bodies are never updated; a PUT inserts a new version."""

    __tablename__ = "policies"
    __table_args__ = (
        UniqueConstraint("scope_type", "scope_id", "decision_type", "version"),
        Index(
            "uq_policies_active",
            "scope_type",
            "scope_id",
            "decision_type",
            unique=True,
            postgresql_where=text("active"),
        ),
    )
    id: Mapped[str] = pk("pol")
    agency_id: Mapped[str | None] = mapped_column(ForeignKey("agencies.id"), index=True)
    scope_type: Mapped[str]
    scope_id: Mapped[str]  # "system" for starter templates
    decision_type: Mapped[str]
    version: Mapped[int]
    body: Mapped[Json]
    active: Mapped[bool] = mapped_column(default=True, server_default=true())
    created_by: Mapped[str | None]
    source: Mapped[str]
    source_text: Mapped[str | None]


# Why a decision is where it is (D20).
REASONS = (
    "rule_matched",
    "policy_default",
    "no_policy",
    "human",
    "timeout",
    "after_hours",
    "no_eligible_approver",
    "cancelled",
)


class Decision(Timestamps, Base):
    __tablename__ = "decisions"
    __table_args__ = (
        Index(
            "ix_decisions_due",
            "status",
            "next_action_at",
            postgresql_where=text("status = 'pending'"),
        ),
        Index(None, "agency_id", "location_id", "created_at"),
        Index(None, "agency_id", "customer_ref"),
        UniqueConstraint("agency_id", "idempotency_key"),
        Index(
            "uq_decisions_pending_short_code",
            "short_code",
            unique=True,
            postgresql_where=text("status = 'pending'"),
        ),
        # Safety floor, enforced again in the database: AI never decides, a timeout never approves.
        CheckConstraint("decided_by IN ('policy', 'human', 'timeout')", name="decided_by"),
        CheckConstraint(
            "decided_by <> 'timeout' OR outcome IN ('defer', 'decline')",
            name="timeout_never_approves",
        ),
        CheckConstraint(f"reason IN ({', '.join(repr(r) for r in REASONS)})", name="reason"),
    )
    id: Mapped[str] = pk("dec")
    agency_id: Mapped[str] = mapped_column(ForeignKey("agencies.id"))
    location_id: Mapped[str] = mapped_column(ForeignKey("locations.id"))
    type: Mapped[str]
    mode: Mapped[str]
    status: Mapped[str] = mapped_column(default="pending", server_default="pending")
    summary: Mapped[str | None]
    subject: Mapped[Json] = mapped_column(default=dict, server_default=text("'{}'::jsonb"))
    context: Mapped[Json | None]
    customer_ref: Mapped[str | None]
    amount: Mapped[Decimal | None]
    policy_id: Mapped[str | None] = mapped_column(ForeignKey("policies.id"))
    policy_version: Mapped[int | None]
    rule_id: Mapped[str | None]
    outcome: Mapped[str | None]
    value: Mapped[Decimal | None]
    say: Mapped[str | None]  # the line the agent should speak now; scrubbed with context
    reason: Mapped[str | None]
    decided_by: Mapped[str | None]
    decided_by_person_id: Mapped[str | None] = mapped_column(ForeignKey("people.id"))
    late_outcome: Mapped[str | None]
    late_value: Mapped[Decimal | None]  # a late counter-offer
    short_code: Mapped[str | None]
    deadline_at: Mapped[datetime]
    next_action_at: Mapped[datetime | None]
    callback_url: Mapped[str | None]
    idempotency_key: Mapped[str | None]
    request_hash: Mapped[str | None]
    context_scrubbed_at: Mapped[datetime | None]
    decided_at: Mapped[datetime | None]


class DecisionAsk(Timestamps, Base):
    __tablename__ = "decision_asks"
    __table_args__ = (
        UniqueConstraint("decision_id", "person_id"),  # no one is asked twice (§9.4)
        Index(None, "person_id", "responded_at"),
    )
    id: Mapped[str] = pk("ask")
    decision_id: Mapped[str] = mapped_column(ForeignKey("decisions.id"))
    person_id: Mapped[str] = mapped_column(ForeignKey("people.id"))
    membership_id: Mapped[str] = mapped_column(ForeignKey("memberships.id"))
    channel: Mapped[str]
    provider_message_id: Mapped[str | None]
    ai_recommendation: Mapped[str | None]
    sent_at: Mapped[datetime | None]
    responded_at: Mapped[datetime | None]
    response: Mapped[str | None]
    response_value: Mapped[Decimal | None]


class RuleDraft(Timestamps, Base):
    __tablename__ = "rule_drafts"
    id: Mapped[str] = pk("drf")
    person_id: Mapped[str] = mapped_column(ForeignKey("people.id"), index=True)
    scope_type: Mapped[str]
    scope_id: Mapped[str]
    decision_type: Mapped[str]
    text: Mapped[str]
    draft: Mapped[Json]
    expires_at: Mapped[datetime]
    status: Mapped[str] = mapped_column(default="pending", server_default="pending")


class Suggestion(Timestamps, Base):
    __tablename__ = "suggestions"
    id: Mapped[str] = pk("sug")
    agency_id: Mapped[str] = mapped_column(ForeignKey("agencies.id"), index=True)
    scope_type: Mapped[str]
    scope_id: Mapped[str]
    decision_type: Mapped[str]
    rule: Mapped[Json]
    evidence: Mapped[Json]
    status: Mapped[str] = mapped_column(default="open", server_default="open")


class CallbackDelivery(Timestamps, Base):
    __tablename__ = "callback_deliveries"
    __table_args__ = (
        Index(
            "ix_callback_deliveries_due",
            "next_attempt_at",
            postgresql_where=text("delivered_at IS NULL"),
        ),
    )
    id: Mapped[str] = pk("cbd")
    decision_id: Mapped[str] = mapped_column(ForeignKey("decisions.id"), index=True)
    event: Mapped[str]
    attempt: Mapped[int] = mapped_column(default=0, server_default="0")
    status_code: Mapped[int | None]
    next_attempt_at: Mapped[datetime | None]
    delivered_at: Mapped[datetime | None]


class AiCall(Timestamps, Base):
    __tablename__ = "ai_calls"
    id: Mapped[str] = pk("aic")
    agency_id: Mapped[str] = mapped_column(ForeignKey("agencies.id"), index=True)
    location_id: Mapped[str | None] = mapped_column(ForeignKey("locations.id"))
    feature: Mapped[str]
    model: Mapped[str]
    tokens_in: Mapped[int]
    tokens_out: Mapped[int]
    latency_ms: Mapped[int]
    ok: Mapped[bool]


class WorkerJob(Timestamps, Base):
    __tablename__ = "worker_jobs"
    name: Mapped[str] = mapped_column(primary_key=True)
    last_run_at: Mapped[datetime | None]
