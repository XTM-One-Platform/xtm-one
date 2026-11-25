# SPDX-License-Identifier: Apache-2.0

"""``list_my_notifications``: an agent reads the bell of the person it acts for (#2198).

Registered with the coordination tools (``builtin_tools/work_items.py``), next to
``send_decision_card``: a desk that posted a decision card reads whether it reached
the person's bell. The answer is ``services/notification_feed.build_feed`` for that
person, the bell's own read: the same entries, order and visibility. Nothing is
marked read. Titles and summaries are other people's words, returned inside
untrusted-content delimiters.
"""

from __future__ import annotations

import json
import logging
import uuid
from collections.abc import Awaitable, Callable

from app.core.utils import format_exception_message
from app.tools.base import ToolDefinition

logger = logging.getLogger(__name__)

TOOL_NAME = "list_my_notifications"
#: The most entries one call answers (the bell's own window is 20).
MAX_LIMIT = 50


def notifications_definition() -> ToolDefinition:
    """The tool's definition, in the coordination group."""
    return ToolDefinition(
        name=TOOL_NAME,
        description=(
            "Read the notification bell of the person you act for, as they see it: waiting "
            "decisions first, then the newest, each read or unread. Nothing is marked read."
        ),
        input_schema={
            "type": "object",
            "properties": {"limit": {"type": "integer", "description": f"1 to {MAX_LIMIT} (default 20)"}},
        },
        source="builtin:coordination",
    )


def build_list_my_notifications(user_id: str | None) -> Callable[..., Awaitable[str]]:
    """The handler of ``list_my_notifications`` acting for *user_id*."""

    async def _list(limit: int = 20) -> str:
        from app.core.database import async_session
        from app.models.user import User
        from app.services.content_safety import wrap_untrusted
        from app.services.notification_feed import build_feed

        try:
            uid = uuid.UUID(str(user_id))
        except (TypeError, ValueError):
            return "This run acts for nobody, so there is no notification bell to read."
        try:
            bounded = max(1, min(int(limit or 20), MAX_LIMIT))
        except (TypeError, ValueError):
            bounded = 20
        try:
            async with async_session() as db:
                user = await db.get(User, uid)
                if user is None or not user.is_active:
                    return "This run acts for nobody, so there is no notification bell to read."
                feed = await build_feed(db, user, limit=bounded)
        except Exception as exc:
            logger.error("list_my_notifications failed: %s", format_exception_message(exc), exc_info=True)
            return "Error: the notifications could not be read. Please try again."
        items = feed.get("items") or []
        if not items:
            return "The notification bell is empty."
        head = f"{len(items)} notification(s), {feed.get('unread_count', 0)} unread, waiting decisions first:\n"
        return head + wrap_untrusted(json.dumps(items, default=str, ensure_ascii=False), "Notification bell")

    return _list


__all__ = ["MAX_LIMIT", "TOOL_NAME", "build_list_my_notifications", "notifications_definition"]
