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

"""The agent that stands in for the General Assistant.

Settings > AI and models > General Assistant > "Link to Agent" stores
``GeneralAssistantConfig.linked_agent_id``: an agent that answers wherever the
General Assistant would, with its own model, persona, tools and bindings.

* ``POST /chat/conversations`` binds a web conversation created without an
  agent to it, so the conversation is that agent's from its first message.
* An unbound conversation started outside the web chat (a Slack or Teams
  thread, any other channel) follows the link turn by turn, wherever it is
  continued (:func:`turn_stand_in`): the channel router answers its messages
  with it, so does the web chat when the conversation is continued there,
  and so does its wake. The conversation stays unbound: the General
  Assistant's channel settings keep deciding where it answers, the agent's
  own do not, and a change of link is followed at the next turn. A web
  conversation created before the link keeps the General Assistant, on every
  surface.

The General Assistant serves every user, so the agent must reach every user:
enabled, open to chat, company-managed and not narrowed with
``access_restricted``. A link that stops meeting the rule (the agent disabled,
made private, restricted) is not followed, and the General Assistant answers
with its own configuration, as it did before the link; so it does for a user
who may not chat with the agent (:func:`may_chat_with`). The edition gate is
asked before any of this, for the General Assistant and an agent alike.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterable
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.agent import Agent
from app.models.general_assistant_config import GeneralAssistantConfig

#: The web chat's own conversations: one created there without an agent
#: keeps the General Assistant.
WEB_CHAT_CHANNEL = "web"


def stand_in_refusal(agent: Agent) -> str | None:
    """Why *agent* cannot stand in for the General Assistant, ``None`` when it can."""
    if not agent.is_company_managed:
        return "is not company-managed"
    if agent.access_restricted:
        return "is restricted to the people granted access"
    if not agent.enabled:
        return "is disabled"
    if agent.disable_chat:
        return "has chat disabled"
    return None


async def general_assistant_stand_in(db: AsyncSession) -> Agent | None:
    """The linked agent when it may answer for the General Assistant, else ``None``.

    One read, on every inbound channel message and conversation created.
    """
    agent = (
        await db.execute(
            select(Agent)
            .join(GeneralAssistantConfig, GeneralAssistantConfig.linked_agent_id == Agent.id)
            .limit(1)
        )
    ).scalar_one_or_none()
    if agent is None or stand_in_refusal(agent) is not None:
        return None
    return agent


def follows_the_link(agent_id: object, conversation_channel: str | None) -> bool:
    """Whether a conversation's General Assistant turns go to the agent linked to it.

    Unbound and started outside the web chat; ``None`` reads as the web chat.
    """
    return agent_id is None and (conversation_channel or WEB_CHAT_CHANNEL) != WEB_CHAT_CHANNEL


async def may_chat_with(db: AsyncSession, agent: Agent, user_id: uuid.UUID | str) -> bool:
    """Whether *user_id* may chat with *agent*: enabled, open to chat, visible to them.

    Visible by the three tiers (``user_visible_filter``). A link that
    qualifies is company-managed and unrestricted, so every user sees it: the
    check keeps the turn's user the one it is asked for.
    """
    if not agent.enabled or agent.disable_chat:
        return False
    from app.api.v1.helpers import get_user_group_ids, user_visible_filter

    uid = user_id if isinstance(user_id, uuid.UUID) else uuid.UUID(str(user_id))
    visible = (
        await db.execute(
            select(Agent.id).where(
                Agent.id == agent.id,
                user_visible_filter(Agent, uid, await get_user_group_ids(db, uid)),
            )
        )
    ).scalar_one_or_none()
    return visible is not None


async def turn_stand_in(
    db: AsyncSession,
    *,
    agent_id: object,
    conversation_channel: str | None,
    user_id: uuid.UUID | str | None,
) -> Agent | None:
    """The agent that answers a turn of a conversation in the General Assistant's place.

    The ONE answer the channel router, the web chat's sends, a parent wake
    and the conversation responses ask, so a conversation is answered by the
    same agent wherever it is continued. ``None``: the conversation's own
    agent answers (*agent_id*), or the General Assistant itself - a web
    conversation, no link that qualifies, or a user who may not chat with
    the linked agent.
    """
    if user_id is None or not follows_the_link(agent_id, conversation_channel):
        return None
    agent = await general_assistant_stand_in(db)
    if agent is None or not await may_chat_with(db, agent, user_id):
        return None
    return agent


async def conversation_stand_ins(db: AsyncSession, conversations: Iterable[Any]) -> dict[Any, Agent]:
    """:func:`turn_stand_in` of each conversation of a page, keyed by its id: one read.

    A link that qualifies is enabled, open to chat, company-managed and
    unrestricted, so the three tiers' company tier shows it to every owner:
    :func:`may_chat_with` answers yes for each of them, and the page asks
    nobody on their own (the conversation list stays batched).
    """
    following = [
        c for c in conversations if c.user_id is not None and follows_the_link(c.agent_id, c.channel)
    ]
    if not following:
        return {}
    agent = await general_assistant_stand_in(db)
    if agent is None:
        return {}
    return {c.id: agent for c in following}
