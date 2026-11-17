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

"""The ``persona`` type of the improvement proposals: what a persona change
does to its agent (#1044, #8256).

The rewrite itself - its evidence, its generation, its rules - is
``services/persona_improvement.py``; this handler writes an approved change as
a new persona version (and queues the re-evaluation that judges it, when the
proposal names an evaluation), tells whether it is still the persona in force,
and rolls it back as a new version. A filed change keeps the loop's rules:
targeted changes only (``rewrite_length_limit``, invariant 16) and a base that
is still the agent's persona. The #1044 request moves onto this handler with
the migration of its ``persona_improvements`` rows.
"""

from __future__ import annotations

import logging
import uuid
from typing import TYPE_CHECKING

from sqlalchemy import select

from app.api.v1.helpers import writable_agent_error
from app.models.agent import Agent
from app.models.improvement_proposal import STATUS_WATCHING, TARGET_AGENT, TYPE_PERSONA, ImprovementProposal
from app.models.user import User
from app.services.audit import log_audit, log_system_audit
from app.services.improvement_proposals.candidate import queue_candidate
from app.services.improvement_proposals.lifecycle import ProposalError, ProposalSpec, existing_user_id
from app.services.improvement_proposals.registry import Applied, TargetInfo, locked, register_proposal_type
from app.services.persona_improvement import (
    CANDIDATE_TRIGGER_REASON,
    MAX_PERSONA_CHARS,
    SYSTEM_ACTOR,
    rewrite_length_limit,
)
from app.services.prompt_versioning import create_agent_persona_version, get_active_agent_persona_version_number

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

logger = logging.getLogger(__name__)


def _check_rewrite(base: str, persona: str) -> None:
    """Targeted changes only (invariant 16: nothing grows the system prompt unasked)."""
    if persona == base:
        raise ProposalError(502, "The model returned the persona unchanged. Try again.")
    if len(persona) > rewrite_length_limit(base):
        raise ProposalError(
            502,
            f"The model's rewrite grew the persona from {len(base):,} to {len(persona):,} "
            "characters; a rewrite makes targeted changes. Try again.",
        )


class PersonaHandler:
    proposal_type = TYPE_PERSONA
    target_type = TARGET_AGENT
    noun = "persona rewrite"
    subject = "persona"
    owns_watch = False

    async def load_target(self, db: AsyncSession, target_id: uuid.UUID, *, lock: bool) -> Agent | None:
        query = select(Agent).where(Agent.id == target_id)
        return await db.scalar(locked(query, lock))

    def describe(self, target: Agent) -> TargetInfo:
        return TargetInfo(
            name=target.name, agent_id=target.id, is_company_managed=bool(target.is_company_managed), managed=target
        )

    def manage_error(
        self, target: Agent, user: User, user_group_ids: list[str] | None, *, action: str
    ) -> str | None:
        return writable_agent_error(target, user, user_group_ids, action=action)

    async def validate(self, db: AsyncSession, spec: ProposalSpec, target: Agent) -> None:
        # The exact text: what approval checks it against and a rollback writes back.
        if spec.base_content != (target.persona or ""):
            raise ProposalError(409, "The persona changed since this rewrite was written.")
        _check_rewrite(spec.base_content.strip(), spec.proposed_content.strip())
        if len(spec.proposed_content) > MAX_PERSONA_CHARS:
            raise ProposalError(422, "The rewrite is longer than the platform keeps.")

    async def stale_error(self, db: AsyncSession, proposal: ImprovementProposal, target: Agent) -> str | None:
        if (target.persona or "") != proposal.base_content:
            return "The persona changed since this rewrite was proposed. Reject it and ask for a new one."
        return None

    async def apply(
        self, db: AsyncSession, proposal: ImprovementProposal, target: Agent, approver: User
    ) -> Applied:
        """The rewrite as a new persona version, and one candidate run of the same
        evaluation on the baseline's model, in the approval's transaction: an
        approval is never committed without the run that judges it. A baseline
        on a sandbox copy (an agent's filing) is matched by a fresh copy of the
        agent, the rewrite in it (``candidate.queue_candidate``)."""
        queued = await queue_candidate(
            db, proposal, target, noun="rewrite", trigger_reason=CANDIDATE_TRIGGER_REASON
        )
        target.persona = proposal.proposed_content
        version = await create_agent_persona_version(
            db,
            agent_id=target.id,
            content=proposal.proposed_content,
            author_id=approver.id,
            commit_message="Approved persona rewrite (self-improvement loop)",
        )
        staged = await queued() if queued else None
        candidate_id = staged.run_id if staged else None
        await log_audit(
            db,
            user=approver,
            action="update",
            entity_type="agent",
            entity_id=target.id,
            entity_name=target.name,
            summary=f"Applied an approved persona rewrite to agent '{target.name}' as v{version.version_number}",
            details={
                "improvement_proposal_id": str(proposal.id),
                "persona_version": version.version_number,
                "previous_version": proposal.base_version_number,
                "candidate_run_id": str(candidate_id) if candidate_id else None,
            },
        )
        return Applied(
            version_number=version.version_number,
            status=STATUS_WATCHING,
            candidate_run_id=candidate_id,
            unmeasured=staged.unmeasured if staged else None,
        )

    async def is_still_applied(self, db: AsyncSession, proposal: ImprovementProposal, target: Agent) -> bool:
        active = await get_active_agent_persona_version_number(db, agent_id=target.id)
        return active == proposal.applied_version_number and (target.persona or "") == proposal.proposed_content

    async def rollback(
        self, db: AsyncSession, proposal: ImprovementProposal, target: Agent, reason: str
    ) -> int | None:
        target.persona = proposal.base_content or None
        version = await create_agent_persona_version(
            db,
            agent_id=target.id,
            content=proposal.base_content,
            # The approver's id carries no key and may outlive their account, while
            # the version's author is a key: a stale id would fail the settlement
            # and leave the regressed persona active on every retry.
            author_id=await existing_user_id(db, proposal.decided_by_user_id),
            commit_message=f"Automatic rollback: {reason[:200]}",
        )
        anchor = await existing_user_id(
            db, proposal.decided_by_user_id, target.user_id, proposal.requested_by_user_id
        )
        if anchor is None:
            logger.warning("Persona rollback of agent %s has no user left to anchor its audit entry", target.id)
        else:
            await log_system_audit(
                db,
                actor=SYSTEM_ACTOR,
                anchor_user_id=anchor,
                action="rollback",
                entity_type="agent",
                entity_id=target.id,
                entity_name=target.name,
                summary=f"Rolled back the persona of agent '{target.name}': {reason[:200]}",
                details={
                    "improvement_proposal_id": str(proposal.id),
                    "rolled_back_version": proposal.applied_version_number,
                    "persona_version": version.version_number,
                    "baseline_score": proposal.baseline_score,
                    "candidate_score": proposal.candidate_score,
                },
            )
        return version.version_number


PERSONA = PersonaHandler()
register_proposal_type(TYPE_PERSONA, PERSONA)
