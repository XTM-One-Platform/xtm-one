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

"""Suggested opening actions for the chat welcome screen.

Shared by the native chat router and the platform (embedded) router so both
surfaces answer identically — the panel shipped in OpenCTI / OpenAEV and the
XTM One web chat should not disagree about what an agent is good for.

The answer is the agent's own ``web_suggested_prompts`` and nothing else: an
agent with none configured answers an empty list, and the panel then shows no
suggestion (or the host's own list). A generic set chosen by platform mode used
to stand in, which put prompts on an agent's welcome screen that were not the
agent's ("Summarize my unread emails" under a threat-intel agent). The resolver
is the seam for making these per-user later: the signature already takes the
requesting user, so a personalised layer slots in ahead of the agent default
without changing either router or the client.
"""

import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

# How many suggestions a caller may receive. The welcome screen wraps them as
# chips under the greeting, so an agent configured with twenty would push the
# composer off-screen.
MAX_SUGGESTIONS = 6


def _clean(raw: object) -> list[str]:
    """Keep non-empty strings only, de-duplicated, capped.

    ``web_suggested_prompts`` is a free-form JSON column filled by an LLM
    generation endpoint, so it is not guaranteed to hold a clean list of
    strings.
    """
    if not isinstance(raw, list):
        return []
    seen: set[str] = set()
    out: list[str] = []
    for item in raw:
        if not isinstance(item, str):
            continue
        text = item.strip()
        if not text or text in seen:
            continue
        seen.add(text)
        out.append(text)
        if len(out) >= MAX_SUGGESTIONS:
            break
    return out


async def resolve_chat_suggestions(
    db: AsyncSession,
    user_id: uuid.UUID,
    user_group_ids: list[str] | None,
    agent_slug: str | None,
    *,
    is_admin: bool = False,
) -> list[str]:
    """The agent's own suggested actions for ``agent_slug``; empty when it has none.

    The agent lookup goes through the same visibility filter as the agent list
    itself: a slug is guessable, and answering for an agent the caller cannot
    see would leak how someone else has configured theirs.
    """
    if not agent_slug:
        return []

    # Imported here rather than at module scope: the routers import this module
    # during app start-up, and pulling models in at import time is how circular
    # imports appear in this codebase.
    from app.api.v1.helpers import user_visible_filter
    from app.models.agent import Agent

    result = await db.execute(
        select(Agent.web_suggested_prompts).where(
            Agent.slug == agent_slug,
            Agent.enabled.is_(True),
            Agent.disable_chat.is_(False),
            user_visible_filter(Agent, user_id, user_group_ids, is_admin=is_admin),
        )
    )
    row = result.scalar_one_or_none()

    # An agent with nothing configured answers nothing: the welcome screen shows
    # no suggestion rather than prompts that are not the agent's own.
    return _clean(row)
