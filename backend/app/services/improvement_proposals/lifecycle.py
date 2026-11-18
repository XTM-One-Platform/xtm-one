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

"""The lifecycle of an improvement proposal (PRD ``platform-concierge.md`` 6.10).

1. **File** (``file_proposal``): a person (the #1044 rewrite request) or an
   agent (the Experimenter) states a change of one target with its evidence and
   its measured result, or ``unmeasured``. A change already open under the
   same dedup key, or one an agent filed and a person rejected, is not filed
   again: the existing proposal is returned.
2. **Approve or reject** (someone who may manage the target, ``approval_error``):
   approving writes the change as a new version of the target, audited, through
   the type's handler.
3. **Watch and settle** (the scheduler, ``settle_open_proposals``): the
   re-evaluation, when the type queued one, is compared with
   ``evaluation_comparison`` against its 2-point margin, and live watchers read
   the window; a regression rolls the target back - as a new version, and only
   while the proposal's change is still the one in force: a human edit made
   meanwhile is kept and the verdict reported. A watch window that ends
   without a regression keeps the change; one nothing read (no re-evaluation,
   no live watcher) ends ``failed``, the change still in force but unmeasured.

A non-success never reads as a success: an unmeasured proposal says so, and a
watch with no verdict ends ``failed`` with the reason.
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

from sqlalchemy import and_, or_, select

from app.api.v1.helpers import can_manage, can_manage_as, get_user_group_ids
from app.core.utils import format_exception_message
from app.models.evaluation import EvaluationRun
from app.models.improvement_proposal import (
    EXPERIMENT_VERDICTS,
    OPEN_STATUSES,
    STATUS_APPLYING,
    STATUS_FAILED,
    STATUS_KEPT,
    STATUS_PENDING_APPROVAL,
    STATUS_REGRESSION_KEPT,
    STATUS_REJECTED,
    STATUS_ROLLED_BACK,
    STATUS_SHADOW,
    STATUS_WATCHING,
    TARGET_OF,
    TYPE_NEW_FLOW,
    ImprovementProposal,
)
from app.services.audit import log_audit, log_system_audit
from app.services.evaluation_comparison import NOISE_MARGIN_POINTS, compare_metric
from app.services.health.availability import health_enabled
from app.services.improvement_proposals.dedup import dedup_key, evidence_fingerprint
from app.services.improvement_proposals.registry import (
    ProposalHandler,
    handler_for,
    live_watched_types,
    live_watcher_for,
    self_watched_types,
)

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from app.models.agent import Agent
    from app.models.user import User

logger = logging.getLogger(__name__)

AUDIT_ENTITY = "improvement_proposal"
#: The outcome note of a change applied without the re-evaluation its proposal names.
UNMEASURED_PREFIX = "Not re-evaluated: "
SYSTEM_ACTOR = "improvement_proposal"
_SETTLE_BATCH = 50
_MAX_REFS = 200

#: Where each status may go next; a terminal status stamps ``settled_at``.
_TRANSITIONS: dict[str, frozenset[str]] = {
    STATUS_PENDING_APPROVAL: frozenset({STATUS_REJECTED, STATUS_APPLYING, STATUS_SHADOW, STATUS_WATCHING, STATUS_FAILED}),
    STATUS_APPLYING: frozenset({STATUS_SHADOW, STATUS_WATCHING, STATUS_FAILED}),
    STATUS_SHADOW: frozenset({STATUS_WATCHING, STATUS_ROLLED_BACK, STATUS_FAILED}),
    STATUS_WATCHING: frozenset({STATUS_KEPT, STATUS_ROLLED_BACK, STATUS_REGRESSION_KEPT, STATUS_FAILED}),
}
_TERMINAL = frozenset({STATUS_REJECTED, STATUS_KEPT, STATUS_ROLLED_BACK, STATUS_REGRESSION_KEPT, STATUS_FAILED})


class ProposalError(Exception):
    """A request the lifecycle refuses, with the HTTP status that says why."""

    def __init__(self, status_code: int, detail: str) -> None:
        super().__init__(detail)
        self.status_code = status_code
        self.detail = detail


@dataclass(frozen=True)
class ProposalSpec:
    """A change to file: what, on which target, on what evidence, measured how."""

    proposal_type: str
    target_id: uuid.UUID
    base_content: str = ""
    proposed_content: str = ""
    change: dict[str, Any] = field(default_factory=dict)
    summary: str | None = None
    finding: dict[str, Any] = field(default_factory=dict)
    evidence_refs: list[dict[str, Any]] = field(default_factory=list)
    experiment_refs: list[dict[str, Any]] = field(default_factory=list)
    experiment: dict[str, Any] = field(default_factory=dict)
    #: Required from an agent: a comparison verdict or ``unmeasured``.
    experiment_verdict: str | None = None
    evaluation_id: uuid.UUID | None = None
    baseline_run_id: uuid.UUID | None = None
    quality_metric: str = "score"
    baseline_score: float | None = None
    base_version_number: int | None = None


@dataclass(frozen=True, slots=True)
class SettleResult:
    changed: bool
    status: str


@dataclass(frozen=True, slots=True)
class FiledProposal:
    proposal: ImprovementProposal
    #: ``False``: the same change was already filed and is returned as it is.
    created: bool


def surface_enabled(proposal: ImprovementProposal) -> bool:
    """Whether a proposal is reachable by people and agents right now.

    A person's persona rewrite (#1044) stays behind ``EVALUATION``, where it
    always was; what an agent filed is Platform Health's, behind its
    availability. The scheduler settles both whatever the switches: a change
    applied before one went off is still watched and rolled back.
    """
    if proposal.author_agent_id is None:
        from app.services.evaluation.availability import evaluation_enabled

        return evaluation_enabled()
    return health_enabled()


def any_surface_enabled() -> bool:
    """Whether any proposal is reachable at all: the routes are served and published only then."""
    from app.services.evaluation.availability import evaluation_enabled

    return evaluation_enabled() or health_enabled()


def surface_filter() -> Any:
    """``surface_enabled`` as a query predicate."""
    from sqlalchemy import false, true

    from app.services.evaluation.availability import evaluation_enabled

    return or_(
        and_(ImprovementProposal.author_agent_id.is_(None), true() if evaluation_enabled() else false()),
        and_(ImprovementProposal.author_agent_id.is_not(None), true() if health_enabled() else false()),
    )


async def watch_days(db: AsyncSession) -> int:
    """The watch window of an agent-filed change, in days (decision B18): Settings >
    Agents and automations > Platform Health (``HealthSettings.proposal_watch_days``), read on *db*."""
    from app.services.health.settings import load_health_settings

    return (await load_health_settings(db)).proposal_watch_days


def _handler(proposal_type: str) -> ProposalHandler:
    handler = handler_for(proposal_type)
    if handler is None:
        raise ProposalError(422, "This kind of improvement is not supported.")
    return handler


def _refs(refs: list[dict[str, Any]]) -> list[dict[str, str]]:
    """References kept as ``{"kind", "id"}`` strings, bounded: never a payload."""
    return [
        {"kind": str(ref.get("kind", ""))[:64], "id": str(ref.get("id", ""))[:128]}
        for ref in refs[:_MAX_REFS]
        if isinstance(ref, dict) and ref.get("kind") and ref.get("id")
    ]


# ---------------------------------------------------------------------------
# Who decides, who reads
# ---------------------------------------------------------------------------


def approval_error(
    handler: ProposalHandler, target: Any, user: User, user_group_ids: list[str] | None
) -> str | None:
    """Why *user* may not approve or reject a change of *target*, or ``None`` (B11).

    The gate of a manual edit of the target, and one more rule for a target
    that is not company-managed: an administrator's bypass does not count -
    its owner, or someone it is shared with for configuration, decides.
    """
    error = handler.manage_error(target, user, user_group_ids, action=f"approve a {handler.noun} for")
    if error:
        return error
    info = handler.describe(target)
    if not info.is_company_managed and not can_manage_as(user.id, False, info.managed, user_group_ids):
        return (
            f"A {handler.noun} of a private {handler.target_type} is approved by its owner or by "
            "someone it is shared with for configuration."
        )
    return None


def can_read(handler: ProposalHandler, target: Any, user: User, user_group_ids: list[str] | None) -> bool:
    """A target's proposals are for its managers, administrators included (#1044's list).

    They carry evidence (evaluation scores, feedback comments, trace ids) whose
    audience is the target's managers, not everyone who sees the target.
    """
    return can_manage(user, handler.describe(target).managed, user_group_ids)


# ---------------------------------------------------------------------------
# Reads
# ---------------------------------------------------------------------------


async def open_proposal_on(db: AsyncSession, target_type: str, target_id: uuid.UUID) -> ImprovementProposal | None:
    """The open change of a target (``new_flow`` excluded: it changes no existing target)."""
    return await db.scalar(
        select(ImprovementProposal).where(
            ImprovementProposal.target_type == target_type,
            ImprovementProposal.target_id == target_id,
            ImprovementProposal.status.in_(OPEN_STATUSES),
            ImprovementProposal.proposal_type != TYPE_NEW_FLOW,
        )
    )


def _unseen(proposal: ImprovementProposal) -> bool:
    """An agent's change waiting for a decision while Platform Health is not available: nobody can see or reject it."""
    return (
        proposal.author_agent_id is not None
        and proposal.status == STATUS_PENDING_APPROVAL
        and not surface_enabled(proposal)
    )


async def blocking_proposal_on(
    db: AsyncSession, target_type: str, target_id: uuid.UUID
) -> ImprovementProposal | None:
    """The open change that keeps a person from asking for another one: not one
    waiting unseen, which their request withdraws (``file_proposal``)."""
    proposal = await open_proposal_on(db, target_type, target_id)
    return None if proposal is not None and _unseen(proposal) else proposal


# ---------------------------------------------------------------------------
# File
# ---------------------------------------------------------------------------


async def existing_user_id(db: AsyncSession, *candidates: uuid.UUID | None) -> uuid.UUID | None:
    """The first of *candidates* still a user: what a system audit entry is anchored on."""
    from app.models.user import User

    wanted = [candidate for candidate in candidates if candidate is not None]
    if not wanted:
        return None
    found = set((await db.execute(select(User.id).where(User.id.in_(wanted)))).scalars())
    return next((candidate for candidate in wanted if candidate in found), None)


async def file_proposal(
    db: AsyncSession,
    *,
    spec: ProposalSpec,
    author_agent: Agent | None = None,
    requested_by: User | None = None,
) -> FiledProposal:
    """File *spec* (the caller commits). Who may file is the caller's check.

    ``requested_by``: a person's request (#1044). ``author_agent``: an agent's
    filing, which must state its experiment's verdict or ``unmeasured``.
    """
    if (author_agent is None) == (requested_by is None):
        raise ValueError("A proposal has one author: an agent or a person.")
    handler = _handler(spec.proposal_type)
    if author_agent is not None and spec.experiment_verdict not in EXPERIMENT_VERDICTS:
        raise ProposalError(
            422, "A proposal states its experiment's verdict, or that it is unmeasured."
        )
    target = await handler.load_target(db, spec.target_id, lock=False)
    if target is None:
        raise ProposalError(404, f"{handler.target_type.capitalize()} not found.")

    evidence_refs = _refs(spec.evidence_refs)
    key = dedup_key(
        spec.proposal_type,
        handler.target_type,
        spec.target_id,
        evidence_fingerprint(spec.finding, evidence_refs),
    )
    already = [ImprovementProposal.status.in_(OPEN_STATUSES)]
    if author_agent is not None:
        # A change a person turned down is not filed again on the next cycle;
        # new evidence makes a new key.
        already.append(ImprovementProposal.status == STATUS_REJECTED)
    existing = await db.scalar(
        select(ImprovementProposal)
        .where(ImprovementProposal.dedup_key == key, or_(*already))
        .order_by(ImprovementProposal.created_at.desc())
        .limit(1)
    )
    if existing is not None:
        return FiledProposal(existing, False)
    if spec.proposal_type != TYPE_NEW_FLOW:
        open_change = await open_proposal_on(db, handler.target_type, spec.target_id)
        if open_change is not None and requested_by is not None and _unseen(open_change):
            # An agent's change filed before its flag went off would block the
            # person's request for good: nobody can reject it now.
            await set_proposal_status(
                db, open_change, STATUS_FAILED, "Withdrawn unseen: a person asked for another change meanwhile."
            )
            await db.flush()
            open_change = None
        if open_change is not None:
            raise ProposalError(409, f"This {handler.target_type} already has a change in progress.")

    await handler.validate(db, spec, target)
    info = handler.describe(target)
    proposal = ImprovementProposal(
        proposal_type=spec.proposal_type,
        target_type=TARGET_OF[spec.proposal_type],
        target_id=spec.target_id,
        agent_id=info.agent_id,
        status=STATUS_PENDING_APPROVAL,
        dedup_key=key,
        base_content=spec.base_content,
        proposed_content=spec.proposed_content,
        change=dict(spec.change),
        summary=spec.summary,
        finding=dict(spec.finding),
        evidence_refs=evidence_refs,
        experiment_refs=_refs(spec.experiment_refs),
        experiment=dict(spec.experiment),
        experiment_verdict=spec.experiment_verdict,
        evaluation_id=spec.evaluation_id,
        baseline_run_id=spec.baseline_run_id,
        quality_metric=spec.quality_metric,
        baseline_score=spec.baseline_score,
        base_version_number=spec.base_version_number,
        author_agent_id=author_agent.id if author_agent is not None else None,
        requested_by_user_id=requested_by.id if requested_by is not None else None,
    )
    db.add(proposal)
    await db.flush()
    summary = f"Filed a {handler.noun} for {handler.target_type} '{info.name}'"
    details = {
        "proposal_type": spec.proposal_type,
        "target_id": str(spec.target_id),
        "experiment_verdict": spec.experiment_verdict,
    }
    if requested_by is not None:
        await log_audit(
            db,
            user=requested_by,
            action="create",
            entity_type=AUDIT_ENTITY,
            entity_id=proposal.id,
            entity_name=info.name,
            summary=summary,
            details=details,
        )
    else:
        anchor = await existing_user_id(
            db, author_agent.user_id if author_agent else None, getattr(info.managed, "user_id", None)
        )
        if anchor is None:
            logger.warning("Improvement proposal %s has no user to anchor its audit entry", proposal.id)
        else:
            await log_system_audit(
                db,
                actor=SYSTEM_ACTOR,
                anchor_user_id=anchor,
                action="create",
                entity_type=AUDIT_ENTITY,
                entity_id=proposal.id,
                entity_name=info.name,
                summary=summary,
                details={**details, "author_agent_id": str(author_agent.id) if author_agent else None},
            )
    return FiledProposal(proposal, True)


# ---------------------------------------------------------------------------
# Decide
# ---------------------------------------------------------------------------


async def _decidable(
    db: AsyncSession, user: User, proposal_id: uuid.UUID
) -> tuple[ImprovementProposal, ProposalHandler, Any]:
    # ``populate_existing``: the route read the row before the lock, and a second
    # approval waiting on it must see the status the first one committed.
    proposal = await db.scalar(
        select(ImprovementProposal)
        .where(ImprovementProposal.id == proposal_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    handler = handler_for(proposal.proposal_type) if proposal is not None else None
    if proposal is None or handler is None:
        raise ProposalError(404, "Improvement proposal not found.")
    target = await handler.load_target(db, proposal.target_id, lock=True)
    if target is None:
        raise ProposalError(404, f"{handler.noun.capitalize()} not found.")
    error = approval_error(handler, target, user, await get_user_group_ids(db, user.id))
    if error:
        raise ProposalError(403, error)
    if proposal.status != STATUS_PENDING_APPROVAL:
        raise ProposalError(409, f"This {handler.noun} is no longer waiting for a decision.")
    return proposal, handler, target


async def approve(db: AsyncSession, *, user: User, proposal_id: uuid.UUID) -> ImprovementProposal:
    """Write the approved change as a new version of its target (the caller commits).

    Whatever applying queues (a re-evaluation run and its orchestration
    execution) joins the approval's own transaction: the caller commits once,
    then wakes the reconciler (``notify_orchestration_wake``).
    """
    proposal, handler, target = await _decidable(db, user, proposal_id)
    stale = await handler.stale_error(db, proposal, target)
    if stale:
        raise ProposalError(409, stale)
    applied = await handler.apply(db, proposal, target, user)

    now = datetime.now(UTC)
    proposal.status = applied.status
    proposal.applied_version_number = applied.version_number
    proposal.candidate_run_id = applied.candidate_run_id
    proposal.decided_by_user_id = user.id
    proposal.decided_at = now
    if applied.status in (STATUS_WATCHING, STATUS_SHADOW):
        proposal.applied_at = now
    if applied.unmeasured:
        proposal.outcome_note = f"{UNMEASURED_PREFIX}{applied.unmeasured}."
    if applied.status == STATUS_WATCHING and proposal.author_agent_id is not None:
        # A person's #1044 request is judged by its re-evaluation alone; an
        # agent's change is watched over the window too.
        proposal.watch_until = now + timedelta(days=await watch_days(db))
    return proposal


async def reject(
    db: AsyncSession, *, user: User, proposal_id: uuid.UUID, reason: str | None
) -> ImprovementProposal:
    """Turn a change down; the target never changed (the caller commits)."""
    proposal, handler, target = await _decidable(db, user, proposal_id)
    proposal.status = STATUS_REJECTED
    proposal.decided_by_user_id = user.id
    proposal.decided_at = datetime.now(UTC)
    proposal.settled_at = proposal.decided_at
    proposal.outcome_note = (reason or "").strip() or None
    info = handler.describe(target)
    await log_audit(
        db,
        user=user,
        action="reject",
        entity_type=AUDIT_ENTITY,
        entity_id=proposal.id,
        entity_name=info.name,
        summary=f"Rejected a {handler.noun} of {handler.target_type} '{info.name}'",
        details={"target_id": str(proposal.target_id), "reason": proposal.outcome_note},
    )
    return proposal


async def set_proposal_status(
    db: AsyncSession,
    proposal: ImprovementProposal,
    status: str,
    note: str | None = None,
    *,
    watch_until: datetime | None = None,
    shadow_until: datetime | None = None,
) -> None:
    """Move a proposal along its lifecycle (a handler that owns its watch, level 7).

    Raises ``ValueError`` on a transition the lifecycle does not have, so a
    proposal never jumps from ``rejected`` back to ``watching``.
    """
    if status not in _TRANSITIONS.get(proposal.status, frozenset()):
        raise ValueError(f"An improvement proposal cannot go from {proposal.status!r} to {status!r}")
    now = datetime.now(UTC)
    proposal.status = status
    if note is not None:
        proposal.outcome_note = note
    if shadow_until is not None:
        proposal.shadow_until = shadow_until
    if watch_until is not None:
        proposal.watch_until = watch_until
    if status in (STATUS_SHADOW, STATUS_WATCHING) and proposal.applied_at is None:
        proposal.applied_at = now
    if status in _TERMINAL:
        proposal.settled_at = now


# ---------------------------------------------------------------------------
# Watch and settle
# ---------------------------------------------------------------------------


def settle_status(verdict: str, still_applied: bool) -> str:
    """Where a watch ends: a regression rolls back only the proposal's own change."""
    if verdict == "regression":
        return STATUS_ROLLED_BACK if still_applied else STATUS_REGRESSION_KEPT
    return STATUS_KEPT


async def _regress(
    db: AsyncSession, proposal: ImprovementProposal, handler: ProposalHandler, target: Any, why: str
) -> None:
    """A regression: roll back the proposal's own change, keep a human edit."""
    if settle_status("regression", await handler.is_still_applied(db, proposal, target)) == STATUS_ROLLED_BACK:
        version = await handler.rollback(db, proposal, target, why)
        proposal.status = STATUS_ROLLED_BACK
        proposal.rollback_version_number = version
        rolled = f", as v{version}" if version is not None else ""
        proposal.outcome_note = f"{why}: the {handler.subject} was rolled back to what it was{rolled}."
    else:
        proposal.status = STATUS_REGRESSION_KEPT
        proposal.outcome_note = (
            f"{why}, but the {handler.subject} was edited after the change was applied: "
            "that edit stays, nothing was rolled back."
        )


async def settle(
    db: AsyncSession, proposal: ImprovementProposal, *, now: datetime | None = None
) -> SettleResult:
    """Read the watch of a ``watching`` proposal and end it if it is over (the caller commits)."""
    unchanged = SettleResult(False, proposal.status)
    handler = handler_for(proposal.proposal_type)
    if proposal.status != STATUS_WATCHING or handler is None or handler.owns_watch:
        return unchanged
    now = now or datetime.now(UTC)

    measuring = proposal.candidate_run_id is not None and proposal.verdict is None
    candidate = await db.get(EvaluationRun, proposal.candidate_run_id) if measuring else None
    if candidate is not None and candidate.status in ("pending", "running"):
        return unchanged

    target = await handler.load_target(db, proposal.target_id, lock=True)
    if target is None:
        # An agent deleted mid-loop takes its proposals with it (ON DELETE
        # CASCADE), so a row read here lost a target that had no such key: a
        # skill, a flow, an assignment whose agent remains.
        proposal.status, proposal.settled_at = STATUS_FAILED, now
        proposal.outcome_note = f"The {handler.target_type} was deleted, so there is no verdict."
        return SettleResult(True, proposal.status)

    if measuring:
        if candidate is None or candidate.status != "completed":
            ended = "was deleted" if candidate is None else f"ended {candidate.status}"
            proposal.status, proposal.settled_at = STATUS_FAILED, now
            proposal.outcome_note = (
                f"The re-evaluation run {ended}, so there is no verdict. The change stays "
                f"active; roll it back from the {handler.subject} version history if needed."
            )
            return SettleResult(True, proposal.status)
        comparison = compare_metric(
            proposal.quality_metric,
            proposal.baseline_score,
            getattr(candidate, proposal.quality_metric, None),
        )
        proposal.candidate_score = comparison.candidate
        proposal.delta = comparison.delta
        proposal.verdict = comparison.verdict
        if comparison.verdict is None:
            proposal.status, proposal.settled_at = STATUS_FAILED, now
            proposal.outcome_note = (
                "The re-evaluation has no quality score, so there is no verdict. The change stays active."
            )
            return SettleResult(True, proposal.status)
        if comparison.verdict == "regression":
            await _regress(
                db,
                proposal,
                handler,
                target,
                f"Quality went {comparison.delta:+.2f} points (margin {NOISE_MARGIN_POINTS:g})",
            )
            proposal.settled_at = now
            return SettleResult(True, proposal.status)

    watcher = live_watcher_for(proposal.proposal_type)
    if watcher is not None and proposal.watch_until is not None:
        reading = await watcher(db, proposal)
        if reading.regressed:
            await _regress(db, proposal, handler, target, reading.note or "Its live metrics regressed")
            proposal.settled_at = now
            return SettleResult(True, proposal.status)

    if proposal.watch_until is not None and now < proposal.watch_until:
        return SettleResult(measuring, proposal.status)
    proposal.status, proposal.settled_at = STATUS_KEPT, now
    if proposal.delta is not None:
        proposal.outcome_note = (
            f"Quality went {proposal.delta:+.2f} points (margin {NOISE_MARGIN_POINTS:g}): the change stays."
        )
    elif watcher is not None and proposal.watch_until is not None:
        proposal.outcome_note = "The watch window ended without a regression: the change stays."
    else:
        # No re-evaluation and no live watcher: nothing was read, so no verdict.
        why = proposal.outcome_note if (proposal.outcome_note or "").startswith(UNMEASURED_PREFIX) else None
        proposal.status = STATUS_FAILED
        proposal.outcome_note = (
            "Nothing measured this change during its watch, so there is no verdict. "
            + (f"{why} " if why else "")
            + "The change stays active, unmeasured."
        )
    return SettleResult(True, proposal.status)


async def settle_open_proposals(*, limit: int = _SETTLE_BATCH) -> int:
    """The scheduler's step 9b: settle every ``watching`` proposal whose watch can move.

    Each proposal in its own transaction, locked with SKIP LOCKED so two
    workers never settle the same one twice. No model call. A proposal with
    nothing to read yet - its re-evaluation still running, or its window still
    open with no live watcher - is not picked: it would fill the batch without
    moving, and hold back one that can.
    """
    from app.core.database import async_session

    owned = self_watched_types()
    measuring = and_(ImprovementProposal.candidate_run_id.is_not(None), ImprovementProposal.verdict.is_(None))
    async with async_session() as db:
        query = (
            select(ImprovementProposal.id)
            .outerjoin(EvaluationRun, EvaluationRun.id == ImprovementProposal.candidate_run_id)
            .where(
                ImprovementProposal.status == STATUS_WATCHING,
                or_(
                    # The re-evaluation ended (or its run is gone): a verdict to read.
                    and_(
                        measuring,
                        or_(EvaluationRun.id.is_(None), EvaluationRun.status.notin_(("pending", "running"))),
                    ),
                    # Measured or never measured: the window, or its live watcher.
                    and_(
                        ~measuring,
                        or_(
                            ImprovementProposal.watch_until.is_(None),
                            ImprovementProposal.watch_until <= datetime.now(UTC),
                            ImprovementProposal.proposal_type.in_(live_watched_types()),
                        ),
                    ),
                ),
            )
            .order_by(ImprovementProposal.applied_at.asc().nulls_first())
            .limit(limit)
        )
        if owned:
            query = query.where(ImprovementProposal.proposal_type.notin_(owned))
        ids = list((await db.execute(query)).scalars())
    settled = 0
    for proposal_id in ids:
        async with async_session() as db:
            try:
                proposal = await db.scalar(
                    select(ImprovementProposal)
                    .where(ImprovementProposal.id == proposal_id)
                    .with_for_update(skip_locked=True)
                )
                if proposal is None:
                    continue
                result = await settle(db, proposal)
                await db.commit()
                settled += int(result.changed)
            except Exception as exc:
                await db.rollback()
                logger.error(
                    "Could not settle improvement proposal %s: %s",
                    proposal_id,
                    format_exception_message(exc),
                    exc_info=True,
                )
    return settled

