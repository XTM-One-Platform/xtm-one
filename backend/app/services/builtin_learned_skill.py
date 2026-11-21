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

"""The learned skill of a built-in agent (#914, slice 3).

Each built-in agent owns one customer skill, "<agent> learned for
<organization>": a company-managed :class:`AgentSkill` that belongs to the
customer layer (``services/builtin_customer_layer``), so the seed never
rewrites or deletes it (it carries no ``slug:`` and no ``builtin`` tag).

The agent writes durable lessons into it with the bounded tool
``record_learned_lesson`` (``services/builtin_tools/learned_skill.py``):
one section per topic, appended or revised, every change a
:class:`SkillVersion` whose commit message names the run. The skill is bound
to the agent and pinned ``always`` so each run reads it. An administrator
views, edits, freezes (tag :data:`FROZEN_TAG`) or clears it from the skill
page, and reverts a lesson with the existing skill rollback.
"""

from __future__ import annotations

import re
import uuid
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

#: Tag keying the learned skill of an agent: ``learned-for-agent:<agent id>``.
AGENT_TAG_PREFIX = "learned-for-agent:"
#: Tag every learned skill carries, for listing.
LEARNED_TAG = "learned"
#: An administrator sets it to stop the agent writing.
FROZEN_TAG = "learned-frozen"
#: Upper bound of the whole skill body.
LEARNED_SKILL_MAX_CHARS = 12000
#: Upper bound of one lesson section.
SECTION_MAX_CHARS = 1500
#: Upper bound of a section title.
TITLE_MAX_CHARS = 80

_EMPTY_BODY = (
    "Lessons this agent learned while working for this organization. "
    "Each section is one durable lesson; the history keeps who wrote it and in which run.\n"
)


class LessonError(ValueError):
    """A lesson the rules refuse; the message says why."""


def agent_tag(agent_id: Any) -> str:
    return f"{AGENT_TAG_PREFIX}{agent_id}"


def skill_name(agent_name: str, organization_name: str | None) -> str:
    """``<agent> learned for <organization>``, within the 200-character name column."""
    org = (organization_name or "").strip() or "this organization"
    return f"{agent_name} learned for {org}"[:200]


def is_frozen(skill: Any) -> bool:
    return FROZEN_TAG in (getattr(skill, "tags", None) or [])


def _clean_title(title: str) -> str:
    cleaned = " ".join(str(title or "").split()).strip("# ")
    if not cleaned:
        raise LessonError("a lesson needs a title")
    return cleaned[:TITLE_MAX_CHARS]


def _section_spans(body: str) -> list[tuple[str, int, int]]:
    """``(title, start, end)`` of every ``## `` section of *body*."""
    heads = [(m.group(1).strip(), m.start()) for m in re.finditer(r"(?m)^## (.+)$", body)]
    spans: list[tuple[str, int, int]] = []
    for i, (title, start) in enumerate(heads):
        end = heads[i + 1][1] if i + 1 < len(heads) else len(body)
        spans.append((title, start, end))
    return spans


def apply_lesson(body: str | None, title: str, text: str, *, mode: str = "append") -> str:
    """The skill body with *text* appended to, or replacing, section *title*.

    ``append`` adds a line to the section (created when absent), ``revise``
    replaces the section's text, ``remove`` drops the section. Pure: the
    caller stores the result.
    """
    current = body if body is not None else _EMPTY_BODY
    head = _clean_title(title)
    if mode not in {"append", "revise", "remove"}:
        raise LessonError("mode is append, revise or remove")
    lesson = (text or "").strip()
    if mode != "remove" and not lesson:
        raise LessonError("a lesson needs a text")
    if len(lesson) > SECTION_MAX_CHARS:
        raise LessonError(f"a lesson is at most {SECTION_MAX_CHARS} characters")
    span = next((s for s in _section_spans(current) if s[0].lower() == head.lower()), None)
    if span is None:
        if mode == "remove":
            raise LessonError(f"no section named {head!r}")
        updated = f"{current.rstrip()}\n\n## {head}\n{lesson}\n"
    else:
        _, start, end = span
        existing = current[start:end].rstrip()
        if mode == "remove":
            section = ""
        elif mode == "revise":
            section = f"## {span[0]}\n{lesson}\n"
        else:
            section = f"{existing}\n{lesson}\n"
        if len(section) > SECTION_MAX_CHARS + TITLE_MAX_CHARS + 8:
            raise LessonError(f"the section would pass {SECTION_MAX_CHARS} characters: revise it instead")
        gap = "\n" if section else ""
        updated = f"{current[:start]}{section}{gap}{current[end:].lstrip()}".rstrip() + "\n"
    if len(updated) > LEARNED_SKILL_MAX_CHARS:
        raise LessonError(
            f"the skill would pass {LEARNED_SKILL_MAX_CHARS} characters: revise or remove a section first"
        )
    return updated


async def find_learned_skill(db: AsyncSession, agent_id: Any) -> Any:
    """The learned skill of *agent_id*, or None."""
    from app.models.agent_skill import AgentSkill

    rows = (await db.execute(select(AgentSkill).where(AgentSkill.is_company_managed.is_(True)))).scalars().all()
    tag = agent_tag(agent_id)
    return next((s for s in rows if tag in (s.tags or [])), None)


async def ensure_learned_skill(db: AsyncSession, agent: Any, *, author_id: uuid.UUID | None) -> Any:
    """The learned skill of a built-in *agent*, created empty, bound and pinned ``always``.

    The binding goes on the agent's customer layer (``skill_ids``,
    ``skill_load_modes``) and is recorded in ``customer_fields`` so the seed keeps it.
    """
    from app.api.v1.helpers import resolve_company_managed_owner_id
    from app.models.agent_skill import AgentSkill
    from app.services.builtin_customer_layer import record_customer_fields
    from app.services.organizations import get_primary
    from app.services.skill_versioning import create_skill_version

    if not getattr(agent, "is_builtin", False):
        raise LessonError("only a built-in agent has a learned skill")
    skill = await find_learned_skill(db, agent.id)
    if skill is None:
        primary = await get_primary(db)
        # Owned like every company-managed row (the platform admin), never by the
        # user whose run recorded the first lesson: deleting that user must not
        # cascade to the skill. ``author_id`` stays on the SkillVersion rows only.
        skill = AgentSkill(
            user_id=await resolve_company_managed_owner_id(db),
            is_company_managed=True,
            name=skill_name(agent.name, getattr(primary, "name", None)),
            description=f"Durable lessons {agent.name} recorded for this organization. Read it each run.",
            content=_EMPTY_BODY,
            tags=[LEARNED_TAG, agent_tag(agent.id)],
            enabled=True,
            hide_from_chat=True,
        )
        db.add(skill)
        await db.flush()
        await create_skill_version(db, skill, author_id=author_id, commit_message="created empty")
    sid = str(skill.id)
    ids = [str(s) for s in (agent.skill_ids or [])]
    modes = dict(agent.skill_load_modes or {})
    if sid not in ids or modes.get(sid) != "always":
        if sid not in ids:
            agent.skill_ids = [*ids, sid]
        modes[sid] = "always"
        agent.skill_load_modes = modes
        record_customer_fields(agent, {"skill_ids", "skill_load_modes"})
    return skill


async def record_lesson(
    db: AsyncSession,
    agent: Any,
    *,
    title: str,
    text: str,
    mode: str,
    run_id: str | None,
    author_id: uuid.UUID | None,
) -> dict[str, Any]:
    """Write one lesson into *agent*'s learned skill, as a new version naming *run_id*."""
    from app.services.skill_versioning import create_skill_version

    skill = await ensure_learned_skill(db, agent, author_id=author_id)
    if is_frozen(skill):
        raise LessonError("an administrator froze this learned skill: nothing is written")
    head = _clean_title(title)
    updated = apply_lesson(skill.content, head, text, mode=mode)
    if updated == skill.content:
        return {"skill_id": str(skill.id), "changed": False}
    skill.content = updated
    message = f"{mode} {head!r}" + (f" (run {run_id})" if run_id else "")
    version = await create_skill_version(db, skill, author_id=author_id, commit_message=message)
    return {
        "skill_id": str(skill.id),
        "skill": skill.name,
        "changed": True,
        "version": version.version_number,
        "section": head,
        "mode": mode,
    }
