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

"""Built-in phone tools - an agent reaches its user's linked phones.

Gated behind the ``phone`` built-in tool id:

* ``notify_my_phone`` - push a short notification to the phones the user
  linked (``models/mobile_device.py``), and put it in their notification bell.

It reaches the phones of the person the agent acts for, and nobody else's: the
notification is written for that user and pushed by the same path as every bell
notification (``services/mobile_push.py``), quiet hours and opt-outs included.
Bounded on every side: a short title and message, a link only to a page of the
platform, a few per run and a budget per person per hour.

Reminders need no tool here: a ``schedule_work_item`` reminder reaches the bell,
and so the phone, when it fires. Approvals neither: a run waiting on the user
pushes its question with Approve / Reject buttons.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from app.core.utils import format_exception_message, safe_uuid
from app.tools.base import CallableToolExecutor, ToolDefinition, ToolResult

from ._shared import logger

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

_SOURCE = "builtin:phone"

MAX_TITLE_CHARS = 80
MAX_MESSAGE_CHARS = 300
#: Notifications one agent turn or run may send.
MAX_PER_RUN = 3
#: Notifications per person per hour, every agent together.
MAX_PER_USER_PER_HOUR = 20


def _error(message: str) -> ToolResult:
    return ToolResult(tool_call_id="", content=message, is_error=True)


def build_phone_tools(
    db: AsyncSession | None,
    user_id: str,
    *,
    agent_name: str | None = None,
) -> tuple[list[ToolDefinition], CallableToolExecutor]:
    """Build the phone tools, acting for ``user_id``."""
    del db  # its own short-lived session (#1973)
    sent = {"count": 0}

    async def _notify_my_phone(title: str, message: str, path: str | None = None, **_kw: object) -> ToolResult:
        from fastapi import HTTPException
        from sqlalchemy import select

        from app.core.database import async_session
        from app.core.internal_paths import is_internal_path
        from app.core.rate_limit import enforce_rate_limit
        from app.models.mobile_device import MobileDevice
        from app.services.notify import notify_web_user

        user_uuid = safe_uuid(user_id)
        if user_uuid is None:
            return _error("Phone notifications can only reach a signed-in user.")
        title = " ".join(str(title or "").split())
        message = " ".join(str(message or "").split())
        if not title or not message:
            return _error("title and message are both required.")
        if len(title) > MAX_TITLE_CHARS or len(message) > MAX_MESSAGE_CHARS:
            return _error(f"Keep the title under {MAX_TITLE_CHARS} characters and the message under {MAX_MESSAGE_CHARS}.")
        if path and not is_internal_path(str(path).strip()):
            return _error(
                "path must be a page of this platform's web app starting with a single '/' (for example "
                "/runs); web addresses, other schemes and /api paths are refused."
            )
        if sent["count"] >= MAX_PER_RUN:
            return _error(f"At most {MAX_PER_RUN} phone notifications per run: put the rest in your answer.")
        try:
            await enforce_rate_limit(
                None, "phone_notify", limit=MAX_PER_USER_PER_HOUR, window_seconds=3600, bucket=str(user_uuid)
            )
        except HTTPException:
            return _error("The user received many phone notifications this hour; put this one in your answer instead.")
        try:
            async with async_session() as own_db:
                has_phone = (
                    await own_db.execute(
                        select(MobileDevice.id)
                        .where(MobileDevice.user_id == user_uuid, MobileDevice.push_enabled.is_(True))
                        .limit(1)
                    )
                ).scalar_one_or_none()
                if has_phone is None:
                    return _error(
                        "The user has no linked phone with notifications on. Tell them in your answer instead; "
                        "they can turn notifications on from the mobile companion."
                    )
                await notify_web_user(
                    own_db,
                    str(user_uuid),
                    title=title,
                    summary=message,
                    source_label=agent_name or "Agent",
                    extra_data={"notification_source": "agent", **({"path": str(path).strip()} if path else {})},
                )
                await own_db.commit()
        except Exception as exc:
            logger.error("notify_my_phone failed: %s", format_exception_message(exc), exc_info=True)
            return _error("Error: the notification could not be sent. Please try again.")
        sent["count"] += 1
        return ToolResult(
            tool_call_id="",
            content="Sent to the user's phone, and added to their notifications.",
        )

    executor = CallableToolExecutor()
    executor.register("notify_my_phone", _notify_my_phone)
    definitions = [
        ToolDefinition(
            name="notify_my_phone",
            description=(
                "Send a short notification to the phone(s) the user linked to XTM One, and to their notification "
                "bell. Use it when something deserves the user's attention while they are away from the app: a "
                "critical finding, a run that needs them, the result they asked to be told about. Not for routine "
                "progress, and never for the answer to a question they are reading right now. To remind them "
                "later, use schedule_work_item instead: its reminder reaches the phone when it is due. "
                f"Title up to {MAX_TITLE_CHARS} characters, message up to {MAX_MESSAGE_CHARS}; 'path' (optional) "
                "is a page of the platform the notification opens, like /runs/ followed by a run's id, or /chat."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "title": {"type": "string", "description": "What happened, in a few words."},
                    "message": {"type": "string", "description": "One or two sentences: what it means, what to do."},
                    "path": {"type": "string", "description": "A platform page to open, starting with '/'."},
                },
                "required": ["title", "message"],
            },
            source=_SOURCE,
        )
    ]
    return definitions, executor
