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

"""Platform Health page API (#101 S8, ``dev-docs/platform-health.md`` section 8).

Mounted under ``/api/v1/health`` beside the other Platform Health routers. It
reads the engine's finding store (``services/health/engine.py``); nothing here
runs a check, calls a model or meters anything.

Who reads what (section 8, "Access"):

* an administrator reads every finding of every module present in the mode;
* anyone else reads the findings whose subject is an agent they may manage
  (``cycle_access.resolve_target``, the rule the cycles share); module and
  platform findings (XTM One, OpenCTI, OpenAEV, OpenCRQ) are administrators'.

Mode (section 1.1): the agent and fleet modules are generic AI and answer in
every mode; the XTM One, OpenCTI, OpenAEV and OpenCRQ modules exist in the XTM
One modes only, so in a Copilot-like mode they are absent from the overview and
a finding of theirs answers 404.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from typing import Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_user
from app.api.v1.helpers import get_user_group_ids
from app.core.database import get_db
from app.core.docs_links import absolute_docs_link
from app.models.health_check import (
    MODULES,
    SEVERITIES,
    STATUS_OPEN,
    STATUSES,
    HealthCheckFinding,
    HealthScoreDay,
)
from app.models.health_module_analyst import HealthFindingRecommendation
from app.models.user import User
from app.services.health import engine

router = APIRouter()

FINDING_NOT_FOUND = "Finding not found"
#: Modules every mode has (generic AI); the others are XTM One modes only.
GENERIC_MODULES: tuple[str, ...] = ("agents", "fleets")
#: The history windows the page offers (days).
HISTORY_WINDOWS = (7, 30, 90)
#: The most findings one page of the list answers.
MAX_PAGE = 200
#: The modules the Module Analyst investigates (#101 S14, section 12): their cards carry its last cycle.
ANALYST_MODULES: tuple[str, ...] = ("opencti", "openaev", "opencrq")
#: The check id segment of a finding the Module Analyst discovered (``<module>.agentic.<slug>``, section 12.4).
AGENTIC_SEGMENT = ".agentic."


def is_agentic(check_id: str) -> bool:
    """Whether a finding was discovered by the Module Analyst rather than a deterministic check."""
    return AGENTIC_SEGMENT in (check_id or "")


def present_modules() -> tuple[str, ...]:
    """The modules of this mode, in display order (section 1.1)."""
    from app.core.config import settings

    if settings.is_copilot_like:
        return GENERIC_MODULES
    return tuple(MODULES)


class LastInvestigation(BaseModel):
    """The Module Analyst's latest cycle on one of the module's registrations (section 12.7)."""

    cycle_id: str
    registration_id: str
    #: ``open`` | ``completed`` | ``budget_reached`` | ``failed`` | ``cancelled``.
    status: str
    #: ``schedule`` (daily) | ``signal`` (finding set changed) | ``on_demand`` (Investigate now).
    trigger: str
    opened_at: datetime
    closed_at: datetime | None


class ModuleCard(BaseModel):
    module: str
    score: int
    critical: int
    warning: int
    info: int
    #: Score 7 days ago (``None`` when no row exists that day): the card's trend.
    previous_score: int | None
    #: OpenCTI, OpenAEV, OpenCRQ for an administrator: the Module Analyst's last cycle, ``None`` when none.
    last_investigation: LastInvestigation | None = None


class OverviewResponse(BaseModel):
    modules: list[ModuleCard]
    #: The lowest module score (100 when nothing is present).
    overall: int
    #: Whether the reader sees every module (administrator) or only their agents.
    scope: Literal["all", "managed"]


class AnalystRecommendation(BaseModel):
    """One ``health_finding_recommendations`` row, shown beside the check's own recommendation."""

    text: str
    #: ``high`` | ``medium`` | ``low``.
    confidence: str
    #: What it read: ``[{"tool", "object_ids", "values", "read_at"}]``.
    evidence: list[dict[str, Any]]
    cycle_id: str
    created_at: datetime


class FindingResponse(BaseModel):
    id: str
    check_id: str
    module: str
    category: str
    severity: str
    status: str
    subject: dict[str, Any]
    #: The subject's display name when it is an agent the reader may see.
    subject_name: str | None
    evidence: dict[str, Any]
    recommendation: str
    doc_link: str
    first_seen: datetime
    last_seen: datetime
    resolved_at: datetime | None
    dismissal: dict[str, Any] | None
    can_dismiss: bool
    #: Discovered by the Module Analyst (check id ``<module>.agentic.<slug>``), not by a check.
    agentic: bool = False
    #: The Module Analyst's newest recommendation on this finding, ``None`` when none (section 12.7).
    analyst_recommendation: AnalystRecommendation | None = None
    #: An agentic finding from the previous check logic (another build or checker playbook,
    #: or none recorded): a ``deploy`` round re-checks it (section 13.6, #1732).
    stale: bool = False
    #: The build whose check logic produced an agentic finding, ``None`` when not recorded.
    check_build: str | None = None


class FindingListResponse(BaseModel):
    items: list[FindingResponse]
    total: int


class HistoryPoint(BaseModel):
    day: str
    module: str
    score: int
    critical: int
    warning: int
    info: int


class DismissRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    reason: str = Field(min_length=1, max_length=engine.MAX_DISMISSAL_REASON)
    #: Days the dismissal lasts; ``None`` keeps it until the finding resolves.
    days: int | None = Field(default=None, ge=1, le=365)


#: The most findings one bulk call dismisses or restores.
MAX_BULK = 200


class BulkDismissRequest(DismissRequest):
    """Dismiss all: one reason and one duration for every open finding named."""

    ids: list[str] = Field(min_length=1, max_length=MAX_BULK)


class BulkRestoreRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    ids: list[str] = Field(min_length=1, max_length=MAX_BULK)


class BulkResponse(BaseModel):
    #: The findings changed, in the order asked.
    items: list[FindingResponse]
    #: The ids asked that were not changed: unknown, not readable, or not in the right status.
    skipped: list[str]


async def _managed_agents(db: AsyncSession, user: User) -> dict[str, str]:
    """The agents of the findings the reader may manage, by id, with their names (non-admins)."""
    from app.services.health.cycle_access import resolve_target

    ids = {
        str(row)
        for row in (
            await db.execute(
                select(HealthCheckFinding.subject["agent_id"].astext).where(
                    HealthCheckFinding.module.in_(GENERIC_MODULES),
                    HealthCheckFinding.subject.has_key("agent_id"),
                )
            )
        ).scalars()
        if row
    }
    if not ids:
        return {}
    group_ids = await get_user_group_ids(db, user.id)
    managed: dict[str, str] = {}
    for raw in sorted(ids):
        try:
            agent_id = uuid.UUID(raw)
        except ValueError:
            continue
        target = await resolve_target(db, user, group_ids, "agent", agent_id)
        if target is not None and target.can_manage:
            managed[raw] = target.name
    return managed


async def _agent_names(db: AsyncSession, ids: set[str]) -> dict[str, str]:
    from app.models.agent import Agent

    parsed = []
    for raw in ids:
        try:
            parsed.append(uuid.UUID(raw))
        except ValueError:
            continue
    if not parsed:
        return {}
    rows = await db.execute(select(Agent.id, Agent.name).where(Agent.id.in_(parsed)))
    return {str(agent_id): name for agent_id, name in rows.all()}


def _visible_query(user: User, managed: dict[str, str] | None):
    query = select(HealthCheckFinding).where(HealthCheckFinding.module.in_(present_modules()))
    if not user.is_admin:
        query = query.where(
            HealthCheckFinding.module.in_(GENERIC_MODULES),
            HealthCheckFinding.subject["agent_id"].astext.in_(list((managed or {}).keys()) or [""]),
        )
    return query


async def _analyst_recommendations(
    db: AsyncSession, rows: list[HealthCheckFinding]
) -> dict[str, AnalystRecommendation]:
    """The newest Module Analyst recommendation of each module finding of *rows*, by finding id."""
    ids = [row.id for row in rows if row.module in ANALYST_MODULES]
    if not ids:
        return {}
    found = await db.execute(
        select(HealthFindingRecommendation)
        .where(HealthFindingRecommendation.finding_id.in_(ids))
        .order_by(HealthFindingRecommendation.created_at.desc())
    )
    newest: dict[str, AnalystRecommendation] = {}
    for rec in found.scalars():
        if rec.finding_id not in newest:
            newest[rec.finding_id] = AnalystRecommendation(
                text=rec.text,
                confidence=rec.confidence,
                evidence=list(rec.evidence or []),
                cycle_id=str(rec.cycle_id),
                created_at=rec.created_at,
            )
    return newest


async def _last_investigations(db: AsyncSession) -> dict[str, LastInvestigation]:
    """The module checkers' newest cycle per analysed module, over all of the module's registrations."""
    from app.models.health import HealthCycle
    from app.models.platform_registration import PlatformRegistration
    from app.services.health.identity import MODULE_CHECKER_ROLES

    latest: dict[str, LastInvestigation] = {}
    for module in ANALYST_MODULES:
        if module not in present_modules():
            continue
        row = (
            await db.execute(
                select(HealthCycle)
                .join(PlatformRegistration, PlatformRegistration.id == HealthCycle.target_id)
                .where(
                    HealthCycle.target_type == "module",
                    HealthCycle.specialist.in_(sorted(MODULE_CHECKER_ROLES)),
                    PlatformRegistration.platform_identifier == module,
                )
                .order_by(HealthCycle.opened_at.desc())
                .limit(1)
            )
        ).scalar_one_or_none()
        if row is not None:
            latest[module] = LastInvestigation(
                cycle_id=str(row.id),
                registration_id=str(row.target_id),
                status=row.status,
                trigger=row.trigger,
                opened_at=row.opened_at,
                closed_at=row.closed_at,
            )
    return latest


def _response(
    row: HealthCheckFinding,
    names: dict[str, str],
    *,
    can_dismiss: bool,
    analyst: dict[str, AnalystRecommendation] | None = None,
) -> FindingResponse:
    from app.services.health.check_reports import finding_check_logic, is_stale

    agent_id = (row.subject or {}).get("agent_id")
    return FindingResponse(
        id=row.id,
        check_id=row.check_id,
        module=row.module,
        category=row.category,
        severity=row.severity,
        status=row.status,
        subject=dict(row.subject or {}),
        subject_name=names.get(str(agent_id)) if agent_id else None,
        evidence=dict(row.evidence or {}),
        recommendation=row.recommendation or "",
        doc_link=absolute_docs_link(row.doc_link or ""),
        first_seen=row.first_seen,
        last_seen=row.last_seen,
        resolved_at=row.resolved_at,
        dismissal=row.dismissal,
        can_dismiss=can_dismiss and row.status == STATUS_OPEN,
        agentic=is_agentic(row.check_id),
        analyst_recommendation=(analyst or {}).get(row.id),
        stale=row.status == STATUS_OPEN and is_stale(row.module, row.evidence),
        check_build=(finding_check_logic(row.evidence) or {}).get("build"),
    )


async def _scope(db: AsyncSession, user: User) -> dict[str, str] | None:
    return None if user.is_admin else await _managed_agents(db, user)


@router.get("/overview", response_model=OverviewResponse)
async def health_overview(user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    """One card per module present: score, open counts by severity, the score a week ago."""
    managed = await _scope(db, user)
    modules = present_modules() if user.is_admin else GENERIC_MODULES
    counts = {module: dict.fromkeys(SEVERITIES, 0) for module in modules}
    rows = (await db.execute(_visible_query(user, managed).where(HealthCheckFinding.status == STATUS_OPEN))).scalars()
    for row in rows:
        if row.module in counts:
            counts[row.module][row.severity] += 1
    week_ago = datetime.now(UTC).date() - timedelta(days=7)
    previous: dict[str, int] = {}
    investigations: dict[str, LastInvestigation] = {}
    if user.is_admin:
        history = await db.execute(select(HealthScoreDay).where(HealthScoreDay.day == week_ago))
        previous = {day.module: day.score for day in history.scalars()}
        investigations = await _last_investigations(db)
    cards = [
        ModuleCard(
            module=module,
            score=engine.score_from_counts(c["critical"], c["warning"], c["info"]),
            previous_score=previous.get(module),
            last_investigation=investigations.get(module),
            **c,
        )
        for module, c in counts.items()
    ]
    return OverviewResponse(
        modules=cards,
        overall=min((card.score for card in cards), default=100),
        scope="all" if user.is_admin else "managed",
    )


@router.get("/findings", response_model=FindingListResponse)
async def list_health_findings(
    module: list[str] | None = Query(default=None),
    severity: list[str] | None = Query(default=None),
    category: list[str] | None = Query(default=None),
    finding_status: list[str] | None = Query(default=None, alias="status"),
    agentic: bool | None = Query(default=None),
    limit: int = Query(default=50, ge=1, le=MAX_PAGE),
    offset: int = Query(default=0, ge=0),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """The findings the reader may see, most severe and most recent first; open ones by default."""
    managed = await _scope(db, user)
    query = _visible_query(user, managed)
    if module:
        query = query.where(HealthCheckFinding.module.in_(module))
    if severity:
        query = query.where(HealthCheckFinding.severity.in_(severity))
    if category:
        query = query.where(HealthCheckFinding.category.in_(category))
    statuses = [s for s in (finding_status or [STATUS_OPEN]) if s in STATUSES]
    query = query.where(HealthCheckFinding.status.in_(statuses or [STATUS_OPEN]))
    if agentic is not None:
        pattern = f"%{AGENTIC_SEGMENT}%"
        check = HealthCheckFinding.check_id.like(pattern)
        query = query.where(check if agentic else ~check)
    rows = list((await db.execute(query)).scalars())
    rank = {s: i for i, s in enumerate(SEVERITIES)}
    rows.sort(key=lambda r: (rank.get(r.severity, 9), -r.last_seen.timestamp(), r.id))
    page = rows[offset : offset + limit]
    names = (
        managed
        if managed is not None
        else await _agent_names(
            db, {str((r.subject or {}).get("agent_id")) for r in page if (r.subject or {}).get("agent_id")}
        )
    )
    analyst = await _analyst_recommendations(db, page)
    return FindingListResponse(
        items=[_response(r, names, can_dismiss=True, analyst=analyst) for r in page],
        total=len(rows),
    )


async def _readable_finding(db: AsyncSession, user: User, finding_id: str) -> tuple[HealthCheckFinding, dict[str, str]]:
    row = await db.get(HealthCheckFinding, finding_id[:64])
    if row is None or row.module not in present_modules():
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=FINDING_NOT_FOUND)
    agent_id = str((row.subject or {}).get("agent_id") or "")
    if user.is_admin:
        return row, await _agent_names(db, {agent_id} if agent_id else set())
    managed = await _managed_agents(db, user)
    if row.module not in GENERIC_MODULES or agent_id not in managed:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=FINDING_NOT_FOUND)
    return row, managed


@router.get("/findings/{finding_id}", response_model=FindingResponse)
async def get_health_finding(
    finding_id: str, user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)
):
    row, names = await _readable_finding(db, user, finding_id)
    return _response(row, names, can_dismiss=True, analyst=await _analyst_recommendations(db, [row]))


@router.post("/findings/{finding_id}/dismiss", response_model=FindingResponse)
async def dismiss_health_finding(
    finding_id: str,
    body: DismissRequest,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Dismiss an open finding with a reason, for ``days`` or until it resolves (audited)."""
    row, names = await _readable_finding(db, user, finding_id)
    if row.status != STATUS_OPEN:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Only an open finding can be dismissed.")
    now = datetime.now(UTC)
    try:
        row = await engine.dismiss_finding(
            db,
            row.id,
            reason=body.reason,
            by=str(user.id),
            by_name=user.full_name or "",
            now=now,
            until=now + timedelta(days=body.days) if body.days else None,
        )
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)) from None
    await _audit(db, user, "dismiss", row, body.days)
    await db.commit()
    return _response(row, names, can_dismiss=True)


async def _audit(db: AsyncSession, user: User, action: str, row: HealthCheckFinding, days: int | None = None) -> None:
    from app.services.audit import log_audit

    verb = "Dismissed" if action == "dismiss" else "Restored"
    details: dict[str, Any] = {"finding_id": row.id, "module": row.module}
    if action == "dismiss":
        details["days"] = days
    await log_audit(
        db,
        user=user,
        action=action,
        entity_type="health_finding",
        entity_name=row.check_id,
        summary=f"{verb} Platform Health finding {row.check_id}",
        # The finding id is a sha256 key, not a UUID: it travels in the details.
        details=details,
    )


@router.post("/findings/{finding_id}/restore", response_model=FindingResponse)
async def restore_health_finding(
    finding_id: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Bring a dismissed finding back to the open list (audited)."""
    row, names = await _readable_finding(db, user, finding_id)
    try:
        row = await engine.restore_finding(db, row.id)
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from None
    await _audit(db, user, "restore", row)
    await db.commit()
    return _response(row, names, can_dismiss=True)


async def _readable_rows(
    db: AsyncSession, user: User, ids: list[str]
) -> tuple[list[HealthCheckFinding], dict[str, str], list[str]]:
    """The findings of ``ids`` the reader may act on, their agent names, and the ids skipped (order kept)."""
    # The reader's scope is read once for the whole batch (the rule of _readable_finding).
    managed = await _scope(db, user)
    rows: list[HealthCheckFinding] = []
    skipped: list[str] = []
    for finding_id in dict.fromkeys(ids):
        row = await db.get(HealthCheckFinding, finding_id[:64])
        if row is None or row.module not in present_modules():
            skipped.append(finding_id)
            continue
        agent_id = str((row.subject or {}).get("agent_id") or "")
        if managed is not None and (row.module not in GENERIC_MODULES or agent_id not in managed):
            skipped.append(finding_id)
            continue
        rows.append(row)
    if managed is not None:
        return rows, managed, skipped
    agent_ids = {str((row.subject or {}).get("agent_id")) for row in rows if (row.subject or {}).get("agent_id")}
    return rows, await _agent_names(db, agent_ids), skipped


@router.post("/findings/dismiss", response_model=BulkResponse)
async def dismiss_health_findings(
    body: BulkDismissRequest,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Dismiss all: every open finding named, with one reason and one duration (one audit row each)."""
    rows, names, skipped = await _readable_rows(db, user, body.ids)
    now = datetime.now(UTC)
    until = now + timedelta(days=body.days) if body.days else None
    done: list[FindingResponse] = []
    for row in rows:
        if row.status != STATUS_OPEN:
            skipped.append(row.id)
            continue
        try:
            row = await engine.dismiss_finding(
                db, row.id, reason=body.reason, by=str(user.id), by_name=user.full_name or "", now=now, until=until
            )
        except ValueError as exc:
            raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)) from None
        await _audit(db, user, "dismiss", row, body.days)
        done.append(_response(row, names, can_dismiss=True))
    await db.commit()
    return BulkResponse(items=done, skipped=skipped)


@router.post("/findings/restore", response_model=BulkResponse)
async def restore_health_findings(
    body: BulkRestoreRequest,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Restore all: every dismissed finding named goes back to the open list (one audit row each)."""
    rows, names, skipped = await _readable_rows(db, user, body.ids)
    done: list[FindingResponse] = []
    for row in rows:
        try:
            row = await engine.restore_finding(db, row.id)
        except ValueError:
            skipped.append(row.id)
            continue
        await _audit(db, user, "restore", row)
        done.append(_response(row, names, can_dismiss=True))
    await db.commit()
    return BulkResponse(items=done, skipped=skipped)


@router.get("/history", response_model=list[HistoryPoint])
async def health_history(
    days: int = Query(default=30),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Daily module scores over 7, 30 or 90 days (administrators; others get their scope's nothing)."""
    if days not in HISTORY_WINDOWS:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="days is 7, 30 or 90")
    if not user.is_admin:
        return []
    since = datetime.now(UTC).date() - timedelta(days=days)
    rows = await db.execute(
        select(HealthScoreDay)
        .where(HealthScoreDay.day >= since, HealthScoreDay.module.in_(present_modules()))
        .order_by(HealthScoreDay.day, HealthScoreDay.module)
    )
    return [
        HistoryPoint(
            day=row.day.isoformat(),
            module=row.module,
            score=row.score,
            critical=row.critical,
            warning=row.warning,
            info=row.info,
        )
        for row in rows.scalars()
    ]
