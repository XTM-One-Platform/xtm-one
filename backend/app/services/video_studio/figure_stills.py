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

"""The editor's figure stills, drawn by the worker.

A figure scene's still (``figures``: the figure built in whole, kept under the
digest of what it shows) was drawn inside the request that saved the video -
up to 25 s of Chromium in the API process, on the editor's autosave path - and
by a preview, in the background of the API process. Both now hand the stills
to a worker job (:data:`ADAPTER`) and answer at once; the editor's preview
words a figure until its still exists and polls, as it already did.

* **Off the request.** :func:`enqueue` claims each still not drawn yet (one
  claim per digest, shared through Redis: a still asked twice is queued once,
  ``figures.claim_drawing``) and creates one execution carrying what to draw -
  the figure page's document, its size and the band it keeps clear, and who
  reads its pictures - in a session of its own, after the save's commit. A
  draft the editor previews is drawn the same way: the job never re-reads the
  saved spec.
* **Isolated and bounded.** The worker runs one such job at a time
  (``concurrency_limit`` 1), in a process of its own supervised like a render
  (``render_process.supervise``): the render's memory ceiling
  (``render_process.memory_ceiling``) over it and the Chromium it starts, and
  a still that takes longer than :data:`STALL_SECONDS` stops it. The claims
  are released however it ends. Its failing never fails a save: the next
  preview that finds a still missing asks for it again.
* **Retained.** :func:`sweep` deletes the stills written more than
  ``VIDEO_STUDIO_FIGURE_STILL_RETENTION_DAYS`` ago. The editor's preview is a
  still's only reader, and it writes a still it shows again once halfway
  there (:func:`keep`): a still in use is kept, one nobody opened goes - and
  is drawn again by the next save or preview that finds it missing.
"""

from __future__ import annotations

import asyncio
import json
import logging
import sys
import uuid
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any, TextIO

from app.core.utils import format_exception_message
from app.services.video_studio import figures

logger = logging.getLogger(__name__)

ADAPTER = "video_figure_stills"
#: A stills process that draws nothing for this long is stopped: one still takes seconds.
STALL_SECONDS = 120.0


def _carried(preview: figures.FigurePreview) -> dict:
    """What the job needs of one still, as the execution's payload keeps it (JSON)."""
    source = preview.source
    return {
        "scene_id": preview.scene_id,
        "digest": preview.digest,
        "kind": source.kind,
        "slide": source.slide,
        "presentation_id": str(source.presentation_id) if source.presentation_id else None,
        "files": sorted(source.files),
        "document": preview.document,
        "viewport": list(preview.viewport),
        "safe": dict(preview.safe),
    }


def _preview(item: dict) -> figures.FigurePreview:
    source = figures.FigureSource(
        str(item["kind"]),
        dict(item["slide"]),
        uuid.UUID(str(item["presentation_id"])) if item.get("presentation_id") else None,
        frozenset(str(f) for f in item.get("files") or ()),
    )
    width, height = item["viewport"]
    return figures.FigurePreview(
        str(item["scene_id"]),
        source,
        dict(item["document"]),
        (int(width), int(height)),
        str(item["digest"]),
        dict(item.get("safe") or figures.NO_SAFE_AREA),
    )


async def enqueue(previews: list[figures.FigurePreview], user: Any) -> set[str]:
    """Queue the drawing of the stills of *previews* not drawn yet, their pictures read as *user*.

    Returns the digests being drawn - queued now or by an earlier ask - so a
    preview marks their scenes ``drawing`` and polls. Never raises: a still
    that could not be queued is asked for again by the next preview.
    """
    from app.core.database import async_session
    from app.services.orchestration_engine import create_execution, notify_orchestration_wake

    asked: dict[str, figures.FigurePreview] = {}
    for preview in previews:
        if preview.digest not in asked and not await figures.has_preview(preview.digest):
            asked[preview.digest] = preview
    claimed = [preview for digest, preview in asked.items() if await figures.claim_drawing(digest)]
    if not claimed:
        return set(asked)
    payload = {"user_id": str(user.id), "role": user.role, "stills": [_carried(p) for p in claimed]}
    try:
        async with async_session() as db:
            await create_execution(db, adapter=ADAPTER, payload=payload, max_attempts=1)
            await db.commit()
        await notify_orchestration_wake()
    except Exception as exc:  # the next preview asks again
        logger.warning("Figure stills not queued: %s", format_exception_message(exc))
        for preview in claimed:
            await figures.release_drawing(preview.digest)
        return set(asked) - {p.digest for p in claimed}
    return set(asked)


def queued_note(digests: set[str]) -> str | None:
    """What a save says of the stills it queued (the tool answers): drawn by the worker, shown once drawn."""
    if not digests:
        return None
    count = len(digests)
    return (
        f"The editor's still of {count} figure{'s' if count > 1 else ''} is being drawn in the background: the "
        "preview names each figure until its still is drawn."
    )


async def release(payload: dict) -> None:
    """The job ended, however it ended: every still it carried may be asked for again."""
    for item in payload.get("stills") or []:
        if isinstance(item, dict) and item.get("digest"):
            await figures.release_drawing(str(item["digest"]))


async def draw(payload: dict, *, on_drawn: Callable[[int], None] | None = None) -> int:
    """Draw the stills *payload* carries (the job's process); how many were drawn.

    A still drawn meanwhile is skipped, one that fails is logged and the
    next one drawn: the next preview that finds it missing asks again.
    """
    # Who reads the pictures, as the ask copied it: the job holds no session of the request's.
    reader = SimpleNamespace(id=uuid.UUID(str(payload["user_id"])), role=str(payload.get("role") or "user"))
    drawn = 0
    for index, item in enumerate(payload.get("stills") or []):
        try:
            preview = _preview(item)
            if not await figures.has_preview(preview.digest):
                await figures.store_still(preview, reader)
                drawn += 1
        except Exception as exc:
            logger.info("Figure still %s not drawn: %s", (item or {}).get("scene_id"), format_exception_message(exc))
        if on_drawn is not None:
            on_drawn(index + 1)
    return drawn


# ── The worker's side ─────────────────────────────────────────────────────


def _command(execution_id: object) -> tuple[list[str], str]:
    import app

    backend = str(Path(app.__file__).resolve().parents[1])
    return [sys.executable, "-m", "app.services.video_studio.figure_stills", str(execution_id)], backend


def _ceiling() -> int:
    """What a stills process is held to: the render's memory ceiling - a scene preview's on a worker no bigger than
    its preview reserve, which holds no render, never unbounded."""
    from app.services.video_studio.render_process import memory_ceiling, preview_memory_ceiling

    return memory_ceiling() or preview_memory_ceiling()


async def run_isolated(execution_id: object, *, cancel_event: asyncio.Event | None = None) -> Any:
    """Draw the job's stills in a process of its own, under the render's memory ceiling; how it ended (``RenderExit``)."""
    from app.services.video_studio.render_process import process_env, supervise

    command, backend = _command(execution_id)
    return await supervise(
        command,
        memory_limit=_ceiling(),
        stall_seconds=STALL_SECONDS,
        cancel_event=cancel_event,
        cwd=backend,
        env=process_env(preview=False),
    )


def describe(ended: Any) -> str:
    """Why a stills process did not end on its own (the execution's error)."""
    from app.services.video_studio import resources

    peak = resources.format_bytes(ended.peak_memory)
    if ended.kind == "memory":
        return f"The figure stills used {peak}, past the {resources.format_bytes(_ceiling())} memory ceiling."
    if ended.kind == "stalled":
        return f"A figure still was not drawn in {int(STALL_SECONDS)} s (memory peak {peak})."
    if ended.kind == "cancelled":
        return "Cancelled."
    return f"The figure stills process {ended.ending()} (memory peak {peak})."


# ── The stills process ────────────────────────────────────────────────────


async def _child(execution_id: str, channel: TextIO) -> None:
    import app.models  # noqa: F401
    from app.core.database import async_session, engine
    from app.llm.identity import load_service_identity
    from app.models.orchestration import OrchestrationExecution
    from app.services.security_options import refresh_security_options_cache

    def report(event: dict) -> None:
        channel.write(json.dumps(event, default=str) + "\n")

    try:
        await refresh_security_options_cache()
    except Exception as exc:
        logger.warning("Figure stills: the security options could not be read: %s", format_exception_message(exc))
    # What a gateway call with nobody bound signs as (``app.llm.identity``): only the platform's bootstrap sets it.
    await load_service_identity()
    async with async_session() as db:
        execution = await db.get(OrchestrationExecution, uuid.UUID(execution_id))
        payload = dict(execution.payload or {}) if execution is not None else {}
    stills = payload.get("stills") or []
    report({"type": "stage", "stage": f"Drawing {len(stills)} figure still(s)"})
    if stills:
        await draw(payload, on_drawn=lambda done: report({"type": "progress", "progress": done}))
    report({"type": "outcome", "kind": "done", "error": None})
    await engine.dispose()


def main(argv: list[str] | None = None) -> int:
    from app.core.config import settings
    from app.core.logging import setup_logging
    from app.services.video_studio.render_process import die_with_the_worker, status_channel, stopped_before_the_worker

    args = list(sys.argv[1:] if argv is None else argv)
    if len(args) != 1:
        sys.stderr.write("usage: python -m app.services.video_studio.figure_stills EXECUTION_ID\n")
        return 2
    channel = status_channel()
    die_with_the_worker()
    stopped_before_the_worker()
    setup_logging(debug=settings.debug, log_level=settings.log_level, log_format=settings.log_format)
    asyncio.run(_child(args[0], channel))
    channel.flush()
    return 0


# ── Retention ─────────────────────────────────────────────────────────────


def _retention_days() -> int:
    from app.core.config import settings

    return settings.video_studio_figure_still_retention_days


def _modified(value: object) -> datetime | None:
    if isinstance(value, datetime):
        moment = value
    else:
        try:
            moment = datetime.fromisoformat(str(value))
        except (TypeError, ValueError):
            return None
    return moment if moment.tzinfo else moment.replace(tzinfo=UTC)


async def keep(digest: str, stat: dict, *, now: datetime | None = None) -> bool:
    """A still the editor shows keeps its place: written again, the same bytes, once it is halfway to the sweep.

    Its only reader is the editor's preview, so a still shown is a still
    used, and :func:`sweep` never reaches it - nor the steps kept beside it
    (``figures.steps_object``), written again with it. Whether it was.
    """
    from app.services.file_storage import file_storage

    days = _retention_days()
    modified = _modified(stat.get("last_modified"))
    if days <= 0 or modified is None or modified >= (now or datetime.now(UTC)) - timedelta(days=days / 2):
        return False
    name = figures.preview_object(digest)
    try:
        data = await file_storage.read_range(name, 0, int(stat.get("size") or 0))
        steps = await figures.read_steps(digest)
        # The steps first, as a still is stored: a still that is kept has its steps beside it.
        if steps is not None:
            await file_storage.upload_to(json.dumps(steps).encode("utf-8"), figures.steps_object(digest), "application/json")
        await file_storage.upload_to(data, name, "image/jpeg")
    except Exception as exc:  # the sweep may take it: the next preview has it drawn again
        logger.info("Figure still %s not kept: %s", digest, format_exception_message(exc))
        return False
    return True


async def sweep(*, now: datetime | None = None) -> int:
    """Delete the stills written more than ``video_studio_figure_still_retention_days`` ago; how many.

    A still the editor shows is written again before it gets there
    (:func:`keep`); one a video no longer shows, or that nobody opened since,
    goes - and is drawn again by the next save or preview that finds it
    missing: nothing references a still but its digest, which every reader
    computes again.
    """
    from app.services.file_storage import file_storage

    days = _retention_days()
    if days <= 0:
        return 0
    cutoff = (now or datetime.now(UTC)) - timedelta(days=days)
    deleted = 0
    for name, value in await file_storage.list_objects_modified(f"{figures.PREVIEW_PREFIX}/"):
        modified = _modified(value) if value is not None else None
        if modified is None or modified >= cutoff:
            continue
        await file_storage.delete(name)
        deleted += 1
    return deleted


if __name__ == "__main__":
    sys.exit(main())
