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

"""Whether tool approvals are in use on this platform, and who asks.

The approval gate holds a tool call only for an agent that asks before running
tools: its own ``approve_tool_calls``, else the install default
(``hitl_whitelist.agent_asks``). A tool's place on the whitelist says what
would happen IF an agent asked, not that anything asks - so every screen that
labels a tool "Asks first", the profile's personal controls and the security
audit read the answer here, and a platform where no agent asks shows no
approval anywhere.

Read-only and advisory: nothing here decides a tool call.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

from sqlalchemy import exists, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.services.hitl_whitelist import agent_asks, install_default_asks

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class AgentApproval:
    """One enabled agent and what its approval setting resolves to."""

    id: str
    name: str
    own: bool | None
    asks: bool


@dataclass(frozen=True)
class ApprovalUsage:
    """The platform's approval switches and what they add up to."""

    # ``tool_approvals_enabled`` off suspends every approval for everyone.
    suspended: bool
    # ``approve_tool_calls_default``: what an agent that sets nothing does.
    asks_by_default: bool
    # Whether the General Assistant is available (chat licensed and AI on); it
    # sets nothing of its own and follows the install default.
    general_assistant_available: bool
    agents: list[AgentApproval] = field(default_factory=list)

    @property
    def asking_agents(self) -> list[AgentApproval]:
        return [a for a in self.agents if a.asks]

    @property
    def general_assistant_asks(self) -> bool:
        return self.general_assistant_available and self.asks_by_default

    @property
    def configured(self) -> bool:
        """Something is set to ask, whether or not approvals are suspended."""
        return bool(self.asking_agents) or self.general_assistant_asks

    @property
    def in_use(self) -> bool:
        """A tool call can pause for a person on this platform right now."""
        return not self.suspended and self.configured

    def to_payload(self) -> dict:
        return {
            "in_use": self.in_use,
            "configured": self.configured,
            "suspended": self.suspended,
            "asks_by_default": self.asks_by_default,
            "general_assistant_available": self.general_assistant_available,
            "general_assistant_asks": self.general_assistant_asks,
            "asking_agents": [
                {"id": a.id, "name": a.name, "follows_default": a.own is None}
                for a in self.asking_agents
            ],
            "agents_following_default": sum(1 for a in self.agents if a.own is None),
            "agents_set_to_ask": sum(1 for a in self.agents if a.own is True),
            "agents_set_not_to_ask": sum(1 for a in self.agents if a.own is False),
        }


def summarize_approval_usage(
    *,
    approvals_enabled: bool | None,
    install_default: bool | None,
    agents: list[tuple[object, str, bool | None]],
    general_assistant_available: bool,
) -> ApprovalUsage:
    """Fold the stored switches and the enabled agents into one answer.

    ``approvals_enabled`` and ``install_default`` are the stored values; no
    settings row reads as approvals available and the install default off.
    """
    asks_by_default = install_default_asks(install_default)
    return ApprovalUsage(
        suspended=approvals_enabled is False,
        asks_by_default=asks_by_default,
        general_assistant_available=general_assistant_available,
        agents=[
            AgentApproval(
                id=str(agent_id),
                name=name,
                own=own,
                asks=agent_asks(own, asks_by_default),
            )
            for agent_id, name, own in agents
        ],
    )


async def _stored_switches(db: AsyncSession) -> tuple[bool | None, bool | None]:
    from app.models.platform_settings import PlatformSettings

    row = (
        await db.execute(
            select(
                PlatformSettings.tool_approvals_enabled,
                PlatformSettings.approve_tool_calls_default,
            )
            .order_by(PlatformSettings.created_at.asc(), PlatformSettings.id.asc())
            .limit(1)
        )
    ).first()
    return (row[0], row[1]) if row is not None else (None, None)


async def approvals_in_use(db: AsyncSession) -> bool:
    """``load_approval_usage(db).in_use`` without loading every agent.

    Read by ``GET /tool-approvals``, which every screen showing an approval
    state calls; an unreadable answer is "in use", so a read error never hides
    a control a person may need. A savepoint, so a failed read cannot roll
    back, and expire, what the caller already loaded.
    """
    from app.api.v1.platform import is_chat_enabled
    from app.models.agent import Agent

    try:
        async with db.begin_nested():
            approvals_enabled, install_default = await _stored_switches(db)
            if approvals_enabled is False:
                return False
            asks_by_default = install_default_asks(install_default)
            asking = Agent.approve_tool_calls.is_(True)
            if asks_by_default:
                asking = or_(asking, Agent.approve_tool_calls.is_(None))
            if await db.scalar(select(exists().where(Agent.enabled.is_(True), asking))):
                return True
            return asks_by_default and await is_chat_enabled(db)
    except Exception:
        logger.exception("Could not tell whether tool approvals are in use - reporting them in use")
        return True


async def load_approval_usage(db: AsyncSession) -> ApprovalUsage:
    """Read the switches and every enabled agent's setting."""
    from app.api.v1.platform import is_chat_enabled
    from app.models.agent import Agent

    approvals_enabled, install_default = await _stored_switches(db)
    agents = (
        await db.execute(
            select(Agent.id, Agent.name, Agent.approve_tool_calls)
            .where(Agent.enabled.is_(True))
            .order_by(Agent.name)
        )
    ).all()
    return summarize_approval_usage(
        approvals_enabled=approvals_enabled,
        install_default=install_default,
        agents=[(agent_id, name, own) for agent_id, name, own in agents],
        general_assistant_available=await is_chat_enabled(db),
    )
