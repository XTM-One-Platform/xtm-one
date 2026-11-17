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

"""The platform events of a Video Studio job that ended: ``video.*`` assignment triggers.

A render, a scene preview, an AI shot or an AI track that ends ready or failed
raises one platform event (``services/platform_events.py``), so an assignment
subscribed to it reacts at once instead of polling ``get_video_jobs`` on a
schedule:

========================  ==============================================
job                       events
========================  ==============================================
whole render              ``video.render.finished`` / ``video.render.failed``
scene preview             ``video.scene_preview.finished`` / ``video.scene_preview.failed``
AI shot                   ``video.clip.ready`` / ``video.clip.failed``
AI track                  ``video.music.ready`` / ``video.music.failed``
========================  ==============================================

**Where.** :func:`schedule` is called wherever the worker is done with a job,
next to the conversation wake (``wake.schedule``): the run returned, or the
engine gave up on it. It reads the row and publishes only an ended job (ready
or failed); a job handed back, retried or held is still in flight and raises
nothing. A cancelled job raises nothing either: someone stopped it.

**Once.** The event id is derived from the job, its status and its end, so the
same end reported twice (the run's return, then the dead letter) is one event:
the dispatcher's dedupe per (assignment, event id) starts one run.

**Scope.** A job is its requester's: ``personal_owner_id`` is the job's user,
so only that user's assignments see it, and the participants are the agents of
the conversations that started it (the job's, the video's, the one its outcome
is owed to). Ids and short fields only; the title is payload data, which the
run reads quarantined.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from typing import Any

from app.core.utils import format_exception_message
from app.models.video_studio import JOB_FAILED, JOB_READY

logger = logging.getLogger(__name__)

#: The ``object_type`` of each job kind's events (``platform_events`` payloads).
OBJECT_TYPES = {"render": "video_render", "clip": "video_clip", "music": "video_music"}
#: The kinds that raise an event: a recording (``take``) has none yet.
EVENT_KINDS = frozenset(OBJECT_TYPES)
#: Bound of the error preview carried in a payload.
_ERROR_MAX_CHARS = 500

# Strong references: asyncio keeps only weak ones to a task.
_PENDING: set[asyncio.Task] = set()


def event_name(kind: str, status: str, *, scene_preview: bool = False) -> str | None:
    """The platform event of a *kind* job that ended *status*, ``None`` when it raises none."""
    if kind not in EVENT_KINDS or status not in (JOB_READY, JOB_FAILED):
        return None
    if kind == "render":
        noun = "video.scene_preview" if scene_preview else "video.render"
        return f"{noun}.finished" if status == JOB_READY else f"{noun}.failed"
    return f"video.{kind}.ready" if status == JOB_READY else f"video.{kind}.failed"


def event_id(kind: str, job_id: object, status: str, finished_at: object) -> str:
    """The id of the event a job's end raises: the same end reported twice is one event."""
    return str(uuid.uuid5(uuid.NAMESPACE_URL, f"video-job:{kind}:{job_id}:{status}:{finished_at}"))


def _str(value: object) -> str | None:
    return str(value) if value is not None else None


def job_data(
    kind: str,
    row: Any,
    *,
    project: Any = None,
    participant_agent_ids: tuple[object, ...] | list[object] = (),
) -> dict[str, Any]:
    """The payload of a ``video.*`` event for the ended job *row* of *kind*."""
    findings = 0
    if kind == "render":
        from app.services.video_studio.render_checks import findings_of

        findings = len(findings_of(getattr(row, "checks", None)))
    participants = {_str(a) for a in participant_agent_ids}
    user_id = _str(getattr(row, "user_id", None))
    title = getattr(project, "title", None) if project is not None else getattr(row, "name", None)
    return {
        "job_kind": kind,
        "job_id": _str(row.id),
        "video_id": _str(getattr(row, "project_id", None)),
        "scene_id": getattr(row, "scene_id", None),
        "title": title,
        "revision": getattr(row, "revision", None),
        "quality": getattr(row, "quality", None),
        "status": row.status,
        "findings_count": findings,
        "error": (getattr(row, "error", None) or "")[:_ERROR_MAX_CHARS] or None,
        "file_id": _str(getattr(row, "file_id", None)),
        "owner_user_id": user_id,
        "participant_agent_ids": sorted(p for p in participants if p is not None),
        "object_type": OBJECT_TYPES[kind],
        "object_id": _str(row.id),
        # A job is its requester's: only that user's assignments see its end.
        "personal_owner_id": user_id,
        # No agent raised it: the worker did.
        "actor_agent_id": None,
    }


async def _conversation_agents(db: Any, conversation_ids: set[uuid.UUID]) -> list[object]:
    """The agents of *conversation_ids*: the one they are routed to and the one they started with."""
    if not conversation_ids:
        return []
    from sqlalchemy import select

    from app.models.chat import Conversation

    rows = (
        await db.execute(
            select(Conversation.agent_id, Conversation.original_agent_id).where(Conversation.id.in_(conversation_ids))
        )
    ).all()
    return [agent for row in rows for agent in (row.agent_id, row.original_agent_id) if agent is not None]


async def publish_ended(kind: str, job_id: object) -> str | None:
    """Raise the platform event of the job *job_id* of *kind* if it ended ready or failed; its event id.

    Never raises: a failure is logged and the job's end is untouched.
    """
    if kind not in EVENT_KINDS:
        return None
    try:
        from app.core.database import async_session
        from app.models.video_studio import VideoProject
        from app.services import platform_events
        from app.services.video_studio import jobs

        row = await jobs.load(kind, job_id)
        if row is None:
            return None
        name = event_name(kind, row.status, scene_preview=bool(getattr(row, "scene_id", None)))
        if name is None:
            return None
        async with async_session() as db:
            project = await db.get(VideoProject, row.project_id) if kind == "render" else None
            conversations = {
                c
                for c in (
                    getattr(row, "conversation_id", None),
                    getattr(row, "owed_conversation_id", None),
                    getattr(project, "conversation_id", None),
                )
                if c is not None
            }
            agents = await _conversation_agents(db, conversations)
            data = job_data(kind, row, project=project, participant_agent_ids=agents)
            eid = event_id(kind, row.id, row.status, getattr(row, "finished_at", None))
        return await platform_events.publish(name, data, event_id=eid)
    except Exception as exc:
        logger.warning(
            "Video Studio %s %s: its platform event was not published: %s", kind, job_id, format_exception_message(exc)
        )
        return None


def schedule(kind: str, job_id: object) -> None:
    """Publish, in the background, the platform event of a job the worker is done with; never raises."""
    if kind not in EVENT_KINDS or not job_id:
        return
    try:
        task = asyncio.get_running_loop().create_task(publish_ended(kind, job_id))
    except RuntimeError:
        return
    _PENDING.add(task)
    task.add_done_callback(_PENDING.discard)
