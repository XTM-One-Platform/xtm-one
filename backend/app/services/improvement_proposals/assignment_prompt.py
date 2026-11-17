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

"""The ``assignment_prompt`` type of the improvement proposals: a change to
what an assignment asks its agent to do (#8256).

Who decides is who may edit the assignment by hand
(``writable_assignment_error``: never on a built-in agent, a company-managed
assignment by administrators, otherwise whoever may manage its agent), with no
administrator bypass on a private agent's assignment. Assignments keep no
version table: the prompt the change starts from is the proposal's
``base_content``, written back on a rollback, and both writes are audited on
the assignment. An evaluation runs the agent, never an assignment's prompt:
applying queues no re-evaluation, and the watch ends unmeasured.
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass
from typing import TYPE_CHECKING

from sqlalchemy import select

from app.api.v1.helpers import writable_assignment_error
from app.models.agent import Agent
from app.models.assignment import Assignment
from app.models.improvement_proposal import (
    STATUS_WATCHING,
    TARGET_ASSIGNMENT,
    TYPE_ASSIGNMENT_PROMPT,
    ImprovementProposal,
)
from app.models.user import User
from app.services.audit import log_audit, log_system_audit
from app.services.improvement_proposals.lifecycle import SYSTEM_ACTOR, ProposalError, ProposalSpec, existing_user_id
from app.services.improvement_proposals.registry import Applied, TargetInfo, locked, register_proposal_type
from app.services.improvement_proposals.text_change import check_text_change

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

logger = logging.getLogger(__name__)

#: The longest prompt a change handles: the persona's ceiling, a prompt being
#: read on every run as a persona is on every call.
MAX_PROMPT_CHARS = 20_000


@dataclass(frozen=True, slots=True)
class AssignmentTarget:
    """An assignment and the agent it runs on, whose managers decide."""

    assignment: Assignment
    agent: Agent


class AssignmentPromptHandler:
    proposal_type = TYPE_ASSIGNMENT_PROMPT
    target_type = TARGET_ASSIGNMENT
    noun = "assignment prompt change"
    subject = "assignment prompt"
    owns_watch = False

    async def load_target(self, db: AsyncSession, target_id: uuid.UUID, *, lock: bool) -> AssignmentTarget | None:
        query = select(Assignment).where(Assignment.id == target_id)
        assignment = await db.scalar(locked(query, lock))
        if assignment is None:
            return None
        agent = await db.get(Agent, assignment.agent_id)
        return AssignmentTarget(assignment, agent) if agent is not None else None

    def describe(self, target: AssignmentTarget) -> TargetInfo:
        return TargetInfo(
            name=target.assignment.name,
            agent_id=target.agent.id,
            # A company-managed agent's assignments are company-managed too; either
            # flag makes administrators the deciders.
            is_company_managed=bool(target.assignment.is_company_managed or target.agent.is_company_managed),
            managed=target.agent,
        )

    def manage_error(
        self, target: AssignmentTarget, user: User, user_group_ids: list[str] | None, *, action: str
    ) -> str | None:
        return writable_assignment_error(target.assignment, target.agent, user, user_group_ids, action=action)

    async def validate(self, db: AsyncSession, spec: ProposalSpec, target: AssignmentTarget) -> None:
        if spec.base_content != (target.assignment.prompt or ""):
            raise ProposalError(409, "The assignment prompt changed since this change was written.")
        check_text_change(
            spec.base_content, spec.proposed_content, ceiling=MAX_PROMPT_CHARS, subject="assignment prompt"
        )

    async def stale_error(
        self, db: AsyncSession, proposal: ImprovementProposal, target: AssignmentTarget
    ) -> str | None:
        if (target.assignment.prompt or "") != proposal.base_content:
            return "The assignment prompt changed since this change was proposed. Reject it and ask for a new one."
        return None

    async def apply(
        self, db: AsyncSession, proposal: ImprovementProposal, target: AssignmentTarget, approver: User
    ) -> Applied:
        assignment = target.assignment
        assignment.prompt = proposal.proposed_content
        await log_audit(
            db,
            user=approver,
            action="update",
            entity_type="assignment",
            entity_id=assignment.id,
            entity_name=assignment.name,
            summary=f"Applied an approved change to the prompt of assignment '{assignment.name}'",
            details={"improvement_proposal_id": str(proposal.id), "agent_id": str(target.agent.id)},
        )
        return Applied(version_number=None, status=STATUS_WATCHING)

    async def is_still_applied(
        self, db: AsyncSession, proposal: ImprovementProposal, target: AssignmentTarget
    ) -> bool:
        return (target.assignment.prompt or "") == proposal.proposed_content

    async def rollback(
        self, db: AsyncSession, proposal: ImprovementProposal, target: AssignmentTarget, reason: str
    ) -> int | None:
        assignment = target.assignment
        assignment.prompt = proposal.base_content
        anchor = await existing_user_id(db, proposal.decided_by_user_id, assignment.user_id, target.agent.user_id)
        if anchor is None:
            logger.warning("Assignment prompt rollback of %s has no user left to anchor its audit entry", assignment.id)
        else:
            await log_system_audit(
                db,
                actor=SYSTEM_ACTOR,
                anchor_user_id=anchor,
                action="rollback",
                entity_type="assignment",
                entity_id=assignment.id,
                entity_name=assignment.name,
                summary=f"Rolled back the prompt of assignment '{assignment.name}': {reason[:200]}",
                details={"improvement_proposal_id": str(proposal.id)},
            )
        return None


ASSIGNMENT_PROMPT = AssignmentPromptHandler()
register_proposal_type(TYPE_ASSIGNMENT_PROMPT, ASSIGNMENT_PROMPT)
