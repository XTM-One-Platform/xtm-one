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

"""Level 6's experiment kinds: a text of the finding's agent rewritten (#8259).

Three kinds, registered with ``experiments.register_experiment_kind`` at
import: ``persona`` (the agent's persona), ``skill`` (the content of a skill
the agent uses) and ``assignment_prompt`` (one of its assignments' prompt).
Each reads the base the variant replaces and checks the variant
(``variants.check_variant``: the #1044 rules); the two arms are sandbox copies
of the agent (level 2), the persona variant carried by its copy, a skill
variant by an evaluation overlay on its arm. The modules that draft or run an
experiment import this one.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from sqlalchemy import func, select

from app.services.health import quality_deps as deps
from app.services.health.experiments import DraftSpec, ExperimentError, register_experiment_kind
from app.services.health.variants import check_variant

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from app.models.agent import Agent
    from app.models.health_experiment import HealthExperiment
    from app.models.health_finding import HealthFinding


@dataclass(frozen=True, slots=True)
class Base:
    text: str
    version: int | None


async def read_base(db: AsyncSession, agent: Agent, change_type: str, target_id: uuid.UUID) -> Base:
    """The current text a variant of *change_type* replaces, on *agent*; ``ExperimentError`` otherwise."""
    if change_type == "persona":
        if target_id != agent.id:
            raise ExperimentError("A persona variant targets the finding's agent.")
        from app.services.prompt_versioning import get_active_agent_persona_version_number

        version = await get_active_agent_persona_version_number(db, agent_id=agent.id)
        return Base(text=agent.persona or "", version=version)
    if change_type == "skill":
        return await _skill_base(db, agent, target_id)
    from app.models.assignment import Assignment

    assignment = await db.get(Assignment, target_id)
    if assignment is None or assignment.agent_id != agent.id:
        raise ExperimentError("An assignment prompt variant targets one of the finding agent's assignments.")
    return Base(text=assignment.prompt or "", version=None)


async def _skill_base(db: AsyncSession, agent: Agent, skill_id: uuid.UUID) -> Base:
    from app.models.agent_skill import AgentSkill
    from app.models.agent_skill_resource import AgentSkillResource
    from app.services.skill_versioning import get_active_skill_version_number

    if str(skill_id) not in {str(s) for s in (agent.skill_ids or [])}:
        raise ExperimentError("A skill variant targets a skill the finding's agent uses.")
    skill = await db.get(AgentSkill, skill_id)
    if skill is None:
        raise ExperimentError("A skill variant targets a skill the finding's agent uses.")
    if not _skill_managers_read_the_agent(skill, agent):
        # A proposal on the skill goes to its managers, and its text was drawn from this agent's conversations.
        raise ExperimentError("A skill variant targets a skill only the agent's conversation readers manage.")
    if skill.url or skill.source_integration_id:
        # Its content comes from its source: it is not ours to change (level 3 refuses it too).
        raise ExperimentError("This skill is synced from its source: its content cannot be changed here.")
    resources = await db.scalar(
        select(func.count()).select_from(AgentSkillResource).where(AgentSkillResource.skill_id == skill.id)
    )
    if resources:
        # The evaluation's stand-in carries no bundled file: it would measure another skill.
        raise ExperimentError("A skill with bundled resource files cannot be measured as a variant yet.")
    version = await get_active_skill_version_number(db, skill.id)
    return Base(text=skill.content or "", version=version)


def _skill_managers_read_the_agent(skill: Any, agent: Agent) -> bool:
    """Whether everyone who may manage *skill* - who reads a proposal filed on it - may read *agent*'s conversations.

    Administrators manage a company-managed skill and read every agent's
    conversations. A personal skill qualifies when its owner is the agent's,
    and each grant that manages it also reads the agent's chats.
    """
    from app.api.v1.helpers import SHARE_PERMISSIONS_CONFIG, get_group_permission, get_user_permission

    if skill.is_company_managed:
        return True
    if str(skill.user_id) != str(agent.user_id):
        return False

    def reads_chats(permission: str | None) -> bool:
        # A company-managed agent's chats are its owner's and the administrators' alone.
        return not agent.is_company_managed and permission == "config_with_chats"

    for user_id in skill.shared_user_ids or []:
        if get_user_permission(skill, user_id) in SHARE_PERMISSIONS_CONFIG and not reads_chats(
            get_user_permission(agent, user_id)
        ):
            return False
    for group_id in skill.shared_group_ids or []:
        if get_group_permission(skill, [str(group_id)]) in SHARE_PERMISSIONS_CONFIG and not reads_chats(
            get_group_permission(agent, [str(group_id)])
        ):
            return False
    return True


@dataclass(frozen=True, slots=True)
class _TextKind:
    """A text of the finding's agent rewritten: its persona, a skill's content, an assignment's prompt."""

    change_type: str
    target_type: str

    async def _agent(self, db: AsyncSession, agent_id: uuid.UUID | None) -> Agent | None:
        from app.models.agent import Agent

        return await db.get(Agent, agent_id) if agent_id is not None else None

    async def prepare_draft(
        self, db: AsyncSession, *, finding: HealthFinding, target_id: uuid.UUID, payload: dict[str, Any]
    ) -> DraftSpec:
        from app.services.health.access import agent_of_target

        agent = await self._agent(db, await agent_of_target(db, finding.target_type, finding.target_id))
        if agent is None:
            raise ExperimentError("A text variant needs the finding's agent.")
        base = await read_base(db, agent, self.change_type, target_id)
        variant = check_variant(self.change_type, base.text, payload.get("text") or "", payload.get("summary") or "")
        return DraftSpec(
            target_type=self.target_type,
            subject_agent_id=agent.id,
            base_text=base.text,
            base_version=base.version,
            variant_text=variant.text,
            variant={},
            summary=variant.summary,
        )

    async def check_stale(self, db: AsyncSession, experiment: HealthExperiment) -> bool:
        agent = await self._agent(db, experiment.subject_agent_id)
        if agent is None:
            return True
        try:
            base = await read_base(db, agent, experiment.change_type, experiment.target_id)
        except ExperimentError:
            return True
        return base.text != experiment.base_text or base.version != experiment.base_version

    async def dataset_target(self, db: AsyncSession, experiment: HealthExperiment) -> tuple[str, uuid.UUID] | None:
        if self.change_type == "assignment_prompt":
            # Level 4 renders no per-subject input template yet: the variant cannot
            # run on the same cases as its baseline.
            return None
        if self.change_type == "skill" and not deps.SUBJECT_OVERLAYS_AVAILABLE:
            # The variant arm's skill stand-in is an evaluation overlay per subject,
            # which a Concierge launch does not take yet.
            return None
        return ("agent", experiment.subject_agent_id) if experiment.subject_agent_id else None

    async def subjects(
        self, db: AsyncSession, experiment: HealthExperiment, *, created_for: str, author_agent_id: uuid.UUID | None
    ) -> tuple[deps.Subject, deps.Subject]:
        agent_id = experiment.subject_agent_id
        baseline_copy, baseline_agent = await deps.create_agent_copy(
            db, agent_id=agent_id, created_for=created_for, created_by_agent_id=author_agent_id, persona=None, source_trace_ids=[]
        )
        variant_copy, variant_agent = await deps.create_agent_copy(
            db,
            agent_id=agent_id,
            created_for=created_for,
            created_by_agent_id=author_agent_id,
            persona=experiment.variant_text if self.change_type == "persona" else None,
            source_trace_ids=[],
        )
        overrides = {"skill_substitutes": await _skill_substitute(db, experiment)} if self.change_type == "skill" else None
        return (
            deps.Subject(kind="agent", id=baseline_agent, copy_id=baseline_copy),
            deps.Subject(kind="agent", id=variant_agent, copy_id=variant_copy, overrides=overrides),
        )

    def proposal_content(self, experiment: HealthExperiment) -> dict[str, Any]:
        return {"base_content": experiment.base_text, "proposed_content": experiment.variant_text}


for _type, _target in (("persona", "agent"), ("skill", "skill"), ("assignment_prompt", "assignment")):
    register_experiment_kind(_type, _TextKind(change_type=_type, target_type=_target))


async def _skill_substitute(db: AsyncSession, experiment: HealthExperiment) -> dict[str, Any]:
    """The evaluation overlay standing the variant in for the skill (the placebo arms' mechanism, #6535)."""
    from app.models.agent_skill import AgentSkill
    from app.services.builtin_tools.skills import SkillSubstitute

    skill = await db.get(AgentSkill, experiment.target_id)
    if skill is None:
        raise ExperimentError("The skill is gone.")
    return {
        str(skill.id): SkillSubstitute(
            name=skill.name, description=skill.description or "", content=experiment.variant_text
        )
    }
