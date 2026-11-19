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

"""The skills an agent created from a template starts with.

A template names its default skills by built-in slug (``AgentTemplate.
default_skills``): the seed tags every company-managed built-in skill
``slug:<slug>`` and re-creates it with a new id whenever it is deleted, so the
slug is the only stable reference a template can carry.

Resolution is per caller and goes through ``user_visible_filter``, the same
predicate ``validate_personal_agent_bindings`` applies when the agent is
created: a skill the caller may not attach (disabled, or narrowed with
``access_restricted``) is never offered, so a default can never make agent
creation fail. A slug that no skill of this deployment carries (a skill seeded
for another platform mode, or deleted by an admin) is skipped.

One reader for every surface - the template list the wizard preselects from,
the REST create and the agent-builder ``create_agent`` tool.

An agent created without a template and without skills gets a suggestion
instead (:func:`suggested_skill_slugs`): the craft skill of each built-in tool
group it carries, resolved through the same per-caller reader, never attached
by itself (#12343).
"""

from __future__ import annotations

import uuid
from collections.abc import Iterable

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.v1.helpers import user_visible_filter
from app.models.agent_skill import AgentSkill

SKILL_SLUG_TAG_PREFIX = "slug:"


def template_skill_slugs(value: object) -> list[str]:
    """The slugs of a ``default_skills`` value, in order, without duplicates.

    The column is admin-saved JSON: anything but a list of non-blank strings
    reads as no default.
    """
    if not isinstance(value, list):
        return []
    slugs: list[str] = []
    for item in value:
        if isinstance(item, str) and (slug := item.strip()) and slug not in slugs:
            slugs.append(slug)
    return slugs


async def visible_skill_ids_by_slug(
    db: AsyncSession,
    slugs: Iterable[str],
    *,
    user_id: uuid.UUID,
    user_group_ids: list[str] | None,
    is_admin: bool,
) -> dict[str, str]:
    """``slug -> skill id`` for the enabled company-managed skills the caller may see."""
    wanted = set(slugs)
    if not wanted:
        return {}
    result = await db.execute(
        select(AgentSkill.id, AgentSkill.tags).where(
            AgentSkill.is_company_managed.is_(True),
            AgentSkill.enabled.is_(True),
            user_visible_filter(AgentSkill, user_id, user_group_ids, is_admin=is_admin),
        )
    )
    found: dict[str, str] = {}
    for skill_id, tags in result.all():
        for tag in tags or []:
            if not isinstance(tag, str) or not tag.startswith(SKILL_SLUG_TAG_PREFIX):
                continue
            slug = tag[len(SKILL_SLUG_TAG_PREFIX):]
            if slug in wanted and slug not in found:
                found[slug] = str(skill_id)
    return found


def ordered_skill_ids(value: object, by_slug: dict[str, str]) -> list[str]:
    """The ids of a ``default_skills`` value resolved through *by_slug*, in template order."""
    return [by_slug[slug] for slug in template_skill_slugs(value) if slug in by_slug]


#: The craft skill(s) of a built-in tool group, by built-in skill slug: the
#: tool description is the contract, the skill the way to use it well. Read by
#: :func:`suggested_skill_slugs` for an agent created with no skill.
SKILL_SLUGS_BY_BUILTIN_GROUP: dict[str, tuple[str, ...]] = {
    "platform": ("fleet-design-practices", "fleet-health-advisory"),
    "generate_presentation": ("presentation-design",),
    "generate_report": ("report-design",),
    "video_studio": ("video-production",),
}


def suggested_skill_slugs(builtin_tool_ids: object) -> list[str]:
    """The craft skill slugs of the built-in groups *builtin_tool_ids* names, in order.

    A former id (``agent_builder``, ``fleets``) counts as its group.
    """
    from app.services.builtin_tool_aliases import alias_group, current_group_id

    if not isinstance(builtin_tool_ids, list):
        return []
    slugs: list[str] = []
    for tool_id in builtin_tool_ids:
        if not isinstance(tool_id, str):
            continue
        group = alias_group(tool_id) or current_group_id(tool_id)
        for slug in SKILL_SLUGS_BY_BUILTIN_GROUP.get(group, ()):
            if slug not in slugs:
                slugs.append(slug)
    return slugs


async def template_default_skill_ids(
    db: AsyncSession,
    default_skills: object,
    *,
    user_id: uuid.UUID,
    user_group_ids: list[str] | None,
    is_admin: bool,
) -> list[str]:
    """The skill ids one template's defaults resolve to for the caller."""
    by_slug = await visible_skill_ids_by_slug(
        db,
        template_skill_slugs(default_skills),
        user_id=user_id,
        user_group_ids=user_group_ids,
        is_admin=is_admin,
    )
    return ordered_skill_ids(default_skills, by_slug)
