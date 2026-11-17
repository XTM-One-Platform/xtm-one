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

"""Skill-package binding reconciliation — skills carry their resources onto agents.

A packaged skill can ship resources that are materialized into first-class
platform entities when the package is imported: ``scripts/*`` become Custom
Tools and ``references/*`` become Knowledge Base documents (see
``api/v1/agent_skills.py::_materialize_mapped_resources``).  The provenance
link lives in ``agent_skill_package_origins``.

Binding such a skill to an agent must also hand the agent the entities its
instructions depend on.  A SKILL.md that says "consult ``references/iocs.md``"
is dead weight when the agent cannot ``search_knowledge`` the KB that file
landed in, and a procedure that calls ``scripts/extract.py`` is dead weight
without the Custom Tool.  Before this module the link was only ever applied in
one direction — at *import* time, to agents that already referenced the skill
(``_auto_link_mapped_resources_to_agents``) — so the far more common order
(import the package, then attach the skill to an agent) silently produced an
agent that could not reach its own skill's references.

This module is the single definition of that rule.  Reconciliation is
idempotent and symmetric:

* every package entity carried by a **bound** skill is bound to the agent;
* every package entity that **no** bound skill carries is unbound.

Only package-origin entities are ever removed: a Knowledge Base or Custom Tool
the user attached by hand is invisible to this reconciliation.  The removal
side is what makes detaching a skill clean up after itself, mirroring the
cascade that skill deletion and package re-import already perform.

Visibility is deliberately *not* re-validated for the entities pulled in here:
they are inseparable from the skill (created by the import with the skill's own
``is_company_managed`` / ``shared_group_ids``), and the skill binding that
triggered the reconciliation was itself validated by the caller.  The one hard
constraint is company-managed purity — a company-managed agent may only bind
company-managed KBs (``validate_company_managed_bindings``), so a
non-company-managed package KB is skipped rather than bound, otherwise the
agent would be left in a state its own update endpoint rejects.
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass, field

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.utils import safe_uuid
from app.models.agent_skill_package_origin import AgentSkillPackageOrigin
from app.models.knowledge_base import KnowledgeBase

logger = logging.getLogger(__name__)


@dataclass
class SkillPackageBindingChanges:
    """What a reconciliation pass changed on an agent (audit-friendly)."""

    added_custom_tool_ids: list[str] = field(default_factory=list)
    added_knowledge_base_ids: list[str] = field(default_factory=list)
    removed_custom_tool_ids: list[str] = field(default_factory=list)
    removed_knowledge_base_ids: list[str] = field(default_factory=list)
    # Package KBs left unbound because the agent is company-managed and the KB
    # is not — binding them would make the agent fail its own update validation.
    skipped_knowledge_base_ids: list[str] = field(default_factory=list)

    @property
    def changed(self) -> bool:
        return bool(
            self.added_custom_tool_ids
            or self.added_knowledge_base_ids
            or self.removed_custom_tool_ids
            or self.removed_knowledge_base_ids
        )

    def as_audit_dict(self) -> dict[str, list[str]]:
        """Serialize for ``log_audit`` details (empty when nothing changed)."""
        if not self.changed and not self.skipped_knowledge_base_ids:
            return {}
        return {
            "added_custom_tool_ids": self.added_custom_tool_ids,
            "added_knowledge_base_ids": self.added_knowledge_base_ids,
            "removed_custom_tool_ids": self.removed_custom_tool_ids,
            "removed_knowledge_base_ids": self.removed_knowledge_base_ids,
            "skipped_knowledge_base_ids": self.skipped_knowledge_base_ids,
        }


def _uuid_set(ids: list | None) -> set[uuid.UUID]:
    """Parse a JSON id column leniently — non-UUID junk is ignored, not fatal."""
    parsed = (safe_uuid(str(value)) for value in (ids or []) if value)
    return {value for value in parsed if value is not None}


async def _package_entities_of_skills(
    db: AsyncSession, skill_ids: set[uuid.UUID]
) -> tuple[set[str], set[str]]:
    """Return (custom tool ids, KB ids) materialized by the given skills' packages."""
    if not skill_ids:
        return set(), set()
    rows = await db.execute(
        select(
            AgentSkillPackageOrigin.custom_tool_id,
            AgentSkillPackageOrigin.knowledge_base_id,
        ).where(AgentSkillPackageOrigin.skill_id.in_(skill_ids))
    )
    tool_ids: set[str] = set()
    kb_ids: set[str] = set()
    for custom_tool_id, knowledge_base_id in rows.all():
        if custom_tool_id is not None:
            tool_ids.add(str(custom_tool_id))
        if knowledge_base_id is not None:
            kb_ids.add(str(knowledge_base_id))
    return tool_ids, kb_ids


async def _package_managed_entities(
    db: AsyncSession,
    *,
    custom_tool_ids: set[uuid.UUID],
    knowledge_base_ids: set[uuid.UUID],
) -> tuple[set[str], set[str]]:
    """Return which of the given entities are package-origin (carried by ANY skill).

    Anything not returned here was attached by hand and must never be unbound
    by the reconciliation.
    """
    conditions = []
    if custom_tool_ids:
        conditions.append(AgentSkillPackageOrigin.custom_tool_id.in_(custom_tool_ids))
    if knowledge_base_ids:
        conditions.append(
            AgentSkillPackageOrigin.knowledge_base_id.in_(knowledge_base_ids)
        )
    if not conditions:
        return set(), set()
    rows = await db.execute(
        select(
            AgentSkillPackageOrigin.custom_tool_id,
            AgentSkillPackageOrigin.knowledge_base_id,
        ).where(or_(*conditions))
    )
    managed_tools: set[str] = set()
    managed_kbs: set[str] = set()
    # A ``script`` origin row has no KB and a ``reference`` row has no tool
    # (enforced by ``ck_agent_skill_package_origins_kind_target``), so the two
    # kinds can never bleed into each other here.
    for custom_tool_id, knowledge_base_id in rows.all():
        if custom_tool_id is not None and custom_tool_id in custom_tool_ids:
            managed_tools.add(str(custom_tool_id))
        if knowledge_base_id is not None and knowledge_base_id in knowledge_base_ids:
            managed_kbs.add(str(knowledge_base_id))
    return managed_tools, managed_kbs


async def _company_managed_kb_ids(
    db: AsyncSession, kb_ids: set[str]
) -> set[str]:
    """Filter *kb_ids* down to the KBs flagged company-managed."""
    uuids = _uuid_set(list(kb_ids))
    if not uuids:
        return set()
    rows = await db.execute(
        select(KnowledgeBase.id).where(
            KnowledgeBase.id.in_(uuids),
            KnowledgeBase.is_company_managed.is_(True),
        )
    )
    return {str(row[0]) for row in rows.all()}


def _merge(existing: list | None, *, wanted: set[str], managed: set[str]) -> list[str]:
    """Drop stale package entities from *existing*, then append missing *wanted* ones.

    The caller's original ordering is preserved — new ids land at the end — so
    reconciling twice is a no-op and never churns the stored list.
    """
    kept = [
        str(value)
        for value in (existing or [])
        if value and (str(value) not in managed or str(value) in wanted)
    ]
    present = set(kept)
    kept.extend(sorted(value for value in wanted if value not in present))
    return list(dict.fromkeys(kept))


async def reconcile_agent_skill_package_bindings(
    db: AsyncSession, agent
) -> SkillPackageBindingChanges:
    """Sync *agent*'s Custom Tool / KB bindings with its bound skills' packages.

    Mutates ``agent.custom_tool_ids`` / ``agent.knowledge_base_ids`` in place
    (the caller owns the transaction and the commit) and prunes now-orphaned
    per-KB write grants.  Safe to call on every agent write: it issues at most
    three SELECTs and returns an empty change set when there is nothing to do.
    """
    changes = SkillPackageBindingChanges()

    wanted_tools, wanted_kbs = await _package_entities_of_skills(
        db, _uuid_set(agent.skill_ids)
    )
    bound_tool_uuids = _uuid_set(agent.custom_tool_ids)
    bound_kb_uuids = _uuid_set(agent.knowledge_base_ids)
    managed_tools, managed_kbs = await _package_managed_entities(
        db,
        custom_tool_ids=bound_tool_uuids,
        knowledge_base_ids=bound_kb_uuids,
    )

    # A company-managed agent can only bind company-managed KBs. Skip the rest
    # instead of persisting a binding its own update endpoint would reject.
    if wanted_kbs and getattr(agent, "is_company_managed", False):
        allowed = await _company_managed_kb_ids(db, wanted_kbs)
        skipped = wanted_kbs - allowed
        if skipped:
            changes.skipped_knowledge_base_ids = sorted(skipped)
            logger.info(
                "Agent %s is company-managed: skipped %d non-company-managed "
                "skill-package knowledge base(s)",
                getattr(agent, "id", "?"),
                len(skipped),
            )
        wanted_kbs = allowed

    new_tools = _merge(agent.custom_tool_ids, wanted=wanted_tools, managed=managed_tools)
    new_kbs = _merge(agent.knowledge_base_ids, wanted=wanted_kbs, managed=managed_kbs)

    old_tools = {str(value) for value in (agent.custom_tool_ids or []) if value}
    old_kbs = {str(value) for value in (agent.knowledge_base_ids or []) if value}
    changes.added_custom_tool_ids = sorted(set(new_tools) - old_tools)
    changes.removed_custom_tool_ids = sorted(old_tools - set(new_tools))
    changes.added_knowledge_base_ids = sorted(set(new_kbs) - old_kbs)
    changes.removed_knowledge_base_ids = sorted(old_kbs - set(new_kbs))

    if not changes.changed:
        return changes

    agent.custom_tool_ids = new_tools
    agent.knowledge_base_ids = new_kbs
    if changes.removed_knowledge_base_ids:
        # A write grant on a KB that just went away must not linger.
        agent.prune_orphaned_knowledge_base_permissions()
    return changes
