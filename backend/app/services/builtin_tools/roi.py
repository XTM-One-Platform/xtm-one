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

"""The ``roi`` built-in tools: an agent reads ROI and declares outcomes for its user (#10006, slice S2).

Six tools over the rules the REST routes of ``api/v1/roi.py`` apply, never a
rule of their own:

* ``list_roi_units``: the unit catalog of the platform's mode (built-ins with
  their origin and minutes, then the custom units).
* ``get_roi_declarations``: what an agent, an assignment or a fleet counts
  (``roi_subjects.readable_subject``: whoever sees the subject).
* ``set_roi_declaration`` / ``delete_roi_declaration``: change it
  (``roi_subjects.manageable_subject``: owner, ``config`` grantee, admin). A
  set queues the retroactive pass of #10016 by default; ``dry_run`` answers
  what the pass would write and saves nothing.
* ``get_roi_report``: the report of one agent, one fleet, the caller's scope
  (``mine``) or the organization (administrators, or every reader while the
  organization setting shows the totals), the same access as ``GET /roi``.
* ``list_roi_records``: the records, ``outcome="unreviewed"`` the review
  queue, the same access as ``GET /roi/records``.
* ``get_roi_chat_series`` / ``adjust_roi_chat_line``: the "From chat,
  estimated" series and a person's correction of one line (#10544 S4), the
  same access as ``GET /roi/chat`` and ``PUT /roi/chat/records/{id}``.

* ``create_roi_unit`` / ``update_roi_unit`` / ``archive_roi_unit``: the unit
  catalog, administrators only, as ``POST /roi/units``, ``PUT /roi/units/{key}``,
  ``PUT /roi/baselines/{unit}`` and ``POST /roi/units/{key}/archive`` (#11881).
  Sam, 14 Nov 2026: an agent uses the seeded units first and adds its own only
  when none fits the work or its minutes do not fit the organization's
  industry; an update of a built-in unit stores the organization's override,
  never rewrites the seed. No tool creates a rate role.

Each call opens its own short session; a write is audited as the user,
``via: agent``. A refusal is answered in words, never raised.
"""

from __future__ import annotations

import difflib
import json
import uuid
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING, Any

from app.core import database
from app.core.utils import safe_uuid
from app.tools.base import CallableToolExecutor, ToolDefinition

from . import agent_builder as _builder
from ._shared import logger

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from app.models.user import User

ROI_SUITE_ID = "roi"

ROI_FUNCTIONS: tuple[str, ...] = (
    "list_roi_units",
    "get_roi_declarations",
    "set_roi_declaration",
    "delete_roi_declaration",
    "get_roi_report",
    "list_roi_records",
    "get_roi_chat_series",
    "adjust_roi_chat_line",
    "create_roi_unit",
    "update_roi_unit",
    "archive_roi_unit",
)

_ANONYMOUS_REFUSAL = "ROI is not available for anonymous users."
_FAILED = "The ROI call failed. Nothing was saved; please try again."
#: The most records one call lists.
_RECORDS_MAX = 200
#: The closest catalog keys an unknown unit is answered with.
_CLOSEST_MAX = 3
_ADMIN_ONLY = "Only an administrator's agent changes the ROI unit catalog."


class _RefusedError(Exception):
    """A refusal handed back to the model; the session is rolled back."""


def _source() -> str:
    # The ROI functions belong to the platform group; ``roi`` is its alias.
    return "builtin:platform"


def _dump(payload: Any) -> str:
    # No space after the separators: an agent reads the JSON, and a wide report stays under the budget (#10664).
    return json.dumps(payload, default=str, ensure_ascii=False, separators=(",", ":"))


#: The keys of the ledger's figures a wide-scope tool answer keeps (#12077 L4): bounded whatever the agents.
_COMPACT_FIGURES_KEYS = (
    "scope",
    "currency",
    "id",
    "window_first",
    "window_last",
    "totals",
    "by_source",
    "cost_masked",
    "since",
)


def _compact_figures(figures: dict[str, Any]) -> dict[str, Any]:
    """The ledger's figures of a wide scope for a tool answer: the hero and By source (at most three rows)."""
    return {key: figures.get(key) for key in _COMPACT_FIGURES_KEYS}


def _subject_type(value: object) -> str:
    from app.services.roi_records import SUBJECT_TYPES

    subject_type = str(value or "").strip()
    if subject_type not in SUBJECT_TYPES:
        raise _RefusedError(f"subject_type must be one of {', '.join(SUBJECT_TYPES)}.")
    return subject_type


def _sentence(text: object) -> str:
    """*text* as a sentence: a refusal from the service ends without a full stop."""
    words = str(text).strip()
    return words if not words or words.endswith((".", "!", "?")) else f"{words}."


def unknown_unit_message(unit: str, known: dict[str, str]) -> str:
    """The refusal of an unknown *unit*: the closest keys of *known* (key -> name) and the way to add one."""
    wanted = unit.strip().lower()
    by_key = difflib.get_close_matches(wanted, list(known), n=_CLOSEST_MAX, cutoff=0.4)
    by_name = [
        key
        for key, name in known.items()
        if key not in by_key and any(part and part in name.lower() for part in wanted.split("_") if len(part) > 3)
    ]
    closest = (by_key + sorted(by_name))[:_CLOSEST_MAX]
    hint = f" Closest units: {', '.join(closest)}." if closest else ""
    return (
        f"Unit {unit!r} is not a known unit.{hint} If none fits, an administrator's agent can add one "
        "with create_roi_unit."
    )


def _minutes(value: object) -> tuple[float, float, float]:
    """``[low, likely, high]`` as three numbers above zero, else a refusal."""
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except ValueError:
            value = None
    if not isinstance(value, (list, tuple)) or len(value) != 3:
        raise _RefusedError("minutes must be three numbers: [low, likely, high].")
    try:
        low, likely, high = (float(v) for v in value)
    except (TypeError, ValueError):
        raise _RefusedError("minutes must be three numbers: [low, likely, high].") from None
    if min(low, likely, high) <= 0:
        raise _RefusedError("minutes must be above 0.")
    return low, likely, high


def _number(value: object, field: str) -> float:
    try:
        return float(str(value))
    except ValueError:
        raise _RefusedError(f"{field} must be a number.") from None


async def _declarable_names(db: AsyncSession) -> dict[str, str]:
    """Every unit a declaration may name (key -> name): the built-ins, then the custom units not archived."""
    from app.services import roi, roi_records

    names = {key: b.name for key, b in roi.BASELINES_BY_UNIT.items()}
    names.update({u.key: u.name for u in await roi_records.list_units(db) if u.archived_at is None})
    return names


def _require_admin(user: User) -> None:
    """The unit catalog is an administrator's, as its REST routes (``require_admin``)."""
    if not bool(getattr(user, "is_admin", False)):
        raise _RefusedError(_ADMIN_ONLY)


def _bool(value: object, default: bool) -> bool:
    if value is None or value == "":
        return default
    if isinstance(value, str):
        return value.strip().lower() in ("1", "true", "yes")
    return bool(value)


def build_roi_tools(user_id: str) -> tuple[list[ToolDefinition], CallableToolExecutor]:
    """The ``roi`` suite for *user_id*, as definitions and their executor."""

    async def _run(action: Callable[[AsyncSession, User], Awaitable[str]], *, write: bool) -> str:
        from app.services.roi_subjects import RoiSubjectError

        uid = safe_uuid(user_id) if user_id else None
        if uid is None:
            return _ANONYMOUS_REFUSAL
        async with database.async_session() as db:
            user = await _builder._load_caller_user_for_builder(db, uid)
            if user is None:
                return "User not found."
            try:
                answer = await action(db, user)
                if write:
                    await db.commit()
                return answer
            except (_RefusedError, ValueError) as refusal:
                await db.rollback()
                return f"{_sentence(refusal)} Nothing was saved." if write else str(refusal)
            except RoiSubjectError as refusal:
                await db.rollback()
                return refusal.detail
            except Exception:
                logger.exception("roi tool failed for user %s", uid)
                await db.rollback()
                return _FAILED

    # ── list_roi_units ────────────────────────────────────────────────

    async def _list_roi_units() -> str:
        async def _action(db: AsyncSession, _user: User) -> str:
            from app.core.config import settings
            from app.services import roi, roi_records

            baselines, overridden = await roi_records.effective_baselines(db)
            origins = await roi_records.baseline_origins(db)
            units: list[dict[str, Any]] = []
            for b in roi.builtin_baselines(xtm_one=settings.is_xtm_one):
                eff = baselines[b.unit]
                origin = (origins.get(b.unit) or {}).get("origin") or "default"
                units.append(
                    {
                        "key": b.unit,
                        "name": eff.name,
                        "origin": origin if b.unit in overridden else "default",
                        "rate_role": eff.rate_role,
                        "minutes": [eff.manual_min_low, eff.manual_min_likely, eff.manual_min_high],
                        "review_min": eff.review_min,
                        "source": eff.minutes_source,
                    }
                )
            for u in await roi_records.list_units(db):
                snap = roi_records.unit_snapshot(u)
                units.append({**snap, "origin": "custom"})
            return _dump({"units": units})

        return await _run(_action, write=False)

    # ── create_roi_unit / update_roi_unit / archive_roi_unit (#11881) ─

    async def _create_roi_unit(
        key: str = "",
        name: str = "",
        rate_role: str = "",
        minutes: object = None,
        review_min: object = 0,
        source: str = "",
    ) -> str:
        async def _action(db: AsyncSession, user: User) -> str:
            from app.services import roi_records
            from app.services.audit import log_audit

            _require_admin(user)
            if not str(source or "").strip():
                raise _RefusedError("source is required: say where the minutes come from.")
            low, likely, high = _minutes(minutes)
            unit = await roi_records.create_unit(
                db,
                key=str(key).strip(),
                name=str(name),
                low=low,
                likely=likely,
                high=high,
                review=_number(review_min, "review_min"),
                rate_role=str(rate_role).strip() or "analyst",
                source=str(source).strip(),
                user_id=user.id,
            )
            snap = roi_records.unit_snapshot(unit)
            await log_audit(
                db,
                user=user,
                action="create",
                entity_type="roi_unit",
                entity_name=unit.name,
                summary=f"Added the ROI unit {unit.name}",
                details={**snap, "via": "agent"},
            )
            return _dump({"saved": True, "unit": {**snap, "origin": "custom"}})

        return await _run(_action, write=True)

    async def _update_roi_unit(
        key: str = "",
        name: str = "",
        rate_role: str = "",
        minutes: object = None,
        review_min: object = None,
        source: str = "",
    ) -> str:
        async def _action(db: AsyncSession, user: User) -> str:
            from app.services import roi, roi_records
            from app.services.audit import log_audit

            _require_admin(user)
            wanted = str(key).strip()
            if wanted in roi.BASELINES_BY_UNIT:
                # A built-in unit: the organization's override of its minutes, the seed stays as shipped.
                baselines, _overridden = await roi_records.effective_baselines(db)
                current = baselines[wanted]
                low, likely, high = (
                    _minutes(minutes)
                    if minutes not in (None, "")
                    else (current.manual_min_low, current.manual_min_likely, current.manual_min_high)
                )
                review = current.review_min if review_min in (None, "") else _number(review_min, "review_min")
                if not str(source or "").strip():
                    raise _RefusedError("source is required: say where the organization's minutes come from.")
                await roi_records.upsert_override(
                    db,
                    unit=wanted,
                    low=low,
                    likely=likely,
                    high=high,
                    review=review,
                    source=str(source).strip(),
                    user_id=user.id,
                )
                await log_audit(
                    db,
                    user=user,
                    action="configure",
                    entity_type="roi_baseline",
                    entity_name=wanted,
                    summary=f"Set the ROI baseline of {wanted}",
                    details={"unit": wanted, "minutes": [low, likely, high], "review_min": review, "via": "agent"},
                )
                return _dump(
                    {
                        "saved": True,
                        "unit": {
                            "key": wanted,
                            "origin": roi_records.ORIGIN_CUSTOMIZED,
                            "minutes": [low, likely, high],
                        },
                        "note": "Built-in unit: the organization's minutes are stored, name and rate role stay seeded.",
                    }
                )
            from app.models.roi import RoiUnit

            row = await db.get(RoiUnit, wanted)
            if row is None:
                raise _RefusedError(f"No unit {wanted!r}.")
            low, likely, high = (
                _minutes(minutes)
                if minutes not in (None, "")
                else (float(row.manual_min_low), float(row.manual_min_likely), float(row.manual_min_high))
            )
            unit = await roi_records.update_unit(
                db,
                wanted,
                name=str(name).strip() or row.name,
                role=row.role or "",
                low=low,
                likely=likely,
                high=high,
                review=float(row.review_min) if review_min in (None, "") else _number(review_min, "review_min"),
                rate_role=str(rate_role).strip() or row.rate_role or "analyst",
                source=str(source).strip() or row.source or "",
                user_id=user.id,
            )
            if unit is None:
                raise _RefusedError(f"No unit {wanted!r}.")
            snap = roi_records.unit_snapshot(unit)
            await log_audit(
                db,
                user=user,
                action="update",
                entity_type="roi_unit",
                entity_name=unit.name,
                summary=f"Changed the ROI unit {unit.name} (version {unit.version})",
                details={**snap, "via": "agent"},
            )
            return _dump({"saved": True, "unit": {**snap, "origin": "custom"}})

        return await _run(_action, write=True)

    async def _archive_roi_unit(key: str = "") -> str:
        async def _action(db: AsyncSession, user: User) -> str:
            from app.services import roi, roi_records
            from app.services.audit import log_audit

            _require_admin(user)
            wanted = str(key).strip()
            if wanted in roi.BASELINES_BY_UNIT:
                raise _RefusedError(f"{wanted!r} is a built-in unit and cannot be archived.")
            unit = await roi_records.archive_unit(db, wanted, user_id=user.id)
            if unit is None:
                raise _RefusedError(f"No unit {wanted!r}.")
            await log_audit(
                db,
                user=user,
                action="update",
                entity_type="roi_unit",
                entity_name=unit.name,
                summary=f"Archived the ROI unit {unit.name}",
                details={"key": unit.key, "via": "agent"},
            )
            return _dump(
                {
                    "saved": True,
                    "unit": {**roi_records.unit_snapshot(unit), "origin": "custom"},
                    "note": "No longer declarable; its past records keep their figures.",
                }
            )

        return await _run(_action, write=True)

    # ── get_roi_declarations ──────────────────────────────────────────

    async def _get_roi_declarations(subject_type: str = "", subject_id: str = "") -> str:
        async def _action(db: AsyncSession, user: User) -> str:
            from app.services import roi_subjects

            kind = _subject_type(subject_type)
            sid = await roi_subjects.readable_subject(db, user, kind, subject_id)
            declaration = await roi_subjects.read_declaration(db, kind, sid)
            if declaration is None:
                return f"The {kind} declares no unit: it is not counted in ROI."
            return _dump(declaration)

        return await _run(_action, write=False)

    # ── set_roi_declaration ───────────────────────────────────────────

    async def _set_roi_declaration(
        subject_type: str = "",
        subject_id: str = "",
        unit: str = "",
        count_mode: str = "",
        units_per_count: object = 1,
        count_path: str = "",
        backfill: object = True,
        dry_run: object = False,
    ) -> str:
        async def _action(db: AsyncSession, user: User) -> str:
            from app.services import roi_backfill, roi_records, roi_subjects
            from app.services.audit import log_audit

            kind = _subject_type(subject_type)
            sid = await roi_subjects.manageable_subject(db, user, kind, subject_id)
            wanted = str(unit).strip()
            if wanted not in await roi_records.known_units(db):
                raise _RefusedError(unknown_unit_message(wanted, await _declarable_names(db)))
            try:
                per_count = int(str(units_per_count or 1))
            except ValueError:
                raise _RefusedError("units_per_count must be a whole number.") from None
            row = await roi_records.put_declaration(
                db,
                subject_type=kind,
                subject_id=sid,
                unit=str(unit).strip(),
                count_mode=str(count_mode).strip(),
                count_path=count_path or None,
                units_per_count=per_count,
                user_id=user.id,
            )
            declaration = roi_subjects.declaration_dict(
                row, default=await roi_subjects.declaration_default(db, kind, sid)
            )
            if _bool(dry_run, False):
                preview = await roi_backfill.backfill_subject(db, subject_type=kind, subject_id=sid, dry_run=True)
                await db.rollback()
                return _dump(
                    {"dry_run": True, "saved": False, "declaration": declaration, "backfill": preview.as_dict()}
                )
            wants_backfill = _bool(backfill, True)
            await log_audit(
                db,
                user=user,
                action="configure",
                entity_type="roi_declaration",
                entity_id=sid,
                entity_name=row.unit,
                summary=f"Declared the ROI unit {row.unit} ({row.count_mode}) on the {kind}",
                details={
                    "subject_type": kind,
                    "unit": row.unit,
                    "count_mode": row.count_mode,
                    "count_path": row.count_path,
                    "units_per_count": row.units_per_count,
                    "backfill": wants_backfill,
                    "via": "agent",
                },
            )
            await db.commit()
            queued = await roi_backfill.enqueue_backfill(kind, sid) if wants_backfill else False
            return _dump(
                {"saved": True, "declaration": declaration, "backfill": {"requested": wants_backfill, "queued": queued}}
            )

        return await _run(_action, write=True)

    # ── delete_roi_declaration ────────────────────────────────────────

    async def _delete_roi_declaration(subject_type: str = "", subject_id: str = "") -> str:
        async def _action(db: AsyncSession, user: User) -> str:
            from app.services import roi_records, roi_subjects
            from app.services.audit import log_audit

            kind = _subject_type(subject_type)
            sid = await roi_subjects.manageable_subject(db, user, kind, subject_id)
            if not await roi_records.delete_declaration(db, kind, sid):
                return f"The {kind} declares no unit. Nothing was changed."
            await log_audit(
                db,
                user=user,
                action="configure",
                entity_type="roi_declaration",
                entity_id=sid,
                entity_name=kind,
                summary=f"Removed the ROI declaration of the {kind}",
                details={"subject_type": kind, "via": "agent"},
            )
            return f"Removed the declaration of the {kind}: resolution falls through to the next level."

        return await _run(_action, write=True)

    # ── get_roi_report ────────────────────────────────────────────────

    async def _get_roi_report(
        scope: str = "mine",
        id: str = "",
        window: str = "30d",
        include_idle: object = False,
        limit: object = 20,
        offset: object = 0,
    ) -> str:
        async def _action(db: AsyncSession, user: User) -> str:
            from app.api.v1.helpers import agent_tool_admin_bypass
            from app.api.v1.platform_settings_helpers import get_platform_settings
            from app.services import roi_access, roi_report

            kind = str(scope or "mine").strip()
            if kind not in roi_report.SCOPES:
                raise _RefusedError(f"scope must be one of {', '.join(roi_report.SCOPES)}.")
            span = str(window or "30d").strip()
            if span not in roi_report.REPORT_WINDOWS:
                raise _RefusedError(f"window must be one of {', '.join(roi_report.REPORT_WINDOWS)}.")
            # Whose ROI the caller sees is visibility: an admin's agent takes the
            # bypass only through the helper (strict admin access for agent tools).
            is_admin = agent_tool_admin_bypass(user)
            reader = None
            scope_id: uuid.UUID | None = None
            if kind == roi_report.SCOPE_MINE:
                reader = await roi_access.reader_scope(db, user_id=user.id, is_admin=is_admin)
            elif kind == roi_report.SCOPE_PLATFORM:
                if not is_admin:
                    if not (await get_platform_settings(db)).roi_org_totals_visible:
                        raise _RefusedError(
                            "The organization-wide totals are shown to administrators only. "
                            'Use scope "mine", "agent" or "fleet".'
                        )
                    reader = await roi_access.reader_scope(db, user_id=user.id, is_admin=False)
            else:
                scope_id = safe_uuid(id) if id else None
                if scope_id is None:
                    raise _RefusedError(f"A {kind} report needs id (the {kind}'s UUID).")
                allowed = is_admin
                if not allowed and kind == roi_report.SCOPE_FLEET:
                    allowed = await roi_access.fleet_roi_reader(
                        db, user_id=user.id, is_admin=False, fleet_id=scope_id
                    ) or await roi_access.fleet_visible(db, user_id=user.id, fleet_id=scope_id)
                if not allowed and kind == roi_report.SCOPE_AGENT:
                    allowed = await roi_access.agent_roi_reader(db, user_id=user.id, is_admin=False, agent_id=scope_id)
                if not allowed:
                    raise _RefusedError(f"You cannot read the ROI of this {kind}.")
            try:
                size = max(1, min(int(str(limit or 20)), _RECORDS_MAX))
                skip = max(0, int(str(offset or 0)))
            except ValueError:
                raise _RefusedError("limit and offset must be whole numbers.") from None
            report = await roi_report.load_report(db, window=span, scope=kind, scope_id=scope_id, reader=reader)
            # The ledger's figures of the same scope (#12077 L4, design 12.8): what GET /roi answers as
            # ``figures``, read through the same function with the acting user as viewer.
            from app.services import roi_ledger

            viewer = await roi_ledger.viewer_for(db, user_id=user.id, is_admin=is_admin)
            ledger = (await roi_ledger.figures(db, scope=kind, viewer=viewer, scope_id=scope_id, window=span)).as_dict()
            if scope_id is not None:
                # One agent or one fleet: the whole report, its computation included.
                return _dump({**roi_report.report_dict(report), "figures": ledger})
            # A wide scope: bounded whatever the number of agents (#10627): the hero and By source only.
            compact = roi_report.compact_report_dict(
                report, include_idle=_bool(include_idle, False), limit=size, offset=skip
            )
            return _dump({**compact, "figures": _compact_figures(ledger)})

        return await _run(_action, write=False)

    # ── list_roi_records ──────────────────────────────────────────────

    async def _list_roi_records(
        agent_id: str = "", fleet_id: str = "", outcome: str = "", limit: object = 50, offset: object = 0
    ) -> str:
        async def _action(db: AsyncSession, user: User) -> str:
            from app.api.v1.helpers import agent_tool_admin_bypass
            from app.services import roi_access, roi_records

            fleet = safe_uuid(fleet_id) if fleet_id else None
            agent = safe_uuid(agent_id) if agent_id else None
            if not agent_tool_admin_bypass(user) and not (
                fleet is not None
                and await roi_access.fleet_roi_reader(db, user_id=user.id, is_admin=False, fleet_id=fleet)
            ):
                raise _RefusedError(
                    "Only administrators, and the managers of a fleet for that fleet (fleet_id), list ROI records."
                )
            try:
                size = max(1, min(int(str(limit or 50)), _RECORDS_MAX))
                skip = max(0, int(str(offset or 0)))
            except ValueError:
                raise _RefusedError("limit and offset must be whole numbers.") from None
            rows = await roi_records.list_records(
                db, agent_id=agent, fleet_id=fleet, outcome=outcome or None, limit=size, offset=skip
            )
            return _dump({"records": [roi_records.record_dict(r) for r in rows], "offset": skip, "limit": size})

        return await _run(_action, write=False)

    # ── get_roi_chat_series / adjust_roi_chat_line (#10544 S4) ───────

    async def _chat_reader(db: AsyncSession, user: User) -> Any:
        from app.api.v1.helpers import agent_tool_admin_bypass
        from app.services import roi_chat_series

        return await roi_chat_series.reader_for(db, user_id=user.id, is_admin=agent_tool_admin_bypass(user))

    async def _get_roi_chat_series(
        window: str = "30d", agent_id: str = "", limit: object = 20, offset: object = 0
    ) -> str:
        async def _action(db: AsyncSession, user: User) -> str:
            from app.services import roi_chat_series, roi_report

            span = str(window or "30d").strip()
            if span not in roi_report.REPORT_WINDOWS:
                raise _RefusedError(f"window must be one of {', '.join(roi_report.REPORT_WINDOWS)}.")
            agent = safe_uuid(agent_id) if agent_id else None
            if agent_id and agent is None:
                raise _RefusedError("agent_id must be a UUID.")
            try:
                size = max(1, min(int(str(limit or 20)), roi_chat_series.LINES_MAX))
                skip = max(0, int(str(offset or 0)))
            except ValueError:
                raise _RefusedError("limit and offset must be whole numbers.") from None
            reader = await _chat_reader(db, user)
            series = await roi_chat_series.load_series(
                db, reader=reader, window=span, agent_id=agent, limit=size, offset=skip
            )
            # The ledger's chat totals of the same scope, as GET /roi/chat answers them (#12077 L4b).
            from app.api.v1.helpers import agent_tool_admin_bypass
            from app.services import roi_ledger

            viewer = await roi_ledger.viewer_for(db, user_id=user.id, is_admin=agent_tool_admin_bypass(user))
            # The lines read are the caller's own unless an administrator: the totals follow (mine).
            series["figures"] = await roi_ledger.chat_figures(
                db, viewer=viewer, mine=agent is None and not viewer.organization, agent_id=agent, window=span
            )
            return _dump(series)

        return await _run(_action, write=False)

    async def _adjust_roi_chat_line(record_id: str = "", minutes: object = None) -> str:
        async def _action(db: AsyncSession, user: User) -> str:
            from app.services import roi_chat_series
            from app.services.audit import log_audit

            rid = safe_uuid(record_id) if record_id else None
            row = await roi_chat_series.get_line(db, rid) if rid is not None else None
            reader = await _chat_reader(db, user)
            if row is None or not roi_chat_series.readable(reader, agent_id=row.agent_id, user_id=row.user_id):
                raise _RefusedError("Chat line not found.")
            value: float | None
            if minutes is None or minutes == "":
                value = None
            else:
                try:
                    value = float(str(minutes))
                except ValueError:
                    raise _RefusedError("minutes must be a number, or empty to return to the estimate.") from None
            previous = row.adjusted_min
            roi_chat_series.apply_correction(row, minutes=value, user_id=user.id)
            await log_audit(
                db,
                user=user,
                action="update",
                entity_type="roi_chat_record",
                entity_id=str(row.id),
                entity_name=row.title or row.kind,
                summary="Corrected a chat ROI line"
                if value is not None
                else "Returned a chat ROI line to its estimate",
                details={"previous": previous, "minutes": value, "kind": row.kind, "via": "agent"},
            )
            return _dump({"saved": True, "id": str(row.id), "adjusted_min": row.adjusted_min})

        return await _run(_action, write=True)

    # ── Definitions ───────────────────────────────────────────────────

    subject = {
        "subject_type": {"type": "string", "enum": ["agent", "assignment", "fleet"]},
        "subject_id": {"type": "string", "description": "The subject's UUID."},
    }
    definitions = [
        ToolDefinition(
            name="list_roi_units",
            description="List the ROI units of work (key, rate role, minutes, source).",
            input_schema={"type": "object", "properties": {}, "required": []},
            source=_source(),
        ),
        ToolDefinition(
            name="get_roi_declarations",
            description="Read the ROI unit an agent, assignment or fleet counts.",
            input_schema={"type": "object", "properties": subject, "required": ["subject_type", "subject_id"]},
            source=_source(),
        ),
        ToolDefinition(
            name="set_roi_declaration",
            description=(
                "Declare the unit (from list_roi_units) an agent, assignment or fleet counts. "
                "Past runs and done items are backfilled; dry_run saves nothing."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    **subject,
                    "unit": {"type": "string"},
                    "count_mode": {"type": "string", "enum": ["per_run", "per_output_item", "per_work_item"]},
                    "units_per_count": {"type": "integer", "default": 1},
                    "count_path": {
                        "type": "string",
                        "description": (
                            "per_output_item: output list path. per_work_item: pull_request_merged or "
                            "result_contains:TEXT|TEXT."
                        ),
                    },
                    "backfill": {"type": "boolean", "default": True},
                    "dry_run": {"type": "boolean", "default": False},
                },
                "required": ["subject_type", "subject_id", "unit", "count_mode"],
            },
            source=_source(),
        ),
        ToolDefinition(
            name="delete_roi_declaration",
            description="Remove the declaration of an agent, assignment or fleet.",
            input_schema={"type": "object", "properties": subject, "required": ["subject_type", "subject_id"]},
            source=_source(),
        ),
        ToolDefinition(
            name="get_roi_report",
            description=(
                "ROI report. mine/platform: totals, rows paged by value, idle agents counted "
                "unless include_idle; detail: scope agent/fleet."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "scope": {"type": "string", "enum": ["mine", "agent", "fleet", "platform"], "default": "mine"},
                    "id": {"type": "string", "description": "agent or fleet scope: its UUID."},
                    "window": {"type": "string", "enum": ["7d", "30d", "90d", "quarter", "all"], "default": "30d"},
                    "include_idle": {"type": "boolean"},
                    "limit": {"type": "integer"},
                    "offset": {"type": "integer"},
                },
                "required": [],
            },
            source=_source(),
        ),
        ToolDefinition(
            name="list_roi_records",
            description='List ROI records, newest first; "unreviewed" is the review queue.',
            input_schema={
                "type": "object",
                "properties": {
                    "agent_id": {"type": "string"},
                    "fleet_id": {"type": "string"},
                    "outcome": {"type": "string", "enum": ["", "unreviewed", "accepted", "modified", "rejected"]},
                    "limit": {"type": "integer", "default": 50},
                    "offset": {"type": "integer", "default": 0},
                },
                "required": [],
            },
            source=_source(),
        ),
        ToolDefinition(
            name="get_roi_chat_series",
            description=(
                'ROI "From chat, estimated": decks, reports, videos, documents built in chat, '
                "low estimate per line, separate from automation."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "window": {"type": "string", "enum": ["7d", "30d", "90d", "quarter", "all"], "default": "30d"},
                    "agent_id": {"type": "string"},
                    "limit": {"type": "integer"},
                    "offset": {"type": "integer"},
                },
                "required": [],
            },
            source=_source(),
        ),
        ToolDefinition(
            name="adjust_roi_chat_line",
            description="Correct one chat ROI line in minutes; empty minutes returns it to the estimate.",
            input_schema={
                "type": "object",
                "properties": {
                    "record_id": {"type": "string", "description": "The line id from get_roi_chat_series."},
                    "minutes": {"type": "number"},
                },
                "required": ["record_id"],
            },
            source=_source(),
        ),
        ToolDefinition(
            name="create_roi_unit",
            description="Admin: add a custom ROI unit when no seeded unit of list_roi_units fits the work.",
            input_schema={
                "type": "object",
                "properties": {
                    "key": {"type": "string", "description": "Slug: 2-64 of a-z, 0-9, _."},
                    "name": {"type": "string"},
                    "rate_role": {"type": "string"},
                    "minutes": {"type": "array", "items": {"type": "number"}, "description": "[low, likely, high]"},
                    "review_min": {"type": "number"},
                    "source": {"type": "string", "description": "Where the minutes come from."},
                },
                "required": ["key", "name", "rate_role", "minutes", "review_min", "source"],
            },
            source=_source(),
        ),
        ToolDefinition(
            name="update_roi_unit",
            description="Admin: change a custom unit, or set the organization's minutes of a seeded unit.",
            input_schema={
                "type": "object",
                "properties": {
                    "key": {"type": "string"},
                    "name": {"type": "string"},
                    "rate_role": {"type": "string"},
                    "minutes": {"type": "array", "items": {"type": "number"}},
                    "review_min": {"type": "number"},
                    "source": {"type": "string"},
                },
                "required": ["key"],
            },
            source=_source(),
        ),
        ToolDefinition(
            name="archive_roi_unit",
            description="Admin: archive a custom ROI unit; its past records keep counting.",
            input_schema={"type": "object", "properties": {"key": {"type": "string"}}, "required": ["key"]},
            source=_source(),
        ),
    ]
    executor = CallableToolExecutor()
    executor.register("list_roi_units", _list_roi_units)
    executor.register("get_roi_declarations", _get_roi_declarations)
    executor.register("set_roi_declaration", _set_roi_declaration)
    executor.register("delete_roi_declaration", _delete_roi_declaration)
    executor.register("get_roi_report", _get_roi_report)
    executor.register("list_roi_records", _list_roi_records)
    executor.register("get_roi_chat_series", _get_roi_chat_series)
    executor.register("adjust_roi_chat_line", _adjust_roi_chat_line)
    executor.register("create_roi_unit", _create_roi_unit)
    executor.register("update_roi_unit", _update_roi_unit)
    executor.register("archive_roi_unit", _archive_roi_unit)
    return definitions, executor
