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

"""Resume the conversation that started a Video Studio job once the job has ended.

A chat turn never holds a job: a tool waits for it only until its own
deadline (``jobs.tool_wait_budget``), then the agent follows up with
``get_video_jobs``. A turn that ended first - the model handed back, its step
budget ran out - left a render that finished minutes later with nobody to
say so. A job a chat turn starts is therefore owed to its conversation
(``owed_conversation_id``, set by ``jobs.enqueue``); a wait that tells the
agent how it ended clears it (``jobs.mark_told``), and so does a cancel.

When the worker is done with a job, :func:`schedule` waits
:data:`WAKE_GRACE_SECONDS` - a turn still waiting on the job reads its row
every two seconds and tells the agent first - then, once no other job owed to
the same conversation is queued or running (the last of a batch wakes it,
once), claims every job owed to it that ended ready or failed and resumes the
conversation with their outcome as ``get_video_jobs`` words it, through
``parent_wake.wake_conversation``: a hidden platform message and one turn,
folded into a turn already running. A Stop recorded after the first of them
was started keeps the conversation idle. The wait runs in the worker that
ran the job: a worker stopped during it takes that wake along, and
:func:`sweep_lost_wakes` (every worker, every :data:`SWEEP_INTERVAL_SECONDS`)
resumes a conversation still owed jobs that ended a while ago.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from datetime import UTC, datetime, timedelta

from app.core.utils import format_exception_message
from app.models.video_studio import JOB_FAILED, JOB_READY
from app.services.video_studio import jobs

logger = logging.getLogger(__name__)

#: Names a Video Studio wake in the log and the activity trail.
WAKE_LABEL = "video-studio"
#: Seconds between the end of a job and the wake of its conversation: longer
#: than a waiting tool's poll (``jobs._WAIT_POLL_SECONDS``), so a turn still
#: waiting reports the job and the wake finds nothing owed.
WAKE_GRACE_SECONDS = 15.0

# Strong references: asyncio keeps only weak ones to a task.
_WAKES: set[asyncio.Task] = set()


def schedule(kind: str, job_id: object) -> None:
    """Resume, after the grace, the conversation the job *job_id* is owed to; never raises."""
    try:
        task = asyncio.get_running_loop().create_task(_wake_after(kind, str(job_id), WAKE_GRACE_SECONDS))
    except RuntimeError:
        return
    _WAKES.add(task)
    task.add_done_callback(_WAKES.discard)


async def _wake_after(kind: str, job_id: str, delay: float) -> None:
    await asyncio.sleep(delay)
    try:
        await wake_owed_conversation(kind, job_id)
    except Exception as exc:
        logger.warning(
            "Could not resume the conversation of Video Studio %s %s: %s",
            jobs.KIND_LABELS[kind],
            job_id,
            format_exception_message(exc),
            exc_info=True,
        )


async def wake_owed_conversation(kind: str, job_id: str) -> bool:
    """Resume the conversation the ended job *job_id* is owed to; ``False`` when nothing is to be told yet."""
    row = await jobs.load(kind, job_id)
    if row is None or row.owed_conversation_id is None or row.user_id is None:
        return False
    if row.status not in (JOB_READY, JOB_FAILED):
        return False
    return await resume_owed_conversation(row.owed_conversation_id, user_id=str(row.user_id))


async def resume_owed_conversation(conversation_id: uuid.UUID, *, user_id: str) -> bool:
    """Tell *conversation_id* how the jobs owed to it ended, once none is in flight; ``False`` when nothing was told."""
    claimed = await _claim_ended(conversation_id)
    if not claimed:
        return False
    await _tell(conversation_id, user_id, claimed)
    return True


async def _claim_ended(conversation_id: uuid.UUID) -> dict[str, list[tuple[str, datetime]]]:
    """The ended jobs owed to *conversation_id*, taken; nothing while one of them is still in flight."""
    if await jobs.owed_jobs_in_flight(conversation_id):
        return {}
    return await jobs.claim_owed_jobs(conversation_id)


async def _tell(conversation_id: uuid.UUID, user_id: str, claimed: dict[str, list[tuple[str, datetime]]]) -> None:
    report, files = await _outcome(claimed, user_id=user_id, conversation_id=conversation_id)
    from app.services.parent_wake import wake_conversation

    logger.info(
        "Resuming conversation %s with the outcome of %s",
        conversation_id,
        jobs.count_phrase({kind: len(taken) for kind, taken in claimed.items()}),
    )
    await wake_conversation(
        conversation_id=str(conversation_id),
        user_id=user_id,
        wake_text=format_wake_prompt(claimed, report),
        label=WAKE_LABEL,
        result_files=files,
        delegated_at=min(created for taken in claimed.values() for _id, created in taken),
    )


# ── Wakes a stopped worker took along ─────────────────────────────

#: How long after a job ended its conversation must still be owed it for the
#: sweep to resume it: well past :data:`WAKE_GRACE_SECONDS`, so the worker's
#: own wake always goes first.
SWEEP_AFTER_SECONDS = 120.0
#: A job that ended longer ago than this is not told any more: its news has aged.
SWEEP_WINDOW = timedelta(hours=24)
#: How often one worker sweeps.
SWEEP_INTERVAL_SECONDS = 300.0
#: Conversations resumed by one sweep at most.
SWEEP_MAX_CONVERSATIONS = 20


async def sweep_lost_wakes(now: datetime | None = None) -> int:
    """Resume the conversations still owed jobs that ended a while ago; how many were resumed.

    A wake runs in the worker that ran the job, after the grace: a worker
    stopped meanwhile takes it along, and the conversation would wait for
    ever. Claiming is atomic (``jobs.claim_owed_jobs``), so a sweep and a late
    wake never both tell the same job, and every worker may sweep. Each
    conversation's turn runs in a task of its own: the sweep only claims.
    """
    now = now or datetime.now(UTC)
    owed = await jobs.conversations_owed_ended_jobs(
        ended_before=now - timedelta(seconds=SWEEP_AFTER_SECONDS),
        ended_after=now - SWEEP_WINDOW,
        limit=SWEEP_MAX_CONVERSATIONS,
    )
    resumed = 0
    for conversation_id, user_id in owed:
        try:
            claimed = await _claim_ended(conversation_id)
        except Exception as exc:
            logger.warning(
                "Could not claim the Video Studio work owed to conversation %s: %s",
                conversation_id,
                format_exception_message(exc),
            )
            continue
        if not claimed:
            continue
        task = asyncio.get_running_loop().create_task(_tell_safely(conversation_id, str(user_id), claimed))
        _WAKES.add(task)
        task.add_done_callback(_WAKES.discard)
        resumed += 1
    if resumed:
        logger.warning(
            "Resumed %d conversation(s) whose Video Studio wake was lost with its worker",
            resumed,
        )
    return resumed


async def _tell_safely(conversation_id: uuid.UUID, user_id: str, claimed: dict[str, list[tuple[str, datetime]]]) -> None:
    try:
        await _tell(conversation_id, user_id, claimed)
    except Exception as exc:
        logger.warning(
            "Could not resume conversation %s with the Video Studio work owed to it: %s",
            conversation_id,
            format_exception_message(exc),
            exc_info=True,
        )


async def _outcome(
    claimed: dict[str, list[tuple[str, datetime]]], *, user_id: str, conversation_id: uuid.UUID
) -> tuple[str, list]:
    """What ``get_video_jobs`` says of the claimed jobs, for their requester: the text and the files it shows."""
    from app.services.builtin_tools.video_studio import build_video_studio_tools
    from app.tools.base import ToolCall

    _definitions, executor = build_video_studio_tools(user_id=user_id, conversation_id=str(conversation_id))
    arguments = {jobs.JOB_ID_ARGUMENTS[kind]: [job_id for job_id, _created in taken] for kind, taken in claimed.items()}
    result = await executor.execute_tool(ToolCall(id=f"{WAKE_LABEL}-wake", name="get_video_jobs", arguments=arguments))
    return result.content, list(result.files or [])


def format_wake_prompt(claimed: dict[str, list[tuple[str, datetime]]], report: str) -> str:
    """The hidden per-turn text that resumes the conversation. Not system-prompt text."""
    what = jobs.count_phrase({kind: len(taken) for kind, taken in claimed.items()})
    return "\n\n".join(
        [
            f"[Platform] The Video Studio work you started in this conversation ended after your turn did: {what}. "
            "Nobody has told the user yet.",
            "Their outcome, as get_video_jobs reports it, is below. Answer the user now: what came out and what did "
            "not (with the reason), each ready video shown with its marker, and carry on with their request if a "
            "step of it is still to do. Do not paste this platform notice.",
            report,
        ]
    )
