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

"""The Message Context block of a channel turn's system prompt.

The router hands ``run_agent_chat`` a dict built from the adapter's metadata,
and every value used to be rendered raw into the system prompt. Several are
typed by channel members - a display name, a channel, team or guild name - so
anyone who can rename themselves wrote text next to the persona. Those values
keep their section, inside a ``wrap_untrusted`` block at its end; the values
the platform derives (ids and flags) stay as they were.
"""

from __future__ import annotations

import re

from app.services.content_safety import wrap_untrusted

# Keys the block never renders: shown above as fixed fields, used by the
# router only, infrastructure, or rendered by a section of their own.
SKIPPED_KEYS = frozenset(
    {
        "platform",
        "channel_type",
        "is_direct_message",
        "platform_channel_type",
        "is_thread_continuation",
        "event_type",
        "is_threaded_reply",
        "reply_to_id",
        "group_id",
        "channel_bot_id",
        "service_url",
        "tenant_id",
        "user_aad_id",
        "conversation_type",
        "chat_type",
        "prior_conversation",
        # An agent account's turn: read by ``agent_chat`` itself.
        "response_policy",
        "account_brief",
        # The messages one agent-account turn answers: trace metadata,
        # the brief tells the agent how many.
        "coalesced_message_ids",
    }
)

# Keys whose values the platform assigns, rendered raw when the value also has
# the shape of an id or a flag. Every other key - a name, a title, anything an
# adapter adds later - is treated as typed by a channel member.
STRUCTURAL_KEYS = frozenset(
    {
        "channel_thread_id",
        "channel_id",
        "team_id",
        "thread_ts",
        "thread_name",
        "space_type",
        "is_dm",
        "bot_was_mentioned",
        "conversation_id",
        "message_id",
        # An agent account's reference to the message answered (Teams).
        "chat_id",
        "parent_message_id",
        "project_number",
        "source",
        "coalesced_count",
    }
)

_ID_SHAPE = re.compile(r"[A-Za-z0-9_.:;/@+=-]{1,256}")


def _is_structural(key: str, value: object) -> bool:
    if key not in STRUCTURAL_KEYS:
        return False
    if isinstance(value, bool | int | float):
        return True
    return isinstance(value, str) and bool(_ID_SHAPE.fullmatch(value))


def render_message_context(context: dict) -> tuple[str, bool]:
    """``(section, quarantined)``: the block, and whether it carries a fence.

    Without a member-typed value the section is exactly what it always was.
    """
    platform = context.get("platform", "unknown")
    lines = [
        f"Platform: {platform}",
        f"Channel type: {context.get('channel_type', 'unknown')}",
        f"Is direct message: {'yes' if context.get('is_direct_message', False) else 'no'}",
    ]
    member_lines: list[str] = []
    for key, value in context.items():
        if key in SKIPPED_KEYS or value in (None, ""):
            continue
        (lines if _is_structural(key, value) else member_lines).append(f"{key}: {value}")
    section = "\n── Message Context ──\n" + "\n".join(lines)
    if member_lines:
        section += "\n" + wrap_untrusted(
            "\n".join(member_lines), source=f"{platform} message metadata", stable=True
        )
    return section, bool(member_lines)
