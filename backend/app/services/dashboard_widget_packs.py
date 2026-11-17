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

"""Dashboard widget packs - the library of ready-made widgets, and inserting them.

A pack (``models/dashboard_widget_pack.py``) is read, imported, saved from a
dashboard, shared and deleted here, by the REST routes and the agent tools
alike:

* **Access** is the library rule: three-tier visibility (own, shared,
  company), ``can_manage_as`` to change one - never a built-in, which the
  startup seed owns (it is duplicated instead).
* **Import** reads a file through ``dashboard_interop.parse_any`` (an XTM One
  pack, an OpenCTI dashboard or widget export, an OpenAEV dashboard export),
  so every widget kept passed the dashboard validator and the report says
  what was changed or left out. An import is never company-managed.
* **Insert** copies chosen widgets into a dashboard the person may edit: each
  platform widget is bound to an integration of its platform the person can
  use - the one they name, else the only one they have - and the dashboard
  write is the ordinary one (``edit_dashboard``: validation, the integration
  rule, a version, a revision check).
* **Built-ins** ship as files (``app/data/dashboard_widget_packs/<slug>.json``)
  and are written on startup (:func:`seed_builtin_packs`): company-managed,
  tagged ``builtin`` and ``slug:<slug>``; one no longer shipped is removed.
"""

from __future__ import annotations

import copy
import json
import logging
import uuid
from pathlib import Path
from typing import Literal

from sqlalchemy import case, cast, select
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import defer

from app.core.json_sql import when_json_parseable
from app.models.custom_dashboard import CustomDashboard
from app.models.dashboard_widget_pack import DashboardWidgetPack
from app.services.dashboard_definition import summary_widgets

logger = logging.getLogger(__name__)

BUILTIN_TAG = "builtin"
#: A pack's name, at most: the column's length, on every write (create, import, duplicate, rename).
MAX_PACK_NAME: int = DashboardWidgetPack.__table__.c.name.type.length
BUILTIN_DIR = Path(__file__).resolve().parent.parent / "data" / "dashboard_widget_packs"
MAX_PACK_DESCRIPTION = 2000
MAX_TAGS = 10
MAX_TAG_CHARS = 40

PackScope = Literal["all", "mine", "shared", "company", "builtin"]


class PackError(ValueError):
    """A pack operation refused, with a reason the person or the agent can act on."""


def is_builtin(pack: DashboardWidgetPack) -> bool:
    return BUILTIN_TAG in (pack.tags or [])


def pack_sources(widgets: list[dict]) -> list[str]:
    return sorted({str(w.get("source")) for w in widgets if w.get("kind") == "platform" and w.get("source")})


def serialize_pack(pack: DashboardWidgetPack, *, can_manage: bool, include_widgets: bool = False) -> dict:
    payload = {
        "id": str(pack.id),
        "name": pack.name,
        "description": pack.description,
        "tags": [t for t in pack.tags or [] if not str(t).startswith("slug:")],
        "origin": pack.origin,
        "sources": list(pack.sources or []),
        "widget_count": pack.widget_count,
        "inputs": list(pack.inputs or []),
        "builtin": is_builtin(pack),
        "is_company_managed": bool(pack.is_company_managed),
        "user_id": str(pack.user_id) if pack.user_id else None,
        "can_manage": can_manage,
        "notes": list((pack.source_meta or {}).get("notes") or [])[:50],
        "created_at": pack.created_at.isoformat() if pack.created_at else None,
        "updated_at": pack.updated_at.isoformat() if pack.updated_at else None,
    }
    if include_widgets:
        payload["widgets"] = pack.widgets or []
    return payload


async def load_pack_for(
    db: AsyncSession,
    pack_id: uuid.UUID,
    *,
    user_id: uuid.UUID,
    is_admin: bool = False,
    need: Literal["read", "manage"] = "read",
) -> tuple[DashboardWidgetPack, bool] | None:
    """The pack and whether *user_id* may change it, when they may *need* it; else ``None``."""
    from app.api.v1.helpers import can_manage_as, get_user_group_ids, user_visible_filter

    group_ids = await get_user_group_ids(db, user_id)
    pack = (
        await db.execute(
            select(DashboardWidgetPack).where(
                DashboardWidgetPack.id == pack_id,
                user_visible_filter(DashboardWidgetPack, user_id, group_ids, is_admin=is_admin),
            )
        )
    ).scalar_one_or_none()
    if pack is None:
        return None
    manage = not is_builtin(pack) and can_manage_as(user_id, is_admin, pack, group_ids)
    if need == "manage" and not manage:
        return None
    return pack, manage


async def list_visible_packs(
    db: AsyncSession,
    *,
    user_id: uuid.UUID,
    is_admin: bool = False,
    scope: PackScope = "all",
    source: str | None = None,
    limit: int = 100,
) -> list[tuple[DashboardWidgetPack, bool]]:
    """The packs *user_id* may use, built-ins first, then the most recent (three-tier for admins too)."""
    from app.api.v1.helpers import can_manage_as, get_user_group_ids, user_visible_filter

    group_ids = await get_user_group_ids(db, user_id)
    query = select(DashboardWidgetPack).where(
        user_visible_filter(DashboardWidgetPack, user_id, group_ids, is_admin=False)
    )
    if scope == "mine":
        query = query.where(DashboardWidgetPack.user_id == user_id, DashboardWidgetPack.is_company_managed.is_(False))
    elif scope == "company":
        query = query.where(DashboardWidgetPack.is_company_managed.is_(True))
    elif scope == "shared":
        query = query.where(
            DashboardWidgetPack.is_company_managed.is_(False),
            (DashboardWidgetPack.user_id != user_id) | DashboardWidgetPack.user_id.is_(None),
        )
    builtin = _json_contains(DashboardWidgetPack.tags, BUILTIN_TAG)
    if scope == "builtin":
        query = query.where(builtin)
    if source:
        query = query.where(_json_contains(DashboardWidgetPack.sources, source))
    # A list never shows the widgets: their JSON stays in the database.
    query = (
        query.options(defer(DashboardWidgetPack.widgets, raiseload=True))
        .order_by(case((builtin, 0), else_=1), DashboardWidgetPack.updated_at.desc())
        .limit(limit)
    )
    rows = (await db.execute(query)).scalars().all()
    return [(r, not is_builtin(r) and can_manage_as(user_id, is_admin, r, group_ids)) for r in rows]


def _json_contains(column, value: str):
    """``column @> [value]`` on a JSON list column, false on a row PostgreSQL cannot parse."""
    return when_json_parseable(column, cast(column, JSONB).contains([value]))


def _clean_text(value: object, limit: int) -> str | None:
    if not isinstance(value, str):
        return None
    text = value.strip()
    return text[:limit] or None


async def create_pack(
    db: AsyncSession,
    *,
    user_id: uuid.UUID,
    name: str,
    description: str | None,
    widgets: list[dict],
    origin: str,
    source_meta: dict | None = None,
    inputs: list[dict] | None = None,
) -> DashboardWidgetPack:
    """A new private pack of *user_id* holding *widgets* and the entity *inputs* they read (already checked, unbound)."""
    from app.services.dashboard_interop import MAX_PACK_WIDGETS
    from app.services.dashboard_interop.pack_format import build_pack

    if not widgets:
        raise PackError("A widget pack holds at least one widget.")
    document = build_pack(name, description, widgets[:MAX_PACK_WIDGETS], inputs)
    unbound = document["widgets"]
    pack = DashboardWidgetPack(
        id=uuid.uuid4(),
        user_id=user_id,
        name=(_clean_text(name, MAX_PACK_NAME) or "Widget pack"),
        description=_clean_text(description, MAX_PACK_DESCRIPTION),
        tags=[],
        origin=origin,
        sources=pack_sources(unbound),
        widgets=unbound,
        widget_count=len(unbound),
        inputs=document.get("inputs") or [],
        source_meta=source_meta,
    )
    db.add(pack)
    await db.flush()
    return pack


_ORIGINS = {
    "xtm-one-pack": "upload",
    "opencti-dashboard": "opencti",
    "opencti-custom-view": "opencti",
    "opencti-widget": "opencti",
    "openaev-dashboard": "openaev",
}


async def import_pack(
    db: AsyncSession, *, user_id: uuid.UUID, data: bytes, filename: str = "", name: str | None = None
) -> DashboardWidgetPack:
    """A pack from an uploaded file; :class:`PackError` (the parser's words) when it is not one."""
    from app.services.dashboard_interop import InteropError, parse_any

    try:
        parsed = parse_any(data, filename=filename)
    except InteropError as exc:
        raise PackError(str(exc)) from None
    meta = {"notes": parsed.report.notes[:50], "kind": parsed.origin}
    if parsed.platform_version:
        meta["platform_version"] = parsed.platform_version
    return await create_pack(
        db,
        user_id=user_id,
        name=name or parsed.name,
        description=parsed.description,
        widgets=parsed.widgets,
        origin=_ORIGINS.get(parsed.origin, "upload"),
        source_meta=meta,
        inputs=parsed.inputs,
    )


async def pack_from_dashboard(
    db: AsyncSession,
    *,
    user_id: uuid.UUID,
    dashboard: CustomDashboard,
    name: str,
    description: str | None = None,
    widget_ids: list[str] | None = None,
) -> DashboardWidgetPack:
    """A pack of a dashboard's widgets (all, or *widget_ids*): platform widgets lose their integration, so the
    OpenCTI saved filters they name are read with *user_id*'s access and written in place (an unreadable one keeps its id)."""
    from app.services.dashboard_publish import inline_saved_filters

    widgets = [w for w in summary_widgets(dashboard.spec) if isinstance(w, dict)]
    if widget_ids:
        wanted = set(widget_ids)
        widgets = [w for w in widgets if w.get("id") in wanted]
    if not widgets:
        raise PackError("None of the named widgets is on the dashboard.")
    widgets, _notes = await inline_saved_filters(db, user_id, widgets)
    return await create_pack(
        db,
        user_id=user_id,
        name=name,
        description=description,
        widgets=widgets,
        origin="dashboard",
        source_meta={"dashboard_id": str(dashboard.id), "revision": dashboard.revision},
        inputs=entity_inputs_read(dashboard.spec, widgets),
    )


def _tokens_read(widgets: list[dict]) -> set[str]:
    from app.services.dashboard_tokens import names_in

    return names_in([w.get("native") for w in widgets if w.get("kind") == "platform"])


def entity_inputs_read(spec: object, widgets: list[dict]) -> list[dict]:
    """The dashboard's entity inputs whose token *widgets* read, each with its id, as a pack keeps them."""
    from app.services.dashboard_definition import upgrade
    from app.services.dashboard_interop.pack_format import PACK_INPUT_TYPES

    used = _tokens_read(widgets)
    definition = upgrade(spec) if isinstance(spec, dict) else {}
    return [
        {**entry, "id": iid}
        for iid, entry in (definition.get("inputs") or {}).items()
        if isinstance(entry, dict) and entry.get("type") in PACK_INPUT_TYPES and entry.get("token") in used
    ]


async def resolve_bindings(
    db: AsyncSession, user_id: uuid.UUID, sources: list[str], requested: dict[str, str] | None
) -> dict[str, str]:
    """The integration each platform reads through: the one named, else the only one the person can use."""
    from app.services.platform_access import visible_platform_integrations

    usable = await visible_platform_integrations(db, user_id)
    by_source: dict[str, list] = {}
    for integration in usable:
        by_source.setdefault(integration.provider, []).append(integration)
    bindings: dict[str, str] = {}
    for source in sources:
        choices = by_source.get(source, [])
        named = (requested or {}).get(source)
        if named:
            if not any(str(i.id) == str(named) for i in choices):
                raise PackError(f"The {source} integration named is not one you can use.")
            bindings[source] = str(named)
        elif len(choices) == 1:
            bindings[source] = str(choices[0].id)
        elif not choices:
            raise PackError(f"These widgets read {source}, and you can use no {source} integration.")
        else:
            listing = ", ".join(f"{i.name} ({i.id})" for i in choices[:10])
            raise PackError(f"Choose the {source} integration these widgets read: {listing}.")
    return bindings


async def insert_pack_widgets(
    db: AsyncSession,
    *,
    user_id: uuid.UUID,
    dashboard: CustomDashboard,
    pack: DashboardWidgetPack,
    widget_ids: list[str] | None = None,
    bindings: dict[str, str] | None = None,
    after: str | None = None,
    expected_revision: int | None = None,
    source: str = "manual",
) -> list[str]:
    """Copy the pack's widgets (all, or *widget_ids*) into *dashboard*; the ids they got (:func:`insert_widgets`)."""
    return await insert_widgets(
        db,
        user_id=user_id,
        dashboard=dashboard,
        widgets=list(pack.widgets or []),
        inputs=list(pack.inputs or []),
        widget_ids=widget_ids,
        bindings=bindings,
        expected_revision=expected_revision,
        source=source,
    )


async def import_dashboard(
    db: AsyncSession,
    *,
    user_id: uuid.UUID,
    data: bytes,
    filename: str = "",
    title: str | None = None,
    bindings: dict[str, str] | None = None,
) -> tuple[CustomDashboard, list[str]]:
    """A new dashboard of *user_id*'s from an uploaded file (what :func:`import_pack` reads), its widgets bound
    to the integrations the person can use; and the notes of what the conversion changed or left out.
    :class:`PackError` (the parser's or the binding's words) when it cannot be made, before anything is kept."""
    from app.services.custom_dashboard_spec import MAX_TITLE_CHARS
    from app.services.custom_dashboards import empty_definition, save_dashboard
    from app.services.dashboard_interop import InteropError, parse_any

    try:
        parsed = parse_any(data, filename=filename)
    except InteropError as exc:
        raise PackError(str(exc)) from None
    name = (_clean_text(title, MAX_TITLE_CHARS) or _clean_text(parsed.name, MAX_TITLE_CHARS) or "Imported dashboard")
    dashboard = await save_dashboard(db, user_id=user_id, spec_payload=empty_definition(name), source="manual", summary="imported a file")
    if parsed.widgets:
        await insert_widgets(
            db, user_id=user_id, dashboard=dashboard, widgets=parsed.widgets, inputs=parsed.inputs, bindings=bindings,
            expected_revision=dashboard.revision, source="manual",
        )  # fmt: skip
    return dashboard, parsed.report.notes[:50]


async def insert_widgets(
    db: AsyncSession,
    *,
    user_id: uuid.UUID,
    dashboard: CustomDashboard,
    widgets: list[dict],
    inputs: list[dict],
    widget_ids: list[str] | None = None,
    bindings: dict[str, str] | None = None,
    expected_revision: int | None = None,
    source: str = "manual",
) -> list[str]:
    """Copy *widgets* (all, or *widget_ids*) into *dashboard*, with the *inputs* they read; the ids they got.

    The widgets keep their arrangement and go below the dashboard's widgets
    (*after* is accepted from older callers and no longer places them). The
    entity inputs they read come too, bound like them and drawn above the tabs,
    unless the dashboard has an input setting that token already. The write is
    ``edit_dashboard``'s: validated, a platform widget or an entity input only
    on an integration the person can use, kept as a version, revision-checked.
    """
    from app.services.custom_dashboards import edit_dashboard
    from app.services.dashboard_grid import bottom, fill_layouts
    from app.services.dashboard_interop.pack_format import bind_widget

    chosen = [w for w in widgets if isinstance(w, dict)]
    if widget_ids:
        wanted = set(widget_ids)
        chosen = [w for w in chosen if w.get("id") in wanted]
    if not chosen:
        raise PackError("None of the named widgets is in the pack.")
    used = _tokens_read(chosen)
    inputs = [i for i in inputs if isinstance(i, dict) and i.get("token") in used]
    input_sources = {str((i.get("options") or {}).get("source")) for i in inputs}
    resolved = await resolve_bindings(db, user_id, sorted(set(pack_sources(chosen)) | input_sources), bindings)
    current_widgets = summary_widgets(dashboard.spec)
    taken = {w.get("id") for w in current_widgets}
    chosen = fill_layouts(copy.deepcopy(chosen))
    shift = bottom(current_widgets) - min(w["layout"]["y"] for w in chosen)
    for widget in chosen:
        widget["layout"]["y"] += shift
    operations: list[dict] = _input_operations(dashboard.spec, inputs, resolved)
    inserted: list[str] = []
    for widget in chosen:
        bound = bind_widget(widget, resolved.get(str(widget.get("source"))) if widget.get("kind") == "platform" else None)
        wid = str(bound.get("id") or "widget")
        candidate, n = wid, 2
        while candidate in taken:
            suffix = f"-{n}"
            candidate, n = wid[: 40 - len(suffix)] + suffix, n + 1
        bound["id"] = candidate
        taken.add(candidate)
        operations.append({"op": "upsert", "widget": bound})
        inserted.append(candidate)
    await edit_dashboard(
        db,
        dashboard,
        operations=operations,
        expected_revision=expected_revision,
        acting_user_id=user_id,
        source=source,
    )
    return inserted


def _input_operations(spec: object, inputs: list[dict], resolved: dict[str, str]) -> list[dict]:
    """The steps adding *inputs* to a dashboard: each bound, under an id of its own, drawn above the tabs;
    an input whose token the dashboard already sets is not added (its own input sets it)."""
    from app.services.dashboard_definition import upgrade

    if not inputs:
        return []
    definition = upgrade(spec) if isinstance(spec, dict) else {}
    existing = definition.get("inputs") or {}
    tokens = {entry.get("token") for entry in existing.values() if isinstance(entry, dict)}
    layout = copy.deepcopy(definition.get("layout") or {})
    shown = list(layout.get("globalInputs") or [])
    operations: list[dict] = []
    for entry in inputs:
        if entry.get("token") in tokens:
            continue
        bound = copy.deepcopy({k: v for k, v in entry.items() if k != "id"})
        bound["options"] = {**(bound.get("options") or {}), "integration_id": resolved[str(bound["options"]["source"])]}
        base = str(entry.get("id") or f"input_{entry.get('token')}")[:50]
        iid, n = base, 2
        while iid in existing:
            iid, n = f"{base}_{n}", n + 1
        existing = {**existing, iid: bound}
        tokens.add(entry.get("token"))
        operations.append({"op": "set", "path": f"inputs.{iid}", "value": bound})
        shown.append(iid)
    if operations and layout:
        operations.append({"op": "set", "path": "layout", "value": {**layout, "globalInputs": shown}})
    return operations


# ── Built-ins ────────────────────────────────────────────────────────────────


def _builtin_files() -> dict[str, dict]:
    files: dict[str, dict] = {}
    for path in sorted(BUILTIN_DIR.glob("*.json")):
        try:
            files[path.stem] = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            logger.warning("Built-in widget pack %s unreadable: %r", path.name, str(exc)[:200])
    return files


async def seed_builtin_packs(db: AsyncSession) -> int:
    """Write the shipped packs (company-managed, read-only), drop the ones no longer shipped; how many written."""
    from app.services.dashboard_interop import InteropError
    from app.services.dashboard_interop.pack_format import parse_pack_document

    shipped = _builtin_files()
    existing = {
        next((t[5:] for t in p.tags or [] if str(t).startswith("slug:")), None): p
        for p in (await db.execute(select(DashboardWidgetPack))).scalars().all()
        if is_builtin(p)
    }
    written = 0
    for slug, document in shipped.items():
        try:
            parsed = parse_pack_document(document)
        except InteropError as exc:
            logger.warning("Built-in widget pack %s refused: %s", slug, exc)
            continue
        row = existing.pop(slug, None)
        values = {
            "name": parsed.name,
            "description": parsed.description,
            "widgets": parsed.widgets,
            "widget_count": len(parsed.widgets),
            "sources": pack_sources(parsed.widgets),
        }
        if row is None:
            db.add(
                DashboardWidgetPack(
                    id=uuid.uuid4(), user_id=None, is_company_managed=True, tags=[BUILTIN_TAG, f"slug:{slug}"],
                    origin="builtin", source_meta=None, **values,
                )
            )  # fmt: skip
            written += 1
        elif any(getattr(row, key) != value for key, value in values.items()):
            for key, value in values.items():
                setattr(row, key, value)
            written += 1
    for row in existing.values():
        await db.delete(row)
    await db.commit()
    return written


__all__ = [
    "PackError",
    "create_pack",
    "import_pack",
    "insert_pack_widgets",
    "list_visible_packs",
    "load_pack_for",
    "pack_from_dashboard",
    "seed_builtin_packs",
    "serialize_pack",
]
