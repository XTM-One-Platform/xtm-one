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

"""A screen insert as the editor's preview plays it: drawn by the render's own compositor, ahead of time.

The editor's stage is the render's compositor page played from video
elements (``preview``): it cannot ask the driver for each frame of an
insert, so it used to map the source's first frame onto the quad statically,
neither moving, keyed nor tracked. A preview now has each screen insert over
footage drawn in the background (:func:`prepare`) through the very function
the render's frame server draws it with (``screen_insert.compose``): the
source at the scene's time, its key or its occlusion read on the footage
frame under it, its quad followed when it is tracked (``insert_tracking``),
its glow under it. The frames - the whole footage frame, transparent but for
the insert - are encoded as a VP9 WebM with its alpha (:data:`BUILD_FPS`, the
footage :data:`BUILD_WIDTH` wide), kept under the digest of what they are
drawn from (:func:`build_digest`), and the stage plays that clip over the
footage, in step with the scene (``compositor_page.previewBuild``). Until it
is drawn the stage maps the first frame as before and the scene says it is
being built (``preview_building``), so the editor polls.

What a render draws is what the preview showed: the same function, the same
inputs, only the size and the frame rate lighter. The bezel lock and the
matte's window (``screen_insert.Matting``) run over the build's own
frames, the frames after the one drawn read ahead (:func:`_with_lookahead`).
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import logging
import math
import tempfile
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from functools import partial
from pathlib import Path
from typing import Any

from app.core.utils import format_exception_message
from app.services.video_studio.spec import INSERT_DEFAULT_LIGHT

logger = logging.getLogger(__name__)

#: Where a build is kept, by the digest of what it is drawn from: the WebM and, beside it, its numbers.
BUILD_PREFIX = "video-studio/insert-builds"
#: Moved when a build is drawn differently, so every build is drawn again (2: the bezel lock and the flat surface;
#: 3: the footage's light over the insert; 4: a keyed insert's contain bands filled, the foreground's edge
#: despilled; 5: the key's hue in the residual margin filled with the bezel's colour; 6: the key's colour
#: limited out of the foreground's edge and the insert's light wrapped onto it; 7: the subject's own colours,
#: read over its clip, left on that edge; 8: a static unkeyed insert's bezel locked once, on its first frame;
#: 9: what stands in front never drawn over, read on the surface's clean plate).
BUILD_VERSION = 9
#: Lighter than a render's: fewer frames, the footage this wide.
BUILD_FPS = 15
BUILD_WIDTH = 960
#: The longest insert drawn ahead (seconds): past it, the stage maps its first frame.
MAX_BUILD_SECONDS = 60.0


@dataclass(frozen=True)
class InsertBuild:
    """One screen insert to draw for a preview: what it shows, on what, where and when (all in its scene's seconds)."""

    key: str
    scene_id: str
    footage_file: str
    source_file: str
    source_kind: str
    quad: tuple[tuple[float, float], ...]
    t0: float
    t1: float
    scene_map: tuple[tuple[float, float], ...]
    source_map: tuple[tuple[float, float], ...] = ()
    fit: str = "cover"
    glow: float = 0.0
    light: float = INSERT_DEFAULT_LIGHT
    track: bool = False
    key_spec: dict | None = None
    #: Its camera push (``timeline.insert_zoom``); ``None``: drawn at its fitted size.
    zoom: dict | None = None
    #: Its edge despill (``timeline.insert_despill``); ``None``: the compositor's default.
    despill: dict | None = None

    def digest(self) -> str:
        from app.services.video_studio import insert_tracking

        shown = {
            "version": BUILD_VERSION,
            "footage": self.footage_file,
            "source": [self.source_kind, self.source_file],
            "quad": [[round(x, 5), round(y, 5)] for x, y in self.quad],
            "when": [round(self.t0, 3), round(self.t1, 3)],
            "scene_map": [[round(a, 4), round(b, 4)] for a, b in self.scene_map],
            "source_map": [[round(a, 4), round(b, 4)] for a, b in self.source_map],
            "fit": self.fit,
            "glow": round(self.glow, 3),
            "light": round(self.light, 3),
            "track": self.track,
            "key": self.key_spec,
            # Only when it pushes in, or names its despill: a build drawn before either existed keeps its digest.
            **({"zoom": self.zoom} if self.zoom else {}),
            **({"despill": self.despill} if self.despill else {}),
            # A tracked one is followed as the tracker follows it now: an untracked build keeps its digest.
            **({"track_version": insert_tracking.TRACK_VERSION} if self.track else {}),
        }
        return hashlib.sha256(json.dumps(shown, sort_keys=True, default=str).encode("utf-8")).hexdigest()


def build_digest(build: InsertBuild) -> str:
    return build.digest()


def build_object(digest: str) -> str:
    return f"{BUILD_PREFIX}/{digest}.webm"


def build_meta_object(digest: str) -> str:
    return f"{BUILD_PREFIX}/{digest}.json"


async def read_build(digest: str) -> dict | None:
    """The build kept under *digest* - ``{duration, width, height, fps, frames}`` - or ``None`` when it is not drawn."""
    from app.services.file_storage import FileNotFoundInStorageError, file_storage

    try:
        meta = json.loads(await file_storage.download(build_meta_object(digest)))
    except (FileNotFoundInStorageError, ValueError):
        return None
    except Exception as exc:  # storage unreachable: the stage maps the first frame meanwhile
        logger.debug("Insert build %s not read: %s", digest, format_exception_message(exc))
        return None
    return meta if isinstance(meta, dict) and all(key in meta for key in ("duration", "width", "height", "fps")) else None


def builds_of(document: dict, ids: dict[str, str], spec: Any) -> list[InsertBuild]:
    """The screen inserts of a preview's *document* drawn ahead: each one over video footage whose files *ids* name.

    *ids* is the preview's ``preview_media_ids`` (the file behind each key);
    *spec* the parsed spec, for what the plan does not carry (the key, the
    tracking). An insert over a picture, or whose files the preview has not
    resolved, is mapped by the stage as before.
    """
    from app.services.video_studio import timeline

    parts = timeline.insert_sources(spec)
    found: list[InsertBuild] = []
    for scene in document.get("scenes") or []:
        footage = timeline.insert_footage(scene)
        if footage is None or timeline.footage_still(footage):
            continue
        footage_file = ids.get(str(footage.get("src") or ""))
        for payload in scene.get("inserts") or []:
            key = str(payload.get("src") or "")
            part = parts.get(key)
            source_file = ids.get(key)
            if part is None or not footage_file or not source_file:
                continue
            overlay = part.overlay
            t0, t1 = float(payload.get("t0") or 0.0), float(payload.get("t1") or 0.0)
            if t1 - t0 <= 0.05 or t1 - t0 > MAX_BUILD_SECONDS:
                continue
            found.append(
                InsertBuild(
                    key=key,
                    scene_id=str(scene.get("id") or ""),
                    footage_file=str(footage_file),
                    source_file=str(source_file),
                    source_kind=str(payload.get("kind") or "image"),
                    quad=tuple((float(x), float(y)) for x, y in payload.get("quad") or part.quad),
                    t0=t0,
                    t1=t1,
                    scene_map=tuple(tuple(map(float, p)) for p in footage.get("time_map") or []),
                    source_map=tuple(tuple(map(float, p)) for p in payload.get("time_map") or []),
                    fit=str(payload.get("fit") or "cover"),
                    glow=float(payload.get("glow") or 0.0),
                    light=float(payload.get("light", INSERT_DEFAULT_LIGHT)),
                    track=bool(overlay.track),
                    key_spec=overlay.key.model_dump() if overlay.key is not None else None,
                    zoom=payload.get("zoom"),
                    despill=timeline.insert_despill(overlay),
                )
            )
    return found


async def _picked_frames(path: str, size: tuple[int, int], wanted: list[int]) -> Any:
    """The frames *wanted* (non-decreasing indices) of *path* at *size*, in order, each ``H x W x 3`` uint8."""
    import numpy as np

    from app.services.video_studio import encoder

    if not wanted:
        return
    width, height = size
    position, last = 0, None
    frames = encoder.iter_rgb_frames(path, width=width, height=height, resize=True)
    async with contextlib.aclosing(frames):
        index = -1
        async for raw in frames:
            index += 1
            last = raw
            while position < len(wanted) and wanted[position] <= index:
                yield np.frombuffer(raw, dtype=np.uint8).reshape(height, width, 3)
                position += 1
            if position >= len(wanted):
                return
    # A map asking past the footage's end holds its last frame, as the page does.
    while position < len(wanted) and last is not None:
        yield np.frombuffer(last, dtype=np.uint8).reshape(height, width, 3)
        position += 1


def _even(value: float) -> int:
    return max(16, round(value / 2) * 2)


async def _with_lookahead(frames: Any, count: int, ahead: int) -> Any:
    """The *count* frames of the async iterator *frames*, each with the up to *ahead* frames after it.

    Yields ``(frame, [later frames])``: a build's matte reads the frames after the one drawn, as a render's does.
    """
    buffer: list[Any] = []
    while len(buffer) < min(count, ahead + 1):
        buffer.append(await anext(frames))
    taken = len(buffer)
    for _ in range(count):
        yield buffer[0], list(buffer[1:])
        buffer.pop(0)
        if taken < count:
            buffer.append(await anext(frames))
            taken += 1


async def _download(file_id: str, base: Path) -> tuple[str | None, Any]:
    from app.services.video_studio import assets

    row = await assets.download_file(file_id, str(base))
    if row is None:
        return None, None
    final = str(base) + (Path(row.filename or "").suffix.lower() or ".bin")
    Path(base).rename(final)
    return final, row


async def _draw(build: InsertBuild, digest: str) -> None:
    """Draw *build* into its WebM and keep it under *digest*: the render's compositor, frame by frame."""
    import asyncio
    import dataclasses

    import numpy as np
    from PIL import Image

    from app.services.file_storage import file_storage
    from app.services.video_studio import compositor, encoder, insert_tracking, screen_insert, timeline

    with tempfile.TemporaryDirectory(prefix="xtm-insert-build-") as work:
        folder = Path(work)
        footage_path, _row = await _download(build.footage_file, folder / "footage")
        source_path, source_row = await _download(build.source_file, folder / "source")
        if footage_path is None or source_path is None:
            raise RuntimeError("a file of the insert is gone")
        info = await encoder.probe(footage_path)
        fps = float(info.fps or 24.0)
        size = (_even(BUILD_WIDTH), _even(BUILD_WIDTH * info.height / max(info.width, 1)))
        content_type = getattr(source_row, "content_type", None) or ""
        if build.source_kind != "video" and (content_type.startswith("image/svg") or source_path.endswith(".svg")):
            try:
                source_path = await screen_insert.svg_to_png(source_path, f"Scene {build.scene_id}: {build.key}")
            except screen_insert.SvgNotDrawnError as exc:
                raise RuntimeError(f"its SVG could not be drawn: {exc}") from exc
        count = max(1, math.ceil((build.t1 - build.t0) * BUILD_FPS))
        times = [build.t0 + k / BUILD_FPS for k in range(count)]
        scene_map = [tuple(p) for p in build.scene_map]
        footage_frames = [max(0, round(timeline.map_forward(scene_map, t) * fps)) for t in times]
        first = footage_frames[0]
        track = None
        if build.track:
            asked = insert_tracking.Asked(
                build.quad, first, max(footage_frames), f"Scene {build.scene_id}: its screen insert", key=screen_insert.KeySpec.of(build.key_spec)
            )
            [track] = await insert_tracking.track_all(footage_path, [asked])
        insert = screen_insert.InsertSource(
            source_path,
            build.source_kind,
            build.quad,
            fit=build.fit,
            glow=build.glow,
            light=build.light,
            track=track,
            key=screen_insert.KeySpec.of(build.key_spec),
            footage=screen_insert.Footage(footage_path, "video", fps),
            first_frame=first,
            plate=screen_insert.plate_frames(first, max(footage_frames)),
            zoom=screen_insert.Zoom.of(build.zoom),
            despill=screen_insert.Despill.of(build.despill),
        )
        if insert.key is not None:
            # What stands in front over its clip: read once, as the save and the render read it.
            try:
                subject = await compositor.read_subject(insert, max(footage_frames))
            except (OSError, RuntimeError, ValueError) as exc:
                logger.info("Insert build %s: what stands in front not read over its clip: %s", build.key, format_exception_message(exc))
                subject = None
            insert = dataclasses.replace(insert, subject=subject)
        picture = None
        source_frames: list[int] = []
        source_size = (1, 1)
        if build.source_kind == "video":
            source_info = await encoder.probe(source_path)
            source_fps = float(source_info.fps or 24.0)
            last_source = max(0, int(source_info.frame_count or 1) - 1)
            source_map = [tuple(p) for p in build.source_map]
            source_frames = [min(last_source, max(0, round(timeline.map_forward(source_map, t) * source_fps))) for t in times]
            source_size = (_even(min(source_info.width, 1920)), _even(min(source_info.width, 1920) * source_info.height / max(source_info.width, 1)))
        else:
            picture = await asyncio.to_thread(screen_insert.read_picture, source_path)
        surface = fill = lock = None
        # The bezel lock and the last mattes carried frame to frame, as a render's page carries them.
        state = screen_insert.Matting()
        half = screen_insert.MATTE_WINDOW // 2
        async with (
            contextlib.aclosing(_picked_frames(footage_path, size, footage_frames)) as footage_iter,
            contextlib.aclosing(_picked_frames(source_path, source_size, source_frames)) as source_iter,
            contextlib.aclosing(_with_lookahead(footage_iter, count, half)) as windowed,
        ):
            index = -1
            async for under, later in windowed:
                index += 1
                if index == 0 and insert.key is not None:
                    insert = await asyncio.to_thread(screen_insert.keyed_on, insert, under)
                    # A static keyed insert fills its quad: the surface of its first frame, the key taken out.
                    fill = await asyncio.to_thread(screen_insert.static_fill, insert, under, size)
                if index == 0 and (insert.key is None or fill is not None):
                    # Its reference: what enters the quad through its rim stays in front (a filled key reads no key),
                    # read on its clean surface over the build's frames as a render reads it.
                    readings = await compositor.plate_footage(insert, size, under)
                    surface = await asyncio.to_thread(screen_insert.Surface.of_plate, readings)
                    # A static unkeyed insert's bezel is locked on this frame, as a render's page locks it.
                    lock = await asyncio.to_thread(screen_insert.static_lock, insert, under, size)
                shown = picture if picture is not None else Image.fromarray(await anext(source_iter), "RGB")
                shown = screen_insert.zoomed(shown, insert.zoom, times[index] - build.t0)
                # The matte's window runs over the build's own frames: the frame drawn, the two before, the two after.
                layer = await asyncio.to_thread(
                    screen_insert.compose,
                    insert,
                    shown,
                    size,
                    footage_frames[index],
                    footage=under,
                    surface=surface,
                    fill=fill,
                    lock=lock,
                    state=state,
                    footage_ahead=[(index + 1 + k, frame) for k, frame in enumerate(later)],
                    window_frame=index,
                )
                frame = Image.new("RGBA", size, (0, 0, 0, 0))
                if layer is not None:
                    if layer.glow is not None:
                        bloom = layer.glow.resize(size, Image.Resampling.BILINEAR)
                        strength = np.asarray(bloom, dtype=np.float32)
                        strength[..., 3] *= build.glow
                        frame = Image.alpha_composite(frame, Image.fromarray(strength.astype(np.uint8), "RGBA"))
                    sharp = Image.new("RGBA", size, (0, 0, 0, 0))
                    sharp.paste(layer.warped.image, layer.warped.box[:2])
                    frame = Image.alpha_composite(frame, sharp)
                await asyncio.to_thread(frame.save, folder / f"f{index:05d}.png", format="PNG", compress_level=1)
        out = folder / "build.webm"
        await encoder.ffmpeg(
            [
                *["-framerate", str(BUILD_FPS), "-i", "f%05d.png"],
                *["-c:v", "libvpx-vp9", "-pix_fmt", "yuva420p", "-auto-alt-ref", "0", "-b:v", "0", "-crf", "34"],
                *["-deadline", "realtime", "-cpu-used", "8", "-an", out.name],
            ],
            cwd=str(folder),
        )
        meta = {
            "duration": round(count / BUILD_FPS, 4),
            "width": size[0],
            "height": size[1],
            "fps": BUILD_FPS,
            "frames": count,
            "t0": round(build.t0, 4),
        }
        await file_storage.upload_path(str(out), build_object(digest), "video/webm")
    # Its numbers last: a build that says it is drawn has its video.
    await file_storage.upload_to(json.dumps(meta).encode("utf-8"), build_meta_object(digest), "application/json")


async def prepare(builds: list[InsertBuild]) -> set[str]:
    """Draw in the background the builds a preview found missing, once each, by one replica; the digests being drawn."""
    from app.services.video_studio import figures

    drawing: set[str] = set()
    for build in builds:
        digest = build.digest()
        await figures.start_background_draw(
            digest, f"Screen insert build {build.key}", partial(_draw, build, digest)
        )
        drawing.add(digest)
    return drawing


def insert_build_url(digest: str, user: Any) -> str:
    """A confined URL to an insert's build (``GET /video-studio/insert-build/{token}``), played by ranges."""
    from app.core.base_path import with_base_path
    from app.services.video_studio import media

    token, _expires = media.mint_insert_token(digest, user.id)
    return with_base_path(f"/api/v1/video-studio/insert-build/{token}")


async def attach(document: dict, spec: Any, played: Any, user: Any, *, draw: bool) -> set[str]:
    """Each screen insert of a preview's *document* given its build where it is drawn; the scenes whose builds are drawing.

    *played* is the preview's media (``preview._PreviewMedia``): a build drawn
    plays under ``build/<insert key>``, its numbers on the insert's payload
    (``build``). *draw*: a missing build is drawn in the background (a
    manager's draft), its scene said building.
    """
    building: set[str] = set()
    try:
        wanted = builds_of(document, played.ids, spec)
        missing: list[InsertBuild] = []
        payloads = {
            str(payload.get("src")): payload for scene in document.get("scenes") or [] for payload in scene.get("inserts") or []
        }
        for build in wanted:
            digest = build.digest()
            meta = await read_build(digest)
            if meta is None:
                missing.append(build)
                continue
            played.put(f"build/{build.key}", insert_build_url(digest, user), digest)
            payloads[build.key]["build"] = {"fps": meta["fps"], "duration": meta["duration"], "t0": build.t0}
        if missing and draw:
            await prepare(missing)
            building = {build.scene_id for build in missing}
        elif missing:
            from app.services.video_studio import figures

            building = {build.scene_id for build in missing if await figures.being_drawn(build.digest())}
    except Exception as exc:  # the stage maps the first frame, as before builds
        logger.info("Insert builds not shown in a preview: %s", format_exception_message(exc))
    await attach_stand_ins(document, played.ids, spec)
    mark_approximate(document, spec)
    return building


async def attach_stand_ins(document: dict[str, Any], ids: dict[str, str], spec: Any) -> None:
    """Give each screen insert shown without its build what the stage needs to draw it as the render does.

    * A keyed one: its key (``preview_key``: the colour and the matte's tolerance), which the page tests on the
      footage frame shown (``compositor_page.keyMask``, ``screens.key_matte``'s arithmetic): what stands in front of
      the surface stays in front.
    * A tracked one over video footage: the quads the last render of the same scene followed (``preview_track``,
      kept in ``render_cache`` by ``render._keep_preview_track``), which the page draws it on frame by frame. None
      kept (never rendered, or its quad or timing changed since): it stays on its quad and is said approximate.
    """
    from app.services.video_studio import insert_tracking, render_cache, timeline
    from app.services.video_studio.screen_insert import KeySpec

    try:
        parts = timeline.insert_sources(spec)
    except Exception as exc:  # a spec the timeline cannot read: the stage maps the sources, as before
        logger.debug("Insert stand-ins not read: %s", format_exception_message(exc))
        return
    for scene in document.get("scenes") or []:
        footage = timeline.insert_footage(scene)
        moving = footage is not None and not timeline.footage_still(footage)
        footage_file = ids.get(str(footage.get("src") or "")) if footage is not None else None
        for payload in scene.get("inserts") or []:
            overlay = getattr(parts.get(str(payload.get("src") or "")), "overlay", None)
            if payload.get("build") or overlay is None:
                continue
            try:
                key = KeySpec.of(overlay.key) if overlay.key is not None else None
                if key is not None:
                    payload["preview_key"] = {"rgb": list(key.key().rgb), "tolerance": key.matte_tolerance()}
                if overlay.track and moving and footage_file and footage is not None:
                    digest = insert_tracking.preview_track_digest(
                        footage_file, scene.get("id"), payload, footage.get("time_map") or []
                    )
                    track = insert_tracking.preview_track_read(
                        await render_cache.get_json(insert_tracking.PREVIEW_TRACKS, digest)
                    )
                    if track is not None:
                        payload["preview_track"] = track
            except Exception as exc:  # this insert keeps its plain stand-in, said approximate
                logger.info("Insert stand-in of %s not read: %s", payload.get("src"), format_exception_message(exc))


def mark_approximate(document: dict[str, Any], spec: Any) -> None:
    """Say which screen inserts the stage cannot draw as the render does: each tracked one with neither its build nor
    the quads a render followed for it.

    Without a build (still drawing, never drawn for a viewer, over a still, past
    :data:`MAX_BUILD_SECONDS`, failed) the stage maps the source onto the quad
    (``compositor_page.previewInsert``), its key tested on the footage frame
    shown (``preview_key``) and, tracked, drawn on the quads the last render
    followed (``preview_track``, :func:`attach_stand_ins`). A tracked insert
    with no such quads stays on its first quad: it is marked ``approximate`` and
    its scene ``preview_inserts_approximate``, so the editor says the picture is
    approximate instead of passing it as the render's. Nothing else is marked.
    """
    from app.services.video_studio import timeline

    try:
        parts = timeline.insert_sources(spec)
    except Exception as exc:  # a spec the timeline cannot read: nothing is said, as before
        logger.debug("Insert approximation not read: %s", format_exception_message(exc))
        return
    for scene in document.get("scenes") or []:
        for payload in scene.get("inserts") or []:
            key = str(payload.get("src") or "")
            if payload.get("build"):
                continue
            overlay = getattr(parts.get(key), "overlay", None)
            keyed = bool(payload.get("keyed")) or getattr(overlay, "key", None) is not None
            unkeyed = keyed and not payload.get("preview_key")
            untracked = bool(getattr(overlay, "track", False)) and not payload.get("preview_track")
            if unkeyed or untracked:
                payload["approximate"] = True
                scene["preview_inserts_approximate"] = True


async def sweep(*, now: datetime | None = None) -> int:
    """Delete the builds written more than ``video_studio_figure_still_retention_days`` ago; how many.

    Nothing references a build but its digest, which every preview computes
    again: one a preview plays again is drawn again.
    """
    from app.core.config import settings
    from app.services.file_storage import file_storage

    days = settings.video_studio_figure_still_retention_days
    if days <= 0:
        return 0
    cutoff = (now or datetime.now(UTC)) - timedelta(days=days)
    deleted = 0
    for name, modified in await file_storage.list_objects_modified(f"{BUILD_PREFIX}/"):
        if modified is None:
            continue
        if (modified if modified.tzinfo else modified.replace(tzinfo=UTC)) >= cutoff:
            continue
        await file_storage.delete(name)
        deleted += 1
    return deleted
