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

"""``record_learned_lesson``: a built-in agent writes a durable lesson into its learned skill (#914).

Offered to every built-in agent in XTM One mode. The rules and the storage are
``services/builtin_learned_skill.py``'s: one section per topic, bounded, every
change a skill version naming the run, refused when an administrator froze
the skill. A change of the organization profile never goes here: it goes
through ``propose_organization_context_change``.
"""

from __future__ import annotations

import json
import uuid

from app.tools.base import CallableToolExecutor, ToolDefinition

from ._shared import logger

LEARNED_LESSON_TOOL = "record_learned_lesson"
_SOURCE = "builtin:learned_skill"


def offers_learned_lesson_tool(*, is_builtin: bool) -> bool:
    """Whether a turn or run of this agent is offered the tool (built-ins, XTM One mode only)."""
    from app.core.config import settings

    if settings.is_copilot_like:
        return False
    return bool(is_builtin)


def build_learned_lesson_tool(
    *, agent_id: str, user_id: str | None, run_id: str | None = None
) -> tuple[list[ToolDefinition], CallableToolExecutor]:
    """The lesson tool of built-in agent *agent_id*; versions name *run_id*."""

    async def _record(title: str = "", text: str = "", mode: str = "append") -> str:
        from app.core.database import async_session
        from app.models.agent import Agent
        from app.services import builtin_learned_skill as learned

        try:
            agent_uuid = uuid.UUID(str(agent_id))
            author = uuid.UUID(str(user_id)) if user_id else None
        except ValueError:
            return "Refused: no agent to record the lesson for."
        try:
            async with async_session() as db:
                agent = await db.get(Agent, agent_uuid)
                if agent is None or not agent.is_builtin:
                    return "Refused: only a built-in agent keeps a learned skill."
                outcome = await learned.record_lesson(
                    db, agent, title=title, text=text, mode=mode or "append", run_id=run_id, author_id=author
                )
                await db.commit()
                return json.dumps(outcome, ensure_ascii=False)
        except learned.LessonError as exc:
            return f"Refused: {exc}. Nothing was written."
        except Exception:
            logger.warning("Learned lesson write failed", exc_info=True)
            return "Error: the lesson could not be written right now. Say so; nothing was written."

    executor = CallableToolExecutor()
    executor.register(LEARNED_LESSON_TOOL, _record)
    definition = ToolDefinition(
        name=LEARNED_LESSON_TOOL,
        description=(
            "Write a durable lesson for this organization into your learned skill (an asset owner, a false "
            "positive pattern, a routing rule). Versioned with this run. Profile changes: propose them instead."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "title": {"type": "string", "description": "Section title, one topic."},
                "text": {"type": "string", "description": "The lesson, at most 1500 characters."},
                "mode": {"type": "string", "enum": ["append", "revise", "remove"]},
            },
            "required": ["title"],
        },
        source=_SOURCE,
        priority=0,
    )
    return [definition], executor
