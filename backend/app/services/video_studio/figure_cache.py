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

"""A figure scene's drawing kept across renders and previews: drawn once per key, reused by every render of it.

A render draws a figure (``figures.draw``) for the length and the narration
timing its scene is planned with, at the render's size and frame rate. Those,
with the figure as its page reads it - the block, the scheme, the palette, the
logos, the band it keeps clear, the pictures the person may read - are all its
frames are drawn from: they are its key (``figures.render_key``), and the same
key draws the same frames, whichever render or preview asks.

* **On the worker's disk.** Every render process of a worker shares one
  folder per key (:func:`folder`). A frame drawn whole is linked there under
  its index, so a render stopped half-way leaves its frames to the next one
  of the same figure - a preview replaced while it drew, the scene's own
  preview after a neighbour's -, which draws only the rest; two drawing it at
  once each take what the other already linked. The video encoded whole is
  kept there too, and its frames go. A folder untouched for
  :data:`KEPT_ON_DISK_SECONDS` is swept (:func:`sweep_disk`).
* **In the object store, beside the video.** The video encoded whole is stored
  under the video's own prefix (:func:`object_name`), so another worker - or
  this one after a deploy - downloads it instead of drawing it. It goes with
  the video (:func:`evict`, ``DELETE /projects/{id}``, with an hourly backstop
  for a video gone, :func:`sweep_store`), and past :data:`KEPT_PER_VIDEO`
  drawings of one video the oldest stored go.

What a scene preview draws of a neighbour's figure - the frames under its
transition (``figures.draw(span=...)``) - is linked into the same folder and
encoded on its own, never stored as the whole.
"""

from __future__ import annotations

import contextlib
import logging
import os
import shutil
import tempfile
import time
import uuid
from dataclasses import dataclass
from pathlib import Path

from app.core.utils import format_exception_message, safe_uuid

logger = logging.getLogger(__name__)

#: Where a worker keeps its figure drawings, beside the renders' working directories (one filesystem: frames are linked).
ROOT = Path(tempfile.gettempdir()) / "xtm-figures"
#: The object store prefix of the figures kept beside each video: ``<prefix>/<video id>/<key>.mp4``.
PREFIX = "video-studio/figure-renders"
#: The video encoded whole, in a key's folder.
VIDEO = "figure.mp4"
#: A folder nothing used for this long is removed: longer than a render may run.
KEPT_ON_DISK_SECONDS = 6 * 3600
#: The drawings one video keeps in the object store; past it the oldest stored go.
KEPT_PER_VIDEO = 48


@dataclass(frozen=True)
class Kept:
    """Where a figure's drawing is kept: its key, and the video whose prefix stores it (``None``: on this worker only)."""

    key: str
    video_id: str | None = None


def folder(key: str) -> Path:
    """The folder of *key* on this worker, made if missing and marked as used now."""
    path = ROOT / key
    path.mkdir(parents=True, exist_ok=True)
    with contextlib.suppress(OSError):
        os.utime(path)
    return path


def object_name(video_id: str, key: str) -> str:
    return f"{PREFIX}/{video_id}/{key}.mp4"


def _link(source: Path, target: Path) -> bool:
    """*source* under *target* too, linked (copied across filesystems); whether *target* holds it now."""
    if target.exists():
        return True
    part = target.with_name(f"{target.name}.{uuid.uuid4().hex[:8]}.part")
    try:
        try:
            os.link(source, part)
        except OSError:
            shutil.copy2(source, part)
        part.replace(target)
        return True
    except OSError:
        with contextlib.suppress(OSError):
            part.unlink()
        return target.exists()


def take_frame(kept: Kept, name: str, into: Path) -> bool:
    """Link the frame *name* another render drew of *kept* into *into*; whether there was one."""
    shared = ROOT / kept.key / name
    return shared.is_file() and shared.stat().st_size > 0 and _link(shared, into / name)


def share_frame(kept: Kept, drawn: Path) -> None:
    """Offer the frame just drawn to every render of *kept* on this worker."""
    _link(drawn, folder(kept.key) / drawn.name)


def whole_on_disk(kept: Kept) -> Path | None:
    """The video of *kept* encoded whole on this worker, or ``None``."""
    path = ROOT / kept.key / VIDEO
    if path.is_file() and path.stat().st_size > 0:
        with contextlib.suppress(OSError):
            os.utime(path.parent)
        return path
    return None


async def find(kept: Kept) -> Path | None:
    """The video of *kept* encoded whole: on this worker, else downloaded from the video's prefix; ``None`` if never.

    A store that cannot be read is a miss: the figure is drawn.
    """
    from app.services.file_storage import FileNotFoundInStorageError, file_storage

    found = whole_on_disk(kept)
    if found is not None or kept.video_id is None:
        return found
    target = folder(kept.key) / VIDEO
    part = target.with_name(f"{VIDEO}.{uuid.uuid4().hex[:8]}.part")
    try:
        await file_storage.download_to_path(object_name(kept.video_id, kept.key), str(part), missing_ok=True)
        part.replace(target)
    except FileNotFoundInStorageError:
        return None
    except Exception as exc:
        logger.info("Figure %s not read from the store, drawn instead: %s", kept.key[:12], format_exception_message(exc))
        return None
    finally:
        with contextlib.suppress(OSError):
            part.unlink()
    return whole_on_disk(kept)


def _covers(found: tuple[str, int, int], wanted: tuple[str, int, int]) -> bool:
    """Whether the drawing *found* holds every frame of *wanted*, sharper: as large or larger, at a multiple of its rate."""
    return (
        found[0] == wanted[0]
        and found[1:] != wanted[1:]
        and found[1] >= wanted[1]
        and found[2] >= wanted[2]
        and found[2] % max(wanted[2], 1) == 0
    )


def serves(found: str, wanted: str) -> bool:
    """Whether the drawing kept under the key *found* serves a render that wants *wanted*.

    The same drawing, or one of the same figure drawn sharper (:func:`find_sharper`): what the pre-flight counts as
    not drawn (``reuse_index``).
    """
    from app.services.video_studio.figures import parse_key

    if found == wanted:
        return True
    have, want = parse_key(found), parse_key(wanted)
    return have is not None and want is not None and _covers(have, want)


async def find_sharper(kept: Kept) -> Path | None:
    """A video of the same figure drawn sharper than *kept* asks - at a higher scale, a multiple of its frame rate.

    What a render at another quality derives its own drawing from with one rescale, no page opened: an
    ultra render's figure serves a high one, a high one's a draft. On this worker first, else downloaded from beside
    the video; the least sharp that covers it. ``None`` when none was drawn, or the key is an older release's.
    """
    from app.services.file_storage import FileNotFoundInStorageError, file_storage
    from app.services.video_studio.figures import parse_key

    wanted = parse_key(kept.key)
    if wanted is None:
        return None
    found: list[tuple[int, int, str]] = []
    if ROOT.is_dir():
        for path in ROOT.glob(f"{wanted[0]}-*"):
            other = parse_key(path.name)
            if other is not None and _covers(other, wanted) and whole_on_disk(Kept(path.name)) is not None:
                found.append((other[1], other[2], path.name))
    if found:
        return whole_on_disk(Kept(min(found)[2]))
    if kept.video_id is None:
        return None
    try:
        names = await file_storage.list_object_names(f"{PREFIX}/{kept.video_id}/{wanted[0]}-")
    except Exception as exc:  # a store that cannot be read: drawn instead
        logger.info("Figure %s: sharper drawings not listed: %s", kept.key[:12], format_exception_message(exc))
        return None
    for name in names:
        key = name.rsplit("/", 1)[-1].removesuffix(".mp4")
        other = parse_key(key)
        if other is not None and _covers(other, wanted):
            found.append((other[1], other[2], key))
    for _scale, _fps, key in sorted(found):
        try:
            if (path := await find(Kept(key, kept.video_id))) is not None:
                return path
        except FileNotFoundInStorageError:
            continue
    return None


async def keep(kept: Kept, video: Path) -> None:
    """Keep *video*, the figure of *kept* encoded whole: on this worker in place of its frames, and beside the video.

    Best effort: a drawing not kept is drawn again by the next render that needs it.
    """
    from app.services.file_storage import file_storage

    shared = folder(kept.key)
    if not _link(video, shared / VIDEO):
        return
    for frame in shared.glob("f*.jpg"):
        with contextlib.suppress(OSError):
            frame.unlink()
    if kept.video_id is None:
        return
    try:
        await file_storage.upload_path(str(shared / VIDEO), object_name(kept.video_id, kept.key), "video/mp4")
        await _bound(kept.video_id)
    except Exception as exc:
        logger.info("Figure %s not stored beside its video: %s", kept.key[:12], format_exception_message(exc))


async def _bound(video_id: str) -> None:
    """Delete the drawings of *video_id* past :data:`KEPT_PER_VIDEO`, the oldest stored first."""
    from datetime import UTC, datetime

    from app.services.file_storage import file_storage

    stored = await file_storage.list_objects_modified(f"{PREFIX}/{video_id}/")
    if len(stored) <= KEPT_PER_VIDEO:
        return
    oldest = sorted(stored, key=lambda item: item[1] or datetime.min.replace(tzinfo=UTC))
    for name, _modified in oldest[: len(stored) - KEPT_PER_VIDEO]:
        await file_storage.delete(name)


async def evict(video_id: object) -> int:
    """Delete every figure drawing kept beside *video_id*: the video is gone. How many."""
    from app.services.file_storage import file_storage

    names = await file_storage.list_object_names(f"{PREFIX}/{video_id}/")
    for name in names:
        await file_storage.delete(name)
    return len(names)


async def sweep_store() -> int:
    """Delete the drawings kept beside videos that no longer exist (:func:`evict` that did not run); how many."""
    from sqlalchemy import select

    from app.core.database import async_session
    from app.models.video_studio import VideoProject
    from app.services.file_storage import file_storage

    prefixes = await file_storage.list_object_names(f"{PREFIX}/", recursive=False)
    by_video = {uid: name for name in prefixes if (uid := safe_uuid(name.rstrip("/").rsplit("/", 1)[-1])) is not None}
    if not by_video:
        return 0
    async with async_session() as db:
        alive = set((await db.execute(select(VideoProject.id).where(VideoProject.id.in_(list(by_video))))).scalars())
    deleted = 0
    for video_id in by_video.keys() - alive:
        deleted += await evict(video_id)
    return deleted


def sweep_disk(*, now: float | None = None) -> int:
    """Remove the figure folders of this worker nothing used for :data:`KEPT_ON_DISK_SECONDS`; how many."""
    now = time.time() if now is None else now
    removed = 0
    if not ROOT.is_dir():
        return 0
    for path in ROOT.iterdir():
        try:
            if path.is_dir() and now - path.stat().st_mtime > KEPT_ON_DISK_SECONDS:
                shutil.rmtree(path, ignore_errors=True)
                removed += 1
        except OSError:
            continue
    return removed
