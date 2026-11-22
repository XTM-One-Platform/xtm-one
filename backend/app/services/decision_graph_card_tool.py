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

"""``send_decision_card``: an agent DMs the decision card of a waiting work item (#1156).

Next to ``decision_graph_cards``, the Microsoft Graph sender it calls: the tool
is registered with the coordination tools (``builtin_tools/work_items.py``),
which stay vendor-neutral (invariant 18).

A fleet that tells a person about a decision from an agent run (a
``work_item.needs_human`` listener) posts the PLATFORM card, not a card of its
own: one signed ``/decide/<token>`` button per option, turned into the
decided card once the decision closes from any channel
(``decision_graph_cards.send_agent_card``). The send is a tool call of the
calling agent through its own Microsoft integration (``agent_graph_caller``:
bindings, scope, deny list and approval gate apply), and it needs no
``payload.notify``, so the item's end sends no text.
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import Awaitable, Callable
from types import SimpleNamespace
from typing import Any

from app.core.utils import format_exception_message
from app.tools.base import ToolDefinition

logger = logging.getLogger(__name__)

TOOL_NAME = "send_decision_card"

#: ``(work_item_id) -> item dict or None``, read with the agent's own access.
ItemReader = Callable[[str], Awaitable[dict[str, Any] | None]]


def decision_card_definition() -> ToolDefinition:
    """The tool's definition, in the coordination group."""
    return ToolDefinition(
        name=TOOL_NAME,
        description=(
            "Post the platform decision card of a needs_human work item to a Teams chat or "
            "channel through your Microsoft integration: a signed answer button per option, "
            "edited to the decided state when answered anywhere. No end-of-item text."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "work_item_id": {"type": "string"},
                "chat_id": {"type": "string", "description": "A Teams chat (48:notes: your own)"},
                "team_id": {"type": "string", "description": "With channel_id, a channel"},
                "channel_id": {"type": "string"},
                "integration": {"type": "string", "description": "Your Microsoft integration, id or name"},
            },
            "required": ["work_item_id", "integration"],
        },
        source="builtin:coordination",
    )


def build_send_decision_card(
    agent_id: str,
    user_id: str | None,
    read_item: ItemReader,
    *,
    caller: Any = None,
) -> Callable[..., Awaitable[str]]:
    """The handler of ``send_decision_card`` for *agent_id* acting for *user_id*."""

    async def _send(
        work_item_id: str,
        integration: str,
        chat_id: str = "",
        team_id: str = "",
        channel_id: str = "",
    ) -> str:
        from app.core.database import async_session
        from app.models.agent import Agent
        from app.services import decision_graph_cards as cards
        from app.services import work_item_notify as notify

        chat_id, team_id, channel_id = (chat_id or "").strip(), (team_id or "").strip(), (channel_id or "").strip()
        if bool(chat_id) == bool(team_id and channel_id) or (bool(team_id) != bool(channel_id)):
            return "Error: name one target: chat_id, or team_id with channel_id. Nothing was sent."
        if not (integration or "").strip():
            return "Error: integration (your Microsoft integration, id or name) is required. Nothing was sent."
        if not user_id:
            return "Error: this run acts for nobody, so no integration can send for it. Nothing was sent."
        try:
            item = await read_item(work_item_id)
        except Exception as exc:
            logger.error("send_decision_card: reading the item failed: %s", format_exception_message(exc))
            return "Error: failed to read the work item. Nothing was sent."
        if item is None:
            return f"Work item `{work_item_id}` not found or not accessible. Nothing was sent."
        target = {"team_id": team_id, "channel_id": channel_id} if team_id else {"chat_id": chat_id}
        try:
            async with async_session() as db:
                agent = await db.get(Agent, uuid.UUID(str(agent_id)))
                message_id, already = await cards.send_agent_card(
                    db,
                    SimpleNamespace(
                        id=uuid.UUID(str(item["id"])),
                        correlation_key=item.get("correlation_key"),
                        title=item.get("title"),
                    ),
                    agent_id=uuid.UUID(str(agent_id)),
                    agent_name=str(getattr(agent, "name", "") or "an agent"),
                    user_id=uuid.UUID(str(user_id)),
                    integration=integration.strip(),
                    target=target,
                    caller=caller,
                )
        except notify.NotifyError as exc:
            return f"Error: {exc}. Nothing was sent."
        except Exception as exc:
            logger.error("send_decision_card failed: %s", format_exception_message(exc), exc_info=True)
            return "Error: the decision card could not be sent. Please try again."
        where = "the channel" if team_id else ("your own chat" if chat_id == notify.SELF_CHAT_ID else "the chat")
        if already:
            return (
                f"The decision card of `{item['id']}` was already in {where} (message {message_id}): "
                "nothing was sent again."
            )
        return (
            f"Sent the decision card of `{item['id']}` to {where} (message {message_id}). Each option "
            "button answers it for its recipient; the message turns into the decided card once answered."
        )

    return _send


__all__ = ["TOOL_NAME", "build_send_decision_card", "decision_card_definition"]
