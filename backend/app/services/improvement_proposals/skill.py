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

"""The ``skill`` type of the improvement proposals: a change to a skill's
instructions, written as a new skill version (#8256).

Applying queues a re-evaluation when the proposal names an evaluation: the
agent its baseline ran is run again with the changed skill - a fresh sandbox
copy of its source when the baseline ran a copy (``candidate.queue_candidate``),
never the production agent itself.

Who decides is who may edit the skill by hand (``can_manage``: a
company-managed skill by administrators, a shared one by a ``config``
grantee), with no administrator bypass on a private skill. A built-in skill is
never changed (its seeded text comes back on every restart), nor a skill whose
content comes from a URL or an integration: the next refresh would overwrite
the change.
"""

from __future__ import annotations

import logging
import uuid
from typing import TYPE_CHECKING

from sqlalchemy import select

from app.api.v1.helpers import can_manage
from app.models.agent import Agent
from app.models.agent_skill import AgentSkill
from app.models.improvement_proposal import STATUS_WATCHING, TARGET_SKILL, TYPE_SKILL, ImprovementProposal
from app.models.user import User
from app.services.audit import log_audit, log_system_audit
from app.services.improvement_proposals.candidate import baseline_of, baseline_subject, queue_candidate
from app.services.improvement_proposals.lifecycle import (
    AUDIT_ENTITY,
    SYSTEM_ACTOR,
    ProposalError,
    ProposalSpec,
    existing_user_id,
)
from app.services.improvement_proposals.registry import Applied, TargetInfo, locked, register_proposal_type
from app.services.improvement_proposals.text_change import check_text_change
from app.services.skill_versioning import create_skill_version, get_active_skill_version_number

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

logger = logging.getLogger(__name__)

#: The longest skill a change handles.
MAX_SKILL_CHARS = 100_000


def _source_bound(skill: AgentSkill) -> bool:
    return bool(skill.url or skill.source_integration_id)


async def _measured_agent(db: AsyncSession, proposal: ImprovementProposal, skill: AgentSkill) -> Agent | None:
    """The production agent the change is measured in: the one its baseline ran,
    or that agent's source when the baseline ran a sandbox copy; ``None`` when
    the proposal names no evaluation, or when that agent is gone or no longer
    uses the skill after a baseline on a sandbox copy (the change then goes
    unmeasured, ``queue_candidate``)."""
    baseline = await baseline_of(db, proposal, "change")
    if baseline is None:
        return None
    subject = await baseline_subject(db, proposal, baseline)
    agent = subject.agent
    if agent is not None and str(skill.id) in {str(s) for s in (agent.skill_ids or [])}:
        return agent
    if subject.sandboxed:
        return None
    raise ProposalError(
        409, "The agent this change was measured on is gone or no longer uses the skill. Reject this change."
    )


class SkillHandler:
    proposal_type = TYPE_SKILL
    target_type = TARGET_SKILL
    noun = "skill change"
    subject = "skill"
    owns_watch = False

    async def load_target(self, db: AsyncSession, target_id: uuid.UUID, *, lock: bool) -> AgentSkill | None:
        query = select(AgentSkill).where(AgentSkill.id == target_id)
        return await db.scalar(locked(query, lock))

    def describe(self, target: AgentSkill) -> TargetInfo:
        return TargetInfo(
            name=target.name, agent_id=None, is_company_managed=bool(target.is_company_managed), managed=target
        )

    def manage_error(
        self, target: AgentSkill, user: User, user_group_ids: list[str] | None, *, action: str
    ) -> str | None:
        if target.tags and "builtin" in target.tags:
            return f"Cannot {action} built-in skills."
        if _source_bound(target):
            return f"Cannot {action} a skill whose content comes from its source."
        if not can_manage(user, target, user_group_ids):
            if target.is_company_managed:
                return f"Only admins can {action} company-managed skills."
            return f"You don't have permission to {action} this skill."
        return None

    async def validate(self, db: AsyncSession, spec: ProposalSpec, target: AgentSkill) -> None:
        if (target.tags and "builtin" in target.tags) or _source_bound(target) or target.content is None:
            raise ProposalError(422, "This skill's content cannot be changed by a proposal.")
        if spec.base_content != target.content:
            raise ProposalError(409, "The skill changed since this change was written.")
        check_text_change(spec.base_content, spec.proposed_content, ceiling=MAX_SKILL_CHARS, subject="skill")

    async def stale_error(self, db: AsyncSession, proposal: ImprovementProposal, target: AgentSkill) -> str | None:
        if _source_bound(target) or (target.content or "") != proposal.base_content:
            return "The skill changed since this change was proposed. Reject it and ask for a new one."
        return None

    async def apply(
        self, db: AsyncSession, proposal: ImprovementProposal, target: AgentSkill, approver: User
    ) -> Applied:
        agent = await _measured_agent(db, proposal, target)
        queued = await queue_candidate(db, proposal, agent, noun="change", trigger_reason=AUDIT_ENTITY)
        target.content = proposal.proposed_content
        version = await create_skill_version(
            db, target, author_id=approver.id, commit_message="Approved improvement proposal"
        )
        staged = await queued() if queued else None
        candidate_id = staged.run_id if staged else None
        await log_audit(
            db,
            user=approver,
            action="update",
            entity_type="agent_skill",
            entity_id=target.id,
            entity_name=target.name,
            summary=f"Applied an approved change to skill '{target.name}' as v{version.version_number}",
            details={
                "improvement_proposal_id": str(proposal.id),
                "skill_version": version.version_number,
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

    async def is_still_applied(self, db: AsyncSession, proposal: ImprovementProposal, target: AgentSkill) -> bool:
        active = await get_active_skill_version_number(db, target.id)
        return active == proposal.applied_version_number and (target.content or "") == proposal.proposed_content

    async def rollback(
        self, db: AsyncSession, proposal: ImprovementProposal, target: AgentSkill, reason: str
    ) -> int | None:
        target.content = proposal.base_content
        version = await create_skill_version(
            db,
            target,
            # The approver's id carries no key and may outlive their account.
            author_id=await existing_user_id(db, proposal.decided_by_user_id),
            commit_message=f"Automatic rollback: {reason[:200]}",
        )
        anchor = await existing_user_id(db, proposal.decided_by_user_id, target.user_id)
        if anchor is None:
            logger.warning("Skill rollback of %s has no user left to anchor its audit entry", target.id)
        else:
            await log_system_audit(
                db,
                actor=SYSTEM_ACTOR,
                anchor_user_id=anchor,
                action="rollback",
                entity_type="agent_skill",
                entity_id=target.id,
                entity_name=target.name,
                summary=f"Rolled back skill '{target.name}': {reason[:200]}",
                details={
                    "improvement_proposal_id": str(proposal.id),
                    "rolled_back_version": proposal.applied_version_number,
                    "skill_version": version.version_number,
                },
            )
        return version.version_number


SKILL = SkillHandler()
register_proposal_type(TYPE_SKILL, SKILL)
