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

"""A render the platform failed runs again once, on its revision.

Some failures come from the video - a recording not ready, a narration every
voice failed - and their reason names what to change. Others come from the
platform, and no edit of the video fixes them: the headless Chromium refusing
a protocol call or never answering it (``compositor.ChromiumError``), the
render's memory ceiling or the kernel's out-of-memory killer, its process
dying, a stall past its resumes, its worker's lease lapsing with no attempt
left. Such a failure, on the last attempt the execution had, runs the render
again once (:func:`run_again`): its row back to pending, saying why, its
execution deferred - no attempt spent - with :data:`INFRA_RETRY_KEY` on its
payload, its finished segments kept. Only while the video is still at the
revision the render drew (:func:`same_revision`): one the author changed
meanwhile is theirs to render again.

A render that fails again, or could not run again, records why on its row
(``VideoRender.failure``, :func:`failure_record`), and its answer's finding
says the failure was the platform's, that it ran again once, and that nothing
in the video needs to change (``render_answer``) - never "fix what its reason
names" on a protocol error.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Any

from app.core.utils import format_exception_message

logger = logging.getLogger(__name__)

#: Where the execution of a render that ran again once records it: ``{cause, reason, revision, at}``.
INFRA_RETRY_KEY = "infra_retry"
#: The ``adapter_state`` of a render deferred to run again once.
INFRA_RETRY_STATE = "infra_retry"
#: How long a render that failed on the platform waits before it runs again: a worker short of memory frees it, a
#: Chromium that went down is started afresh.
RETRY_DELAY_SECONDS = 30.0

#: The causes of a failure the platform made, and how a finding says each.
CHROMIUM, LEASE, MEMORY, CRASH, STALL = "chromium", "lease", "memory", "crash", "stall"
CAUSE_WORDS = {
    CHROMIUM: "the headless Chromium it draws in failed",
    LEASE: "its worker stopped renewing its lease",
    MEMORY: "it ran out of memory",
    CRASH: "its render process died",
    STALL: "it stood still past its resumes",
}


def cause_of_exception(exc: BaseException) -> str | None:
    """The platform's cause of a render that raised *exc*: :data:`CHROMIUM` for its Chromium's failure; ``None``."""
    from app.services.video_studio.compositor import ChromiumError

    return CHROMIUM if isinstance(exc, ChromiumError) else None


def cause_of_exit(kind: str, *, oom_killed: bool = False) -> str | None:
    """The platform's cause of a render process that ended *kind* (``render_process.RenderExit.kind``); ``None``."""
    if kind == "memory" or (kind == "crashed" and oom_killed):
        return MEMORY
    return {"crashed": CRASH, "stalled": STALL}.get(kind)


def record_of(payload: dict | None) -> dict | None:
    """The run again an execution's *payload* says it already had (:data:`INFRA_RETRY_KEY`); ``None``."""
    record = (payload or {}).get(INFRA_RETRY_KEY)
    return record if isinstance(record, dict) else None


async def same_revision(render_id: object) -> int | None:
    """The revision the render *render_id* drew, while its video is still at it; ``None`` otherwise."""
    from app.core.database import async_session
    from app.models.video_studio import JOB_TERMINAL_STATUSES, VideoProject
    from app.services.video_studio import jobs

    row = await jobs.load("render", render_id)
    if row is None or row.status in JOB_TERMINAL_STATUSES:
        return None
    async with async_session() as db:
        project = await db.get(VideoProject, row.project_id)
    if project is None or int(project.revision) != int(row.revision):
        return None
    return int(row.revision)


def _said(cause: str, reason: str, revision: int) -> str:
    return (
        f"Running again once on revision {revision}: {CAUSE_WORDS.get(cause, cause)}, not the video "
        f"({reason.rstrip('.')})"
    )


async def _revision_kept(render_id: object) -> int | None:
    """:func:`same_revision`, ``None`` when it cannot be read: the render then fails as before."""
    try:
        return await same_revision(render_id)
    except Exception as exc:
        logger.warning("Video render %s: its revision was not read: %s", render_id, format_exception_message(exc))
        return None


async def _reopen(render_id: object, cause: str, reason: str, revision: int, pages: int | None = None) -> dict | None:
    """The render's row back to pending, saying why it runs again; its record, else ``None``: it ended meanwhile."""
    from app.models.video_studio import JOB_PENDING
    from app.services.video_studio import jobs

    now = datetime.now(UTC)
    said = _said(cause, reason, revision)
    if not await jobs.mark_if_open("render", render_id, status=JOB_PENDING, progress=0, stage=said[:200], progress_at=now):
        return None
    logger.warning("Video render %s: %s.", render_id, said, extra={"render_id": str(render_id), "infra_retry": cause})
    record = {"cause": cause, "reason": reason[:500], "revision": revision, "at": now.isoformat()}
    if pages:
        record["pages"] = int(pages)
    return record


async def run_again(render_id: object, cause: str, reason: str, *, pages: int | None = None) -> dict | None:
    """Put the render *render_id* back to pending to run again once after the platform failed it (*cause*, *reason*);
    the record its execution keeps (:data:`INFRA_RETRY_KEY`) - with the *pages* its process laid out, which a run again
    after the memory ceiling halves (:func:`lighter_pages`) -, else ``None``: its video changed, or it ended."""
    revision = await _revision_kept(render_id)
    return None if revision is None else await _reopen(render_id, cause, reason, revision, pages)


def lighter_pages(retried: dict | None) -> int | None:
    """The pages a whole render runs again on after its memory ceiling stopped it: half those its first run laid
    out (:func:`run_again`'s *pages*), one when they are not known; ``None`` for any other run - never the same layout
    again, which fails the same way after the same minutes."""
    if not isinstance(retried, dict) or retried.get("cause") != MEMORY:
        return None
    try:
        pages = int(retried.get("pages") or 0)
    except (TypeError, ValueError):
        pages = 0
    return max(1, pages // 2) if pages > 1 else 1


async def requeue(execution: Any, cause: str, reason: str) -> bool:
    """Run again once the render of a dead-lettered *execution* the platform failed; whether it does.

    Its execution is queued again (``requeue_execution``: its attempts back, its deadline kept) with the record of
    :func:`run_again` on its payload: never a second time.
    """
    from app.core.database import async_session
    from app.services import orchestration_engine as engine

    render_id = (execution.payload or {}).get("render_id")
    if not render_id or record_of(execution.payload) is not None:
        return False
    if execution.deadline_at is not None:
        deadline = execution.deadline_at if execution.deadline_at.tzinfo else execution.deadline_at.replace(tzinfo=UTC)
        if deadline <= datetime.now(UTC):
            return False
    revision = await _revision_kept(render_id)
    if revision is None:
        return False
    said = _said(cause, reason, revision)
    record = {"cause": cause, "reason": reason[:500], "revision": revision, "at": datetime.now(UTC).isoformat()}
    async with async_session() as db:
        requeued = await engine.requeue_execution(db, execution_id=execution.id)
        if requeued is None:
            return False
        requeued.payload = {**(requeued.payload or {}), INFRA_RETRY_KEY: record}
        requeued.last_error = said[:2000]
        await db.commit()
    from app.services.video_studio import jobs

    if await _reopen(render_id, cause, reason, revision) is None:
        # Cancelled or replaced meanwhile: it keeps the end its cancel wrote, and never runs again.
        async with async_session() as db:
            await engine.cancel_execution(db, execution_id=execution.id)
            await db.commit()
        return True
    await jobs.wake()
    return True


def failure_record(cause: str, *, retried: dict | None, revision: int | None = None) -> dict:
    """What the row of a render the platform failed for good keeps (``VideoRender.failure``): its *cause*, and the
    run again it had (*retried*, :func:`record_of`), or none - its video changed meanwhile."""
    return {
        "cause": cause,
        "retried": bool(retried),
        "first": (retried or {}).get("reason"),
        "first_cause": (retried or {}).get("cause"),
        "revision": (retried or {}).get("revision", revision),
    }


def finding_words(failure: Any) -> tuple[str, str] | None:
    """``(what it adds to the reason, the fix)`` of a render the platform failed (``VideoRender.failure``); ``None``
    for one its video failed, whose reason names what to change."""
    if not isinstance(failure, dict) or failure.get("cause") not in CAUSE_WORDS:
        return None
    cause = CAUSE_WORDS[failure["cause"]]
    revision = failure.get("revision")
    on = f" on revision {revision}" if revision is not None else ""
    if failure.get("retried"):
        first = CAUSE_WORDS.get(str(failure.get("first_cause")), "the same")
        what = (
            f"An infrastructure failure, not the video's: {cause}. Its first run failed on the platform too "
            f"({first}); it ran again once{on} and failed again."
        )
    else:
        what = f"An infrastructure failure, not the video's: {cause}. It was not run again: the video changed since."
    fix = (
        "nothing in the video: render it again as it is; should it fail the same way, the administrator looks at the "
        "render worker (its memory, its Chromium) - the reason above says what failed"
    )
    return what, fix
