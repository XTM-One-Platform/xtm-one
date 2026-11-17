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

"""Fork a skill: an editable personal copy of any skill, a built-in one included (#9174).

A built-in skill is read-only (``update_skill`` refuses a skill tagged
``builtin``) and rewritten by the seed on every boot; a fork is how a person
adapts one. The copy is a normal personal skill the seed never reads:

* **Content.** Body, description, compatibility, licence, tags and bundled
  resources (``references/``, ``scripts/``, ``assets/``, the licence file) are
  copied. The seed markers (``builtin``, ``slug:...``) are dropped, so the copy
  is editable and the seed never matches it. The source URL and its credential
  are not copied: a fork is pasted content, never refreshed over its edits.
* **History.** The copy starts its own version history at v1.
* **Provenance.** ``agent_skills.forked_from`` records the source (id, name,
  seed slug, built-in flag, the source's latest version number, fork time).
* **Repoint (optional).** With a fleet, every home member agent of that fleet
  the caller may manage and that binds the source skill is re-bound to the
  copy, its load mode kept. Agents the caller cannot manage are left as they
  are and counted in the warnings.

Nothing runs and nothing is metered. The caller checked visibility of the
source and audits; this module flushes, never commits.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.utils import fit_copy_name
from app.models.agent import Agent
from app.models.agent_skill import AgentSkill
from app.models.agent_skill_resource import copy_skill_resources
from app.models.fleet import FLEET_ROLE_MEMBER, Fleet
from app.models.skill_version import SkillVersion
from app.models.user import User
from app.services.fleets import FleetActionRefusedError, fleet_members

#: Tags the seed writes on a built-in skill; a fork never carries them.
_SEED_TAG = "builtin"
_SEED_SLUG_PREFIX = "slug:"


@dataclass
class SkillForkResult:
    """What a fork wrote: the new skill, its resources, the agents re-bound."""

    skill: AgentSkill
    resources_copied: int = 0
    repointed_agent_ids: list[uuid.UUID] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


def seed_slug(tags: list | None) -> str | None:
    """The seed slug of a skill (its ``slug:...`` tag), or None."""
    for tag in tags or []:
        if isinstance(tag, str) and tag.startswith(_SEED_SLUG_PREFIX):
            return tag[len(_SEED_SLUG_PREFIX) :] or None
    return None


def fork_tags(tags: list | None) -> list[str]:
    """*tags* without the seed markers, order kept."""
    return [
        t for t in (tags or []) if isinstance(t, str) and t != _SEED_TAG and not t.startswith(_SEED_SLUG_PREFIX)
    ]


def repoint_skill_ids(agent: Agent, source_id: str, fork_id: str) -> bool:
    """Re-bind *agent* from *source_id* to *fork_id*, load mode kept. True when it changed."""
    bound = [str(s) for s in (agent.skill_ids or [])]
    if source_id not in bound:
        return False
    out: list[str] = []
    for sid in bound:
        target = fork_id if sid == source_id else sid
        if target not in out:
            out.append(target)
    agent.skill_ids = out
    modes = dict(agent.skill_load_modes or {})
    if source_id in modes:
        modes[fork_id] = modes.pop(source_id)
        agent.skill_load_modes = modes
    return True


async def _latest_version_number(db: AsyncSession, skill_id: uuid.UUID) -> int | None:
    return (
        await db.execute(select(func.max(SkillVersion.version_number)).where(SkillVersion.skill_id == skill_id))
    ).scalar_one_or_none()


async def fork_skill(
    db: AsyncSession,
    source: AgentSkill,
    user: User,
    user_group_ids: list[str],
    *,
    name: str | None = None,
    repoint_fleet: Fleet | None = None,
) -> SkillForkResult:
    """Write a personal fork of *source* for *user*; the caller audits and commits.

    Refused (:class:`FleetActionRefusedError`): a skill whose content was never
    fetched (400), there is nothing to copy yet.
    """
    from app.api.v1.helpers import can_manage
    from app.services.skill_versioning import create_skill_version

    if source.content is None:
        raise FleetActionRefusedError(400, "The skill has no content yet: refresh it before forking it.")

    tags = source.tags or []
    is_builtin = _SEED_TAG in tags
    fork = AgentSkill(
        user_id=user.id,
        is_company_managed=False,
        name=(name or "").strip()[:200] or fit_copy_name(source.name, AgentSkill.__table__.c.name, prefix="Copy of "),
        description=source.description,
        compatibility=source.compatibility,
        license=source.license,
        content=source.content,
        tags=fork_tags(tags),
        hide_from_chat=bool(source.hide_from_chat),
        enabled=True,
        forked_from={
            "skill_id": str(source.id),
            "name": source.name,
            "slug": seed_slug(tags),
            "is_builtin": is_builtin,
            "version_number": await _latest_version_number(db, source.id),
            "forked_at": datetime.now(UTC).isoformat(),
        },
    )
    db.add(fork)
    await db.flush()
    result = SkillForkResult(skill=fork)
    result.resources_copied = await copy_skill_resources(db, source.id, fork.id)
    await create_skill_version(
        db, skill=fork, author_id=user.id, commit_message=f"Forked from '{source.name}'"
    )

    if repoint_fleet is not None:
        homes = [
            r.agent_id
            for r in await fleet_members(db, repoint_fleet.id)
            if r.role == FLEET_ROLE_MEMBER and r.agent_id is not None
        ]
        agents = (await db.execute(select(Agent).where(Agent.id.in_(homes)))).scalars().all() if homes else []
        source_id, fork_id = str(source.id), str(fork.id)
        skipped = 0
        for agent in agents:
            if source_id not in [str(s) for s in (agent.skill_ids or [])]:
                continue
            if not can_manage(user, agent, user_group_ids):
                skipped += 1
                continue
            if repoint_skill_ids(agent, source_id, fork_id):
                result.repointed_agent_ids.append(agent.id)
        if skipped:
            result.warnings.append(f"{skipped} member agent(s) you cannot edit still use the source skill")
    await db.flush()
    return result
