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

"""The bezel a static screen insert is drawn on, measured before anything is drawn.

A ``screen_insert`` without ``track`` stays on one surface of its scene's
footage: the bezel the footage frame it comes in on shows. Which frame that
is, the plan says as soon as the footage is made
(``timeline.estimate_plan(place_inserts=True)``), so the bezel is measured
when the video is saved, never discovered by a preview:

* **A quad left to the footage** (none given, or ``auto``) takes the bezel
  measured there (``surface_refine.measure_bezel``), written into the spec the
  save stores: ``get_video`` shows it, and the render draws it as given. No
  bezel on that frame fails the save, the frame attached
  (:class:`BezelRefusedError`).
* **A quad given** within :data:`SNAP_MAX_SHARE` of the frame's width of the
  bezel measured near it is snapped to it, said in a note; further off, the
  save is refused, saying how far - unless the quad stands inside the surface
  measured: an insert drawn on part of a screen on purpose.
* **The surface's flatness** travels with the measure: a curved surface - an
  edge bowing past ``surface_refine.FLAT_MAX_SHARE`` on its visible part, its
  points confirming it, or a quad that is not convex - is a finding
  only a shot made again fixes (:func:`findings`, read by
  ``quality.content_findings``). A flat surface seen at an angle is drawn by
  the perspective warp exactly: how far it is turned is said in a note
  (in degrees), never a finding. An edge mostly behind what
  stands in front is said too, its bow not judged (:func:`covered_note`),
  and so is the key's spill on the edge of what stands in front with what the
  insert's edge despill leaves of it (:func:`spill_note`): a preview's
  halo gate is said before any preview. On a curved keyed surface the insert is
  drawn once on that frame, as the render draws it
  (``screen_insert.drawn_on``): on its bezel with no key left past it, it covers
  the bow - a fact said in a note (:func:`bow_note`), never a finding.
* **What stands in front, over the clip.** A keyed insert's footage
  is read once more, from the frame it comes in on to the frame it goes out
  on (``compositor.read_subject``, ``screen_insert.subject_of``): the colours
  of what stands in front of its surface, inside the quad and past it. A
  hue holding ``screen_insert.OWN_HUE_MIN_SHARE`` of its pixels is its own on
  every frame the render draws (:func:`subjects`), never despilled off its
  edge nor counted by the halo gate - a pink muzzle crossing a green panel
  -, and said when it is the key's hue or its complement
  (:func:`subject_note`).
* **One measure.** The frame is read as the render's lock reads it
  (``surface_refine.measure_bezel``): its key, its spread, what stands in
  front left out of the edges. What the save concluded (:func:`verdicts`),
  and what the shot's own lint concluded of the take that plays
  (:func:`shot_verdicts`), is what the render holds its own findings to: a
  render finding a gate either passed on the same shot and spec disagrees
  with it, a warning. The surface's flatness is measured once, here
  (:func:`measured_surfaces`): a preview and a render take this measure,
  on the footage frame at its own resolution, and never read it again on the
  frames they draw - a 4K frame's edges read a bow a 1080p one does not.

* **A tracked insert** is measured by the save too (``measure(...,
  tracks=True)``): its quad snapped to the bezel measured near it on the
  frame it comes in on (within :data:`SNAP_MAX_SHARE`, said in a note) - the
  seed its track starts from, the same read the static gate makes - then
  followed through its footage as a preview follows it
  (``insert_tracking.track_all``, through the raw take of a stabilised shot),
  the answer saying "track: N frames by the tracker, M by the bezel read"
  (:func:`track_note`), or the loss a preview would fail on. Never refused,
  never judged by the static gates: its frames are judged as a preview draws
  them. The tracks of one save are bounded by :data:`TRACK_SECONDS`; one not
  followed in time is left to the preview.

A quad is measured once per footage file, frame, quad given and key
(``render_cache``, :data:`CACHE_KIND`): a save that changes nothing under it
reads it back. Footage not made yet is not measured - the next save measures
it, and the pre-render lint (:func:`settle`) before anything is drawn. A
measurement that cannot be made (a file gone, a decoder failing, past
:data:`MEASURE_SECONDS`) never fails a save: the render's own bezel lock stands.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import tempfile
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from app.core.utils import format_exception_message
from app.services.video_studio.spec import SpecProblem, VideoSpecError

logger = logging.getLogger(__name__)

#: A quad given this close to the bezel measured near it (a share of the frame's width, mean over its corners) is
#: snapped to it; further off, the save is refused.
SNAP_MAX_SHARE = 0.02
#: Closer than this, a quad given is the bezel: nothing is written.
SNAP_NOISE_SHARE = 0.0005
#: The longest the measurements of one save take, every insert together: past it, the rest are left to the render.
MEASURE_SECONDS = 45.0
#: The longest the tracks a save follows take, every tracked insert together: past it, the rest are left to the
#: preview.
TRACK_SECONDS = 30.0
#: Where a measurement is kept (``render_cache``), and the version of what it measures: moved, everything again.
CACHE_KIND = "insert-bezels"
FRAME_KIND = "insert-bezel-frames"
MEDIA_KIND = "insert-bezel-media"
#: 2: each edge's points' agreement with its line, what confirms a bow. 3: how far the surface is turned, in
#: degrees, and whether its quad is convex. 4: read as the render's lock reads it - its key widened to the
#: surface's spread, what stands in front of its edges left out, each edge's visible share. 5: the key's spill
#: on the edge of what stands in front, and what the insert's edge despill leaves of it. 6: what stands in
#: front's colours over the insert's clip, and the complement counted only where the clamp made it. 7: a bowed
#: keyed surface's insert drawn on that frame, its alignment and residual key (:attr:`Measured.drawn`). 8: the
#: size of the frame read (:attr:`Measured.size`). 9: a keyed side hidden behind what stands in front placed at the key
#: colour's extent past it (``surface_refine.hidden_sides``).
MEASURE_VERSION = 9
#: What a drawn frame's audit keeps of the insert drawn on a bowed surface: what says whether it covers it.
DRAWN_FIELDS = ("alignment", "alignment_found", "alignment_sides", "residual_key", "residual_where")
#: Past this share of the edge of what stands in front holding the key's hue on the frame measured, the save says the
#: edge spill a preview's halo gate will read (:func:`spill_note`): the halo gate's own threshold.
SPILL_NOTE_SHARE = 0.05
#: The rate a footage whose own is not read is taken back a quarter frame of (an AI shot plans at its row's numbers).
EARLY_RATE = 30.0


@dataclass(frozen=True)
class Measured:
    """One static screen insert, measured on the footage frame it comes in on."""

    scene_id: str
    #: Its index among its scene's overlays.
    index: int
    #: What plays under it: ``("clip", id)`` (an AI shot), ``("take", id)``, ``("file", id)``.
    footage: tuple[str, str]
    #: The file read, the frame of it (its index where its rate is known) and its second.
    file_id: str
    frame: int | None
    seconds: float
    #: The quad given (0-1), ``None`` when left to the footage.
    given: tuple[tuple[float, float], ...] | None
    #: The bezel measured there (``surface_refine.Bezel``), ``None`` for none.
    bezel: Any
    #: What the measurement and its frame are kept under.
    digest: str
    #: What stands in front of its keyed surface over its clip (``screen_insert.Subject``), and the hue of the
    #: key it was read with, in degrees; ``None`` each: not keyed, a still, or not read.
    subject: Any = None
    key_hue: float | None = None
    #: On a curved keyed surface, the insert drawn on that frame as the render draws it
    #: (``screen_insert.drawn_on``): its audit's :data:`DRAWN_FIELDS` - whether it covers the bow
    #: (``insert_checks.covers_surface``). ``None``: flat, unkeyed, or not drawn.
    drawn: dict | None = None
    #: The size of the frame read, ``(w, h)`` - its footage's own: what a drawn frame's reading of the bezel is
    #: told lower than (``insert_checks.bezel_readings``). ``None``: not known.
    size: tuple[int, int] | None = None
    #: A tracked insert: its bezel is the seed its track starts from, never a static gate's verdict ...
    tracked: bool = False
    #: ... and its track as the save followed it: ``{"tracker": N, "bezel_read": M, "spans": ...}`` (frames each placed,
    #: and the runs of frames each source placed when both did: ``InsertTrack.said_spans``), or
    #: ``{"lost": the loss, "frame", "seconds"}``; ``None``: not followed (a picture under it, or past the budget).
    track: dict | None = None
    #: One of several inserts of its scene left to the footage: the screen it was handed (``"screen 2 of 3"``, read
    #: left to right then top to bottom, :func:`assign_screens`), or why none was (``refusal``); ``None`` for both
    #: when it is its scene's only one.
    screen: str | None = None
    refusal: str | None = None

    @property
    def label(self) -> str:
        return f"its screen insert {self.index}"

    @property
    def shot(self) -> str | None:
        """The AI shot under it, the one a regeneration makes again; ``None`` for a recording or a file."""
        return self.footage[1] if self.footage[0] == "clip" else None

    @property
    def where(self) -> str:
        if self.frame is None:
            return f"the frame of its footage at {self.seconds:.2f} s"
        return f"frame {self.frame} ({self.seconds:.2f} s) of its footage"


def _centre(quad: Any) -> tuple[float, float]:
    return sum(float(x) for x, _y in quad) / 4.0, sum(float(y) for _x, y in quad) / 4.0


def assign_screens(
    autos: list[int], screens: list[Any], *, previous: dict[int, Any] | None = None, taken: list[Any] | None = None
) -> tuple[dict[int, int], str | None]:
    """Which of *screens* (0-1 quads, in reading order) each insert of *autos* (overlay indexes of one scene, left
    to the footage) takes: ``({index: screen}, None)``, or ``({}, why)`` when there are fewer free screens than inserts.

    A screen holding the centre of a quad *taken* (an insert of the scene given its quad) is no one's. An insert
    with a *previous* quad (the one stored before this save: a re-measure) takes the free screen nearest it, every
    such insert together the nearest overall (centre to centre, ``linear_sum_assignment``); the rest take the free
    screens left in reading order, in their overlay order.
    """
    previous = previous or {}
    free = [i for i, quad in enumerate(screens) if not any(_inside([_centre(t)], quad, 0.0) for t in taken or [])]
    if len(free) < len(autos):
        found = "; ".join(f"screen {n + 1} {_quad_text(quad)}" for n, quad in enumerate(screens)) or "none"
        why = (
            f"{len(autos)} screen inserts are left to the footage (quad auto) and it shows {len(free)} free flat "
            f"screen(s) for them (found: {found}) - give the quads of the inserts that share a screen"
        )
        return {}, why
    out: dict[int, int] = {}
    seeded = [index for index in autos if previous.get(index) is not None]
    if seeded:
        import numpy as np
        from scipy.optimize import linear_sum_assignment

        cost = np.array(
            [
                [float(np.hypot(*np.subtract(_centre(previous[index]), _centre(screens[s])))) for s in free]
                for index in seeded
            ]
        )
        rows, cols = linear_sum_assignment(cost)
        out = {seeded[r]: free[c] for r, c in zip(rows, cols, strict=True)}
    left = [s for s in free if s not in out.values()]
    for index in autos:
        if index not in out:
            out[index] = left.pop(0)
    return out, None


def _quad_text(quad: Any) -> str:
    return "[" + ", ".join(f"[{float(x):.4f}, {float(y):.4f}]" for x, y in quad) + "]"


def _inside(quad: Any, surface: Any, tolerance: float = 0.005) -> bool:
    """Whether every corner of *quad* stands inside the convex *surface* (both 0-1), within *tolerance*."""
    corners = [(float(x), float(y)) for x, y in surface]
    for x, y in quad:
        for (ax, ay), (bx, by) in zip(corners, corners[1:] + corners[:1], strict=True):
            length = max(((bx - ax) ** 2 + (by - ay) ** 2) ** 0.5, 1e-9)
            # Clockwise on screen (y down): inside is where every edge turns the same way.
            if ((bx - ax) * (float(y) - ay) - (by - ay) * (float(x) - ax)) / length < -tolerance:
                return False
    return True


async def _footage_of(db: Any, scene: Any) -> tuple[tuple[str, str], str, bool] | None:
    """``(what plays, its file, whether it is a picture)`` under *scene*'s inserts; ``None`` while it is not made."""
    from app.models.file_attachment import FileAttachment
    from app.models.video_studio import JOB_READY, VideoClip, VideoTake
    from app.services.video_studio import clips

    async def shot(clip_id: str) -> tuple[tuple[str, str], str, bool] | None:
        row = await db.get(VideoClip, uuid.UUID(clip_id))
        if row is None or not clips.playable(row):
            return None
        return ("clip", clip_id), str(row.file_id), False

    async def upload(file_id: str) -> tuple[tuple[str, str], str, bool] | None:
        row = await db.get(FileAttachment, uuid.UUID(file_id))
        if row is None:
            return None
        return ("file", file_id), file_id, not (row.content_type or "").startswith("video/")

    if scene.type == "clip":
        return await shot(scene.clip_id) if scene.clip_id else (await upload(scene.file_id) if scene.file_id else None)
    if scene.type == "recording":
        take = await db.get(VideoTake, uuid.UUID(scene.take_id))
        if take is None or take.status != JOB_READY or not take.file_id or getattr(take, "expired_at", None) is not None:
            return None
        return ("take", scene.take_id), str(take.file_id), False
    if scene.type == "statement" and scene.backdrop is not None:
        if scene.backdrop.clip_id:
            return await shot(scene.backdrop.clip_id)
        return await upload(scene.backdrop.file_id) if scene.backdrop.file_id else None
    if scene.type == "image":
        # Its animation once made plays in its place, else its still: a picture, as a statement's picture backdrop.
        if scene.animate is not None and scene.animate.clip_id and (animated := await shot(scene.animate.clip_id)) is not None:
            return animated
        return await upload(scene.file_id) if scene.file_id else None
    return None


def _measured(overlay: Any, tracks: bool) -> bool:
    """Whether a save measures *overlay*: a static screen insert, and with *tracks* a tracked one."""
    return overlay.type == "screen_insert" and (tracks or not overlay.track)


def _digest(
    file_id: str,
    seconds: float,
    given: Any,
    key: Any,
    despill: dict | None = None,
    until: float | None = None,
    *,
    track: tuple[float, float] | None = None,
) -> str:
    """What a measure is kept under; *track* (a tracked insert's): the seconds of its footage it is followed
    over, with the tracker's version."""
    from app.services.video_studio.insert_tracking import TRACK_VERSION

    shown = {
        "version": MEASURE_VERSION,
        "file": file_id,
        "at": round(float(seconds), 3),
        **({"until": round(float(until), 3)} if until is not None else {}),
        "given": None if given is None else [[round(float(x), 5), round(float(y), 5)] for x, y in given],
        "key": None if key is None else [str(key.color), round(float(key.tolerance), 4)],
        **({"despill": despill} if despill else {}),
        **({"track": [TRACK_VERSION, round(track[0], 3), round(track[1], 3)]} if track is not None else {}),
    }
    return hashlib.sha256(json.dumps(shown, sort_keys=True).encode("utf-8")).hexdigest()


def stored_quads(spec: dict | None) -> dict[tuple[str, int], Any]:
    """``(scene id, overlay index) -> quad`` of every screen insert of a stored *spec* whose quad is written: what a
    scene's several inserts left to the footage are handed their screens by again (:func:`assign_screens`)."""
    out: dict[tuple[str, int], Any] = {}
    for scene_id, scene in _scenes_by_id(spec).items():
        for index, overlay in enumerate(scene.get("overlays") or []):
            quad = overlay.get("quad") if isinstance(overlay, dict) and overlay.get("type") == "screen_insert" else None
            if isinstance(quad, list) and len(quad) == 4:
                out[(scene_id, index)] = tuple((float(x), float(y)) for x, y in quad)
    return out


async def measure(
    db: Any,
    spec: Any,
    *,
    user: Any = None,
    voices: dict[str, float] | None = None,
    tracks: bool = False,
    previous: dict[tuple[str, int], Any] | None = None,
    swap: dict[str, str] | None = None,
) -> list[Measured]:
    """Every static screen insert of the parsed effective *spec* whose footage is made, measured (:class:`Measured`);
    *swap* (``clip id -> file id``) reads an AI shot's new take in place of the one that plays (:func:`promotion_findings`);
    with *tracks* (a save), every tracked one too - its seed measured and its track followed
    (:attr:`Measured.track`), read by :func:`resolve` alone. Several
    inserts of a scene left to the footage are each handed a screen of
    that frame (:func:`assign_screens`), the one nearest its *previous* quad
    (:func:`stored_quads` of the spec stored) first.

    Planned as the render plans it - the recordings' and shots' real lengths,
    each narration at its spoken length (*voices*, else read for *user*,
    ``preview.spoken_lengths``) - so the frame read is the one the insert
    comes in on. Never raises; bounded by :data:`MEASURE_SECONDS` (and
    :data:`TRACK_SECONDS` more for the tracks).
    """
    if not any(_measured(overlay, tracks) for scene in spec.scenes for overlay in scene.overlays):
        return []
    try:
        if voices is None and user is not None and spec.voiceover.enabled:
            from app.services.video_studio.preview import spoken_lengths

            voices = await spoken_lengths(db, spec, user)
        bound = MEASURE_SECONDS + (TRACK_SECONDS if tracks else 0.0)
        return await asyncio.wait_for(
            _measure(db, spec, voices, tracks=tracks, previous=previous, swap=swap), timeout=bound
        )
    except TimeoutError:
        logger.info("Screen insert bezels not measured within %.0f s: left to the render", MEASURE_SECONDS)
    except Exception as exc:
        logger.info("Screen insert bezels not measured: %s", format_exception_message(exc))
    return []


async def _measure(
    db: Any,
    spec: Any,
    voices: dict[str, float] | None,
    *,
    tracks: bool = False,
    previous: dict | None = None,
    swap: dict[str, str] | None = None,
) -> list[Measured]:
    import dataclasses

    import numpy as np
    from PIL import Image

    from app.models.video_studio import VideoTake
    from app.services.video_studio import assets, encoder, render_cache, screen_insert, surface_refine, timeline
    from app.services.video_studio.preview import planned_clip
    from app.services.video_studio.projects import take_media

    found: dict[str, tuple[tuple[str, str], str, bool]] = {}
    for scene in spec.scenes:
        if any(_measured(overlay, tracks) for overlay in scene.overlays) and (footage := await _footage_of(db, scene)) is not None:
            (kind, ref), _file, picture = footage
            if kind == "clip" and swap and ref in swap:
                footage = ((kind, ref), swap[ref], picture)
            found[scene.id or ""] = footage
    if not found:
        return []
    #: When the tracks of this save are given up (:data:`TRACK_SECONDS`), set by the first one followed.
    budget: list[float] = []
    out: list[Measured] = []
    with tempfile.TemporaryDirectory(prefix="xtm-insert-bezel-") as work:
        workdir = Path(work)
        paths: dict[str, str | None] = {}

        async def local(file_id: str) -> str | None:
            if file_id not in paths:
                base = workdir / f"footage-{len(paths)}"
                row = await assets.download_file(file_id, str(base))
                paths[file_id] = str(base) if row is not None else None
            return paths[file_id]

        async def probed(file_id: str) -> Any:
            """An upload's numbers, probed once and kept by file: a save reading its measures back reads nothing."""
            kept = await render_cache.get_json(MEDIA_KIND, file_id)
            if isinstance(kept, dict) and kept.get("duration"):
                return timeline.ClipMedia(**kept)
            path = await local(file_id)
            if path is None:
                return None
            info = await encoder.probe(path)
            media = timeline.ClipMedia(info.duration, info.width, info.height, info.fps or 24.0, info.frame_count, info.has_audio)
            await render_cache.put_json(MEDIA_KIND, file_id, dataclasses.asdict(media))
            return media

        # The footage's numbers as the render plans with them - a recording's, a shot's from its row (the preview's
        # reading), an upload's probed - so the frame read is the one the insert comes in on.
        clips_media: dict[str, Any] = {}
        backdrops: dict[str, Any] = {}
        takes: dict[str, Any] = {}
        #: ``scene id -> its footage's frame rate`` where it is known (``None``: a picture, or a shot's unread rate).
        rates: dict[str, float | None] = {}
        for scene in spec.scenes:
            sid = scene.id or ""
            if sid not in found:
                continue
            (kind, ref), file_id, picture = found[sid]
            if kind == "take":
                takes[ref] = take_media(await db.get(VideoTake, uuid.UUID(ref)))
                rates[sid] = float(takes[ref].fps or 60)
                continue
            if picture:
                if scene.type == "statement":
                    backdrops[sid] = timeline.ImageMedia(1920, 1080)
                rates[sid] = None
                continue
            media = await planned_clip(db, ref) if kind == "clip" else await probed(file_id)
            if media is None:
                continue
            (backdrops if scene.type == "statement" else clips_media)[sid] = media
            rates[sid] = float(media.fps) if kind == "file" else None
        plan = timeline.estimate_plan(spec, takes, clips_media, voices, place_inserts=True, backdrops=backdrops)
        planned = {scene.get("id"): scene for scene in plan.document.get("scenes") or []}

        async def frame_pixels(path: str, picture: bool, seconds: float, fps: float | None, name: str) -> Any:
            """The footage frame at *seconds* (a picture's own), written to *name* in the work directory, as pixels."""
            if picture:
                with Image.open(path) as image:
                    image.convert("RGB").save(workdir / name)
            else:
                # A quarter frame early: the decoder's first frame at or after it is the one the insert comes in on.
                early = 0.25 / (fps or EARLY_RATE)
                await encoder.extract_frame(path, at=max(0.0, seconds - early), out_path=str(workdir / name))
            with Image.open(workdir / name) as image:
                return np.asarray(image.convert("RGB"))

        async def hand_out(scene: Any, file_id: str, picture: bool, seconds: float, fps: float | None) -> dict | None:
            """The screens of the footage frame at *seconds* (where the first of them comes in) handed out to a
            scene's several inserts left to the footage (:func:`assign_screens`): ``{"screens": [quad 0-1, ...],
            "keys", "hues", "assigned": {index: screen}, "refusal"}``, kept as its measures are; ``None`` when the
            footage cannot be read."""
            sid = scene.id or ""
            inserts = [(i, o) for i, o in enumerate(scene.overlays) if o.type == "screen_insert"]
            autos = [i for i, o in inserts if o.auto_quad]
            taken = [tuple((float(x), float(y)) for x, y in o.quad) for _i, o in inserts if isinstance(o.quad, list)]
            before = {i: (previous or {}).get((sid, i)) for i in autos}
            # One key for all of them reads their screens by it; inserts keyed apart read every key colour.
            specs = [screen_insert.KeySpec.of(scene.overlays[i].key) for i in autos]
            colours = {str(spec_.color) if spec_ is not None else None for spec_ in specs}
            key = specs[0] if len(colours) == 1 else None
            shown = {
                "version": MEASURE_VERSION,
                "hand_out": 1,
                "file": file_id,
                "at": round(float(seconds), 3),
                "autos": autos,
                "taken": [[list(p) for p in q] for q in taken],
                "before": {str(i): [list(p) for p in q] if q else None for i, q in before.items()},
                "key": None if key is None else [str(key.color), round(float(key.tolerance), 4)],
            }
            digest = hashlib.sha256(json.dumps(shown, sort_keys=True).encode("utf-8")).hexdigest()
            kept = await render_cache.get_json(CACHE_KIND, digest)
            if not (isinstance(kept, dict) and "screens" in kept):
                path = await local(file_id)
                if path is None:
                    return None
                pixels = await frame_pixels(path, picture, seconds, fps, f"screens-{sid}.png")
                height, width = pixels.shape[:2]
                found = await asyncio.to_thread(surface_refine.screens_on, pixels, key.key() if key else None)
                screens = [
                    [[round(float(x) / width, 5), round(float(y) / height, 5)] for x, y in item.corners]
                    for item in found
                ]
                assigned, refusal = assign_screens(
                    autos, screens, previous={i: q for i, q in before.items() if q}, taken=taken
                )
                kept = {
                    "screens": screens,
                    "keys": [item.key for item in found],
                    "hues": [float(item.hue) for item in found],
                    "assigned": {str(i): s for i, s in assigned.items()},
                    "refusal": refusal,
                }
                await render_cache.put_json(CACHE_KIND, digest, kept)
            return {**kept, "assigned": {int(i): int(s) for i, s in (kept.get("assigned") or {}).items()}}

        #: ``scene id -> its hand-out`` (:func:`hand_out`), for a scene of several inserts left to the footage.
        handed: dict[str, dict | None] = {}
        for scene in spec.scenes:
            sid = scene.id or ""
            if sid not in found or sid not in rates:
                continue
            (kind, ref), file_id, picture = found[sid]
            payload = planned.get(sid) or {}
            under = timeline.insert_footage(payload)
            fps = rates[sid]
            for index, overlay in enumerate(scene.overlays):
                if not _measured(overlay, tracks):
                    continue
                tracked = bool(overlay.track)
                base = timeline.insert_key(sid, index)
                placed = [p for p in payload.get("inserts") or [] if p.get("src") == base or str(p.get("src")).startswith(f"{base}.")]
                if not placed or not isinstance(under, dict):
                    continue
                until = followed_over = None
                if picture:
                    frame, seconds = 0, 0.0
                else:
                    time_map = [tuple(point) for point in under.get("time_map") or []]
                    seconds = timeline.map_forward(time_map, float(placed[0]["t0"])) if time_map else 0.0
                    frame = max(0, round(seconds * fps)) if fps else None
                    ends = float(placed[0].get("t1") or placed[0]["t0"])
                    until = timeline.map_forward(time_map, ends) if time_map else ends
                    if tracked:
                        # The footage's seconds its track is followed over, as a preview follows it.
                        drawn_from, drawn_to = timeline.insert_drawn(placed[0])
                        followed_over = (timeline.map_forward(time_map, drawn_from), timeline.map_forward(time_map, max(ends, drawn_to)))
                given = None if overlay.auto_quad else tuple((float(x), float(y)) for x, y in overlay.quad)
                key = screen_insert.KeySpec.of(overlay.key)
                despill = timeline.insert_despill(overlay)
                # One of several inserts of the scene left to the footage: the screen of that frame it is handed.
                screen = handed_screen = None
                if given is None and sum(o.type == "screen_insert" and o.auto_quad for o in scene.overlays) > 1:
                    if sid not in handed:
                        handed[sid] = await hand_out(scene, file_id, picture, seconds, fps)
                    out_of = handed[sid]
                    if out_of is None:
                        continue
                    if out_of.get("refusal") or index not in out_of["assigned"]:
                        out.append(
                            Measured(
                                sid,
                                index,
                                (kind, ref),
                                file_id,
                                frame,
                                round(seconds, 3),
                                None,
                                None,
                                "",
                                refusal=out_of.get("refusal"),
                            )
                        )
                        continue
                    number = out_of["assigned"][index]
                    handed_screen = out_of["screens"][number]
                    screen = f"screen {number + 1} of {len(out_of['screens'])}"
                digest = _digest(file_id, seconds, given, key, despill, until if key is not None else None, track=followed_over)
                if handed_screen is not None:
                    digest = hashlib.sha256(f"{digest}:{json.dumps(handed_screen)}".encode()).hexdigest()
                kept = await render_cache.get_json(CACHE_KIND, digest)
                subject = key_hue = drawn = size = track = None
                label = f"Scene {sid}: its screen insert {index}"
                if isinstance(kept, dict) and "bezel" in kept:
                    bezel = surface_refine.Bezel.from_dict(kept["bezel"]) if kept["bezel"] else None
                    subject = screen_insert.Subject.from_dict(kept.get("subject"))
                    key_hue = kept.get("key_hue")
                    drawn = kept.get("drawn") if isinstance(kept.get("drawn"), dict) else None
                    read = kept.get("size")
                    size = (int(read[0]), int(read[1])) if isinstance(read, list) and len(read) == 2 else None
                    track = kept.get("track") if isinstance(kept.get("track"), dict) else None
                    record = dict(kept)
                else:
                    path = await local(file_id)
                    if path is None:
                        continue
                    name = f"frame-{sid}-{index}.png"
                    if picture:
                        with Image.open(path) as image:
                            image.convert("RGB").save(workdir / name)
                    else:
                        # A quarter frame early: the decoder's first frame at or after it is the one the insert comes in on.
                        early = 0.25 / (fps or EARLY_RATE)
                        await encoder.extract_frame(path, at=max(0.0, seconds - early), out_path=str(workdir / name))
                    with Image.open(workdir / name) as image:
                        pixels = np.asarray(image.convert("RGB"))
                    size = (int(pixels.shape[1]), int(pixels.shape[0]))
                    surface = None
                    if handed_screen is not None:
                        from app.services.video_studio.screens import FoundScreen

                        corners = tuple((float(x) * size[0], float(y) * size[1]) for x, y in handed_screen)
                        number = out_of["assigned"][index]
                        surface = FoundScreen(corners, out_of["keys"][number], float(out_of["hues"][number]))
                    bezel = await asyncio.to_thread(
                        surface_refine.measure_bezel,
                        pixels,
                        key.key() if key else None,
                        key.matte_tolerance() if key else None,
                        given=given,
                        feather=key.feather if key else 1.5,
                        despill=screen_insert.Despill.of(despill),
                        surface=surface,
                    )
                    quad = bezel.quad if bezel is not None else given
                    # What stands in front over its clip, and a bowed surface drawn on: the static gates' readings.
                    if not tracked and key is not None and not picture and until is not None and quad is not None:
                        subject, key_hue = await _subject(path, pixels, quad, key, seconds, until, fps, label=label)
                    if not tracked and key is not None and bezel is not None and not bezel.flatness.flat and bezel.flatness.convex:
                        drawn = await _drawn(pixels, drawn_quad(given, bezel), key, despill, label=label)
                    record = {
                        "bezel": bezel.as_dict() if bezel else None,
                        "subject": subject.as_dict() if subject else None,
                        "key_hue": key_hue,
                        "drawn": drawn,
                        "size": list(size),
                    }
                    await render_cache.put_json(CACHE_KIND, digest, record)
                    await render_cache.put_file(FRAME_KIND, digest, str(workdir / name), suffix=".png")
                if followed_over is not None and track is None and (path := await local(file_id)) is not None:
                    seed = seed_quad(given, bezel)
                    # A new take read in place of the one that plays: the raw take its row names is the old one's.
                    row_db = None if swap and ref in swap else db
                    track = await _track_of(
                        row_db, (kind, ref), path, local, placed[0], under, seed, label, sid, key, budget
                    )
                    if track is not None:
                        await render_cache.put_json(CACHE_KIND, digest, {**record, "track": track})
                out.append(
                    Measured(
                        sid,
                        index,
                        (kind, ref),
                        file_id,
                        frame,
                        round(seconds, 3),
                        given,
                        bezel,
                        digest,
                        subject,
                        key_hue,
                        drawn,
                        size,
                        tracked=tracked,
                        track=track,
                        screen=screen,
                    )
                )
    return out


def seed_quad(given: Any, bezel: Any) -> Any:
    """The quad a tracked insert's track starts from, the one the save writes: the bezel measured near the quad
    *given* on the frame it comes in on (past :data:`SNAP_NOISE_SHARE`, within :data:`SNAP_MAX_SHARE`) - the read the
    static gate makes -, else the quad given."""
    if bezel is None or bezel.found_by != "seed" or bezel.error is None:
        return given
    return bezel.quad if SNAP_NOISE_SHARE < bezel.error <= SNAP_MAX_SHARE else given


async def _track_of(
    db: Any,
    footage: tuple[str, str],
    path: str,
    local: Any,
    payload: dict,
    under: dict,
    seed: Any,
    label: str,
    scene_id: str,
    key: Any,
    budget: list[float],
) -> dict | None:
    """A tracked insert followed through its footage at *path* from *seed* (0-1), as a preview follows it
    (``insert_tracking.asked_of``; the raw take of a stabilised shot, ``insert_tracking.track_stabilised``):
    ``{"tracker": N, "bezel_read": M}``, ``{"lost": ...}`` on a surface lost, ``None`` past :data:`TRACK_SECONDS` (all
    of a save's tracks together, *budget* the deadline the first one set) or on footage that cannot be read."""
    from app.models.video_studio import VideoClip
    from app.services.video_studio import encoder, insert_tracking, stabilise

    loop = asyncio.get_running_loop()
    if not budget:
        budget.append(loop.time() + TRACK_SECONDS)
    left = budget[0] - loop.time()
    if left <= 0:
        logger.info("%s: its track is left to the preview (past %.0f s of tracks at save)", label, TRACK_SECONDS)
        return None
    try:
        fps = float((await encoder.probe(path)).fps or EARLY_RATE)
        asked = insert_tracking.asked_of(payload, under, fps, [list(p) for p in seed], label, scene_id=scene_id, key=key)
        raw = None
        if footage[0] == "clip" and db is not None:
            row = await db.get(VideoClip, uuid.UUID(footage[1]))
            raw = stabilise.raw_take(row.params) if row is not None else None
        raw_path = await local(raw[0]) if raw is not None else None
        if raw is not None and raw_path is not None:
            follow = insert_tracking.track_stabilised(raw_path, raw[1], [asked], fps=fps, kept_share=raw[2])
        else:
            follow = insert_tracking.track_all(path, [asked])
        [followed] = await asyncio.wait_for(follow, timeout=left)
    except insert_tracking.InsertTrackLostError as exc:
        return {"lost": str(exc), "frame": exc.frame, "seconds": round(exc.seconds, 3)}
    except TimeoutError:
        logger.info("%s: its track is left to the preview (not followed within %.0f s at save)", label, TRACK_SECONDS)
        return None
    except (OSError, RuntimeError, ValueError) as exc:  # a decoder failing: the preview follows it
        logger.info("%s: its track not followed at save: %s", label, format_exception_message(exc))
        return None
    return {
        "tracker": len(followed.quads) - len(followed.bezel_read),
        "bezel_read": len(followed.bezel_read),
        "spans": followed.said_spans() if followed.bezel_read else None,
    }


def drawn_quad(given: Any, bezel: Any) -> Any:
    """The quad a static insert is drawn on once saved: the bezel *bezel* measured when the save writes it (*given*
    ``None``) or snaps the quad *given* to it (within :data:`SNAP_MAX_SHARE`), else the quad given."""
    if given is None or bezel.error is None or bezel.error <= SNAP_MAX_SHARE:
        return bezel.quad
    return given


async def _drawn(pixels: Any, quad: Any, key: Any, despill: dict | None, *, label: str) -> dict | None:
    """The insert keyed on *key* drawn on *quad* of the frame *pixels* (``screen_insert.drawn_on``): its audit's
    :data:`DRAWN_FIELDS`; ``None`` when it cannot be drawn - the render's own frames are read then."""
    from app.services.video_studio import screen_insert

    try:
        audit = await asyncio.to_thread(screen_insert.drawn_on, pixels, quad, key, screen_insert.Despill.of(despill))
    except Exception as exc:  # a frame the compositor cannot draw at save: its bow stays what the measure says
        logger.info("%s: its insert not drawn on its bowed surface at save: %s", label, format_exception_message(exc))
        return None
    if audit is None:
        return None
    read = audit.as_dict()
    return {name: read.get(name) for name in DRAWN_FIELDS}


async def _subject(
    path: str, first: Any, quad: Any, key: Any, seconds: float, until: float, fps: float | None, *, label: str
) -> tuple[Any, float | None]:
    """What stands in front of a static keyed insert's surface over its clip (``compositor.read_subject``): its
    footage at *path* from *seconds* to *until* on *quad* (0-1), keyed on *key* (``screen_insert.KeySpec``) as *first* -
    the frame it comes in on - shows it. ``(subject, the key's hue in degrees)``; ``(None, None)`` when it cannot be
    read: the render reads it then."""
    import colorsys

    from app.services.video_studio import compositor, encoder, screen_insert

    try:
        rate = float(fps or (await encoder.probe(path)).fps or EARLY_RATE)
        height, width = first.shape[:2]
        keyed = key.sampled(first, [(x * width, y * height) for x, y in quad])
        probe = screen_insert.InsertSource(
            path,
            "image",
            tuple(quad),
            key=keyed,
            footage=screen_insert.Footage(path, "video", rate),
            first_frame=max(0, round(seconds * rate)),
            label=label,
        )
        subject = await compositor.read_subject(probe, max(0, round(until * rate)))
    except (OSError, RuntimeError, ValueError) as exc:  # a decoder failing, a footage too short: the render reads it
        logger.info("%s: what stands in front not read over its clip at save: %s", label, format_exception_message(exc))
        return None, None
    hue = colorsys.rgb_to_hsv(*(channel / 255.0 for channel in keyed.key().rgb))[0] * 360.0
    return subject, round(hue, 1)


def subjects(measured: list[Measured]) -> dict[str, Any]:
    """What stands in front of each static keyed insert over its clip, as the save read it: ``{insert key:
    screen_insert.Subject}`` - the overlay's key, every pane of a wall taking it (``render._subject_of``)."""
    from app.services.video_studio import timeline

    return {timeline.insert_key(item.scene_id, item.index): item.subject for item in measured if item.subject is not None}


async def frame_png(measured: Measured) -> bytes | None:
    """The footage frame *measured* was read on, as a PNG (the evidence of a refusal); ``None`` when not kept."""
    from app.services.video_studio import render_cache

    if not measured.digest:
        return None
    with tempfile.TemporaryDirectory(prefix="xtm-insert-bezel-") as work:
        path = Path(work) / "frame.png"
        if not await render_cache.get_file(FRAME_KIND, measured.digest, str(path), suffix=".png"):
            return None
        return path.read_bytes()


class BezelRefusedError(VideoSpecError):
    """A save refused by what its screen inserts' footage shows: :attr:`measured` names the frames to attach."""

    def __init__(self, problems: list[SpecProblem], measured: list[Measured]) -> None:
        super().__init__(problems)
        self.measured = measured


@dataclass
class Resolution:
    """What the measurements make of an authored spec (:func:`resolve`)."""

    authored: dict
    #: ``(scene id, index) -> the quad written`` (0-1), and how far the one it replaced was (``None``: none given).
    written: dict[tuple[str, int], tuple[tuple, float | None]] = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)
    problems: list[SpecProblem] = field(default_factory=list)
    refused: list[Measured] = field(default_factory=list)

    @property
    def changed(self) -> bool:
        return bool(self.written)


def _scenes_by_id(spec: dict | None) -> dict[str, dict]:
    """The scenes of *spec* keyed by their id (those without one left out)."""
    return {
        scene["id"]: scene
        for scene in (spec or {}).get("scenes") or []
        if isinstance(scene, dict) and scene.get("id")
    }


def _kept(scene: dict, stored: dict[str, dict], scene_id: str, index: int) -> bool:
    """Whether the screen insert at *index* of *scene* is one the save stored already and the edit left alone: its
    scene identical to the stored scene of the same id, its quad given. Its quad was snapped when it was stored, so it
    is kept as stored - a bezel read again from that quad lands a hair off, and writing it would change the scene's
    drawing and void its preview on an edit made elsewhere."""
    held = stored.get(scene_id)
    if held is None or held != scene:
        return False
    overlays = held.get("overlays") or []
    return index < len(overlays) and isinstance(overlays[index], dict) and overlays[index].get("quad") is not None


def resolve(authored: dict, measured: list[Measured], held: dict | None = None) -> Resolution:
    """*authored* (a copy) with each quad left to the footage written and each quad given near its bezel snapped.

    A quad left to the footage with no bezel on its frame, and a quad given
    further than :data:`SNAP_MAX_SHARE` from its bezel that does not stand
    inside it, are problems the save refuses on, their frames named. A
    tracked insert's quad is snapped to its seed (:func:`seed_quad`)
    and its track said (:func:`track_note`), never refused. With *held*, the
    spec stored before the save, an insert whose scene the edit left
    identical keeps its stored quad (:func:`_kept`): the snap is idempotent.
    """
    import copy

    out = Resolution(copy.deepcopy(authored))
    stored = _scenes_by_id(held)
    positions = {
        scene.get("id"): index
        for index, scene in enumerate(out.authored.get("scenes") or [])
        if isinstance(scene, dict) and scene.get("id")
    }
    for item in measured:
        at = positions.get(item.scene_id)
        overlays = (out.authored["scenes"][at].get("overlays") or []) if at is not None else []
        if at is None or item.index >= len(overlays) or overlays[item.index].get("type") != "screen_insert":
            continue
        overlay = overlays[item.index]
        path = f"scenes.{at}.overlays.{item.index}.quad"
        bezel = item.bezel
        kept = _kept(authored["scenes"][at], stored, item.scene_id, item.index)
        if item.tracked:
            seed = seed_quad(item.given, bezel)
            if seed != item.given and not kept:
                overlay["quad"] = [[x, y] for x, y in seed]
                out.written[(item.scene_id, item.index)] = (seed, bezel.error)
                out.notes.append(
                    f"Scene {item.scene_id}: {item.label}'s quad was {bezel.error * 100:.1f}% of the frame's width off the "
                    f"bezel on {item.where}: snapped to it, where its track starts."
                )
            if (said := track_note(item)) is not None:
                out.notes.append(said)
            continue
        if (angled := angle_note(item)) is not None:
            out.notes.append(angled)
        if (bowed := bow_note(item)) is not None:
            out.notes.append(bowed)
        if (covered := covered_note(item)) is not None:
            out.notes.append(covered)
        if (hidden := hidden_note(item)) is not None:
            out.notes.append(hidden)
        if (spilled := spill_note(item)) is not None:
            out.notes.append(spilled)
        if (owned := subject_note(item)) is not None:
            out.notes.append(owned)
        if item.refusal is not None:
            # Several inserts left to the footage and fewer free screens than they are: said once per scene.
            if not any(problem.message.endswith(item.refusal) for problem in out.problems):
                out.problems.append(SpecProblem(path, f"scene {item.scene_id}: {item.refusal}"))
                out.refused.append(item)
            continue
        if item.given is None:
            if bezel is None:
                out.problems.append(
                    SpecProblem(
                        path,
                        f"scene {item.scene_id}: no bezel on {item.where}, where {item.label} comes in (no flat green, "
                        "blue or magenta screen nor surface edges there; the frame is attached) - generate the shot "
                        "again with its screen one flat key colour, or give the quad of the surface it covers",
                    )
                )
                out.refused.append(item)
                continue
            overlay["quad"] = [[x, y] for x, y in bezel.quad]
            out.written[(item.scene_id, item.index)] = (bezel.quad, None)
            handed = f" ({item.screen}, read left to right then top to bottom)" if item.screen else ""
            out.notes.append(
                f"Scene {item.scene_id}: {item.label} takes the bezel measured on {item.where}{handed}: "
                f"{_quad_text(bezel.quad)}."
            )
            continue
        if kept or bezel is None or bezel.error is None:
            continue
        if bezel.error <= SNAP_NOISE_SHARE and not bezel.hidden:
            continue
        if bezel.error <= SNAP_MAX_SHARE:
            overlay["quad"] = [[x, y] for x, y in bezel.quad]
            out.written[(item.scene_id, item.index)] = (bezel.quad, bezel.error)
            out.notes.append(
                f"Scene {item.scene_id}: {item.label}'s quad was {bezel.error * 100:.1f}% of the frame's width off the "
                f"bezel on {item.where}: snapped to it."
            )
            continue
        if bezel.found_by == "frame" and _inside(item.given, bezel.quad):
            # Drawn on part of the surface on purpose: the quad given stands.
            continue
        out.problems.append(
            SpecProblem(
                path,
                f"scene {item.scene_id}: {item.label}'s quad is {bezel.error * 100:.1f}% of the frame's width off the "
                f"bezel {item.where} shows - leave its quad out (or quad: auto) so the save measures that bezel and "
                "stores it",
            )
        )
        out.refused.append(item)
    return out


def track_note(item: Measured) -> str | None:
    """What the save's follow of a tracked insert *item* found: ``track: N frames by the tracker, M by the bezel
    read``, or the surface lost where a preview fails; ``None`` when it was not followed."""
    from app.services.video_studio.insert_tracking import placed_by

    track = item.track
    if not item.tracked or not isinstance(track, dict):
        return None
    if track.get("lost"):
        return f"{track['lost']} (followed at save: a preview of scene {item.scene_id} fails there)"
    spans = f" ({track['spans']})" if track.get("spans") else ""
    return f"Scene {item.scene_id}: {item.label}, track: {placed_by(int(track['tracker']), int(track['bezel_read']))}{spans}."


def angle_note(item: Measured) -> str | None:
    """A flat surface *item* measured seen at an angle - how far it is turned, ``surface_refine.Flatness.seen_at``
    -, in words: by design, the perspective warp maps the insert onto any convex planar quad exactly. ``None``
    otherwise."""
    bezel = item.bezel
    if bezel is None or not bezel.flatness.flat or (seen := bezel.flatness.seen_at()) is None:
        return None
    return (
        f"Scene {item.scene_id}: the surface under {item.label} is {seen} on {item.where}: by design, the "
        "perspective warp draws the insert onto it exactly."
    )


def covered_note(item: Measured) -> str | None:
    """The edges of the surface *item* measured that stand mostly behind what is in front of it
    (``surface_refine.Flatness.covered_words``), in words: their bow is not judged on that frame. ``None`` otherwise."""
    bezel = item.bezel
    if bezel is None or (covered := bezel.flatness.covered_words()) is None:
        return None
    return f"Scene {item.scene_id}: the surface under {item.label} has its {covered} on {item.where}: its bow is not judged there."


def hidden_note(item: Measured) -> str | None:
    """The sides of the keyed surface *item* measured that stand hidden behind what is in front of it, placed at the key
    colour's extent past them rather than at the occluder (``surface_refine.Bezel.hidden_words``): ``right side hidden
    behind the foreground: placed at the key colour's extent, x 0.985``. ``None`` otherwise."""
    words = item.bezel.hidden_words() if item.bezel is not None else None
    if words is None:
        return None
    return f"Scene {item.scene_id}: {item.label}'s {words} on {item.where} - the screen continues there."


def spill_note(item: Measured) -> str | None:
    """The key's spill on the edge of what stands in front of the surface *item* measured
    (``screen_insert.EdgeSpill``), in words, when past :data:`SPILL_NOTE_SHARE` of that edge: what a preview's halo gate
    will read there, and what the insert's edge despill leaves of it - said before any preview. ``None`` otherwise."""
    from app.services.video_studio.screen_insert import MAX_HALO_SHARE

    spill = getattr(item.bezel, "spill", None)
    if spill is None or spill.share <= SPILL_NOTE_SHARE:
        return None
    head = (
        f"Scene {item.scene_id}: what stands in front of the surface under {item.label} carries the key's hue on "
        f"{spill.share:.0%} of its edge ({spill.edge_px} px) on {item.where}: expected edge spill"
    )
    if spill.after is None:
        return f"{head}, which the edge despill is off for (despill.edge: false): a preview's halo gate reads it as it is."
    if spill.after > MAX_HALO_SHARE:
        return (
            f"{head}; the edge despill (width {spill.width_px:g} px) leaves {spill.after:.1%} of it, past the halo gate's "
            f"{MAX_HALO_SHARE:.0%}: raise despill.width_px before a preview, or generate the shot again with what stands "
            "in front lit apart from the surface."
        )
    return f"{head}; the edge despill (width {spill.width_px:g} px) leaves {spill.after:.1%} of it."


def subject_note(item: Measured) -> str | None:
    """What stands in front of the surface *item* measured holding the key's hue or its complement over its clip
    (``screen_insert.Subject``), in words, on ``screen_insert.OWN_HUE_MIN_SHARE`` of its pixels at least: its own colour,
    which the edge despill keeps and the halo gate never counts. ``None`` otherwise."""
    from app.services.video_studio.screen_insert import OWN_HUE_MIN_SHARE, hue_name

    subject = item.subject
    if subject is None or item.key_hue is None:
        return None
    held = []
    for offset, which in ((0.0, "the key's hue"), (180.0, "the key's complement")):
        share, hue = subject.near(item.key_hue + offset)
        if share >= OWN_HUE_MIN_SHARE and hue is not None:
            held.append(f"{hue_name(hue)} - {which} - on {share:.1%} of its pixels")
    if not held:
        return None
    return (
        f"Scene {item.scene_id}: what stands in front of the surface under {item.label} holds {' and '.join(held)} over "
        f"its clip ({subject.frames} frames read): its own colour, which the edge despill keeps and the halo gate never "
        "counts."
    )


def bow_covered(item: Measured) -> bool:
    """Whether the insert drawn on the curved surface *item* measured covers it: a confirmed bow on a convex
    quad, and the insert drawn on that frame (:attr:`Measured.drawn`) on its bezel with no key left past it
    (``insert_checks.covers_surface``) - nothing of the surface shows: a fact, never a finding."""
    from app.services.video_studio.insert_checks import covers_surface

    bezel = item.bezel
    if bezel is None or bezel.flatness.flat or not bezel.flatness.convex or item.drawn is None:
        return False
    return covers_surface(item.drawn)


def bow_note(item: Measured) -> str | None:
    """A bow the insert drawn on it covers (:func:`bow_covered`), in words: a fact. ``None`` otherwise."""
    from app.services.video_studio.insert_checks import off_bezel

    if not bow_covered(item):
        return None
    drawn = item.drawn or {}
    return (
        f"Scene {item.scene_id}: the surface under {item.label} bows on {item.where} ({item.bezel.flatness.words()}), and "
        f"the insert drawn on its bezel covers it - {off_bezel(drawn) * 100:.2f}% of the frame's width off it, "
        f"{float(drawn.get('residual_key') or 0.0) * 100:.2f}% of its quad left showing the key: a fact, not a finding."
    )


def measured_surfaces(measured: list[Measured]) -> dict[str, Measured]:
    """Each static screen insert whose bezel the save measured, by its overlay's key: the one measure of its
    surface's flatness - on its footage frame, whatever the resolution drawn - that its previews and its renders hold
    to, never read again on their frames (``insert_checks.findings``' *measured*)."""
    from app.services.video_studio import timeline

    return {timeline.insert_key(item.scene_id, item.index): item for item in measured if item.bezel is not None}


def verdicts(measured: list[Measured], revision: int | None = None) -> dict[str, list[dict]]:
    """What the save measured of each static screen insert, as the render holds its own findings to it
    (``insert_checks.findings``' *passed*): ``{insert key: [{stage: "save", id, gates: {flatness, alignment}}]}`` - the
    surface flat, and the quad on its bezel (written by the save, or within ``surface_refine.ALIGNMENT_MAX_SHARE`` of
    it). The same shot and spec, measured the same way: a render finding the save passed disagrees with it. A quad
    given carries how far off its bezel it is (``alignment``), with the frame read and its size (``where``,
    ``measured_at``): the measure a neighbour's preview at another size says (``insert_checks.bezel_readings``)."""
    from app.services.video_studio import timeline
    from app.services.video_studio.surface_refine import ALIGNMENT_MAX_SHARE

    out: dict[str, list[dict]] = {}
    for item in measured:
        bezel = item.bezel
        if bezel is None:
            continue
        gates = {"flatness": bezel.flatness.flat or bow_covered(item)}
        gates["alignment"] = item.given is None or bezel.error is None or bezel.error <= ALIGNMENT_MAX_SHARE
        named = f"revision {revision}" if revision is not None else f"measure {item.digest[:8]}"
        record: dict[str, Any] = {"stage": "save", "id": named, "gates": gates}
        if item.given is not None and bezel.error is not None and item.size is not None:
            record.update(alignment=round(float(bezel.error), 5), measured_at=list(item.size), where=item.where)
        out.setdefault(timeline.insert_key(item.scene_id, item.index), []).append(record)
    return out


def shot_verdict(params: dict | None, file_id: str) -> bool | None:
    """Whether a shot's own lint found its surface flat on the take that plays, the file *file_id*
    (``clips.take_surface``, kept in its frames as ``frame_qc.SURFACE``); ``None`` when that lint read another file - a
    take a later step drew over, an earlier take - or none: a record of other pixels is no evidence about these."""
    from app.services.video_studio import frame_qc
    from app.services.video_studio.surface_refine import Flatness

    qc = (params or {}).get(frame_qc.QC_KEY)
    record = qc.get(frame_qc.SURFACE) if isinstance(qc, dict) else None
    if not isinstance(record, dict) or record.get("take_file_id") != str(file_id) or not isinstance(record.get("flatness"), dict):
        return None
    return Flatness.from_dict(record["flatness"]).flat


async def shot_verdicts(db: Any, measured: list[Measured]) -> dict[str, list[dict]]:
    """What the AI shot under each static screen insert *measured* said of its own surface when the take that plays was
    made (:func:`shot_verdict`): ``{insert key: [{stage: "save", id: "shot <id>", gates: {flatness}}]}`` - held,
    as :func:`verdicts` is, against a render's own flatness finding."""
    from app.models.video_studio import VideoClip
    from app.services.video_studio import timeline

    out: dict[str, list[dict]] = {}
    rows: dict[str, Any] = {}
    for item in measured:
        if item.shot is None:
            continue
        if item.shot not in rows:
            rows[item.shot] = await db.get(VideoClip, uuid.UUID(item.shot))
        row = rows[item.shot]
        flat = shot_verdict(getattr(row, "params", None), item.file_id) if row is not None else None
        if flat is not None:
            key = timeline.insert_key(item.scene_id, item.index)
            out.setdefault(key, []).append({"stage": "save", "id": f"shot {item.shot}", "gates": {"flatness": flat}})
    return out


def flat_problem(item: Measured) -> str | None:
    """The words of a surface *item* measured that is curved or whose quad is not convex;
    ``None`` when it is flat - seen at an angle or not (:func:`angle_note`) - or its bow covered by the insert drawn on
    it (:func:`bow_covered`, a fact: :func:`bow_note`).

    Said first as what fixes it: only a shot made again does.
    """
    bezel = item.bezel
    if bezel is None or (fault := bezel.flatness.fault()) is None or bow_covered(item):
        return None
    if item.shot:
        fix = f"Regenerate the shot `{item.shot}` with a flat screen (generate_video_clip with clip_id `{item.shot}`)"
    else:
        fix = "Replace its footage with one showing a flat screen"
    shape = "curved" if bezel.flatness.convex else "not flat"
    return f"{fix}: the surface under {item.label} is {shape} (on {item.where}: {fault}) - a planar insert cannot cover it"


def findings(measured: list[Measured]) -> list[tuple[str, str, str, str | None]]:
    """``(scene id, kind, words, shot)`` of what the measurements say a render would show (``quality``): a curved
    surface (``insert_flatness``; one seen at an angle is none), and - footage made again since the save
    measured it - a quad left with no bezel, or a quad now off its bezel (``insert_bezel``)."""
    out: list[tuple[str, str, str, str | None]] = []
    for item in measured:
        if item.refusal is not None:
            if not any(found[2] == item.refusal for found in out):
                out.append((item.scene_id, "insert_bezel", item.refusal, item.shot))
            continue
        if (words := flat_problem(item)) is not None:
            out.append((item.scene_id, "insert_flatness", words, item.shot))
        bezel = item.bezel
        if item.given is None and bezel is None:
            out.append(
                (
                    item.scene_id,
                    "insert_bezel",
                    f"Regenerate the shot with its screen one flat green, blue or magenta surface: no bezel on "
                    f"{item.where}, where {item.label} comes in - or give the quad of the surface it covers",
                    item.shot,
                )
            )
        elif bezel is not None and bezel.error is not None and bezel.error > SNAP_MAX_SHARE:
            if bezel.found_by == "frame" and item.given is not None and _inside(item.given, bezel.quad):
                continue
            out.append(
                (
                    item.scene_id,
                    "insert_bezel",
                    f"{item.label}'s quad is {bezel.error * 100:.1f}% of the frame's width off the bezel {item.where} "
                    "shows now (its footage changed since the quad was measured) - leave its quad out (quad: auto) and "
                    "save: the bezel measured there is stored",
                    item.shot,
                )
            )
    return out


async def promotion_findings(db: Any, user: Any, clip_id: str, file_id: str) -> list[str]:
    """What the pre-render lint would refuse in each video of *user* that draws a screen insert on the AI shot
    *clip_id*, were its new take *file_id* to replace the one that plays (issue #10901): a quad left with no bezel
    or now off it (:func:`resolve`), a curved surface (:func:`flat_problem`), a track lost (:func:`track_note`), an
    edge spill the despill leaves past the halo gate (:func:`spill_note`). The same measure the save and the
    pre-render lint make (:func:`measure`), read on the new file and kept by it: a preview after a promotion reads
    it back. Empty when no video draws an insert on the shot, or nothing could be measured. Never raises."""
    from sqlalchemy import select

    from app.models.video_studio import VideoProject
    from app.services.video_studio import retention
    from app.services.video_studio.clips import UNDER_INSERT_SCAN
    from app.services.video_studio.projects import effective_spec
    from app.services.video_studio.screen_insert import MAX_HALO_SHARE
    from app.services.video_studio.spec import insert_footage, parse_video_spec

    out: list[str] = []
    try:
        keys = retention.media_keys([("clip", clip_id)])
        if not keys or user is None:
            return []
        rows = await db.execute(
            select(VideoProject)
            .where(retention.uses_media(keys), VideoProject.user_id == user.id)
            .limit(UNDER_INSERT_SCAN)
        )
        for project in rows.scalars().all():
            stored = dict(project.spec or {})
            if str(clip_id) not in insert_footage(stored):
                continue
            effective = (await effective_spec(db, project, user)).spec
            measured = await measure(
                db,
                parse_video_spec(effective),
                user=user,
                tracks=True,
                previous=stored_quads(stored),
                swap={str(clip_id): str(file_id)},
            )
            mine = [item for item in measured if item.shot == str(clip_id)]
            said = [problem.message for problem in resolve(stored, mine).problems]
            for item in mine:
                spill = getattr(item.bezel, "spill", None)
                lost = item.track.get("lost") if item.tracked and isinstance(item.track, dict) else None
                for words in (
                    None if item.tracked else flat_problem(item),
                    track_note(item) if lost else None,
                    spill_note(item)
                    if spill is not None and spill.after is not None and spill.after > MAX_HALO_SHARE
                    else None,
                ):
                    if words:
                        said.append(words)
            out.extend(f"video `{project.title}`, {words}" for words in dict.fromkeys(said))
    except Exception as exc:
        logger.info(
            "Screen inserts on shot %s not linted before its promotion: %s", clip_id, format_exception_message(exc)
        )
    return out


@dataclass
class Settled:
    """What the pre-render lint did with a video's screen inserts (:func:`settle`)."""

    #: What it wrote, said as the answer says it, by scene id.
    notes: list[tuple[str, str]] = field(default_factory=list)
    #: Why the render may not start - a bezel missing, a quad far off -, by scene id (``None``: the video's).
    problems: list[tuple[str | None, str]] = field(default_factory=list)
    #: The revision it saved, ``None`` when nothing was written.
    revision: int | None = None
    #: The scenes it changed.
    scenes: list[str] = field(default_factory=list)


async def settle(db: Any, project: Any, user: Any, effective: dict) -> Settled:
    """Measure *project*'s static screen inserts on their footage as it is now, and write what that settles.

    The save measured them; footage made since (a shot generated again in place,
    a recording made after the video named it) has not been. A quad left to
    the footage that it now finds, or a quad given within
    :data:`SNAP_MAX_SHARE` of the bezel, is written through the one spec write
    (``projects.save_spec``): a new revision, its scenes to preview again. What
    no write fixes is said (:attr:`Settled.problems`). Never raises.
    """
    from app.services.video_studio.projects import save_spec
    from app.services.video_studio.spec import parse_video_spec

    try:
        measured = await measure(
            db, parse_video_spec(effective), user=user, previous=stored_quads(dict(project.spec or {}))
        )
        resolution = resolve(dict(project.spec or {}), measured)
        if resolution.problems:
            return Settled(
                problems=[(item.scene_id, problem.message) for problem, item in zip(resolution.problems, resolution.refused, strict=True)]
            )
        if not resolution.changed:
            return Settled()
        saved = await save_spec(db, user=user, raw=resolution.authored, project=project, conversation_id=project.conversation_id)
    except VideoSpecError as exc:
        return Settled(problems=[(None, problem.message) for problem in exc.problems])
    except Exception as exc:
        logger.info("Screen insert bezels of video %s not settled: %s", getattr(project, "id", "?"), format_exception_message(exc))
        return Settled()
    if saved.unchanged:
        return Settled()
    notes = []
    for (scene_id, index), (quad, off) in resolution.written.items():
        how = f"Quad {off * 100:.1f}% off: snapped to the measured bezel" if off is not None else "Quad measured on its footage"
        notes.append(
            (
                scene_id,
                f"Scene {scene_id}, screen insert {index}: {how} {_quad_text(quad)}, saved as revision "
                f"{saved.project.revision} - re-preview scene {scene_id} (render_video_scene)",
            )
        )
    return Settled(
        notes=notes,
        revision=saved.project.revision,
        scenes=sorted({scene_id for scene_id, _index in resolution.written}),
    )


__all__ = [
    "SNAP_MAX_SHARE",
    "BezelRefusedError",
    "Measured",
    "Resolution",
    "Settled",
    "angle_note",
    "assign_screens",
    "bow_covered",
    "bow_note",
    "covered_note",
    "drawn_quad",
    "findings",
    "flat_problem",
    "frame_png",
    "hidden_note",
    "measure",
    "measured_surfaces",
    "promotion_findings",
    "resolve",
    "seed_quad",
    "settle",
    "spill_note",
    "stored_quads",
    "subject_note",
    "subjects",
    "track_note",
    "verdicts",
]
