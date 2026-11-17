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

"""The live preview of a video in the editor: the render's compositor, in the browser.

The editor shows a video as it will render without rendering it. It loads the
compositor page the worker drives in headless Chromium (``compositor_page``)
and feeds it the same frame-exact document ``timeline.plan`` builds for a
render - of the DRAFT spec, the template applied - with one difference: the
recordings and clips are played by video elements from confined media
URLs (``media.mint_media_token``) instead of frames extracted to disk. One
planner for the render, the preview and the editor's timeline, so the three
never disagree on when a scene starts.

What a preview cannot know before a render is estimated, never invented:
an image's size (the page fits it itself) and a deck slide's picture (the
deck renders at render time: the preview draws a card naming the slide). The
narration is spoken from the render's own voice cache: a spoken one
plays at its measured length, one a manager's draft has not spoken yet is
spoken in the background and paced at the planner's reading speed meanwhile. The music is the bed the render
prepares - a built-in score composed for the draft, a track fitted
to it - at its level, with the ducking envelope of the paced narration. A recording or an AI shot still being
made, or one that failed, never fails the preview: its scene says so where it
plays, a failure with its reason.
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.base_path import with_base_path
from app.models.file_attachment import FileAttachment
from app.models.user import User
from app.models.video_studio import JOB_CANCELLED, JOB_FAILED, JOB_READY, VideoClip, VideoMusic, VideoProject, VideoTake
from app.services.video_studio import beats, lexicon_store, media, motion_design, templates, themes, timeline
from app.services.video_studio.spec import (
    SpecProblem,
    VideoSpecError,
    authored_spec,
    clip_sound_choice,
    painted_size,
    parse_video_spec,
)

logger = logging.getLogger(__name__)

#: The frame rate the preview plans at: the editor scrubs and plays it, the render picks its own.
PREVIEW_FPS = 30


def _media_url(file_id: object, user: User) -> str | None:
    """A confined URL to a file, the same for every preview planned within a token window."""
    if not file_id:
        return None
    token, _expires = media.mint_media_token(uuid.UUID(str(file_id)), user.id, windowed=True)
    return with_base_path(f"/api/v1/video-studio/media/{token}")


@dataclass
class _PreviewMedia:
    """The media a preview plays: each key's URL, and what it plays.

    ``ids`` names the file (or the figure still) behind each URL: the editor's
    stage keeps playing what it loaded for it when a later preview brings a
    newer token of the same one, and swaps only for another file.
    """

    user: User
    urls: dict[str, str] = field(default_factory=dict)
    ids: dict[str, str] = field(default_factory=dict)

    def add(self, key: str, file_id: object) -> bool:
        url = _media_url(file_id, self.user)
        if url:
            self.put(key, url, str(file_id))
        return bool(url)

    def put(self, key: str, url: str, identity: str) -> None:
        self.urls[key] = url
        self.ids[key] = identity


async def _clip_poster(db: AsyncSession, clip_id: str) -> object | None:
    """The poster of the take an AI shot plays (its kept one while generated again), ``None`` before it has one."""
    from app.services.video_studio import clips

    clip = await db.get(VideoClip, uuid.UUID(clip_id))
    return clip.poster_file_id if clip is not None and clips.playable(clip) else None


@dataclass
class _ClipPlaceholder:
    """A video's planning numbers before its file is probed (the render probes it)."""

    duration: float = 4.0
    width: int = 1920
    height: int = 1080
    has_audio: bool = False

    def media(self) -> timeline.ClipMedia:
        return timeline.ClipMedia(
            duration=self.duration,
            width=self.width,
            height=self.height,
            fps=24.0,
            frame_count=max(1, int(self.duration * 24)),
            has_audio=self.has_audio,
        )


async def _clip(db: AsyncSession, clip_id: str) -> tuple[object, _ClipPlaceholder, dict | None]:
    """An AI shot as the preview plays it: ``(file id or None, planning numbers, state or None)``.

    A ready shot generated again plays the take it keeps until the new one is ready (``clips.playable``).
    """
    from app.services.video_studio import clips

    clip = await db.get(VideoClip, uuid.UUID(clip_id))
    if clip is None or clip.expired_at is not None:
        # Never "being generated" forever: a deleted shot, or one whose media
        # the retention removed, says so.
        reason = "The AI shot no longer exists." if clip is None else "Its media expired with the retention."
        state = {"state": "failed", "kind": "clip", "clip_id": clip_id, "error": reason}
        return None, _ClipPlaceholder(), {**state, "missing": True} if clip is None else state
    if clips.playable(clip):
        numbers = _ClipPlaceholder(
            duration=(clip.duration_ms or 4000) / 1000,
            width=clip.width or 1920,
            height=clip.height or 1080,
            has_audio=bool(clip.has_audio),
        )
        return clip.file_id, numbers, None
    numbers = _ClipPlaceholder(duration=float((clip.params or {}).get("duration_seconds") or 4))
    if clip.status in (JOB_FAILED, JOB_CANCELLED):
        return (
            None,
            numbers,
            {"state": "failed", "kind": "clip", "clip_id": clip_id, "error": clip.error or clip.status},
        )
    return None, numbers, {"state": "clip", "clip_id": clip_id, "progress": clip.progress}


async def planned_clip(db: AsyncSession, clip_id: str) -> timeline.ClipMedia:
    """An AI shot's planning numbers: its file's once made, the length it was asked for meanwhile."""
    _file_id, numbers, _state = await _clip(db, clip_id)
    return numbers.media()


async def overlay_picture_size(db: AsyncSession, file_id: object) -> tuple[int, int] | None:
    """The pixel size of an image overlay's picture; ``None`` when it is gone or no picture.

    Read from the geometry stored with the file (``image_meta``), else read
    once from its bytes and stored with it, as a logo wall reads a mark.
    """
    from app.services.builtin_tools._shared import _store_image_meta
    from app.services.file_storage import file_storage
    from app.services.image_geometry import image_geometry

    row = await db.get(FileAttachment, uuid.UUID(str(file_id)))
    if row is None or not (row.content_type or "").startswith("image/"):
        return None
    meta = row.image_meta if isinstance(row.image_meta, dict) else None
    if not (meta and isinstance(meta.get("w"), int) and isinstance(meta.get("h"), int)):
        try:
            meta = await image_geometry(await file_storage.download(row.s3_object_name), row.content_type)
        except Exception:  # a picture that cannot be read is left out of the preview, like the render
            return None
        if not meta:
            return None
        await _store_image_meta(row.id, meta)
    width, height = meta.get("w"), meta.get("h")
    return (int(width), int(height)) if width and height else None


async def insert_source(db: AsyncSession, kind: str, ref: str) -> tuple[object, timeline.ClipMedia | timeline.ImageMedia] | None:
    """A screen insert's source as the preview plans it: ``(file id, planning numbers)``, ``None`` when not ready.

    A picture is sized from its stored geometry; a video plans at its own
    length once made (a recording, an AI shot), else at a placeholder's.
    """
    if kind == "take":
        take = await db.get(VideoTake, uuid.UUID(ref))
        if take is None or take.status != JOB_READY or not take.file_id:
            return None
        width, height = painted_size((take.viewport_width or 1600, take.viewport_height or 900), take.pixel_ratio or 1.0)
        return take.file_id, _ClipPlaceholder((take.duration_ms or 4000) / 1000, width, height).media()
    if kind == "clip":
        file_id, numbers, state = await _clip(db, ref)
        return (file_id, numbers.media()) if file_id and state is None else None
    row = await db.get(FileAttachment, uuid.UUID(ref))
    if row is None:
        return None
    if (row.content_type or "").startswith("video/"):
        return row.id, _ClipPlaceholder().media()
    size = await overlay_picture_size(db, row.id)
    return (row.id, timeline.ImageMedia(*size)) if size is not None else None


async def _inserts(db: AsyncSession, spec: Any, plan_media: timeline.PlanMedia, played: _PreviewMedia, palette: dict) -> None:
    """The screen inserts' sources, a wall's pane by pane: a brand mark by its own file of the template,
    never another."""
    plan_media.inserts_gathered = True
    marks = timeline.brand_asset_files(spec, palette)
    for key, part in timeline.insert_sources(spec).items():
        kind, ref = part.kind, part.ref
        if kind == "asset":
            if not marks.get(ref):
                continue
            kind, ref = "file", str(marks[ref])
        found = await insert_source(db, kind, ref)
        if found is not None and played.add(key, found[0]):
            plan_media.inserts[key] = found[1]


async def _theme(
    db: AsyncSession, spec: Any, template_id: object, user: User
) -> tuple[dict, dict[str, object], list[str], dict]:
    """The brand palette the render would resolve, the files of its logo and mark, notes, and the palette."""
    palette: dict = {}
    notes: list[str] = []
    if template_id:
        from app.core.utils import format_exception_message
        from app.services.presentations import resolve_template_theme

        try:
            palette = await resolve_template_theme(
                db,
                uuid.UUID(str(template_id)),
                user_id=user.id,
                deck_theme=spec.theme.mode,
                is_admin=user.role == "admin",
            )
        except Exception as exc:
            notes.append(f"The template's palette could not be read: {format_exception_message(exc)}")
    brand: dict[str, object] = {}
    logo_id = timeline.palette_marks(spec, palette)["logo"]
    if logo_id:
        brand["logo"] = logo_id
    if palette.get("mark_file_id") and not spec.theme.logo_file_id:
        brand["mark"] = palette["mark_file_id"]
    theme = themes.build_theme(
        mode=spec.theme.mode,
        background_style=spec.theme.background,
        template_palette=palette,
        accent=spec.theme.accent,
        accent_2=spec.theme.accent_2,
        background_color=spec.theme.background_color,
        text_color=spec.theme.text_color,
        has_logo="logo" in brand,
        has_mark="mark" in brand,
        motion=spec.theme.motion,
    )
    return theme, brand, notes, palette


def figure_still_url(digest: str, user: User) -> str:
    """A confined URL to a figure's still (``GET /video-studio/figure-preview/{token}``)."""
    token, _expires = media.mint_figure_token(digest, user.id)
    return with_base_path(f"/api/v1/video-studio/figure-preview/{token}")


def figure_build_url(digest: str, user: User) -> str:
    """A confined URL to a figure's build (``GET /video-studio/figure-build/{token}``), played by ranges."""
    token, _expires = media.mint_figure_token(digest, user.id)
    return with_base_path(f"/api/v1/video-studio/figure-build/{token}")


async def _figure_stills(
    db: AsyncSession,
    spec: Any,
    palette: dict,
    user: User,
    *,
    played: _PreviewMedia,
    states: dict,
    unsized: set,
    draw: bool,
) -> dict:
    """Each figure scene whose still is drawn plays it, instead of the words naming the figure: those scenes, by id.

    The still is found by the digest of the figure as *user* resolves it (its
    deck as they may read it, the palette of the template they see) and
    shown only when they may read every picture it draws. *draw*: a still not
    drawn yet is handed to the worker (``figure_stills.enqueue``) and
    its scene says so (``drawing``), so the editor polls until it is; without
    *draw*, a still already queued (by a save) says so too.
    """
    from app.core.utils import format_exception_message
    from app.services.video_studio import figure_stills, figures

    shown: dict = {}
    try:
        stills = await figures.figure_previews(db, user, spec, palette)
        missing: list[figures.FigurePreview] = []
        for sid, still in stills.items():
            stat = await figures.preview_stat(still.digest)
            if stat is None:
                missing.append(still)
            elif await figures.pictures_readable(db, still.source, user):
                played.put(f"image/{sid}", figure_still_url(still.digest, user), still.digest)
                states.pop(sid, None)
                unsized.discard(sid)
                shown[sid] = still
                # Shown, so used: the retention sweep never reaches it.
                await figure_stills.keep(still.digest, stat)
        if missing:
            # A preview that does not ask (a reader's, the saved video's) still polls a still a save queued.
            drawing = (
                await figure_stills.enqueue(missing, user)
                if draw
                else {still.digest for still in missing if await figures.being_drawn(still.digest)}
            )
            for still in missing:
                if still.digest in drawing and still.scene_id in states:
                    states[still.scene_id] = {**states[still.scene_id], "drawing": True}
    except Exception as exc:  # the preview words the figure, as it did before stills
        logger.info("Figure stills not shown in a preview: %s", format_exception_message(exc))
    return shown


async def _figure_builds(plan: Any, shown: dict, user: User, *, plan_media: Any, played: _PreviewMedia, draw: bool) -> tuple[dict, set]:
    """The figures whose build is drawn for the scene as *plan* times it play it: ``(scene -> when it is whole, scenes drawing)``.

    *shown*: the figure scenes whose still plays (:func:`_figure_stills`). A
    build plays as the render's drawn figure does - a clip of the scene's
    length - its still the poster the stage and the timeline show while it
    loads. *draw*: a build not drawn yet is drawn in the background
    (``figures.prepare_builds``), its scene marked ``preview_building``.
    """
    from app.core.utils import format_exception_message
    from app.services.video_studio import figures

    whole: dict[str, float] = {}
    building: set[str] = set()
    try:
        missing = []
        for sid, still in shown.items():
            timing = figures.figure_timing(plan, sid)
            if timing is None or still.scene is None:
                continue
            digest = figures.build_digest(still, timing)
            meta = await figures.read_build(digest)
            if meta is None:
                missing.append((still, timing, digest))
                continue
            plan_media.clips[sid] = timeline.ClipMedia(
                duration=float(meta["duration"]),
                width=int(meta["width"]),
                height=int(meta["height"]),
                fps=float(meta["fps"]),
                frame_count=int(meta["frames"]),
            )
            played.put(f"clip/{sid}", figure_build_url(digest, user), digest)
            played.put(f"poster/{sid}", played.urls[f"image/{sid}"], still.digest)
            whole[sid] = float(meta["whole"])
        if draw and missing:
            await figures.prepare_builds(missing, user)
            building = {still.scene_id for still, _timing, _digest in missing}
    except Exception as exc:  # the stage shows the still, as before builds
        logger.info("Figure builds not shown in a preview: %s", format_exception_message(exc))
    return whole, building


def music_bed_url(bed: Any, user: User) -> str:
    """A confined URL to a music bed (``GET /video-studio/music-bed/{token}``)."""
    token, _expires = media.mint_music_token(bed.claims(), user.id)
    return with_base_path(f"/api/v1/video-studio/music-bed/{token}")


async def _music(db: AsyncSession, spec: Any, plan: Any, user: User) -> dict | None:
    """The bed the render will play, as the editor plays it: its URL, its level and its ducking envelope.

    An AI track still being composed, or one that failed, says so (``state``)
    and plays nothing, like an AI shot.
    """
    from app.services.video_studio import encoder
    from app.services.video_studio.music import Bed

    music = spec.music
    if music is None or not music.enabled:
        return None
    payload: dict[str, Any] = {"url": None, "volume": music.volume, "duck": None, "source": None, "state": None}
    if music.ducking:
        payload["duck"] = [
            list(point) for point in encoder.duck_points(plan.duck_windows(), depth_db=music.duck_db, duration=plan.duration)
        ]
    source = music.source
    if source is None:
        payload["state"] = {"state": "none"}
        return payload
    payload["source"] = source[0]
    if source[0] == "music":
        row = await db.get(VideoMusic, uuid.UUID(source[1]))
        if row is None or row.status in (JOB_FAILED, JOB_CANCELLED):
            reason = "The AI track no longer exists." if row is None else (row.error or row.status)
            payload["state"] = {"state": "failed", "music_id": source[1], "error": reason}
            return payload
        if row.status != JOB_READY or not row.file_id:
            payload["state"] = {"state": "music", "music_id": source[1], "progress": row.progress}
            return payload
    bed = Bed.of(music, plan.duration, plan.cues())
    if bed is not None:
        payload["url"] = music_bed_url(bed, user)
        # What the bed is, whatever its token: the editor's player keeps its source while it is the same.
        payload["key"] = bed.digest()
    return payload


async def _voice(
    db: AsyncSession, spec: Any, user: User, *, speak: bool, lexicon: Any = None
) -> tuple[dict | None, dict[str, dict]]:
    """The narration's voice as the editor plays it: the voice-over's state and, per narrated scene, its line.

    Spoken with the video's pronunciation lexicon (*lexicon*), as the render speaks it.

    A line is ``ready`` (spoken: its measured ``seconds`` and the
    ``audio_url`` that plays it), ``speaking`` (being spoken for this
    preview), ``failed`` (with the ``error``) or ``none`` (not spoken yet: a
    reader's preview, or AI is off). Only a manager's draft (*speak*) has its
    missing narrations spoken, into the cache the render reads.
    """
    import asyncio

    from app.core.utils import format_exception_message
    from app.services.video_studio import voice

    voiceover = spec.voiceover
    lines = voice.lines_of(spec)
    if not lines:
        return None, {}
    state: dict[str, Any] = {"state": "on", "volume": voiceover.volume, "error": None}
    if not voiceover.enabled:
        return {**state, "state": "off"}, {}
    try:
        target = await voice.resolve(db, user.id, voiceover)
    except voice.VoiceUnavailableError as exc:
        return {**state, "state": "unavailable", "error": str(exc)}, {}
    except Exception as exc:  # a provider that cannot be read never fails the preview
        return {**state, "state": "unavailable", "error": format_exception_message(exc)[:300]}, {}
    digests = [(line, voice.digest_of(target, line, voiceover, lexicon)) for line in lines]
    lengths = await asyncio.gather(*(voice.spoken_seconds(digest) for _line, digest in digests))
    spoken: dict[str, dict] = {}
    missing: list[tuple[voice.Line, str]] = []
    for (line, digest), seconds in zip(digests, lengths, strict=True):
        if seconds:
            token, _expires = media.mint_voice_token(digest, user.id)
            url = with_base_path(f"/api/v1/video-studio/voice/{token}")
            spoken[line.scene_id] = {"state": "ready", "seconds": seconds, "audio_url": url, "key": digest}
        elif reason := await voice.failure(digest):
            spoken[line.scene_id] = {"state": "failed", "error": reason}
        else:
            spoken[line.scene_id] = {"state": "none"}
            missing.append((line, digest))
    if speak and missing:
        speaking = await voice.prepare(target, user, missing, voiceover, lexicon)
        for line, digest in missing:
            if digest in speaking:
                spoken[line.scene_id] = {"state": "speaking"}
    return state, spoken


async def spoken_lengths(db: AsyncSession, spec: Any, user: User) -> dict[str, float]:
    """``scene id -> seconds`` of each narration of *spec* already spoken in its voice, read from the voice cache as the
    editor's preview plans it (:func:`_voice`): the length a render places it at. Nothing is spoken here; a
    narration not spoken yet, or a voice that cannot be read, has none."""
    from app.core.utils import format_exception_message

    try:
        lexicon = await lexicon_store.for_voiceover(db, spec.voiceover)
        _state, lines = await _voice(db, spec, user, speak=False, lexicon=lexicon)
    except Exception as exc:  # the reading pace then, as before anything was spoken
        logger.info("Spoken narration lengths not read: %s", format_exception_message(exc))
        return {}
    return {scene_id: float(line["seconds"]) for scene_id, line in lines.items() if line.get("seconds")}


async def build_preview(
    db: AsyncSession,
    project: VideoProject,
    user: User,
    raw: Any | None = None,
    *,
    speak: bool = False,
    draw: bool = False,
) -> dict:
    """The preview of *project*, or of the draft *raw* spec of it, as *user* sees it.

    *speak*: the narrations not spoken yet are spoken in the background
    (``_voice``), for a manager's draft while AI is on - the caller asks the
    switch, at request entry. *draw*: the figures' stills not drawn yet are
    drawn in the background, for a manager's draft. Raises :class:`VideoSpecError` for a
    draft that does not validate: the editor shows the problems where they are
    and keeps the last good preview.
    """
    from app.services.video_studio.projects import effective_spec, media_state, take_media, verify_references

    if raw is None:
        applied = await effective_spec(db, project, user)
        template_id = project.template_container_id if not (project.spec or {}).get("freestyle") else None
    else:
        authored = templates.keep_look(authored_spec(raw), project.spec)
        problems = await verify_references(db, authored, user, held=dict(project.spec or {}))
        if problems:
            raise VideoSpecError(problems)
        container = await templates.resolve_for_spec(db, user, authored, current_id=project.template_container_id)
        applied = templates.apply_template(authored, await templates.load_video_template(db, container))
        template_id = container.id if container is not None else None
    spec = parse_video_spec(applied.spec)

    plan_media = timeline.PlanMedia()
    played = _PreviewMedia(user)
    #: What a scene shows instead of its media in the preview: ``slide`` (rendered
    #: at render time), ``recording`` / ``clip`` (still being made), ``failed``
    #: (with the reason, and the AI shot to generate again). The editor writes
    #: the words, in the reader's language.
    states: dict[str, dict] = {}
    #: The name of the recording or the AI shot a scene plays: what the timeline calls the scene.
    names: dict[str, str] = {}
    unsized: set[str] = set()
    notes: list[str] = list(applied.notes)
    for scene in spec.scenes:
        sid = scene.id or ""
        if scene.type == "recording":
            take = await db.get(VideoTake, uuid.UUID(scene.take_id))
            if take is not None and take.name:
                names[sid] = take.name
            if take is not None and take.status == JOB_READY and take.file_id:
                plan_media.takes[scene.take_id] = take_media(take)
                played.add(f"take/{sid}", take.file_id)
                # What the stage and the timeline show while the recording loads.
                played.add(f"poster/{sid}", take.poster_file_id)
            elif take is None:
                # Deleted since the video named it: never "recording" forever, nor a refused preview.
                states[sid] = {
                    "state": "failed",
                    "kind": "recording",
                    "missing": True,
                    "error": "The recording no longer exists.",
                }
            elif take.expired_at is not None:
                states[sid] = {"state": "failed", "kind": "recording", "error": "Its media expired with the retention."}
            elif take.status in (JOB_FAILED, JOB_CANCELLED):
                states[sid] = {"state": "failed", "kind": "recording", "error": take.error or take.status}
            else:
                states[sid] = {"state": "recording"}
        elif scene.type == "clip":
            file_id = scene.file_id
            # An upload is not probed before the render: its video element plays whatever sound it has.
            clip_media = _ClipPlaceholder(has_audio=True)
            if scene.clip_id:
                file_id, clip_media, state = await _clip(db, scene.clip_id)
                if state is not None:
                    states[sid] = state
                clip = await db.get(VideoClip, uuid.UUID(scene.clip_id))
                if clip is not None and (clip.name or clip.prompt):
                    names[sid] = (clip.name or clip.prompt or "")[:120]
            plan_media.clips[sid] = clip_media.media()
            if played.add(f"clip/{sid}", file_id) and scene.clip_id:
                played.add(f"poster/{sid}", await _clip_poster(db, scene.clip_id))
        elif scene.type == "statement" and scene.backdrop is not None:
            backdrop = scene.backdrop
            if backdrop.clip_id:
                file_id, clip_media, state = await _clip(db, backdrop.clip_id)
                plan_media.backdrops[sid] = clip_media.media()
                if state is not None:
                    states[sid] = {**state, "backdrop": True}
            else:
                file_id = backdrop.file_id
                row = await db.get(FileAttachment, uuid.UUID(backdrop.file_id))
                is_video = bool(row and (row.content_type or "").startswith("video/"))
                plan_media.backdrops[sid] = (
                    _ClipPlaceholder().media() if is_video else timeline.ImageMedia(width=1920, height=1080)
                )
            if played.add(f"backdrop/{sid}", file_id) and backdrop.clip_id:
                played.add(f"poster/{sid}", await _clip_poster(db, backdrop.clip_id))
        elif scene.type == "image":
            animated = None
            if scene.animate is not None and scene.animate.clip_id:
                animated, clip_media, _state = await _clip(db, scene.animate.clip_id)
            if animated:
                # Its animation is made: the preview plays it, as the render will.
                plan_media.clips[sid] = clip_media.media()
                played.add(f"clip/{sid}", animated)
                played.add(f"poster/{sid}", await _clip_poster(db, scene.animate.clip_id))
            else:
                played.add(f"image/{sid}", scene.file_id)
                # Effects and screen inserts are placed in shares of the image: the preview frames it at its real size,
                # as the render.
                placed = scene.effects or any(overlay.type == "screen_insert" for overlay in scene.overlays)
                size = await overlay_picture_size(db, scene.file_id) if placed and scene.file_id else None
                if size is not None:
                    plan_media.images[sid] = timeline.ImageMedia(*size)
                else:
                    unsized.add(sid)
        elif scene.type == "slide":
            states[sid] = {"state": "slide", "slide": scene.slide or 1}
            unsized.add(sid)
        elif scene.type == "figure":
            # Built in by the render (``figures.draw``): the preview plays its still drawn at the last
            # save (``_figure_stills``), else names it where it plays.
            kind = (scene.block_spec or {}).get("type") if scene.block_spec else None
            states[sid] = {"state": "figure", **({"block": kind} if kind else {"slide": scene.slide or 1})}
            unsized.add(sid)
    for scene in spec.scenes:
        if scene.type == "recording" and scene.take_id not in plan_media.takes:
            plan_media.takes[scene.take_id] = timeline.TakeMedia(
                take_id=scene.take_id,
                duration=10.0,
                css_width=1600,
                css_height=900,
                pixel_ratio=1.0,
                fps=60,
                frame_count=600,
            )

    theme, brand, theme_notes, palette = await _theme(db, spec, template_id, user)
    for key, file_id in brand.items():
        played.add(key, file_id)
    notes.extend(theme_notes)
    shown_figures = await _figure_stills(db, spec, palette, user, played=played, states=states, unsized=unsized, draw=draw)
    # The image overlays' pictures, sized as the render sizes them, so both place them alike.
    plan_media.overlays_gathered = True
    for key, file_id in timeline.overlay_file_ids(spec, palette).items():
        size = await overlay_picture_size(db, file_id) if file_id else None
        if size is not None and played.add(key, file_id):
            plan_media.overlays[key] = timeline.ImageMedia(*size)
    # A motion scene's Lottie files, checked as the render checks them before the page fetches one.
    plan_media.lotties_gathered = True
    for key, file_id in motion_design.lottie_files(spec.scenes).items():
        check = await motion_design.read_lottie(db, file_id)
        if check is not None and check.problem is None and played.add(key, file_id):
            plan_media.lotties[key] = check
    await _inserts(db, spec, plan_media, played, palette)
    lexicon = await lexicon_store.for_voiceover(db, spec.voiceover)
    plan_media.captions = timeline.caption_texts(spec, lexicon)
    voice_state, narration_lines = await _voice(db, spec, user, speak=speak, lexicon=lexicon)
    for scene_id, line in narration_lines.items():
        if line.get("seconds"):
            plan_media.voices[scene_id] = timeline.VoiceMedia(line["seconds"])
    # The beats the render lands the cuts and the builds on: the same grid, read from the same analysis.
    plan_media.beats, beat_note = await beats.video_grid(spec, db=db)
    if beat_note:
        notes.append(beat_note)
    # A narration not spoken yet is paced at the planner's speed, without the render's "no voice" note.
    planned_spec = spec.model_copy(update={"voiceover": spec.voiceover.model_copy(update={"enabled": False})})
    try:
        plan = timeline.plan(planned_spec, plan_media, fps=PREVIEW_FPS, theme=theme)
        figure_whole, figures_building = await _figure_builds(
            plan, shown_figures, user, plan_media=plan_media, played=played, draw=draw
        )
        if figure_whole:
            # A figure's length never depends on its drawing: planned again, the timeline is the same, its figures their builds.
            plan = timeline.plan(planned_spec, plan_media, fps=PREVIEW_FPS, theme=theme)
    except ValueError as exc:
        raise VideoSpecError([SpecProblem("scenes", str(exc))]) from None
    # Each screen insert over footage plays as the render draws it: keyed, tracked, moving.
    from app.services.video_studio import insert_builds

    figures_building = set(figures_building) | await insert_builds.attach(plan.document, spec, played, user, draw=draw)

    from app.services.video_studio import encoder

    # The clips' own sound goes down under the paced narration, as the render's does.
    spoken = plan.voice_windows()
    under_voice = (
        [list(point) for point in encoder.duck_points(spoken, depth_db=encoder.CLIP_DUCK_DB, duration=plan.duration)]
        if spoken
        else None
    )
    audio = {sound.scene_id: sound.volume for sound in plan.clip_audio}
    by_id = {scene.id: scene for scene in spec.scenes}
    for payload in plan.document["scenes"]:
        sid = payload.get("id") or ""
        if sid in audio:
            payload["preview_audio"] = {"volume": audio[sid], "duck": under_voice}
        if sid in plan.clip_sound:
            plays, reason = plan.clip_sound[sid]
            scene = by_id.get(sid)
            if sid in states and scene is not None and scene.type == "clip":
                # Not made yet: what will decide once it is, not "silent".
                plays, reason = clip_sound_choice(scene, spec)
            payload["preview_sound"] = {"plays": plays, "reason": reason}
        if sid in states:
            payload["preview_state"] = states[sid]
        if sid in names:
            payload["preview_name"] = names[sid]
        if sid in unsized:
            payload["preview_unsized"] = True
        if sid in figure_whole:
            payload["preview_figure_whole"] = figure_whole[sid]
        if sid in figures_building:
            payload["preview_building"] = True
    plan.document["preview_media"] = played.urls
    plan.document["preview_media_ids"] = played.ids

    narration = []
    for scene_id, start in plan.narration_starts.items():
        scene = by_id.get(scene_id)
        if scene is not None and scene.narration:
            line = narration_lines.get(scene_id, {"state": "none"})
            seconds = line.get("seconds") or timeline.estimate_narration_seconds(scene.narration, timeline.narration_speed(scene, spec))
            narration.append(
                {
                    "scene_id": scene_id,
                    "start": start,
                    "seconds": round(seconds, 3),
                    "text": scene.narration,
                    "state": line["state"],
                    "audio_url": line.get("audio_url"),
                    "key": line.get("key"),
                    "error": line.get("error"),
                }
            )
    music = await _music(db, spec, plan, user)
    media = await media_state(db, applied.spec)
    return {
        "revision": project.revision,
        "spec": applied.spec,
        "document": plan.document,
        "duration": plan.duration,
        "narration": narration,
        "voice": voice_state,
        "music": music,
        "template_scenes": list(applied.added_scenes),
        "notes": notes + plan.notes,
        # Changes when a shot, a recording or a track it shows does, without a save.
        "media_key": media.key,
        "regenerating": media.regenerating,
    }
