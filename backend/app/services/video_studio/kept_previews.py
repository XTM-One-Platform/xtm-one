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

"""A scene preview served from a kept one: every frame cached, only the encode left, never the preview lane.

A sweep previews every scene of a video, then previews them again after an
edit. A scene the edit left alone draws, frame for frame, what its last
preview drew - and still queued behind the other scenes' previews in its
video's lane: an unchanged figure scene waited 12 minutes behind two insert
scenes for 744 frames its last preview had drawn.

So a preview records what its picture and its sound are made from
(:func:`inputs`, ``VideoRender.measured["kept_as"]``): the window the render
plans it in (the scene and the ones around it, the look, the music where the
window starts in the whole video, ``render.scene_window``), what it shows
beside its neighbours (``render_checks.digest_scenes``: the takes, the shots,
the decks it plays), the narrations it speaks (``reuse_index.voice_digests``),
the template it wears, its quality, the person it is drawn for and the release
that draws it. A preview asked with the same inputs (:func:`kept`: a ready
preview of the scene whose files are there) has every frame cached:
:func:`serve` makes it on the worker that asked - the kept MP4 encoded into
the new preview's own file (``encoder.remux``), its frames and the files its
checks show stored as the new preview's, its checks carried - and its answer
says "744 of 744 frames cached, encoded in 1.4 s" (:func:`cached_line`). Its
job is queued behind :data:`SERVE_GRACE_SECONDS`: a worker draws it as before
only when the serve could not finish; one served is ready, and its job ends
at once (``render.run_render`` returns for a finished row).
"""

from __future__ import annotations

import copy
import hashlib
import json
import logging
import tempfile
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from app.core.utils import format_exception_message, safe_uuid

logger = logging.getLogger(__name__)

#: Moved when what :func:`inputs` hashes changes: a preview recorded under another version is never served again.
INPUTS_VERSION = 1
#: How long the job of a preview being served waits before a worker may draw it: past it, the serve did not finish.
SERVE_GRACE_SECONDS = 180.0
#: The ready previews of a scene read for one made from the same inputs, newest first (the retention keeps 5).
LOOKED_AT = 5
#: The keys of a render's checks that name a file it stored (a finding's frame, an insert's crop).
_FILE_KEYS = frozenset({"file_id", "frame_file_id"})


@dataclass(frozen=True)
class KeptInputs:
    """What a scene preview is made from (:func:`inputs`): its digest, and the scene's drawing digests (what a
    ready preview records it drew, ``render_checks.record_seen``)."""

    digest: str
    whole: dict[str, str] = field(default_factory=dict)
    content: dict[str, str] = field(default_factory=dict)
    #: Every scene's drawing digest: what its reading of an insert on a join is kept with (``render_checks.join_measures``).
    joined: dict[str, str] = field(default_factory=dict)
    #: The scene's parts as it is now (``render_checks.SceneDigests.parts``): what a later refusal names.
    parts: dict[str, dict[str, str]] = field(default_factory=dict)


@dataclass(frozen=True)
class Kept:
    """A ready preview made from the same inputs as one asked (:func:`kept`): what :func:`serve` makes it from."""

    render_id: str
    inputs: KeptInputs
    #: The frames its MP4 holds: every one of them cached.
    frames: int


def _dumped(value: object) -> str:
    return json.dumps(value, sort_keys=True, default=str)


async def _template_part(db: Any, template: Any) -> str:
    """The template a video wears as its preview reads it: the container and when it last changed (its palette, its
    marks), ``free`` without one."""
    if template is None:
        return "free"
    from app.models.template_container import TemplateContainer

    uid = safe_uuid(str(getattr(template, "id", "")))
    row = await db.get(TemplateContainer, uid) if uid is not None else None
    changed = getattr(row, "updated_at", None)
    return f"{getattr(template, 'id', '')}:{changed.isoformat() if changed is not None else ''}"


async def inputs(db: Any, effective: dict, template: Any, *, scene_id: str, quality: str, user: Any) -> KeptInputs | None:
    """What a preview of *scene_id* at *quality* drawn for *user* from the *effective* spec, wearing *template*
    (``templates.VideoTemplate``), is made from, before anything is drawn; ``None`` for a scene the video does not
    have or whose drawing cannot be read."""
    from app.core.config import settings
    from app.services.video_studio import lexicon_store, render_checks, reuse_index, timeline
    from app.services.video_studio.projects import estimate
    from app.services.video_studio.render import scene_window
    from app.services.video_studio.spec import parse_video_spec

    spec = parse_video_spec(effective)
    ids = [scene.id or "" for scene in spec.scenes]
    if scene_id not in ids:
        return None
    lexicon = await lexicon_store.for_voiceover(db, spec.voiceover)
    marks = await render_checks.digest_scenes(db, effective, lexicon)
    if not marks.whole.get(scene_id):
        return None
    window, _target = scene_window(effective, scene_id, (await estimate(db, effective)).document)
    voices = reuse_index.voice_digests(await reuse_index.voice_target(db, user, spec), spec, lexicon)
    first, end = timeline.preview_window(len(ids), ids.index(scene_id))
    parts = [
        f"kept-preview-v{INPUTS_VERSION}",
        str(settings.app_version),
        str(quality),
        str(getattr(user, "id", "")),
        await _template_part(db, template),
        _dumped(window),
        marks.whole[scene_id],
        *(f"voice:{sid}:{voices.get(sid, '')}" for sid in ids[first:end]),
    ]
    content = {scene_id: marks.content[scene_id]} if marks.content.get(scene_id) else {}
    return KeptInputs(
        hashlib.sha256("|".join(parts).encode("utf-8")).hexdigest()[:40],
        {scene_id: marks.whole[scene_id]},
        content,
        dict(marks.whole),
        {scene_id: marks.parts[scene_id]} if marks.parts.get(scene_id) else {},
    )


async def kept(db: Any, project_id: object, *, scene_id: str, quality: str, made_from: KeptInputs) -> Kept | None:
    """The newest ready preview of *scene_id* at *quality* made from the same inputs (*made_from*) whose MP4 is still
    stored: every frame of a preview asked now is cached in it. ``None`` otherwise."""
    from sqlalchemy import select

    from app.models.file_attachment import FileAttachment
    from app.models.video_studio import JOB_READY, VideoRender

    rows = (
        (
            await db.execute(
                select(VideoRender)
                .where(
                    VideoRender.project_id == project_id,
                    VideoRender.scene_id == scene_id,
                    VideoRender.status == JOB_READY,
                    VideoRender.quality == quality,
                    VideoRender.file_id.is_not(None),
                )
                .order_by(VideoRender.finished_at.desc())
                .limit(LOOKED_AT)
            )
        )
        .scalars()
        .all()
    )
    for row in rows:
        measured = row.measured if isinstance(row.measured, dict) else {}
        if measured.get("kept_as") != made_from.digest:
            continue
        if await db.get(FileAttachment, row.file_id) is None:
            continue
        fps = int(row.fps or 0)
        frames = int(measured.get("frames_total") or round(float(row.duration_ms or 0) / 1000 * fps))
        return Kept(str(row.id), made_from, max(1, frames))
    return None


#: What :func:`inputs` compares, as a preview served from a kept one says it.
COMPARED = (
    "the scene and its neighbours as specified (each overlay's text, subtext, position, start and duration included), "
    "the recordings, shots and decks they play, the narrations heard and their voice, the look, the template, the "
    "quality, the person and the release"
)


def cached_line(frames: int, seconds: float, kept_from: str) -> str:
    """``744 of 744 frames cached, encoded in 1.4 s``: what a preview served from a kept one says first, and what it
    compared (:data:`COMPARED`)."""
    return (
        f"{frames:,} of {frames:,} frames cached, encoded in {seconds:.1f} s: scene preview `{kept_from}` drew them from "
        f"the same inputs - {COMPARED} - so this one was served on the worker that asked, without the preview lane."
    )


def _file_ids(value: object) -> list[str]:
    """Every file id a render's checks name (:data:`_FILE_KEYS`), wherever they sit."""
    found: list[str] = []
    if isinstance(value, dict):
        for key, item in value.items():
            if key in _FILE_KEYS and isinstance(item, str) and safe_uuid(item) is not None:
                found.append(item)
            else:
                found.extend(_file_ids(item))
    elif isinstance(value, list):
        for item in value:
            found.extend(_file_ids(item))
    return found


def _renamed(value: object, ids: dict[str, str]) -> object:
    """*value* with every file id of :data:`_FILE_KEYS` in *ids* replaced by its copy; one without a copy dropped."""
    if isinstance(value, dict):
        out = {}
        for key, item in value.items():
            if key in _FILE_KEYS and isinstance(item, str) and safe_uuid(item) is not None:
                if item in ids:
                    out[key] = ids[item]
                continue
            out[key] = _renamed(item, ids)
        return out
    if isinstance(value, list):
        return [_renamed(item, ids) for item in value]
    return value


async def _copy(file_id: str, workdir: Path, *, user_id: object, name: str | None = None) -> Any | None:
    """A stored file copied as a new file of *user_id* (its name - *name* when given -, type and description kept);
    ``None`` when it is gone."""
    from app.services.video_studio import assets

    target = workdir / f"copy-{uuid.uuid4().hex}"
    row = await assets.download_file(file_id, str(target))
    if row is None:
        return None
    return await assets.store_path(
        str(target),
        filename=name or row.filename,
        user_id=user_id,
        source=assets.SOURCE_RENDER,
        content_type=row.content_type,
        description=row.description,
    )


async def serve(render_id: str, source: Kept) -> bool:
    """Make the preview *render_id* from the kept preview *source* on this worker; ``False`` when it could
    not - its source gone meanwhile, a file that cannot be read -: the preview is queued again as it was, its job
    (held behind :data:`SERVE_GRACE_SECONDS`) due at once, and a worker draws it as before.

    Its MP4 is the kept one encoded into a file of its own; its three frames and every file its checks name are
    copied as files of its person, its checks carried with them; it is finished like any preview (``render.finish_made``:
    what it drew recorded on the video, the retention applied)."""
    served = False
    try:
        served = await _serve(render_id, source)
    except Exception as exc:  # drawn by its job instead
        logger.warning("Video preview %s not served from %s: %s", render_id, source.render_id, format_exception_message(exc))
    if not served:
        await _give_back(render_id)
    return served


async def _give_back(render_id: str) -> None:
    """A preview not served: queued again as it was, its held job due now."""
    from app.core.database import async_session
    from app.models.video_studio import JOB_PENDING, JOB_RUNNING, JOB_TERMINAL_STATUSES, VideoRender
    from app.services.video_studio import jobs

    async with async_session() as db:
        row = await db.get(VideoRender, uuid.UUID(render_id), with_for_update=True)
        if row is None or row.status in JOB_TERMINAL_STATUSES:
            return
        if row.status == JOB_RUNNING:
            row.status, row.stage, row.progress, row.started_at = JOB_PENDING, "Queued", 0, None
        await jobs.due_now(db, row)
        await db.commit()
    await jobs.wake()


async def _serve(render_id: str, source: Kept) -> bool:
    from app.core.database import async_session
    from app.models.video_studio import JOB_READY, JOB_RUNNING, JOB_TERMINAL_STATUSES, VideoProject, VideoRender
    from app.services.video_studio import assets, encoder, render, render_checks
    from app.services.video_studio.runners import _slug

    began = time.monotonic()
    async with async_session() as db:
        row = await db.get(VideoRender, uuid.UUID(render_id), with_for_update=True)
        kept_row = await db.get(VideoRender, uuid.UUID(source.render_id))
        if row is None or row.status in JOB_TERMINAL_STATUSES or kept_row is None or kept_row.status != JOB_READY:
            return False
        project = await db.get(VideoProject, row.project_id)
        title = project.title if project is not None else "video"
        user_id, scene_id = row.user_id, row.scene_id
        made_from = {
            "file_id": kept_row.file_id,
            "frame_file_ids": list(kept_row.frame_file_ids or []),
            "checks": copy.deepcopy(kept_row.checks) if isinstance(kept_row.checks, dict) else {},
            "notes": [note for note in kept_row.notes or [] if isinstance(note, str)],
            "music": kept_row.music,
            "asset_refs": kept_row.asset_refs,
            "fps": kept_row.fps,
        }
        row.status = JOB_RUNNING
        row.started_at = row.progress_at = render._now()
        row.progress = 50
        row.stage = f"Encoding its {source.frames:,} cached frames"
        await db.commit()
    stored: list = []
    try:
        with tempfile.TemporaryDirectory(prefix="xtm-kept-preview-") as work:
            folder = Path(work)
            kept_mp4 = folder / "kept.mp4"
            if await assets.download_file(made_from["file_id"], str(kept_mp4)) is None:
                return False
            out = folder / "preview.mp4"
            await encoder.remux(source_path=str(kept_mp4), out_path=str(out))
            info = await encoder.probe(str(out))
            base = f"{_slug(title)}-{render_id[:8]}-{scene_id}"
            shown = await assets.load_file(made_from["file_id"])
            video = await assets.store_path(
                str(out),
                filename=f"{base}.mp4",
                user_id=user_id,
                source=assets.SOURCE_RENDER,
                content_type="video/mp4",
                description=shown.description if shown is not None else None,
            )
            stored.append(video.id)
            frames = []
            for index, file_id in enumerate(made_from["frame_file_ids"]):
                copied = await _copy(str(file_id), folder, user_id=user_id, name=f"{base}-{('first', 'mid', 'last', 'frame')[min(index, 3)]}.jpg")
                if copied is not None:
                    frames.append(copied)
                    stored.append(copied.id)
            renamed: dict[str, str] = {}
            for file_id in dict.fromkeys(_file_ids(made_from["checks"])):
                copied = await _copy(file_id, folder, user_id=user_id)
                if copied is not None:
                    renamed[file_id] = str(copied.id)
                    stored.append(copied.id)
        checks = render_checks.Checks.from_dict(_renamed(made_from["checks"], renamed))
        count = int(info.frame_count or source.frames)
        seconds = time.monotonic() - began
        said = cached_line(count, seconds, source.render_id)
        made = {
            "width": info.width,
            "height": info.height,
            "fps": made_from["fps"],
            "duration_ms": int(info.duration * 1000),
            "size_bytes": video.size,
            "file_id": video.id,
            "poster_file_id": None,
            "captions_file_id": None,
            "captions_burned_in": False,
            "frame_file_ids": [str(f.id) for f in frames] or None,
            "chapters": None,
            "notes": [said, *made_from["notes"]][:40],
            "music": made_from["music"],
            "asset_refs": made_from["asset_refs"],
            # What it is made from, so a preview asked after it is served from it in turn; no frame drawn, so no rate
            # the next pre-flights read.
            "measured": {
                "kept_as": source.inputs.digest,
                "kept_from": source.render_id,
                "frames_total": count,
                "frames_cached": count,
                "encode_seconds": round(seconds, 2),
            },
        }
        finished = await render.finish_made(
            render_id,
            stored,
            made,
            whole=False,
            checks=checks,
            scenes_drawn=source.inputs.whole,
            scenes_content=source.inputs.content,
            scenes_joined=source.inputs.joined,
            scenes_parts=source.inputs.parts,
        )
        if finished:
            logger.info("Video preview %s: %s", render_id, said)
        return finished
    except BaseException:
        await assets.delete_files(stored)
        raise


async def inputs_or_none(db: Any, effective: dict, template: Any, **kwargs: Any) -> KeptInputs | None:
    """:func:`inputs`, ``None`` when they cannot be read: a preview whose inputs are unknown is drawn, never served."""
    try:
        return await inputs(db, effective, template, **kwargs)
    except Exception as exc:
        logger.info("What a scene preview is made from was not read: %s", format_exception_message(exc))
        return None


__all__ = ["SERVE_GRACE_SECONDS", "Kept", "KeptInputs", "cached_line", "inputs", "inputs_or_none", "kept", "serve"]
