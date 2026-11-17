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

"""What a reviewer asked of a video in its Video Studio rail, read back by an agent.

The studio rail is a chat bound to the video (``Conversation.video_project_id``,
``video_studio.studio``); every message the person sends says which scene the
playhead is on (``studio_scene``). That scene is stored on the user message
(:data:`STUDIO_SCENE_META_KEY`, :func:`with_studio_scene`), so a remark keeps
the scene it was about after the turn that answered it.

:func:`studio_feedback` is what ``get_video(fields=["feedback"])`` shows: each
request the caller made in their own studio conversations on the video - when,
on which scene, the verbatim text - and the revisions the turn that answered it
saved (read from the stored tool trace of its replies, :func:`saved_revisions`).

Read-only. The video must be one the caller can open (the tool loads it with
``projects.load_visible_project``), and only the caller's own one-person threads
are read (``workspace_knowledge.private_thread_clause``): another person's
studio conversation on a shared video stays theirs, as
``read_conversation_history`` keeps it. The studio has no comment threads and
no resolved state: the answer says what is stored, nothing more.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import datetime
from typing import TYPE_CHECKING, Any

from sqlalchemy import and_, select

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from app.models.user import User
    from app.models.video_studio import VideoProject

#: The key of a studio user message's metadata holding the scene on screen.
STUDIO_SCENE_META_KEY = "studio_scene"
#: The longest scene id kept (spec scene ids are short; a client may send anything).
_MAX_SCENE_CHARS = 120
#: The video tools whose answer names the revision they saved.
_SAVING_TOOLS = frozenset({"create_video", "edit_video"})
#: Characters of one request shown before it is cut, and of the whole answer.
_REQUEST_CHARS = 2_000
FEEDBACK_CHARS = 12_000


def with_studio_scene(metadata_json: str | None, scene: object) -> str | None:
    """*metadata_json* with the scene the studio playhead was on, when the client sent one."""
    if not isinstance(scene, str) or not scene.strip():
        return metadata_json
    meta: dict[str, Any] = {}
    if metadata_json:
        try:
            parsed = json.loads(metadata_json)
        except (TypeError, ValueError):
            parsed = None
        if isinstance(parsed, dict):
            meta = parsed
    meta[STUDIO_SCENE_META_KEY] = scene.strip()[:_MAX_SCENE_CHARS]
    return json.dumps(meta)


def saved_revisions(meta: dict[str, Any], video_id: str) -> list[int]:
    """The revisions of *video_id* the tool calls of a stored reply saved, in call order."""
    trace = meta.get("tool_call_trace")
    if not isinstance(trace, list):
        return []
    pattern = re.compile(rf"\(`{re.escape(video_id)}`\), revision (\d+)")
    revisions: list[int] = []
    for entry in trace:
        if not isinstance(entry, dict) or entry.get("name") not in _SAVING_TOOLS or not entry.get("success", True):
            continue
        found = pattern.search(str(entry.get("output") or ""))
        if found and int(found.group(1)) not in revisions:
            revisions.append(int(found.group(1)))
    return revisions


@dataclass
class _Request:
    conversation_id: str
    at: datetime | None
    scene: str | None
    text: str
    revisions: list[int] = field(default_factory=list)

    def render(self) -> str:
        when = f"{self.at:%Y-%m-%d %H:%M} UTC" if isinstance(self.at, datetime) else "time unknown"
        scene = f"scene {self.scene}" if self.scene else "scene not recorded"
        text = self.text.strip()
        if len(text) > _REQUEST_CHARS:
            text = f"{text[:_REQUEST_CHARS].rstrip()} [... {len(text) - _REQUEST_CHARS} more characters]"
        saved = "saved revision " + ", ".join(str(r) for r in self.revisions) if self.revisions else "saved no revision"
        return f"- {when}, {scene}, conversation `{self.conversation_id}`, {saved}:\n  " + text.replace("\n", "\n  ")


async def _requests(db: AsyncSession, user: User, project: VideoProject) -> tuple[list[_Request], int]:
    from app.models.chat import Conversation, Message
    from app.services.builtin_tools.conversation_history import listed_message_filter
    from app.services.chat_messages import message_order_asc, parse_message_metadata
    from app.services.workspace_knowledge import private_thread_clause

    conversations = list(
        (
            await db.execute(
                select(Conversation.id)
                .where(
                    Conversation.user_id == user.id,
                    Conversation.video_project_id == project.id,
                    private_thread_clause(),
                )
                .order_by(Conversation.created_at.asc())
            )
        )
        .scalars()
        .all()
    )
    video_id = str(project.id)
    requests: list[_Request] = []
    for conversation_id in conversations:
        rows = (
            await db.execute(
                select(Message)
                .where(and_(listed_message_filter(conversation_id), Message.role.in_(("user", "assistant"))))
                .order_by(*message_order_asc())
            )
        ).scalars()
        current: _Request | None = None
        for message in rows:
            meta = parse_message_metadata(message.metadata_json)
            if message.role == "user":
                scene = meta.get(STUDIO_SCENE_META_KEY)
                current = _Request(
                    str(conversation_id),
                    message.created_at,
                    scene if isinstance(scene, str) else None,
                    message.content or "",
                )
                requests.append(current)
            elif current is not None:
                current.revisions.extend(r for r in saved_revisions(meta, video_id) if r not in current.revisions)
    requests.sort(key=lambda r: r.at.timestamp() if isinstance(r.at, datetime) else 0.0)
    return requests, len(conversations)


async def studio_feedback(
    db: AsyncSession, user: User, project: VideoProject, *, scenes: set[str] | None = None
) -> str:
    """``get_video``'s feedback part: the caller's studio requests on *project*, oldest first, bounded."""
    from app.services.content_safety import wrap_untrusted

    requests, conversation_count = await _requests(db, user, project)
    scope = (
        "Studio feedback (the requests made in your own studio conversations on this video; another person's "
        "studio conversation stays theirs; the studio keeps no comment threads or resolved state)"
    )
    if scenes is not None:
        requests = [r for r in requests if r.scene in scenes]
        scope += f", on scene {', '.join(sorted(scenes))}"
    if not requests:
        return f"{scope}: none" + ("." if conversation_count else " - no studio conversation of yours on it.")
    shown: list[str] = []
    used = 0
    for request in reversed(requests):
        line = request.render()
        if shown and used + len(line) > FEEDBACK_CHARS:
            break
        shown.append(line)
        used += len(line)
    shown.reverse()
    left = len(requests) - len(shown)
    header = (
        f"{scope}: {len(requests)} request(s) in {conversation_count} conversation(s), oldest first"
        + (f"; the {left} oldest are not shown (read_conversation_history reads them whole)" if left else "")
        + "."
    )
    return header + "\n" + wrap_untrusted("\n".join(shown), source="Video Studio conversations")
