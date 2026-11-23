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

"""Platform Health cycles, signals and the Improvements inbox (#8261).

Mounted under ``/api/v1/health`` beside level 1's routes, built in with no
feature flag. Access is ``services/health/cycle_access.py``, the rule
the orchestrator's tools share: a person reads the cycles of the targets they
manage (administrators: all); "Improve with Platform Health" needs the same right.
Approve / Reject stay level 3's ``/api/v1/improvement-proposals`` routes.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal
from typing import Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from pydantic import BaseModel, ConfigDict
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_user, require_admin, require_agentic_ai
from app.api.v1.helpers import get_user_group_ids, parse_path_id
from app.core.database import get_db
from app.models.health import HealthCycle
from app.models.health_cycle import HealthCycleRun, HealthScheduleMark
from app.models.user import User
from app.services.health.cycle_access import (
    TARGET_NOT_FOUND,
    evidence_links,
    may_read_cycle,
    may_read_cycle_narrative,
    readable_proposals,
    resolve_target,
)
from app.services.health.naming import load_health_agent_name

router = APIRouter()

CYCLE_NOT_FOUND = "Cycle not found"


class CycleRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    target_type: Literal["agent", "flow"]
    target_id: uuid.UUID


class CycleStartResponse(BaseModel):
    cycle_id: uuid.UUID
    run_id: uuid.UUID


class ModuleInvestigationStart(BaseModel):
    """What "Investigate now" did on one registration of the module (section 12.7)."""

    registration_id: uuid.UUID
    cycle_id: uuid.UUID | None
    run_id: uuid.UUID | None
    #: ``None`` when the cycle started, else ``start_cycle``'s refusal code (``cycle_open``, budget, ...).
    refusal: str | None
    #: With ``cycle_open``: the cycle already investigating it.
    open_cycle_id: uuid.UUID | None = None


class ModuleInvestigationResponse(BaseModel):
    module: str
    items: list[ModuleInvestigationStart]


class ModuleCheckReport(BaseModel):
    """The latest check report of one registration of the module (#101 S15c, section 13.6)."""

    report_id: uuid.UUID
    registration_id: uuid.UUID
    cycle_id: uuid.UUID
    started_at: datetime | None
    finished_at: datetime
    passed: int
    warned: int
    failed: int
    not_run: int
    #: ``[{"id", "verdict", "category", "evidence", "recommendation", "reason", "finding_id"}]``.
    checks: list[dict[str, Any]]


class ModuleCheckReportsResponse(BaseModel):
    module: str
    items: list[ModuleCheckReport]
    #: Whether the checks have a usable "Run checks as" user (12.3): ``ok``, ``not_set`` (none
    #: configured) or ``no_access`` (the user cannot read this module). The card shows one
    #: state for the last two instead of a "not run" line per check.
    run_as: Literal["ok", "not_set", "no_access"] = "ok"


class CycleResponse(BaseModel):
    id: uuid.UUID
    specialist: str
    trigger: str
    target_type: str | None
    target_id: uuid.UUID | None
    status: str
    stop_reason: str | None
    opened_at: datetime
    closed_at: datetime | None
    spent_tokens: int
    spent_cost_usd: Decimal
    run_id: uuid.UUID | None
    signal: dict | None
    digest: dict | None
    requested_by_me: bool
    #: The target's name: a module cycle's registration title (13.8), an agent's, flow's or
    #: assignment's name; ``None`` when the target no longer exists.
    target_name: str | None = None


class SignalResponse(BaseModel):
    target_type: str
    target_id: uuid.UUID
    #: The target's name (the agent, flow or assignment); ``None`` when it no longer exists.
    target_name: str | None = None
    kind: str
    active: bool
    last_value: dict | None
    fired_at: datetime | None
    cycle_id: uuid.UUID | None
    refusal: str | None
    evaluated_at: datetime


class ScheduleMarkResponse(BaseModel):
    schedule: str
    fired_at: datetime
    cycle_id: uuid.UUID | None
    refusal: str | None


class EvidenceLinkResponse(BaseModel):
    kind: str
    id: str | None
    href: str | None
    hidden: bool


class ImprovementListItem(BaseModel):
    id: uuid.UUID
    proposal_type: str
    target_type: str
    target_id: uuid.UUID
    agent_id: uuid.UUID | None
    status: str
    summary: str | None
    experiment_verdict: str | None
    experiment: dict | None
    created_at: datetime
    can_decide: bool


class ImprovementListResponse(BaseModel):
    items: list[ImprovementListItem]
    total: int


def _cycle_response(
    cycle: HealthCycle,
    record: HealthCycleRun | None,
    user: User,
    *,
    narrative_readable: bool,
    target_name: str | None = None,
) -> CycleResponse:
    digest = record.digest if record else None
    if digest and not narrative_readable:
        digest = {**digest, "narrative": None}
    return CycleResponse(
        id=cycle.id,
        specialist=cycle.specialist,
        trigger=cycle.trigger,
        target_type=cycle.target_type,
        target_id=cycle.target_id,
        status=cycle.status,
        stop_reason=cycle.stop_reason,
        opened_at=cycle.opened_at,
        closed_at=cycle.closed_at,
        spent_tokens=int(cycle.spent_tokens),
        spent_cost_usd=cycle.spent_cost_usd,
        run_id=record.run_id if record else None,
        signal=record.signal if record else None,
        digest=digest,
        requested_by_me=bool(record and record.requested_by_user_id == user.id),
        target_name=target_name,
    )


async def _require_target_reader(
    db: AsyncSession, user: User, group_ids: list[str], target_type: str | None, target_id: uuid.UUID | None
) -> None:
    """A non-admin lists one target's cycles and signals, one they manage; else the target's 404."""
    if user.is_admin:
        return
    if (
        target_type is None
        or target_id is None
        or not await may_read_cycle(db, user, group_ids, target_type, target_id)
    ):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=TARGET_NOT_FOUND)


@router.post("/cycles", response_model=CycleStartResponse, status_code=status.HTTP_202_ACCEPTED)
async def request_cycle(
    body: CycleRequest,
    request: Request,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    # Agentic work: refused while AI is off or unlicensed, before any cycle opens.
    _agentic_gate: None = Depends(require_agentic_ai),
):
    """ "Improve with Platform Health": an investigation of one agent or flow the caller manages."""
    from app.services.audit import log_audit
    from app.services.health.cycles import start_cycle

    group_ids = await get_user_group_ids(db, user.id)
    target = await resolve_target(db, user, group_ids, body.target_type, body.target_id)
    if target is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=TARGET_NOT_FOUND)
    if not target.can_manage:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN, detail="Only who may manage it can ask Platform Health."
        )
    result = await start_cycle(
        db,
        key="investigation",
        trigger="on_demand",
        target_type=body.target_type,
        target_id=body.target_id,
        requested_by_user_id=user.id,
    )
    if not result.started:
        detail: dict[str, Any] = {"code": result.refusal, "message": result.message(await load_health_agent_name(db))}
        if result.open_cycle_id is not None and await may_read_cycle(
            db, user, group_ids, body.target_type, body.target_id
        ):
            detail["open_cycle_id"] = str(result.open_cycle_id)
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=detail)
    await log_audit(
        db,
        user=user,
        action="trigger",
        entity_type="health_cycle",
        entity_id=result.cycle_id,
        entity_name=target.name,
        summary=f"Asked Platform Health to improve the {body.target_type} '{target.name}'",
        details={"target_type": body.target_type, "target_id": str(body.target_id)},
        request=request,
    )
    await db.commit()
    return CycleStartResponse(cycle_id=result.cycle_id, run_id=result.run_id)


@router.post(
    "/modules/{module}/investigate",
    response_model=ModuleInvestigationResponse,
    status_code=status.HTTP_202_ACCEPTED,
)
async def investigate_module(
    module: str,
    request: Request,
    user: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
    # Agentic work: refused while AI is off or unlicensed, before any cycle opens.
    _agentic_gate: None = Depends(require_agentic_ai),
) -> ModuleInvestigationResponse:
    """ "Run checks now": the module checker's round on every active registration of *module* (13.2, 13.8).

    Administrators, XTM One mode, Platform Health and the Module Analyst on; the
    cycle is the analyst's own (its quota exemption and ceiling apply), a
    registration already under investigation answers its open cycle.
    """
    from app.api.v1.platform_health_findings import ANALYST_MODULES, present_modules
    from app.services.audit import log_audit
    from app.services.health.cycles import start_cycle
    from app.services.health.fleet_seed import CHECKS_KEY_BY_PROVIDER
    from app.services.health.module_analyst import active_registrations
    from app.services.health.settings import load_health_settings

    if module not in ANALYST_MODULES or module not in present_modules():
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Unknown health module")
    health = await load_health_settings(db)
    if not health.enabled or not health.module_analyst.enabled:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={"code": "module_analyst_off", "message": "The Module Analyst is switched off."},
        )
    registrations = await active_registrations(db, module, health.module_analyst.excluded_registration_ids)
    if not registrations:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={"code": "no_registration", "message": "No active registration of this module to check."},
        )
    items: list[ModuleInvestigationStart] = []
    for registration_id in registrations:
        result = await start_cycle(
            db,
            key=CHECKS_KEY_BY_PROVIDER[module],
            trigger="on_demand",
            target_type="module",
            target_id=registration_id,
            requested_by_user_id=user.id,
            payload={"module_trigger": "on_demand"},
        )
        items.append(
            ModuleInvestigationStart(
                registration_id=registration_id,
                cycle_id=result.cycle_id,
                run_id=result.run_id,
                refusal=result.refusal,
                open_cycle_id=result.open_cycle_id,
            )
        )
        if result.started:
            await log_audit(
                db,
                user=user,
                action="trigger",
                entity_type="health_cycle",
                entity_id=result.cycle_id,
                entity_name=module,
                summary=f"Asked Platform Health to run the {module} checks",
                details={"target_type": "module", "target_id": str(registration_id)},
                request=request,
            )
    await db.commit()
    return ModuleInvestigationResponse(module=module, items=items)


@router.get("/modules/{module}/checks", response_model=ModuleCheckReportsResponse)
async def list_module_check_reports(
    module: str,
    _admin: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
) -> ModuleCheckReportsResponse:
    """The latest check report per registration of *module* (section 13.6, administrators).

    Stored rows only, no model: readable with AI off. A registration whose checker never
    stored a report is absent; a module of another mode answers 404.
    """
    from app.api.v1.platform_health_findings import ANALYST_MODULES, present_modules
    from app.models.health_module_analyst import HealthModuleCheckReport
    from app.services.health.run_as import run_as_state

    if module not in ANALYST_MODULES or module not in present_modules():
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Unknown health module")
    run_as = await run_as_state(db, module)
    rows = (
        await db.execute(
            select(HealthModuleCheckReport)
            .where(HealthModuleCheckReport.module == module)
            .order_by(HealthModuleCheckReport.registration_id, HealthModuleCheckReport.finished_at.desc())
            .distinct(HealthModuleCheckReport.registration_id)
        )
    ).scalars()
    return ModuleCheckReportsResponse(
        module=module,
        run_as=run_as,
        items=[
            ModuleCheckReport(
                report_id=row.id,
                registration_id=row.registration_id,
                cycle_id=row.cycle_id,
                started_at=row.started_at,
                finished_at=row.finished_at,
                passed=row.passed,
                warned=row.warned,
                failed=row.failed,
                not_run=row.not_run,
                checks=list(row.checks or []),
            )
            for row in rows
        ],
    )


@router.get("/cycles", response_model=list[CycleResponse])
async def list_cycles(
    target_type: Literal["agent", "flow", "assignment"] | None = None,
    target_id: uuid.UUID | None = None,
    cycle_status: str | None = Query(None, alias="status", max_length=16),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0, le=100_000),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """The cycles the caller may read, newest first: all for an administrator, one managed target's otherwise."""
    group_ids = await get_user_group_ids(db, user.id)
    await _require_target_reader(db, user, group_ids, target_type, target_id)
    query = select(HealthCycle, HealthCycleRun).outerjoin(HealthCycleRun, HealthCycleRun.cycle_id == HealthCycle.id)
    if target_id is not None:
        query = query.where(HealthCycle.target_type == target_type, HealthCycle.target_id == target_id)
    if cycle_status:
        query = query.where(HealthCycle.status == cycle_status)
    rows = (await db.execute(query.order_by(HealthCycle.opened_at.desc()).limit(limit).offset(offset))).all()
    # A non-admin lists one target's cycles (``_require_target_reader``): one check covers every row.
    readable = await may_read_cycle_narrative(db, user, group_ids, target_type, target_id)
    names = await _target_names(db, [cycle for cycle, _ in rows])
    return [
        _cycle_response(
            cycle,
            record,
            user,
            narrative_readable=readable,
            target_name=names.get((cycle.target_type, cycle.target_id)),
        )
        for cycle, record in rows
    ]


@router.get("/cycles/{cycle_id}", response_model=CycleResponse)
async def get_cycle(cycle_id: str, user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    cid = parse_path_id(cycle_id, not_found_detail=CYCLE_NOT_FOUND)
    row = (
        await db.execute(
            select(HealthCycle, HealthCycleRun)
            .outerjoin(HealthCycleRun, HealthCycleRun.cycle_id == HealthCycle.id)
            .where(HealthCycle.id == cid)
        )
    ).first()
    group_ids = await get_user_group_ids(db, user.id)
    if row is None or not await may_read_cycle(db, user, group_ids, row[0].target_type, row[0].target_id):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=CYCLE_NOT_FOUND)
    readable = await may_read_cycle_narrative(db, user, group_ids, row[0].target_type, row[0].target_id)
    names = await _target_names(db, [row[0]])
    return _cycle_response(
        row[0], row[1], user, narrative_readable=readable, target_name=names.get((row[0].target_type, row[0].target_id))
    )


async def _target_names(db: AsyncSession, states: list) -> dict[tuple[str, uuid.UUID], str]:
    """The name of every signal's or cycle's target, one query per kind; a deleted target is absent.

    A module cycle's target is a platform registration, named by its title (13.8). The
    caller already reads these targets (``_require_target_reader``): an administrator
    lists every signal and cycle, anyone else one target they manage.
    """
    from app.models.agent import Agent
    from app.models.assignment import Assignment
    from app.models.flow import Flow
    from app.models.platform_registration import PlatformRegistration

    columns = {
        "agent": (Agent.id, Agent.name),
        "flow": (Flow.id, Flow.name),
        "assignment": (Assignment.id, Assignment.name),
        "module": (PlatformRegistration.id, PlatformRegistration.platform_title),
    }
    names: dict[tuple[str, uuid.UUID], str] = {}
    for kind, (id_column, name_column) in columns.items():
        ids = {s.target_id for s in states if s.target_type == kind and s.target_id is not None}
        if not ids:
            continue
        rows = await db.execute(select(id_column, name_column).where(id_column.in_(ids)))
        names.update({(kind, row_id): name for row_id, name in rows.all()})
    return names


@router.get("/signals", response_model=list[SignalResponse])
async def list_signals(
    target_type: Literal["agent", "flow", "assignment"] | None = None,
    target_id: uuid.UUID | None = None,
    active_only: bool = True,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Signal states: all for an administrator, one managed target's otherwise."""
    from app.services.health.signals import list_signal_states

    group_ids = await get_user_group_ids(db, user.id)
    await _require_target_reader(db, user, group_ids, target_type, target_id)
    states = await list_signal_states(db, active_only=active_only, target_type=target_type, target_id=target_id)
    names = await _target_names(db, states)
    return [
        SignalResponse(
            target_type=s.target_type,
            target_id=s.target_id,
            target_name=names.get((s.target_type, s.target_id)),
            kind=s.kind,
            active=s.active,
            last_value=s.last_value,
            fired_at=s.fired_at,
            cycle_id=s.cycle_id,
            refusal=s.refusal,
            evaluated_at=s.evaluated_at,
        )
        for s in states
    ]


@router.get("/schedules", response_model=list[ScheduleMarkResponse], dependencies=[Depends(require_admin)])
async def list_schedule_marks(db: AsyncSession = Depends(get_db)):
    """When each schedule and signal pass last fired, and what came of it (Settings > Agents and automations > Platform Health)."""
    rows = (await db.execute(select(HealthScheduleMark).order_by(HealthScheduleMark.schedule))).scalars()
    return [
        ScheduleMarkResponse(schedule=m.schedule, fired_at=m.fired_at, cycle_id=m.cycle_id, refusal=m.refusal)
        for m in rows
    ]


#: Proposals read per batch before the per-row read rule.
_IMPROVEMENTS_SCAN = 500


@router.get("/improvements", response_model=ImprovementListResponse)
async def list_improvements(
    proposal_type: list[str] | None = Query(None, max_length=8, description="Repeat to keep several types."),
    proposal_status: list[str] | None = Query(
        None, alias="status", max_length=8, description="Repeat to keep several statuses."
    ),
    target_type: Literal["agent", "flow", "skill", "assignment"] | None = None,
    target_id: uuid.UUID | None = None,
    agent_id: uuid.UUID | None = Query(None, description="Every proposal on this agent, its skills and assignments."),
    q: str | None = Query(None, max_length=200, description="Words of the proposal's summary (case-insensitive)."),
    sort: Literal["date", "type", "status"] = Query(
        "date", description="Order: creation date, proposal type or status"
    ),
    direction: Literal["asc", "desc"] = Query("desc", description="Order direction; newest first breaks ties"),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0, le=100_000),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Proposals across targets, newest first, each kept only if the caller may read its target.

    The cross-target views level 3's per-target list has no room for: every
    proposal (administrators' Platform Health page) and an agent's with its skills'
    and assignments'. A non-administrator names an agent or a target. The diff
    and Approve / Reject are level 3's ``/improvement-proposals/{id}``.
    """
    from app.models.improvement_proposal import ImprovementProposal
    from app.services import improvement_proposals as proposals

    if not user.is_admin and agent_id is None and target_id is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=TARGET_NOT_FOUND)
    if (target_type is None) != (target_id is None):
        raise HTTPException(status_code=422, detail="target_type and target_id go together")
    from app.api.v1.users import escape_like

    query = select(ImprovementProposal).where(proposals.surface_filter())
    types = [value for value in proposal_type or [] if value]
    statuses = [value for value in proposal_status or [] if value]
    if types:
        query = query.where(ImprovementProposal.proposal_type.in_(types))
    if statuses:
        query = query.where(ImprovementProposal.status.in_(statuses))
    if target_id is not None:
        query = query.where(ImprovementProposal.target_type == target_type, ImprovementProposal.target_id == target_id)
    if agent_id is not None:
        query = query.where(ImprovementProposal.agent_id == agent_id)
    if q and q.strip():
        query = query.where(ImprovementProposal.summary.ilike(f"%{escape_like(q.strip())}%", escape="\\"))
    # The sorted column first, then newest first and the id so a page boundary never moves.
    column = {"type": ImprovementProposal.proposal_type, "status": ImprovementProposal.status}.get(sort)
    order = [column.asc() if direction == "asc" else column.desc()] if column is not None else []
    oldest_first = sort == "date" and direction == "asc"
    created = ImprovementProposal.created_at.asc() if oldest_first else ImprovementProposal.created_at.desc()
    query = query.order_by(*order, created, ImprovementProposal.id.desc())
    group_ids = await get_user_group_ids(db, user.id)
    if user.is_admin:
        # An administrator manages every target: the database pages and counts.
        total = int(await db.scalar(select(func.count()).select_from(query.order_by(None).subquery())) or 0)
        rows = list((await db.execute(query.limit(limit).offset(offset))).scalars())
        page = await readable_proposals(db, user, group_ids, rows)
    else:
        # One agent's or one target's proposals, read whole in batches: the
        # total and the pages count only what the rule lets this person read.
        readable = []
        scanned = 0
        while True:
            batch = list((await db.execute(query.limit(_IMPROVEMENTS_SCAN).offset(scanned))).scalars())
            readable += await readable_proposals(db, user, group_ids, batch)
            scanned += len(batch)
            if len(batch) < _IMPROVEMENTS_SCAN:
                break
        total = len(readable)
        page = readable[offset : offset + limit]
    items = [
        ImprovementListItem(
            id=p.id,
            proposal_type=p.proposal_type,
            target_type=p.target_type,
            target_id=p.target_id,
            agent_id=p.agent_id,
            status=p.status,
            summary=p.summary,
            experiment_verdict=p.experiment_verdict,
            experiment=p.experiment or None,
            created_at=p.created_at,
            can_decide=p.status == "pending_approval"
            and proposals.approval_error(handler, target, user, group_ids) is None,
        )
        for p, target, handler in page
    ]
    return ImprovementListResponse(items=items, total=total)


@router.get("/improvements/{proposal_id}/evidence", response_model=list[EvidenceLinkResponse])
async def proposal_evidence(
    proposal_id: str, user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)
):
    """The proposal's evidence, each item linked only where the caller may open it."""
    from app.models.improvement_proposal import ImprovementProposal

    pid = parse_path_id(proposal_id, not_found_detail="Proposal not found")
    proposal = await db.get(ImprovementProposal, pid)
    group_ids = await get_user_group_ids(db, user.id)
    if proposal is None or not await readable_proposals(db, user, group_ids, [proposal]):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Proposal not found")
    refs = list(proposal.evidence_refs or []) + list(proposal.experiment_refs or [])
    links = await evidence_links(db, user, group_ids, refs)
    return [EvidenceLinkResponse(kind=x.kind, id=x.id, href=x.href, hidden=x.hidden) for x in links]
