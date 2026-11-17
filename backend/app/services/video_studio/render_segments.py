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

"""A render's finished segments kept beside it in the object store: what an attempt on another worker resumes from.

The compositor encodes a render as segments of ``compositor.SEGMENT_SECONDS``,
each closed whole under the digest of what it was drawn from
(``compositor.kept_segments``). They live in the render's working
directory, which a worker keeps between the attempts it runs - but a deploy
replaces the worker, and its disk with it. So each segment closed whole is
also stored under the render and the digest (:func:`keep`), and an attempt
whose working directory lacks a segment of the same digest takes it from
there (:func:`restore`): the render resumes where it stopped on whichever
worker takes it. They go once the render ended (:func:`forget`), with an
hourly backstop for a render that ended without an attempt to clear them
(:func:`sweep`).

A render cancelled by the render of a newer revision of its video at its
quality hands its segments over (:func:`handed_over`): they stay while
that render is queued or running, which takes each one whose frames are drawn
from what its own same frames are drawn from (:func:`take_over`, the window
digest each segment records: ``compositor.WindowDigests``) - the scenes the
newer revision left alone - and stores it as its own.

The same rule takes a render's own segments back across a worker change
(:func:`take_back`): the strict digest of the whole drawing names each
file the render made itself (a slide, a picture drawn from an SVG, a figure)
by its bytes, and the next worker makes them again. One byte different and
nothing was found under the new digest, so every frame was drawn again; now
each segment the render stored under an earlier digest whose own frames are
drawn from the same inputs is taken back, and only the frames around what
changed are drawn.

A whole render that fails at a check stage after its drawing keeps its
segments and its checks' place for :data:`RENDER_FAILED_KEEP_SECONDS`, a
marker beside them saying until when and under which drawing digest
(:func:`keep_failed`). The next render of the same video at the same quality
whose drawing digest matches copies them as its own (:func:`adopt_kept`) and
resumes from them; a digest that no longer matches forgets them. The hourly
:func:`sweep` forgets them once their time is over.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import re
import tempfile
from datetime import UTC, datetime, timedelta
from pathlib import Path, PurePosixPath
from typing import Any

from app.core.utils import format_exception_message, safe_uuid

logger = logging.getLogger(__name__)

#: The object store prefix of the segments kept beside each render: ``<prefix>/<render id>/<digest>/<segment>``.
PREFIX = "video-studio/render-segments"
#: A segment stored beside a render, under its digest: ``<digest>/seg-<first>-<end>.mp4``.
_STORED_SEGMENT = re.compile(r"^([^/]+)/seg-(\d{7})-(\d{7})\.mp4$")


def prefix_of(render_id: str, digest: str | None = None) -> str:
    return f"{PREFIX}/{render_id}/" + (f"{digest}/" if digest else "")


#: Where the place of a render's checks is kept beside its segments: ``<prefix>/<render id>/_checks/<file>``.
#: Never a segment (:data:`_STORED_SEGMENT`), gone with them (:func:`forget`, :func:`sweep`).
CHECKS_DIR = "_checks"


def check_place_of(render_id: str, name: str) -> str:
    return f"{prefix_of(render_id)}{CHECKS_DIR}/{name}"


#: How long a whole render that failed at a check stage keeps its frames for the next render of the same revision.
RENDER_FAILED_KEEP_SECONDS = 86400
#: The marker of frames kept after a failure: ``<prefix>/<render id>/_kept.json``. Never a segment.
KEPT_FILE = "_kept.json"


def kept_marker_of(render_id: str) -> str:
    return f"{prefix_of(render_id)}{KEPT_FILE}"


#: Where a failed render's row (``VideoRender.drawing``) records until when its frames are kept.
FRAMES_KEPT_UNTIL = "frames_kept_until"


def frames_kept_until(render: Any, now: datetime | None = None) -> datetime | None:
    """Until when the failed *render* keeps its frames for the next render of its revision; ``None`` when it
    keeps none, a later render took them, or their time is over."""
    from app.models.video_studio import JOB_FAILED

    drawing = getattr(render, "drawing", None)
    if getattr(render, "status", None) != JOB_FAILED or not isinstance(drawing, dict) or not drawing.get(FRAMES_KEPT_UNTIL):
        return None
    try:
        until = datetime.fromisoformat(str(drawing[FRAMES_KEPT_UNTIL]))
    except ValueError:
        return None
    return until if until > (now or _now()) else None


def kept_until_line(until: datetime) -> str:
    """``frames kept until 2026-11-05 07:00 UTC: ...``: what a failed render's record says while they are kept."""
    return (
        f"frames kept until {until.astimezone(UTC):%Y-%m-%d %H:%M} UTC: the next render of this revision at this quality "
        "resumes from them instead of drawing again from 0 s"
    )


def _now() -> datetime:
    return datetime.now(UTC)


async def keep_failed(
    render_id: str, digest: str, *, fps: int, size: tuple[int, int], now: datetime | None = None
) -> datetime | None:
    """Keep the segments and the checks' place a whole render stored under *digest* past its end, its checks having
    failed it: until when, or ``None`` when it stored no segment (it failed before drawing) or the marker was
    not written. Best effort: a store failure is a log line, never a failed render."""
    from app.services.file_storage import file_storage

    try:
        stored = [name for name in await file_storage.list_object_names(prefix_of(render_id, digest)) if name.endswith(".mp4")]
        if not stored:
            return None
        until = (now or _now()) + timedelta(seconds=RENDER_FAILED_KEEP_SECONDS)
        marker = {"render": render_id, "digest": digest, "fps": int(fps), "size": [int(size[0]), int(size[1])], "until": until.isoformat()}
        with tempfile.TemporaryDirectory(prefix="render-kept-") as scratch:
            local = Path(scratch) / KEPT_FILE
            local.write_text(json.dumps(marker), encoding="utf-8")
            await file_storage.upload_path(str(local), kept_marker_of(render_id), "application/json")
    except Exception as exc:
        logger.info("Video render %s: its frames not kept after its failed checks: %s", render_id, format_exception_message(exc))
        return None
    logger.info("Video render %s: %d segments of digest %s kept until %s", render_id, len(stored), digest, until.isoformat())
    return until


async def kept_of(render_id: str) -> dict | None:
    """The marker of the frames *render_id* keeps after a failure (:func:`keep_failed`), its ``until`` a datetime;
    ``None`` when none is stored or it is unread."""
    marker = await _read_json(kept_marker_of(render_id))
    if not marker or not marker.get("digest"):
        return None
    try:
        return {**marker, "until": datetime.fromisoformat(str(marker["until"]))}
    except (KeyError, ValueError):
        return None


async def still_kept(render_id: str, now: datetime | None = None) -> dict | None:
    """The marker of *render_id*'s kept frames while their time runs (:func:`kept_of`); ``None`` once over."""
    marker = await kept_of(render_id)
    return marker if marker is not None and marker["until"] > (now or _now()) else None


async def _copy(source: str, target: str, scratch: Path) -> None:
    from app.services.file_storage import file_storage

    local = scratch / "object"
    await file_storage.download_to_path(source, str(local), missing_ok=False)
    await file_storage.upload_path(str(local), target, None)
    with contextlib.suppress(OSError):
        local.unlink()


async def adopt_kept(
    render_id: str,
    candidates: list[str],
    digest: str,
    *,
    fps: int,
    size: tuple[int, int],
    now: datetime | None = None,
) -> dict | None:
    """Copy as *render_id*'s own the frames the first of the failed renders *candidates* keeps (:func:`keep_failed`)
    whose drawing digest, fps and size match: its segments under *digest* and its checks' place, which the
    compositor (:func:`restore`) and the frozen-picture watch (:func:`restore_check_place`) then resume from.

    A candidate whose frames were drawn from something else (the revision changed) has them forgotten; one whose time
    is over is left to :func:`sweep`. The source's frames are forgotten once copied.
    ``{render, digest, segments, frames: [[first, end], ...]}`` or ``None``. Best effort: nothing copied is a render
    drawn from the start."""
    from app.services.file_storage import file_storage

    moment = now or _now()
    for candidate in candidates:
        if candidate == render_id:
            continue
        marker = await kept_of(candidate)
        if marker is None or marker["until"] <= moment:
            continue
        if marker["digest"] != digest or int(marker.get("fps") or 0) != int(fps) or list(marker.get("size") or []) != [int(size[0]), int(size[1])]:
            logger.info(
                "Video render %s: the frames render %s kept (digest %s) are of another drawing (digest %s): forgotten",
                render_id,
                candidate,
                marker["digest"],
                digest,
            )
            with contextlib.suppress(Exception):
                await forget(candidate)
            continue
        source, checks = prefix_of(candidate, digest), f"{prefix_of(candidate)}{CHECKS_DIR}/"
        try:
            names = [*await file_storage.list_object_names(source), *await file_storage.list_object_names(checks)]
            frames: list[list[int]] = []
            with tempfile.TemporaryDirectory(prefix="render-kept-") as scratch:
                for name in names:
                    if name.startswith(source):
                        relative = name[len(source) :]
                        await _copy(name, prefix_of(render_id, digest) + relative, Path(scratch))
                        if match := _STORED_SEGMENT.match(f"{digest}/{relative}"):
                            frames.append([int(match.group(2)), int(match.group(3))])
                    else:
                        await _copy(name, f"{prefix_of(render_id)}{CHECKS_DIR}/{name[len(checks) :]}", Path(scratch))
        except Exception as exc:
            logger.info("Video render %s: the frames render %s kept not copied: %s", render_id, candidate, format_exception_message(exc))
            continue
        if not frames:
            continue
        with contextlib.suppress(Exception):
            await forget(candidate)
        frames.sort()
        logger.info("Video render %s: %d segments of digest %s taken from the frames failed render %s kept", render_id, len(frames), digest, candidate)
        return {"render": candidate, "digest": digest, "segments": len(frames), "frames": frames}
    return None


def resumed_seconds(frames: list[list[int]], fps: int) -> float:
    """Where a render resuming from kept *frames* (:func:`adopt_kept`) stands: the end of the run from frame 0, in s."""
    reached = 0
    for first, end in sorted(frames):
        if first > reached:
            break
        reached = max(reached, end)
    return reached / max(1, int(fps))


async def forget_unless_kept(render_id: str, now: datetime | None = None) -> int:
    """What an ended render's attempt does with what it stored (``render_process``): forgotten, unless the render
    of a newer revision takes them (:func:`handed_over`) or its checks failed it and its frames are kept
    (:func:`keep_failed`). How many objects were deleted."""
    if await handed_over(render_id):
        return 0
    try:
        if await still_kept(render_id, now) is not None:
            return 0
    except Exception as exc:  # unread: forgotten as any ended render's
        logger.info("Video render %s: whether it keeps its frames was not read: %s", render_id, format_exception_message(exc))
    return await forget(render_id)


async def expire_kept(render_ids: list[str], now: datetime | None = None) -> set[str]:
    """Of the ended renders *render_ids*, those whose frames are still kept after a failure; each one whose
    time is over is forgotten here, its render id and digest logged."""
    moment = now or _now()
    kept: set[str] = set()
    for render_id in render_ids:
        marker = await kept_of(render_id)
        if marker is None:
            continue
        if marker["until"] > moment:
            kept.add(render_id)
            continue
        deleted = await forget(render_id)
        logger.info(
            "Video render %s: the frames kept after its failed checks (digest %s) expired at %s: %d objects forgotten",
            render_id,
            marker["digest"],
            marker["until"].isoformat(),
            deleted,
        )
    return kept


async def keep_check_place(render_id: str, path: Path) -> None:
    """Store the place a check of the render keeps in *path* (the frozen-picture watch's, ``frame_freeze.Checkpoint``;
    the presence gate's, ``insert_checks.PresencePlace``) beside the render, over the last one: an attempt on another
    worker goes on from it (:func:`restore_check_place`). It raises: the caller bounds and logs it."""
    from app.services.file_storage import file_storage

    kind = "application/json" if path.suffix == ".json" else "application/octet-stream"
    await file_storage.upload_path(str(path), check_place_of(render_id, path.name), kind)


async def restore_check_place(render_id: str, path: Path) -> bool:
    """Download the place a check of the render stored (:func:`keep_check_place`) to *path* when the working
    directory lacks it: a deploy replaced the worker and its disk at the checks. Whether one came back; what
    it is of is the check's to judge (its key). Best effort: none read is a check from the start."""
    from app.services.file_storage import file_storage

    if path.is_file() and path.stat().st_size > 0:
        return False
    part = path.with_name(f"{path.name}.restore.part")
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        await file_storage.download_to_path(check_place_of(render_id, path.name), str(part), missing_ok=True)
        part.replace(path)
        return True
    except Exception as exc:  # none stored, or unread: the check starts from its beginning
        logger.debug("Video render %s: no check place %s restored: %s", render_id, path.name, format_exception_message(exc))
        return False
    finally:
        with contextlib.suppress(OSError):
            part.unlink()


def _evidence_files(audits: Path) -> list[str]:
    """The evidence frames a segment's audits name (``compositor.segment_audits``), its crops, the
    scenes drawn alone for the render's guard and painted black and white for the presence gate: a
    segment taken back is read as the attempt that drew it read it."""
    try:
        found = json.loads(audits.read_text(encoding="utf-8")) or {}
    except (OSError, ValueError):
        return []
    proofs = [proof["file"] for part in found.values() for proof in (part.get("evidence") or {}).values() if proof.get("file")]
    crops = [name for part in found.values() for name in (part.get("crops") or {}).values() if name]
    alone = sorted({shot["file"] for part in found.values() for shot in (part.get("alone") or {}).values() if shot.get("file")})
    toned = sorted({name for part in found.values() for tones in (part.get("blend") or {}).values() for name in tones.values() if name})
    return proofs + crops + alone + toned


async def keep(render_id: str, digest: str, segment: Path) -> None:
    """Store *segment*, closed whole, beside the render, with what its matted screen inserts showed and who drew it.

    Its audits and the evidence frames they name and its record (the
    revision and the attempt that drew it, ``compositor.DRAWN_SUFFIX``) go
    first and the segment last, so a segment stored has them stored too. Best
    effort: a segment not stored is drawn again if lost.
    """
    from app.services.file_storage import file_storage
    from app.services.video_studio.compositor import AUDIT_SUFFIX, DRAWN_SUFFIX, EVIDENCE_DIR

    prefix = prefix_of(render_id, digest)
    audits = segment.with_name(segment.stem + AUDIT_SUFFIX)
    drawn = segment.with_name(segment.stem + DRAWN_SUFFIX)
    try:
        if audits.is_file():
            for file in _evidence_files(audits):
                if (frame := segment.parent / EVIDENCE_DIR / file).is_file():
                    kind = "image/png" if file.lower().endswith(".png") else "image/jpeg"
                    await file_storage.upload_path(str(frame), f"{prefix}{EVIDENCE_DIR}/{file}", kind)
            await file_storage.upload_path(str(audits), prefix + audits.name, "application/json")
        if drawn.is_file():
            await file_storage.upload_path(str(drawn), prefix + drawn.name, "application/json")
        await file_storage.upload_path(str(segment), prefix + segment.name, "video/mp4")
    except Exception as exc:
        logger.info("Video render %s: segment %s not kept in the store: %s", render_id, segment.name, format_exception_message(exc))


async def restore(render_id: str, digest: str, directory: Path) -> int:
    """Download into *directory* what is stored beside the render for *digest* that it lacks; how many segments.

    A segment comes back with its audits and their evidence frames, where the
    compositor reads them (``compositor.segment_audits``).
    """
    from app.services.file_storage import file_storage

    prefix = prefix_of(render_id, digest)
    restored = 0
    for name in await file_storage.list_object_names(prefix):
        relative = PurePosixPath(name[len(prefix) :])
        if not relative.parts or ".." in relative.parts:
            continue
        target = directory.joinpath(*relative.parts)
        if target.is_file() and target.stat().st_size > 0:
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        part = target.with_name(f"{target.name}.restore.part")
        try:
            await file_storage.download_to_path(name, str(part), missing_ok=True)
            part.replace(target)
            if target.suffix == ".mp4":
                restored += 1
        except Exception as exc:  # a segment not read is drawn again
            logger.info("Video render %s: %s not restored: %s", render_id, relative, format_exception_message(exc))
        finally:
            with contextlib.suppress(OSError):
                part.unlink()
    return restored


async def forget(render_id: str) -> int:
    """Delete the segments stored beside a render that ended; how many."""
    from app.services.file_storage import file_storage

    names = await file_storage.list_object_names(prefix_of(render_id))
    for name in names:
        await file_storage.delete(name)
    return len(names)


def _successor() -> Any:
    """The render that replaced a render at its quality (``superseded_by_id``), as a second ``VideoRender`` to join."""
    from sqlalchemy.orm import aliased

    from app.models.video_studio import VideoRender

    return aliased(VideoRender)


async def handed_over(render_id: str) -> bool:
    """Whether the render *render_id* was replaced by a render of its video at its quality still queued or running,
    which takes its segments (:func:`take_over`): they stay until that render ends. ``False`` when it cannot be read."""
    from sqlalchemy import and_, select

    from app.core.database import async_session
    from app.models.video_studio import JOB_PENDING, JOB_RUNNING, VideoRender

    uid = safe_uuid(render_id)
    if uid is None:
        return False
    successor = _successor()
    try:
        async with async_session() as db:
            found = await db.execute(
                select(successor.id)
                .join(VideoRender, and_(VideoRender.superseded_by_id == successor.id, VideoRender.quality == successor.quality))
                .where(VideoRender.id == uid, VideoRender.scene_id.is_(None), successor.status.in_((JOB_PENDING, JOB_RUNNING)))
            )
            return found.first() is not None
    except Exception as exc:  # unread: its segments go as any ended render's
        logger.info("Video render %s: whether it hands its segments over was not read: %s", render_id, format_exception_message(exc))
        return False


async def forget_handed_to(render_id: str) -> int:
    """Delete the segments the renders *render_id* replaced handed over to it (:func:`handed_over`), once it ended."""
    from sqlalchemy import select

    from app.core.database import async_session
    from app.models.video_studio import VideoRender

    uid = safe_uuid(render_id)
    if uid is None:
        return 0
    async with async_session() as db:
        replaced = (await db.execute(select(VideoRender.id).where(VideoRender.superseded_by_id == uid))).scalars().all()
    deleted = 0
    for previous in replaced:
        deleted += await forget(str(previous))
    return deleted


async def _read_json(name: str) -> dict | None:
    from app.services.file_storage import file_storage

    try:
        read = json.loads(await file_storage.download(name))
    except Exception:  # unread: the segment is drawn again
        return None
    return read if isinstance(read, dict) else None


async def _download(name: str, target: Path) -> None:
    from app.services.file_storage import file_storage

    target.parent.mkdir(parents=True, exist_ok=True)
    part = target.with_name(f"{target.name}.take.part")
    try:
        await file_storage.download_to_path(name, str(part), missing_ok=False)
        part.replace(target)
    finally:
        with contextlib.suppress(OSError):
            part.unlink()


async def _take_one(source: str, segment: Path, record: dict, stored: set[str]) -> None:
    """The segment stored at *source* (``<prefix>/<digest>/seg-....mp4``, one of the *stored* names) downloaded to
    *segment*, its audits and their evidence frames first, its *record* written beside it."""
    from app.services.video_studio.compositor import AUDIT_SUFFIX, DRAWN_SUFFIX, EVIDENCE_DIR

    folder = source.rsplit("/", 1)[0] + "/"
    audits = segment.with_name(segment.stem + AUDIT_SUFFIX)
    if folder + audits.name in stored:
        await _download(folder + audits.name, audits)
        for file in _evidence_files(audits):
            with contextlib.suppress(Exception):  # an evidence frame not read is a proof the guard lacks, never a frame
                await _download(f"{folder}{EVIDENCE_DIR}/{file}", segment.parent / EVIDENCE_DIR / file)
    segment.with_name(segment.stem + DRAWN_SUFFIX).write_text(json.dumps(record, default=str), encoding="utf-8")
    await _download(source, segment)


async def take_over(
    predecessors: list[tuple[str, int]],
    render_id: str,
    digest: str,
    directory: Path,
    frames: tuple[int, int],
    windows: Any,
    finished: set[tuple[int, int]],
    *,
    own: bool = False,
) -> list[dict]:
    """Take into *directory* the segments the *predecessors* ``(render id, revision)`` - the renders of older
    revisions *render_id* replaced - stored whose frames are drawn from what the same frames of *render_id* are drawn
    from (*windows*: ``compositor.WindowDigests``, against the ``window`` each segment records), within *frames* and
    clear of the *finished* ones; each stored as *render_id*'s own (:func:`keep`, under *digest*), so an attempt on
    another worker takes it back. ``[{render, revision, frames: [first, end], segments, pieces, scenes: {id: frames}}]``
    by run of consecutive segments of one predecessor. Best effort: a segment not taken is drawn."""
    from app.services.file_storage import file_storage
    from app.services.video_studio.compositor import DRAWN_SUFFIX, segment_name

    first, end = frames
    claimed = sorted(finished)

    def clear(a: int, b: int) -> bool:
        return first <= a < b <= end and all(b <= x or a >= y for x, y in claimed)

    runs: list[dict] = []
    for previous, revision in predecessors:
        prefix = prefix_of(previous)
        try:
            names = await file_storage.list_object_names(prefix)
        except Exception as exc:
            logger.info("Video render %s: the segments of render %s not listed: %s", render_id, previous, format_exception_message(exc))
            continue
        listed = set(names)
        stored = sorted(
            (int(match.group(2)), int(match.group(3)), name)
            for name in names
            if (match := _STORED_SEGMENT.match(name[len(prefix) :]))
        )
        for a, b, name in stored:
            drawn = name[: -len(".mp4")] + DRAWN_SUFFIX
            if not clear(a, b) or drawn not in listed:
                continue
            record = await _read_json(drawn)
            if not record or not record.get("window") or record["window"] != await asyncio.to_thread(windows.of, a, b):
                continue
            target = directory / segment_name(a, b)
            try:
                await _take_one(name, target, {**record, "render": previous, "revision": record.get("revision", revision)}, listed)
            except Exception as exc:  # a segment not taken is drawn again
                logger.info("Video render %s: segment %s of render %s not taken: %s", render_id, target.name, previous, format_exception_message(exc))
                with contextlib.suppress(OSError):
                    target.unlink()
                continue
            claimed.append((a, b))
            await keep(render_id, digest, target)
            scenes = windows.scene_frames(a, b)
            last = runs[-1] if runs else None
            if last is not None and last["render"] == previous and last["frames"][1] == a:
                last["frames"][1] = b
                last["segments"] += 1
                last["pieces"].append([a, b])
                for scene, count in scenes.items():
                    last["scenes"][scene] = last["scenes"].get(scene, 0) + count
            else:
                runs.append(
                    {
                        "render": previous,
                        "revision": int(record.get("revision") or revision),
                        "frames": [a, b],
                        "segments": 1,
                        "pieces": [[a, b]],
                        "scenes": dict(scenes),
                    }
                )
    if runs:
        logger.info(
            "Video render %s: %d frames taken from %s (%s)",
            render_id,
            sum(b - a for run in runs for a, b in run["pieces"]),
            "what it stored under an earlier digest, on the worker before" if own else "the renders it replaced",
            "; ".join(f"frames {run['frames'][0]}-{run['frames'][1]} of render {run['render']} (revision {run['revision']})" for run in runs),
        )
    return runs


async def take_back(
    render_id: str,
    digest: str,
    directory: Path,
    frames: tuple[int, int],
    windows: Any,
    finished: set[tuple[int, int]],
) -> list[dict]:
    """Take back into *directory* the segments *render_id* stored under another digest than *digest*.

    An attempt on another worker computes the digest of the whole drawing
    again; a file the render made itself is named by its bytes, and one made
    differently changes it, which :func:`restore` then finds nothing under.
    Each stored segment clear of the *finished* ones whose frames are drawn
    from what the same frames are drawn from now (*windows*, the ``window``
    its record carries, as :func:`take_over` judges a replaced render's) is
    taken and stored again under *digest*. ``[{render, revision, frames,
    segments, pieces, scenes}]`` by run of consecutive segments; ``[]`` when
    the render stored nothing else. Best effort: a segment not taken is drawn.
    """
    return await take_over([(render_id, 0)], render_id, digest, directory, frames, windows, finished, own=True)


async def sweep() -> int:
    """Delete the segments stored beside renders that ended or are gone (:func:`forget` that did not run); how many.

    A render no worker resumed keeps them while a relaunch may still bring it back (``jobs.relaunchable``),
    and one replaced while the render that replaced it is queued or running (:func:`handed_over`).
    """
    from sqlalchemy import and_, select

    from app.core.database import async_session
    from app.models.orchestration import OrchestrationExecution
    from app.models.video_studio import JOB_PENDING, JOB_RUNNING, JOB_TERMINAL_STATUSES, VideoRender
    from app.services.file_storage import file_storage
    from app.services.video_studio.jobs import relaunchable

    prefixes = await file_storage.list_object_names(f"{PREFIX}/", recursive=False)
    by_render = {uid: name for name in prefixes if (uid := safe_uuid(name.rstrip("/").rsplit("/", 1)[-1])) is not None}
    if not by_render:
        return 0
    successor = _successor()
    async with async_session() as db:
        rows = await db.execute(
            select(VideoRender.id, VideoRender.status, relaunchable(), successor.status)
            .outerjoin(OrchestrationExecution, OrchestrationExecution.id == VideoRender.execution_id)
            .outerjoin(
                successor, and_(successor.id == VideoRender.superseded_by_id, successor.quality == VideoRender.quality)
            )
            .where(VideoRender.id.in_(list(by_render)))
        )
        open_renders = {
            render_id
            for render_id, status, kept, next_status in rows.all()
            if status not in JOB_TERMINAL_STATUSES or kept or next_status in (JOB_PENDING, JOB_RUNNING)
        }
    ended = [str(render_id) for render_id in by_render.keys() - open_renders]
    # Frames a render keeps after its failed checks stay while their time runs; past it they go here.
    kept = await expire_kept(ended)
    deleted = 0
    for render_id in ended:
        if render_id not in kept:
            deleted += await forget(render_id)
    return deleted
