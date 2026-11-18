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

"""The re-evaluation that judges an applied change: one candidate run of the
proposal's evaluation, for the agent the change runs in, on the baseline's
model. The handler of a type an evaluation measures queues it on apply; the
settlement compares it with the baseline (``lifecycle.settle``).

Like for like: a baseline that ran a sandbox copy is compared with a
candidate run on a fresh sandbox copy of the same source, made once the
change is written so it carries it - its recorded calls replayed and its
writes intercepted, as the baseline's were. A production agent never runs an
evaluation the Concierge built (``concierge_built``: production cases,
recorded calls): its writes would happen again for real. When no such copy
can be made (the source is gone, out of the supervision scope, or no longer
runs the change), or the baseline of a ``concierge_built`` evaluation ran no
copy, the change applies unmeasured, with the reason (:class:`StagedCandidate`). The copy is kept while the proposal waits
for its verdict (:func:`copies_awaiting_a_verdict`), then purged with the
other expired copies.
"""

from __future__ import annotations

import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING

from sqlalchemy import func, select

from app.models.dataset import DatasetEntry, EvaluationDataset
from app.models.evaluation import Evaluation, EvaluationRun
from app.models.improvement_proposal import STATUS_WATCHING, ImprovementProposal
from app.models.sandbox_copy import SOURCE_AGENT, SandboxCopy
from app.services.health.sandbox.copies import register_sandbox_reference_check
from app.services.improvement_proposals.lifecycle import AUDIT_ENTITY, ProposalError

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from app.models.agent import Agent


@dataclass(frozen=True, slots=True)
class BaselineSubject:
    """What the baseline ran, as the candidate is matched to it."""

    #: The production agent it measured: its subject, or the source of the
    #: sandbox copy it ran; ``None`` when that agent is gone.
    agent: Agent | None
    #: The sandbox copy it ran, when it ran one.
    copy: SandboxCopy | None
    #: The candidate runs on a fresh sandbox copy, never on a production agent.
    sandboxed: bool


@dataclass(frozen=True, slots=True)
class StagedCandidate:
    """What staging queued: the candidate run, or why the change goes unmeasured."""

    run_id: uuid.UUID | None
    unmeasured: str | None = None


async def _entry_count(db: AsyncSession, evaluation_id: uuid.UUID) -> int:
    return int(
        await db.scalar(
            select(func.count(DatasetEntry.id))
            .join(EvaluationDataset, EvaluationDataset.dataset_id == DatasetEntry.dataset_id)
            .where(EvaluationDataset.evaluation_id == evaluation_id)
        )
        or 0
    )


async def baseline_of(db: AsyncSession, proposal: ImprovementProposal, noun: str) -> EvaluationRun | None:
    """The baseline run *proposal* is measured against; ``None`` when it names no evaluation."""
    if proposal.evaluation_id is None or proposal.baseline_run_id is None:
        return None
    baseline = await db.get(EvaluationRun, proposal.baseline_run_id)
    if baseline is None or await db.get(Evaluation, proposal.evaluation_id) is None:
        raise ProposalError(409, f"The evaluation or its baseline run was deleted. Reject this {noun}.")
    return baseline


async def baseline_subject(db: AsyncSession, proposal: ImprovementProposal, baseline: EvaluationRun) -> BaselineSubject:
    """The agent *baseline* ran, mapped back to production when it was a sandbox copy."""
    from app.models.agent import Agent
    from app.services.health.sandbox.copies import sandbox_copy_of

    evaluation = await db.get(Evaluation, proposal.evaluation_id)
    subject = await db.get(Agent, baseline.agent_id) if baseline.agent_id is not None else None
    copy = None
    if subject is not None and subject.is_sandbox:
        copy = await sandbox_copy_of(db, agent_id=subject.id)
        source_id = copy.source_id if copy is not None and copy.source_type == SOURCE_AGENT else None
        subject = await db.get(Agent, source_id) if source_id is not None else None
    sandboxed = copy is not None or bool(evaluation is not None and evaluation.concierge_built)
    return BaselineSubject(agent=subject, copy=copy, sandboxed=sandboxed)


def _unlike(subject: BaselineSubject, agent: Agent | None, noun: str) -> str | None:
    """Why no candidate can match a baseline that ran a sandbox copy, or ``None``."""
    if agent is None:
        return f"the agent this {noun} was measured on is gone or no longer runs it"
    if subject.copy is None:
        return "its baseline ran no sandbox copy to compare a copy with"
    if subject.copy.source_id != agent.id:
        return f"its baseline ran a copy of another agent than the one this {noun} changes"
    return None


def _unmeasured(reason: str) -> Callable[[], Awaitable[StagedCandidate]]:
    async def stage() -> StagedCandidate:
        return StagedCandidate(None, reason)

    return stage


class _CopyRefusedError(Exception):
    """The fresh copy may not be evaluated: its savepoint is rolled back."""


async def _fresh_copy(
    db: AsyncSession, proposal: ImprovementProposal, agent: Agent, baseline_copy: SandboxCopy
) -> tuple[uuid.UUID | None, str | None]:
    """A sandbox copy of *agent* as it is now, the change in it, replaying what
    the baseline's copy replayed: ``(copy agent id, None)``, or ``(None, why)``
    with nothing left behind when the Concierge may not evaluate it."""
    from app.services.health.evaluations import evaluation_subject_refusal
    from app.services.health.sandbox.copies import SandboxCopyError, create_sandbox_copy

    try:
        async with db.begin_nested():
            copy = await create_sandbox_copy(
                db,
                source_type=SOURCE_AGENT,
                source_id=agent.id,
                created_for=f"{AUDIT_ENTITY}:{proposal.id}",
                created_by_agent_id=proposal.author_agent_id,
                source_trace_ids=baseline_copy.source_trace_ids or (),
                source_run_ids=baseline_copy.source_run_ids or (),
            )
            refusal = await evaluation_subject_refusal(db, SOURCE_AGENT, copy.copy_agent_id)
            if refusal is not None:
                raise _CopyRefusedError(refusal)
    except SandboxCopyError as exc:
        return None, f"no sandbox copy of the agent could be made ({exc})"
    except _CopyRefusedError as exc:
        return None, f"its sandbox copy may not be evaluated ({exc})"
    return copy.copy_agent_id, None


async def queue_candidate(
    db: AsyncSession, proposal: ImprovementProposal, agent: Agent | None, *, noun: str, trigger_reason: str
) -> Callable[[], Awaitable[StagedCandidate]] | None:
    """The checks of the candidate run, before the target changes, then what
    stages it (call it once the change is written); ``None`` when the proposal
    measures no re-evaluation.

    *agent* is the production agent the change runs in, ``None`` when the
    agent the baseline measured is gone or no longer runs the change: then a
    baseline on a sandbox copy leaves the change unmeasured, and any other is
    refused.
    """
    baseline = await baseline_of(db, proposal, noun)
    if baseline is None:
        return None
    subject = await baseline_subject(db, proposal, baseline)
    if subject.sandboxed:
        reason = _unlike(subject, agent, noun)
        if reason is not None:
            return _unmeasured(reason)
    if agent is None:
        raise ProposalError(409, f"The agent this {noun} was measured on is gone. Reject this {noun}.")
    evaluation = await db.get(Evaluation, proposal.evaluation_id)
    entries = await _entry_count(db, evaluation.id)
    if entries == 0:
        raise ProposalError(409, "The evaluation has no dataset entries left to run.")
    # The checks of every launch, before the target changes: an approval
    # whose candidate run would be refused, or fail at once, is not taken.
    # A copy has its source's model and model independence.
    from app.services.evaluation.launch import LaunchRefusedError, check_launchable, check_runs_served

    try:
        await check_launchable(db, evaluation)
        await check_runs_served(db, evaluation, [(agent.id, baseline.model_id, baseline.is_default_model)])
    except LaunchRefusedError as refused:
        raise ProposalError(refused.status_code, refused.detail) from None

    async def stage() -> StagedCandidate:
        from app.services.agent_version_snapshots import resolve_agent_version_snapshot
        from app.services.evaluation_engine import stage_evaluation_execution

        subject_id: uuid.UUID | None = agent.id
        if subject.sandboxed:
            subject_id, refusal = await _fresh_copy(db, proposal, agent, subject.copy)
            if subject_id is None:
                return StagedCandidate(None, refusal)
        persona_version, prompt_versions, config_versions, skill_versions = await resolve_agent_version_snapshot(
            db, subject_id
        )
        candidate = EvaluationRun(
            evaluation_id=evaluation.id,
            agent_id=subject_id,
            model_id=baseline.model_id,
            is_default_model=baseline.is_default_model,
            status="pending",
            trigger_reason=trigger_reason,
            total_entries=entries,
            persona_version_number=persona_version,
            prompt_versions=prompt_versions,
            agent_config_versions=config_versions,
            skill_versions=skill_versions,
        )
        db.add(candidate)
        await db.flush()
        execution = await stage_evaluation_execution(db, str(evaluation.id), trigger_reason)
        # Every ACTIVE run names its owning execution: a cleanup keyed on
        # ownership skips an unstamped run.
        candidate.owner_execution_id = execution.id
        return StagedCandidate(candidate.id)

    return stage


async def copies_awaiting_a_verdict(db: AsyncSession, copy_ids: list[uuid.UUID]) -> set[uuid.UUID]:
    """The copies among *copy_ids* a watched proposal's candidate run still has
    to be read on: the purge keeps them until the settlement reads it."""
    if not copy_ids:
        return set()
    rows = await db.execute(
        select(SandboxCopy.id)
        .join(EvaluationRun, EvaluationRun.agent_id == SandboxCopy.copy_agent_id)
        .join(ImprovementProposal, ImprovementProposal.candidate_run_id == EvaluationRun.id)
        .where(
            SandboxCopy.id.in_(copy_ids),
            ImprovementProposal.status == STATUS_WATCHING,
            ImprovementProposal.verdict.is_(None),
        )
    )
    return set(rows.scalars())


register_sandbox_reference_check(copies_awaiting_a_verdict)
