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

"""Whether a ``record_browser_demo`` the interrupted segment started created its recording.

A resumed turn's note names the calls the cut segment started and never saw
answered (``turn_checkpoint.in_flight_calls``) as "may have taken effect". A
recording call stores its take, then waits minutes for it: a worker stop in
that wait leaves a recording the resumed turn cannot see, and a retry under
another name queued a second take. The answer is in ``video_takes``: a
recording of the same user and conversation (none for an assignment run),
created within ``SAME_TAKE_WINDOW_SECONDS``, with the name the call gave it.
The verdict is written on the in-flight call (``landed``, ``outcome``) so the
note names the recording and its status (``turn_checkpoint.resume_note``).

Anything this module cannot tell (no user, an argument the replay cut, a read
error) is left without a verdict: the note keeps saying the call may have
taken effect.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import or_, select

from app.core.utils import format_exception_message, safe_uuid

logger = logging.getLogger(__name__)

#: The recording write told apart here.
RECORD_TOOL = "record_browser_demo"
#: The name ``record_browser_demo`` gives a take asked without one.
DEFAULT_TAKE_NAME = "Demo recording"
#: What ``record_browser_demo`` appends to the copied name of a re-record without a name.
RERECORDED_SUFFIX = " (re-recorded)"
#: What ``turn_checkpoint._cut_arguments`` appends to a string it cut.
_CUT_MARK = "[cut for the replay:"
#: Recent recordings of the user and conversation compared per call.
_SCANNED = 20


def _pending(in_flight: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [c for c in in_flight if c.get("name") == RECORD_TOOL and not c.get("answered")]


def name_matches(arguments: dict[str, Any], take_name: str) -> bool | None:
    """Whether a take named *take_name* is the one a call with *arguments* stored; ``None``: cannot tell.

    The name given, stripped; without one, the copied name of the recording a
    ``from_recording_id`` call records again (any name ending in
    :data:`RERECORDED_SUFFIX`), else :data:`DEFAULT_TAKE_NAME`.
    """
    given = arguments.get("name")
    if isinstance(given, str) and _CUT_MARK in given:
        return None
    wanted = str(given or "").strip()
    if wanted:
        return take_name == wanted
    if str(arguments.get("from_recording_id") or "").strip():
        return take_name.endswith(RERECORDED_SUFFIX)
    return take_name == DEFAULT_TAKE_NAME


def outcome_line(take: Any, created: bool) -> str:
    """The note's words for a verdict on one call."""
    if not created:
        return "it created no recording (none of yours with its name in the last minutes): record it again"
    stamp = take.created_at.isoformat(timespec="seconds") if getattr(take, "created_at", None) else "?"
    return (
        f"it created recording {take.id} ({take.status}, created {stamp}): "
        f'get_video_recording(recording_id="{take.id}") reads it; do not record it again'
    )


def verdict(arguments: dict[str, Any], takes: list[Any]) -> tuple[bool, Any] | None:
    """``(created, take)`` for a call with *arguments* among *takes* (newest first); ``None``: cannot tell."""
    for take in takes:
        matched = name_matches(arguments, str(getattr(take, "name", "") or ""))
        if matched is None:
            return None
        if matched:
            return True, take
    return False, None


async def judge_for_resume(
    in_flight: list[dict[str, Any]],
    conversation_id: object,
    user_id: object,
    *,
    now: datetime | None = None,
    session_factory: Any = None,
) -> int:
    """Write ``landed`` and ``outcome`` on each unanswered ``record_browser_demo`` of *in_flight*.

    Returns how many calls got a verdict; a read failure is logged and judges
    nothing.
    """
    calls = _pending(in_flight)
    uid = safe_uuid(str(user_id)) if user_id else None
    if not calls or uid is None:
        return 0
    from app.models.video_studio import VideoTake
    from app.services.builtin_tools.video_studio import SAME_TAKE_WINDOW_SECONDS

    if session_factory is None:
        from app.core.database import async_session as session_factory
    conversation = safe_uuid(str(conversation_id)) if conversation_id else None
    cutoff = (now or datetime.now(UTC)) - timedelta(seconds=SAME_TAKE_WINDOW_SECONDS)
    judged = 0
    try:
        async with session_factory() as session:
            # An assignment run's tools store takes without a conversation even when its task has one.
            scope = (
                VideoTake.conversation_id.is_(None)
                if conversation is None
                else or_(VideoTake.conversation_id == conversation, VideoTake.conversation_id.is_(None))
            )
            result = await session.execute(
                select(VideoTake)
                .where(VideoTake.user_id == uid, scope, VideoTake.created_at >= cutoff)
                .order_by(VideoTake.created_at.desc())
                .limit(_SCANNED)
            )
            takes = list(result.scalars().all())
        for call in calls:
            raw = call.get("arguments")
            found = verdict(raw if isinstance(raw, dict) else {}, takes)
            if found is None:
                continue
            created, take = found
            call["landed"] = created
            call["outcome"] = outcome_line(take, created)
            judged += 1
    except Exception as exc:
        logger.warning(
            "Could not tell whether the interrupted recordings were created: %s", format_exception_message(exc)
        )
    return judged
