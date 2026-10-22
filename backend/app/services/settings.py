"""Layered client settings (§6): code defaults → agency → business → location → policy overrides
→ request (tighten-only). Nearest wins; each value reports which layer it came from."""

from dataclasses import dataclass
from typing import Any

from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Agency, Business, Location, Policy
from app.schemas import ClientSettings
from app.services.scopes import SYSTEM, nearest_policy

Layer = tuple[str, dict[str, Any]]  # (origin, partial overrides)

# Top-level keys whose value is a group of settings: layers merge into them key by key.
# Every other key (quiet_hours included) is replaced whole by the nearest layer that sets it.
SECTIONS = frozenset(
    name
    for name, field in ClientSettings.model_fields.items()
    if isinstance(field.annotation, type) and issubclass(field.annotation, BaseModel)
)

# What a single request may lower but never raise (§6.2).
TIGHTENABLE = frozenset(
    {("deadlines", "live_ms"), ("deadlines", "async_ms"), ("escalation", "max_fanout")}
)


@dataclass
class Resolved:
    settings: ClientSettings
    origins: dict[str, str]
    policy: Policy | None


def merge(layers: list[Layer]) -> tuple[dict[str, Any], dict[str, str]]:
    """Merge partial layers over the code defaults. Returns (unvalidated values, origins)."""
    merged = ClientSettings().model_dump()
    origins: dict[str, str] = {}
    for key, value in merged.items():
        if key in SECTIONS:
            origins.update({f"{key}.{sub}": "default" for sub in value})
        else:
            origins[key] = "default"
    for origin, layer in layers:
        for key, value in layer.items():
            if key in SECTIONS and isinstance(value, dict):
                merged[key] = {**merged[key], **value}
                origins.update({f"{key}.{sub}": origin for sub in value})
            else:
                merged[key] = value
                origins[key] = origin
    return merged, origins


def validate_layer(partial: dict[str, Any]) -> None:
    """Raise pydantic.ValidationError if one scope's partial overrides are invalid on their own."""
    ClientSettings.model_validate(merge([("layer", partial)])[0])


def apply_patch(stored: dict[str, Any], patch: dict[str, Any]) -> dict[str, Any]:
    """Set keys on one scope's overrides; sections merge key by key. null is a value, not a
    delete (contact_hours: null = any time). Dropping an override is a PUT of the whole set."""
    out = dict(stored)
    for key, value in patch.items():
        if key in SECTIONS and isinstance(value, dict):
            out[key] = {**out.get(key, {}), **value}
        else:
            out[key] = value
    return out


def tighten(
    merged: dict[str, Any], origins: dict[str, str], request: dict[str, dict[str, int]]
) -> None:
    """Apply per-request overrides in place. Lower values win; higher ones are ignored."""
    for section, values in request.items():
        for key, value in values.items():
            if (section, key) not in TIGHTENABLE:
                raise ValueError(f"a request cannot override {section}.{key}")
            if value < merged[section][key]:
                merged[section][key] = value
                origins[f"{section}.{key}"] = "request"


async def resolve(
    session: AsyncSession,
    location_id: str,
    decision_type: str | None = None,
    request: dict[str, dict[str, int]] | None = None,
) -> Resolved | None:
    row = (
        await session.execute(
            select(Agency.id, Agency.settings, Business.id, Business.settings, Location.settings)
            .select_from(Location)
            .join(Business, Location.business_id == Business.id)
            .join(Agency, Location.agency_id == Agency.id)
            .where(Location.id == location_id)
        )
    ).one_or_none()
    if row is None:
        return None
    agency_id, agency_settings, business_id, business_settings, location_settings = row
    layers: list[Layer] = [
        (f"agency:{agency_id}", agency_settings),
        (f"business:{business_id}", business_settings),
        (f"location:{location_id}", location_settings),
    ]
    policy = None
    if decision_type is not None:
        scopes = [("location", location_id), ("business", business_id), ("agency", agency_id)]
        policy = await nearest_policy(session, [*scopes, SYSTEM], decision_type)
        if policy is not None:
            overrides = policy.body.get("overrides", {})
            layers.append((f"policy:{policy.id}@{policy.version}", overrides))
    merged, origins = merge(layers)
    if request:
        tighten(merged, origins, request)
    return Resolved(ClientSettings.model_validate(merged), origins, policy)
