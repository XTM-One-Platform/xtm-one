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

"""Custom dashboard persistence, access and history.

The one place that turns a validated ``DashboardSpec`` into a
``CustomDashboard`` row and back into the JSON the frontend renderer consumes.
Shared by the agent tools (``builtin_tools/custom_dashboard.py``) and the REST
routes (``api/v1/custom_dashboards.py``), so the two cannot drift on access,
revisioning or validation.

Access is one rule, :func:`dashboard_permission`, applied by
:func:`load_dashboard_for` on every surface (REST, agent tools, the studio,
the home dashboard): the owner; a ``config`` grantee edits, a ``read_only``
grantee reads; a company-managed dashboard is read by everyone and edited by
admins; an administrator reads anyone else's dashboard through the REST routes
(``is_admin``) but never edits it, and an agent never acts beyond its user's
own tier. Every write records a :class:`CustomDashboardVersion`.
"""

from __future__ import annotations

import json
import logging
import uuid
from datetime import UTC, datetime
from typing import Literal

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.custom_dashboard import CustomDashboard, CustomDashboardVersion
from app.services.custom_dashboard_spec import DashboardSpecError
from app.services.dashboard_definition import (
    apply_definition_operations,
    from_widgets,
    is_definition,
    platform_sources,
    upgrade,
    validate_definition,
    visualization_count,
    widget_view,
)
from app.services.dashboard_sources import check_source_references

logger = logging.getLogger(__name__)

#: The marker a tool answer carries so the web chat draws the dashboard in
#: place (``channels.base._DASHBOARD_MARKER_RE`` reads it back).
DASHBOARD_MARKER = "[[DASHBOARD:{id}]]"

#: A person's tier on a dashboard. ``owner`` and ``config`` edit; ``read_only`` reads.
DashboardTier = Literal["owner", "manage", "config", "read_only"]
#: Who edits a dashboard - rearranges, refines and shares it.
EDIT_TIERS: frozenset[str] = frozenset({"owner", "manage", "config"})
#: Who deletes it: its owner, and a ``manage`` grantee (OpenCTI's "admin" access to a dashboard).
DELETE_TIERS: frozenset[str] = frozenset({"owner", "manage"})

#: Where a version came from.
VERSION_SOURCES: tuple[str, ...] = ("agent", "manual", "restore", "import")
#: Versions kept per dashboard; older ones are pruned on every write.
DASHBOARD_VERSION_RETENTION = 30
#: Layout-only edits by one person this close together are one version: a drag
#: session must not push the kept versions out.
LAYOUT_VERSION_WINDOW_SECONDS = 60
REARRANGED = "rearranged widgets"


def append_markers(text: str, dashboard_ids: list[str]) -> str:
    """Append the card marker of every dashboard a turn built or edited that its answer lacks.

    The card never depends on the model copying the marker the tool returned.
    """
    wanted = dict.fromkeys(DASHBOARD_MARKER.format(id=str(i).lower()) for i in dashboard_ids)
    missing = [m for m in wanted if m.lower() not in (text or "").lower()]
    if not missing:
        return text
    return "\n\n".join([(text or "").rstrip(), *missing]).strip()


class DashboardRevisionConflictError(DashboardSpecError):
    """An edit named a revision the dashboard has moved past."""

    def __init__(self, current: int) -> None:
        super().__init__(
            f"The dashboard changed since that revision (it is now at revision {current}). "
            "Read it again and apply the edit to the current widgets."
        )
        self.current = current


def serialize_dashboard(dashboard: CustomDashboard, *, include_spec: bool = True) -> dict:
    """The JSON-safe payload the renderer and the list views consume."""
    payload = {
        "id": str(dashboard.id),
        "title": dashboard.title,
        "widget_count": dashboard.widget_count,
        "revision": dashboard.revision,
        "conversation_id": str(dashboard.conversation_id) if dashboard.conversation_id else None,
        "assignment_run_id": str(dashboard.assignment_run_id) if dashboard.assignment_run_id else None,
        "created_at": dashboard.created_at.isoformat() if dashboard.created_at else None,
        "updated_at": dashboard.updated_at.isoformat() if dashboard.updated_at else None,
        "tags": [str(tag) for tag in dashboard.tags or []],
    }
    if include_spec:
        payload["spec"] = upgrade(dashboard.spec)
        payload["widgets"] = widget_view(payload["spec"])
        payload["manifest"] = dashboard_manifest(dashboard.spec, payload["spec"])
    payload["user_id"] = str(dashboard.user_id) if dashboard.user_id else None
    payload["is_company_managed"] = bool(dashboard.is_company_managed)
    payload["is_shared"] = bool(dashboard.shared_user_ids or dashboard.shared_group_ids)
    return payload


def dashboard_manifest(spec: object, working: dict | None = None) -> dict:
    """A stored dashboard in the native form (``dashboard_native``): as stored, or written from an older form.

    *working* is the definition already read from *spec*, when the caller has it.
    """
    from app.services.dashboard_native import is_native, to_native

    if is_native(spec):
        return spec  # type: ignore[return-value]
    return to_native(working if working is not None else upgrade(spec))


def sharing_fields(dashboard: CustomDashboard) -> dict:
    """Who it is shared with, for the people who may change that (the Share dialog)."""
    return {
        "shared_user_ids": list(dashboard.shared_user_ids or []),
        "shared_user_permissions": dict(dashboard.shared_user_permissions or {}),
        "shared_group_ids": list(dashboard.shared_group_ids or []),
        "shared_group_permissions": dict(dashboard.shared_group_permissions or {}),
        "access_restricted": bool(dashboard.access_restricted),
    }


def dashboard_permission(
    dashboard: CustomDashboard,
    *,
    user_id: uuid.UUID | str | None,
    user_group_ids: list[str] | None = None,
    is_admin: bool = False,
) -> DashboardTier | None:
    """The tier of *user_id* on *dashboard*, or ``None`` when they may not see it.

    * the owner -> ``owner``;
    * a company-managed dashboard -> ``owner`` for an admin, ``read_only`` for
      everyone the visibility filter admits;
    * an admin, on someone else's personal dashboard -> ``read_only``, and only
      when *is_admin* is passed (the REST routes; never an agent tool);
    * a ``config_with_chats`` grant (by user or group) -> ``manage``: on a dashboard the strongest grant also
      deletes it (OpenCTI's "admin" access); a ``config`` grant -> ``config``; any other grant -> ``read_only``.
    """
    from app.api.v1.helpers import (
        SHARE_PERMISSION_CONFIG_WITH_CHATS,
        SHARE_PERMISSIONS_CONFIG,
        get_effective_permission,
    )

    acting = str(user_id) if user_id is not None else None
    if acting is not None and dashboard.user_id is not None and str(dashboard.user_id) == acting:
        return "owner"
    if dashboard.is_company_managed:
        return "owner" if is_admin else "read_only"
    perm = get_effective_permission(dashboard, acting, user_group_ids) if acting is not None else None
    if perm == SHARE_PERMISSION_CONFIG_WITH_CHATS:
        return "manage"
    if perm is not None:
        return "config" if perm in SHARE_PERMISSIONS_CONFIG else "read_only"
    return "read_only" if is_admin else None


async def load_dashboard_for(
    db: AsyncSession,
    dashboard_id: uuid.UUID,
    *,
    user_id: uuid.UUID | None,
    is_admin: bool = False,
    need: Literal["read", "edit"] = "read",
    for_update: bool = False,
) -> tuple[CustomDashboard, DashboardTier] | None:
    """The dashboard and *user_id*'s tier, when they may *need* it; else ``None``.

    The visibility predicate runs in SQL (a dashboard one may not see does not
    exist for them) and the tier is computed from the row read, never from the
    request. ``for_update`` locks the row for a revision-checked write.
    """
    from app.api.v1.helpers import get_user_group_ids, user_visible_filter

    if user_id is None:
        return None
    group_ids = await get_user_group_ids(db, user_id)
    query = select(CustomDashboard).where(
        CustomDashboard.id == dashboard_id,
        user_visible_filter(CustomDashboard, user_id, group_ids, is_admin=is_admin),
    )
    if for_update:
        query = query.with_for_update(of=CustomDashboard)
    dashboard = (await db.execute(query)).scalar_one_or_none()
    if dashboard is None:
        return None
    tier = dashboard_permission(dashboard, user_id=user_id, user_group_ids=group_ids, is_admin=is_admin)
    if tier is None or (need == "edit" and tier not in EDIT_TIERS):
        return None
    return dashboard, tier


async def list_visible_dashboards(
    db: AsyncSession,
    *,
    user_id: uuid.UUID,
    scope: Literal["all", "mine", "shared", "company"] = "all",
    limit: int = 50,
) -> list[tuple[CustomDashboard, DashboardTier]]:
    """The dashboards *user_id* sees (own, shared with them, company), most recent first.

    Three-tier for admins too: a library list is what one may open, not
    everything an admin may inspect.
    """
    from app.api.v1.helpers import get_user_group_ids, user_visible_filter

    group_ids = await get_user_group_ids(db, user_id)
    query = select(CustomDashboard).where(user_visible_filter(CustomDashboard, user_id, group_ids, is_admin=False))
    if scope == "mine":
        query = query.where(CustomDashboard.user_id == user_id, CustomDashboard.is_company_managed.is_(False))
    elif scope == "company":
        query = query.where(CustomDashboard.is_company_managed.is_(True))
    elif scope == "shared":
        query = query.where(
            CustomDashboard.is_company_managed.is_(False),
            (CustomDashboard.user_id != user_id) | CustomDashboard.user_id.is_(None),
        )
    rows = (await db.execute(query.order_by(CustomDashboard.updated_at.desc()).limit(limit))).scalars().all()
    out: list[tuple[CustomDashboard, DashboardTier]] = []
    for row in rows:
        tier = dashboard_permission(row, user_id=user_id, user_group_ids=group_ids)
        if tier is not None:
            out.append((row, tier))
    return out


def _check_revision(dashboard: CustomDashboard, expected_revision: int | None) -> None:
    if expected_revision is not None and expected_revision != dashboard.revision:
        raise DashboardRevisionConflictError(dashboard.revision)


def _platform_sources(spec: object) -> dict[str, dict]:
    """The platform data sources of *spec* (either version), by id: what they read and who reads them."""
    if not isinstance(spec, dict):
        return {}
    definition = upgrade(spec)
    readers: dict[str, str] = {}
    for vid, viz in (definition.get("visualizations") or {}).items():
        sid = (viz.get("dataSources") or {}).get("primary") if isinstance(viz, dict) else None
        if sid:
            readers.setdefault(sid, vid)
    found = {}
    for sid, source in platform_sources(definition).items():
        options = source.get("options") or {}
        found[sid] = {
            "id": readers.get(sid, sid),
            "source": options.get("source"),
            "integration_id": str(options.get("integration_id")),
            "native": options.get("native"),
            "key": json.dumps(options, sort_keys=True, default=str),
        }
    # An entity input searches a platform as a platform source reads it: the same rules.
    for iid, entry in (definition.get("inputs") or {}).items():
        if isinstance(entry, dict) and entry.get("type") == "input.entity":
            options = entry.get("options") or {}
            found[f"inputs.{iid}"] = {
                "id": iid,
                "source": options.get("source"),
                "integration_id": str(options.get("integration_id")),
                "native": None,
                "key": json.dumps(options, sort_keys=True, default=str),
            }
    return found


def check_platform_widgets_allowed(previous: object, spec: dict) -> list[dict]:
    """The new or changed platform data sources of *spec* (``{id, source, integration_id}``, *id* the
    first visualization reading it); refused while the Dashboard Studio is off.

    Live OpenCTI / OpenAEV / OpenCRQ widgets are the studio's
    (``DASHBOARD_STUDIO``). With it off the routes answer 404 and the tool is
    never built, so this is the service's own guard: a write that reaches it
    anyway may keep the platform widgets already there, never add or change one.
    """
    from app.core.feature_flags import is_feature_enabled

    before = {sid: found["key"] for sid, found in _platform_sources(previous).items()}
    changed = [found for sid, found in _platform_sources(spec).items() if before.get(sid) != found["key"]]
    if changed and not is_feature_enabled("DASHBOARD_STUDIO"):
        raise DashboardSpecError(
            f"Widget '{changed[0].get('id')}' reads live OpenCTI, OpenAEV or OpenCRQ data, which needs the "
            "Dashboard Studio; it is not enabled on this platform. Use the other widget kinds."
        )
    return changed


async def check_platform_integrations(db: AsyncSession, user_id: uuid.UUID | None, widgets: list[dict]) -> None:
    """Refuse a platform widget whose integration the writer cannot read through.

    The same rule as drawing it (``platform_access.visible_platform_integrations``):
    a widget can only point at an integration of its source the writer sees and
    may use. Checked on the widgets a write adds or changes, against the rows.
    """
    if not widgets:
        return
    from app.services.platform_access import visible_platform_integrations

    usable = (
        {(str(i.id), i.provider) for i in await visible_platform_integrations(db, user_id)} if user_id else set()
    )
    for widget in widgets:
        if (str(widget.get("integration_id")), widget.get("source")) not in usable:
            raise DashboardSpecError(
                f"Widget '{widget.get('id')}': integration_id is not an integration of {widget.get('source')} you can "
                "use. List your integrations and pick one of that platform."
            )


def _write(dashboard: CustomDashboard, spec: dict) -> None:
    """Store the validated working definition *spec* in the native form (``dashboard_native``)."""
    from app.services.dashboard_native import to_native

    dashboard.spec = to_native(spec)
    dashboard.title = str(spec["title"])
    dashboard.widget_count = visualization_count(spec)


#: A dashboard's tags, at most, and each tag's length.
MAX_TAGS = 20
MAX_TAG_CHARS = 40


def clean_tags(tags: list[str]) -> list[str]:
    """Tags as stored: trimmed, lower-cased, blanks and repeats dropped, in their order, at most :data:`MAX_TAGS`."""
    kept: list[str] = []
    for tag in tags:
        word = " ".join(str(tag).split()).lower()[:MAX_TAG_CHARS]
        if word and word not in kept:
            kept.append(word)
    return kept[:MAX_TAGS]


async def duplicate_dashboard(db: AsyncSession, dashboard: CustomDashboard, *, user_id: uuid.UUID) -> CustomDashboard:
    """A private copy of *dashboard* for *user_id*: its definition, tags and the images its canvases draw, named
    "(copy)", shared with no one.

    Written like any new dashboard (``save_dashboard``): a platform widget is copied only when the person can use
    its integration, so a copy never reads through someone else's access (``DashboardSpecError`` otherwise). An
    image is a file of ONE dashboard (``dashboard_assets``): each is copied to the new one and named by its copy.
    """
    from sqlalchemy import Column, String

    from app.core.utils import fit_copy_name
    from app.services.custom_dashboard_spec import MAX_TITLE_CHARS
    from app.services.dashboard_assets import copy_drawn_assets

    # Fitted to the definition's title, shorter than the column.
    title = fit_copy_name(dashboard.title, Column("title", String(MAX_TITLE_CHARS)), suffix=" (copy)")
    spec = {**upgrade(dashboard.spec), "title": title}
    new_id = uuid.uuid4()
    copied = await copy_drawn_assets(db, source_id=dashboard.id, target_id=new_id, user_id=user_id, definition=spec)
    if copied:
        # File ids are UUIDs: their quoted spelling names nothing else in a definition.
        text = json.dumps(spec)
        for old, new in copied.items():
            text = text.replace(f'"{old}"', f'"{new}"')
        spec = json.loads(text)
    try:
        copy = await save_dashboard(
            db, user_id=user_id, spec_payload=spec, source="manual", summary=f"duplicated '{dashboard.title}'", new_id=new_id
        )
    except DashboardSpecError:
        # Refused (a widget reading through an integration the person cannot use): no copied file stays behind.
        from app.core.utils import format_exception_message
        from app.services.dashboard_assets import delete_dashboard_assets
        from app.services.file_storage import file_storage

        for key in await delete_dashboard_assets(db, new_id):
            try:
                await file_storage.delete(key)
            except Exception as exc:  # A leftover object is harmless: nothing names it.
                logger.warning("Could not purge a copied dashboard asset: %s", format_exception_message(exc))
        raise
    copy.tags = list(dashboard.tags or [])
    return copy


def empty_definition(title: str, description: str | None = None) -> dict:
    """A dashboard with nothing on it yet: one grid tab, its tab bar hidden, its description when given."""
    return {
        "version": 2,
        "title": title,
        **({"description": description} if description else {}),
        "visualizations": {},
        "layout": {
            "tabs": {"items": [{"layoutId": "main", "label": "Main"}], "options": {"showTabBar": False}},
            "layoutDefinitions": {"main": {"type": "grid", "structure": []}},
        },
    }


async def save_dashboard(
    db: AsyncSession,
    *,
    user_id: uuid.UUID,
    spec_payload: object,
    conversation_id: uuid.UUID | None = None,
    assignment_run_id: uuid.UUID | None = None,
    dashboard: CustomDashboard | None = None,
    expected_revision: int | None = None,
    source: str = "agent",
    summary: str | None = None,
    restored_from_revision: int | None = None,
    new_id: uuid.UUID | None = None,
) -> CustomDashboard:
    """Validate *spec_payload* and create a dashboard (under *new_id* when given), or replace one in place.

    Replacing keeps the id, so the ``[[DASHBOARD:<id>]]`` marker of an earlier
    turn and a bookmarked page show the new revision. *user_id* is who writes
    (the owner of a new dashboard). Raises ``DashboardSpecError`` before
    anything is written; the revision written is kept as a version.
    """
    from app.services.dashboard_native import from_native, is_native

    if is_native(spec_payload):
        spec = validate_definition(from_native(spec_payload))  # type: ignore[arg-type]
    else:
        spec = validate_definition(spec_payload) if is_definition(spec_payload) else from_widgets(spec_payload)
    previous = upgrade(dashboard.spec) if dashboard is not None else None
    changed = check_platform_widgets_allowed(previous, spec)
    await check_platform_integrations(db, user_id, changed)
    await check_source_references(db, user_id, previous, spec)
    if dashboard is None:
        dashboard = CustomDashboard(
            # Set here so the tool can put the id in its marker before the INSERT.
            id=new_id or uuid.uuid4(),
            user_id=user_id,
            conversation_id=conversation_id,
            assignment_run_id=assignment_run_id,
            revision=1,
        )
        _write(dashboard, spec)
        db.add(dashboard)
    else:
        _check_revision(dashboard, expected_revision)
        _write(dashboard, spec)
        dashboard.revision = (dashboard.revision or 1) + 1
        _stamp_origin(dashboard, conversation_id, assignment_run_id)
        await _follow_author(db, dashboard.id, user_id)
    await db.flush()
    await record_dashboard_version(
        db, dashboard, source=source, author_id=user_id, summary=summary, restored_from_revision=restored_from_revision
    )
    return dashboard


async def edit_dashboard(
    db: AsyncSession,
    dashboard: CustomDashboard,
    *,
    operations: object,
    title: str | None = None,
    subtitle: str | None = None,
    expected_revision: int | None = None,
    conversation_id: uuid.UUID | None = None,
    assignment_run_id: uuid.UUID | None = None,
    acting_user_id: uuid.UUID | None = None,
    source: str = "agent",
) -> CustomDashboard:
    """Apply widget operations (upsert / remove / move) and bump the revision.

    *acting_user_id* is who writes: a platform widget it adds or changes must
    read through an integration that person can use. The revision written is
    kept as a version.
    """
    _check_revision(dashboard, expected_revision)
    current = upgrade(dashboard.spec)
    spec = apply_definition_operations(current, operations, title=title, subtitle=subtitle)
    layout_only = (
        title is None
        and subtitle is None
        and isinstance(operations, list)
        and (all(isinstance(op, dict) and op.get("op") == "layout" for op in operations) or _placement_only(current, spec))
    )
    changed = check_platform_widgets_allowed(current, spec)
    await check_platform_integrations(db, acting_user_id, changed)
    await check_source_references(db, acting_user_id, current, spec)
    _write(dashboard, spec)
    dashboard.revision = (dashboard.revision or 1) + 1
    _stamp_origin(dashboard, conversation_id, assignment_run_id)
    if not layout_only:
        # Moving widgets reads nothing new; any other change does.
        await _follow_author(db, dashboard.id, acting_user_id)
    await db.flush()
    await record_dashboard_version(
        db,
        dashboard,
        source=source,
        author_id=acting_user_id,
        summary=REARRANGED if layout_only else _summarize_change(current, spec, title, subtitle),
        coalesce=layout_only,
    )
    return dashboard


async def _follow_author(db: AsyncSession, dashboard_id: uuid.UUID, author_id: uuid.UUID | None) -> None:
    """A published link reads what the dashboard now holds with its author's access (``follow_author``)."""
    from app.services.dashboard_public_links import follow_author

    await follow_author(db, dashboard_id, author_id)


def _placement_only(previous: dict, current: dict) -> bool:
    """Whether two definitions differ only by where their items sit or how they stack (a drag on a grid or a canvas)."""

    def unplaced(spec: dict) -> str:
        definition = upgrade(spec)
        for layout in ((definition.get("layout") or {}).get("layoutDefinitions") or {}).values():
            if isinstance(layout, dict):
                entries = [{k: v for k, v in e.items() if k != "position"} for e in layout.get("structure") or [] if isinstance(e, dict)]
                layout["structure"] = sorted(entries, key=lambda e: str(e.get("item")))
        return json.dumps(definition, sort_keys=True, default=str)

    return unplaced(previous) == unplaced(current)


def _summarize_change(previous: dict, current: dict, title: str | None, subtitle: str | None) -> str | None:
    """One line saying which widgets an edit added, changed or removed (a new place is not a change)."""

    def normalized(spec: dict) -> dict:
        # Both sides as validation writes them: an older row lacks the defaults it fills.
        definition = upgrade(spec)
        try:
            return validate_definition(definition)
        except DashboardSpecError:
            return definition

    old, new = normalized(previous), normalized(current)

    def content(definition: dict, vid: str) -> str:
        viz = definition["visualizations"][vid]
        sid = (viz.get("dataSources") or {}).get("primary")
        source = (definition.get("dataSources") or {}).get(sid) if sid else None
        return json.dumps([viz, source], sort_keys=True, default=str)

    def places(definition: dict) -> dict:
        return {
            (lid, entry.get("item")): entry.get("position")
            for lid, layout in definition["layout"]["layoutDefinitions"].items()
            for entry in layout.get("structure") or []
        }

    before, after = old.get("visualizations") or {}, new.get("visualizations") or {}
    parts = []
    added = [vid for vid in after if vid not in before]
    removed = [vid for vid in before if vid not in after]
    changed = [vid for vid in after if vid in before and content(old, vid) != content(new, vid)]
    for label, ids in (("added", added), ("changed", changed), ("removed", removed)):
        if ids:
            parts.append(f"{label} {', '.join(str(i) for i in ids[:5])}{' …' if len(ids) > 5 else ''}")
    for key, label in (("inputs", "changed inputs"), ("defaults", "changed defaults"), ("expressions", "changed expressions")):
        if (old.get(key) or {}) != (new.get(key) or {}):
            parts.append(label)
    if [t for t in old["layout"]["tabs"]["items"]] != [t for t in new["layout"]["tabs"]["items"]]:
        parts.append("changed tabs")
    if not parts and places(old) != places(new):
        parts.append(REARRANGED)
    if title is not None or subtitle is not None:
        parts.append("renamed")
    return "; ".join(parts)[:500] or None


async def record_dashboard_version(
    db: AsyncSession,
    dashboard: CustomDashboard,
    *,
    source: str,
    author_id: uuid.UUID | None,
    summary: str | None = None,
    restored_from_revision: int | None = None,
    coalesce: bool = False,
) -> None:
    """Keep the revision just written; prune past :data:`DASHBOARD_VERSION_RETENTION`.

    With *coalesce* (a layout-only edit), the newest version takes the new
    revision instead when it is the same person's layout-only edit of the last
    :data:`LAYOUT_VERSION_WINDOW_SECONDS`.

    Bookkeeping around the write: a failure is logged and the dashboard write
    still lands (it runs in a savepoint, so it cannot poison the session).
    """
    try:
        async with db.begin_nested():
            if coalesce and await _coalesced(db, dashboard, source=source, author_id=author_id):
                return
            db.add(
                CustomDashboardVersion(
                    dashboard_id=dashboard.id,
                    revision=dashboard.revision,
                    title=dashboard.title[:200],
                    widget_count=dashboard.widget_count,
                    spec=dashboard.spec,
                    source=source if source in VERSION_SOURCES else "manual",
                    summary=summary[:500] if summary else None,
                    restored_from_revision=restored_from_revision,
                    author_id=author_id,
                )
            )
            await db.flush()
            keep = (
                select(CustomDashboardVersion.id)
                .where(CustomDashboardVersion.dashboard_id == dashboard.id)
                .order_by(CustomDashboardVersion.revision.desc())
                .limit(DASHBOARD_VERSION_RETENTION)
            )
            await db.execute(
                delete(CustomDashboardVersion).where(
                    CustomDashboardVersion.dashboard_id == dashboard.id,
                    CustomDashboardVersion.id.not_in(keep.scalar_subquery()),
                )
            )
    except Exception as exc:
        logger.warning("Dashboard %s: version %s not recorded: %r", dashboard.id, dashboard.revision, str(exc)[:200])


async def _coalesced(db: AsyncSession, dashboard: CustomDashboard, *, source: str, author_id: uuid.UUID | None) -> bool:
    newest = (
        await db.execute(
            select(CustomDashboardVersion)
            .where(CustomDashboardVersion.dashboard_id == dashboard.id)
            .order_by(CustomDashboardVersion.revision.desc())
            .limit(1)
        )
    ).scalar_one_or_none()
    now = datetime.now(UTC)
    if (
        newest is None
        or newest.summary != REARRANGED
        or newest.source != source
        or newest.author_id != author_id
        or newest.created_at is None
        or (now - newest.created_at).total_seconds() > LAYOUT_VERSION_WINDOW_SECONDS
    ):
        return False
    newest.revision = dashboard.revision
    newest.spec = dashboard.spec
    newest.title = dashboard.title[:200]
    newest.widget_count = dashboard.widget_count
    newest.created_at = now
    await db.flush()
    return True


async def list_dashboard_versions(db: AsyncSession, dashboard_id: uuid.UUID) -> list[CustomDashboardVersion]:
    """The kept versions of a dashboard, newest first."""
    rows = await db.execute(
        select(CustomDashboardVersion)
        .where(CustomDashboardVersion.dashboard_id == dashboard_id)
        .order_by(CustomDashboardVersion.revision.desc())
    )
    return list(rows.scalars().all())


async def get_dashboard_version(
    db: AsyncSession, dashboard_id: uuid.UUID, revision: int
) -> CustomDashboardVersion | None:
    return (
        await db.execute(
            select(CustomDashboardVersion).where(
                CustomDashboardVersion.dashboard_id == dashboard_id, CustomDashboardVersion.revision == revision
            )
        )
    ).scalar_one_or_none()


async def restore_dashboard_version(
    db: AsyncSession,
    dashboard: CustomDashboard,
    revision: int,
    *,
    author_id: uuid.UUID,
    expected_revision: int | None = None,
) -> CustomDashboard | None:
    """Write version *revision* back as a NEW revision (``source="restore"``), or ``None`` if not kept.

    The old spec is validated again like any write: a version that no longer
    passes the checks is refused with the same error, never written blindly.
    """
    version = await get_dashboard_version(db, dashboard.id, revision)
    if version is None:
        return None
    return await save_dashboard(
        db,
        user_id=author_id,
        spec_payload=version.spec,
        dashboard=dashboard,
        expected_revision=expected_revision,
        source="restore",
        summary=f"Restored revision {revision}",
        restored_from_revision=revision,
    )


def _version_text(version: CustomDashboardVersion) -> str:
    """A version as the compare shows it: its manifest as indented JSON, keys in the stored order."""
    return json.dumps(dashboard_manifest(version.spec), indent=2, ensure_ascii=False, default=str)


def _diff_versions(left: CustomDashboardVersion, right: CustomDashboardVersion) -> dict:
    from app.services.prompt_versioning import build_version_diff

    diff = build_version_diff(
        from_content=_version_text(left),
        to_content=_version_text(right),
        from_version=left.revision,
        to_version=right.revision,
    )
    renamed = left.title if left.title != right.title else None
    diff["changes"] = _summarize_change(left.spec, right.spec, renamed, None)
    return diff


async def diff_dashboard_versions(
    db: AsyncSession, dashboard_id: uuid.UUID, from_revision: int, to_revision: int
) -> dict | None:
    """Two kept versions compared line by line (their definitions as JSON), with which widgets changed; ``None`` if one is not kept.

    The line diff runs off the event loop: a definition holds up to
    ``MAX_DEFINITION_BYTES``, and comparing two large ones is CPU work.
    """
    import asyncio

    left = await get_dashboard_version(db, dashboard_id, from_revision)
    right = await get_dashboard_version(db, dashboard_id, to_revision)
    if left is None or right is None:
        return None
    return await asyncio.to_thread(_diff_versions, left, right)


def serialize_version(version: CustomDashboardVersion, *, include_spec: bool = False) -> dict:
    payload = {
        "revision": version.revision,
        "title": version.title,
        "widget_count": version.widget_count,
        "source": version.source,
        "summary": version.summary,
        "restored_from_revision": version.restored_from_revision,
        "author_id": str(version.author_id) if version.author_id else None,
        "created_at": version.created_at.isoformat() if version.created_at else None,
    }
    if include_spec:
        payload["spec"] = upgrade(version.spec)
        payload["manifest"] = dashboard_manifest(version.spec, payload["spec"])
    return payload


def _stamp_origin(
    dashboard: CustomDashboard, conversation_id: uuid.UUID | None, assignment_run_id: uuid.UUID | None
) -> None:
    # The run that last refreshed it; the conversation it was first built in.
    if assignment_run_id is not None:
        dashboard.assignment_run_id = assignment_run_id
    if conversation_id is not None and dashboard.conversation_id is None:
        dashboard.conversation_id = conversation_id
