# Copyright (c) 2025-2026 Filigran SAS
#
# This file is part of the XTM One Enterprise Edition ("EE") and is
# licensed under the XTM One Enterprise Edition License (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     https://github.com/XTM-One-Platform/xtm-one/blob/main/LICENSE
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.

"""Presets of fleet member settings (dev-docs/ctem-fleets.md section 10.6, R4 of #9162).

A preset is a named set of values for any of the three member fields
(:data:`app.services.fleets.MEMBER_SETTING_FIELDS`). Decision R-D3 A:
company-wide presets managed by administrators, plus the three built-ins
below; no personal preset.

* The built-ins live here, in code, with stable ids (``builtin:<key>``): they
  are never stored, never edited and never deleted.
* A stored preset (``fleet_member_presets``) is validated with the same rules
  as a fleet default or a bulk change (``check_member_setting``), so a preset
  can never hold a value the bulk endpoint would refuse.
* Applying a preset is the existing write it stands for: ``PATCH
  /fleets/{id}`` with ``member_defaults`` (the defaults bar) or ``POST
  /fleets/{id}/members/bulk`` with ``set`` (the selected rows). No link is
  kept, so editing a preset changes no fleet. A ``list`` organization scope is
  checked against the active organizations when it is applied, as any scope.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.fleet import FleetMemberPreset
from app.services.fleets import (
    MEMBER_SETTING_FIELDS,
    FleetActionRefusedError,
    check_member_setting,
    check_scope_organizations,
)

#: The most stored presets a company keeps.
MAX_PRESETS = 100

PRESET_NAME_MAX_LENGTH = 120

BUILTIN_ID_PREFIX = "builtin:"


@dataclass(frozen=True)
class PresetView:
    """A preset as every surface reads it, built-in or stored."""

    id: str
    name: str
    values: dict[str, Any]
    builtin: bool


#: The three built-ins of section 10.6, in the picker's order.
BUILTIN_PRESETS: tuple[PresetView, ...] = (
    PresetView(
        id=f"{BUILTIN_ID_PREFIX}own-summary",
        name="Own organization, a summary",
        values={"context_inclusion": "summary", "organization_scope": {"mode": "primary"}},
        builtin=True,
    ),
    PresetView(
        id=f"{BUILTIN_ID_PREFIX}monitored-nothing",
        name="All monitored organizations, nothing",
        values={"context_inclusion": "none", "organization_scope": {"mode": "monitored"}},
        builtin=True,
    ),
    PresetView(
        id=f"{BUILTIN_ID_PREFIX}own-full",
        name="Own organization, the full conversation",
        values={"context_inclusion": "full", "organization_scope": {"mode": "primary"}},
        builtin=True,
    ),
)


def normalize_preset_values(values: object) -> dict[str, Any]:
    """Validate a preset's values and answer them in canonical shape.

    At least one of the member fields, nothing else; each value checked with
    the rule a fleet default follows. ``None`` (reset) is not a preset value.
    """
    if not isinstance(values, dict) or not values:
        raise FleetActionRefusedError(422, f"values names at least one of {', '.join(MEMBER_SETTING_FIELDS)}.")
    unknown = set(values) - set(MEMBER_SETTING_FIELDS)
    if unknown:
        raise FleetActionRefusedError(422, f"values takes {', '.join(MEMBER_SETTING_FIELDS)}.")
    out: dict[str, Any] = {}
    for name in MEMBER_SETTING_FIELDS:
        if name not in values:
            continue
        if values[name] is None:
            raise FleetActionRefusedError(422, f"values.{name} is a value; a preset does not reset a field.")
        out[name] = check_member_setting(name, values[name])
    return out


def _normalize_name(name: object) -> str:
    text = " ".join(str(name or "").split())
    if not text:
        raise FleetActionRefusedError(422, "name is required.")
    if len(text) > PRESET_NAME_MAX_LENGTH:
        raise FleetActionRefusedError(422, f"name is at most {PRESET_NAME_MAX_LENGTH} characters.")
    if text.casefold() in {p.name.casefold() for p in BUILTIN_PRESETS}:
        raise FleetActionRefusedError(409, f"'{text}' is the name of a built-in preset.")
    return text


def preset_view(row: FleetMemberPreset) -> PresetView:
    return PresetView(id=str(row.id), name=row.name, values=dict(row.values or {}), builtin=False)


async def list_presets(db: AsyncSession) -> list[PresetView]:
    """The built-ins first, then the stored presets by name."""
    rows = (await db.execute(select(FleetMemberPreset).order_by(func.lower(FleetMemberPreset.name)))).scalars().all()
    return [*BUILTIN_PRESETS, *(preset_view(r) for r in rows)]


async def _name_taken(db: AsyncSession, name: str, *, except_id: uuid.UUID | None = None) -> bool:
    query = select(FleetMemberPreset.id).where(func.lower(FleetMemberPreset.name) == name.casefold())
    if except_id is not None:
        query = query.where(FleetMemberPreset.id != except_id)
    return (await db.execute(query.limit(1))).scalar() is not None


async def create_preset(db: AsyncSession, *, name: object, values: object, user_id: uuid.UUID) -> FleetMemberPreset:
    """Store a company preset; 409 on a name taken, 422 on a value refused. The caller commits."""
    clean_name = _normalize_name(name)
    clean_values = normalize_preset_values(values)
    await check_scope_organizations(db, clean_values.get("organization_scope"))
    if await _name_taken(db, clean_name):
        raise FleetActionRefusedError(409, f"A preset named '{clean_name}' exists.")
    count = (await db.execute(select(func.count()).select_from(FleetMemberPreset))).scalar_one()
    if count >= MAX_PRESETS:
        raise FleetActionRefusedError(422, f"At most {MAX_PRESETS} presets; delete one first.")
    row = FleetMemberPreset(name=clean_name, values=clean_values, created_by_user_id=user_id)
    db.add(row)
    await db.flush()
    return row


async def _stored(db: AsyncSession, preset_id: str) -> FleetMemberPreset:
    if preset_id.startswith(BUILTIN_ID_PREFIX):
        raise FleetActionRefusedError(403, "A built-in preset cannot be changed or deleted.")
    try:
        key = uuid.UUID(preset_id)
    except ValueError:
        raise FleetActionRefusedError(404, "Preset not found.") from None
    row = await db.get(FleetMemberPreset, key)
    if row is None:
        raise FleetActionRefusedError(404, "Preset not found.")
    return row


async def update_preset(
    db: AsyncSession, preset_id: str, *, name: object = None, values: object = None
) -> FleetMemberPreset:
    """Rename a stored preset or replace its values; a built-in is refused (403). The caller commits."""
    row = await _stored(db, preset_id)
    if name is not None:
        clean_name = _normalize_name(name)
        if await _name_taken(db, clean_name, except_id=row.id):
            raise FleetActionRefusedError(409, f"A preset named '{clean_name}' exists.")
        row.name = clean_name
    if values is not None:
        clean_values = normalize_preset_values(values)
        await check_scope_organizations(db, clean_values.get("organization_scope"))
        row.values = clean_values
    await db.flush()
    return row


async def delete_preset(db: AsyncSession, preset_id: str) -> str:
    """Delete a stored preset and answer its name; no fleet changes. The caller commits."""
    row = await _stored(db, preset_id)
    name = row.name
    await db.delete(row)
    await db.flush()
    return name
