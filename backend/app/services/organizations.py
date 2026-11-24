# SPDX-License-Identifier: Apache-2.0

"""The primary organization and the organizations it tracks (#9157, slice M1).

Design: ``dev-docs/ctem-fleets.md`` section 9.2. The primary organization
holds the full profile contract of section 6
(``services/organization_context.OrganizationProfile``); a tracked
organization (a provider, a customer, a partner, a subsidiary) holds a
smaller one, :class:`TrackedOrganizationProfile`: its sector, its regions,
the services it supplies to or consumes from the primary, and the internal
owner of the relationship (a person of the platform, never the third
party's contact). Primary assets, risk appetite, rate and
ticketing stay primary-only: the loss is always the primary's.

Every tracked organization starts with ``monitoring`` ``off``; nothing in
this module provisions or meters anything.
"""

from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.organization import ORGANIZATION_CRITICALITIES, ORGANIZATION_RELATIONSHIPS, Organization
from app.models.organization_context import OrganizationContextVersion
from app.services import organization_context as org_context

#: The argument of ``get_organization_context`` that lists the tracked organizations.
EVERY_ORGANIZATION = "*"

_Strict = ConfigDict(extra="forbid", str_strip_whitespace=True)


class TrackedService(BaseModel):
    model_config = _Strict

    name: str = Field(min_length=1, max_length=200)
    #: ``supplies`` (to the primary) or ``consumes`` (from the primary).
    direction: Literal["supplies", "consumes"] = "supplies"
    #: Names of the primary's primary assets this service supports.
    primary_assets: list[str] = Field(default_factory=list, max_length=50)
    opencrq_asset_id: str = Field(default="", max_length=100)


class TrackedOrganizationProfile(BaseModel):
    """A tracked organization's profile as stored and served (every field optional)."""

    model_config = _Strict

    sector: str = Field(default="", max_length=100)
    regions: list[str] = Field(default_factory=list, max_length=250)
    services: list[TrackedService] = Field(default_factory=list, max_length=100)
    #: The internal owner of the relationship: a user of the platform.
    contacts_owner: str = Field(default="", max_length=200)
    #: Filled by M3 (the OpenCTI PIR that covers this organization).
    pir_ids: list[str] = Field(default_factory=list, max_length=20)

    @field_validator("regions")
    @classmethod
    def _iso_regions(cls, value: list[str]) -> list[str]:
        return org_context.normalize_regions(value)

    @field_validator("pir_ids")
    @classmethod
    def _pir_ids(cls, value: list[str]) -> list[str]:
        return [pir for pir in dict.fromkeys(str(v).strip() for v in value) if pir]


#: The tracked contract, in the order a report names a missing field.
TRACKED_CONTRACT_FIELDS: tuple[tuple[str, str], ...] = (
    ("sector", "sector"),
    ("regions", "regions of operation"),
    ("services", "services supplied or consumed"),
    ("contacts_owner", "internal owner of the relationship"),
)


def parse_tracked_profile(raw: Any) -> TrackedOrganizationProfile:
    """A stored or submitted tracked profile, validated (``ValueError`` naming the bad field)."""
    try:
        return TrackedOrganizationProfile.model_validate(raw or {})
    except ValidationError as exc:
        first = exc.errors()[0]
        where = ".".join(str(part) for part in first.get("loc", ())) or "profile"
        raise ValueError(f"{where}: {first.get('msg', 'invalid value')}") from None


def tracked_missing_fields(profile: TrackedOrganizationProfile) -> list[str]:
    """The tracked contract fields *profile* leaves empty, in contract order."""
    return [path for path, _label in TRACKED_CONTRACT_FIELDS if getattr(profile, path) in ("", [])]


def parse_for(organization: Organization, raw: Any) -> BaseModel:
    """*raw* validated against the contract of *organization*'s role."""
    if organization.role == "primary":
        return org_context.parse_profile(raw)
    return parse_tracked_profile(raw)


def missing_for(organization: Organization, profile: BaseModel) -> list[str]:
    if organization.role == "primary":
        return org_context.missing_fields(profile)  # type: ignore[arg-type]
    return tracked_missing_fields(profile)  # type: ignore[arg-type]


async def get_primary(db: AsyncSession) -> Organization | None:
    return (await db.execute(select(Organization).where(Organization.role == "primary"))).scalar_one_or_none()


async def ensure_primary(db: AsyncSession, *, name: str = "") -> Organization:
    """The primary organization, created on first use (a fresh install has none)."""
    primary = await get_primary(db)
    if primary is not None:
        return primary
    primary = Organization(name=(name or "").strip()[:200] or "Primary", role="primary", criticality="critical")
    db.add(primary)
    await db.flush()
    return primary


async def list_organizations(db: AsyncSession, *, include_archived: bool = False) -> list[Organization]:
    """The primary first, then the tracked ones by name."""
    query = select(Organization)
    if not include_archived:
        query = query.where(Organization.archived_at.is_(None))
    rows = list((await db.execute(query)).scalars().all())
    return sorted(rows, key=lambda o: (o.role != "primary", o.name.lower()))


async def get_organization(db: AsyncSession, organization_id: uuid.UUID) -> Organization | None:
    return await db.get(Organization, organization_id)


async def resolve(db: AsyncSession, ref: str) -> Organization | None:
    """An active organization by id or by name (case-insensitive); ``None`` when none or several."""
    ref = (ref or "").strip()
    if not ref:
        return await get_primary(db)
    try:
        found = await db.get(Organization, uuid.UUID(ref))
        if found is not None and found.archived_at is None:
            return found
    except ValueError:
        pass
    matches = [o for o in await list_organizations(db) if o.name.lower() == ref.lower()]
    return matches[0] if len(matches) == 1 else None


def check_relationship(relationship: str) -> str:
    if relationship not in ORGANIZATION_RELATIONSHIPS:
        raise ValueError(f"relationship must be one of {', '.join(ORGANIZATION_RELATIONSHIPS)}")
    return relationship


def check_criticality(criticality: str) -> str:
    if criticality not in ORGANIZATION_CRITICALITIES:
        raise ValueError(f"criticality must be one of {', '.join(ORGANIZATION_CRITICALITIES)}")
    return criticality


async def create_tracked(
    db: AsyncSession,
    *,
    name: str,
    relationship: str,
    criticality: str = "medium",
    opencti_organization_id: str | None = None,
) -> Organization:
    """A new tracked organization, monitoring ``off`` (the caller commits)."""
    name = (name or "").strip()
    if not name:
        raise ValueError("name is required")
    organization = Organization(
        name=name[:200],
        role="tracked",
        relationship=check_relationship(relationship),
        criticality=check_criticality(criticality),
        opencti_organization_id=(opencti_organization_id or None),
        monitoring="off",
    )
    db.add(organization)
    await db.flush()
    return organization


def archive(organization: Organization) -> None:
    """Archive a tracked organization: kept with its versions, out of every list."""
    if organization.role == "primary":
        raise ValueError("the primary organization cannot be archived")
    if organization.archived_at is None:
        organization.archived_at = datetime.now(UTC)


def render_tracked_list(organizations: list[Organization]) -> str:
    """What ``get_organization_context(organization="*")`` answers."""
    tracked = [o for o in organizations if o.role == "tracked"]
    if not tracked:
        return "No tracked organization. The primary organization is read without the organization argument."
    lines = ["Tracked organizations (read one by passing its name or id as the organization argument):"]
    for o in tracked:
        lines.append(
            f"- {o.name} (id {o.id}): {o.relationship}, criticality {o.criticality}, monitoring {o.monitoring}"
        )
    return "\n".join(lines)


def render_tracked_for_agent(organization: Organization, row: OrganizationContextVersion | None) -> str:
    """A tracked organization's profile as an agent reads it: filled fields quarantined, missing ones named."""
    from app.services.content_safety import wrap_untrusted

    header = (
        f"Tracked organization {organization.name}: {organization.relationship} of the primary "
        f"organization, criticality {organization.criticality}, monitoring {organization.monitoring}."
    )
    labels = dict(TRACKED_CONTRACT_FIELDS)
    if row is None:
        return (
            f"{header} No profile is saved yet. Missing: "
            + ", ".join(labels.values())
            + ". Do not guess them. The loss of any exposure is the primary organization's: read its profile too."
        )
    profile = parse_tracked_profile(row.profile)
    missing = tracked_missing_fields(profile)
    filled = {k: v for k, v in profile.model_dump(mode="json").items() if v not in ("", [], None)}
    return "\n".join(
        [
            header,
            f"Profile version {row.version} (saved {row.created_at:%Y-%m-%d %H:%M} UTC, source {row.source}). "
            "Fields not filled: "
            + (", ".join(labels[p] for p in missing) if missing else "none")
            + ". The loss of any exposure is the primary organization's: read its profile too.",
            "",
            wrap_untrusted(json.dumps(filled, ensure_ascii=False, indent=1), "tracked organization profile"),
        ]
    )
