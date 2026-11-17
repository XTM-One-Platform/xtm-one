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

"""Teams chat messages an assignment's own run posted (``services/agent_posts.py``).

A ``new_teams_chat_message`` assignment that answers in the chat it watches
would otherwise run on its own reply. A chat message is its chat and its Graph
id; the record and the lookup go through the one store of ``agent_posts``.
"""

from __future__ import annotations

from app.services.agent_posts import is_agent_post, record_agent_post

_PROVIDER = "teams_chat"

#: The channel name of Teams sends in ``chat_reply_guard`` (its refusal metric label).
REPLY_GUARD_CHANNEL = "teams"


def chat_conversation_key(chat_id: object) -> str:
    """The ``conversation_key`` of a Teams 1:1 or group chat."""
    return f"teams-chat:{chat_id}" if chat_id else ""


def thread_conversation_key(team_id: object, channel_id: object, thread_id: object) -> str:
    """The ``conversation_key`` of a Teams channel thread, by its root message id."""
    return f"teams-thread:{team_id}:{channel_id}:{thread_id}" if thread_id else ""


async def record_teams_chat_post(chat_id: str, response: dict) -> None:
    """Record the chat message a successful Graph ``POST .../chats/{id}/messages`` created."""
    if isinstance(response, dict):
        await record_agent_post(_PROVIDER, chat_id, response.get("id"))


async def is_agent_teams_chat_post(redis, chat_id: str, message_id: str, assignment_id: object) -> bool:
    """The lookup of the Teams chat poller and the chat notifications."""
    return await is_agent_post(redis, _PROVIDER, chat_id, message_id, assignment_id=assignment_id)
