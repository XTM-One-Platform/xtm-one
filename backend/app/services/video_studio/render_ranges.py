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

"""A render drawn in scene ranges on several workers: the cap applies per range.

A render one worker cannot draw within the 4 h a render may run
(``render_preflight.ranges_for``, decided again by the render on the worker
that runs it) is cut into scene ranges (:func:`plan_ranges`):

* **What a range draws from: the bundle.** The render publishes once what its
  frames are drawn from (:func:`publish`): the planned document, the quality,
  the output size, the release, and every file the compositor reads - a
  recording, a clip or an image by the file id it was downloaded from, what the
  render made itself (a slide, a figure's video, an SVG drawn to a picture)
  stored under the bundle's prefix - with its screen inserts as the render
  prepared them, their tracks and mattes included. A range reads it
  (:func:`read`), downloads those files once (:func:`fetch`) and draws: it never
  gathers the media, speaks a narration, draws a figure or follows a track again.
* **Its row: who draws it.** ``VideoRenderRange`` (:func:`open_ranges`):
  whoever moves it from ``pending`` to ``running`` (:func:`claim`) draws it - a
  range execution on another worker, or the render itself. Its frames drawn and
  ``progress_at`` are its progress and heartbeat (:func:`progress`), which the
  render aggregates.
* **Its execution: on another worker.** The render adapter's (payload
  ``render_id``, ``range`` and ``digest``), so each worker draws ranges within
  its render budget. Its process (``render_process --range``,
  :func:`run_range`) draws the range through ``compositor.render_frames``, every
  segment closed kept beside the render in the store (``render_segments``),
  under its own lease, stall window, attempts and deadline: the cap applies per
  range.
* **The render joins them.** :func:`draw_all` draws the first range itself,
  then every range no worker took, waiting for those drawn elsewhere - its own
  deadline pushed out a range's cap at a time (``orchestration_engine.
  extend_deadline``). Once a range is drawn, ``render_frames`` on the same range
  restores its segments from the store and draws only what is missing (a range
  whose worker died is finished there), and ``encoder.concat`` joins the ranges
  by stream copy: the sound is mixed and muxed once, as for a whole render.
"""

from __future__ import annotations

import asyncio
import contextlib
import dataclasses
import hashlib
import itertools
import json
import logging
import math
import os
import time
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from sqlalchemy import and_, delete, or_, select, update

from app.core.config import settings
from app.core.database import async_session
from app.core.utils import format_exception_message, safe_uuid
from app.models.video_studio import (
    JOB_CANCELLED,
    JOB_FAILED,
    JOB_PENDING,
    JOB_READY,
    JOB_RUNNING,
    JOB_TERMINAL_STATUSES,
    VideoRender,
    VideoRenderRange,
)

logger = logging.getLogger(__name__)

#: The object store prefix of what a render's ranges are drawn from: ``<prefix>/<render id>/...``.
PREFIX = "video-studio/render-ranges"
#: Who draws a range the render took itself (``VideoRenderRange.drawn_by``).
RENDER_OWNER = "render"
#: The execution payload key naming a range, and the bundle it is drawn from.
RANGE_KEY = "range"
DIGEST_KEY = "digest"
#: How long the render waits between two reads of the ranges drawn elsewhere.
WAIT_SECONDS = 5.0
#: Moved when a bundle is read differently, so a worker of another release never draws from it.
BUNDLE_VERSION = 1
_FETCH_CONCURRENCY = 4


class BundleError(RuntimeError):
    """What the frames are drawn from cannot be handed to another worker: the render draws them whole."""


class RangeTakenError(RuntimeError):
    """Another drawer has the range now (the render took over a range whose worker stopped): this one stops."""


# ── The ranges ────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class Range:
    """One scene range: its place, the frames ``[first, end)`` it draws and the scenes it shows."""

    part: int
    first: int
    end: int
    scenes: tuple[str, ...]

    @property
    def frames(self) -> int:
        return self.end - self.first


def _scene_frames(document: dict) -> list[tuple[str, int, int]]:
    """``(scene id, first frame, end frame)`` of each planned scene, transitions in included."""
    fps = float(document["fps"])
    total = int(document["frames"])
    found = []
    scenes = list(document.get("scenes") or [])
    for index, scene in enumerate(scenes):
        first = max(0, math.floor(float(scene["start"]) * fps))
        end = total if index == len(scenes) - 1 else min(total, math.ceil(float(scene["end"]) * fps))
        found.append((str(scene.get("id") or index), first, max(first, end)))
    return found


def plan_ranges(document: dict, count: int) -> list[Range]:
    """*count* contiguous ranges covering every frame of *document*, cut where a scene starts, of about the same frames.

    A cut never falls inside a scene: the page draws each frame as a pure function of time, but a range opens a page
    of its own, and its first frame is a scene's first. Fewer ranges when the scenes do not allow *count*.
    """
    total = int(document["frames"])
    scenes = _scene_frames(document)
    starts = sorted({first for _sid, first, _end in scenes[1:] if 0 < first < total})
    cuts: list[int] = []
    for k in range(1, max(1, count)):
        ideal = total * k / count
        free = [start for start in starts if start not in cuts and (not cuts or start > cuts[-1])]
        if not free:
            break
        cuts.append(min(free, key=lambda start: abs(start - ideal)))
    bounds = [0, *cuts, total]
    ranges = []
    for part, (first, end) in enumerate(itertools.pairwise(bounds)):
        shown = tuple(sid for sid, a, b in scenes if a < end and b > first)
        ranges.append(Range(part, first, end, shown))
    return ranges


# ── The bundle: what the frames are drawn from ────────────────────────────


@dataclass(frozen=True)
class Bundle:
    """What a render's frames are drawn from, as every range reads it."""

    digest: str
    document: dict
    quality: str
    size: tuple[int, int]
    #: ``{"sources": ..., "images": ..., "inserts": ...}`` with a reference (:data:`refs`) for each file.
    assets: dict
    #: ``reference -> {"file": file id} | {"object": object name}``, each with its ``suffix``.
    refs: dict
    release: str = ""


def prefix_of(render_id: str, digest: str | None = None) -> str:
    return f"{PREFIX}/{render_id}/" + (f"{digest}/" if digest else "")


def _sha256(path: str) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as source:
        for chunk in iter(lambda: source.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _manifest(assets: Any, ref: Callable[[str], str]) -> dict:
    """The compositor's assets with every path replaced by its reference (*ref*); :class:`BundleError` if unreadable."""
    from app.services.video_studio.insert_tracking import InsertTrack

    inserts: dict[str, dict] = {}
    for key, insert in assets.inserts.items():
        track = insert.track
        if track is not None and not isinstance(track, InsertTrack):
            raise BundleError(f"the track of {key} is not one a range can read")
        footage = insert.footage
        inserts[key] = {
            "path": ref(insert.path),
            "kind": insert.kind,
            "quad": [list(point) for point in insert.quad],
            "fps": insert.fps,
            "fit": insert.fit,
            "glow": insert.glow,
            "light": insert.light,
            "track": (
                None
                if track is None
                else {
                    "first": track.first,
                    "quads": [[list(p) for p in quad] for quad in track.quads],
                    "box": list(track.box),
                }
            ),
            "key": dataclasses.asdict(insert.key) if insert.key is not None else None,
            "footage": (
                None
                if footage is None
                else {"path": ref(footage.path), "kind": footage.kind, "fps": footage.fps, "key": footage.key}
            ),
            "first_frame": insert.first_frame,
            # The frames its clean surface is read on: a range reads the same plate.
            "plate": list(insert.plate),
            "logo": insert.logo,
            "label": insert.label,
            # The box every frame of a matted insert covers: the page's, so a range places it the same.
            "box": [list(p) for p in insert.box] if insert.box is not None else None,
            "zoom": insert.zoom.as_dict() if insert.zoom is not None else None,
            "despill": insert.despill.as_dict(),
            # Its subject's colours over its clip: read once, never again by a range.
            "subject": insert.subject.as_dict() if insert.subject is not None else None,
            # The other surfaces its scene draws on the same footage: what its key past the quad leaves out.
            "others": [[list(point) for point in quad] for quad in insert.others],
        }
    return {
        "sources": {key: {"path": ref(source.path), "fps": source.fps} for key, source in assets.sources.items()},
        "images": {key: ref(path) for key, path in assets.images.items()},
        "inserts": inserts,
    }


async def publish(
    render_id: str,
    *,
    document: dict,
    assets: Any,
    origins: dict[str, object],
    quality: str,
    size: tuple[int, int],
) -> Bundle:
    """Store what *render_id*'s frames are drawn from, once; the :class:`Bundle` every range reads.

    A file downloaded from a file id (*origins*: its path on this worker -> that id) is named by it; any other -
    what the render made - is stored under the bundle's prefix by its content's digest. The bundle's digest is
    that of its document, quality, size, release and references: an attempt of the same render publishes the same.
    """
    from app.services.file_storage import file_storage

    refs: dict[str, dict] = {}
    by_path: dict[str, str] = {}
    made: list[tuple[str, str]] = []

    def ref(path: str) -> str:
        if path in by_path:
            return by_path[path]
        suffix = Path(path).suffix.lower()
        origin = origins.get(path)
        if origin is not None:
            name = f"f{len(refs):03d}"
            refs[name] = {"file": str(origin), "suffix": suffix}
        else:
            digest = _sha256(path)
            name = f"o{digest[:24]}"
            refs[name] = {"object": f"{prefix_of(render_id)}files/{digest}{suffix}", "suffix": suffix, "sha256": digest}
            made.append((path, refs[name]["object"]))
        by_path[path] = name
        return name

    manifest = await asyncio.to_thread(_manifest, assets, ref)
    release = str(settings.app_version or "")
    digest = hashlib.sha256(
        json.dumps(
            {
                "version": BUNDLE_VERSION,
                "release": release,
                "document": document,
                "quality": quality,
                "size": list(size),
                "assets": manifest,
                "refs": refs,
            },
            sort_keys=True,
            default=str,
        ).encode("utf-8")
    ).hexdigest()
    stored = set(await file_storage.list_object_names(f"{prefix_of(render_id)}files/"))
    for path, name in made:
        if name not in stored:
            await file_storage.upload_path(path, name)
    bundle = Bundle(digest, document, quality, tuple(size), manifest, refs, release)
    body = {
        "version": BUNDLE_VERSION,
        "release": release,
        "digest": digest,
        "document": document,
        "quality": quality,
        "size": list(size),
        "assets": manifest,
        "refs": refs,
    }
    await file_storage.upload_to(
        json.dumps(body, default=str).encode("utf-8"), f"{prefix_of(render_id, digest)}bundle.json", "application/json"
    )
    return bundle


async def read(render_id: str, digest: str) -> Bundle:
    """The bundle *render_id* published under *digest*; :class:`BundleError` when it is gone or of another release."""
    from app.services.file_storage import FileNotFoundInStorageError, file_storage

    name = f"{prefix_of(render_id, digest)}bundle.json"
    try:
        body = json.loads((await file_storage.download(name)).decode("utf-8"))
    except FileNotFoundInStorageError:
        raise BundleError("the render's drawing inputs are gone") from None
    except Exception as exc:
        raise BundleError(f"the render's drawing inputs could not be read: {format_exception_message(exc)}") from exc
    if body.get("version") != BUNDLE_VERSION or body.get("digest") != digest:
        raise BundleError("the render's drawing inputs were written for another bundle")
    release = str(body.get("release") or "")
    if release != str(settings.app_version or ""):
        # Another release may draw a frame differently: its seams would show.
        raise BundleError(f"the render's drawing inputs were written by release {release or 'unknown'}")
    return Bundle(digest, body["document"], body["quality"], tuple(body["size"]), body["assets"], body["refs"], release)


async def _fetch_one(ref: dict, path: Path) -> None:
    from app.services.file_storage import file_storage
    from app.services.video_studio import assets as studio_assets

    if path.is_file() and path.stat().st_size > 0:
        return
    part = path.with_name(f"{path.name}.part")
    try:
        if "file" in ref:
            if await studio_assets.download_file(ref["file"], str(part)) is None:
                raise BundleError(f"file {ref['file']} the render draws from is gone")
        else:
            await file_storage.download_to_path(ref["object"], str(part))
        part.replace(path)
    finally:
        with contextlib.suppress(OSError):
            part.unlink()


async def fetch(bundle: Bundle, workdir: Path) -> Any:
    """Download what *bundle* draws from into *workdir* - once: a resumed range keeps what it has - as compositor assets."""
    from app.services.video_studio import compositor
    from app.services.video_studio.insert_tracking import InsertTrack
    from app.services.video_studio.screen_insert import Despill, Footage, InsertSource, KeySpec, Subject, Zoom
    from app.services.video_studio.spec import INSERT_DEFAULT_LIGHT

    inputs = workdir / "inputs"
    inputs.mkdir(parents=True, exist_ok=True)
    paths = {name: inputs / f"{name}{ref.get('suffix') or ''}" for name, ref in bundle.refs.items()}
    gate = asyncio.Semaphore(_FETCH_CONCURRENCY)

    async def one(name: str) -> None:
        async with gate:
            await _fetch_one(bundle.refs[name], paths[name])

    await asyncio.gather(*(one(name) for name in paths))
    local = {name: str(path.resolve()) for name, path in paths.items()}
    found = compositor.CompositorAssets()
    # Each file named by what it holds, as the render names it (``compositor.file_identity``): the same segments' digests.
    for name, ref in bundle.refs.items():
        if "file" in ref:
            found.origins[local[name]] = f"file:{ref['file']}"
        elif ref.get("sha256"):
            found.origins[local[name]] = f"sha256:{ref['sha256']}"
    for key, source in bundle.assets.get("sources", {}).items():
        found.sources[key] = compositor.FrameSource(local[source["path"]], float(source["fps"]))
    for key, name in bundle.assets.get("images", {}).items():
        found.images[key] = local[name]
    for key, insert in bundle.assets.get("inserts", {}).items():
        track = insert.get("track")
        footage = insert.get("footage")
        found.inserts[key] = InsertSource(
            local[insert["path"]],
            insert["kind"],
            tuple(tuple(point) for point in insert["quad"]),
            fps=float(insert["fps"]),
            fit=insert["fit"],
            glow=float(insert["glow"]),
            light=float(insert.get("light", INSERT_DEFAULT_LIGHT)),
            track=(
                None
                if track is None
                else InsertTrack(
                    int(track["first"]),
                    tuple(tuple(tuple(point) for point in quad) for quad in track["quads"]),
                    tuple(track["box"]),
                )
            ),
            key=KeySpec(**insert["key"]) if insert.get("key") else None,
            footage=(
                None
                if footage is None
                else Footage(local[footage["path"]], footage["kind"], float(footage["fps"]), footage.get("key"))
            ),
            first_frame=int(insert.get("first_frame") or 0),
            plate=tuple(int(frame) for frame in insert.get("plate") or ()),
            logo=insert.get("logo"),
            label=insert.get("label") or "",
            box=tuple(tuple(point) for point in insert["box"]) if insert.get("box") else None,
            zoom=Zoom.of(insert.get("zoom")),
            despill=Despill.of(insert.get("despill")),
            subject=Subject.from_dict(insert.get("subject")),
            others=tuple(tuple(tuple(point) for point in quad) for quad in insert.get("others") or ()),
        )
    return found


# ── The rows: who draws each range ────────────────────────────────────────


def _now() -> datetime:
    return datetime.now(UTC)


def dedup_key(render_id: str, digest: str, part: int) -> str:
    from app.services.video_studio.jobs import RENDER_ADAPTER

    return f"{RENDER_ADAPTER}:{render_id}:range:{digest[:16]}:{part}"


async def open_ranges(render_id: str, digest: str, planned: list[Range], *, quality: str) -> list[VideoRenderRange]:
    """The ranges of *render_id* drawn from *digest*: made with their executions the first time, read again after.

    The first range has no execution: the render draws it itself. Ranges drawn from another digest (the render
    planned anew between two attempts) are cancelled with their executions, and their segments left to the sweep.
    """
    from sqlalchemy.dialects.postgresql import insert as pg_insert

    from app.services.orchestration_engine import cancel_execution, create_execution
    from app.services.video_studio import jobs

    rid = uuid.UUID(str(render_id))
    async with async_session() as db:
        stale = (
            (
                await db.execute(
                    select(VideoRenderRange).where(
                        VideoRenderRange.render_id == rid,
                        VideoRenderRange.digest != digest,
                        VideoRenderRange.status.notin_(JOB_TERMINAL_STATUSES),
                    )
                )
            )
            .scalars()
            .all()
        )
        for row in stale:
            row.status = JOB_CANCELLED
            row.error = "The render was planned anew: its ranges are drawn again."
            if row.execution_id:
                await cancel_execution(db, execution_id=row.execution_id)
        now = _now()
        for item in planned:
            await db.execute(
                pg_insert(VideoRenderRange)
                .values(
                    id=uuid.uuid4(),
                    render_id=rid,
                    digest=digest,
                    part=item.part,
                    first_frame=item.first,
                    end_frame=item.end,
                    scenes=list(item.scenes),
                    status=JOB_PENDING,
                    frames_done=0,
                    created_at=now,
                )
                .on_conflict_do_nothing(constraint="uq_video_render_ranges_part")
            )
        rows = list(
            (
                await db.execute(
                    select(VideoRenderRange)
                    .where(VideoRenderRange.render_id == rid, VideoRenderRange.digest == digest)
                    .order_by(VideoRenderRange.part)
                )
            ).scalars()
        )
        for row in rows:
            if row.part == 0 or row.execution_id is not None or row.status in JOB_TERMINAL_STATUSES:
                continue
            execution = await create_execution(
                db,
                adapter=jobs.RENDER_ADAPTER,
                payload={"render_id": str(render_id), RANGE_KEY: row.part, DIGEST_KEY: digest},
                deadline_at=jobs._deadline("render", quality=quality),
                dedup_key=dedup_key(str(render_id), digest, row.part),
                max_attempts=3,
            )
            row.execution_id = execution.id
        await db.commit()
    await jobs.wake()
    return rows


async def load(range_id: uuid.UUID) -> VideoRenderRange | None:
    async with async_session() as db:
        return await db.get(VideoRenderRange, range_id)


async def find(render_id: str, digest: str, part: int) -> VideoRenderRange | None:
    async with async_session() as db:
        return (
            await db.execute(
                select(VideoRenderRange).where(
                    VideoRenderRange.render_id == uuid.UUID(str(render_id)),
                    VideoRenderRange.digest == digest,
                    VideoRenderRange.part == int(part),
                )
            )
        ).scalar_one_or_none()


def _running_and(clause: Any) -> Any:
    return and_(VideoRenderRange.status == JOB_RUNNING, clause)


async def claim(
    range_id: uuid.UUID, *, by: str, execution_id: uuid.UUID | None = None, take_over: bool = False
) -> bool:
    """Whether *by* draws the range now: it moved it from ``pending`` to ``running`` - one conditional ``UPDATE``.

    The execution that drew it before (a retry, a resumed attempt) takes it again; *take_over* (the render) takes
    one whose drawer stopped - ``failed``, or ``running`` while its execution ended - and the stopped drawer's later
    writes no longer land (:func:`progress`). A range drawn or cancelled is never taken.
    """
    allowed = [VideoRenderRange.status == JOB_PENDING]
    if execution_id is not None:
        # Its own execution, run again - never a range the render took meanwhile.
        allowed.append(
            _running_and(and_(VideoRenderRange.execution_id == execution_id, VideoRenderRange.drawn_by != RENDER_OWNER))
        )
    if by == RENDER_OWNER:
        allowed.append(_running_and(VideoRenderRange.drawn_by == RENDER_OWNER))
    if take_over:
        allowed.append(VideoRenderRange.status.in_((JOB_RUNNING, JOB_FAILED)))
    now = _now()
    values: dict[str, Any] = {"status": JOB_RUNNING, "drawn_by": by[:200], "progress_at": now, "error": None}
    async with async_session() as db:
        result = await db.execute(
            update(VideoRenderRange)
            .where(VideoRenderRange.id == range_id, or_(*allowed))
            .values(**values, started_at=now)
        )
        await db.commit()
    return bool(result.rowcount)


async def progress(range_id: uuid.UUID, *, by: str, frames_done: int) -> bool:
    """Its frames drawn and its heartbeat; ``False`` once *by* no longer draws it (the render took it over)."""
    async with async_session() as db:
        result = await db.execute(
            update(VideoRenderRange)
            .where(
                VideoRenderRange.id == range_id,
                VideoRenderRange.drawn_by == by[:200],
                VideoRenderRange.status == JOB_RUNNING,
            )
            .values(frames_done=int(frames_done), progress_at=_now())
        )
        await db.commit()
    return bool(result.rowcount)


async def finish(
    range_id: uuid.UUID, *, by: str, status: str = JOB_READY, error: str | None = None, measured: dict | None = None
) -> bool:
    """End the range *by* draws - drawn whole (``ready``), ``failed`` or ``cancelled``; whether it still drew it."""
    row = await load(range_id)
    values: dict[str, Any] = {"status": status, "finished_at": _now(), "progress_at": _now(), "error": error}
    if status == JOB_READY and row is not None:
        values["frames_done"] = row.end_frame - row.first_frame
    if measured is not None:
        values["measured"] = measured
    async with async_session() as db:
        result = await db.execute(
            update(VideoRenderRange)
            .where(
                VideoRenderRange.id == range_id,
                VideoRenderRange.drawn_by == by[:200],
                VideoRenderRange.status == JOB_RUNNING,
            )
            .values(**values)
        )
        await db.commit()
    return bool(result.rowcount)


async def fail_open(range_id: uuid.UUID, error: str) -> None:
    """A range whose execution the engine gave up on: ``failed`` unless drawn, for the render to finish it."""
    async with async_session() as db:
        await db.execute(
            update(VideoRenderRange)
            .where(VideoRenderRange.id == range_id, VideoRenderRange.status.in_((JOB_PENDING, JOB_RUNNING)))
            .values(status=JOB_FAILED, error=error[:2000], finished_at=_now())
        )
        await db.commit()


async def cancel_ranges(db: Any, render_id: object) -> int:
    """Cancel *render_id*'s ranges still to draw and their executions, in *db*'s transaction; how many."""
    from app.services.orchestration_engine import cancel_execution

    rows = (
        (
            await db.execute(
                select(VideoRenderRange).where(
                    VideoRenderRange.render_id == uuid.UUID(str(render_id)),
                    VideoRenderRange.status.notin_(JOB_TERMINAL_STATUSES),
                )
            )
        )
        .scalars()
        .all()
    )
    for row in rows:
        row.status = JOB_CANCELLED
        row.error = "The render was cancelled."
        row.finished_at = _now()
        if row.execution_id:
            await cancel_execution(db, execution_id=row.execution_id)
    return len(rows)


async def forget(render_id: str) -> int:
    """Delete the ranges of a render that ended and what they were drawn from; how many objects.

    Best effort: what is left is the hourly sweep's (:func:`sweep`), never a failure of the render's end.
    """
    from app.services.file_storage import file_storage

    try:
        async with async_session() as db:
            await cancel_ranges(db, render_id)
            await db.execute(delete(VideoRenderRange).where(VideoRenderRange.render_id == uuid.UUID(str(render_id))))
            await db.commit()
        names = await file_storage.list_object_names(prefix_of(render_id))
        for name in names:
            await file_storage.delete(name)
    except Exception as exc:
        logger.info("Video render %s: its ranges were not cleared: %s", render_id, format_exception_message(exc))
        return 0
    return len(names)


async def sweep() -> int:
    """Delete the ranges and bundles of renders that ended or are gone (:func:`forget` that did not run); how many.

    A render no worker resumed keeps them while a relaunch may still bring it back (``jobs.relaunchable``).
    """
    from app.models.orchestration import OrchestrationExecution
    from app.services.file_storage import file_storage
    from app.services.video_studio import jobs

    prefixes = await file_storage.list_object_names(f"{PREFIX}/", recursive=False)
    stored = {uid for name in prefixes if (uid := safe_uuid(name.rstrip("/").rsplit("/", 1)[-1])) is not None}
    async with async_session() as db:
        rows = set((await db.execute(select(VideoRenderRange.render_id).distinct())).scalars())
        renders = stored | rows
        if not renders:
            return 0
        found = await db.execute(
            select(VideoRender.id, VideoRender.status, jobs.relaunchable())
            .outerjoin(OrchestrationExecution, OrchestrationExecution.id == VideoRender.execution_id)
            .where(VideoRender.id.in_(list(renders)))
        )
        open_renders = {
            render_id for render_id, status, *kept in found.all() if status not in JOB_TERMINAL_STATUSES or any(kept)
        }
    ended = renders - open_renders
    deleted = 0
    for render_id in ended:
        deleted += await forget(str(render_id))
    return deleted


async def execution_states(ids: list[uuid.UUID]) -> dict[uuid.UUID, str]:
    from app.models.orchestration import OrchestrationExecution

    if not ids:
        return {}
    async with async_session() as db:
        rows = await db.execute(
            select(OrchestrationExecution.id, OrchestrationExecution.state).where(OrchestrationExecution.id.in_(ids))
        )
        return dict(rows.all())


async def rows_of(render_id: object) -> list[VideoRenderRange]:
    """The ranges of *render_id* still open or drawn, the newest digest's, in order; empty when it is drawn whole."""
    async with async_session() as db:
        rows = list(
            (
                await db.execute(
                    select(VideoRenderRange)
                    .where(
                        VideoRenderRange.render_id == uuid.UUID(str(render_id)),
                        VideoRenderRange.status != JOB_CANCELLED,
                    )
                    .order_by(VideoRenderRange.created_at.desc(), VideoRenderRange.part)
                )
            ).scalars()
        )
    if not rows:
        return []
    newest = rows[0].digest
    return sorted((row for row in rows if row.digest == newest), key=lambda row: row.part)


def _scene_span(scenes: list | None) -> str:
    shown = [str(scene) for scene in scenes or []]
    if not shown:
        return ""
    return f"scene {shown[0]}" if len(shown) == 1 else f"scenes {shown[0]} to {shown[-1]}"


def describe_ranges(rows: list[VideoRenderRange]) -> str:
    """``Drawn in 3 scene ranges: 1 (scenes a to f) drawn; 2 (...) 45% on worker-1; 3 (...) waiting for a worker.``"""
    if len(rows) < 2:
        return ""
    parts = []
    for row in rows:
        frames = max(1, row.end_frame - row.first_frame)
        span = _scene_span(row.scenes)
        label = f"{row.part + 1}" + (f" ({span})" if span else "")
        if row.status == JOB_READY:
            state = "drawn"
        elif row.status == JOB_FAILED:
            state = f"failed on {row.drawn_by or 'its worker'} ({(row.error or '')[:120]}): the render draws it"
        elif row.status == JOB_RUNNING:
            where = "by the render" if row.drawn_by == RENDER_OWNER else f"on {row.drawn_by}"
            state = f"{round(100 * (row.frames_done or 0) / frames)}% {where}"
        else:
            state = "waiting for a worker (the render draws it after its own when none takes it)"
        parts.append(f"{label} {state}")
    return f"Drawn in {len(rows)} scene ranges: " + "; ".join(parts) + "."


# ── The worker drawing one range ─────────────────────────────────────────


def own_execution() -> tuple[uuid.UUID | None, str | None]:
    """The execution and the worker this render process draws for (``lease.EXECUTION_ENV`` / ``OWNER_ENV``)."""
    from app.services.video_studio.lease import EXECUTION_ENV, OWNER_ENV

    return safe_uuid(os.environ.get(EXECUTION_ENV, "").strip()), (os.environ.get(OWNER_ENV, "").strip() or None)


async def run_range(
    render_id: str,
    part: int,
    digest: str,
    *,
    cancel_event: asyncio.Event | None = None,
    report: Callable[[dict], None] | None = None,
    workdir: Path,
    on_frame: Callable[[], None] | None = None,
) -> Any:
    """Draw one range of *render_id* on this worker - the render process of a range execution; its job outcome.

    It claims the range (:func:`claim`), reads the bundle, downloads what it draws from and draws its frames, each
    segment closed kept beside the render in the store. A range the render took over, a render that ended, a
    bundle it cannot read (gone, another release) end it ``done``: the render draws what is left.
    """
    from app.services.builtin_tools.html_render import font_face_css
    from app.services.orchestration_engine import LeaseLostError
    from app.services.video_studio import compositor, jobs, render_preflight
    from app.services.video_studio.render_process import memory_ceiling
    from app.services.video_studio.runners import JobOutcome
    from app.services.video_studio.spec import QUALITY_PRESETS

    execution_id, owner = own_execution()
    by = owner or f"worker-{os.getpid()}"
    row = await find(render_id, digest, part)
    render = await jobs.load("render", render_id)
    if row is None or row.status in (JOB_READY, JOB_CANCELLED):
        return JobOutcome("done")
    if render is None or render.status in JOB_TERMINAL_STATUSES:
        # The render ended: its ranges go with it (:func:`forget`).
        return JobOutcome("done")
    if not await claim(row.id, by=by, execution_id=execution_id):
        logger.info("Video render %s: range %d is drawn by %s", render_id, part, row.drawn_by)
        return JobOutcome("done")

    def emit(event: dict) -> None:
        if report is not None:
            with contextlib.suppress(Exception):
                report(event)

    try:
        emit({"type": "stage", "stage": "range inputs", "event": "start"})
        bundle = await read(render_id, digest)
        drawn_from = await fetch(bundle, workdir)
    except BundleError as exc:
        logger.warning("Video render %s: range %d left to the render: %s", render_id, part, exc)
        await finish(row.id, by=by, status=JOB_FAILED, error=f"Left to the render: {exc}")
        return JobOutcome("done")
    preset = QUALITY_PRESETS[bundle.quality]
    range_inserts = render_preflight.InsertLoad.of(bundle.document)
    drawn = render_preflight.layout(
        bundle.size,
        x264_preset=preset.x264_preset,
        ceiling=render_preflight.launch_ceiling(memory_ceiling(bundle.quality)),
        inserts=range_inserts,
    )
    emit(render_preflight.layout_event(drawn, bundle.size, x264_preset=preset.x264_preset, inserts=range_inserts))
    tracker = compositor.DrawProgress()
    frames = row.end_frame - row.first_frame

    async def frame_progress(percent: int) -> None:
        emit(
            {
                "type": "progress",
                "progress": percent,
                "stage": f"Drawing range {part + 1} ({percent}%)",
                "frames": tracker.done,
                "drawing": tracker.drawing(bundle.document),
            }
        )
        if not await progress(row.id, by=by, frames_done=tracker.done):
            raise RangeTakenError(f"range {part} was taken over by the render")

    async def cancelled() -> bool:
        if cancel_event is not None and cancel_event.is_set():
            return True
        return await jobs.is_cancelled("render", render_id)

    began = time.monotonic()
    try:
        from app.services.video_studio import insert_checks

        placed = insert_checks.placed_inserts(bundle.document, drawn_from.inserts)
        await compositor.render_frames(
            bundle.document,
            drawn_from,
            preset=preset,
            output_size=bundle.size,
            workdir=str(workdir),
            font_css=font_face_css(),
            pages=drawn.pages,
            progress=frame_progress,
            cancelled=cancelled,
            tracker=tracker,
            on_frame=on_frame,
            frame_range=(row.first_frame, row.end_frame),
            render_id=render_id,
            # The insert regions kept as 100 % crops: the frames of this range among them.
            crops=insert_checks.crop_frames(
                placed,
                fps=float(bundle.document.get("fps") or preset.fps),
                frame_range=(row.first_frame, row.end_frame),
            ),
            # The frames the render's guard reads once encoded: where the page places each insert on them.
            guard=insert_checks.guard_frames(placed, frame_range=(row.first_frame, row.end_frame)),
            # ... and the presence gate: every frame an insert's scene is painted on.
            presence=insert_checks.presence_frames(placed, frame_range=(row.first_frame, row.end_frame)),
            drawn_for={
                "revision": getattr(render, "revision", None),
                "attempt": f"range {part + 1} on {by} at {_now().isoformat(timespec='seconds')}",
            },
        )
    except compositor.RenderCancelledError:
        await finish(row.id, by=by, status=JOB_CANCELLED, error="The render was cancelled.")
        return JobOutcome("done")
    except compositor.RenderHandedOverError as exc:
        # Its worker hands it over (``render_process.supervise``): the row is the supervisor's to write.
        return JobOutcome("retry", str(exc))
    except RangeTakenError as exc:
        logger.info("Video render %s: %s", render_id, exc)
        return JobOutcome("done")
    except LeaseLostError as exc:
        return JobOutcome("lease_lost", f"The range's lease is gone: {exc}"[:2000])
    measured = render_preflight.measurement(
        drawn,
        frames=tracker.done - tracker.kept,
        frames_reused=tracker.kept,
        seconds=time.monotonic() - began,
        segments_reused=render_preflight.segments_reused(tracker),
    )
    await finish(row.id, by=by, measured=measured)
    logger.info(
        "Video render %s: range %d (%d frames) drawn on %d pages in %.0f s",
        render_id,
        part,
        frames,
        drawn.pages,
        time.monotonic() - began,
    )
    return JobOutcome("done")


# ── The render joining its ranges ────────────────────────────────────────


async def extend_own_deadline(seconds: float) -> None:
    """Push this render's own execution deadline out to *seconds* from now: the cap applies per range.

    Best effort: a deadline not pushed out ends the render past its first cap, as before ranges.
    """
    from app.services import orchestration_engine as engine

    execution_id, owner = own_execution()
    if execution_id is None or owner is None:
        return
    try:
        async with async_session() as db:
            await engine.extend_deadline(
                db, execution_id=execution_id, worker_id=owner, deadline_at=_now() + timedelta(seconds=seconds)
            )
            await db.commit()
    except Exception as exc:
        logger.info(
            "Video render execution %s: its deadline was not pushed out: %s",
            execution_id,
            format_exception_message(exc),
        )


#: What a render's deadline keeps past the end its measured pace gives when it is moved out to it (#11782).
ETA_MARGIN_SECONDS = 600.0


async def extend_to_eta(seconds_left: float, *, most_seconds: float) -> datetime | None:
    """Move this render's own execution deadline out to the end its measured pace gives - *seconds_left* from now,
    :data:`ETA_MARGIN_SECONDS` more - when it would end past its deadline (#11782): a render that still draws
    steadily is never killed minutes short of its end. Never in, and never past *most_seconds* after the deadline it
    was queued with (``orchestration_engine.give_deadline_back``). The new deadline, else ``None``. Best effort.
    """
    from app.services import orchestration_engine as engine

    execution_id, owner = own_execution()
    if execution_id is None or owner is None or seconds_left <= 0:
        return None
    try:
        async with async_session() as db:
            deadline, given = await engine.give_deadline_back(
                db,
                execution_id=execution_id,
                worker_id=owner,
                until=_now() + timedelta(seconds=seconds_left + ETA_MARGIN_SECONDS),
                most_seconds=most_seconds,
            )
            await db.commit()
    except Exception as exc:
        logger.info(
            "Video render execution %s: its deadline was not moved to its pace: %s",
            execution_id,
            format_exception_message(exc),
        )
        return None
    return deadline if given > 0 else None


def _claimable(row: VideoRenderRange, execution_state: str | None) -> bool:
    """Whether the render may take *row*: no worker has it, or the worker that had it stopped.

    A range whose execution went back to the queue - handed back by its stopping worker, taken back from a worker
    gone - has no drawer either: waiting for a worker to claim it again hangs the render whenever the render
    itself holds the lane's only slot. Its execution, claimed later, finds the range taken (:func:`claim`).
    """
    if row.status == JOB_PENDING or row.status == JOB_FAILED:
        return True
    if row.status != JOB_RUNNING:
        return False
    if row.drawn_by == RENDER_OWNER:
        return True  # an earlier attempt of the render had it
    return execution_state is not None and execution_state != "running"


async def draw_all(
    render_id: str,
    rows: list[VideoRenderRange],
    *,
    draw: Callable[[VideoRenderRange, bool], Awaitable[str]],
    report: Callable[[int, str], Awaitable[None]],
    cancelled: Callable[[], Awaitable[bool]],
    heartbeat: Callable[[], None],
    wall_seconds: float,
    wait_seconds: float = WAIT_SECONDS,
) -> list[str]:
    """The video of each range of *render_id*, in order: drawn here when no worker has it, else waited for.

    *draw(row, mine)* draws the range (*mine*: the render claimed it) or restores it from the segments its worker
    stored, drawing only what is missing: its video's path. *report(frames, stage)* says where the ranges stand;
    *heartbeat* tells the stall window the render waits on another worker. Each range the render draws or waits
    for pushes its own deadline out by *wall_seconds* (:func:`extend_own_deadline`).
    """
    from app.services.video_studio import compositor

    total = sum(row.end_frame - row.first_frame for row in rows) or 1
    videos: dict[int, str] = {}
    drawn: dict[int, int] = {}
    pushed = 0.0
    while len(videos) < len(rows):
        if await cancelled():
            raise compositor.RenderCancelledError("The render was cancelled.")
        current = {row.part: row for row in await rows_of(render_id) if row.digest == rows[0].digest}
        states = await execution_states([row.execution_id for row in current.values() if row.execution_id])
        todo = [current.get(row.part, row) for row in rows if row.part not in videos]
        if any(row.status == JOB_CANCELLED for row in todo):
            raise compositor.RenderCancelledError("The render was cancelled.")
        if time.monotonic() - pushed > 60:
            await extend_own_deadline(wall_seconds)
            pushed = time.monotonic()
        taken = None
        for row in todo:
            state = states.get(row.execution_id) if row.execution_id else None
            if _claimable(row, state) and await claim(
                row.id, by=RENDER_OWNER, take_over=row.status != JOB_PENDING or row.drawn_by not in (None, RENDER_OWNER)
            ):
                taken = row
                break
        if taken is not None:
            if taken.drawn_by not in (None, RENDER_OWNER):
                logger.warning("Video render %s: range %d taken over from %s", render_id, taken.part, taken.drawn_by)
            videos[taken.part] = await draw(taken, True)
            drawn[taken.part] = taken.end_frame - taken.first_frame
            continue
        ready = next((row for row in todo if row.status == JOB_READY), None)
        if ready is not None:
            videos[ready.part] = await draw(ready, False)
            drawn[ready.part] = ready.end_frame - ready.first_frame
            continue
        frames = sum(drawn.values()) + sum(row.frames_done or 0 for row in todo)
        await report(min(total, frames), describe_ranges([current.get(row.part, row) for row in rows]))
        heartbeat()
        await asyncio.sleep(wait_seconds)
    return [videos[row.part] for row in rows]


def merge_audits(into: dict, more: dict) -> None:
    """Add a range's insert audits (``compositor.segment_audits``) to the render's: an insert a cut crosses is in both."""
    for key, part in more.items():
        merged = into.setdefault(key, {"frames": {}, "evidence": {}, "crops": {}})
        merged["frames"].update(part.get("frames") or {})
        for kind, proofs in (part.get("evidence") or {}).items():
            merged["evidence"].setdefault(kind, []).extend(proofs)
        merged.setdefault("crops", {}).update(part.get("crops") or {})
        merged.setdefault("crop_boxes", {}).update(part.get("crop_boxes") or {})
        merged.setdefault("placed", {}).update(part.get("placed") or {})
        merged.setdefault("over", {}).update(part.get("over") or {})
        merged.setdefault("alone", {}).update(part.get("alone") or {})
        merged.setdefault("overrun", {}).update(part.get("overrun") or {})
        merged.setdefault("blend", {}).update(part.get("blend") or {})
        if part.get("crop_frame"):
            merged["crop_frame"] = part["crop_frame"]


def is_range(payload: dict | None) -> bool:
    return isinstance(payload, dict) and payload.get(RANGE_KEY) is not None
