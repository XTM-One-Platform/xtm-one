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

"""The skills the General Assistant carries.

An agent's skills are its ``skill_ids``; the General Assistant of a Copilot
platform is no agent, so its own live on ``GeneralAssistantConfig.skill_ids``
and are rendered, like an agent's, as a catalog in the prompt with the
``load_skill`` / ``read_skill_resource`` tools (``builtin_tools/skills.py``).

``NULL`` means "the platform's choice": the built-in skills declared with
``bind_to_general_assistant`` (:data:`GENERAL_ASSISTANT_SKILL_SLUGS`), so a
release that ships a new one reaches every General Assistant whose admin
never chose. A list is the admin's choice and is kept as it is. A General
Assistant linked to an agent takes that agent's skills instead.
"""

from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.agents.builtin_agents import BUILTIN_SKILLS
from app.models.agent_skill import AgentSkill

#: The built-in skills a General Assistant carries until its admin chooses.
GENERAL_ASSISTANT_SKILL_SLUGS: tuple[str, ...] = tuple(
    skill["slug"] for skill in BUILTIN_SKILLS if skill.get("bind_to_general_assistant")
)


def _builtin_slug(tags: list | None) -> str | None:
    """The slug of a built-in skill, from its ``slug:`` tag; ``None`` for any other skill."""
    if "builtin" not in (tags or []):
        return None
    for tag in tags or []:
        if isinstance(tag, str) and tag.startswith("slug:"):
            return tag[5:]
    return None


async def default_skill_ids(db: AsyncSession) -> list[str]:
    """The ids of the enabled built-in General Assistant skills of this platform, in declaration order.

    Two columns, not the skill bodies: this runs on every General Assistant turn.
    """
    if not GENERAL_ASSISTANT_SKILL_SLUGS:
        return []
    rows = (
        await db.execute(
            select(AgentSkill.id, AgentSkill.tags).where(
                AgentSkill.is_company_managed.is_(True), AgentSkill.enabled.is_(True)
            )
        )
    ).all()
    by_slug = {slug: skill_id for skill_id, tags in rows if (slug := _builtin_slug(tags))}
    return [str(by_slug[slug]) for slug in GENERAL_ASSISTANT_SKILL_SLUGS if slug in by_slug]


async def effective_skill_ids(db: AsyncSession, config: Any) -> list[str]:
    """The skills a General Assistant configured by *config* carries (its own list, else the defaults)."""
    chosen = getattr(config, "skill_ids", None) if config is not None else None
    if chosen is not None:
        return [str(sid) for sid in chosen]
    return await default_skill_ids(db)


async def unbindable_skills(db: AsyncSession, skill_ids: list[str]) -> list[str]:
    """The ids in *skill_ids* the General Assistant may not carry: not a company-managed, enabled skill."""
    refused: list[str] = []
    wanted: list[uuid.UUID] = []
    for raw in skill_ids:
        try:
            wanted.append(uuid.UUID(str(raw)))
        except ValueError:
            refused.append(str(raw))
    if wanted:
        rows = (
            await db.execute(
                select(AgentSkill.id).where(
                    AgentSkill.id.in_(wanted),
                    AgentSkill.is_company_managed.is_(True),
                    AgentSkill.enabled.is_(True),
                )
            )
        ).scalars()
        found = set(rows)
        refused.extend(str(sid) for sid in wanted if sid not in found)
    return refused
