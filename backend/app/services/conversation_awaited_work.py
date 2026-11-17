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

"""The work a conversation waits on, which resumes it once it ends.

A turn that hands long work to the background ends normally, and until that
work resumes the conversation (a Video Studio job's wake) the
conversation looks finished: nothing says the assistant comes back. This
module lists that work for the chat to show beside the transcript - the Video
Studio jobs owed to the conversation that are still queued or running, each as
``jobs.job_state`` reads its row, so the chat and ``get_video_jobs`` never
disagree on where a job is.

The background tasks a conversation started have a banner of their own
(``BackgroundTasksBanner``) and are not repeated here.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime

from app.services.video_studio import jobs

#: Items one read returns at most: a banner names a few and counts the rest.
MAX_AWAITED_ITEMS = 20


@dataclass(frozen=True)
class AwaitedWork:
    """One piece of work the conversation waits on."""

    #: ``video_take`` / ``video_render`` / ``video_clip`` / ``video_music``.
    kind: str
    id: str
    #: What the person calls it (a render's video title, a shot's name), or ``None``.
    name: str | None
    #: ``queued`` or ``running``.
    status: str
    percent: int
    stage: str | None

    def as_dict(self) -> dict:
        return {
            "kind": self.kind,
            "id": self.id,
            "name": self.name,
            "status": self.status,
            "percent": self.percent,
            "stage": self.stage,
        }


async def awaited_work(conversation_id: uuid.UUID) -> list[AwaitedWork]:
    """The work *conversation_id* waits on, oldest first; read on sessions of its own."""
    now = datetime.now(UTC)
    items: list[AwaitedWork] = []
    for kind, row, name in (await jobs.owed_jobs_pending(conversation_id))[:MAX_AWAITED_ITEMS]:
        state = jobs.job_state(kind, row, now=now)
        if state.status not in (jobs.STATE_QUEUED, jobs.STATE_RUNNING):
            continue
        items.append(
            AwaitedWork(
                kind=f"video_{kind}",
                id=state.id,
                name=name,
                status=state.status,
                percent=max(0, min(100, int(state.percent or 0))),
                stage=state.held or state.stage,
            )
        )
    return items
