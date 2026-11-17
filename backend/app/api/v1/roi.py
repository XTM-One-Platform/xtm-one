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

"""ROI API (#8990, slice 3): the value of the agentic work, per agent, per fleet, per organization.

Administrators only: the figures span every agent's and fleet's runs and
their cost. One widening (#9350, decision D1): a fleet's managers and the
group managers of its workspace read that fleet's report, export and records
and review its runs (``services/roi_access.py``). ``GET /api/v1/roi``
answers the report of a window (``7d``, ``30d``, ``90d``, ``all``);
``GET /export?format=csv|pdf`` downloads it (the PDF is the one-page "Value
delivered by XTM One"); ``GET /baselines`` lists the unit catalog with the
organization's overrides, ``PUT`` / ``DELETE /baselines/{unit}`` set or
drop one. The rules are ``services/roi.py``,
``services/roi_records.py`` and ``services/roi_report.py``'s.

Custom units (#9252, ``dev-docs/roi-outcomes.md`` sections 3.1 and 4):
``GET /units`` lists the built-in and the custom units to any signed-in user
(the unit picker reads it); ``POST /units``, ``PUT /units/{key}``, ``POST
/units/{key}/archive`` and ``GET /units/{key}/versions`` are administrators'.
A built-in unit is read-only here (its minutes change through ``/baselines``).

Declarations (#9285, design sections 3.2 and 4): ``GET``, ``PUT`` and
``DELETE /declarations?subject_type=&subject_id=`` read, set or drop the unit
an agent, an assignment or a fleet declares and how it is counted. Whoever
manages the subject (its agent for an assignment) or an administrator.

Outcome review (#9310, design sections 3.3 and 4): ``PUT
/records/{record_id}/outcome`` marks a record accepted, modified or rejected
(or back to unreviewed with ``null``), for whoever may act on its run (its
owner, an administrator for a company-managed run) or owns its work item;
once reviewed, only the reviewer or an administrator changes it. ``GET
/records`` is the administrators' review queue (``outcome=unreviewed``).

Scopes, per-fleet export and rates by role (#9321, design sections 3.4 and
4): ``GET /?scope=agent|fleet|platform&id=`` answers one agent's, one
fleet's or every record's report; ``GET /export?fleet_id=`` exports one
fleet. The PDF title names XTM One only in XTM One mode ("Value delivered by
your agents" in Copilot mode). ``GET /rates``, ``PUT /rates/{role}`` and
``DELETE /rates/{role}?currency=`` are the administrators' hourly rates by
role, a custom role ("legal") included.

ROI everywhere (#9836, slice R1, design 10.2): ``GET /?scope=mine`` is every
user's report over the agents and fleets they can see
(``roi_access.reader_scope``); ``scope=fleet`` also opens to whoever sees the
fleet; ``scope=platform`` opens to a non-administrator while the
organization setting ``roi_org_totals_visible`` is on, its rows outside the
reader's scope folded into one "other" row. ``GET /settings`` (every user)
and ``PUT /settings`` (administrators, audited) read and write that setting.
``GET /summary?subject_type=agent|fleet&ids=`` (slice R6a, design 10.8)
answers the figures of one page of the agent or fleet list in one read.
``GET /suggest?agent_id=`` (or ``?name=&description=`` for an agent not yet
created; slice R8a, design 10.3) answers the catalog unit suggested for a
custom agent by ``services/roi_suggest.py``, a deterministic match, no model.
"""

from __future__ import annotations

import uuid
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import Response
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_user, require_admin
from app.api.v1.platform_settings_helpers import get_platform_settings
from app.core.config import settings
from app.core.database import get_db, release_db_connection_before_loop
from app.core.utils import build_content_disposition
from app.models.user import User
from app.services import roi, roi_access, roi_records, roi_report, roi_subjects, roi_suggest
from app.services.audit import log_audit
from app.services.file_responses import NO_STORE_CACHE_CONTROL

router = APIRouter()

#: The rolling windows plus ``quarter``, the current calendar quarter (#9836 R5a, design 10.7).
_WINDOW_PATTERN = "^(" + "|".join(roi_report.REPORT_WINDOWS) + ")$"
_CURRENCY_PATTERN = "^[A-Za-z]{3}$"


class BaselineOverrideRequest(BaseModel):
    manual_min_low: float = Field(ge=0)
    manual_min_likely: float = Field(ge=0)
    manual_min_high: float = Field(ge=0)
    review_min: float = Field(ge=0)
    source: str = Field(default="", max_length=300)


class UnitRequest(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    role: str = Field(default="", max_length=100)
    manual_min_low: float = Field(ge=0)
    manual_min_likely: float = Field(ge=0)
    manual_min_high: float = Field(ge=0)
    review_min: float = Field(ge=0)
    rate_role: str = Field(default="analyst", max_length=64)
    source: str = Field(default="", max_length=300)


class UnitCreateRequest(UnitRequest):
    key: str = Field(min_length=2, max_length=64, pattern="^[a-z0-9_]+$")


_SUBJECT_PATTERN = "^(" + "|".join(roi_records.SUBJECT_TYPES) + ")$"
_COUNT_MODE_PATTERN = "^(" + "|".join(roi_records.COUNT_MODES) + ")$"


_OUTCOME_PATTERN = "^(" + "|".join(roi.OUTCOMES) + ")$"
_OUTCOME_FILTER_PATTERN = "^(" + "|".join((*roi.OUTCOMES, roi.OUTCOME_UNREVIEWED)) + ")$"


class OutcomeReviewRequest(BaseModel):
    #: ``null`` returns the record to unreviewed.
    outcome: str | None = Field(default=None, pattern=_OUTCOME_PATTERN)
    review_min: float | None = Field(default=None, ge=0)
    note: str | None = Field(default=None, max_length=roi_records.REVIEW_NOTE_MAX)


class RateRequest(BaseModel):
    """The organization's hourly rate of a role in one currency (#9321)."""

    currency: str = Field(..., pattern=_CURRENCY_PATTERN)
    amount: float = Field(..., gt=0, le=100_000)
    source: str = Field("", max_length=300)


class RoiSettingsRequest(BaseModel):
    """The ROI settings to change; a field left out keeps its value."""

    roi_org_totals_visible: bool | None = None
    #: k of the k-anonymity rule (#12077 L5, design 12.5).
    roi_min_group_size: int | None = Field(default=None, ge=2, le=50)
    #: The administrators' "By person" breakdown (D12-1).
    roi_by_person_visible: bool | None = None


_ROI_SETTING_SUMMARIES: dict[str, Any] = {
    "roi_org_totals_visible": lambda v: (
        "Showed the organization-wide ROI totals to every user"
        if v
        else "Showed the organization-wide ROI totals to administrators only"
    ),
    "roi_min_group_size": lambda v: f"Set the ROI anonymity threshold to {v} people",
    "roi_by_person_visible": lambda v: (
        "Showed the ROI By person breakdown to administrators" if v else "Hid the ROI By person breakdown"
    ),
}


def _roi_settings_dict(row: Any) -> dict[str, Any]:
    return {
        "roi_org_totals_visible": bool(row.roi_org_totals_visible),
        "roi_min_group_size": int(row.roi_min_group_size),
        "roi_by_person_visible": bool(row.roi_by_person_visible),
    }


class DeclarationRequest(BaseModel):
    unit: str = Field(min_length=1, max_length=64)
    count_mode: str = Field(pattern=_COUNT_MODE_PATTERN)
    count_path: str | None = Field(default=None, max_length=200)
    units_per_count: int = Field(default=1, ge=1, le=10_000)


def _declaration_dict(
    row: Any, *, default: dict[str, Any] | None = None, names: dict[uuid.UUID, str] | None = None
) -> dict[str, Any]:
    return roi_subjects.declaration_dict(row, default=default, names=names)


async def _manageable_subject(db: AsyncSession, user: User, subject_type: str, subject_id: str) -> uuid.UUID:
    """The subject's id once *user* may manage it: 404 when absent, 403 when not theirs.

    The rule is ``roi_subjects.manageable_subject``'s, shared with the agent
    tools (#10006 S2).
    """
    try:
        return await roi_subjects.manageable_subject(db, user, subject_type, subject_id)
    except roi_subjects.RoiSubjectError as exc:
        raise HTTPException(status_code=exc.status, detail=exc.detail) from None


async def _readable_subject(db: AsyncSession, user: User, subject_type: str, subject_id: str) -> uuid.UUID:
    """The subject's id once *user* may READ it: 404 when absent or not visible.

    Reading follows the visibility of the subject, changing stays with
    :func:`_manageable_subject` (#9836 R3b, UX round 2 B1).
    """
    try:
        return await roi_subjects.readable_subject(db, user, subject_type, subject_id)
    except roi_subjects.RoiSubjectError as exc:
        raise HTTPException(status_code=exc.status, detail=exc.detail) from None


async def _declaration_default(db: AsyncSession, subject_type: str, sid: uuid.UUID) -> dict[str, Any] | None:
    """The declaration a built-in agent ships with, ``None`` for any other subject."""
    return await roi_subjects.declaration_default(db, subject_type, sid)


def _unit_fields(body: UnitRequest) -> dict[str, Any]:
    return {
        "name": body.name,
        "role": body.role,
        "low": body.manual_min_low,
        "likely": body.manual_min_likely,
        "high": body.manual_min_high,
        "review": body.review_min,
        "rate_role": body.rate_role,
        "source": body.source,
    }


def _custom_unit_dict(unit: Any) -> dict[str, Any]:
    return {**roi_records.unit_snapshot(unit), "builtin": False}


def _baseline_dict(
    b: roi.Baseline, overridden: bool, origins: dict[str, dict[str, Any]] | None = None
) -> dict[str, Any]:
    origin = (origins or {}).get(b.unit) or roi_records.origin_fields(None, {}, has_default=True)
    return {
        **origin,
        "default": roi_records.baseline_default(b.unit),
        "unit": b.unit,
        "name": b.name,
        "fleet": b.fleet,
        "role": b.role,
        "rate_role": b.rate_role,
        "manual_min_low": b.manual_min_low,
        "manual_min_likely": b.manual_min_likely,
        "manual_min_high": b.manual_min_high,
        "review_min": b.review_min,
        "minutes_label": b.minutes_label,
        "minutes_source": b.minutes_source,
        "overridden": overridden,
    }


_SCOPE_PATTERN = "^(" + "|".join(roi_report.SCOPES) + ")$"

#: Why a non-administrator is refused an ROI read.
NOT_A_ROI_READER_DETAIL = (
    "Only administrators, the managers of a fleet for that fleet, and the readers of an agent for that agent, read ROI."
)
#: Why a non-administrator is refused the organization-wide report (#9836 R1).
ORG_TOTALS_HIDDEN_DETAIL = (
    "Your administrator shows the organization-wide totals to administrators only. "
    "Your own agents and fleets stay readable."
)


async def _require_roi_reader(
    db: AsyncSession,
    user: User,
    *,
    fleet_id: uuid.UUID | None,
    agent_id: uuid.UUID | None = None,
    fleet_viewers: bool = False,
) -> None:
    """403 unless *user* is an administrator, reads fleet *fleet_id*'s ROI (D1, #9350) or sees agent *agent_id* (#9807).

    Without a fleet or an agent (the platform view, the unscoped records),
    administrators only.
    """
    if user.is_admin:
        return
    if fleet_id is not None and await roi_access.fleet_roi_reader(
        db, user_id=user.id, is_admin=False, fleet_id=fleet_id
    ):
        return
    # The report of a fleet also opens to whoever sees it (#9836 R1); its records and export stay its managers'.
    if (
        fleet_viewers
        and fleet_id is not None
        and await roi_access.fleet_visible(db, user_id=user.id, fleet_id=fleet_id)
    ):
        return
    if agent_id is not None and await roi_access.agent_roi_reader(
        db, user_id=user.id, is_admin=False, agent_id=agent_id
    ):
        return
    raise HTTPException(status_code=403, detail=NOT_A_ROI_READER_DETAIL)


@router.get("")
async def get_roi_report(
    window: str = Query("30d", pattern=_WINDOW_PATTERN),
    currency: str | None = Query(None, pattern=_CURRENCY_PATTERN),
    scope: str = Query(roi_report.SCOPE_PLATFORM, pattern=_SCOPE_PATTERN),
    id: uuid.UUID | None = Query(None, description="The agent or fleet of a scoped report"),
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
) -> dict[str, Any]:
    """The report of *window*: every record (``platform``), one agent's or one fleet's, with their assumptions.

    Administrators read every scope; a fleet's ROI readers and whoever sees
    the fleet its ``fleet`` scope (#9350, #9836); whoever sees an agent its
    ``agent`` scope (#9807); every user ``mine`` (#9836 R1); a
    non-administrator ``platform`` while ``roi_org_totals_visible`` is on,
    the rows outside their scope folded.
    """
    if scope == roi_report.SCOPE_MINE:
        reader = await roi_access.reader_scope(db, user_id=user.id, is_admin=user.is_admin)
        answer = roi_report.report_dict(
            await roi_report.load_report(db, window=window, currency=currency, scope=scope, reader=reader)
        )
        return await _with_ledger_figures(
            answer, db, user, scope=scope, scope_id=None, window=window, currency=currency
        )
    if scope == roi_report.SCOPE_PLATFORM and not user.is_admin:
        if not (await get_platform_settings(db)).roi_org_totals_visible:
            raise HTTPException(status_code=403, detail=ORG_TOTALS_HIDDEN_DETAIL)
        reader = await roi_access.reader_scope(db, user_id=user.id, is_admin=False)
        answer = roi_report.report_dict(
            await roi_report.load_report(db, window=window, currency=currency, scope=scope, reader=reader)
        )
        return await _with_ledger_figures(
            answer, db, user, scope=scope, scope_id=None, window=window, currency=currency
        )
    if scope != roi_report.SCOPE_PLATFORM and id is None:
        raise HTTPException(status_code=422, detail=f"a {scope} report needs id")
    await _require_roi_reader(
        db,
        user,
        fleet_id=id if scope == roi_report.SCOPE_FLEET else None,
        agent_id=id if scope == roi_report.SCOPE_AGENT else None,
        fleet_viewers=True,
    )
    scope_id = id if scope != roi_report.SCOPE_PLATFORM else None
    answer = roi_report.report_dict(
        await roi_report.load_report(db, window=window, currency=currency, scope=scope, scope_id=scope_id)
    )
    return await _with_ledger_figures(
        answer, db, user, scope=scope, scope_id=scope_id, window=window, currency=currency
    )


async def _with_ledger_figures(
    answer: dict[str, Any],
    db: AsyncSession,
    user: User,
    *,
    scope: str,
    scope_id: uuid.UUID | None,
    window: str,
    currency: str | None,
) -> dict[str, Any]:
    """*answer* with ``figures``: the ledger's figures of the same scope for *user* (#12077 slice L4, design 12.8).

    ``figures`` is the one answer shape every surface moves onto (``roi_ledger.figures``, the
    invariant suite's function); the report keys next to it stay until the agent and fleet views
    read ``figures`` (slice L6). ``figures.names`` names the agents, fleets and units its rows key
    (the export's own labels, slice L6a), never a person. The access rule was checked by the caller.
    """
    from app.services import roi_export, roi_ledger

    viewer = await roi_ledger.viewer_for(db, user_id=user.id, is_admin=user.is_admin)
    result = await roi_ledger.figures(
        db, scope=scope, viewer=viewer, scope_id=scope_id, window=window, currency=currency
    )
    await _audit_by_person(db, user, result)
    figures = result.as_dict()
    names, _assumptions = await roi_export.labels(db, figures)
    return {**answer, "figures": {**figures, "names": names}}


async def _audit_by_person(db: AsyncSession, user: User, result: Any) -> None:
    """An answer carrying "By person" writes ``log_audit`` (action read, entity roi_by_person), committed
    before the response (#12077 L5, design 12.5, decision D12-1). Nothing when it carries none.
    """
    if result.by_person is None:
        return
    await log_audit(
        db,
        user=user,
        action="read",
        entity_type="roi_by_person",
        entity_id=str(result.scope_id) if result.scope_id else None,
        entity_name=result.scope,
        summary=f"Read the ROI By person breakdown ({result.scope})",
        details={"scope": result.scope, "rows": len(result.by_person)},
    )
    await db.commit()


@router.get("/summary")
async def get_roi_summary(
    subject_type: str = Query(..., pattern="^(agent|fleet)$"),
    ids: list[uuid.UUID] = Query(..., description="The agents or fleets of one page of a list"),
    window: str = Query("30d", pattern=_WINDOW_PATTERN),
    currency: str | None = Query(None, pattern=_CURRENCY_PATTERN),
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
) -> dict[str, Any]:
    """The figures of each listed agent or fleet over *window*, for the list column (#9836 R6a, design 10.8).

    One read for a page of ids, never one per row. Each id the caller cannot read (outside
    ``roi_access.reader_scope``) is left out of the answer, never refused one by one. Since
    #12077 slice L7a the figures are the ledger's (``roi_ledger.figures_for_subjects``), read as
    the viewer of every other surface, k rule included: each row's ``figures`` equals ``GET
    /roi?scope=agent|fleet&id=`` ``figures`` of that id for the same window (invariant d). An
    id that declares no unit and holds no counted fact answers ``declared: false`` (the list
    shows "-"). Costs follow the observability cost setting: ``null`` with ``cost_masked``.
    """
    if len(ids) > roi_report.SUMMARY_MAX_IDS:
        raise HTTPException(status_code=422, detail=f"at most {roi_report.SUMMARY_MAX_IDS} ids per summary")
    from app.services import roi_ledger

    reader = await roi_access.reader_scope(db, user_id=user.id, is_admin=user.is_admin)
    holds = reader.holds_agent if subject_type == roi_report.SCOPE_AGENT else reader.holds_fleet
    readable = [sid for sid in dict.fromkeys(ids) if holds(sid)]
    viewer = await roi_ledger.viewer_for(db, user_id=user.id, is_admin=user.is_admin)
    used_currency, ledger = await roi_ledger.figures_for_subjects(
        db, subject_type=subject_type, ids=readable, viewer=viewer, window=window, currency=currency
    )
    declared = await roi_report.declared_subjects(db, subject_type, readable)
    details = await roi_report.record_details(db, subject_type, readable, window=window, currency=used_currency)
    return {
        "subject_type": subject_type,
        "window": window,
        "currency": used_currency,
        "cost_masked": not settings.observability_cost_data_enabled,
        "items": [
            _summary_item(sid, ledger.get(sid), declares=sid in declared, records=details.get(sid)) for sid in readable
        ],
    }


#: The keys of a ledger answer one list row carries: the hero, its "How this is computed" lines
#: (By source) and the k rule's trace; the other breakdowns stay on the object's own page.
_SUMMARY_FIGURE_KEYS = ("scope", "currency", "totals", "by_source", "cost_masked", "suppressed")


def _summary_item(
    sid: uuid.UUID, result: Any, *, declares: bool, records: dict[str, Any] | None = None
) -> dict[str, Any]:
    """One list row: ``declared`` false (no figure) when the object declares no unit and its ledger
    totals count nothing (spend alone included, #10441), else the ledger's ``figures`` of the object
    and its ``records`` (``roi_report.record_details``: rejected records, work items and the inputs),
    so the row's "How this is computed" says what the agent page's says (#12077 L7a UX r1 B1)."""
    if result is None:
        return {"id": str(sid), "declared": False}
    answer = result.as_dict()
    totals = answer["totals"]
    counts = totals["runs"] > 0 or totals["deliverables"] > 0 or totals["value"] > 0
    if not (declares or counts):
        return {"id": str(sid), "declared": False}
    row: dict[str, Any] = {
        "id": str(sid),
        "declared": True,
        "figures": {key: answer[key] for key in _SUMMARY_FIGURE_KEYS},
    }
    if records is not None:
        row["records"] = records
    return row


@router.get("/export")
async def export_roi_report(
    format: str = Query("csv", pattern="^(csv|pdf)$"),
    window: str = Query("30d", pattern=_WINDOW_PATTERN),
    currency: str | None = Query(None, pattern=_CURRENCY_PATTERN),
    scope: str | None = Query(None, pattern=_SCOPE_PATTERN),
    id: uuid.UUID | None = Query(None, description="The agent or fleet of a scoped export"),
    fleet_id: uuid.UUID | None = Query(None, description="Export one fleet's figures (scope=fleet&id=)"),
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
) -> Response:
    """The ledger's figures of a scope as CSV, or as the PDF value report (#12077 slice L4c, design 12.8).

    The numbers are ``GET /roi``'s ``figures`` for the same caller, scope and window
    (``roi_ledger.figures``, invariant d), every breakdown with its residual and folded rows.
    Every scope exports for whoever reads it, under the rule of ``GET /roi``: ``mine`` every
    user; ``agent`` and ``fleet`` their readers; ``platform`` administrators, and a
    non-administrator while ``roi_org_totals_visible`` is on. ``fleet_id`` alone reads as
    ``scope=fleet&id=`` (the fleet view's export). The PDF title names XTM One in XTM One
    mode only ("Value delivered by your agents" in Copilot mode).
    """
    from app.services import roi_export, roi_ledger

    if scope is None:
        scope = roi_report.SCOPE_FLEET if fleet_id is not None else roi_report.SCOPE_PLATFORM
    if scope == roi_report.SCOPE_FLEET and id is None:
        id = fleet_id
    scope_id = id if scope in (roi_report.SCOPE_AGENT, roi_report.SCOPE_FLEET) else None
    if scope in (roi_report.SCOPE_AGENT, roi_report.SCOPE_FLEET):
        if scope_id is None:
            raise HTTPException(status_code=422, detail=f"a {scope} export needs id")
        await _require_roi_reader(
            db,
            user,
            fleet_id=scope_id if scope == roi_report.SCOPE_FLEET else None,
            agent_id=scope_id if scope == roi_report.SCOPE_AGENT else None,
            fleet_viewers=True,
        )
    elif scope == roi_report.SCOPE_PLATFORM and not user.is_admin:
        if not (await get_platform_settings(db)).roi_org_totals_visible:
            raise HTTPException(status_code=403, detail=ORG_TOTALS_HIDDEN_DETAIL)
    viewer = await roi_ledger.viewer_for(db, user_id=user.id, is_admin=user.is_admin)
    result = await roi_ledger.figures(
        db, scope=scope, viewer=viewer, scope_id=scope_id, window=window, currency=currency
    )
    await _audit_by_person(db, user, result)
    figures = result.as_dict()
    names, assumptions = await roi_export.labels(db, figures)
    xtm_one = settings.is_xtm_one
    filename = roi_export.export_filename(window, format, xtm_one=xtm_one, scope=scope, scope_id=scope_id)
    headers = {"Content-Disposition": build_content_disposition(filename), "Cache-Control": NO_STORE_CACHE_CONTROL}
    if format == "csv":
        text = roi_export.figures_csv(figures, names=names, assumptions=assumptions)
        return Response(content=text.encode("utf-8"), media_type="text/csv; charset=utf-8", headers=headers)

    from app.services.presentation_export import enforce_export_rate_limit

    await enforce_export_rate_limit(user.id)
    await release_db_connection_before_loop(db)
    try:
        pdf = await roi_export.figures_pdf(
            figures,
            names=names,
            assumptions=assumptions,
            window=window,
            title=roi_report.report_title(xtm_one=xtm_one),
        )
    except Exception as exc:
        raise HTTPException(
            status_code=503, detail="The platform could not render the PDF. Try again later, or export as CSV."
        ) from exc
    return Response(content=pdf, media_type="application/pdf", headers=headers)


@router.get("/settings")
async def get_roi_settings(
    db: AsyncSession = Depends(get_db),
    _user: User = Depends(get_current_user),
) -> dict[str, Any]:
    """The organization's ROI settings, read by every user: they only say what the Value page shows (#9836 R1).

    ``roi_min_group_size`` is the k the captions interpolate (#12077 L5, design 12.5).
    """
    return _roi_settings_dict(await get_platform_settings(db))


@router.put("/settings")
async def put_roi_settings(
    body: RoiSettingsRequest,
    db: AsyncSession = Depends(get_db),
    admin: User = Depends(require_admin),
) -> dict[str, Any]:
    """Set the ROI settings (administrators): the organization totals for every user, the k of
    the anonymity rule and the "By person" breakdown (#12077 L5). One audit entry per field changed.
    """
    row = await get_platform_settings(db)
    for name, value in body.model_dump(exclude_none=True).items():
        before = getattr(row, name)
        if before == value:
            continue
        setattr(row, name, value)
        await log_audit(
            db,
            user=admin,
            action="configure",
            entity_type="roi_settings",
            entity_name=name,
            summary=_ROI_SETTING_SUMMARIES[name](value),
            details={name: value, "previous": before},
        )
    await db.commit()
    return _roi_settings_dict(row)


@router.get("/rates")
async def list_roi_rates(
    db: AsyncSession = Depends(get_db),
    _admin: User = Depends(require_admin),
) -> dict[str, Any]:
    """The organization's hourly rates by role, and the built-in roles they layer over.

    A built-in rate answers ``origin`` "customized" while the organization sets
    its own amount for that role and currency (``DELETE`` resets it).
    """
    rows = await roi_records.list_rates(db)
    names = await roi_records.user_names(db, (r.updated_by_user_id for r in rows))
    customized = {(r.role, r.currency) for r in rows}
    return {
        "rates": [roi_records.rate_dict(r, names) for r in rows],
        "builtin": [
            {
                "role": r.role,
                "currency": r.currency,
                "amount": r.amount,
                "label": r.label,
                "source": r.source,
                "origin": (
                    roi_records.ORIGIN_CUSTOMIZED if (r.role, r.currency) in customized else roi_records.ORIGIN_DEFAULT
                ),
            }
            for by_currency in roi.builtin_rates(xtm_one=settings.is_xtm_one).values()
            for r in by_currency.values()
        ],
    }


@router.put("/rates/{role}")
async def put_roi_rate(
    role: str,
    body: RateRequest,
    db: AsyncSession = Depends(get_db),
    admin: User = Depends(require_admin),
) -> dict[str, Any]:
    """Set the organization's hourly rate of *role* in a currency; 422 names what is wrong."""
    try:
        row = await roi_records.put_rate(
            db, role=role, currency=body.currency, amount=body.amount, source=body.source, user_id=admin.id
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from None
    await log_audit(
        db,
        user=admin,
        action="configure",
        entity_type="roi_rate",
        entity_name=row.role,
        summary=f"Set the ROI rate of {row.role} in {row.currency}",
        details={**body.model_dump(), "role": row.role},
    )
    await db.commit()
    await db.refresh(row)
    return roi_records.rate_dict(row, await roi_records.user_names(db, [admin.id]))


@router.delete("/rates/{role}", status_code=204)
async def delete_roi_rate(
    role: str,
    currency: str = Query(..., pattern=_CURRENCY_PATTERN),
    db: AsyncSession = Depends(get_db),
    admin: User = Depends(require_admin),
) -> Response:
    """Drop the organization's rate of *role* in *currency*; 404 when it has none."""
    if not await roi_records.delete_rate(db, role=role, currency=currency):
        raise HTTPException(status_code=404, detail=f"no rate for role {role} in {currency.upper()}")
    await log_audit(
        db,
        user=admin,
        action="delete",
        entity_type="roi_rate",
        entity_name=role,
        summary=f"Removed the ROI rate of {role} in {currency.upper()}",
        details={"role": role, "currency": currency.upper()},
    )
    await db.commit()
    return Response(status_code=204)


@router.get("/baselines")
async def list_roi_baselines(
    db: AsyncSession = Depends(get_db),
    _admin: User = Depends(require_admin),
) -> list[dict[str, Any]]:
    """The unit catalog with the organization's overrides applied, each figure labelled."""
    baselines, overridden = await roi_records.effective_baselines(db)
    origins = await roi_records.baseline_origins(db)
    return [
        _baseline_dict(baselines[b.unit], b.unit in overridden, origins)
        for b in roi.builtin_baselines(xtm_one=settings.is_xtm_one)
    ]


@router.put("/baselines/{unit}")
async def put_roi_baseline(
    unit: str,
    body: BaselineOverrideRequest,
    db: AsyncSession = Depends(get_db),
    admin: User = Depends(require_admin),
) -> dict[str, Any]:
    """Set the organization's minutes for *unit*; 422 names what is wrong."""
    try:
        await roi_records.upsert_override(
            db,
            unit=unit,
            low=body.manual_min_low,
            likely=body.manual_min_likely,
            high=body.manual_min_high,
            review=body.review_min,
            source=body.source,
            user_id=admin.id,
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from None
    await log_audit(
        db,
        user=admin,
        action="configure",
        entity_type="roi_baseline",
        entity_name=unit,
        summary=f"Set the ROI baseline of {unit}",
        details={**body.model_dump(), "unit": unit},
    )
    await db.commit()
    baselines, overridden = await roi_records.effective_baselines(db)
    return _baseline_dict(baselines[unit], unit in overridden, await roi_records.baseline_origins(db))


@router.get("/units")
async def list_roi_units(
    db: AsyncSession = Depends(get_db),
    _user: User = Depends(get_current_user),
) -> list[dict[str, Any]]:
    """Every unit of work: the built-ins (read-only, overrides applied), then the custom units."""
    baselines, overridden = await roi_records.effective_baselines(db)
    origins = await roi_records.baseline_origins(db)
    built_in = [
        {
            **_baseline_dict(baselines[b.unit], b.unit in overridden, origins),
            "key": b.unit,
            "builtin": True,
            "archived_at": None,
        }
        for b in roi.builtin_baselines(xtm_one=settings.is_xtm_one)
    ]
    return built_in + [_custom_unit_dict(u) for u in await roi_records.list_units(db)]


@router.post("/units", status_code=201)
async def create_roi_unit(
    body: UnitCreateRequest,
    db: AsyncSession = Depends(get_db),
    admin: User = Depends(require_admin),
) -> dict[str, Any]:
    """Add a custom unit; 409 when the key is taken, 422 names a bad field."""
    try:
        unit = await roi_records.create_unit(db, key=body.key, user_id=admin.id, **_unit_fields(body))
    except ValueError as exc:
        status = 409 if "already exists" in str(exc) or "built-in" in str(exc) else 422
        raise HTTPException(status_code=status, detail=str(exc)) from None
    await log_audit(
        db,
        user=admin,
        action="create",
        entity_type="roi_unit",
        entity_name=unit.name,
        summary=f"Added the ROI unit {unit.name}",
        details=body.model_dump(),
    )
    await db.commit()
    return _custom_unit_dict(unit)


@router.put("/units/{key}")
async def update_roi_unit(
    key: str,
    body: UnitRequest,
    db: AsyncSession = Depends(get_db),
    admin: User = Depends(require_admin),
) -> dict[str, Any]:
    """Change a custom unit (a new version); a built-in unit is refused (409)."""
    if key in roi.BASELINES_BY_UNIT:
        raise HTTPException(status_code=409, detail=f"{key!r} is a built-in unit: set its minutes under /baselines.")
    try:
        unit = await roi_records.update_unit(db, key, user_id=admin.id, **_unit_fields(body))
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from None
    if unit is None:
        raise HTTPException(status_code=404, detail=f"No unit {key!r}.")
    await log_audit(
        db,
        user=admin,
        action="update",
        entity_type="roi_unit",
        entity_name=unit.name,
        summary=f"Changed the ROI unit {unit.name} (version {unit.version})",
        details={**body.model_dump(), "key": unit.key},
    )
    await db.commit()
    return _custom_unit_dict(unit)


@router.post("/units/{key}/archive")
async def archive_roi_unit(
    key: str,
    db: AsyncSession = Depends(get_db),
    admin: User = Depends(require_admin),
) -> dict[str, Any]:
    """Archive a custom unit: no longer declarable, its records keep their figures."""
    if key in roi.BASELINES_BY_UNIT:
        raise HTTPException(status_code=409, detail=f"{key!r} is a built-in unit and cannot be archived.")
    unit = await roi_records.archive_unit(db, key, user_id=admin.id)
    if unit is None:
        raise HTTPException(status_code=404, detail=f"No unit {key!r}.")
    await log_audit(
        db,
        user=admin,
        action="update",
        entity_type="roi_unit",
        entity_name=unit.name,
        summary=f"Archived the ROI unit {unit.name}",
        details={"key": unit.key},
    )
    await db.commit()
    return _custom_unit_dict(unit)


@router.get("/units/{key}/versions")
async def list_roi_unit_versions(
    key: str,
    db: AsyncSession = Depends(get_db),
    _admin: User = Depends(require_admin),
) -> list[dict[str, Any]]:
    """Every version of a custom unit, newest first."""
    versions = await roi_records.unit_versions(db, key)
    if not versions:
        raise HTTPException(status_code=404, detail=f"No unit {key!r}.")
    return [
        {
            "version": v.version,
            "snapshot": v.snapshot,
            "user_id": str(v.user_id) if v.user_id else None,
            "created_at": v.created_at.isoformat() if v.created_at else None,
        }
        for v in versions
    ]


@router.get("/suggest")
async def get_roi_suggestion(
    agent_id: str | None = Query(default=None, min_length=1),
    name: str | None = Query(default=None, max_length=200),
    description: str | None = Query(default=None, max_length=4000),
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
) -> dict[str, Any]:
    """The unit of work suggested for a custom agent, ``suggestion`` null when none fits.

    With ``agent_id`` the agent's name, description and output schema are
    read (anyone who can see the agent); without it, the draft ``name`` and
    ``description`` of the create dialog. A built-in agent ships its own
    declaration and gets no suggestion; ``declared`` says whether the agent
    already declares a unit (the page then shows no suggestion). A token match
    over the catalog (``roi_suggest.suggest_unit``): no run, no model call.
    """
    if agent_id is None and not (name or description):
        raise HTTPException(status_code=422, detail="Give agent_id, or a name or description.")
    declared = False
    schema: dict[str, Any] | None = None
    if agent_id is not None:
        from app.models.agent import Agent

        sid = await _readable_subject(db, user, roi_records.SUBJECT_AGENT, agent_id)
        agent = await db.get(Agent, sid)
        if agent is None:
            raise HTTPException(status_code=404, detail=f"No agent {agent_id}.")
        if agent.is_builtin:
            return {"agent_id": str(sid), "declared": True, "suggestion": None}
        name, description, schema = agent.name, agent.description, agent.output_schema
        declared = (
            await roi_records.get_declaration(db, roi_records.SUBJECT_AGENT, sid) is not None
            or roi.unit_declaration(schema) is not None
        )
    catalog = roi_suggest.catalog_of(
        roi.builtin_baselines(xtm_one=settings.is_xtm_one), await roi_records.list_units(db)
    )
    found = roi_suggest.suggest_unit(name, description, schema, catalog)
    return {
        "agent_id": agent_id,
        "declared": declared,
        "suggestion": found.as_dict() if found is not None else None,
    }


@router.get("/declarations")
async def get_roi_declaration(
    subject_type: str = Query(pattern=_SUBJECT_PATTERN),
    subject_id: str = Query(min_length=1),
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
) -> dict[str, Any] | None:
    """The subject's own declaration, ``null`` when it declares none.

    A built-in agent nobody customized answers the declaration it ships with,
    ``origin`` "default" (#9836 R3b, UX round 1 B2): the card shows what the
    agent counts out of the box instead of "Not counted in ROI". Anyone who
    can see the subject reads it (UX round 2 B1); changing it needs manage.
    """
    sid = await _readable_subject(db, user, subject_type, subject_id)
    row = await roi_records.get_declaration(db, subject_type, sid)
    if row is None:
        default = await _declaration_default(db, subject_type, sid)
        if default is None:
            return None
        return {
            **roi_records.origin_fields(None, {}, has_default=True),
            "default": default,
            "subject_type": subject_type,
            "subject_id": str(sid),
            **default,
            "updated_at": None,
        }
    return _declaration_dict(
        row,
        default=await _declaration_default(db, subject_type, sid),
        names=await roi_records.user_names(db, [row.updated_by_user_id]),
    )


@router.get("/declarations/backfill-preview")
async def preview_roi_backfill(
    subject_type: str = Query(pattern=_SUBJECT_PATTERN),
    subject_id: str = Query(min_length=1),
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
) -> dict[str, Any]:
    """What a retroactive pass would write for the subject's current declarations, written nothing (#10006).

    Same rule as changing the declaration (whoever manages the subject or an
    administrator): the preview counts the subject's whole history.
    """
    from app.services import roi_backfill

    sid = await _manageable_subject(db, user, subject_type, subject_id)
    result = await roi_backfill.backfill_subject(db, subject_type=subject_type, subject_id=sid, dry_run=True)
    await db.rollback()
    return result.as_dict()


@router.get("/chat/backfill-preview")
async def preview_roi_chat_backfill(
    db: AsyncSession = Depends(get_db),
    _admin: User = Depends(require_admin),
) -> dict[str, Any]:
    """The dry run of the one-off pass over past chat deliverables (#10544, S3), written nothing.

    Administrators only: it reads every user's decks, reports, videos and documents.
    """
    from app.services import roi_chat_backfill

    result = await roi_chat_backfill.backfill_chat_records(db, dry_run=True)
    await db.rollback()
    return result.as_dict()


@router.post("/chat/backfill")
async def run_roi_chat_backfill(
    db: AsyncSession = Depends(get_db),
    admin: User = Depends(require_admin),
) -> dict[str, Any]:
    """Record every past chat deliverable that has no record yet (#10544, S3; administrators, audited).

    Idempotent: a second call writes nothing. Run the preview first.
    """
    from app.services import roi_chat_backfill

    result = await roi_chat_backfill.backfill_chat_records(db, dry_run=False)
    answer = result.as_dict()
    await log_audit(
        db,
        user=admin,
        action="configure",
        entity_type="roi_settings",
        entity_name="chat_backfill",
        summary=f"Recorded {result.records_created} past chat deliverables for ROI",
        details={"records_created": result.records_created, "totals": answer["totals"]},
    )
    await db.commit()
    return answer


class ChatCorrectionRequest(BaseModel):
    """A person's correction of a chat line, in minutes; ``null`` returns it to the estimate."""

    minutes: float | None = Field(default=None, ge=0)


@router.get("/chat")
async def get_roi_chat_series(
    window: str = Query("30d", pattern=_WINDOW_PATTERN),
    currency: str | None = Query(None, pattern=_CURRENCY_PATTERN),
    agent_id: uuid.UUID | None = None,
    fleet_id: uuid.UUID | None = None,
    mine: bool = False,
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
) -> dict[str, Any]:
    """The "From chat, estimated" series of *window* (#10544, S4), separate from the automation report.

    Administrators read every line; anyone else only the lines attributed to
    them (``roi_chat_series.readable``, Sam 15 Nov 2026). ``mine`` reads the
    caller's own lines (the Value page's "Mine" scope); ``fleet_id`` narrows
    to the lines the ledger attributes to the fleet (the fleet stamped on each
    record, #12077), so the card, the fleet hero and By fleet count one chat.
    A non-administrator's organization read (no ``mine``, no agent, no fleet) answers the organization's totals and
    counts by kind with no line, while ``roi_org_totals_visible`` is on (the
    rule of the automation report): no title, person or agent leaves.
    """
    from app.services import roi_chat_series

    organization_totals = not user.is_admin and not mine and agent_id is None and fleet_id is None
    if organization_totals:
        if not (await get_platform_settings(db)).roi_org_totals_visible:
            raise HTTPException(status_code=403, detail=ORG_TOTALS_HIDDEN_DETAIL)
        reader = roi_chat_series.Reader(user_id=user.id, organization=True)
    else:
        reader = await roi_chat_series.reader_for(db, user_id=user.id, is_admin=user.is_admin and not mine)
    series = await roi_chat_series.load_series(
        db,
        reader=reader,
        window=window,
        currency=currency,
        agent_id=agent_id,
        fleet_id=fleet_id,
        limit=limit,
        offset=offset,
        aggregate_only=organization_totals,
    )
    # The ledger's chat totals of the same scope (#12077 L4b, design 12.8): the figures the chat card
    # shows, the Chat line of By source on the same page (one source of truth).
    from app.services import roi_ledger

    viewer = await roi_ledger.viewer_for(db, user_id=user.id, is_admin=user.is_admin)
    series["figures"] = await roi_ledger.chat_figures(
        db, viewer=viewer, mine=mine, agent_id=agent_id, fleet_id=fleet_id, window=window, currency=currency
    )
    return series


@router.put("/chat/records/{record_id}")
async def put_roi_chat_correction(
    record_id: uuid.UUID,
    body: ChatCorrectionRequest,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
) -> dict[str, Any]:
    """Raise or lower one chat line (#10544, S4; audited). The person of the line or an administrator; anyone else 404."""
    from app.services import roi_chat_series

    row = await roi_chat_series.get_line(db, record_id)
    reader = await roi_chat_series.reader_for(db, user_id=user.id, is_admin=user.is_admin)
    if row is None or not roi_chat_series.readable(reader, agent_id=row.agent_id, user_id=row.user_id):
        raise HTTPException(status_code=404, detail="Chat line not found")
    previous = row.adjusted_min
    try:
        roi_chat_series.apply_correction(row, minutes=body.minutes, user_id=user.id)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from None
    await log_audit(
        db,
        user=user,
        action="update",
        entity_type="roi_chat_record",
        entity_id=str(row.id),
        entity_name=row.title or row.kind,
        summary="Corrected a chat ROI line" if body.minutes is not None else "Returned a chat ROI line to its estimate",
        details={"previous": previous, "minutes": body.minutes, "kind": row.kind},
    )
    await db.commit()
    return {
        "id": str(row.id),
        "adjusted_min": row.adjusted_min,
        "adjusted_at": row.adjusted_at.isoformat() if row.adjusted_at else None,
    }


@router.put("/declarations")
async def put_roi_declaration(
    body: DeclarationRequest,
    subject_type: str = Query(pattern=_SUBJECT_PATTERN),
    subject_id: str = Query(min_length=1),
    backfill: bool = Query(True),
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
) -> dict[str, Any]:
    """Set the unit the subject declares and how it is counted; 422 names a bad field.

    ``backfill`` (default true, #10006) queues a retroactive pass once the
    declaration is committed: the subject's past runs and done work items
    are counted under it; ``backfill.queued`` says whether the pass was queued.
    """
    sid = await _manageable_subject(db, user, subject_type, subject_id)
    try:
        row = await roi_records.put_declaration(
            db,
            subject_type=subject_type,
            subject_id=sid,
            unit=body.unit,
            count_mode=body.count_mode,
            count_path=body.count_path,
            units_per_count=body.units_per_count,
            user_id=user.id,
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from None
    await log_audit(
        db,
        user=user,
        action="configure",
        entity_type="roi_declaration",
        entity_id=sid,
        entity_name=body.unit,
        summary=f"Declared the ROI unit {body.unit} ({body.count_mode}) on the {subject_type}",
        details={**body.model_dump(), "subject_type": subject_type, "backfill": backfill},
    )
    await db.commit()
    queued = False
    if backfill:
        from app.services import roi_backfill

        queued = await roi_backfill.enqueue_backfill(subject_type, sid)
    return {
        **_declaration_dict(
            row,
            default=await _declaration_default(db, subject_type, sid),
            names=await roi_records.user_names(db, [user.id]),
        ),
        "backfill": {"requested": backfill, "queued": queued},
    }


@router.delete("/declarations", status_code=204)
async def delete_roi_declaration(
    subject_type: str = Query(pattern=_SUBJECT_PATTERN),
    subject_id: str = Query(min_length=1),
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
) -> Response:
    """Drop the subject's declaration: resolution falls through to the next level."""
    sid = await _manageable_subject(db, user, subject_type, subject_id)
    if not await roi_records.delete_declaration(db, subject_type, sid):
        raise HTTPException(status_code=404, detail=f"The {subject_type} declares no unit.")
    await log_audit(
        db,
        user=user,
        action="configure",
        entity_type="roi_declaration",
        entity_id=sid,
        entity_name=subject_type,
        summary=f"Removed the ROI declaration of the {subject_type}",
        details={"subject_type": subject_type},
    )
    await db.commit()
    return Response(status_code=204)


async def _require_review_access(db: AsyncSession, user: User, row: Any) -> None:
    """Raise 403/404 unless *user* may review the record *row*.

    A run's record follows the run's own action rule (``require_run_access``);
    a work item's record its owner; beside them, a reader of the record's
    fleet ROI (``roi_access.fleet_roi_reader``, #9350), never on a
    company-managed run; a record whose run or item is gone, an
    administrator only. A reviewed record changes only by its reviewer or an
    administrator.
    """
    from app.api.v1.helpers import require_run_access
    from app.models.assignment import AssignmentRun
    from app.models.work_item import WorkItem

    if user.is_admin:
        return
    # A reader of the record's fleet ROI reviews it too (D1, #9350).
    fleet_reader = row.fleet_id is not None and await roi_access.fleet_roi_reader(
        db, user_id=user.id, is_admin=False, fleet_id=row.fleet_id
    )
    if row.run_id is not None:
        run = await db.get(AssignmentRun, row.run_id)
        if run is None:
            raise HTTPException(status_code=404, detail="Record not found")
        if not fleet_reader or run.is_company_managed:
            require_run_access(run, user, action="review")
    elif row.work_item_id is not None:
        item = await db.get(WorkItem, row.work_item_id)
        if item is None or (item.user_id != user.id and not fleet_reader):
            raise HTTPException(status_code=404, detail="Record not found")
    else:
        raise HTTPException(status_code=404, detail="Record not found")
    if row.reviewed_by_user_id is not None and row.reviewed_by_user_id != user.id:
        raise HTTPException(status_code=403, detail="Only its reviewer or an administrator can change this review.")


@router.put("/records/{record_id}/outcome")
async def put_roi_record_outcome(
    record_id: uuid.UUID,
    body: OutcomeReviewRequest,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
) -> dict[str, Any]:
    """Review a run's or a work item's ROI record: accepted, modified, rejected or ``null``."""
    row = await roi_records.get_record(db, record_id)
    if row is None:
        raise HTTPException(status_code=404, detail="Record not found")
    await _require_review_access(db, user, row)
    previous = row.outcome
    try:
        roi_records.apply_review(row, outcome=body.outcome, review_min=body.review_min, note=body.note, user_id=user.id)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from None
    await log_audit(
        db,
        user=user,
        action="update",
        entity_type="roi_record",
        entity_id=str(row.id),
        entity_name=row.unit,
        summary=f"Reviewed an ROI record as {body.outcome or roi.OUTCOME_UNREVIEWED}",
        details={"previous": previous, **body.model_dump()},
    )
    await db.commit()
    return roi_records.record_dict(row)


def _records_since(window: str | None) -> Any:
    """The start of a report window, ``None`` without one (every record)."""
    return roi_report.window_since(window) if window else None


@router.get("/records")
async def list_roi_records(
    agent_id: uuid.UUID | None = None,
    fleet_id: uuid.UUID | None = None,
    outcome: str | None = Query(default=None, pattern=_OUTCOME_FILTER_PATTERN),
    window: str | None = Query(default=None, pattern=_WINDOW_PATTERN),
    search: str | None = Query(default=None, max_length=200, description="Keep the records whose title contains it"),
    direction: str = Query(default="desc", pattern="^(asc|desc)$"),
    limit: int = Query(50, ge=1, le=500),
    offset: int = Query(0, ge=0),
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
) -> list[dict[str, Any]]:
    """The ROI records, newest first (``direction=asc``: oldest first), runs and work items alike, each with its ``kind`` and ``title``.

    ``outcome=unreviewed`` keeps the records nobody reviewed; ``window`` the
    records of a report window, so the list holds what the report counts
    (#10570). Administrators read every record; a fleet's ROI readers read
    that fleet's (``fleet_id`` named, #9350); whoever reads an agent's ROI
    reads that agent's (``agent_id`` named, #10570).
    """
    await _require_roi_reader(db, user, fleet_id=fleet_id, agent_id=agent_id)
    rows = await roi_records.list_records(
        db,
        agent_id=agent_id,
        fleet_id=fleet_id,
        outcome=outcome,
        since=_records_since(window),
        search=search,
        direction=direction,
        limit=limit,
        offset=offset,
    )
    titles = await roi_records.record_titles(db, rows)
    answer = [{**roi_records.record_dict(r), "title": titles.get(r.id)} for r in rows]
    if not user.is_admin:
        # Another person's id never reaches a non-administrator (Sam, 15 Nov 2026): the reviewer is named to them
        # only when it is them.
        mine = str(user.id)
        for entry in answer:
            if entry["reviewed_by_user_id"] not in (None, mine):
                entry["reviewed_by_user_id"] = None
    return answer


@router.get("/records/count")
async def count_roi_records(
    agent_id: uuid.UUID | None = None,
    fleet_id: uuid.UUID | None = None,
    outcome: str | None = Query(default=None, pattern=_OUTCOME_FILTER_PATTERN),
    window: str | None = Query(default=None, pattern=_WINDOW_PATTERN),
    search: str | None = Query(default=None, max_length=200, description="Keep the records whose title contains it"),
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
) -> dict[str, int]:
    """How many records ``GET /roi/records`` pages through under the same filter (#10570)."""
    await _require_roi_reader(db, user, fleet_id=fleet_id, agent_id=agent_id)
    total = await roi_records.count_records(
        db, agent_id=agent_id, fleet_id=fleet_id, outcome=outcome, since=_records_since(window), search=search
    )
    return {"total": total}


@router.delete("/baselines/{unit}", status_code=204)
async def delete_roi_baseline(
    unit: str,
    db: AsyncSession = Depends(get_db),
    admin: User = Depends(require_admin),
) -> Response:
    """Drop the organization's override of *unit*: the catalog default applies again."""
    if not await roi_records.delete_override(db, unit):
        raise HTTPException(status_code=404, detail=f"No override of {unit!r}.")
    await log_audit(
        db,
        user=admin,
        action="configure",
        entity_type="roi_baseline",
        entity_name=unit,
        summary=f"Reset the ROI baseline of {unit} to the catalog default",
        details={"unit": unit},
    )
    await db.commit()
    return Response(status_code=204)
