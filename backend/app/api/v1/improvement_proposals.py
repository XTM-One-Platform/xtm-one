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

"""Improvement proposals (#8256): the routes around their lifecycle.

``POST /persona`` asks for a persona rewrite (#1044: administrators, who own
evaluations, behind ``EVALUATION``); ``approve`` and ``reject`` are for someone
who may manage the target - without the admin bypass for a private one
(``improvement_proposals.approval_error``); the list and the detail are for
the target's managers (``can_read``). What a person asked for is reachable
while ``EVALUATION`` is on, what an agent filed while Platform Health is
available (``surface_enabled``); the verdict is reached by the scheduler. Every rule
lives in ``services/improvement_proposals``.
"""

from __future__ import annotations

import uuid
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field, model_validator
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_user, require_admin, require_ai_enabled
from app.api.v1.helpers import (
    get_user_agent,
    get_user_group_ids,
    parse_path_id,
    user_visible_query,
    writable_agent_error,
)
from app.core.database import get_db
from app.core.parse_offload import run_parse
from app.models.agent import Agent
from app.models.evaluation import Evaluation
from app.models.improvement_proposal import (
    STATUS_PENDING_APPROVAL,
    TARGET_AGENT,
    ImprovementProposal,
)
from app.models.user import User
from app.services import improvement_proposals as proposals
from app.services import persona_improvement as persona_loop
from app.services.evaluation.availability import DISABLED_DETAIL as EVALUATION_DISABLED_DETAIL
from app.services.evaluation.availability import evaluation_enabled, require_evaluation
from app.services.evaluation_comparison import NOISE_MARGIN_POINTS
from app.services.improvement_proposals.persona import PERSONA
from app.services.prompt_versioning import build_version_diff


def require_surface() -> None:
    """403 while neither ``EVALUATION`` nor Platform Health is available: no proposal is reachable then.
    Named as the persona routes always answered (``EVALUATION``)."""
    if not proposals.any_surface_enabled():
        raise HTTPException(status_code=403, detail=EVALUATION_DISABLED_DETAIL)


router = APIRouter(dependencies=[Depends(require_surface)])

_NOT_FOUND = "Improvement proposal not found."
_TARGET_NOT_FOUND = "Target not found."
_MANAGERS_ONLY = "Improvement proposals require target owner, admin, or config group permission."
_LIST_LIMIT = 50

#: ``TARGET_TYPES``, as the query validates it.
TargetType = Literal["agent", "skill", "assignment", "flow"]


class PersonaRequest(BaseModel):
    agent_id: uuid.UUID
    evaluation_id: uuid.UUID


class RejectRequest(BaseModel):
    reason: str | None = Field(default=None, max_length=2000)


class ListQuery(BaseModel):
    """One target: ``agent_id`` for an agent's page, or ``target_type`` + ``target_id``."""

    agent_id: uuid.UUID | None = None
    target_type: TargetType | None = None
    target_id: uuid.UUID | None = None

    @model_validator(mode="after")
    def _one_target(self) -> ListQuery:
        if self.agent_id is not None:
            if self.target_type is not None or self.target_id is not None:
                raise ValueError("Pass agent_id, or target_type with target_id, not both.")
            self.target_type, self.target_id = TARGET_AGENT, self.agent_id
        elif self.target_type is None or self.target_id is None:
            raise ValueError("Pass agent_id, or target_type with target_id.")
        return self

    @property
    def target(self) -> tuple[str, uuid.UUID]:
        return str(self.target_type), self.target_id  # type: ignore[return-value]


class DiffLine(BaseModel):
    type: str
    content: str
    old_line: int | None = None
    new_line: int | None = None


class Reference(BaseModel):
    kind: str
    id: str


class ImprovementProposalResponse(BaseModel):
    id: str
    proposal_type: str
    target_type: str
    target_id: str
    target_name: str | None = None
    agent_id: str | None = None
    status: str
    summary: str | None = None
    base_content: str
    proposed_content: str
    diff: list[DiffLine]
    added: int
    removed: int
    base_version_number: int | None = None
    applied_version_number: int | None = None
    rollback_version_number: int | None = None
    #: The experiment the proposal was filed with: a verdict or ``unmeasured``;
    #: ``None`` for a person's request, measured by its re-evaluation alone.
    experiment_verdict: str | None = None
    experiment: dict[str, Any]
    evidence_refs: list[Reference]
    experiment_refs: list[Reference]
    evaluation_id: str | None = None
    evaluation_name: str | None = None
    baseline_run_id: str | None = None
    candidate_run_id: str | None = None
    quality_metric: str
    baseline_score: float | None = None
    candidate_score: float | None = None
    delta: float | None = None
    verdict: str | None = None
    noise_margin_points: float = NOISE_MARGIN_POINTS
    outcome_note: str | None = None
    author_agent_id: str | None = None
    author_agent_name: str | None = None
    requested_by: str | None = None
    decided_by: str | None = None
    #: Whether the caller may approve or reject it now.
    can_decide: bool
    created_at: str
    decided_at: str | None = None
    applied_at: str | None = None
    watch_until: str | None = None
    settled_at: str | None = None


class ImprovementProposalListResponse(BaseModel):
    items: list[ImprovementProposalResponse]
    #: Whether the caller may ask for a persona rewrite of this agent now.
    can_propose: bool


def _refusal(exc: proposals.ProposalError) -> HTTPException:
    return HTTPException(status_code=exc.status_code, detail=exc.detail)


def _iso(value) -> str | None:
    return value.isoformat() if value else None


def _diffs(rows: list[tuple[str, str, int, int]]) -> list[dict]:
    return [
        build_version_diff(from_content=base, to_content=proposed, from_version=from_version, to_version=to_version)
        for base, proposed, from_version, to_version in rows
    ]


async def _names(db: AsyncSession, model, columns, ids: set[uuid.UUID]) -> dict[uuid.UUID, str]:
    if not ids:
        return {}
    rows = (await db.execute(select(model.id, *columns).where(model.id.in_(ids)))).all()
    return {row[0]: next((value for value in row[1:] if value), None) for row in rows}


async def _responses(
    db: AsyncSession, user: User, target: Any, target_handler: proposals.ProposalHandler, rows: list[ImprovementProposal]
) -> list[ImprovementProposalResponse]:
    """The proposals of one target, as its managers read them."""
    users = await _names(
        db,
        User,
        (User.full_name, User.email),
        {uid for row in rows for uid in (row.requested_by_user_id, row.decided_by_user_id) if uid},
    )
    evaluations = await _names(db, Evaluation, (Evaluation.name,), {row.evaluation_id for row in rows if row.evaluation_id})
    authors = await _names(db, Agent, (Agent.name,), {row.author_agent_id for row in rows if row.author_agent_id})
    user_group_ids = await get_user_group_ids(db, user.id)
    deciders: dict[str, bool] = {}
    for row in rows:
        handler = proposals.handler_for(row.proposal_type)
        if row.proposal_type not in deciders:
            deciders[row.proposal_type] = (
                handler is not None and proposals.approval_error(handler, target, user, user_group_ids) is None
            )
    target_name = target_handler.describe(target).name
    # ndiff is quadratic on long similar lines, and a list holds up to 50
    # texts of up to 20,000 characters: never on the event loop.
    diffs = await run_parse(
        _diffs,
        [
            (
                row.base_content,
                row.proposed_content,
                row.base_version_number or 0,
                row.applied_version_number or (row.base_version_number or 0) + 1,
            )
            for row in rows
        ],
    )
    return [
        ImprovementProposalResponse(
            id=str(row.id),
            proposal_type=row.proposal_type,
            target_type=row.target_type,
            target_id=str(row.target_id),
            target_name=target_name,
            agent_id=str(row.agent_id) if row.agent_id else None,
            status=row.status,
            summary=row.summary,
            base_content=row.base_content,
            proposed_content=row.proposed_content,
            diff=[DiffLine(**line) for line in diff["lines"]],
            added=diff["added"],
            removed=diff["removed"],
            base_version_number=row.base_version_number,
            applied_version_number=row.applied_version_number,
            rollback_version_number=row.rollback_version_number,
            experiment_verdict=row.experiment_verdict,
            experiment=row.experiment or {},
            evidence_refs=[Reference(**ref) for ref in row.evidence_refs or []],
            experiment_refs=[Reference(**ref) for ref in row.experiment_refs or []],
            evaluation_id=str(row.evaluation_id) if row.evaluation_id else None,
            evaluation_name=evaluations.get(row.evaluation_id) if row.evaluation_id else None,
            baseline_run_id=str(row.baseline_run_id) if row.baseline_run_id else None,
            candidate_run_id=str(row.candidate_run_id) if row.candidate_run_id else None,
            quality_metric=row.quality_metric,
            baseline_score=row.baseline_score,
            candidate_score=row.candidate_score,
            delta=row.delta,
            verdict=row.verdict,
            outcome_note=row.outcome_note,
            author_agent_id=str(row.author_agent_id) if row.author_agent_id else None,
            author_agent_name=authors.get(row.author_agent_id) if row.author_agent_id else None,
            requested_by=users.get(row.requested_by_user_id) if row.requested_by_user_id else None,
            decided_by=users.get(row.decided_by_user_id) if row.decided_by_user_id else None,
            can_decide=deciders[row.proposal_type] and row.status == STATUS_PENDING_APPROVAL,
            created_at=row.created_at.isoformat(),
            decided_at=_iso(row.decided_at),
            applied_at=_iso(row.applied_at),
            watch_until=_iso(row.watch_until),
            settled_at=_iso(row.settled_at),
        )
        for row, diff in zip(rows, diffs, strict=True)
    ]


async def _readable_target(
    db: AsyncSession, user: User, handler: proposals.ProposalHandler, target_id: uuid.UUID, *, not_found: str
) -> Any:
    """The target, when the caller may read its proposals.

    A target the caller cannot see answers like no target (404); one they see
    but do not manage, 403 - the rule of the agent's own pages.
    """
    target = await handler.load_target(db, target_id, lock=False)
    if target is None:
        raise HTTPException(status_code=404, detail=not_found)
    user_group_ids = await get_user_group_ids(db, user.id)
    if proposals.can_read(handler, target, user, user_group_ids):
        return target
    managed = handler.describe(target).managed
    visible = await db.scalar(
        user_visible_query(type(managed), user.id, user_group_ids, is_admin=user.is_admin)
        .where(type(managed).id == managed.id)
        .with_only_columns(type(managed).id)
    )
    raise HTTPException(status_code=403 if visible else 404, detail=_MANAGERS_ONLY if visible else not_found)


async def _load(db: AsyncSession, user: User, proposal_id: uuid.UUID) -> tuple[ImprovementProposal, Any, Any]:
    proposal = await db.get(ImprovementProposal, proposal_id)
    handler = proposals.handler_for(proposal.proposal_type) if proposal is not None else None
    if proposal is None or handler is None or not proposals.surface_enabled(proposal):
        raise HTTPException(status_code=404, detail=_NOT_FOUND)
    try:
        target = await _readable_target(db, user, handler, proposal.target_id, not_found=_NOT_FOUND)
    except HTTPException as exc:
        # A proposal on a target the caller may not read is answered like no proposal.
        raise HTTPException(status_code=404, detail=_NOT_FOUND) from exc
    return proposal, handler, target


def _target_handler(target_type: str) -> proposals.ProposalHandler:
    handler = proposals.handler_for_target(target_type)
    if handler is None:
        raise HTTPException(status_code=404, detail=_TARGET_NOT_FOUND)
    return handler


@router.get("", response_model=ImprovementProposalListResponse)
async def list_improvement_proposals(
    query: Annotated[ListQuery, Query()],
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Every proposal on one target, newest first, for its managers.

    A proposal carries evidence (an evaluation's scores, feedback comments,
    trace ids) whose audience is the target's managers, not everyone who sees
    the target.
    """
    target_type, target_id = query.target
    handler = _target_handler(target_type)
    target = await _readable_target(db, user, handler, target_id, not_found=_TARGET_NOT_FOUND)
    rows = list(
        (
            await db.execute(
                select(ImprovementProposal)
                .where(
                    ImprovementProposal.target_type == target_type,
                    ImprovementProposal.target_id == target_id,
                    proposals.surface_filter(),
                )
                .order_by(ImprovementProposal.created_at.desc())
                .limit(_LIST_LIMIT)
            )
        ).scalars()
    )
    rows = [row for row in rows if proposals.handler_for(row.proposal_type) is not None]
    can_propose = (
        target_type == TARGET_AGENT
        and user.is_admin
        and evaluation_enabled()
        and writable_agent_error(target, user, await get_user_group_ids(db, user.id)) is None
        and await persona_loop.open_improvement(db, target.id) is None
    )
    return ImprovementProposalListResponse(
        items=await _responses(db, user, target, handler, rows), can_propose=can_propose
    )


@router.get("/{proposal_id}", response_model=ImprovementProposalResponse)
async def get_improvement_proposal(
    proposal_id: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    pid = parse_path_id(proposal_id, not_found_detail=_NOT_FOUND)
    proposal, handler, target = await _load(db, user, pid)
    [response] = await _responses(db, user, target, handler, [proposal])
    return response


@router.post(
    "/persona",
    response_model=ImprovementProposalResponse,
    status_code=201,
    dependencies=[Depends(require_evaluation)],
    include_in_schema=evaluation_enabled(),
)
async def request_persona_rewrite(
    body: PersonaRequest,
    user: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
    _ai_gate: None = Depends(require_ai_enabled),
):
    """Ask for a rewrite of the agent's persona, measured against the evaluation (#1044)."""
    user_group_ids = await get_user_group_ids(db, user.id)
    agent = await get_user_agent(db, user.id, str(body.agent_id), user_group_ids, is_admin=True)
    evaluation = await db.scalar(
        user_visible_query(Evaluation, user.id, user_group_ids, is_admin=True).where(
            Evaluation.id == body.evaluation_id
        )
    )
    if evaluation is None:
        raise HTTPException(status_code=404, detail="Evaluation not found.")
    try:
        proposal = await persona_loop.propose(db, user=user, agent=agent, evaluation=evaluation)
        await db.commit()
    except proposals.ProposalError as exc:
        await db.rollback()
        raise _refusal(exc) from exc
    except IntegrityError as exc:
        await db.rollback()
        raise HTTPException(
            status_code=409, detail="This agent already has a persona rewrite in progress."
        ) from exc
    [response] = await _responses(db, user, agent, PERSONA, [proposal])
    return response


@router.post("/{proposal_id}/approve", response_model=ImprovementProposalResponse)
async def approve_improvement_proposal(
    proposal_id: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    _ai_gate: None = Depends(require_ai_enabled),
):
    """Apply the change as a new version of its target; the watch starts."""
    pid = parse_path_id(proposal_id, not_found_detail=_NOT_FOUND)
    _proposal, handler, target = await _load(db, user, pid)
    try:
        proposal = await proposals.approve(db, user=user, proposal_id=pid)
        await db.commit()
    except proposals.ProposalError as exc:
        await db.rollback()
        raise _refusal(exc) from exc

    from app.services.orchestration_engine import notify_orchestration_wake

    await notify_orchestration_wake()
    [response] = await _responses(db, user, target, handler, [proposal])
    return response


@router.post("/{proposal_id}/reject", response_model=ImprovementProposalResponse)
async def reject_improvement_proposal(
    proposal_id: str,
    body: RejectRequest,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    _ai_gate: None = Depends(require_ai_enabled),
):
    """Turn the change down; the target never changed."""
    pid = parse_path_id(proposal_id, not_found_detail=_NOT_FOUND)
    _proposal, handler, target = await _load(db, user, pid)
    try:
        proposal = await proposals.reject(db, user=user, proposal_id=pid, reason=body.reason)
        await db.commit()
    except proposals.ProposalError as exc:
        await db.rollback()
        raise _refusal(exc) from exc
    [response] = await _responses(db, user, target, handler, [proposal])
    return response
