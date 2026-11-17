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

"""One production of a video project, run by the worker.

1. **Media.** Each recording's mezzanine and each clip is downloaded to the
   worker's disk; a ``slide`` scene's slide is rendered by the deck exporter
   (``presentation_export.render_deck``, 2x) as the person who asked for the
   render, so a deck they cannot open renders nothing; images, the logo and
   the music's file (an upload or a ready AI track) are downloaded.
2. **Voice-over.** Each narrated scene is spoken (``voice.synthesize``: the
   pronunciations applied, the voice chain) and heard back
   (``voice_check.speak_checked``: transcribed, held against its text; when
   it failed, the take of its voice that passed on the same words before,
   else spoken again, then with a term heard wrong twice in another context,
   then slower, else by the closest voice of the same speech model), its
   length measured; with no speech provider the narration becomes captions
   only, and the render says so in its notes. A narration that failed every
   attempt stops a whole render before its frames are drawn, unless forced;
   so does one only the closest voice said, a finding naming
   that voice, why and what to change.
3. **Plan.** ``timeline.plan`` turns spec + media into the frame-exact
   document, with the brand palette (``themes.build_theme``) resolved from
   the template the spec names.
4. **Frames.** ``compositor.render_frames`` draws and encodes them in parallel
   chunks, cancellable between frames.
5. **Sound.** The narrations are laid on one voice stem (:func:`build_stem`,
   ``stitch.py``: sample-exact, faded, never overlapping); the music's bed is
   prepared for the plan (``music.make_bed``: a built-in score composed for
   its length and scenes, or the file fitted to it); ``encoder.mix_audio``
   takes the stem, keeps the clips' own sound where asked, ducks the bed
   where they play and normalises the master in two passes;
   ``encoder.mux`` joins picture and sound.
6. **Checks** (:func:`check_render`). The final AAC is demuxed and
   heard back against the stem (``audio_qc``: the voice-over transcribed, a
   sentence heard twice, stutters), the picture watched for frozen frames
   (``frame_freeze``); every finding is stored with its time
   (``render_checks``). Each narration heard and each stretch of picture
   watched is the render's progress. The watch is bounded per window
   and resumable: a resume past the encode keeps the encoded video
   (``compositor.encoded_video``) and only checks it again. A whole render
   with an audio finding fails, its MP4 kept, unless it was forced past its
   gate.
7. **Outputs.** The MP4, a poster and the WebVTT captions are stored as files
   of the requester; the project's ``latest_render_id`` moves to it, and a
   scene preview replaces the previous previews of its scene
   (``render_retention.replaced_previews``).

What could not be done as asked (a missing slide, no voice) is a note on the
render, never a failure of the whole video.

A scene preview (``VideoRender.scene_id``) runs the same pipeline on
a window of the video (:func:`scene_window`): the scene with the one before
and the one after it, planned as in the whole video, and only the frames from
the start of its transition in to the end of its transition out are drawn
(:func:`window_times`), at draft quality, with their voice and music; the
sound is cut to the same span in its one encode. Each narration is spoken
with its neighbours in the whole video, never the window's, so its
check is the one a whole render reads. The same stitcher, mixer,
encoder and checks run, so a preview's findings are the scene's own - a
neighbour's narration that failed, heard in its transitions, is a warning on
that neighbour. A neighbour is read as its own preview reads it - its
screen inserts' sources included, failing the preview on one that cannot be
read -, and its figure is drawn only for the frames the transition
shows, every figure kept by what it draws (``figure_cache``). It
stores the MP4 and three frames of the scene and the scene's digest, never
becomes what the video plays, and has a retention of its own.

The worker runs this in a process of its own (``render_process``):
*report* hands that process's supervisor each stage and each step of progress,
and every stage is logged at info level with its duration and the memory of
the render's processes, so a render that dies is diagnosable from the logs.
*on_frame* is called after each frame the render process draws, follows or
watches - a figure's frames, the footage under a tracked screen insert, the
compositor's frames, the frozen-picture check: that process renews
the execution's lease from it (``lease.FrameLease``), and a lease that
is gone ends the render without a word on its row, which another attempt or
the engine owns by then.
"""

from __future__ import annotations

import asyncio
import contextlib
import contextvars
import copy
import functools
import hashlib
import json
import logging
import math
import os
import shutil
import tempfile
import time
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from sqlalchemy import select

from app.core.config import settings
from app.core.database import async_session
from app.core.utils import format_exception_message, safe_uuid
from app.llm.identity import bind_llm_caller
from app.models.user import User
from app.models.video_studio import (
    JOB_CANCELLED,
    JOB_FAILED,
    JOB_READY,
    JOB_RUNNING,
    JOB_TERMINAL_STATUSES,
    VideoClip,
    VideoProject,
    VideoRender,
    VideoTake,
)
from app.services.orchestration_engine import LeaseLostError
from app.services.video_studio import (
    assets,
    audio_qc,
    beats,
    compositor,
    cpu_lanes,
    cut_blend,
    encoder,
    figures,
    frame_freeze,
    frame_qc,
    insert_bezels,
    insert_checks,
    insert_tracking,
    jobs,
    kept_regions,
    logos,
    motion_design,
    music_level,
    render_checks,
    render_pace,
    render_retention,
    render_retry,
    render_segments,
    resources,
    retention,
    reuse_index,
    stabilise,
    stitch,
    text_backfill,
    themes,
    timeline,
    voice,
    voice_check,
    voice_recheck,
)
from app.services.video_studio import music as music_tracks
from app.services.video_studio.frame_owners import FrameOwners
from app.services.video_studio.render_checks import clock
from app.services.video_studio.runners import JobOutcome, _slug
from app.services.video_studio.spec import (
    PREVIEW_QUALITIES,
    QUALITY_PRESETS,
    VideoSpec,
    asset_refs,
    design_size,
    output_size,
    parse_video_spec,
    recorded_ratio,
)

logger = logging.getLogger(__name__)

#: Receives the render's events (``{"type": "progress" | "stage" | "heartbeat", ...}``) as they happen.
Reporter = Callable[[dict], None]
#: How often a frame loop tells the supervisor the render moves, between its progress reports.
HEARTBEAT_SECONDS = 10.0
#: In a render's working directory, what its frozen-picture watch judged so far (``frame_freeze.Checkpoint``).
WATCH_FILE = "frame-watch.json"
#: ... and what its presence gate judged so far (``insert_checks.PresencePlace``, issue #8958).
PRESENCE_FILE = "presence-place.npz"
#: ... and which footage frames its duplicate logo pass judged so far (:func:`_duplicate_logos`, issue #9404).
DUPLICATE_LOGO_FILE = "duplicate-logo-place.json"


def _now() -> datetime:
    return datetime.now(UTC)


def frame_reporter(on_frame: Callable[[], None] | None, report: Reporter | None) -> Callable[[], None]:
    """What a stage calls for each frame it draws, follows or watches, and while an encoder it waits on works.

    It renews the lease (*on_frame*) and tells the stall window the render moves: at 2160p a stage
    can work for minutes between two progress reports.
    """
    beaten = [time.monotonic()]

    def frame_done() -> None:
        if on_frame is not None:
            on_frame()
        if time.monotonic() - beaten[0] >= HEARTBEAT_SECONDS:
            beaten[0] = time.monotonic()
            _emit(report, {"type": "heartbeat"})

    return frame_done


def _emit(report: Reporter | None, event: dict) -> None:
    if report is None:
        return
    try:
        report(event)
    except Exception as exc:  # the report is diagnostics: it never fails the render
        logger.debug("Render event not reported: %s", format_exception_message(exc))


@contextlib.asynccontextmanager
async def _stage(
    render_id: str, name: str, report: Reporter | None, timings: dict | None = None
) -> AsyncIterator[None]:
    """Log a stage's start and end with its duration and the memory of the render's processes; *timings* adds its
    seconds to the stage's (what the render's measurement keeps of each stage)."""
    started = time.monotonic()
    memory = resources.tree_memory(os.getpid())
    logger.info(
        "Video render %s: %s started (memory %s)",
        render_id,
        name,
        resources.format_bytes(memory),
        extra={"render_id": render_id, "render_stage": name, "memory_bytes": memory},
    )
    _emit(report, {"type": "stage", "stage": name, "event": "start"})
    outcome = "failed"
    try:
        yield
        outcome = "finished"
    finally:
        seconds = round(time.monotonic() - started, 2)
        if timings is not None:
            timings[name] = timings.get(name, 0.0) + seconds
        memory = resources.tree_memory(os.getpid())
        logger.info(
            "Video render %s: %s %s in %.1f s (memory %s)",
            render_id,
            name,
            outcome,
            seconds,
            resources.format_bytes(memory),
            extra={"render_id": render_id, "render_stage": name, "seconds": seconds, "memory_bytes": memory},
        )
        _emit(report, {"type": "stage", "stage": name, "event": outcome, "seconds": seconds})


#: How often the screen inserts' read-back says it moves while one of its steps works: an ffmpeg pass picking
#: a join's frames, a brand mark matched over a footage frame at every size, can work for minutes between two frames.
READBACK_BEAT_SECONDS = 5.0
#: The read-back's own wall time: a floor, and this much more for each frame its presence gate reads. Its
#: heartbeat keeps the stall window from stopping a step that works; this bound stops one that never ends.
READBACK_FLOOR_SECONDS = 900.0
READBACK_SECONDS_PER_FRAME = 0.5
#: How often the read-back's stage line says where it stands and how long is left (frame X of Y, about N min left).
READBACK_SAY_SECONDS = 30.0
#: The frames the presence gate decodes in this attempt before its pace is trusted for the time left.
READBACK_PACE_MIN_FRAMES = 60
#: The presence frames' watchdog: no new frame for this long (at least), while the read-back's heartbeat still beats,
#: ends the attempt with where it stood - a stalled step never keeps its lease (and its render slot) forever.
READBACK_STALL_FLOOR_SECONDS = 300.0
#: ... or for this many times the attempt's own measured seconds per frame, when that is longer.
READBACK_STALL_PACE_FACTOR = 10.0
#: The duplicate logo pass's own stage title: after the presence frames, it reads footage frames for minutes.
DUPLICATE_LOGO_STAGE = "Looking for the brand's mark twice on the footage frames"
#: The duplicate logo pass keeps its place after this many footage frames judged since the last one ...
DUPLICATE_LOGO_PLACE_FRAMES = 8
#: ... or after this many wall-clock seconds with a frame judged since: a relaunch goes on from there (issue #9404).
DUPLICATE_LOGO_PLACE_SECONDS = 30.0
#: The wall-clock seconds the copy of that place beside the render may take: a slow store costs that copy only.
DUPLICATE_LOGO_PLACE_STORE_SECONDS = 30.0


class ReadbackStalledError(Exception):
    """The presence frames stood still past their watchdog (:meth:`ReadbackWhere.stall_seconds`) while the read-back's
    heartbeat beat: the attempt ends with where it stood and runs again from the place kept. Not a ``RuntimeError``:
    a stalled step is the platform's, never the project's, so the render is retried, not failed."""


class ReadbackTimeoutError(RuntimeError):
    """The screen inserts' read-back ran past its own wall time (:func:`reading_back`): its message names the
    step, the frame and its second, and the inserts it stood at. A ``RuntimeError``: the render fails with it said."""


@dataclass
class ReadbackWhere:
    """Where the screen inserts' read-back stands: its step, the output frame it reads and the inserts on it."""

    step: str = "starting"
    frame: int | None = None
    inserts: tuple[str, ...] = ()
    #: The presence gate's last frame to decode, the frame this attempt began decoding at and when (monotonic).
    last: int | None = None
    began_frame: int | None = None
    began_at: float | None = None
    #: The frame a relaunch's presence gate went on from (its place kept by an attempt before).
    resumed_from: int | None = None
    #: When the presence gate last moved to a new frame (monotonic): what its watchdog and its stage line read.
    moved_at: float | None = None
    #: Set once its watchdog ended the read-back: the stall said.
    stalled: str | None = None
    #: The stage's title: what the render shows before the step (the duplicate logo pass has its own).
    title: str = "Reading the screen inserts back off the render"
    #: When the current step began (monotonic): a step outside the presence frames says how long it has run.
    step_at: float | None = None
    #: A step reading samples one by one (the duplicate logo pass): the sample it reads and how many it has.
    sample: int | None = None
    samples: int | None = None

    def at(self, step: str, now: float | None = None) -> None:
        self.step, self.frame, self.inserts, self.moved_at = step, None, (), None
        self.step_at = time.monotonic() if now is None else now
        self.sample = self.samples = None

    def moved(self, now: float) -> None:
        """The presence gate reached a new frame at *now*."""
        self.moved_at = now

    def still_seconds(self, now: float) -> float | None:
        """How long the presence gate has stood on its frame; ``None`` outside the presence frames."""
        if self.frame is None or self.moved_at is None:
            return None
        return max(0.0, now - self.moved_at)

    def stall_seconds(self, now: float) -> float:
        """The watchdog of the presence frames: :data:`READBACK_STALL_FLOOR_SECONDS`, or
        :data:`READBACK_STALL_PACE_FACTOR` times this attempt's measured seconds per frame when longer."""
        per_frame = 0.0
        if self.frame is not None and self.began_frame is not None and self.began_at is not None:
            read = self.frame - self.began_frame
            if read >= READBACK_PACE_MIN_FRAMES:
                per_frame = max(0.0, now - self.began_at) / read
        return max(READBACK_STALL_FLOOR_SECONDS, READBACK_STALL_PACE_FACTOR * per_frame)

    def stall_words(self, still: float) -> str:
        """Why the watchdog ended the read-back: the stage, the frame of the last and how long it stood."""
        last = f" of {self.last:,}" if self.last is not None else ""
        return (
            f"The screen inserts' read-back stalled at {self.step}, frame {self.frame or 0:,}{last}: no new frame for "
            f"{int(still)} s while its heartbeat beat, so this attempt ended and the next goes on from the place kept."
        )

    def seconds_left(self, now: float) -> float | None:
        """The presence gate's time left at this attempt's own pace; ``None`` before it is measured."""
        if self.frame is None or self.last is None or self.began_frame is None or self.began_at is None:
            return None
        read = self.frame - self.began_frame
        took = now - self.began_at
        if read < READBACK_PACE_MIN_FRAMES or took <= 0:
            return None
        return max(0, self.last - self.frame) * took / read

    def stage_words(self, now: float) -> str:
        """The stage line of the read-back: its step and, on the presence frames, frame X of Y and the time left."""
        said = f"{self.title}: {self.step}"
        if self.frame is None and self.samples:
            said += f", sample {min(self.sample or 0, self.samples):,} of {self.samples:,}"
        if self.frame is None and self.step_at is not None:
            # No pace to measure outside the presence frames: how long the step has run, never an invented ETA.
            ran = max(0.0, now - self.step_at)
            if ran >= READBACK_SAY_SECONDS:
                said += (
                    f", running for {max(1, math.floor(ran / 60))} min" if ran >= 60 else f", running for {int(ran)} s"
                )
        if self.frame is not None and self.last is not None:
            said += f", frame {self.frame:,} of {self.last:,}"
            left = self.seconds_left(now)
            still = self.still_seconds(now)
            if still is not None and still >= READBACK_SAY_SECONDS and (left is None or still > left):
                # Standing on its frame longer than the time it said was left: stalled, never "about 1 min left".
                said += f", stalled: no new frame for {max(1, math.floor(still / 60))} min"
            elif left is not None:
                said += f", about {max(1, math.ceil(left / 60))} min left"
            else:
                said += ", time left measured after its first frames"
        if self.resumed_from:
            said += f" (resumed from frame {self.resumed_from:,})"
        return said

    def words(self, fps: float) -> str:
        said = self.step
        if self.frame is not None:
            said += f", frame {self.frame} ({self.frame / max(float(fps), 1.0):.1f} s)"
        if self.inserts:
            said += f", insert {', '.join(self.inserts[:3])}"
        return said


def readback_budget(frames: int) -> float:
    """The read-back's wall time for a render whose presence gate reads *frames* frames."""
    return READBACK_FLOOR_SECONDS + READBACK_SECONDS_PER_FRAME * max(0, int(frames))


@contextlib.asynccontextmanager
async def reading_back(
    beat: Callable[[], None],
    *,
    budget: float | None,
    fps: float,
    say: Callable[[ReadbackWhere], Awaitable[None]] | None = None,
    title: str | None = None,
) -> AsyncIterator[ReadbackWhere]:
    """The screen inserts' read-back of an encoded render: *beat* (``frame_reporter``) called every
    :data:`READBACK_BEAT_SECONDS` while it runs, so the supervisor's stall window sees a working step move, and the
    whole of it bounded by *budget* seconds: past it, :class:`ReadbackTimeoutError` with where it stood. A *budget* of
    ``None`` beats without a bound: a check that cannot run is a note, never a failed render. Its steps
    keep the event loop free (their heavy work in threads), or neither the beat nor the bound could run. *say* is
    awaited every :data:`READBACK_SAY_SECONDS` with where it stands: the stage line with its time left, and at the
    first beat after the read-back moves to another step, so a short step never leaves the line of the one before.
    *title* is the stage's own name (``ReadbackWhere.title``)."""
    where = ReadbackWhere() if title is None else ReadbackWhere(title=title)
    # The step said last: the one it starts at, so the body's first step is said at the first beat.
    first_step = where.step

    async def beating() -> None:
        said_at = time.monotonic()
        said_step = first_step
        while True:
            await asyncio.sleep(READBACK_BEAT_SECONDS)
            now = time.monotonic()
            still = where.still_seconds(now)
            if still is not None and still > where.stall_seconds(now):
                # Its heartbeat would renew the lease forever over a step that no longer moves: end the attempt here.
                where.stalled = where.stall_words(still)
                logger.warning("Video render: %s", where.stalled)
                bound.reschedule(asyncio.get_running_loop().time())
                return
            beat()
            if say is not None and (time.monotonic() - said_at >= READBACK_SAY_SECONDS or where.step != said_step):
                said_at, said_step = time.monotonic(), where.step
                try:
                    await say(where)
                except asyncio.CancelledError:
                    raise
                except Exception as exc:  # diagnostics: never what stops the read-back
                    logger.info(
                        "Video render: the read-back's stage line was not written: %s", format_exception_message(exc)
                    )

    beats = asyncio.create_task(beating())
    bound = asyncio.timeout(budget)
    try:
        async with bound:
            yield where
    except TimeoutError as exc:
        if not bound.expired():  # a step's own timeout (an ffmpeg pass): its error, not the read-back's bound
            raise
        if where.stalled is not None:
            raise ReadbackStalledError(where.stalled) from exc
        raise ReadbackTimeoutError(
            f"The screen inserts were not read back off the render in {int(budget or 0)} s: it stood at {where.words(fps)}."
        ) from exc
    finally:
        beats.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await beats


class _Media:
    def __init__(self) -> None:
        self.plan = timeline.PlanMedia()
        self.assets = compositor.CompositorAssets()
        self.voice_paths: dict[str, str] = {}
        self.clip_paths: dict[str, str] = {}
        self.music_path: str | None = None
        #: The file the music was read from: what the render records it played.
        self.music_file_id: object | None = None
        self.notes: list[str] = []
        self.palette: dict = {}
        #: ``scene id -> (figures.FigureSource, the figure page's document)``: drawn once the timeline is planned.
        self.figures: dict[str, tuple[Any, dict]] = {}
        #: ``scene id -> the name of what it shows`` (a recording, an AI shot): its chapter's name.
        self.scene_names: dict[str, str] = {}
        #: Each narration as spoken and heard back (``voice_check.speak_checked``).
        self.spoken: list[voice_check.SpokenSegment] = []
        #: What spoke them: what speaks a drifted take again (:func:`_retake_drifted`); ``None`` with no voice.
        self.speaker: voice_check.Speaker | None = None
        #: The narrations as written (``voice.lines_of``) and the lexicon they are said with.
        self.lines: list[voice.Line] = []
        self.lexicon: Any = None
        #: ``scene id -> the digest of its narration as spoken`` (``reuse_index.voice_digests``).
        self.voice_digests: dict[str, str] = {}
        #: ``scene id -> figures.Drawing``: what drawing each figure did - its key, its frames drawn and taken.
        self.drawings: dict[str, figures.Drawing] = {}
        #: What transcribes the voice-over for the render's checks, or why nothing does.
        self.transcriber: Any = None
        self.unchecked_reason: str | None = None
        #: ``footage path -> (its raw take's path, each frame's map, the share kept)``: a stabilised shot a tracked
        #: screen insert sits on (``stabilise.raw_take``).
        self.steadied: dict[str, tuple[str, list, float | None]] = {}
        #: ``insert key -> why``: the screen inserts the render cannot draw - a brand mark the template has no file
        #: for, an SVG the rasteriser drew nothing of - each an ``insert_missing`` finding (``insert_checks.missing``).
        self.missing_inserts: dict[str, str] = {}
        #: ``insert key -> the quad found`` of each screen insert whose quad the render found on its footage
        #: (``quad: auto``, :func:`_find_quads`): reported with what its compositing measured.
        self.auto_quads: dict[str, list[list[float]]] = {}
        #: ``AI shot id -> the logo check of the take it plays`` (``frame_qc.logo_gate``), for the shots a scene
        #: plays as its footage: each duplicated logo a ``logo_duplicated`` finding (``logos.duplicate_findings``), each
        #: logo not integrated a ``logo_not_integrated`` one (``logos.integration_findings``).
        self.logo_checks: dict[str, dict] = {}
        #: ``AI shot id -> (its take to read, text_check)`` of the shots a scene plays as its footage whose take kept no
        #: text check (``text_backfill.unchecked_of``): read by the render for invented lettering (issue #9021).
        self.text_reads: dict[str, tuple] = {}
        #: ``AI shot id -> the regions its take draws back from its start picture`` (``kept_regions.boxes_of`` of the take
        #: that plays), for the shots a scene plays as its footage: a ``duplicate_logo`` place over one counts only when
        #: the template's mark is matched inside it (``logos.confirmed_places``).
        self.kept_regions: dict[str, list[list[float]]] = {}
        #: ``AI shot id -> its replaced screen`` (``params.screen``: the quad in the start picture, the screenshot
        #: as fitted), for the shots a scene plays as its footage: its pixels per pixel at the render's size are judged
        #: (``insert_checks.soft_screens``), and its scenes get check frames at the render's own size.
        self.screens: dict[str, dict] = {}
        #: ``recording id -> the pixel ratio it was recorded at`` (``spec.recorded_ratio``) of each recording a
        #: screen insert shows: what its ``soft`` record advises, never the ratio a browser zoom laid its page out at.
        self.insert_ratios: dict[str, float] = {}


async def _image_size(path: str) -> tuple[int, int]:
    """A picture's size; an SVG's from the size it declares (the page draws it)."""

    def read() -> tuple[int, int]:
        from PIL import Image

        with Image.open(path) as image:
            return image.size

    try:
        return await asyncio.to_thread(read)
    except Exception:
        from app.services.builtin_tools.image_assets import svg_dimensions

        try:
            with open(path, "rb") as source:
                declared = svg_dimensions(source.read(4096))
        except OSError:
            declared = None
        return declared or (1920, 1080)


#: ``path on this worker -> the file id it was downloaded from``, for the render running in this task: a scene range
#: drawn on another worker downloads the same file rather than one this render stores (``render_ranges``).
_ORIGINS: contextvars.ContextVar[dict[str, object] | None] = contextvars.ContextVar("render_origins", default=None)


def _downloaded(path: str, file_id: object) -> None:
    origins = _ORIGINS.get()
    if origins is not None and file_id:
        origins[path] = file_id


async def _download_file(file_id: object, path: str) -> object:
    """:func:`assets.download_file`, the file's origin recorded for the render's scene ranges."""
    row = await assets.download_file(file_id, path)
    if row is not None:
        _downloaded(path, file_id)
    return row


async def _download_as(file_id: object, base: Path, default_suffix: str) -> tuple[str | None, object]:
    """Download a file next to *base*, keeping its extension; ``(path, row)`` or ``(None, None)``."""
    row = await assets.download_file(file_id, str(base))
    if row is None:
        return None, None
    final = str(base) + (Path(row.filename).suffix.lower() or default_suffix)
    Path(base).rename(final)
    _downloaded(final, file_id)
    return final, row


async def _gather_lotties(media: _Media, spec: VideoSpec, workdir: Path) -> None:
    """The Lottie files the motion scenes play, each checked again before the page is handed it.

    The save checked them (``projects.verify_references``); a file replaced or
    gone since is left out of its scene, said, never played unchecked.
    """
    media.plan.lotties_gathered = True
    for index, (key, file_id) in enumerate(motion_design.lottie_files(spec.scenes).items()):
        found, _row = await _download_as(file_id, workdir / f"lottie-{index}", ".json")
        if not found:
            media.notes.append(f"Lottie file {file_id} is gone: its element is left out.")
            continue
        final = str(Path(found).with_suffix(".json"))
        if final != found:
            Path(found).rename(final)
        check = motion_design.check_lottie(await asyncio.to_thread(Path(final).read_bytes))
        if check.problem:
            media.notes.append(f"Lottie file {file_id} is left out: {check.problem}.")
            continue
        media.notes.extend(f"Lottie file {file_id}: {note}." for note in check.notes)
        media.assets.images[key] = final
        media.plan.lotties[key] = check


def _footage_shot(scene) -> str | None:
    """The AI shot a scene plays as the footage its screen inserts map onto: a clip's, a statement's backdrop's."""
    if scene.type == "clip":
        return scene.clip_id
    if scene.type == "statement" and scene.backdrop is not None:
        return scene.backdrop.clip_id
    return None


def _tracks_inserts(scene) -> bool:
    return any(overlay.type == "screen_insert" and overlay.track for overlay in scene.overlays)


async def _gather_raw_take(media: _Media, sid: str, shot: str | None, path: str, refs: dict, workdir: Path) -> None:
    """The un-stabilised take of the stabilised shot *path* plays: its tracked inserts are followed through it."""
    raw = refs["steadied"].get(shot or "")
    if raw is None or path in media.steadied:
        return
    file_id, maps, share = raw
    raw_path = str(workdir / f"raw-{sid}.mp4")
    if await _download_file(file_id, raw_path) is None:
        media.notes.append(
            f"Scene {sid}: the un-stabilised take of its AI shot is gone: its screen inserts follow the stabilised one."
        )
        return
    media.steadied[path] = (raw_path, maps, share)


async def _ready_clip(db, scene_id: str | None, clip_id: str) -> object:
    """The file of an AI shot a scene shows, or the reason it cannot render yet (``RuntimeError``).

    A ready shot generated again plays the take it keeps until the new one is ready (``clips.playable``).
    """
    from app.services.video_studio import clips

    clip = await db.get(VideoClip, uuid.UUID(clip_id))
    if clip is None:
        # Gone, never "still being generated": the same words for a scene's footage and a neighbour's.
        raise RuntimeError(
            f"Scene {scene_id}: AI shot {clip_id} was deleted: generate another one, or remove the scene."
        )
    label = clip.name or clip_id
    if clip.expired_at is not None:
        raise RuntimeError(
            f"Scene {scene_id}: the media of AI shot {label} expired with the retention: generate it again."
        )
    if clips.playable(clip):
        return clip.file_id
    if clip.status in (JOB_FAILED, JOB_CANCELLED):
        raise RuntimeError(
            f"Scene {scene_id}: AI shot {label} {clip.status} ({clip.error or 'no reason given'}): "
            "generate it again, or remove the scene."
        )
    if clip.status != JOB_READY or not clip.file_id:
        raise RuntimeError(f"Scene {scene_id}: AI shot {label} is still being generated: render once it is ready.")
    return clip.file_id


async def _ready_take(db, scene_id: str | None, take_id: str, *, what: str = "recording") -> VideoTake:
    """A recording a scene plays - its footage, or its screen insert's source (*what*) - or why it cannot render (``RuntimeError``).

    The one rule of both: a scene drawn as a neighbour in a scene preview reads its insert as its own
    preview does, never by a lighter check that leaves the insert out.
    """
    take = await db.get(VideoTake, uuid.UUID(take_id))
    if take is None:
        raise RuntimeError(f"Scene {scene_id}: {what} {take_id} was deleted: record it again, or remove the scene.")
    if take.expired_at is not None:
        raise RuntimeError(
            f"Scene {scene_id}: the media of {what} {take.name} expired with the retention: record it again."
        )
    if take.status in (JOB_FAILED, JOB_CANCELLED):
        raise RuntimeError(
            f"Scene {scene_id}: {what} {take.name} {take.status} ({take.error or 'no reason given'}): "
            "record it again, or remove the scene."
        )
    if take.status != JOB_READY or not take.file_id:
        raise RuntimeError(f"Scene {scene_id}: {what} {take_id} is not ready.")
    return take


async def _take_file(workdir: Path, take_id: str, file_id: object, scene_id: str | None) -> str:
    """A recording's file on the worker's disk, downloaded once whatever plays it: a scene's footage or a screen insert."""
    path = str(workdir / f"take-{take_id}.mp4")
    if not Path(path).exists() and await assets.download_file(file_id, path) is None:
        raise RuntimeError(f"Scene {scene_id}: the file of recording {take_id} is gone.")
    _downloaded(path, file_id)
    return path


async def _read_references(spec, user: User, template_id: str | None) -> dict:
    """Everything the render needs from the database, read once on a short session."""
    from app.api.v1.helpers import get_user_group_ids, user_visible_filter
    from app.models.presentation import Presentation
    from app.services.video_studio import lexicon_store
    from app.services.video_studio.projects import take_media

    found: dict = {
        "takes": {},
        "clips": {},
        "animations": {},
        "animation_notes": [],
        "decks": {},
        "palette": {},
        "voice_target": None,
        "voice_note": None,
        "insert_files": {},
        "insert_ratios": {},
        # The screen inserts that cannot be drawn, the same in a scene's own preview and a neighbour's.
        "missing_inserts": {},
        "steadied": {},
        "logo_checks": {},
        "text_reads": {},
        "kept_regions": {},
        "screens": {},
        "names": {},
        "transcriber": None,
        "unchecked_reason": None,
        "lexicon": None,
    }
    async with async_session() as db:
        groups = await get_user_group_ids(db, user.id)
        # The pronunciation lexicon: the voice says it, the captions show the entries applied to them.
        found["lexicon"] = await lexicon_store.for_voiceover(db, spec.voiceover)
        # A screen insert is part of its scene's picture: its source is read as the scene's own footage is,
        # in the scene's preview and in a neighbour's alike, and one that cannot be read fails the render.
        for key, part in timeline.insert_sources(spec).items():
            scene_id, label = timeline.insert_scene(key), timeline.insert_label(key)
            if part.kind == "file":
                found["insert_files"][key] = part.ref
            elif part.kind == "asset":
                # A brand mark of the template: resolved once the palette is read, below.
                continue
            elif part.kind == "take":
                take = await _ready_take(db, scene_id, part.ref, what=f"the recording of {label}")
                found["insert_files"][key] = take.file_id
                found["insert_ratios"][part.ref] = recorded_ratio(take.pixel_ratio or 1.0, take.script)
            else:
                found["insert_files"][key] = await _ready_clip(db, f"{scene_id} ({label})", part.ref)
        for scene in spec.scenes:
            if scene.type == "recording" and scene.take_id not in found["takes"]:
                take = await _ready_take(db, scene.id, scene.take_id, what="its recording")
                found["takes"][scene.take_id] = (take.file_id, float(take.fps or 60), take_media(take))
            elif scene.type == "clip" and scene.clip_id:
                found["clips"][scene.clip_id] = await _ready_clip(db, scene.id, scene.clip_id)
            elif scene.type == "statement" and scene.backdrop is not None and scene.backdrop.clip_id:
                found["clips"][scene.backdrop.clip_id] = await _ready_clip(db, scene.id, scene.backdrop.clip_id)
            elif scene.type == "image" and scene.animate is not None and scene.animate.clip_id:
                # An animation not made (yet) never stops a render: the scene is its still.
                try:
                    found["animations"][scene.id or ""] = await _ready_clip(db, scene.id, scene.animate.clip_id)
                except RuntimeError as exc:
                    found["animation_notes"].append(f"{exc} It plays as the still image meanwhile.")
            elif (
                scene.type in ("slide", "figure")
                and scene.presentation_id
                and scene.presentation_id not in found["decks"]
            ):
                deck = (
                    await db.execute(
                        select(Presentation.id, Presentation.spec).where(
                            Presentation.id == uuid.UUID(scene.presentation_id),
                            user_visible_filter(Presentation, user.id, groups, is_admin=user.role == "admin"),
                        )
                    )
                ).first()
                if deck is None:
                    raise RuntimeError(f"Scene {scene.id}: presentation {scene.presentation_id} is not available.")
                found["decks"][scene.presentation_id] = (deck[0], dict(deck[1] or {}))
        # A stabilised shot under a tracked screen insert: the raw take it was made from is what the insert follows.
        for scene in spec.scenes:
            shot = _footage_shot(scene)
            if shot is None or shot in found["steadied"] or not _tracks_inserts(scene):
                continue
            clip = await db.get(VideoClip, uuid.UUID(shot))
            if clip is not None and (raw := stabilise.raw_take(clip.params)) is not None:
                found["steadied"][shot] = raw
        # The duplicate check of each AI shot a scene plays as its footage: the take that plays, read above.
        from app.services.video_studio.clips import SCREEN, take_params

        for scene in spec.scenes:
            shot = _footage_shot(scene)
            if shot is None or shot in found["logo_checks"] or shot in found["screens"]:
                continue
            clip = await db.get(VideoClip, uuid.UUID(shot))
            played = take_params(clip.params) if clip is not None else {}
            record = frame_qc.logo_gate(played.get(frame_qc.QC_KEY)) if clip is not None else None
            if record is not None:
                found["logo_checks"][shot] = record
            # A ready take that kept no text check: its lettering is read by the render (issue #9021).
            if clip is not None and clip.status == JOB_READY and clip.expired_at is None:
                unread = text_backfill.unchecked_of(played, clip.file_id, clip.duration_ms)
                if unread is not None:
                    found["text_reads"][shot] = (unread, bool(played.get("text_check", True)))
            # The start picture's regions the take draws back: what shows there is the picture's own.
            if clip is not None and (regions := kept_regions.boxes_of(played)):
                found["kept_regions"][shot] = regions
            # The real screen drawn into the take, judged at the render's size.
            screen = played.get(SCREEN) if clip is not None else None
            if isinstance(screen, dict) and screen.get("quad"):
                found["screens"][shot] = screen
        # What the editor's timeline calls a recording or an AI shot (``preview.py``): its chapter's name.
        # The rows were read above: ``get`` answers from the session.
        for scene in spec.scenes:
            if scene.type == "recording":
                take = await db.get(VideoTake, uuid.UUID(scene.take_id))
                name = take.name if take is not None else None
            elif scene.type == "clip" and scene.clip_id:
                clip = await db.get(VideoClip, uuid.UUID(scene.clip_id))
                name = (clip.name or clip.prompt) if clip is not None else None
            else:
                continue
            if name:
                found["names"][scene.id or ""] = name[: timeline.CHAPTER_TITLE_MAX]
        if template_id:
            from app.services.presentations import resolve_template_theme

            try:
                found["palette"] = await resolve_template_theme(
                    db,
                    uuid.UUID(template_id),
                    user_id=user.id,
                    deck_theme=spec.theme.mode,
                    is_admin=user.role == "admin",
                )
            except Exception as exc:
                found["palette_note"] = f"The template's palette could not be read: {format_exception_message(exc)}"
        marks = timeline.brand_asset_files(spec, found["palette"])
        for key, part in timeline.insert_sources(spec).items():
            if part.kind != "asset":
                continue
            if marks.get(part.ref):
                found["insert_files"][key] = marks[part.ref]
            else:
                # Another mark never stands for it, and leaving it out shows the model's own drawn mark.
                found["missing_inserts"][key] = f"the template has no file for its {part.ref}"
        if spec.voiceover.enabled and any(s.narration for s in spec.scenes):
            try:
                found["voice_target"] = await voice.resolve(db, user.id, spec.voiceover)
            except voice.VoiceUnavailableError as exc:
                found["voice_note"] = str(exc)
            if found["voice_target"] is not None:
                found.update(await _hearing(db, user))
        source = spec.music.source if spec.music is not None else None
        if source is not None and source[0] == "music":
            # An AI track not made yet is the person's to wait for, like an AI shot.
            found["music_file"] = await music_tracks.ready_file(db, source[1])
        elif source is not None and source[0] == "file":
            found["music_file"] = source[1]
    return found


async def _hearing(db, user: User) -> dict:
    """What hears the narrations back: the speech-to-text the person may use, or why there is none."""
    from app.llm.transcription import resolve_transcriber

    found: dict = {}
    try:
        found["transcriber"] = await resolve_transcriber(db, user.id)
    except Exception as exc:
        found["unchecked_reason"] = format_exception_message(exc)[:300]
    return found


async def _gather_backdrop(media: _Media, sid: str, backdrop, refs: dict, workdir: Path) -> None:
    """What plays behind a statement: an AI shot or an uploaded video as frames, an image as a still."""
    if backdrop.clip_id:
        path = str(workdir / f"backdrop-{sid}.mp4")
        if await _download_file(refs["clips"][backdrop.clip_id], path) is None:
            raise RuntimeError(f"Scene {sid}: the video behind it is gone.")
    else:
        path, row = await _download_as(backdrop.file_id, workdir / f"backdrop-{sid}", ".png")
        if path is None:
            raise RuntimeError(f"Scene {sid}: the file behind it ({backdrop.file_id}) is gone.")
        if not (getattr(row, "content_type", None) or "").startswith("video/"):
            media.plan.backdrops[sid] = timeline.ImageMedia(*await _image_size(path))
            media.assets.images[f"backdrop/{sid}"] = path
            return
    info = await encoder.probe(path)
    media.plan.backdrops[sid] = timeline.ClipMedia(
        duration=info.duration,
        width=info.width,
        height=info.height,
        fps=info.fps or 24.0,
        frame_count=info.frame_count or int(info.duration * (info.fps or 24)),
    )
    media.assets.sources[f"backdrop/{sid}"] = compositor.FrameSource(path, info.fps or 24.0)


async def _gather_inserts(media: _Media, spec, refs: dict, workdir: Path) -> None:
    """The sources of the screen inserts: a video probed and decoded by its scene's page, a picture read once.

    A picture keeps its alpha; an SVG (a template's logo) is drawn to a PNG
    first, at the size a screen shows it - one the rasteriser draws
    nothing of is an ``insert_missing`` finding, with the rasteriser's reason.
    A recording is the file the scenes playing it as footage use
    (:func:`_take_file`), in a scene's own preview and a neighbour's alike; a
    source gone fails the render, naming its scene.
    """
    from app.services.video_studio.screen_insert import (
        Despill,
        InsertSource,
        KeySpec,
        SvgNotDrawnError,
        Zoom,
        svg_to_png,
    )

    media.plan.inserts_gathered = True
    sources = timeline.insert_sources(spec)
    for index, (key, file_id) in enumerate(refs["insert_files"].items()):
        part = sources[key]
        overlay = part.overlay
        scene_id = timeline.insert_scene(key)
        if part.kind == "take":
            path, row = await _take_file(workdir, part.ref, file_id, scene_id), None
        else:
            path, row = await _download_as(file_id, workdir / f"insert-{index}", ".mp4")
        if path is None:
            raise RuntimeError(f"Scene {scene_id}: the source of {timeline.insert_label(key)} ({file_id}) is gone.")
        quad = part.quad
        fit, glow = overlay.fit or "cover", float(overlay.glow or 0.0)
        content_type = getattr(row, "content_type", None) or ""
        is_video = part.kind in ("take", "clip") or content_type.startswith("video/")
        label = f"Scene {timeline.insert_scene(key)}: {timeline.insert_label(key)}"
        # The other surfaces its scene draws on the same footage: their key is theirs, never this one's spill.
        others = tuple(
            other.quad for name, other in sources.items() if name != key and timeline.insert_scene(name) == scene_id
        )
        shared = {
            "others": others,
            "fit": fit,
            "glow": glow,
            "light": timeline.insert_light(overlay),
            "key": KeySpec.of(overlay.key),
            "logo": part.ref if part.kind == "asset" else None,
            "label": label,
            "zoom": Zoom.of(timeline.insert_zoom(overlay)),
            "despill": Despill.of(timeline.insert_despill(overlay)),
        }
        if not is_video:
            if content_type.startswith("image/svg") or path.lower().endswith(".svg"):
                try:
                    path = await svg_to_png(path, label)
                except SvgNotDrawnError as exc:
                    media.missing_inserts[key] = f"its SVG was not drawn: {exc}"
                    continue
            media.plan.inserts[key] = timeline.ImageMedia(*await _image_size(path))
            media.assets.inserts[key] = InsertSource(path, "image", quad, **shared)
            continue
        info = await encoder.probe(path)
        fps = info.fps or 24.0
        media.plan.inserts[key] = timeline.ClipMedia(
            duration=info.duration,
            width=info.width,
            height=info.height,
            fps=fps,
            frame_count=info.frame_count or int(info.duration * fps),
        )
        media.assets.inserts[key] = InsertSource(path, "video", quad, fps=fps, **shared)


#: How ``compositor_page.buildStill`` frames an image scene's still: covering the frame with a ``cover`` fit or within
#: this share of its proportions, else contained in this share of its width and in its height less this share on each
#: side (the subtitles' room aside: the most it is drawn at).
_STILL_COVERS_WITHIN, _STILL_WIDTH, _STILL_EDGE = 0.03, 0.86, 0.09


def _still_scale(src_w: float, src_h: float, width: int, height: int, fit: object) -> float:
    """The scale an image scene's still of *src_w* x *src_h* is drawn at in a frame of *width* x *height*."""
    ratio, frame = src_w / max(src_h, 1.0), width / max(height, 1)
    if fit == "cover" or abs(ratio - frame) / frame < _STILL_COVERS_WITHIN:
        return max(width / max(src_w, 1.0), height / max(src_h, 1.0))
    return min(width * _STILL_WIDTH, height * (1.0 - 2 * _STILL_EDGE) * ratio) / max(src_w, 1.0)


def host_size(scene: dict, size: tuple[int, int]) -> tuple[float, float]:
    """The size, in the render's pixels, the footage of a planned *scene* is drawn at in a frame of *size*.

    What the page does (``compositor_page``): a clip's or a backdrop's picture
    covers the frame (``coverRect``: the frame, or more on the side its
    proportions overflow), a recording's viewport covers it the same way and
    is seen at its camera's closest zoom, an image scene's still is framed as
    the page frames it (:func:`_still_scale`), and a picture drifts in by a
    little more than its size (``timeline.STILL_HOST_ZOOM``). A screen
    insert's quad, a share of that host, is this many pixels wide and high.
    """
    width, height = size
    footage = timeline.insert_footage(scene)
    if footage is None:
        return float(width), float(height)
    src_w = float(footage.get("css_w") or footage.get("src_w") or footage.get("image_w") or width)
    src_h = float(footage.get("css_h") or footage.get("src_h") or footage.get("image_h") or height)
    if scene.get("type") == "image":
        scale = _still_scale(src_w, src_h, width, height, scene.get("fit"))
    else:
        scale = max(width / max(src_w, 1.0), height / max(src_h, 1.0))
    zoom = 1.0
    if scene.get("type") == "recording":
        zoom = max([1.0, *(float(k[3]) for k in scene.get("camera") or [] if len(k) > 3 and k[3])])
    elif timeline.footage_still(footage):
        zoom = timeline.STILL_HOST_ZOOM
    return src_w * scale * zoom, src_h * scale * zoom


def _insert_density(
    payload: dict, source: Any, host: tuple[float, float], kind: str | None, pixel_ratio: float | None
) -> dict | None:
    """The insert's source pixels per pixel of its quad (``screen_insert.density``) at the *host* size, as the payload keeps it.

    At the peak of its push (the payload's ``zoom``): the source drawn
    as on a quad that many times larger. A recording's record names
    the pixel ratio that would make it sharp: the smallest of the ratios a
    take can be recorded at that passes the check on the same viewport
    (``insert_checks.ratio_for``).
    """
    from app.services.video_studio import screen_insert
    from app.services.video_studio.pictures import quad_size

    width = getattr(source, "width", None)
    height = getattr(source, "height", None)
    if not width or not height:
        return None
    quad_w, quad_h = quad_size(tuple((float(x) * host[0], float(y) * host[1]) for x, y in payload["quad"]))
    push = float((payload.get("zoom") or {}).get("scale") or 1.0)
    found = screen_insert.density(
        (int(width), int(height)), quad_w * push, quad_h * push, str(payload.get("fit") or "cover")
    )
    if found is None:
        return None
    record = found.as_dict()
    if kind == "take" and pixel_ratio and found.soft:
        wanted = insert_checks.ratio_for(max(int(width) / float(pixel_ratio), 1.0), found)
        if wanted is not None:
            record["pixel_ratio"] = wanted
    return record


async def _matte_inserts(
    document: dict,
    media: _Media,
    size: tuple[int, int] | None = None,
    parts: dict | None = None,
    subjects: dict | None = None,
) -> None:
    """Each screen insert told the footage its matte reads: the payload's ``matte``, the source's ``footage``.

    On a video, the page names the footage frame under the insert and the
    driver mattes it there, the footage frame it comes in on its surface's
    reference; on a still backdrop the matte reads the still. A key named by
    its colour keys on the shade of it that first frame shows inside the quad
    (``screen_insert.keyed_on``): a video model's "chroma green" is its own. A
    picture's padding (a letterbox baked into it) is measured once, at the
    shape its quad gives it (``screen_insert.padded_sides``). With the
    render's *size*, each insert's source pixels per pixel of its
    quad at that size go on the payload (``soft``, :func:`_insert_density`),
    the ``soft_insert`` finding's measure; *parts* (``timeline.insert_sources``)
    say what each source is. A keyed insert on a video takes what stands in
    front of its surface over its clip (``InsertSource.subject``): the
    save's reading (*subjects*, ``insert_bezels.subjects``) when it read it,
    else read here (:func:`_subject_of`).
    """
    import dataclasses

    from app.services.video_studio import screen_insert
    from app.services.video_studio.pictures import quad_size

    for scene in document.get("scenes") or []:
        footage = timeline.insert_footage(scene)
        host = host_size(scene, size) if size is not None else None
        for payload in scene.get("inserts") or []:
            key = payload.get("src")
            insert = media.assets.inserts.get(key)
            if insert is None or footage is None:
                continue
            if host is not None:
                part = (parts or {}).get(key)
                ratio = media.insert_ratios.get(part.ref) if part is not None and part.kind == "take" else None
                density = _insert_density(
                    payload, media.plan.inserts.get(key), host, getattr(part, "kind", None), ratio
                )
                if density is not None:
                    payload["soft"] = density
            source_key = footage.get("src") or ""
            still = media.assets.images.get(source_key)
            frames = media.assets.sources.get(source_key)
            if frames is not None:
                time_map = [tuple(point) for point in footage.get("time_map") or []]
                first = round(timeline.map_forward(time_map, float(payload["t0"])) * frames.fps)
                matte = screen_insert.Footage(frames.path, "video", frames.fps, source_key)
                count = int(footage.get("src_frames") or 0) or 1
                payload["matte"] = {"fps": frames.fps, "frames": count}
                # Its clean surface is read over the frames it is drawn on, within its footage.
                last = round(timeline.map_forward(time_map, float(payload.get("t1") or payload["t0"])) * frames.fps)
                plate = screen_insert.plate_frames(max(first, 0), min(max(last, 0), count - 1))
                insert = dataclasses.replace(insert, footage=matte, first_frame=max(first, 0), plate=plate)
            elif still is not None:
                insert = dataclasses.replace(insert, footage=screen_insert.Footage(still, "image"))
                payload["matte"] = {"still": True}
            if insert.footage is not None:
                # The box every frame covers: the quad - a track's box - grown, so the quad can be snapped to
                # its bezel inside it; the page places the same box (``insertBox(ins.box)``).
                aspect = float(footage.get("src_w") or footage.get("image_w") or 1280) / float(
                    footage.get("src_h") or footage.get("image_h") or 720
                )
                box = screen_insert.grown_box(payload.get("box") or insert.quad, aspect)
                payload["box"] = [list(p) for p in box]
                insert = dataclasses.replace(insert, box=tuple(box))
            if insert.key is not None and insert.footage is not None:
                size = (
                    int(footage.get("src_w") or footage.get("image_w") or 1280),
                    int(footage.get("src_h") or footage.get("image_h") or 720),
                )
                first = await compositor.first_footage(insert, size)
                insert = await asyncio.to_thread(screen_insert.keyed_on, insert, first)
                if insert.footage.kind == "video":
                    insert = dataclasses.replace(
                        insert, subject=await _subject_of(key, insert, payload, footage, subjects)
                    )
            if insert.kind == "image":
                width = float(footage.get("src_w") or footage.get("image_w") or 1920)
                height = float(footage.get("src_h") or footage.get("image_h") or 1080)
                quad_w, quad_h = quad_size(tuple((x * width, y * height) for x, y in insert.quad))
                picture = await asyncio.to_thread(screen_insert.read_picture, insert.path)
                sides = await asyncio.to_thread(screen_insert.padded_sides, picture, insert.fit, quad_w, quad_h)
                if sides:
                    payload["padded"] = sides
            media.assets.inserts[key] = insert


async def _subject_of(key: str, insert: Any, payload: dict, footage: dict, subjects: dict | None) -> Any:
    """What stands in front of a keyed *insert*'s surface over its clip (``screen_insert.Subject``): the save's
    reading of its overlay (*subjects*, by the overlay's key - every pane of a wall alike), else read on its footage
    from the frame it comes in on to the frame it goes out on (``compositor.read_subject``); ``None`` when it cannot be
    read, the edge then judged frame by frame alone."""
    base = ".".join(key.split(".")[:2])
    if (subjects or {}).get(base) is not None:
        return subjects[base]
    time_map = [tuple(point) for point in footage.get("time_map") or []]
    last = round(timeline.map_forward(time_map, float(payload.get("t1") or payload["t0"])) * insert.footage.fps)
    try:
        return await compositor.read_subject(insert, last)
    except (OSError, RuntimeError, ValueError) as exc:  # a decoder failing, a footage frame past its end
        logger.info(
            "%s: what stands in front of it not read over its clip: %s",
            insert.label or key,
            format_exception_message(exc),
        )
        return None


def resumed_progress(relaunches: object) -> int:
    """The furthest a whole render stood (its percent) when a worker change took it over (``VideoRender.relaunches``,
    each entry's ``progress``): what its next attempt never reports less than, so a reader polling its jobs never sees
    the percent go down after a rollout. ``0`` for a render never taken over."""
    best = 0
    for entry in relaunches if isinstance(relaunches, list) else []:
        if isinstance(entry, dict):
            with contextlib.suppress(TypeError, ValueError):
                best = max(best, int(entry.get("progress") or 0))
    return max(0, min(99, best))


def _track_digest(footage: str, asked: list, steadied: tuple | None, *, render_id: str | None = None) -> str | None:
    """What the inserts shown on *footage* are followed from: the files, the stabilisation, what is asked.

    Keyed on the release, so a new release follows them again. With *render_id*, the key of that render's own
    tracks instead, whatever the release: a worker of the next release that takes the render over reads back the
    tracks the frames it kept were drawn on, rather than following every surface again.
    ``None`` for footage not downloaded from a file (a figure drawn by the render): followed every time.
    """
    import hashlib
    import json

    origins = _ORIGINS.get() or {}
    origin = origins.get(footage)
    raw = origins.get(steadied[0]) if steadied is not None else None
    if origin is None or (steadied is not None and raw is None):
        return None
    payload = {
        "release": settings.app_version,
        "version": insert_tracking.TRACK_VERSION,
        "footage": str(origin),
        "raw": str(raw) if raw is not None else None,
        "maps": steadied[1] if steadied is not None else None,
        "share": steadied[2] if steadied is not None else None,
        "asked": [[ask.quad, ask.first, ask.last, list(ask.time_map), ask.since, ask.key] for ask in asked],
    }
    if render_id is not None:
        del payload["release"]
        payload["render"] = str(render_id)
    return hashlib.sha256(json.dumps(payload, sort_keys=True, default=str).encode("utf-8")).hexdigest()


async def _kept_tracks(digest: str | None, count: int) -> list | None:
    """The tracks an earlier render of the video followed for *digest*, or ``None`` (``render_cache``)."""
    from app.services.video_studio import render_cache
    from app.services.video_studio.insert_tracking import InsertTrack

    if digest is None:
        return None
    kept = await render_cache.get_json("tracks", digest)
    if not isinstance(kept, list) or len(kept) != count:
        return None
    try:
        return [
            InsertTrack(
                int(item["first"]),
                tuple(tuple(tuple(p) for p in quad) for quad in item["quads"]),
                tuple(item["box"]),
                tuple(int(frame) for frame in item.get("bezel_read") or ()),
                int(item.get("held_before") or 0),
            )
            for item in kept
        ]
    except (KeyError, TypeError, ValueError):
        return None


async def _keep_preview_track(
    footage_file: object, scene_id: object, payload: dict[str, Any], time_map: Any, track: Any, fps: float
) -> None:
    """Keep the quads a tracked insert followed for the editor's preview (``insert_tracking.preview_track_digest``):
    its stand-in is drawn on them, frame by frame, until its build is drawn."""
    from app.services.video_studio import render_cache

    try:
        digest = insert_tracking.preview_track_digest(footage_file, scene_id, payload, time_map)
        kept = insert_tracking.preview_track_payload(track, fps)
    except (KeyError, TypeError, ValueError):  # the preview keeps its badge for it
        return
    await render_cache.put_json(insert_tracking.PREVIEW_TRACKS, digest, kept)


async def _keep_tracks(digest: str | None, follows: list) -> None:
    from app.services.video_studio import render_cache

    if digest is None:
        return
    try:
        kept = [
            {
                "first": f.first,
                "quads": [[list(p) for p in quad] for quad in f.quads],
                "box": list(f.box),
                "bezel_read": list(f.bezel_read),
                "held_before": f.held_before,
            }
            for f in follows
        ]
    except (AttributeError, TypeError):  # a track it cannot write down is followed again next time
        return
    await render_cache.put_json("tracks", digest, kept)


#: Why a screen insert whose quad is left to the render is not drawn: the one way ``quad: auto`` fails.
NO_BEZEL = (
    "no bezel was found on its first footage frame (quad: auto) - generate the shot again with its screen one flat "
    "green, blue or magenta surface, or give the quad of the surface it covers"
)


async def _find_quads(spec, document: dict, media: _Media):
    """*spec* with each quad left to the render (``quad: auto``) found on its insert's first footage frame.

    Read on the *document* planned with those inserts on their stand-in
    (``timeline.with_quads(stand_in=True)``): the footage frame each comes
    in on, as :func:`_matte_inserts` reads its surface's reference, and the
    bezel there (``surface_refine.found_quad``) is its quad - said in a
    note, reported with what its compositing measured (``media.auto_quads``)
    and drawn on every frame. None found there: the insert is not drawn, an
    ``insert_missing`` finding (:data:`NO_BEZEL`).
    """
    import dataclasses

    from app.services.video_studio import screen_insert, surface_refine

    planned = {scene.get("id"): scene for scene in document.get("scenes") or []}
    overlays = {
        (scene.id or "", index): overlay for scene in spec.scenes for index, overlay in enumerate(scene.overlays)
    }
    found: dict[tuple[str, int], tuple] = {}
    autos = timeline.auto_quads(spec)
    #: A scene of several inserts left to the footage: its screens, handed out once (``insert_bezels.assign_screens``).
    handed: dict[str, tuple[list, dict[int, int]]] = {}
    for scene_id, index in autos:
        scene = planned.get(scene_id) or {}
        base = timeline.insert_key(scene_id, index)
        payloads = [
            p for p in scene.get("inserts") or [] if p.get("src") == base or str(p.get("src")).startswith(f"{base}.")
        ]
        insert = media.assets.inserts.get(payloads[0]["src"]) if payloads else None
        footage = timeline.insert_footage(scene)
        if insert is None or footage is None:
            continue
        source_key = footage.get("src") or ""
        frames, still = media.assets.sources.get(source_key), media.assets.images.get(source_key)
        size = (
            int(footage.get("src_w") or footage.get("image_w") or 1280),
            int(footage.get("src_h") or footage.get("image_h") or 720),
        )
        probe = None
        if frames is not None:
            time_map = [tuple(point) for point in footage.get("time_map") or []]
            first = round(timeline.map_forward(time_map, float(payloads[0]["t0"])) * frames.fps)
            read = screen_insert.Footage(frames.path, "video", frames.fps, source_key)
            probe = dataclasses.replace(insert, footage=read, first_frame=max(first, 0))
        elif still is not None:
            probe = dataclasses.replace(insert, footage=screen_insert.Footage(still, "image"))
        try:
            frame = await compositor.first_footage(probe, size) if probe is not None else None
        except RuntimeError:  # a footage frame past its end: no bezel to read there
            frame = None
        key = screen_insert.KeySpec.of(overlays[(scene_id, index)].key)
        quad = None
        siblings = [i for sid, i in autos if sid == scene_id]
        if frame is not None and len(siblings) > 1:
            from app.services.video_studio import insert_bezels

            if scene_id not in handed:
                shown = await asyncio.to_thread(surface_refine.screens_on, frame, key.key() if key else None)
                height, width = frame.shape[:2]
                quads = [[(x / width, y / height) for x, y in item.corners] for item in shown]
                handed[scene_id] = (shown, insert_bezels.assign_screens(siblings, quads)[0])
            shown, assigned = handed[scene_id]
            if index in assigned:
                bezel = await asyncio.to_thread(
                    functools.partial(
                        surface_refine.measure_bezel,
                        frame,
                        key.key() if key else None,
                        key.matte_tolerance() if key else None,
                        surface=shown[assigned[index]],
                    )
                )
                quad = bezel.quad if bezel is not None else None
        elif frame is not None:
            quad = await asyncio.to_thread(
                surface_refine.found_quad, frame, key.key() if key else None, key.matte_tolerance() if key else None
            )
        if quad is None:
            for payload in payloads:
                media.missing_inserts[payload["src"]] = NO_BEZEL
                media.assets.inserts.pop(payload["src"], None)
            continue
        found[(scene_id, index)] = quad
        written = [[round(x, 4), round(y, 4)] for x, y in quad]
        for payload in payloads:
            media.auto_quads[payload["src"]] = written
        media.notes.append(
            f"Scene {scene_id}: {timeline.insert_label(base)} takes the bezel found on its first footage frame as its "
            f"quad (quad: auto): {written}."
        )
    resolved = timeline.with_quads(spec, found)
    for key, part in timeline.insert_sources(resolved).items():
        if key in media.auto_quads and key in media.assets.inserts:
            media.assets.inserts[key] = dataclasses.replace(media.assets.inserts[key], quad=tuple(part.quad))
    return resolved


async def _track_inserts(
    spec, document: dict, media: _Media, progress=None, *, on_frame=None, render_id: str | None = None
) -> None:
    """The quad of every tracked screen insert followed through its scene's footage (``insert_tracking``).

    Run on the planned *document*, whose time maps say which footage frames
    the insert shows: its quad on each frame goes to the driver
    (``InsertSource.track``) and the payload gets the frames and the box the
    page places it on. The inserts shown on the same footage are followed in
    one decode of it (``insert_tracking.track_all``); a wall of panes is
    followed once, its panes split from it (``insert_tracking.pane_track``).
    On a stabilised AI shot the inserts are followed through the raw take it
    was made from and placed back where the stabilised take draws them
    (``insert_tracking.track_stabilised``). A surface lost raises
    ``InsertTrackLostError`` - one the stabilisation's crop cut,
    ``ScreenCroppedError`` -, which fails the render with a ``surface_lost``
    finding and its frame (:func:`_surface_lost`); an insert over a picture
    (no footage to follow) stays where it is, said.
    *progress* and *on_frame* are the render's. The tracks are kept for the release and for the render
    *render_id* (:func:`_track_digest`): an attempt that takes it over on another release follows none again.
    """
    import dataclasses

    from app.services.video_studio import insert_tracking, screen_insert

    tracked = {key: part for key, part in timeline.insert_sources(spec).items() if part.overlay.track}
    if not tracked:
        return
    # By footage file: the source, then each insert followed - a wall once - with the payloads of its panes.
    by_footage: dict[str, tuple[Any, dict[tuple, tuple[insert_tracking.Asked, list]]]] = {}
    for scene in document.get("scenes") or []:
        footage = timeline.insert_footage(scene)
        for payload in scene.get("inserts") or []:
            key = payload.get("src")
            part = tracked.get(key)
            if part is None or key not in media.assets.inserts:
                continue
            where = (
                f"Scene {scene.get('id')}: {timeline.insert_label(timeline.insert_key(scene.get('id'), part.index))}"
            )
            source = media.assets.sources.get((footage or {}).get("src") or "")
            if source is None or footage is None or timeline.footage_still(footage):
                if part.pane in (None, 0):
                    media.notes.append(
                        f"{where} stays where its quad is: a picture behind it has no footage to follow."
                    )
                continue
            followed = by_footage.setdefault(source.path, (source, {}))[1]
            wall = (scene.get("id"), part.index)
            if wall not in followed:
                key_spec = screen_insert.KeySpec.of(part.overlay.key)
                asked = insert_tracking.asked_of(
                    payload, footage, source.fps, part.overlay.quad, where, scene_id=scene.get("id"), key=key_spec
                )
                followed[wall] = (asked, [])
            followed[wall][1].append((payload, part))
    for source, walls in by_footage.values():
        asked = [ask for ask, _payloads in walls.values()]
        if len(asked) == 1:
            said = f"{asked[0].what}: following its surface"
        else:
            said = f"{len(asked)} screen inserts on one footage: following their surfaces"

        async def heard(done: int, total: int, said: str = said) -> None:
            if progress is not None:
                await progress(11, f"{said} ({done}/{total} frames)")

        steadied = media.steadied.get(source.path)
        digest = _track_digest(source.path, asked, steadied)
        own = _track_digest(source.path, asked, steadied, render_id=render_id) if render_id else None
        follows = await _kept_tracks(digest, len(asked))
        if follows is None and own is not None:
            follows = await _kept_tracks(own, len(asked))
            if follows is not None:
                logger.info(
                    "Video render %s: %s: the tracks it followed before a worker change, read back", render_id, said
                )
        if follows is None:
            if steadied is not None:
                raw_path, maps, share = steadied
                follows = await insert_tracking.track_stabilised(
                    raw_path, maps, asked, fps=source.fps, kept_share=share, progress=heard, on_frame=on_frame
                )
            else:
                follows = await insert_tracking.track_all(source.path, asked, progress=heard, on_frame=on_frame)
            await _keep_tracks(digest, follows)
            await _keep_tracks(own, follows)
        for ask, follow in zip(asked, follows, strict=True):
            if follow.bezel_read:
                # Two sources placed it: which frames each one placed, the switches between them blended.
                media.notes.append(f"{ask.what}: track: {follow.said()} ({follow.said_spans()}).")
            if follow.held_before:
                # Its surface lost before it comes in: held there, never a failure of the render.
                media.notes.append(
                    f"{ask.what}: its surface could not be followed over the {follow.held_before} footage frames before "
                    "it comes in, so its keyed surface is held on the quad of the frame after them there; the residual "
                    "key gate reads what those frames show."
                )
        footage_file = (_ORIGINS.get() or {}).get(source.path)
        for ((scene_id, _index), (ask, payloads)), follow in zip(walls.items(), follows, strict=True):
            for payload, part in payloads:
                rows, cols = part.overlay.grid or (1, 1)
                pane = (
                    follow
                    if part.pane is None
                    else insert_tracking.pane_track(follow, rows, cols, part.overlay.pane_gutter, part.pane)
                )
                if footage_file is not None and steadied is None:
                    # The editor's preview draws its stand-in on these quads (insert_builds.attach_tracks).
                    await _keep_preview_track(footage_file, scene_id, payload, ask.time_map, pane, source.fps)
                payload["track"] = {"from": pane.first, "to": pane.last, "fps": source.fps}
                payload["box"] = pane.box_quad()
                media.assets.inserts[payload["src"]] = dataclasses.replace(
                    media.assets.inserts[payload["src"]], track=pane
                )


async def _figure_source(scene, refs: dict, *, scheme: str, user: User) -> tuple[Any, dict]:
    """A figure scene's block and the figure page's document, on the video template's palette, its template
    assets drawn as their twins for *scheme* where the slide places them for the other (``figure_twins``)."""
    from app.services.presentations import load_logo_asset_meta
    from app.services.video_studio import figure_twins

    try:
        if scene.presentation_id:
            deck_id, deck_spec = refs["decks"][scene.presentation_id]
            source = figures.source_from_deck(scene, deck_spec, deck_id)
        else:
            source = figures.source_from_spec(scene)
        async with async_session() as db:
            source = await figure_twins.themed(db, source, scheme=scheme, user=user)
            asset_meta = await load_logo_asset_meta(db, {"slides": [source.slide]})
        return source, figures.figure_document(source, scheme=scheme, palette=refs["palette"], asset_meta=asset_meta)
    except figures.FigureError as exc:
        raise RuntimeError(str(exc)) from None


def figure_span(at: dict, window: tuple[float, float] | None, fps: int) -> tuple[float, float] | None:
    """The seconds of the figure scene placed *at* a scene preview draws, when it draws less than all of it.

    A preview's *window* (``window_times``: from the start of its scene's
    transition in to the end of its transition out) shows a neighbour only
    under a transition; ``None`` for a scene the window shows whole, or for a
    whole render. Two frames of margin each side.
    """
    if window is None:
        return None
    start, length, margin = float(at["start"]), float(at["end"]) - float(at["start"]), 2.0 / max(fps, 1)
    lo = max(0.0, window[0] - start - margin)
    hi = min(length, window[1] - start + margin)
    if lo <= 0.0 and hi >= length:
        return None
    return round(lo, 4), round(max(lo, hi), 4)


async def _draw_figures(
    spec,
    plan,
    media: _Media,
    *,
    user: User,
    fps: int,
    scale: int,
    workdir: Path,
    progress,
    on_frame=None,
    video_id: str | None = None,
    window: tuple[float, float] | None = None,
) -> None:
    """Draw every figure scene for the length the plan gives it, its build timed on its narration.

    The narration's words are timed on its spoken voice when it has one
    (``speech_timing``), else paced over the span the plan gives it, and a
    step never comes in after the subtitle showing its words (the scene's
    caption cues); the figure keeps clear of the scene's subtitles.
    *on_frame* is the render's: each frame drawn renews its lease. Each figure is drawn once per
    key across renders and previews, kept beside the video *video_id*
    (``figure_cache``); in a scene preview's *window* a neighbour's is
    drawn only for the frames the window shows (:func:`figure_span`). What
    each drawing did - its key, its frames drawn and taken - is kept on
    *media* (``drawings``).
    """
    from app.services.video_studio import figure_fit, speech_timing

    viewport = design_size(spec.format.aspect_ratio)
    # The brand's easing a figure's stand in space turns with: what its drift is fitted to the frame by.
    ease = figure_fit.move_ease(spec.theme.motion)
    placed = {scene["id"]: scene for scene in plan.document["scenes"]}
    by_id = {scene.id: scene for scene in spec.scenes}
    for position, (sid, (source, document)) in enumerate(media.figures.items()):
        scene, at = by_id[sid], placed[sid]
        length = float(at["end"]) - float(at["start"])
        span = figure_span(at, window, fps)
        words = None
        if sid in plan.narration_spans and scene.narration:
            said_from, said_to = plan.narration_spans[sid]
            words = await speech_timing.word_times(
                scene.narration,
                start=said_from - float(at["start"]),
                seconds=said_to - said_from,
                audio_path=media.voice_paths.get(sid),
            )

        async def drawn(done: int, total: int, *, _sid: str = sid, _position: int = position, _span=span) -> None:
            what = f"Drawing figure {_position + 1}/{len(media.figures)}"
            if not total:
                said = f"{what}: kept from an earlier render"
            elif done >= total:
                said = f"Encoding figure {_position + 1}/{len(media.figures)} ({total} frames)"
            elif _span is not None:
                said = f"{what} ({done}/{total} frames, under the transition)"
            else:
                said = f"{what} ({done}/{total} frames)"
            await progress(11, said, drawing=[_sid])

        try:
            path, clip = await figures.draw(
                source,
                document,
                user=user,
                scene=scene,
                length=length,
                words=words,
                fps=fps,
                viewport=viewport,
                scale=scale,
                workdir=workdir,
                safe=figures.caption_safe_area(spec, scene, viewport[1]),
                progress=drawn,
                on_frame=on_frame,
                video_id=video_id,
                readable=await figures.readable_pictures(source, user),
                span=span,
                drawing=media.drawings.setdefault(sid, figures.Drawing()),
                captions=figures.scene_captions(plan, sid, float(at["start"])),
                ease=ease,
            )
        except figures.FigurePageError as exc:
            # A valid figure whose page failed twice on fresh pages (after a resume, across builds): the platform's
            # failure, said as its Chromium's, so the render runs again once (``render_retry``), never a spec finding.
            raise compositor.ChromiumError(str(exc)) from None
        except figures.FigureError as exc:
            raise RuntimeError(str(exc)) from None
        media.plan.clips[sid] = clip
        media.assets.sources[f"clip/{sid}"] = compositor.FrameSource(path, float(fps))


async def _gather_media(
    spec,
    *,
    user: User,
    template_id: str | None,
    workdir: Path,
    progress,
    forced: bool = False,
    lines: list[voice.Line] | None = None,
    origin: str | None = None,
    keep: dict[str, dict] | None = None,
) -> _Media:
    """Download, render and speak what the spec references, with no database session open.

    Each narration is heard back (``voice_check``); one that failed every
    attempt is placed only when the render was *forced* past its gate.
    *lines*: the narrations to speak, each with its neighbours (a scene
    preview's, read in the whole video); ``None``: *spec*'s own.
    *origin*: the render, as a take that passes keeps it (``Speaker.origin``).
    *keep*: ``scene id -> the take a ready render played`` of each narration a
    splice does not re-check (``voice_check.place_kept``): placed as is.
    """
    from app.services.presentation_export import render_deck

    media = _Media()
    refs = await _read_references(spec, user, template_id)
    if refs.get("palette_note"):
        media.notes.append(refs["palette_note"])
    media.notes.extend(refs["animation_notes"])
    media.missing_inserts.update(refs["missing_inserts"])
    media.logo_checks.update(refs["logo_checks"])
    media.text_reads.update(refs.get("text_reads") or {})
    media.kept_regions.update(refs.get("kept_regions") or {})
    media.screens.update(refs["screens"])
    media.insert_ratios.update(refs["insert_ratios"])
    media.palette = refs["palette"]
    media.scene_names = dict(refs["names"])
    for position, scene in enumerate(spec.scenes):
        if position:
            await progress(1 + 5 * position // len(spec.scenes), f"Gathering the media ({position}/{len(spec.scenes)})")
        sid = scene.id or ""
        if scene.type == "recording":
            file_id, fps, take = refs["takes"][scene.take_id]
            path = await _take_file(workdir, scene.take_id, file_id, sid)
            media.plan.takes[scene.take_id] = take
            media.assets.sources[f"take/{sid}"] = compositor.FrameSource(path, fps)
        elif scene.type == "clip" or (scene.type == "image" and sid in refs["animations"]):
            if scene.type == "image":
                file_id = refs["animations"][sid]
            else:
                file_id = refs["clips"][scene.clip_id] if scene.clip_id else scene.file_id
            path = str(workdir / f"clip-{sid}.mp4")
            if await _download_file(file_id, path) is None:
                raise RuntimeError(f"Scene {sid}: its video file is gone.")
            info = await encoder.probe(path)
            media.plan.clips[sid] = timeline.ClipMedia(
                duration=info.duration,
                width=info.width,
                height=info.height,
                fps=info.fps or 24.0,
                frame_count=info.frame_count or int(info.duration * (info.fps or 24)),
                has_audio=info.has_audio,
            )
            media.assets.sources[f"clip/{sid}"] = compositor.FrameSource(path, info.fps or 24.0)
            media.clip_paths[sid] = path
            if scene.type == "clip":
                await _gather_raw_take(media, sid, scene.clip_id, path, refs, workdir)
        elif scene.type == "statement" and scene.backdrop is not None:
            await _gather_backdrop(media, sid, scene.backdrop, refs, workdir)
            if (source := media.assets.sources.get(f"backdrop/{sid}")) is not None:
                await _gather_raw_take(media, sid, scene.backdrop.clip_id, source.path, refs, workdir)
        elif scene.type == "image":
            final, _row = await _download_as(scene.file_id, workdir / f"image-{sid}", ".png")
            if final is None:
                raise RuntimeError(f"Scene {sid}: image {scene.file_id} is gone.")
            media.plan.images[sid] = timeline.ImageMedia(*await _image_size(final))
            media.assets.images[f"image/{sid}"] = final
        elif scene.type == "slide":
            deck_id, deck_spec = refs["decks"][scene.presentation_id]
            slides = list(deck_spec.get("slides") or [])
            index = (scene.slide or 1) - 1
            if scene.slide_id:
                index = next((i for i, s in enumerate(slides) if s.get("id") == scene.slide_id), -1)
            if not 0 <= index < len(slides):
                raise RuntimeError(f"Scene {sid}: the deck has no slide {scene.slide_id or scene.slide}.")
            rendered = await render_deck(
                deck_id,
                user.id,
                aspect_ratio=deck_spec.get("aspect_ratio") or "16:9",
                want_slide_images=True,
                image_scale=2,
                slide_indices=[index],
                hide_chrome=not scene.chrome,
            )
            if not rendered.slide_images:
                raise RuntimeError(f"Scene {sid}: the slide did not render.")
            final = str(workdir / f"slide-{sid}.png")
            Path(final).write_bytes(rendered.slide_images[0])
            media.plan.images[sid] = timeline.ImageMedia(*await _image_size(final))
            media.assets.images[f"image/{sid}"] = final
        elif scene.type == "figure":
            media.figures[sid] = await _figure_source(scene, refs, scheme=spec.theme.mode, user=user)
    await progress(6, "Media ready")

    palette = media.palette
    logo_id = timeline.palette_marks(spec, palette)["logo"]
    if logo_id:
        final, _row = await _download_as(logo_id, workdir / "logo", ".png")
        if final:
            media.assets.images["logo"] = final
    # The watermark draws the brand's mark when the template has one.
    if palette.get("mark_file_id") and not spec.theme.logo_file_id:
        final, _row = await _download_as(palette["mark_file_id"], workdir / "mark", ".png")
        if final:
            media.assets.images["mark"] = final
    # The pictures of the image overlays: a file, or a brand mark of the template by its role.
    media.plan.overlays_gathered = True
    for index, (key, file_id) in enumerate(timeline.overlay_file_ids(spec, palette).items()):
        final = (await _download_as(file_id, workdir / f"overlay-{index}", ".png"))[0] if file_id else None
        if final:
            media.assets.images[key] = final
            media.plan.overlays[key] = timeline.ImageMedia(*await _image_size(final))
    await _gather_lotties(media, spec, workdir)
    await _gather_inserts(media, spec, refs, workdir)
    if refs.get("music_file"):
        final, _row = await _download_as(refs["music_file"], workdir / "music-source", ".mp3")
        if final is None:
            media.notes.append("The music file is gone: rendered without music.")
        media.music_path = final
        media.music_file_id = refs["music_file"] if final else None
    elif spec.music is not None and spec.music.enabled and spec.music.source is None:
        media.notes.append("The music's AI track was never started (no music model): rendered without music.")
    # The music's beats, where the cuts and the builds land: the preview reads the same ones.
    media.plan.beats, beat_note = await beats.video_grid(spec, music_path=media.music_path, file_id=media.music_file_id)
    if beat_note:
        media.notes.append(beat_note)

    lines = voice.lines_of(spec) if lines is None else lines
    media.lines, media.lexicon = lines, refs["lexicon"]
    if media.lexicon is not None:
        media.plan.captions = timeline.caption_texts(spec, media.lexicon)
    if refs["voice_note"]:
        media.notes.append(refs["voice_note"])
    target = refs["voice_target"]
    media.voice_digests = reuse_index.voice_digests(target, spec, media.lexicon)
    media.transcriber, media.unchecked_reason = refs["transcriber"], refs["unchecked_reason"]
    if target is not None:
        speaker = voice_check.Speaker(
            target,
            spec.voiceover,
            refs["transcriber"],
            refs["unchecked_reason"],
            lexicon=media.lexicon,
            origin=origin,
        )
        media.speaker = speaker
        for index, line in enumerate(lines):
            kept = (keep or {}).get(line.scene_id)
            spoken = (
                await voice_check.place_kept(
                    line, kept, workdir=str(workdir), video_speed=voice.line_speed(line, spec.voiceover)
                )
                if kept
                else None
            )
            if spoken is None:
                spoken = await voice_check.speak_checked(speaker, line, workdir=str(workdir))
            _place_narration(media, spoken, forced=forced)
            await progress(6 + int(6 * (index + 1) / len(lines)), "Voice-over (spoken and heard back)")
    return media


def _place_narration(media: _Media, spoken: voice_check.SpokenSegment, *, forced: bool) -> None:
    """*spoken* as the render lays it: kept with its notes, its take placed when the render plays it.

    A narration that failed every attempt is placed only by a forced render, with its finding; one the closest voice
    of its speech model said is placed, with its finding too (:func:`_voice_findings`): a whole render stops
    on it before drawing unless forced, a preview plays that voice.
    """
    media.spoken.append(spoken)
    media.notes.extend(note for note in spoken.notes if note not in media.notes)
    if spoken.path and (spoken.status != voice_check.FAILED or forced):
        media.plan.voices[spoken.scene_id] = timeline.VoiceMedia(spoken.duration or 0.0)
        media.voice_paths[spoken.scene_id] = spoken.path


def _voice_findings(
    media: _Media,
    plan,
    *,
    offset: float = 0.0,
    span: tuple[float, float] | None = None,
    scene_id: str | None = None,
) -> list:
    """A finding for each narration that failed its transcript check, at its place in the MP4.

    A narration the closest voice of its speech model said after the video's
    failed is one too (``voice_fallback``): another voice
    mid-film, which the audience hears - a failure like a narration that
    failed, naming the scene, that voice, why and what to change; a
    forced render plays it with the finding. In the preview of *scene_id*,
    the narration of a scene before or after it, heard in its
    transitions, is that scene's: a warning on it, never a failure of the
    scene previewed. The gate refuses a whole render on either all the same
    (its check record failed in the video's voice).
    """
    findings = []
    for spoken in media.spoken:
        fallback = spoken.fallback and spoken.status != voice_check.FAILED
        if spoken.status != voice_check.FAILED and not fallback:
            continue
        start = plan.narration_starts.get(spoken.scene_id, 0.0)
        end = start + (spoken.duration or 0.0)
        if span is not None and (end < span[0] or start > span[0] + span[1]):
            continue
        data: dict = {"attempts": [a.as_dict() for a in spoken.attempts]}
        if fallback:
            kind = render_checks.VOICE_FALLBACK
            change = spoken.voice_change or voice_check.VoiceChange(
                "the video's voice",
                spoken.placed.voice if spoken.placed is not None else "another voice",
                voice_check.own_voice_failure(spoken),
                "",
            )
            message = change.sentence(spoken.scene_id)
            data["voice_change"] = change.as_dict()
        else:
            kind = render_checks.VOICE_SEGMENT
            placed = "placed anyway (forced)" if spoken.scene_id in media.voice_paths else "not placed"
            message = f"its narration failed its transcript check after {len(spoken.attempts)} attempts ({spoken.reason}): {placed}"
        neighbour = scene_id is not None and spoken.scene_id != scene_id
        if neighbour:
            message += (
                f" - heard in the transition of scene {scene_id}, which it does not fail; fix it, then preview scene "
                f"{spoken.scene_id} (render_video_scene(scene='{spoken.scene_id}')): a render of the video refuses it "
                "until then"
            )
        findings.append(
            render_checks.Finding(
                kind,
                max(0.0, start - offset),
                max(0.0, end - offset),
                spoken.scene_id,
                message,
                render_checks.WARN if neighbour else render_checks.FAIL,
                data,
            )
        )
    return findings


async def build_stem(plan, voice_paths: dict[str, str], workdir: Path) -> stitch.Stem | None:
    """The voice stem of *plan*: each narration on its sample (``stitch.stitch``); ``None`` with none spoken."""
    segments = [
        stitch.Segment(scene_id, path, plan.narration_starts[scene_id])
        for scene_id, path in voice_paths.items()
        if scene_id in plan.narration_starts
    ]
    if not segments:
        return None
    try:
        return await asyncio.to_thread(
            stitch.stitch, segments, duration=plan.duration, out_path=str(workdir / "voice-stem.wav")
        )
    except stitch.StitchError as exc:
        raise RuntimeError(f"The voice-over could not be laid out: {exc}") from None


async def _retake_drifted(
    media: _Media,
    findings: list[render_checks.Finding],
    *,
    workdir: Path,
    progress: Callable[[int, str], Awaitable[None]] | None = None,
) -> list[str]:
    """Each narration take the render heard say another scene's sentence instead of its own text
    (``audio_qc.drifted_takes``) spoken anew once and heard back (``voice_check.speak_checked(fresh=True)``): the scenes
    whose new take passed its own check in the video's voice and fits the time planned for the one it replaces, now in
    *media*. The render mixes and hears its sound again over them; a take that cannot be replaced stays, and so does its
    finding. A sentence a scene writes and says twice is a real repeat: never spoken again, its finding blocks.
    """
    if media.speaker is None:
        return []
    lines = {line.scene_id: line for line in media.lines}
    retaken: list[str] = []
    for scene in audio_qc.drifted_takes(findings):
        line = lines.get(scene)
        before = next((s for s in media.spoken if s.scene_id == scene), None)
        if line is None or before is None or scene not in media.voice_paths:
            continue
        if progress is not None:
            await progress(93, f"Speaking scene {scene}'s narration again: its take said another scene's sentence")
        # Its own folder: the take it may replace keeps its file until the new one passed.
        folder = workdir / f"retake-{scene}"
        try:
            folder.mkdir(parents=True, exist_ok=True)
            spoken = await voice_check.speak_checked(media.speaker, line, workdir=str(folder), fresh=True)
        except Exception as exc:  # a retake that cannot be spoken leaves the take and its finding as they were
            media.notes.append(
                f"Scene {scene}: its take said another scene's sentence; it could not be spoken again "
                f"({format_exception_message(exc)[:160]})."
            )
            continue
        planned = before.duration or 0.0
        fits = spoken.duration is not None and spoken.duration <= planned + 1e-3
        if spoken.path is None or spoken.status != voice_check.PASSED or spoken.fallback or not fits:
            why = (
                f"it lasts {spoken.duration:.2f} s, past the {planned:.2f} s planned"
                if spoken.status == voice_check.PASSED and not spoken.fallback and spoken.path and not fits
                else f"it did not pass its own check in the video's voice ({spoken.reason or spoken.status})"
            )
            media.notes.append(
                f"Scene {scene}: its take said another scene's sentence; spoken again once, not placed: {why}."
            )
            continue
        media.spoken[media.spoken.index(before)] = spoken
        media.voice_paths[scene] = spoken.path
        media.notes.extend(note for note in spoken.notes if note not in media.notes)
        media.notes.append(
            f"Scene {scene}: its take said another scene's sentence instead of its own text; spoken again once, "
            "heard back, and the render's sound mixed and heard again with it."
        )
        retaken.append(scene)
    return retaken


async def check_render(
    *,
    final_path: str,
    stem: stitch.Stem | None,
    plan,
    media: _Media,
    size: tuple[int, int],
    fps: int,
    workdir: Path,
    span: tuple[float, float] | None = None,
    scene_id: str | None = None,
    bitrate: str | None = None,
    on_frame: Callable[[], None] | None = None,
    progress: Callable[[int, str], Awaitable[None]] | None = None,
    music: encoder.MusicTrack | None = None,
    voice_volume: float = 1.0,
    music_closing: float = 0.0,
    watch_key: str | None = None,
    render_id: str | None = None,
    owners: FrameOwners | None = None,
    watch: bool = True,
    speeds: dict[str, float] | None = None,
    loop_at: tuple[float, float, float] | None = None,
    render_fps: float | None = None,
    scene_takes: dict | None = None,
    remix: Callable[[], Awaitable[stitch.Stem | None]] | None = None,
) -> render_checks.Checks:
    """What the render sounds and looks like, heard and watched back: every finding timed in its MP4.

    *render_fps* (a scene preview drawn under the whole render's rate): the take frames of its recording scenes'
    synthesised stretches are read as the render at that rate shows them (``double_take``, *scene_takes*: scene id ->
    its ``TakeMedia``), so a preview that reads green raises no ``double_image`` the render then finds (issue #9474),
    and the take frames the render's watch reads go through its static chrome watch (``flicker_take``): no
    ``static_flicker`` the render then blocks (issue #10054).

    *watch* ``False``: its frames are a ready render's, byte for byte (a splice) - their watch is that render's.

    *span* ``(start, seconds)`` is the part of *plan* the MP4 plays (a scene
    preview of *scene_id*): its own narration alone is transcribed and graded
    (``audio_qc.graded_narrations``), its frames watched, everything
    timed from the MP4's start. The voice is heard through
    the final mix's encode at *bitrate*, its lexicon terms checked.
    The *music* bed the mix took is measured as it plays under the voice at
    *voice_volume* (``music_level``): the audio check's ``music`` section,
    a quiet stretch starting in its last *music_closing* seconds its decay
    under the closing card (``music_level.closing_seconds``).
    *on_frame* is the render's: each frame watched renews its lease.
    *progress* ``(percent, stage)`` is told each narration heard and each
    stretch of picture watched, with the window and the scenes the
    watch reads: a check that stalls is told from a long one by its
    stage and when it last moved, and by where. Each window of the watch is
    bounded well inside the stall window (``frame_freeze.window_budget``);
    with *watch_key* (what the MP4's frames were drawn from) what it judged
    is kept in *workdir* after each window, and a resume goes on from there;
    with *render_id* it is stored beside the render too, and an attempt
    on another worker - a deploy replaced the one before and its disk - takes
    it back before the watch, which goes on where it stood.
    Each frame it watches is the scene *owners* (the render's
    ``frame_owners.FrameOwners``) gives its encoded frame.
    *remix* (a whole render): a narration take heard saying another scene's sentence instead of its own text
    (``audio_qc.drifted_takes``) is spoken again once (:func:`_retake_drifted`); *remix* then lays the voice stem,
    mixes and muxes *final_path* again, and the voice-over is heard again before the picture is watched.
    Last, the mix as heard (``mix_check``): loudness and true peak per scene, the music's end and a loop
    seam (*loop_at*: ``encoder.bed_repeat`` of a bed shorter than the video), each narration's pace against its *speeds*, silent
    gaps and still pictures over silence - the audio check's ``mix`` section and its warnings.
    """
    offset = span[0] if span else 0.0

    async def heard_so_far(done: int, total: int) -> None:
        if progress is not None:
            await progress(93, f"Hearing the render: {done} of {total} narration{'s' if total != 1 else ''} heard")

    async def watched_so_far(seconds: float, total: float, window: frame_freeze.Window | None) -> None:
        if progress is not None:
            stage = f"Watching the render for frozen pictures: {seconds:.0f} of {total:.0f} s"
            await progress(94, f"{stage}, now {window.where()}" if window is not None else stage)

    def scene_of(at: float) -> str | None:
        return (render_checks.scene_at(plan.document, at + offset) or {}).get("id")

    async def hear(stem: stitch.Stem | None) -> render_checks.Checks:
        """The narrations' findings and the voice-over heard back off the MP4 as *stem* lays it."""
        checks = render_checks.Checks(voice=[spoken.summary() for spoken in media.spoken])
        checks.findings.extend(_voice_findings(media, plan, offset=offset, span=span, scene_id=scene_id))
        texts = {line.scene_id: line.text for line in media.lines}
        heard = {s.scene_id: s.heard_words for s in media.spoken if s.heard_words and s.scene_id in media.voice_paths}
        spoken_by = {s.scene_id: s for s in media.spoken}
        narrations: list[audio_qc.Narration] = []
        known: dict[str, audio_qc.HeardTake] = {}
        own: dict[str, audio_qc.OwnCheck] = {}
        if stem is not None:
            # Each narration where the stem placed its take; a preview's own alone: its transitions play the
            # neighbours', never graded here.
            narrations = audio_qc.graded_narrations(stem.spans(), texts, offset=offset, scene_id=scene_id)
            for narration in narrations:
                # Heard already: read from its own words while the voice heard still plays that take.
                if (scene := narration.scene_id) in heard:
                    known[scene] = audio_qc.HeardTake(
                        media.voice_paths[scene], heard[scene], spoken_by[scene].heard_digest
                    )
                if (spoken := spoken_by.get(narration.scene_id)) is not None:
                    own[narration.scene_id] = audio_qc.OwnCheck(
                        spoken.status == voice_check.PASSED, spoken.transcript, spoken.language
                    )

        # The takes of the narrations it grades that it does not play, with the terms they heard wrong: the
        # pronunciation summary counts them beside the takes kept.
        unplayed = [
            (spoken.scene_id, wrong)
            for spoken in media.spoken
            if scene_id is None or spoken.scene_id == scene_id
            for wrong in spoken.unplayed_failures(played=spoken.scene_id in media.voice_paths)
        ]
        try:
            audio = await audio_qc.check_audio(
                final_path=final_path,
                stem_path=stem.path if stem is not None and narrations else None,
                narrations=narrations,
                transcriber=media.transcriber,
                unchecked_reason=media.unchecked_reason,
                scene_of=scene_of,
                workdir=str(workdir),
                span=span,
                lexicon=media.lexicon,
                bitrate=bitrate,
                known=known,
                own=own,
                heard=heard_so_far,
            )
            checks.audio = audio.as_summary()
            checks.pronunciation = audio_qc.with_attempts(audio.pronunciation, unplayed)
            checks.findings.extend(audio.findings)
            checks.notes.extend(audio.notes)
        except compositor.RenderCancelledError:
            raise
        except Exception as exc:  # a check that cannot run is said, never a failed render
            logger.warning("Audio check of %s failed: %s", final_path, format_exception_message(exc), exc_info=True)
            reason = f"the check could not run ({format_exception_message(exc)[:200]})"
            checks.audio = {"status": render_checks.SKIPPED, "reason": reason}
            checks.pronunciation = audio_qc.with_attempts({"status": render_checks.SKIPPED, "reason": reason}, unplayed)
        return checks

    checks = await hear(stem)
    # A take that said another scene's sentence instead of its own text is spoken again once, like a narration that
    # failed its own check, then the sound is mixed and heard again (*remix*); a sentence written once and said twice
    # in its own scene is never retaken, and every finding of the second hearing stands.
    if (
        remix is not None
        and scene_id is None
        and audio_qc.drifted_takes(checks.findings)
        and await _retake_drifted(media, checks.findings, workdir=workdir, progress=progress)
    ):
        stem = await remix()
        checks = await hear(stem)
    bed_blocks: list[tuple[float, float]] | None = None
    if music is not None and music.volume > 0:
        try:
            bed_blocks = await encoder.momentary_loudness(music.path)
            level = music_level.measure(
                bed_blocks,
                duration=plan.duration,
                volume=music.volume,
                duck_db=music.duck_db,
                duck_windows=music.windows,
                voice_windows=plan.voice_windows(voiced=set(media.voice_paths)),
                voice_volume=voice_volume,
                span=span,
                closing=music_closing,
            )
            checks.audio["music"] = level.summary()
            checks.findings.extend(
                render_checks.Finding(
                    render_checks.MUSIC_LEVEL,
                    problem.at,
                    problem.end,
                    scene_of(problem.at),
                    problem.message,
                    render_checks.WARN,
                    {"code": problem.code},
                )
                for problem in level.problems()
            )
        except compositor.RenderCancelledError:
            raise
        except Exception as exc:  # a measure that cannot run is said, never a failed render
            logger.warning("Music level of %s not measured: %s", final_path, format_exception_message(exc))
            checks.notes.append(f"The music's level was not measured ({format_exception_message(exc)[:160]}).")
    mix = MixInputs(final_path, stem, plan, media, span, scene_id, music, bed_blocks, speeds or {}, loop_at)
    if not watch:
        await _check_mix(checks, mix)
        return checks
    checkpoint = None
    if watch_key:
        place = Path(workdir) / WATCH_FILE
        if render_id is not None and await render_segments.restore_check_place(render_id, place):
            logger.info("Video render %s: the frozen-picture watch's place taken back from the store", render_id)
        checkpoint = frame_freeze.Checkpoint(
            place,
            f"{watch_key}|{fps}|{size[0]}x{size[1]}|{offset}",
            functools.partial(render_segments.keep_check_place, render_id) if render_id is not None else None,
        )
    try:
        frames = await frame_freeze.check_frames(
            final_path,
            document=plan.document,
            width=size[0],
            height=size[1],
            fps=fps,
            offset=offset,
            on_frame=on_frame,
            duration=span[1] if span else None,
            watched=watched_so_far,
            budget=frame_freeze.window_budget(float(settings.video_studio_render_stall_seconds)),
            checkpoint=checkpoint,
            owners=owners,
        )
        checks.frames = frames.as_summary()
        checks.findings.extend(frames.findings)
    except (LeaseLostError, compositor.RenderCancelledError):
        raise
    except Exception as exc:
        logger.warning("Frame check of %s failed: %s", final_path, format_exception_message(exc), exc_info=True)
        checks.frames = {
            "status": render_checks.SKIPPED,
            "reason": f"the check could not run ({format_exception_message(exc)[:200]})",
        }
    if scene_id and render_fps and render_fps > fps and scene_takes:
        try:
            from app.services.video_studio import double_take

            doubled, read = await double_take.findings(
                plan.document,
                sources=media.assets.sources,
                takes=scene_takes,
                render_fps=float(render_fps),
                offset=offset,
                span=span,
                on_frame=on_frame,
            )
            checks.findings.extend(doubled)
            if isinstance(checks.frames, dict) and read["take_frames"]:
                checks.frames = {**checks.frames, "render_rate": read}
        except (LeaseLostError, compositor.RenderCancelledError):
            raise
        except Exception as exc:  # a gate that cannot run is said, never a failed preview
            logger.warning("Render-rate double image check of %s failed: %s", final_path, format_exception_message(exc))
            checks.notes.append(
                f"The take frames were not read at the render's {render_fps:g} fps ({format_exception_message(exc)[:160]})."
            )
    if scene_id and render_fps and render_fps > fps:
        try:
            # The take frames the whole render's watch reads, through its static chrome watch: a flicker the
            # preview's own frames step over is said here, never first by the render (issue #10054).
            from app.services.video_studio import flicker_take

            flickered, watched = await flicker_take.findings(
                plan.document,
                sources=media.assets.sources,
                render_fps=float(render_fps),
                offset=offset,
                span=span,
                on_frame=on_frame,
            )
            checks.findings.extend(flickered)
            if isinstance(checks.frames, dict) and watched["take_frames"]:
                checks.frames = {**checks.frames, "render_rate_flicker": watched}
        except (LeaseLostError, compositor.RenderCancelledError):
            raise
        except Exception as exc:  # a gate that cannot run is said, never a failed preview
            logger.warning("Render-rate static flicker check of %s failed: %s", final_path, format_exception_message(exc))
            checks.notes.append(
                f"The take frames were not watched for flicker at the render's {render_fps:g} fps "
                f"({format_exception_message(exc)[:160]})."
            )
    try:
        # Every cut join the MP4 plays, read at its own frame rate: a frame of both scenes is a double image. What it
        # read is stored beside its findings (``checks.cuts``), so a clean pass is said, never silent (issue #8935).
        cuts = await cut_blend.read_cuts(
            final_path, plan.document, size=size, offset=offset, seconds=span[1] if span else None
        )
        checks.cuts = cuts.as_summary()
        checks.findings.extend(cuts.findings)
    except compositor.RenderCancelledError:
        raise
    except Exception as exc:  # a gate that cannot run is said, never a failed render
        logger.warning("Cut join check of %s failed: %s", final_path, format_exception_message(exc))
        checks.cuts = cut_blend.not_read(f"the check could not run ({format_exception_message(exc)[:160]})")
        checks.notes.append(f"The frames at the cuts were not read ({format_exception_message(exc)[:160]}).")
    await _check_mix(checks, mix)
    return checks


async def _loop_point(bed: music_tracks.Bed, source_path: str | None) -> tuple[float, float, float] | None:
    """Where a looping bed made from a file repeats: ``encoder.bed_repeat`` of its source, the repeat lag
    (the crossfade included), the bed's second the repeat enters at and its first second heard clean in the repeat;
    ``None`` for a built-in score, a bed played once and a track that lasts the whole video."""
    if bed.kind == "track" or not bed.loop or not source_path:
        return None
    try:
        length = (await encoder.probe(source_path)).duration
    except encoder.EncoderError:
        return None
    return encoder.bed_repeat(length, bed.duration, start=bed.start, loop=bed.loop, ending=bed.ending)


@dataclass(frozen=True)
class MixInputs:
    """What :func:`_check_mix` reads: the MP4, the stem and the plan it was made from, the bed the mix took."""

    final_path: str
    stem: stitch.Stem | None
    plan: Any
    media: Any
    span: tuple[float, float] | None
    scene_id: str | None
    music: encoder.MusicTrack | None
    bed_blocks: list[tuple[float, float]] | None
    speeds: dict[str, float]
    loop_at: tuple[float, float, float] | None


async def _check_mix(checks: render_checks.Checks, mix: MixInputs) -> None:
    """The mix as heard (``mix_check``): the audio check's ``mix`` section and its warnings; a measure that
    cannot run is a note, never a failed render."""
    from app.services.video_studio import mix_check

    offset = mix.span[0] if mix.span else 0.0
    try:
        log = await encoder.loudness_log(mix.final_path)
        scenes = [
            (str(scene.get("id")), float(scene.get("start") or 0.0), float(scene.get("end") or 0.0))
            for scene in mix.plan.document.get("scenes") or []
            if isinstance(scene, dict) and scene.get("id")
        ]
        paces: list[mix_check.ScenePace] = []
        if mix.stem is not None:
            spans = {
                sid: span
                for sid, span in mix.stem.spans().items()
                if (mix.scene_id is None or sid == mix.scene_id) and sid in mix.media.voice_paths
            }
            if spans:
                samples = audio_qc._from_f32(await encoder.decode_mono(mix.stem.path, rate=audio_qc.ANALYSIS_RATE))
                texts = {line.scene_id: line.text for line in mix.media.lines}
                paces = mix_check.narration_pace(samples, audio_qc.ANALYSIS_RATE, spans, texts, mix.speeds)
        music_end = loop = None
        if mix.music is not None and mix.bed_blocks:
            gain = 20.0 * math.log10(mix.music.volume) if mix.music.volume > 0 else -120.0
            music_end = mix_check.music_end_of([(t, lufs + gain) for t, lufs in mix.bed_blocks])
            if mix.loop_at is not None:
                bed = audio_qc._from_f32(await encoder.decode_mono(mix.music.path, rate=mix_check.SEAM_RATE))
                lag, at, clean = mix.loop_at
                found = mix_check.find_loop_seam(bed, mix_check.SEAM_RATE, expected=lag, after=max(1.5, clean + 0.1))
                # The lag found, said as the bed's second the repeat enters at: the lag less the source seconds the
                # first pass skipped (music.start, a natural ending's cut).
                loop = (round(found[0] - (lag - at), 2), found[1]) if found is not None else None
        holds = [
            (float(held.get("at") or 0.0), float(held.get("end") or 0.0), held.get("scene"))
            for held in (checks.frames or {}).get("by_design") or []
            if isinstance(held, dict)
        ]
        holds += [
            (f.at, f.end, f.scene_id) for f in checks.findings if f.kind == render_checks.FROZEN and f.end is not None
        ]
        report = mix_check.analyse(
            log.blocks,
            duration=mix.span[1] if mix.span else mix.plan.duration,
            scenes=scenes,
            peaks=log.peaks,
            true_peak=log.true_peak,
            paces=paces,
            offset=offset,
            music_end=music_end,
            video_end=mix.plan.duration if music_end is not None else None,
            loop=loop,
            holds=holds,
        )
        checks.audio["mix"] = report.summary()
        checks.findings.extend(
            render_checks.Finding(
                render_checks.AUDIO_MIX,
                problem.at,
                problem.end,
                problem.scene_id,
                problem.message,
                render_checks.WARN,
                {"code": problem.code},
            )
            for problem in report.problems
        )
    except compositor.RenderCancelledError:
        raise
    except Exception as exc:  # a measure that cannot run is said, never a failed render
        logger.warning("Mix of %s not measured: %s", mix.final_path, format_exception_message(exc))
        checks.notes.append(f"The mixed audio was not measured ({format_exception_message(exc)[:160]}).")


def scene_window(effective: dict, scene_id: str, full: dict) -> tuple[dict, int]:
    """The spec of a scene preview and the scene's index in it; :class:`LookupError` for no such scene.

    The scene with the one before and the one after it: its transition in
    plays over the end of the first and its transition out over the start of
    the next, planned as in the whole video. The video's fades stay only where
    the window reaches its start or its end. A music file or an AI track
    starts where it plays at that point of the whole video (*full*: the whole
    video's planned document); a built-in score is composed for the window.
    """
    scenes = list(effective.get("scenes") or [])
    ids = [scene.get("id") for scene in scenes]
    if scene_id not in ids:
        raise LookupError(scene_id)
    index = ids.index(scene_id)
    first, end = timeline.preview_window(len(scenes), index)
    window = copy.deepcopy(effective)
    window["scenes"] = copy.deepcopy(scenes[first:end])
    if first > 0:
        window["fade_in"] = False
    if end < len(scenes):
        window["fade_out"] = False
    music = window.get("music")
    planned = {scene.get("id"): scene for scene in full.get("scenes") or []}
    if isinstance(music, dict) and (music.get("music_id") or music.get("file_id")) and first > 0:
        offset = float((planned.get(ids[first]) or {}).get("start") or 0.0)
        music["start"] = round(min(600.0, float(music.get("start") or 0.0) + offset), 3)
        if music.get("ending") == "natural":
            music["ending"] = "fade"
    return window, index - first


def window_times(document: dict, target: int) -> tuple[float, float, float, float]:
    """``(from, to, settled from, settled to)`` of the scene at *target* in a window's planned *document*.

    It is drawn from the start of its transition in to the end of the next
    scene's (its transition out), or to the end for the last scene; it is
    settled - neither transition playing, nor the video's fade from or to
    black where the window reaches it - in between.
    """
    scenes = document["scenes"]
    start = float(scenes[target]["start"])
    if target + 1 < len(scenes):
        following = scenes[target + 1]
        end = float(following["start"]) + float((following.get("transition_in") or {}).get("duration") or 0.0)
    else:
        end = float(document["duration"])
    settled_from, settled_to = timeline.owned_span(document, target)
    return start, end, settled_from, settled_to


async def _store_proofs(shown: list, *, base: str, user_id, what: str) -> list:
    """The frames the screen-insert findings show, stored as files of the requester: their ids, each on its finding."""
    stored = []
    for index, (finding, path) in enumerate(shown):
        if not path or not Path(path).is_file():
            continue
        caption = f"The frame of {what} its {finding.kind} finding shows ({clock(finding.at)} into its MP4): {finding.message}"
        if finding.data.get("jumped"):
            caption = (
                f"The two frames of {what} its {finding.kind} finding shows, the insert's region at 100%, the frame before "
                f"on the left: {finding.message}"
            )
        row = await assets.store_path(
            path,
            filename=f"{base}-{finding.kind}-{index}{Path(path).suffix.lower() or '.jpg'}",
            user_id=user_id,
            source=assets.SOURCE_RENDER,
            description=caption[:1000],
        )
        finding.data["frame_file_id"] = str(row.id)
        stored.append(row.id)
    return stored


#: The frame each watch finding cut from the encoded MP4 shows: its kind, and the data key of its second.
_WATCH_PROOF_AT = {
    render_checks.BRIGHTNESS_DIP: "minimum_at",
    render_checks.DOUBLE_IMAGE: "frame_at",
    render_checks.STATIC_FLICKER: "frame_at",
    render_checks.NEAR_EMPTY: "frame_at",
}


async def _dip_proofs(final_path: str, findings: list, workdir: Path) -> list:
    """``(finding, frame on disk)`` for each ``brightness_dip`` (its darkest frame) and ``double_image``
    finding (its strongest blend) of *findings*, cut from the encoded *final_path*; a frame that cannot be cut
    leaves its finding without one."""
    shown = []
    for index, finding in enumerate(f for f in findings if f.kind in _WATCH_PROOF_AT):
        target = str(workdir / f"{finding.kind.replace('_', '-')}-{index}.jpg")
        try:
            at = float(finding.data.get(_WATCH_PROOF_AT[finding.kind], finding.at))
            await encoder.extract_frame(final_path, at=at, out_path=target)
        except encoder.EncoderError as exc:
            logger.warning("%s frame of %s not cut: %s", finding.kind, final_path, format_exception_message(exc))
            target = None
        shown.append((finding, target))
    return shown


async def _logo_duplicates(
    spec, document: dict, media: _Media, workdir: Path, *, span: tuple[float, float] | None
) -> list:
    """The ``logo_duplicated`` and ``logo_not_integrated`` findings of the AI shots
    the render's scenes play, each with its frame on disk.

    The frame is the one the take's check kept, downloaded to be stored again
    as a file of the render (``_store_proofs``): a render's findings never
    name a file the take owns.
    """
    shots = {scene.id or "": shot for scene in spec.scenes if (shot := _footage_shot(scene)) is not None}
    offset = span[0] if span else 0.0
    found = [
        *logos.duplicate_findings(document, shots, media.logo_checks, offset=offset, span=span),
        *logos.integration_findings(document, shots, media.logo_checks, offset=offset, span=span),
    ]
    shown = []
    for index, (finding, file_id) in enumerate(found):
        path = None
        if file_id:
            target = str(workdir / f"{finding.kind.replace('_', '-')}-{index}.jpg")
            path = target if await assets.download_file(file_id, target) is not None else None
        shown.append((finding, path))
    return shown


#: The moments of a footage scene's own span the ``duplicate_logo`` check reads: shares of the span.
DUPLICATE_LOGO_SAMPLES = (0.15, 0.5, 0.85)
#: The image keys of the drawn brand marks (``_gather_media``): the logo the cards draw and the mark the watermark does.
_BRAND_IMAGE_KEYS = {"logo": "logo", "mark": "logo_mark"}


def _brand_overlays(scene: dict) -> list[tuple[float, float, str]]:
    """``(t0, t1, asset)``, in the scene's own time, of each image overlay of a planned *scene* drawing a brand mark
    of the template (``brand`` in its payload, ``timeline._overlay_payload``: ``logo`` or ``mark``): a card with no
    footage draws the brand there, which the watermark doubles as footage showing the logo does."""
    found = []
    for overlay in scene.get("overlays") or []:
        if isinstance(overlay, dict) and overlay.get("type") == "image" and overlay.get("brand"):
            t0 = float(overlay.get("t0") or 0.0)
            found.append((t0, max(t0, float(overlay.get("t1") or t0)), str(overlay["brand"])))
    return found


def _brand_overlay_windows(scene: dict) -> list[tuple[float, float]]:
    """``(t0, t1)``, in the scene's own time, of each brand image overlay of *scene* (:func:`_brand_overlays`)."""
    return [(t0, t1) for t0, t1, _asset in _brand_overlays(scene)]


def _brand_overlay_on_screen(scene: dict, at: float) -> str | None:
    """The brand asset (``logo`` or ``mark``) of the first brand image overlay of a planned *scene* on screen at *at*,
    a moment of the video; ``None`` when none is: a sample outside every overlay's window is not the overlay's."""
    start = float(scene.get("start") or 0.0)
    for t0, t1, asset in _brand_overlays(scene):
        if start + t0 <= at <= start + t1:
            return asset
    return None


def _duplicate_logo_moments(document: dict, index: int) -> list[float]:
    """The moments in the video the ``duplicate_logo`` check reads scene *index* of a planned *document* at: the
    :data:`DUPLICATE_LOGO_SAMPLES` of the span it owns, and the middle of each brand image overlay it shows,
    kept inside that span - an empty list for a scene that plays no footage and draws no brand overlay."""
    from app.services.video_studio.spec import SCREEN_INSERT_SCENES

    scene = (document.get("scenes") or [])[index]
    kind = scene.get("type")
    footage = kind in SCREEN_INSERT_SCENES and not (kind == "statement" and not scene.get("backdrop"))
    overlays = _brand_overlay_windows(scene)
    if not footage and not overlays:
        return []
    first, last = timeline.owned_span(document, index)
    moments = [first + share * (last - first) for share in DUPLICATE_LOGO_SAMPLES]
    start = float(scene.get("start") or 0.0)
    moments.extend(min(max(start + (t0 + t1) / 2, first), last) for t0, t1 in overlays)
    return moments


def _is_svg(path: str) -> bool:
    """Whether the file at *path* is an SVG: by its name, else by an ``<svg`` in its first 1 KB (a template's file is
    stored under its own name, not always with the extension)."""
    if path.lower().endswith((".svg", ".svgz")):
        return True
    try:
        with open(path, "rb") as source:
            head = source.read(1024)
    except OSError:
        return False
    return b"<svg" in head.lower()


async def _brand_mark_pictures(images: dict[str, str]) -> tuple[dict, list[str]]:
    """The template's drawn brand marks as RGBA pictures (``role -> picture``) and a note per mark that could not be
    read.

    A mark stored as an SVG (a template's logo often is) is drawn by the bounded rasteriser first
    (``screen_insert.svg_to_png``): PIL never opens an SVG. A mark the rasteriser draws nothing of, or a file PIL
    cannot read, is left out with a note naming it; the check runs on the others.
    """
    from PIL import Image

    from app.services.video_studio.screen_insert import SvgNotDrawnError, svg_to_png

    marks: dict = {}
    notes: list[str] = []
    for key, role in _BRAND_IMAGE_KEYS.items():
        path = images.get(key)
        if not path:
            continue
        name = Path(path).name
        try:
            if _is_svg(path):
                path = await svg_to_png(path, f"The template's {key}")
            with Image.open(path) as picture:
                marks[role] = picture.convert("RGBA")
        except SvgNotDrawnError as exc:
            notes.append(
                f"The duplicate logo check skipped the template's {key} ({name}): its SVG was not drawn ({exc})."
            )
        except (OSError, ValueError) as exc:
            notes.append(
                f"The duplicate logo check skipped the template's {key} ({name}): it could not be read "
                f"({format_exception_message(exc)[:160]})."
            )
    return marks, notes


#: The ready whole renders of a revision read for a picture gate a later release added
#: (``render_checks.settle_new_gates``), newest first.
EARLIER_RENDERS_READ = 5


@dataclass(frozen=True)
class DuplicateLogoPlace:
    """Where the duplicate logo pass over one encoded video keeps what it judged (issue #9404), like the
    frozen-picture watch's ``frame_freeze.Checkpoint``: read back only by a pass over the same video (*key*: what its
    frames were drawn from) reading the same frames, which goes on from the first frame not judged. *stored* is called
    with :attr:`path` after each save: the render keeps it beside itself, so an attempt on another worker - a deploy
    replaced this one and its disk - goes on from there too. Best effort."""

    path: Path
    key: str
    stored: Callable[[Path], Awaitable[None]] | None = None

    def load(self, key: str) -> dict | None:
        """The state kept for *key* (:meth:`save`), ``None`` for none, another pass's or unreadable."""
        try:
            kept = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        if not isinstance(kept, dict) or kept.get("key") != key:
            return None
        try:
            int(kept["next"])
            for name in ("said", "seen_mark", "read_marked", "footage_said", "notes", "shown"):
                if not isinstance(kept[name], list):
                    return None
            if any(not isinstance(item, list) or len(item) != 3 for item in kept["shown"]):
                return None
        except (KeyError, TypeError, ValueError):
            return None
        return kept

    def save(self, key: str, state: dict) -> bool:
        """Write *state* whole, then over the last one: a process stopped mid-write leaves the place before."""
        partial = self.path.with_name(self.path.name + ".part")
        try:
            partial.write_text(json.dumps({"key": key, **state}, default=str), encoding="utf-8")
            os.replace(partial, self.path)
            return True
        except (OSError, TypeError, ValueError) as exc:
            logger.warning("Duplicate logo pass: its place not kept in %s: %s", self.path, exc)
            return False

    async def store(self) -> None:
        """Hand the place just kept to :attr:`stored`, bounded: a slow or failing store costs that copy only."""
        if self.stored is None or not self.path.is_file():
            return
        try:
            await asyncio.wait_for(self.stored(self.path), DUPLICATE_LOGO_PLACE_STORE_SECONDS)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # best effort: the next place goes again
            logger.info(
                "Duplicate logo pass: its place not stored beside the render: %s", format_exception_message(exc)
            )


def duplicate_logo_key(video_key: str, frames: list[int]) -> str:
    """The key a duplicate logo place is kept under: the video's (*video_key*) and the footage frames it reads."""
    said = json.dumps([video_key, [int(frame) for frame in frames]])
    return hashlib.sha256(said.encode("utf-8")).hexdigest()


async def _duplicate_logos(
    spec,
    document: dict,
    media: _Media,
    final_path: str,
    workdir: Path,
    *,
    fps: float,
    span: tuple[float, float] | None,
    scene_id: str | None,
    notes: list[str] | None = None,
    where: ReadbackWhere | None = None,
    place: DuplicateLogoPlace | None = None,
) -> list:
    """The ``duplicate_logo`` finding of each footage scene whose drawn frames show the brand's mark twice, each
    with its frame on disk, the two places outlined. *where* counts the samples it reads (its stage line).

    With *place*, what the pass judged is kept every :data:`DUPLICATE_LOGO_PLACE_FRAMES` frames or
    :data:`DUPLICATE_LOGO_PLACE_SECONDS` (issue #9404): a pass over the same video, reading the same frames, goes on
    from the first frame not judged - a relaunch past the draw never reads the footage frames again from the first.

    Every scene playing footage (``spec.SCREEN_INSERT_SCENES``; a statement over a backdrop) is read on
    :data:`DUPLICATE_LOGO_SAMPLES` of the span it owns, off the encoded MP4: the template's logo and mark matched over
    the whole frame (``logos.brand_marks_on``), and on a watermarked scene the corner watermark where and at the size
    the page draws it (``logos.watermark_on``: about 2 % of the width, under what the whole-frame search
    reaches). The render and a scene preview read the same frames; a scene outside a preview's span is not its own. A
    scene drawing a brand image overlay (a statement card with ``{asset: logo}``) is read too, inside the
    overlay's window: the second mark is the brand's whatever draws it, at every quality, forced or not. A
    mark that could not be read, and a watermarked scene whose watermark was not seen, are lines of *notes*. A place over a
    region the scene's AI shot draws back from its start picture (``keep_regions``) counts only when the template's mark
    is matched inside it at the frame's own resolution, and every other place only when the mark is matched again on its
    own box (``logos.confirmed_places``): the picture's own lettering, a banner or a plain background is not the
    brand's; each place cleared so is a line of *notes*. A scene the page draws no brand mark over (watermark off, no
    brand image overlay on screen, not a brand card: ``logos.page_draws_brand``) gets no finding: its footage's marks
    are a fact, said once in *notes*. The finding names the file and score of each place.
    """
    from PIL import Image

    marks, unread = await _brand_mark_pictures(media.assets.images)
    mark_files = {
        role: Path(path).name for key, role in _BRAND_IMAGE_KEYS.items() if (path := media.assets.images.get(key))
    }
    kept_by_shot: dict[str, list[list[float]]] = getattr(media, "kept_regions", None) or {}
    if notes is not None:
        notes.extend(unread)
    if not marks:
        return []
    offset = span[0] if span else 0.0
    shots = {scene.id or "": shot for scene in spec.scenes if (shot := _footage_shot(scene)) is not None}
    wanted: dict[int, tuple[dict, float]] = {}
    for index, scene in enumerate(document.get("scenes") or []):
        if scene_id and scene.get("id") != scene_id:
            continue
        for at in _duplicate_logo_moments(document, index):
            if span is not None and not span[0] <= at <= span[0] + span[1]:
                continue
            wanted.setdefault(max(0, round((at - offset) * fps)), (scene, at - offset))
    if not wanted:
        return []
    shown, said, seen_mark, read_marked, footage_said = [], set(), set(), set(), set()
    loop_notes: list[str] = []
    key = duplicate_logo_key(place.key, sorted(wanted)) if place is not None else ""
    kept = place.load(key) if place is not None else None
    first = 0
    #: The footage frame each finding of *shown* was read on: what a kept place names it by.
    shown_at: list[int] = []
    redraw: dict[int, list[int]] = {}
    if kept is not None:
        first = int(kept["next"])
        said, seen_mark = set(kept["said"]), set(kept["seen_mark"])
        read_marked, footage_said = set(kept["read_marked"]), set(kept["footage_said"])
        loop_notes = list(kept["notes"])
        found = render_checks.Checks.from_dict({"findings": [item[0] for item in kept["shown"]]}).findings
        for finding, (_item, proof, number) in zip(found, kept["shown"], strict=True):
            if not Path(proof).is_file():
                # Kept by a worker gone with its disk: the frame is read again, its places outlined again here.
                proof = str(workdir / f"duplicate-logo-{int(number)}.jpg")
                redraw.setdefault(int(number), []).append(len(shown))
            shown.append((finding, proof))
            shown_at.append(int(number))
        logger.info("Duplicate logo pass of %s resumed at frame %d, %d finding(s) kept", final_path, first, len(shown))
    todo = [number for number in sorted(wanted) if number >= first]
    frames_dir = workdir / "duplicate-logo"
    frames_dir.mkdir(exist_ok=True)
    try:
        pictures = (
            await encoder.extract_frames(final_path, sorted({*todo, *redraw}), fps=fps, out_dir=str(frames_dir))
            if todo or redraw
            else {}
        )
    except encoder.EncoderError as exc:
        logger.warning("Duplicate logo frames of %s not read: %s", final_path, format_exception_message(exc))
        return []
    for number, indexes in redraw.items():
        if number not in pictures:
            continue
        with Image.open(pictures[number]) as picture:
            frame = picture.convert("RGB")
        for index in indexes:
            finding, proof = shown[index]
            Path(proof).write_bytes(logos.boxed_frame_jpeg(frame, [tuple(box) for box in finding.data["boxes"]]))
    if redraw:
        # A finding whose frame could not be read again keeps no proof: it is said once the pass ends, left out.
        held = [(pair, at) for pair, at in zip(shown, shown_at, strict=True) if Path(pair[1]).is_file()]
        shown, shown_at = [pair for pair, _at in held], [at for _pair, at in held]
    corner = str(document.get("watermark") or "none")
    drawn_mark = marks.get("logo_mark") or marks.get("logo")
    design_height = float(document.get("height") or 1080)
    judged = len(wanted) - len(todo)
    if where is not None:
        # The frames are extracted: the step's clock keeps running from its start, the samples now count.
        where.sample, where.samples = judged, len(wanted)
    last_kept, kept_at = judged, time.monotonic()

    async def keep(next_frame: int) -> None:
        if place is None:
            return
        state = {
            "next": next_frame,
            "said": sorted(said, key=str),
            "seen_mark": sorted(seen_mark, key=str),
            "read_marked": sorted(read_marked, key=str),
            "footage_said": sorted(footage_said, key=str),
            "notes": loop_notes,
            "shown": [[finding.as_dict(), proof, at] for (finding, proof), at in zip(shown, shown_at, strict=True)],
        }
        if place.save(key, state):
            await place.store()

    for number in todo:
        if number not in pictures:
            continue
        if place is not None and (
            judged - last_kept >= DUPLICATE_LOGO_PLACE_FRAMES
            or (judged > last_kept and time.monotonic() - kept_at >= DUPLICATE_LOGO_PLACE_SECONDS)
        ):
            await keep(number)
            last_kept, kept_at = judged, time.monotonic()
        judged += 1
        if where is not None:
            where.sample = (where.sample or 0) + 1
        scene, at = wanted[number]
        if scene.get("id") in said:
            continue
        with Image.open(pictures[number]) as picture:
            frame = picture.convert("RGB")
        places = await asyncio.to_thread(logos.brand_marks_on, frame, marks)
        watermarked = logos.scene_watermarked(document, scene)
        if watermarked and drawn_mark is not None:
            read_marked.add(scene.get("id"))
            mark_place = await asyncio.to_thread(
                logos.watermark_on, frame, drawn_mark, corner, design_height=design_height
            )
            if mark_place is not None:
                seen_mark.add(scene.get("id"))
            places = logos.with_watermark(places, mark_place)
        shot = shots.get(scene.get("id") or "")
        regions = kept_by_shot.get(shot or "")
        if len(places) > 1:
            places, cleared = await asyncio.to_thread(logos.confirmed_places, frame, marks, places, regions)
            loop_notes.extend(
                f"The duplicate logo check left out a place of scene {scene.get('id')}"
                + (" over a region its AI shot keeps from its start picture" if over_kept else "")
                + f": the template's {role} matched {score:.2f} on the whole frame, {inside:.2f} on the place "
                "itself at full size ("
                + ("the picture's own lettering" if over_kept else "no brand mark there")
                + ", not the brand's mark)."
                for role, _box, score, inside, over_kept in cleared
            )
        overlay = _brand_overlay_on_screen(scene, at + offset)
        if len(places) > 1 and not logos.page_draws_brand(document, scene, overlay=overlay):
            # Nothing the page composites carries the brand: every place is the footage's own (its start picture, its
            # kept regions, a logo worn by several subjects), a fact of the shot, never a duplicate the platform drew.
            if scene.get("id") not in footage_said:
                footage_said.add(scene.get("id"))
                loop_notes.append(
                    f"Scene {scene.get('id')} shows the brand's mark in {len(places)} places of its footage ("
                    + ", ".join(f"{mark_files.get(role) or role} {score:.2f}" for role, _box, score in places)
                    + "), with the watermark off and no brand overlay on screen: the footage's own marks, not a duplicate."
                )
            continue
        finding = logos.duplicate_logo_finding(
            scene,
            places,
            at=at,
            watermarked=watermarked,
            clip_id=shot,
            overlay=overlay,
            regions=regions,
            assets=mark_files,
        )
        if finding is None:
            continue
        said.add(scene.get("id"))
        # Named by its frame (one finding a frame at most): a resumed pass never writes over a kept one's proof.
        target = workdir / f"duplicate-logo-{number}.jpg"
        target.write_bytes(logos.boxed_frame_jpeg(frame, [tuple(box) for box in finding.data["boxes"]]))
        shown.append((finding, str(target)))
        shown_at.append(number)
    # The whole pass judged: a relaunch before the render ends reads none of its frames again.
    await keep(max(wanted) + 1)
    if notes is not None:
        notes.extend(loop_notes)
        notes.extend(
            f"The duplicate logo check did not see the watermark in the {corner} corner of scene {sid}: only the logos "
            "in its picture were compared."
            for sid in sorted(read_marked - seen_mark - said, key=str)
        )
    return shown


#: The failing picture findings :func:`_alike_frames` compares at most, worst first by order of the report.
ALIKE_FINDINGS_READ = 16


async def _alike_frames(
    checks: render_checks.Checks, earlier: list[dict], final_path: str, workdir: Path, *, notes: list[str] | None = None
) -> dict[int, dict]:
    """``index of a finding of checks.findings -> earlier render`` for each failing picture finding whose frame, read
    off this render's MP4 at its worst moment (``data.worst_at``, else its start), is the same drawing under encoder
    noise (``render_checks.frames_alike``) as the frame of a ready render of the same revision and quality at that
    moment, that render having passed its picture gates (dev:render-picture-check-gate-drift-2). Every MP4 is read on
    the frames asked only; a render whose MP4 is gone or cannot be read is skipped, said in *notes*. Best effort: a
    finding not compared keeps its block."""
    from PIL import Image

    wanted = [
        (index, finding)
        for index, finding in enumerate(checks.findings)
        if finding.severity == render_checks.FAIL
        and finding.kind in render_checks.PICTURE_KINDS
        # A flicker reads across frames: one frame alike to a render the gate passed before it existed clears nothing.
        and finding.kind != render_checks.STATIC_FLICKER
    ][:ALIKE_FINDINGS_READ]
    candidates = [render for render in earlier if render.get("file_id") and render_checks.passed_pictures(render)]
    if not wanted or not candidates:
        return {}
    folder = workdir / "alike-frames"
    folder.mkdir(exist_ok=True)

    async def frame(path: str, at: float, name: str) -> Any | None:
        target = folder / name
        try:
            await encoder.extract_frame(path, at=at, out_path=str(target))
            with Image.open(target) as picture:
                return picture.convert("RGB")
        except (encoder.EncoderError, OSError) as exc:
            logger.warning("Frame at %.3f s of %s not read: %s", at, path, format_exception_message(exc))
            return None

    def moment(finding: render_checks.Finding) -> float:
        worst = finding.data.get("worst_at")
        return float(worst) if isinstance(worst, (int, float)) else float(finding.at)

    ours = {index: await frame(final_path, moment(finding), f"ours-{index}.png") for index, finding in wanted}
    alike: dict[int, dict] = {}
    for render in candidates:
        left = [index for index, _finding in wanted if index not in alike and ours.get(index) is not None]
        if not left:
            break
        mp4 = folder / f"earlier-{str(render['id'])[:8]}.mp4"
        try:
            row = await assets.download_file(render["file_id"], str(mp4))
        except Exception as exc:
            logger.warning("MP4 of render %s not read: %s", render["id"], format_exception_message(exc))
            row = None
        if row is None:
            if notes is not None:
                notes.append(f"Render {str(render['id'])[:8]}'s MP4 could not be read: its frames were not compared.")
            continue
        try:
            for index in left:
                theirs = await frame(
                    str(mp4), moment(checks.findings[index]), f"earlier-{str(render['id'])[:8]}-{index}.png"
                )
                if theirs is not None and render_checks.frames_alike(ours[index], theirs):
                    alike[index] = render
        finally:
            mp4.unlink(missing_ok=True)
    return alike


async def _store_crops(reports: list[dict], *, base: str, user_id, what: str, size: tuple[int, int]) -> list:
    """The 100 % crops of each insert's region (``insert_checks.reports``), stored as files of the requester.

    Each crop's ``path`` becomes its ``file_id`` on the report; a crop whose file is gone is dropped from it. So do
    the crop of the side where most of a residual key sits (``residual_crop``), that of the worst stretch of a
    halo (``halo_crop``), that of the insert drawn over what stands in front (``foreground_crop``), that
    of the worst stretch of a grey rim (``rim_crop``) and that of the source seen through what stands in front
    (``through_crop``): ``render_checks.INSERT_STRIPS``.
    """
    stored = []
    for report in reports:
        kept = []
        for crop in report.get("crops") or []:
            path = crop.pop("path", None)
            if not path or not Path(path).is_file():
                continue
            caption = (
                f"The {crop.get('word')} frame of {report.get('label') or 'a screen insert'} in {what}, {clock(crop.get('at'))} "
                f"into its MP4: the insert region cut from the frame as drawn, at 100% of the {size[0]}x{size[1]} render - "
                "the real pixels, for the compositing to be judged on"
            )
            row = await assets.store_path(
                path,
                filename=f"{base}-{_slug(str(report.get('key') or 'insert'))}-{crop.get('word')}.png",
                user_id=user_id,
                source=assets.SOURCE_RENDER,
                content_type="image/png",
                description=caption[:1000],
            )
            crop["file_id"] = str(row.id)
            kept.append(crop)
            stored.append(row.id)
        report["crops"] = kept
        label = report.get("label") or "a screen insert"
        for name in render_checks.INSERT_STRIPS:
            strip = report.get(name)
            if not isinstance(strip, dict):
                continue
            path = strip.pop("path", None)
            if not path or not Path(path).is_file():
                report.pop(name)
                continue
            if name == "halo_crop":
                caption = (
                    f"The worst stretch of the halo of {label} in {what}, {clock(strip.get('at'))} into its MP4: the edge of "
                    f"what stands in front, unmarked, at 100% of the {size[0]}x{size[1]} render - the key's hue left on it "
                    "after the edge despill"
                )
                filename = f"{base}-{_slug(str(report.get('key') or 'insert'))}-halo.png"
            elif name == "foreground_crop":
                caption = (
                    f"The worst stretch of {label} drawn over what stands in front of its surface in {what}, "
                    f"{clock(strip.get('at'))} into its MP4: unmarked, at 100% of the {size[0]}x{size[1]} render"
                )
                filename = f"{base}-{_slug(str(report.get('key') or 'insert'))}-over-foreground.png"
            elif name == "rim_crop":
                caption = (
                    f"The worst stretch of the grey rim of {label} in {what}, {clock(strip.get('at'))} into its MP4: the "
                    f"band past what stands in front, unmarked, at 100% of the {size[0]}x{size[1]} render - grey where the "
                    "insert is not"
                )
                filename = f"{base}-{_slug(str(report.get('key') or 'insert'))}-rim.png"
            elif name == "through_crop":
                caption = (
                    f"The worst stretch of {label}'s source seen through what stands in front of its surface in {what}, "
                    f"{clock(strip.get('at'))} into its MP4: unmarked, at 100% of the {size[0]}x{size[1]} render - the "
                    "source's own strokes drawn across it"
                )
                filename = f"{base}-{_slug(str(report.get('key') or 'insert'))}-through-foreground.png"
            else:
                caption = (
                    f"The {strip.get('side')} side of {label} in {what}, {clock(strip.get('at'))} "
                    f"into its MP4, where most of its residual key sits: the margin past that edge and as deep inside it, "
                    f"unmarked, at 100% of the {size[0]}x{size[1]} render - the footage's own key colour past the bezel "
                    "told from the key left on the surface"
                )
                filename = f"{base}-{_slug(str(report.get('key') or 'insert'))}-residual-{strip.get('side')}.png"
            row = await assets.store_path(
                path,
                filename=filename,
                user_id=user_id,
                source=assets.SOURCE_RENDER,
                content_type="image/png",
                description=caption[:1000],
            )
            strip["file_id"] = str(row.id)
            stored.append(row.id)
    return stored


async def _surface_lost(
    render_id: str, error: Exception, document: dict, media: _Media, *, user_id, label: str
) -> None:
    """End a render whose tracked surface was lost: failed, a ``surface_lost`` finding carrying the frame."""
    finding = insert_checks.surface_lost(error, document)
    data = getattr(error, "frame_png", None)
    stored = None
    if data:
        stored = await assets.store_bytes(
            data,
            filename=f"{_slug(label)}-{render_id[:8]}-surface-lost.jpg",
            user_id=user_id,
            source=assets.SOURCE_RENDER,
            content_type="image/jpeg",
            description=f'The footage frame where the render of "{label}" lost a tracked surface: {error}'[:1000],
        )
        finding.data["frame_file_id"] = str(stored.id)
    checks = render_checks.Checks(findings=[finding])
    ended = await jobs.mark_if_open(
        "render",
        render_id,
        status=JOB_FAILED,
        error=f"{error}"[:2000],
        checks=checks.as_dict(),
        notes=media.notes[:40] or None,
        finished_at=_now(),
        stage=None,
    )
    if stored is not None and not ended:
        await assets.delete_files([stored.id])


def settled_frames(
    times: tuple[float, float, float, float], frame_range: tuple[int, int], fps: int, count: int
) -> tuple[int, int, int]:
    """The first, middle and last settled frames of a previewed scene, as frames of its composed MP4.

    *times* are the scene's in its window's plan (:func:`window_times`),
    *frame_range* the frames of that plan the MP4 was drawn from, *count* the
    frames the MP4 holds, as probed. The first is 0.05 s after its transition
    in, the last the last one before its transition out - the MP4's own last
    when nothing follows the scene in its window (an outro, a card cut into
    the next scene) - and each is a frame the MP4 has.
    """
    _start, _end, settled_from, settled_to = times
    top = max(0, count - 1)
    first = min(max(math.ceil((settled_from + 0.05) * fps) - frame_range[0], 0), top)
    last = min(max(math.ceil(settled_to * fps) - 1 - frame_range[0], first), top)
    return first, (first + last) // 2, last


async def _scene_frames(
    final_path: str,
    times: tuple[float, float, float, float],
    fps: int,
    workdir: Path,
    base: str,
    user_id,
    *,
    frame_range: tuple[int, int],
    count: int,
    label: str = "",
    max_side: int = 1280,
    what: str = "the preview of",
) -> list:
    """The first, middle and last frames of a previewed scene, stored as files of the requester.

    Picked among the frames its MP4 holds (:func:`settled_frames`), each cut
    from a quarter of a frame before its own time: ffmpeg keeps the first
    frame at or after the time it is given, and the exact time of the MP4's
    last frame, rounded up to the millisecond, is past it - no frame decoded,
    the preview failed. Each says what it is (*label*: which scene of
    which video), so a second read in a turn answers with it. At
    *max_side* at most: the render's own size for a scene whose pixels are
    checked, never a downscaled still.
    """
    rate = max(fps, 1)
    stored = []
    for (name, word), frame in zip(
        (("first", "first"), ("mid", "middle"), ("last", "last")),
        settled_frames(times, frame_range, rate, count),
        strict=True,
    ):
        out = workdir / f"scene-{name}.jpg"
        await encoder.extract_still(
            final_path, at=max(0.0, (frame - 0.25) / rate), out_path=str(out), max_side=max_side
        )
        caption = f"The {word} settled frame ({frame / rate:.1f} s into its MP4) of {what} {label or 'a scene'}."
        stored.append(
            await assets.store_path(
                str(out),
                filename=f"{base}-{name}.jpg",
                user_id=user_id,
                source=assets.SOURCE_RENDER,
                description=caption,
            )
        )
    return stored


def composited_scenes(document: dict, spec: Any, screens_replaced: dict[str, dict]) -> list[tuple[int, str]]:
    """``(index, scene id)`` of the planned *document*'s scenes that composite a real screen: a screen insert,
    or an AI shot with a replaced screen - the scenes whose check frames are kept at the render's own size."""
    found: list[tuple[int, str]] = []
    shots = {scene.id or "": shot for scene in spec.scenes if (shot := _footage_shot(scene)) is not None}
    for index, scene in enumerate(document.get("scenes") or []):
        scene_id = str(scene.get("id") or "")
        if scene.get("inserts") or shots.get(scene_id) in screens_replaced:
            found.append((index, scene_id))
    return found


async def _check_frames(
    final_path: str,
    document: dict,
    scenes: list[tuple[int, str]],
    *,
    fps: int,
    size: tuple[int, int],
    count: int,
    workdir: Path,
    base: str,
    user_id,
    title: str,
) -> list:
    """The first, middle and last settled frames of each of *scenes* in a whole render's MP4, at its native *size*.

    A 720p preview hides what a 4K render shows of a composited screen: these
    are the frames a reviewer reads, stored as files of the render
    (``frame_file_ids``, deleted with it). Best effort: a frame that cannot be
    cut leaves the render without it, logged.
    """
    stored: list = []
    for index, scene_id in scenes:
        try:
            times = window_times(document, index)
            stored.extend(
                await _scene_frames(
                    final_path,
                    times,
                    fps,
                    workdir,
                    f"{base}-{scene_id}",
                    user_id,
                    frame_range=(0, int(document["frames"])),
                    count=count,
                    label=f'scene {scene_id} of the video "{title}" at its native {size[0]}x{size[1]} (its composited screen)',
                    max_side=max(size),
                    what="the render of",
                )
            )
        except Exception as exc:  # advisory: the render stands without this scene's check frames
            logger.warning(
                "Video render: check frames of scene %s not cut: %s", scene_id, format_exception_message(exc)
            )
    return stored


async def finish_made(
    render_id: str,
    stored: list,
    made: dict[str, Any],
    *,
    whole: bool,
    checks: render_checks.Checks,
    checks_failed: bool = False,
    scenes_drawn: dict | None = None,
    scenes_content: dict | None = None,
    scenes_joined: dict | None = None,
    played: dict | None = None,
    scenes_parts: dict | None = None,
    fps: float | None = None,
) -> bool:
    """End a render with what it *made*, under its row lock; ``False`` when it was cancelled meanwhile.

    A render replaced while it saves ends replaced or finished, never both:
    a cancelled row keeps its reason and loses the files it
    just *stored*. It ends ready, or failed when *checks_failed* (its
    checks refuse it, its files kept to look at). A *whole* render that
    passed becomes what the video plays (a scene preview never does), a
    render that passed records the scenes it drew, and a scene preview that
    ended ready replaces the previous previews of its scene.
    *played*: what a whole render records beside what it draws - each
    narration's take, each scene playing a fallback take and what its frames
    were drawn from (``voice_recheck.played``, ``encoded_as``). *scenes_parts*:
    the fingerprint of each part of each scene drawn
    (``render_checks.SceneDigests.parts``), what a later refusal names. *fps*: the frame rate it was drawn at,
    recorded on each scene's drawing - what a later drawing at another rate that disagrees with it says.
    """
    replaced = render_retention.Deletion()
    async with async_session() as db:
        row = await db.get(VideoRender, uuid.UUID(render_id), with_for_update=True)
        if row is None or row.status == JOB_CANCELLED:
            await db.rollback()
            await assets.delete_files(stored)
            return False
        for key, value in made.items():
            setattr(row, key, value)
        if played and isinstance(row.drawing, dict):
            row.drawing = {**row.drawing, **played}
        row.status = JOB_FAILED if checks_failed else JOB_READY
        row.error = render_checks.failure_summary(checks) if checks_failed else None
        row.checks = checks.as_dict()
        row.progress = 100
        row.stage = None
        row.finished_at = _now()
        # Written before its video is indexed again, which reads what its ready renders drew.
        await db.flush()
        # Locked: two renders ending together both record the scenes they drew.
        project = await db.get(VideoProject, row.project_id, with_for_update=True)
        if project is not None:
            if row.status == JOB_READY:
                retention.mark_for_reindex(project)
            # A scene preview is a check of one scene, a failed render a draft to look at: the video keeps
            # playing its current render.
            if whole and not checks_failed:
                project.latest_render_id = row.id
            if not checks_failed:
                project.scene_previews = render_checks.record_seen(
                    project.scene_previews,
                    scenes_drawn or {},
                    row.id,
                    checks=row.checks,
                    content=scenes_content,
                    revision=row.revision,
                    stage="render" if whole else "preview",
                    joined=scenes_joined,
                    parts=scenes_parts,
                    fps=fps,
                )
            await retention.touch(db, project.spec or {})
            await db.flush()
            replaced = await render_retention.replaced_previews(db, row)
        await db.commit()
    await render_retention.purge(replaced)
    return True


async def ranges_here(document: dict, *, quality: str, size: tuple[int, int]) -> int:
    """The scene ranges a whole render of *document* is drawn in on this worker: 1, drawn whole.

    The pre-flight's estimate made again here, on this worker's CPUs and memory ceiling, its frames now known: past
    the 4 h a render may run, or past ``VIDEO_STUDIO_RENDER_SPLIT_MINUTES`` on the pages its memory opens with other
    workers there, ranges each on a worker of its own (``render_preflight.ranges_for``).
    """
    from app.services.video_studio import render_preflight
    from app.services.video_studio.render_process import memory_ceiling

    inserts = render_preflight.InsertLoad.of(document)
    kinds = render_preflight.FrameKinds.of(document, quality=quality)
    room = render_preflight.launch_ceiling(memory_ceiling(quality))
    drawn = render_preflight.layout(
        size, x264_preset=QUALITY_PRESETS[quality].x264_preset, ceiling=room, inserts=inserts
    )
    try:
        async with async_session() as db:
            rates = await render_preflight.measured_rates(
                db, quality, contended_pages=render_preflight.contended(drawn.pages, drawn.cpus)
            )
    except Exception as exc:  # the CI rate then
        logger.info("Video render rates not read for its ranges: %s", format_exception_message(exc))
        rates = None
    said = render_preflight.estimate(
        frames=int(document["frames"]),
        size=size,
        quality=quality,
        rate=rates,
        ceiling=room,
        scenes=len(document.get("scenes") or []),
        inserts=inserts,
        kinds=kinds,
        insert_seconds=sum(b - a for a, b in kinds.inserts),
        workers=await render_preflight.workers_present(),
    )
    return said.ranges


async def _draw_in_ranges(
    render_id: str,
    document: dict,
    media: _Media,
    *,
    count: int,
    origins: dict[str, object],
    preset: Any,
    size: tuple[int, int],
    workdir: Path,
    layout: Any,
    progress: Callable[..., Any],
    cancelled: Callable[[], Any],
    on_frame: Callable[[], None] | None,
    report: Reporter | None,
    audits: dict,
    crops: dict[str, set[int]] | None = None,
    drawn_for: dict | None = None,
    frame_costs: Any = None,
    guard: dict[int, list[str]] | None = None,
    presence: dict[int, list[str]] | None = None,
) -> tuple[str, dict] | None:
    """Draw *document* in *count* scene ranges, each on a worker of its own: ``(silent video, what it drew)``.

    ``None`` when it cannot be split (one scene, drawing inputs another worker cannot read): drawn whole then. What
    it drew is this process's own frames and seconds, what the render records it measured, with the segments its
    own ranges kept from an attempt before (``segments_reused``). *crops* are the frames whose insert regions are
    kept as 100 % crops, each range taking its own; *guard* the frames the guard reads once encoded,
    *presence* those the presence gate reads; *drawn_for* what each segment it closes records.
    """
    import json

    from app.services.builtin_tools.html_render import font_face_css
    from app.services.video_studio import render_preflight, render_ranges

    planned = render_ranges.plan_ranges(document, count)
    if len(planned) < 2:
        return None
    # Drawn here from the very document the ranges read back from the bundle: the same segments' digests.
    document = json.loads(json.dumps(document, default=str))
    try:
        bundle = await render_ranges.publish(
            render_id, document=document, assets=media.assets, origins=origins, quality=preset.name, size=size
        )
    except render_ranges.BundleError as exc:
        logger.warning("Video render %s: drawn whole, its ranges cannot be handed out: %s", render_id, exc)
        return None
    rows = await render_ranges.open_ranges(render_id, bundle.digest, planned, quality=preset.name)
    total = max(1, int(document["frames"]))
    # ``reused``: its own ranges' frames kept from an earlier attempt; ``joined``: the frames other workers drew.
    drawn: dict[str, Any] = {"done": 0, "frames": 0, "seconds": 0.0, "kept": 0, "reused": 0, "joined": 0}
    own: list[compositor.DrawProgress] = []
    logger.info(
        "Video render %s: drawn in %d scene ranges (%s)",
        render_id,
        len(rows),
        ", ".join(f"{row.first_frame}-{row.end_frame}" for row in rows),
    )

    async def draw(row: Any, mine: bool) -> str:
        part_tracker = compositor.DrawProgress()
        part_audits: dict = {}
        base = drawn["done"]
        what = "Drawing" if mine else "Joining"

        async def part_progress(percent: int) -> None:
            await progress(
                12 + int(76 * (base + part_tracker.done) / total),
                f"{what} scene range {row.part + 1} of {len(rows)} ({percent}%)",
                drawing=part_tracker.drawing(document),
            )

        began = time.monotonic()
        video = await compositor.render_frames(
            document,
            media.assets,
            preset=preset,
            output_size=size,
            workdir=str(workdir / f"range-{row.part}"),
            font_css=font_face_css(),
            pages=layout.pages,
            progress=part_progress,
            cancelled=cancelled,
            tracker=part_tracker,
            on_frame=on_frame,
            frame_range=(row.first_frame, row.end_frame),
            audits=part_audits,
            render_id=render_id,
            crops=crops,
            drawn_for=drawn_for,
            frame_costs=frame_costs,
            guard=guard,
            presence=presence,
        )
        render_ranges.merge_audits(audits, part_audits)
        # Beside the others under a name of its own: the join lists its parts by name.
        joined = workdir / f"range-{row.part:03d}.mp4"
        Path(video).replace(joined)
        drawn["done"] += row.end_frame - row.first_frame
        if mine:
            frames = part_tracker.done - part_tracker.kept
            seconds = time.monotonic() - began
            drawn["frames"] += frames
            drawn["seconds"] += seconds
            measured = render_preflight.measurement(
                layout,
                frames=frames,
                seconds=seconds,
                frames_reused=part_tracker.kept,
                segments_reused=render_preflight.segments_reused(part_tracker),
            )
            await render_ranges.finish(row.id, by=render_ranges.RENDER_OWNER, measured=measured)
            drawn["reused"] += part_tracker.kept
            own.append(part_tracker)
        else:
            drawn["joined"] += part_tracker.done
        drawn["kept"] += part_tracker.kept if mine else part_tracker.done
        return str(joined)

    async def said(frames: int, stage: str) -> None:
        await progress(12 + int(76 * frames / total), stage[:200])

    videos = await render_ranges.draw_all(
        render_id,
        rows,
        draw=draw,
        report=said,
        cancelled=cancelled,
        heartbeat=lambda: _emit(report, {"type": "heartbeat"}),
        wall_seconds=render_preflight.wall_seconds(preset.name),
    )
    video_path = str(workdir / "video.mp4")
    await encoder.concat(videos, out_path=video_path, workdir=str(workdir))
    drawn["segments_reused"] = render_preflight.segments_reused(*own)
    drawn["insert_frames"] = compositor.DrawProgress(
        insert_cached=sum(part.insert_cached for part in own), insert_drawn=sum(part.insert_drawn for part in own)
    ).insert_frames()
    return video_path, drawn


async def _spliced_frames(source: voice_recheck.Source | None, encoded_as: str, workdir: Path) -> str | None:
    """The MP4 of the ready render a splice re-checks takes into (``voice_recheck.Source``), downloaded, when
    its frames were drawn from exactly what this render's would be (``encoded_as``, ``compositor.drawing_digest``):
    ``encoder.mux`` copies its picture as it is under this render's sound. ``None`` when they were not, or it is gone."""
    if source is None or not source.file_id or source.encoded_as != encoded_as:
        return None
    path = str(workdir / "spliced-source.mp4")
    return path if await assets.download_file(source.file_id, path) is not None else None


def kept_resume_line(resumed: dict, fps: int) -> str:
    """``Resumed from kept frames of render `id` at 80.0 s: ...`` (``render_segments.adopt_kept``)."""
    at = render_segments.resumed_seconds(resumed["frames"], fps)
    return (
        f"Resumed from kept frames of render `{resumed['render']}` at {at:.1f} s: {resumed['segments']} segments its "
        "checks failed after drawing were kept, never drawn again."
    )


async def _kept_frames_taken(render_id: str) -> None:
    """The failed render whose kept frames a new render took no longer says it keeps them. Best effort."""
    try:
        async with async_session() as db:
            row = await db.get(VideoRender, uuid.UUID(render_id), with_for_update=True)
            kept_key = render_segments.FRAMES_KEPT_UNTIL
            if row is not None and isinstance(row.drawing, dict) and kept_key in row.drawing:
                row.drawing = {key: value for key, value in row.drawing.items() if key != kept_key}
                await db.commit()
    except Exception as exc:
        logger.info(
            "Video render %s: its kept frames taken, its record not updated: %s",
            render_id,
            format_exception_message(exc),
        )


def inherited_line(inherited: list[dict]) -> str:
    """``Kept from render `id` (revision 99), which this render replaced: 1,800 frames of the scenes this revision
    leaves unchanged (s01 600, s02 1,200) ...`` (``compositor.DrawProgress.inherited``); empty for none."""
    by_render: dict[tuple[str, int], dict[str, int]] = {}
    for run in inherited:
        scenes = by_render.setdefault((str(run["render"]), int(run["revision"])), {})
        for scene, count in (run.get("scenes") or {}).items():
            scenes[scene] = scenes.get(scene, 0) + int(count)
    said = []
    for (previous, revision), scenes in by_render.items():
        listed = ", ".join(f"{scene} {count:,}" for scene, count in scenes.items())
        said.append(
            f"Kept from render `{previous}` (revision {revision}), which this render replaced: {sum(scenes.values()):,} "
            f"frames of the scenes this revision leaves unchanged ({listed}), never drawn again"
        )
    return "; ".join(said) + "." if said else ""


def drawing_of(render: Any, revision: int, marks: render_checks.SceneDigests, sources: str) -> dict:
    """What this attempt of the whole render *render* draws, read from revision *revision*
    (``VideoRender.drawing``): each scene's drawn digest and its music's sources, what a save of a later revision
    carries it on (``projects.carry_renders``), keeping the revision it was first asked for; and what it plays beside
    its frames (``SceneDigests.heard``), what a start of a later revision finds it covers it by
    (``projects.covering_render``), and the release that draws it (``render_checks.release_drawing``). And
    what a voice change alone moves (``SceneDigests.said``, ``beside``, ``picture``): what a re-check of a
    scene playing a fallback take is offered by (``voice_recheck.offered``); and the splice it is, kept."""
    earlier = getattr(render, "drawing", None)
    earlier = earlier if isinstance(earlier, dict) else {}
    drawing = {
        "revision": revision,
        "scenes": marks.drawn,
        "whole": marks.whole,
        "sources": sources,
        "heard": marks.heard,
        "said": marks.said,
        "beside": marks.beside,
        "picture": marks.picture,
        "release": render_checks.release_drawing(),
    }
    if earlier.get("carried_from") and earlier.get("revision") == revision:
        drawing["carried_from"] = earlier["carried_from"]
    if isinstance(earlier.get(voice_recheck.SPLICE), dict):
        drawing[voice_recheck.SPLICE] = earlier[voice_recheck.SPLICE]
    return drawing


async def _carried_sound(render_id: str, revision: int) -> dict | None:
    """The later revision this whole render was carried to while it drew (``projects.carry_renders``):
    ``{revision, spec, whole, content, carried_from}`` - the spec it mixes that revision's sound from -, ``None`` when
    it is still the render of the *revision* its attempt read."""
    async with async_session() as db:
        row = await db.get(VideoRender, uuid.UUID(render_id))
    drawing = row.drawing if row is not None and isinstance(row.drawing, dict) else None
    if row is None or drawing is None or row.revision == revision:
        return None
    if drawing.get("revision") != row.revision or not isinstance(drawing.get("spec"), dict):
        return None
    return {**drawing, "revision": row.revision}


def _sound_of(
    carried: dict, spec: VideoSpec, plan: timeline.TimelinePlan, media: _Media, *, fps: int, theme: dict
) -> tuple:
    """``(spec, plan)`` whose sound is the *carried* revision's: its clips' own sound, the voice-over's level
    and the music as it sets them, planned on the media gathered - the same scenes, drawn the same."""
    import dataclasses

    later = parse_video_spec(carried["spec"])
    if timeline.auto_quads(later):
        later = timeline.with_quads(later, {}, stand_in=True)
    replanned = timeline.plan(later, media.plan, fps=fps, theme=theme)
    heard = spec.model_copy(update={"music": later.music, "voiceover": later.voiceover, "clip_sound": later.clip_sound})
    return heard, dataclasses.replace(plan, clip_audio=replanned.clip_audio, clip_sound=replanned.clip_sound)


#: How often a whole render writes the pace its frames measure (:class:`PaceNote`), once it measures one.
PACE_EVERY_SECONDS = 30.0
#: How often a whole render drawing at a measured pace moves its deadline out to the end that pace gives (#11782).
ETA_EXTEND_EVERY_SECONDS = 300.0


class PaceNote:
    """Keeps the pace a whole render's frames measured on its ``preflight`` (``render_pace.paced``): once
    ``render_pace.PACE_SHARE`` of its frames are drawn, then every :data:`PACE_EVERY_SECONDS` - what replaces its
    pre-flight's estimate in the render card and in ``get_video_jobs`` (``render_pace.eta_of``). With a *watch*
    (``render_pace.PaceWatch``), what explains a pace under half the pre-flight's too, as ``slow``."""

    def __init__(self, render_id: str, expected: dict[str, Any] | None, *, most_seconds: float | None = None) -> None:
        self.render_id = render_id
        self.expected = dict(expected or {})
        self.written: float | None = None
        #: With *most_seconds*, a render still drawing steadily has its deadline moved out to the end its pace gives,
        #: never past *most_seconds* after the one it was queued with (#11782).
        self.most_seconds = most_seconds
        self.extended: float | None = None

    async def note(self, tracker: compositor.DrawProgress, watch: Any = None) -> None:

        pace = tracker.pace()
        slow = watch.breakdown() if watch is not None else None
        now = time.monotonic()
        if (
            self.most_seconds
            and pace is not None
            and pace.per_second
            and (self.extended is None or now - self.extended >= ETA_EXTEND_EVERY_SECONDS)
        ):
            # Steady: its frames move at a measured pace. Its deadline follows the end that pace gives.
            from app.services.video_studio import render_ranges

            self.extended = now
            after = float(self.expected.get("check_seconds") or 0.0)
            await render_ranges.extend_to_eta(pace.seconds_left + after, most_seconds=self.most_seconds)
        if (pace is None and slow is None) or (self.written is not None and now - self.written < PACE_EVERY_SECONDS):
            return
        self.written = now
        kept = render_pace.paced(self.expected, pace) if pace is not None else dict(self.expected)
        if slow is not None:
            kept["slow"] = slow
        try:
            await jobs.mark("render", self.render_id, preflight=kept)
        except Exception as exc:  # what the card says of its end, never a reason to stop drawing
            logger.info("Video render %s: its pace was not kept: %s", self.render_id, format_exception_message(exc))


async def _frame_costs(
    video_id: str | None, *, preset: Any, size: tuple[int, int], layout: Any, document: dict, spec: Any
) -> Any:
    """What a frame of each kind costs one of *layout*'s pages at *preset*, as its pre-flight prices it
    (``render_preflight.page_costs``): its insert frames at what the previous render of *video_id* measured, the
    rest at the platform's median, each by its scene's type; ``None`` when no history can be read - the compositor
    prices them by default."""
    from app.services.video_studio import render_preflight

    try:
        async with async_session() as db:
            rates = await render_preflight.rates_of(
                db,
                preset.name,
                contended_pages=render_preflight.contended(layout.pages, layout.cpus),
                video_id=video_id,
            )
    except Exception as exc:  # the frames are shared out all the same, at default prices
        logger.info("Video render: what its frames cost was not read: %s", format_exception_message(exc))
        return None
    kinds = render_preflight.FrameKinds.of(document, quality=preset.name, classes=render_preflight.scene_classes(spec))
    return render_preflight.page_costs(
        rates or render_preflight.Rates(render_preflight.DEFAULT_PAGE_RATE),
        size=size,
        quality=preset.name,
        pages=layout.pages,
        cpus=layout.cpus,
        samples=kinds.samples,
        frame_kinds=kinds,
    )


async def run_render(
    render_id: str,
    *,
    cancel_event: asyncio.Event | None = None,
    report: Reporter | None = None,
    workdir: Path | None = None,
    on_frame: Callable[[], None] | None = None,
) -> JobOutcome:
    origins: dict[str, object] = {}
    token = _ORIGINS.set(origins)
    try:
        return await _run_render(
            render_id, cancel_event=cancel_event, report=report, workdir=workdir, on_frame=on_frame, origins=origins
        )
    finally:
        _ORIGINS.reset(token)


def _cpu_since(began: float | None) -> float | None:
    """The CPU seconds this process tree used since it used *began* (``resources.tree_cpu_seconds``); ``None`` unread."""
    now = resources.tree_cpu_seconds(os.getpid())
    return max(0.0, now - began) if now is not None and began is not None else None


async def _run_render(
    render_id: str,
    *,
    cancel_event: asyncio.Event | None,
    report: Reporter | None,
    workdir: Path | None,
    on_frame: Callable[[], None] | None,
    origins: dict[str, object],
) -> JobOutcome:
    from app.services.builtin_tools.html_render import font_face_css

    async with async_session() as db:
        # Locked: a newer render of the video cancelling this one is
        # read here, never written over by ``running``.
        render = await db.get(VideoRender, uuid.UUID(render_id), with_for_update=True)
        if render is None or render.status in JOB_TERMINAL_STATUSES:
            return JobOutcome("done")
        project = await db.get(VideoProject, render.project_id)
        user = await db.get(User, render.user_id) if render.user_id else None
        if project is None or user is None:
            render.status = JOB_FAILED
            render.error = "The project or the person who asked for the render no longer exists."
            render.finished_at = _now()
            await db.commit()
            return JobOutcome("done")
        await bind_llm_caller(db, user.id)
        from app.services.video_studio.projects import effective_spec
        from app.services.video_studio.spec import VideoSpecError

        try:
            applied = await effective_spec(db, project, user)
        except VideoSpecError as exc:
            render.status = JOB_FAILED
            render.error = f"The video no longer validates with its template: {exc}"[:2000]
            render.finished_at = _now()
            await db.commit()
            return JobOutcome("done")
        template_id = applied.template.id if applied.template is not None else None
        scene_id, source, target = render.scene_id, applied.spec, None
        if scene_id:
            from app.services.video_studio.projects import estimate

            try:
                source, target = scene_window(applied.spec, scene_id, (await estimate(db, applied.spec)).document)
            except LookupError:
                render.status = JOB_FAILED
                render.error = f"Scene `{scene_id}` is no longer in the video: preview one it has."
                render.finished_at = _now()
                await db.commit()
                return JobOutcome("done")
        render.revision = revision = project.revision
        video_id = str(project.id)
        # A render resumed in place after a stall or taken back from a lost lease is the same one: it
        # keeps when it started.
        render.started_at = render.started_at if render.status == JOB_RUNNING and render.started_at else _now()
        render.progress_at = _now()
        render.status = JOB_RUNNING
        # A whole render taken over by another worker never reports less than its attempts before stood at.
        floor = 0 if scene_id else resumed_progress(getattr(render, "relaunches", None))
        render.progress = max(1, floor)
        render.stage = "Gathering the media"
        quality = render.quality
        # Each attempt measures its own pace: what an earlier one measured no longer says when this one ends.
        from app.services.video_studio import render_preflight

        render.preflight = render_pace.unpaced(render.preflight)
        expected = dict(render.preflight) if isinstance(render.preflight, dict) else {}
        # Forced past its quality gate: its failed narrations are placed, its findings kept, never fatal.
        forced = bool(render.gate_override)
        # A splice of re-checked takes into a ready render: the frames and the other takes it keeps, read once.
        splice = (
            voice_recheck.Splice.of(render.drawing.get(voice_recheck.SPLICE))
            if isinstance(render.drawing, dict)
            else None
        )
        splice = splice if not scene_id else None
        spliced_from = None
        if splice is not None and (source_id := safe_uuid(splice.source)) is not None:
            spliced_from = voice_recheck.Source.of(await db.get(VideoRender, source_id), project.id, quality)
        # The scenes the render draws, as they are now: recorded on the video once it is ready (the gate's "seen").
        from app.services.video_studio import lexicon_store

        whole = parse_video_spec(applied.spec)
        lexicon = await lexicon_store.for_voiceover(db, whole.voiceover)
        marks = await render_checks.digest_scenes(db, applied.spec, lexicon)
        scenes_drawn, scenes_content, scenes_parts = marks.whole, marks.content, dict(marks.parts)
        # Every scene as it is now, a preview's neighbours too: what its reading of an insert on a join is kept with.
        scenes_joined = dict(marks.whole)
        # The video's drawings read as the digests are hashed now: an earlier release's of a scene as it is now.
        previews_now = render_checks.adopted(project.scene_previews, marks)
        predecessors: list[tuple[str, int]] = []
        earlier_renders: list[dict] = []
        kept_candidates: list[str] = []
        if not scene_id:
            # What it draws, written under its row's lock: a save of a later revision that draws the same
            # carries it, never one that read it before this attempt did.
            render.drawing = drawing_of(render, revision, marks, render_checks.sound_sources(applied.spec))
            # The renders it replaced at its quality: the frames they drew of what it draws are taken.
            replaced = await db.execute(
                select(VideoRender.id, VideoRender.revision)
                .where(
                    VideoRender.superseded_by_id == render.id,
                    VideoRender.scene_id.is_(None),
                    VideoRender.quality == render.quality,
                )
                .order_by(VideoRender.created_at)
            )
            predecessors = [(str(previous), int(drawn_for)) for previous, drawn_for in replaced.all()]
            # The whole renders of this video at this quality its checks failed within the day: the frames one
            # of them keeps, drawn from what this render draws, are resumed from (``render_segments.adopt_kept``).
            failed_renders = await db.execute(
                select(VideoRender.id)
                .where(
                    VideoRender.project_id == project.id,
                    VideoRender.id != render.id,
                    VideoRender.quality == render.quality,
                    VideoRender.scene_id.is_(None),
                    VideoRender.status == JOB_FAILED,
                    VideoRender.finished_at >= _now() - timedelta(seconds=render_segments.RENDER_FAILED_KEEP_SECONDS),
                )
                .order_by(VideoRender.finished_at.desc())
                .limit(EARLIER_RENDERS_READ)
            )
            kept_candidates = [str(previous) for (previous,) in failed_renders.all()]
            # The ready whole renders of this revision at this quality (dev:render-picture-check-gate-drift): a picture
            # gate a later release added never silently fails frames one of them, drawn alike, passed.
            passed_renders = await db.execute(
                select(
                    VideoRender.id,
                    VideoRender.finished_at,
                    VideoRender.checks,
                    VideoRender.drawing,
                    VideoRender.file_id,
                )
                .where(
                    VideoRender.project_id == project.id,
                    VideoRender.id != render.id,
                    VideoRender.revision == revision,
                    VideoRender.quality == render.quality,
                    VideoRender.scene_id.is_(None),
                    VideoRender.status == JOB_READY,
                )
                .order_by(VideoRender.finished_at.desc())
                .limit(EARLIER_RENDERS_READ)
            )
            earlier_renders = [
                {
                    "id": str(rid),
                    "finished_at": done.isoformat() if done else None,
                    "checks": stored,
                    "drawing": drawn,
                    "file_id": str(fid) if fid else None,
                }
                for rid, done, stored, drawn, fid in passed_renders.all()
            ]
        await db.commit()
        # What each scene's own drawing, as it is now, reported of its picture: a preview never fails on
        # what a neighbour drawn beside it shows.
        reported = render_checks.reported_pictures(previews_now, marks.whole)
        # What nothing in the spec caused on each scene's drawing as it is now: the harness's defects said there
        # are not said again on the same frames.
        unexplained = render_checks.unexplained_before(previews_now, marks.whole)
        # What the drawings of the scenes as they are now read of each insert on its joins: two drawings of one join
        # that disagree say so, never warn on one of them.
        joins_before = render_checks.join_readings(previews_now, marks.whole)
        # What the video's kept drawings measured of each insert at its key and light: a finding no key
        # or light setting moved between two of them is said so, never as a setting to change.
        residues = render_checks.earlier_measures(project.scene_previews)
        # What measured each insert before on the same shot and spec: a preview of its scene as it is now, the
        # save's own measure (cached by the save and the pre-render lint) and the shot's own lint of the take that
        # plays - a gate one of them passed is a warning here that disagrees with it, never a block.
        passed_before = render_checks.insert_verdicts(previews_now, marks.whole)
        bezels = await insert_bezels.measure(db, whole, user=user)
        for verdicts in (insert_bezels.verdicts(bezels, revision), await insert_bezels.shot_verdicts(db, bezels)):
            for name, records in verdicts.items():
                passed_before.setdefault(name, []).extend(records)
        # What stands in front of each static keyed insert over its clip, as the save read it.
        subjects = insert_bezels.subjects(bezels)
        # Each static insert's surface as the save measured it, on its footage frame: its flatness, never read
        # again on the frames drawn here at this resolution.
        surfaces_measured = insert_bezels.measured_surfaces(bezels)
        made_from = None
        if scene_id:
            scenes_drawn = {scene_id: scenes_drawn[scene_id]} if scene_id in scenes_drawn else {}
            scenes_content = {scene_id: scenes_content[scene_id]} if scene_id in scenes_content else {}
            scenes_parts = {scene_id: scenes_parts[scene_id]} if scene_id in scenes_parts else {}
            # What it is made from: the next preview made from the same is served from this one.
            from app.services.video_studio import kept_previews

            made_from = await kept_previews.inputs_or_none(
                db, applied.spec, applied.template, scene_id=scene_id, quality=quality, user=user
            )
        db.expunge(user)
    spec = parse_video_spec(source)
    # A quad left to the render stands on the whole footage until its first frame is read: gathered and
    # planned like the others, then found (:func:`_find_quads`).
    found_from = spec if timeline.auto_quads(spec) else None
    if found_from is not None:
        spec = timeline.with_quads(spec, {}, stand_in=True)
    # A scene preview speaks each narration with its neighbours in the whole video: the narration, its
    # cache and its check are the whole render's, whatever the window says before or after it.
    lines = voice.lines_of(whole, within={scene.id for scene in spec.scenes}) if scene_id else None
    origin = f"render {render_id[:8]}" + (f" (a preview of scene {scene_id})" if scene_id else "")
    # A scene preview is drawn at one of the preview qualities, never past 1080p30.
    if scene_id:
        preset = QUALITY_PRESETS[quality if quality in PREVIEW_QUALITIES else "draft"]
    else:
        preset = QUALITY_PRESETS.get(quality) or QUALITY_PRESETS["high"]
    fps = spec.format.fps if spec.format.fps and not scene_id else preset.fps
    out_w, out_h = output_size(spec.format.aspect_ratio, preset.short_side)
    tracker = compositor.DrawProgress()

    async def progress(value: int, stage: str, *, drawing: list[str] | None = None) -> None:
        if cancel_event is not None and cancel_event.is_set():
            raise compositor.RenderCancelledError("The render was cancelled.")
        value = max(1, floor, min(99, value))
        _emit(
            report,
            {"type": "progress", "progress": value, "stage": stage, "frames": tracker.done, "drawing": drawing or []},
        )
        shown = f"{stage} - {', '.join(drawing)}" if drawing else stage
        await jobs.mark("render", render_id, progress=value, stage=shown[:200], progress_at=_now())

    async def cancelled() -> bool:
        return (cancel_event is not None and cancel_event.is_set()) or await jobs.is_cancelled("render", render_id)

    frame_done = frame_reporter(on_frame, report)
    owned = workdir is None
    workdir = Path(workdir) if workdir is not None else Path(tempfile.mkdtemp(prefix="xtm-render-"))
    # What this attempt measures of its stages (each stage's seconds): what the next pre-flights estimate from.
    began, figure_seconds, timings = time.monotonic(), 0.0, {}
    # And the CPU its process tree uses: what the next pre-flights count a preview beside a render for.
    cpu_began = resources.tree_cpu_seconds(os.getpid())
    # A splice keeps every narration it does not re-check as the ready render played it.
    keep = (
        {sid: take for sid, take in spliced_from.takes.items() if sid not in splice.scenes}
        if splice is not None and spliced_from is not None
        else None
    )
    try:
        async with _stage(render_id, "media", report, timings):
            media = await _gather_media(
                spec,
                user=user,
                template_id=template_id,
                workdir=workdir,
                progress=progress,
                forced=forced,
                lines=lines,
                origin=origin,
                keep=keep,
            )
        media.notes[:0] = applied.notes
        theme = themes.build_theme(
            mode=spec.theme.mode,
            background_style=spec.theme.background,
            template_palette=media.palette,
            accent=spec.theme.accent,
            accent_2=spec.theme.accent_2,
            background_color=spec.theme.background_color,
            text_color=spec.theme.text_color,
            has_logo="logo" in media.assets.images,
            has_mark="mark" in media.assets.images,
            motion=spec.theme.motion,
        )
        plan = timeline.plan(spec, media.plan, fps=fps, theme=theme)
        if media.figures:
            # A figure is drawn for the length and the narration the plan gives its scene, which do not
            # depend on the figure: planned again with it, the timeline is the same, the figure now a shot.
            figures_began = time.monotonic()
            async with _stage(render_id, "figures", report, timings):
                await _draw_figures(
                    spec,
                    plan,
                    media,
                    user=user,
                    fps=fps,
                    scale=max(1, round(out_w / design_size(spec.format.aspect_ratio)[0])),
                    workdir=workdir,
                    progress=progress,
                    on_frame=frame_done,
                    video_id=video_id,
                    window=window_times(plan.document, target)[:2] if target is not None else None,
                )
            figure_seconds = time.monotonic() - figures_began
            plan = timeline.plan(spec, media.plan, fps=fps, theme=theme)
        # The screen inserts' preparation is a stage of its own: the next pre-flights count it per second of
        # insert on screen.
        async with _stage(render_id, "inserts", report, timings):
            if found_from is not None:
                # The quads found do not move a frame of the timeline: planned again, each insert now on its bezel.
                spec = await _find_quads(found_from, plan.document, media)
                plan = timeline.plan(spec, media.plan, fps=fps, theme=theme)
            try:
                await _track_inserts(
                    spec, plan.document, media, progress, on_frame=frame_done, render_id=None if scene_id else render_id
                )
            except insert_tracking.InsertTrackLostError as exc:
                await _surface_lost(render_id, exc, plan.document, media, user_id=user.id, label=spec.title)
                return JobOutcome("done")
            insert_parts = timeline.insert_sources(spec)
            await _matte_inserts(plan.document, media, (out_w, out_h), insert_parts, subjects)
        # Each file the frames are drawn from named by the file it was downloaded from: what the segments' digest
        # reads of it (``compositor.drawing_inputs``), a file the render made by its content.
        media.assets.origins.update({path: f"file:{file_id}" for path, file_id in origins.items()})
        drawn_for = {"revision": revision, "attempt": f"started {_now().isoformat(timespec='seconds')}"}
        if plan.duration > settings.video_studio_max_video_seconds:
            # Every failure below ends the render only while it runs: one replaced meanwhile stays replaced.
            await jobs.mark_if_open(
                "render",
                render_id,
                status=JOB_FAILED,
                error=f"The video would last {int(plan.duration)} s, past the {settings.video_studio_max_video_seconds} s limit.",
                finished_at=_now(),
            )
            return JobOutcome("done")
        times, frame_range, span = None, None, None
        if target is not None:
            times = window_times(plan.document, target)
            frame_range = (math.floor(times[0] * fps), min(int(plan.document["frames"]), math.ceil(times[1] * fps)))
            # The seconds of the window the preview's MP4 plays: its sound is cut to them in its one encode.
            span = (frame_range[0] / fps, (frame_range[1] - frame_range[0]) / fps)
            media.notes.insert(
                0,
                f"Scene {scene_id} previewed at {preset.name} quality ({out_w}x{out_h}, {fps} fps), from the start of "
                "its transition in to the end of its transition out, planned with the scene before and after it.",
            )
        # A narration that failed every attempt is never placed silently: a whole render stops before it is drawn
        # (a scene preview goes on and says it; a forced render places it). So does one only another voice said:
        # two narrators in one film, which the audience hears.
        failed_voices = [f for f in _voice_findings(media, plan) if f.severity == render_checks.FAIL]
        if failed_voices and times is None and not forced:
            stopped = render_checks.Checks(voice=[s.summary() for s in media.spoken], findings=failed_voices)
            await jobs.mark(
                "render",
                render_id,
                status=JOB_FAILED,
                error=render_checks.failure_summary(stopped, drawn=False),
                checks=stopped.as_dict(),
                notes=(media.notes + plan.notes)[:40] or None,
                finished_at=_now(),
                stage=None,
            )
            return JobOutcome("done")
        stem = await build_stem(plan, media.voice_paths, workdir)
        drawn = (frame_range[1] - frame_range[0]) if frame_range else plan.document["frames"]
        # As many pages as the CPUs and the memory hold, each with the screen inserts it draws at once,
        # laid out in the room its supervisor read on this worker as it launched it - never in
        # what the pre-flight read earlier. A scene preview's ceiling and pages are its own: its
        # admission's, one page once that ceiling stopped it on more, which its answer says.
        from app.services.video_studio import render_preflight
        from app.services.video_studio.render_process import memory_ceiling, preview_memory_ceiling

        drawn_inserts = render_preflight.InsertLoad.of(plan.document)
        # A whole render run again after its memory ceiling: on fewer pages of the worker's own CPUs.
        lighter = None if scene_id else render_preflight.retry_pages()
        layout = render_preflight.layout(
            (out_w, out_h),
            x264_preset=preset.x264_preset,
            ceiling=render_preflight.launch_ceiling(
                preview_memory_ceiling(render_preflight.InsertLoad.of_spec(spec), quality=preset.name)
                if scene_id
                else memory_ceiling(quality)
            ),
            pages=render_preflight.preview_drawn_pages() if scene_id else lighter,
            cpus=resources.cpu_budget() if lighter is not None else None,
            inserts=drawn_inserts,
        )
        _emit(
            report,
            render_preflight.layout_event(
                layout, (out_w, out_h), x264_preset=preset.x264_preset, inserts=drawn_inserts
            ),
        )
        if scene_id and (one_page := render_preflight.preview_one_page_note()):
            media.notes.insert(0, one_page)
        if lighter is not None and (said := render_preflight.retry_pages_note()):
            media.notes.insert(0, said)
        if scene_id and (where := render_preflight.preview_admission_note()):
            media.notes.insert(0, where)
        logger.info(
            "Video render %s: %d frames on %d of %d pages (%d CPUs)",
            render_id,
            drawn,
            layout.pages,
            layout.wanted,
            layout.cpus,
        )
        await progress(12, f"Drawing {drawn} frames on {layout.pages} pages")
        # What a frame of each kind costs a page here before this render measured its own: the previous
        # render of this video's insert frames, the platform's median for the rest.
        frame_costs = await _frame_costs(
            video_id, preset=preset, size=(out_w, out_h), layout=layout, document=plan.document, spec=spec
        )
        # A whole render's pace replaces its pre-flight's estimate once measured; a preview has none to replace.
        pacer = (
            None
            if scene_id
            else PaceNote(render_id, expected, most_seconds=float(render_preflight.wall_seconds(preset.name)))
        )
        # And a pace under half its pre-flight's says why, in its status line and in get_video_jobs.
        watch = (
            None
            if scene_id
            else render_pace.PaceWatch(expected, pages=layout.pages, cpus=layout.cpus, pressure=cpu_lanes.own_pressure)
        )

        resumed_said = False

        async def frame_progress(percent: int) -> None:
            nonlocal resumed_said
            if not resumed_said and not scene_id and tracker.total:
                # The frame a relaunched render's drawing resumed from: what the attempts before kept.
                resumed_said = True
                from app.services.video_studio.render_process import record_resumed_from

                await record_resumed_from(render_id, tracker.kept)
            stage = tracker.status(percent)
            if watch is not None:
                await watch.observe(tracker)
                stage = watch.line(stage)
            await progress(12 + int(percent * 0.76), stage, drawing=tracker.drawing(plan.document))
            if pacer is not None:
                await pacer.note(tracker, watch)

        # Composing and encoding are one stage: each frame is piped into its
        # page's encoder as soon as it is drawn.
        audits: dict = {}
        frames_began = time.monotonic()
        # Past the 4 h a render may run on this worker: in scene ranges, each on a worker of its own.
        count = (
            1 if frame_range is not None else await ranges_here(plan.document, quality=preset.name, size=(out_w, out_h))
        )
        split = None
        # The scenes each encoded frame paints: the one table the frame server and every check read.
        owners = FrameOwners.of(plan.document)
        # Where each screen insert is, and the frames whose insert region is kept as a 100 % crop.
        placed = insert_checks.placed_inserts(plan.document, media.assets.inserts, insert_parts, owners)
        crops = insert_checks.crop_frames(placed, fps=fps, frame_range=frame_range)
        # The frames of every join read back once encoded: where the page places each insert on them is kept.
        guard = insert_checks.guard_frames(placed, frame_range=frame_range)
        # Every frame an insert's scene is painted on, read back once encoded: where the page places it is kept.
        presence = insert_checks.presence_frames(placed, frame_range=frame_range)
        # What the frames are drawn from: a resume past the encode keeps the video an attempt before encoded
        # of it, and its frozen-picture watch goes on where that attempt's stopped.
        encoded_as = await asyncio.to_thread(
            compositor.drawing_digest,
            plan.document,
            preset,
            (out_w, out_h),
            frame_range or (0, int(plan.document["frames"])),
            media.assets,
        )
        encoded = await asyncio.to_thread(compositor.encoded_video, workdir, encoded_as)
        # A splice whose frames are drawn from exactly what its ready render's were: that render's MP4, never drawn.
        spliced = (
            await _spliced_frames(spliced_from, encoded_as, workdir)
            if encoded is None and spliced_from is not None and frame_range is None
            else None
        )
        resumed_from = None
        if encoded is None and spliced is None and count == 1 and frame_range is None and kept_candidates:
            # A render of this revision its checks failed keeps its frames: taken as this render's own, the
            # drawing goes on from the first segment missing and the frozen-picture watch from where it stood.
            resumed_from = await render_segments.adopt_kept(
                render_id, kept_candidates, encoded_as, fps=fps, size=(out_w, out_h)
            )
            if resumed_from is not None:
                await _kept_frames_taken(resumed_from["render"])
        async with _stage(render_id, "frames", report, timings):
            if encoded is not None:
                video_path, kept_audits = encoded
                audits.update(kept_audits)
                tracker.total = tracker.done = tracker.kept = drawn
                logger.info("Video render %s: its %d frames kept, encoded by an attempt before", render_id, drawn)
                await progress(88, f"{drawn} frames kept: encoded by an attempt before")
            elif spliced is not None:
                video_path = spliced
                tracker.total = drawn
                logger.info(
                    "Video render %s: its %d frames are render %s's, spliced", render_id, drawn, spliced_from.render_id
                )
                await progress(88, f"{drawn} frames kept: spliced into render {spliced_from.render_id[:8]}")
            elif count > 1:
                split = await _draw_in_ranges(
                    render_id,
                    plan.document,
                    media,
                    count=count,
                    origins=origins,
                    preset=preset,
                    size=(out_w, out_h),
                    workdir=workdir,
                    layout=layout,
                    progress=progress,
                    cancelled=cancelled,
                    on_frame=frame_done,
                    report=report,
                    audits=audits,
                    crops=crops,
                    drawn_for=drawn_for,
                    frame_costs=frame_costs,
                    guard=guard,
                    presence=presence,
                )
            if split is not None:
                video_path, drawn_here = split
                tracker.total = int(plan.document["frames"])
                tracker.done, tracker.kept = drawn_here["done"], drawn_here["kept"]
            elif encoded is None and spliced is None:
                video_path = await compositor.render_frames(
                    plan.document,
                    media.assets,
                    preset=preset,
                    output_size=(out_w, out_h),
                    workdir=str(workdir),
                    font_css=font_face_css(),
                    pages=layout.pages,
                    progress=frame_progress,
                    cancelled=cancelled,
                    tracker=tracker,
                    on_frame=frame_done,
                    frame_range=frame_range,
                    audits=audits,
                    render_id=render_id,
                    crops=crops,
                    drawn_for=drawn_for,
                    # Each frame timed under its scene's type, as the pre-flight reads it off the spec.
                    scene_classes=render_preflight.scene_classes(spec),
                    frame_costs=frame_costs,
                    guard=guard,
                    predecessors=predecessors,
                    presence=presence,
                )
                if tracker.inherited:
                    media.notes.insert(0, inherited_line(tracker.inherited))
                if resumed_from is not None:
                    media.notes.insert(0, kept_resume_line(resumed_from, fps))
            if encoded is None and spliced is None:
                await asyncio.to_thread(compositor.keep_encoded, workdir, encoded_as, video_path, drawn, audits)
        if splice is not None:
            media.notes.insert(
                0,
                voice_recheck.splice_note(
                    splice.source,
                    drawn,
                    spliced is not None,
                    media.spoken,
                    splice.scenes,
                    {line.scene_id: voice.line_speed(line, spec.voiceover) for line in media.lines},
                ),
            )
        frames_stage = frames_seconds = time.monotonic() - frames_began
        if split is not None:
            # What this worker drew, in how long: what the next pre-flights estimate a page's rate from.
            frames_seconds = split[1]["seconds"]
        logger.info(
            "Video render %s: %d frames drawn, at most %d decoders open on one page",
            render_id,
            tracker.done,
            tracker.peak_open_decoders,
        )
        if times is None and (carried := await _carried_sound(render_id, revision)) is not None:
            # Carried while it drew to a later revision that draws the same: its sound is that revision's - a
            # clip's own sound, the levels -, mixed over the frames drawn, and it is that revision's render.
            try:
                spec, plan = _sound_of(carried, spec, plan, media, fps=fps, theme=theme)
                media.notes.insert(
                    0,
                    f"Drawn for revision {revision}; revision {carried['revision']} changes nothing it draws, so it was "
                    "carried to it, its sound mixed for it, instead of being cancelled and drawn again.",
                )
            except Exception as exc:  # its sound stays the one it was drawn with: said, never a failed render
                logger.warning(
                    "Video render %s: the carried revision's sound was not planned: %s",
                    render_id,
                    format_exception_message(exc),
                )
                media.notes.insert(
                    0, f"Carried to revision {carried['revision']} with the sound of revision {revision}."
                )
            revision = int(carried["revision"])
            scenes_drawn = dict(carried.get("whole") or scenes_drawn)
            scenes_content = dict(carried.get("content") or scenes_content)
            scenes_joined = dict(carried.get("whole") or scenes_joined)
            # What the revision it was carried to is made of, never the one it drew: none when not recorded.
            scenes_parts = dict(carried.get("parts") or {})
        await progress(89, "Mixing the sound")
        spoken = tuple(plan.voice_windows(voiced=set(media.voice_paths)))
        audio_path = str(workdir / "audio.m4a")
        async with _stage(render_id, "audio", report, timings):
            clips = []
            for sound in plan.clip_audio:
                path = media.clip_paths.get(sound.scene_id)
                if path is None:
                    continue
                try:
                    loudness = await encoder.measure_loudness(
                        path, start=sound.trim_start, seconds=sound.duration * sound.speed
                    )
                except encoder.EncoderError:
                    loudness = None  # unmeasured: mixed at its own level
                clips.append(
                    encoder.ClipTrack(
                        path,
                        sound.start,
                        sound.trim_start,
                        sound.duration,
                        sound.speed,
                        sound.volume,
                        gain_db=encoder.clip_gain(loudness),
                        fade_in=sound.fade_in,
                        fade_out=sound.fade_out,
                        windows=spoken,
                    )
                )
            music = None
            played_bed = None
            loop_at = None
            bed = music_tracks.Bed.of(spec.music, plan.duration, plan.cues())
            if bed is not None and (bed.kind == "track" or media.music_path):
                await progress(90, "Preparing the music")
                bed_path = str(workdir / "bed.wav")
                try:
                    bed_notes = await music_tracks.make_bed(
                        bed, bed_path, source_path=media.music_path, workdir=workdir
                    )
                    media.notes.extend(bed_notes)
                    windows = plan.duck_windows(voiced=set(media.voice_paths)) if spec.music.ducking else []
                    music = encoder.MusicTrack(
                        bed_path, spec.music.volume, spec.music.duck_db if spec.music.ducking else 0.0, tuple(windows)
                    )
                    played_bed = bed
                    loop_at = await _loop_point(bed, media.music_path)
                except encoder.EncoderError as exc:
                    media.notes.append(f"The music could not be prepared ({format_exception_message(exc)[:200]}).")
            await progress(92, "Mixing the sound")
            # A preview's window is mixed whole, so its voice and music are as planned, then cut to the frames drawn.
            await encoder.mix_audio(
                out_path=audio_path,
                duration=plan.duration,
                voice=encoder.VoiceTrack(stem.path, spec.voiceover.volume) if stem is not None else None,
                clips=clips,
                music=music,
                bitrate=preset.audio_bitrate,
                span=span,
            )
        final_path = str(workdir / "final.mp4")
        async with _stage(render_id, "mux", report, timings):
            await encoder.mux(video_path=video_path, audio_path=audio_path, out_path=final_path)

        async def remix() -> stitch.Stem | None:
            # A narration spoken again (``_retake_drifted``): its stem laid, the sound mixed as above and muxed over
            # the same frames, so the MP4 heard and kept plays the new take.
            async with _stage(render_id, "retake", report, timings):
                await progress(93, "Mixing the sound again with the narration spoken again")
                laid = await build_stem(plan, media.voice_paths, workdir)
                await encoder.mix_audio(
                    out_path=audio_path,
                    duration=plan.duration,
                    voice=encoder.VoiceTrack(laid.path, spec.voiceover.volume) if laid is not None else None,
                    clips=clips,
                    music=music,
                    bitrate=preset.audio_bitrate,
                    span=span,
                )
                await encoder.mux(video_path=video_path, audio_path=audio_path, out_path=final_path)
            return laid

        await progress(93, "Hearing and watching the render")
        async with _stage(render_id, "checks", report, timings):
            checks = await check_render(
                final_path=final_path,
                stem=stem,
                plan=plan,
                media=media,
                size=(out_w, out_h),
                fps=fps,
                workdir=workdir,
                span=span,
                scene_id=scene_id,
                bitrate=preset.audio_bitrate,
                on_frame=frame_done,
                progress=progress,
                music=music,
                voice_volume=spec.voiceover.volume,
                music_closing=music_level.closing_seconds(
                    plan.document, ending=spec.music.ending, fade_out=spec.music.fade_out
                )
                if music is not None
                else 0.0,
                watch_key=encoded_as,
                render_id=render_id,
                owners=owners,
                watch=spliced is None,
                speeds={line.scene_id: voice.line_speed(line, spec.voiceover) for line in media.lines},
                loop_at=loop_at,
                # A scene preview reads its takes' synthesised frames as the whole render's rate shows them.
                render_fps=(spec.format.fps or QUALITY_PRESETS["high"].fps) if scene_id else None,
                scene_takes={
                    scene.id: media.plan.takes[scene.take_id]
                    for scene in spec.scenes
                    if scene.type == "recording" and scene.id and scene.take_id in media.plan.takes
                }
                if scene_id
                else None,
                # A whole render retakes a narration that said another scene's sentence once, before it fails on it.
                remix=remix if times is None and spliced_from is None else None,
            )
        # The frame of each brightness dip at its darkest and of each double image at its strongest blend,
        # stored with its finding (``_store_proofs``).
        dip_proofs = await _dip_proofs(final_path, checks.findings, workdir)
        shown: list = []
        if spliced is not None:
            # Its frames are the ready render's, byte for byte: their picture checks are that render's - what
            # its frozen-picture watch judged, what each screen insert measured (its crops that render's own files).
            if spliced_from.frames:
                checks.frames = dict(spliced_from.frames)
            # Its cut joins are that render's frames too: the verdict is kept, and said to be (issue #8935).
            if spliced_from.cuts:
                checks.cuts = {**spliced_from.cuts, "read_on": f"render {spliced_from.render_id}, kept by the splice"}
            checks.inserts = [dict(report) for report in spliced_from.inserts]
        else:

            async def say_readback(where: ReadbackWhere) -> None:
                # Where the read-back stands and how long is left: the stage line a relaunch past the draw shows too.
                await progress(94, where.stage_words(time.monotonic()))

            async with reading_back(
                frame_done, budget=readback_budget(len(presence)), fps=fps, say=say_readback if placed else None
            ) as where:
                if placed:
                    # Its own stage: a long read-back of the inserts is never taken for the frozen-picture watch.
                    await progress(94, "Reading the screen inserts back off the render")
                # The bezel readings of the joins its findings judge: a neighbour's insert drawn at another size
                # than it was last measured at says that measure, and a reading smaller than the save's that disagrees
                # with it is set aside for it - the platform's, said to the administrator.
                # Each step says where the read-back stands, and its heavy reading runs in a thread: the loop
                # keeps beating, and the read-back's bound names the step it stopped at.
                where.at("bezel readings")
                readings = await asyncio.to_thread(
                    functools.partial(
                        insert_checks.bezel_readings,
                        audits,
                        placed,
                        passed=passed_before,
                        measured=surfaces_measured,
                        scene_id=scene_id,
                    )
                )
                checks.notes.extend(readings.facts)
                checks.infra.extend(readings.infra)
                for line in readings.infra:
                    logger.info("Video render %s: %s", render_id, line)
                # An insert drawn without the bezel its save or a drawing before found: its measurement lost, a
                # block - what it would say of its presence or of frames not drawn is another reading's.
                lost = insert_checks.unmeasured(
                    readings.audits, placed, measured=surfaces_measured, passed=passed_before, frame_range=frame_range
                )
                # What the screen inserts showed: over the foreground, padded, a brand mark lost, its colours
                # shifted, off their bezel, keyed with residue, flickering, soft, haloed - and the ones not drawn
                # at all.
                where.at("compositing findings")
                shown = await asyncio.to_thread(
                    functools.partial(
                        insert_checks.findings,
                        readings.audits,
                        placed,
                        fps=fps,
                        offset=span[0] if span else 0.0,
                        span=span,
                        earlier=residues,
                        passed=passed_before,
                        frame_range=frame_range,
                        measured=surfaces_measured,
                        unmeasured=lost,
                    )
                )
                # A quad that jumps shows it on two frames: both cut from the encoded video, side by side.
                where.at("jitter frames")
                shown = await insert_checks.jitter_proofs(
                    final_path,
                    shown,
                    readings.audits,
                    fps=fps,
                    workdir=workdir,
                    first_frame=frame_range[0] if frame_range else 0,
                )
                checks.findings.extend(finding for finding, _proof in shown)
                # Every join's frames read back off the encoded video: a keyed surface showing its key there fails
                # it, whatever the frames' audits said.
                where.at("join frames (insert dropped)")
                dropped = await insert_checks.dropped(
                    final_path,
                    audits,
                    placed,
                    guard,
                    plan.document,
                    {key: insert.key.key() for key, insert in media.assets.inserts.items() if insert.key is not None},
                    fps=fps,
                    workdir=workdir,
                    first_frame=frame_range[0] if frame_range else 0,
                    offset=span[0] if span else 0.0,
                )
                checks.findings.extend(finding for finding, _proof in dropped)
                shown.extend(dropped)
                # An insert that runs to its scene's end, its scene painted past its painted end or asked partly shown.
                overran = insert_checks.overruns(
                    audits, placed, plan.document, fps=fps, offset=span[0] if span else 0.0
                )
                checks.findings.extend(overran)
                shown.extend((finding, None) for finding in overran)
                # Two inserts back to back on one surface leaving it bare between them, read frame by frame on the plan.
                gaps = insert_checks.insert_gaps(
                    plan.document, placed, fps=fps, offset=span[0] if span else 0.0, span=span
                )
                checks.findings.extend(gaps)
                shown.extend((finding, None) for finding in gaps)
                # Every frame each insert shows whole on, read back off this video's own encoded frames: a flat
                # surface or a picture other than its source there is the insert missing, whatever its layer's audits said.
                where.at("presence frames")
                presence_first = frame_range[0] if frame_range else 0
                presence_read = [presence_first]

                where.last = max(presence) if presence else None
                where.began_frame, where.began_at = presence_first, time.monotonic()

                def presence_resumed(frame: int) -> None:
                    # A place kept by an attempt before: its pace is measured from there, never from frame 0.
                    where.resumed_from = frame
                    where.began_frame, where.began_at = frame, time.monotonic()

                def presence_frame() -> None:
                    # Where the presence gate's decoder stands: the frame, and the inserts drawn on it.
                    if where.resumed_from is not None and presence_read[0] < where.resumed_from:
                        # Frames judged by an attempt before: decoded past, never counted in this attempt's pace.
                        where.began_at = time.monotonic()
                    where.frame = presence_read[0]
                    where.inserts = tuple(presence.get(presence_read[0], ()))
                    where.moved(time.monotonic())
                    presence_read[0] += 1
                    frame_done()

                def presence_source() -> None:
                    # A video insert's source decoded up to the frame it is compared with (issue #9331): the gate
                    # works, never a stall for its watchdog, though the encoded frame has not moved yet.
                    where.moved(time.monotonic())

                presence_place = Path(workdir) / PRESENCE_FILE
                if render_id is not None and await render_segments.restore_check_place(render_id, presence_place):
                    logger.info("Video render %s: the presence gate's place taken back from the store", render_id)
                try:
                    present = await insert_checks.presence(
                        final_path,
                        audits,
                        placed,
                        plan.document,
                        media.assets.inserts,
                        fps=fps,
                        size=(out_w, out_h),
                        workdir=workdir,
                        first_frame=frame_range[0] if frame_range else 0,
                        offset=span[0] if span else 0.0,
                        frame_range=frame_range,
                        on_frame=presence_frame,
                        unmeasured=lost,
                        place=insert_checks.PresencePlace(
                            presence_place,
                            encoded_as,
                            functools.partial(render_segments.keep_check_place, render_id)
                            if render_id is not None
                            else None,
                        ),
                        handing_over=compositor.handing_over,
                        resumed=presence_resumed,
                        on_source=presence_source,
                    )
                except insert_checks.PresenceHandedOverError as exc:
                    # Its worker hands it over mid read-back: the place is kept at the frame reached, the next
                    # attempt goes on from there (``render_process.supervise``, SIGUSR1).
                    raise compositor.RenderHandedOverError(str(exc)) from exc
                where.moved_at = None
                checks.notes.extend(
                    insert_checks.agree_on_joins(present, joins_before, placed, offset=span[0] if span else 0.0)
                )
                checks.findings.extend(finding for finding, _proof in present.findings)
                shown.extend(present.findings)
                # What each insert's compositing measured, with the 100 % crops of its region: the answer's numbers.
                where.at("insert reports")
                checks.inserts = await asyncio.to_thread(
                    functools.partial(
                        insert_checks.reports,
                        readings.audits,
                        placed,
                        fps=fps,
                        offset=span[0] if span else 0.0,
                        presence=present.by_key,
                        measured=surfaces_measured,
                    )
                )
                for report in checks.inserts:
                    if report.get("key") in media.auto_quads:
                        report["auto_quad"] = media.auto_quads[report["key"]]
                # One source of truth: a frame the sweep frames failing a gate is that gate's finding, or an advisory
                # under its tolerance said so; a finding no frame of its coverage fails is a disagreement, said.
                checks.notes.extend(insert_checks.reconcile_coverage(checks.inserts, checks.findings))
                checks.findings.extend(
                    insert_checks.missing(
                        media.missing_inserts,
                        insert_parts,
                        plan.document,
                        offset=span[0] if span else 0.0,
                        span=span,
                    )
                )
                # An AI shot whose composited logo shows twice, or reads as a flat sticker:
                # its take's own check, failing the scene.
                where.at("logo checks of the AI shots")
                duplicated = await _logo_duplicates(spec, plan.document, media, workdir, span=span)
                checks.findings.extend(finding for finding, _proof in duplicated)
                shown.extend(duplicated)
            # The brand's mark twice on a drawn frame: the watermark over footage showing the logo, or two
            # instances of any mark - the render's and the scene preview's frames read alike.
            # A check that cannot run is a note, never a failed render: it beats while it works but stays out of
            # the read-back's bound, whose size follows the presence frames, not the footage scenes it samples.
            logo_say = say_readback if spliced is None else None
            async with reading_back(
                frame_done, budget=None, fps=fps, say=logo_say, title=DUPLICATE_LOGO_STAGE
            ) as logo_where:
                # Its own stage line (sample X of Y, how long it has run): never the presence frames' last ETA left over.
                logo_where.at("footage frames")
                if spliced is None:
                    await progress(94, logo_where.stage_words(time.monotonic()))
                # What the pass judged is kept beside the render: a relaunch goes on from there (issue #9404).
                logo_place = Path(workdir) / DUPLICATE_LOGO_FILE
                if render_id is not None and await render_segments.restore_check_place(render_id, logo_place):
                    logger.info("Video render %s: the duplicate logo pass's place taken back from the store", render_id)
                try:
                    twice = await _duplicate_logos(
                        spec,
                        plan.document,
                        media,
                        final_path,
                        workdir,
                        fps=fps,
                        span=span,
                        scene_id=scene_id,
                        notes=checks.notes,
                        where=logo_where,
                        place=DuplicateLogoPlace(
                            logo_place,
                            f"{encoded_as}|{fps}|{out_w}x{out_h}|{scene_id or ''}",
                            functools.partial(render_segments.keep_check_place, render_id)
                            if render_id is not None
                            else None,
                        ),
                    )
                except (LeaseLostError, compositor.RenderCancelledError):
                    raise
                except Exception as exc:
                    logger.warning(
                        "Duplicate logo check of %s failed: %s",
                        final_path,
                        format_exception_message(exc),
                        exc_info=True,
                    )
                    checks.notes.append(
                        f"The duplicate logo check could not run ({format_exception_message(exc)[:160]})."
                    )
                    twice = []
                checks.findings.extend(finding for finding, _proof in twice)
                shown.extend(twice)
                # A replaced screen's screenshot too small for the screen at this size.
                footage_shots = {
                    scene.id or "": shot for scene in spec.scenes if (shot := _footage_shot(scene)) is not None
                }
                checks.findings.extend(
                    insert_checks.soft_screens(
                        plan.document,
                        footage_shots,
                        media.screens,
                        (out_w, out_h),
                        offset=span[0] if span else 0.0,
                        span=span,
                    )
                )
        # The lettering of each AI shot played whose take kept no text check, read now: an invented_text finding
        # with its crop as its frame, or a facts line - never a silent pass (issue #9021).
        if media.text_reads:
            lettered, read_lines = await text_backfill.render_findings(
                user.id,
                plan.document,
                {scene.id or "": shot for scene in spec.scenes if (shot := _footage_shot(scene)) is not None},
                media.text_reads,
                offset=span[0] if span else 0.0,
                span=span,
                scene_id=scene_id,
            )
            checks.notes.extend(read_lines)
            for index, (finding, crop_id) in enumerate(lettered):
                path = None
                if crop_id:
                    target = str(workdir / f"invented-text-{index}.jpg")
                    path = target if await assets.download_file(crop_id, target) is not None else None
                checks.findings.append(finding)
                shown.append((finding, path))
        shown = dip_proofs + shown
        # A finding nothing in the spec causes is the harness's, never the video's: kept with its frame, said once.
        checks.findings, checks.harness = render_checks.harness_defects(checks.findings, unexplained)
        if not scene_id and earlier_renders:
            # A gate newer than a passed render of the same frames is an advisory, never a silent block - the same
            # frames told by their digests and drawing release, or read off both MP4s (dev:render-picture-check-gate-drift-2).
            alike = await _alike_frames(checks, earlier_renders, final_path, workdir, notes=checks.notes)
            render_checks.settle_new_gates(
                checks, earlier_renders, whole=marks.whole, release=render_checks.release_drawing(), alike=alike
            )
        # A note its own report denies is the checks' defect, never the video's: logged, the verdict left as is.
        for said in insert_checks.contradictions(checks.as_dict()):
            logger.error("Video render %s: its report contradicts itself: %s", render_id, said)
        if scene_id:
            # Its verdict is its own scene's: what a neighbour drawn beside it shows stays on that neighbour.
            left_out = render_checks.settle_neighbours(checks, scene_id, reported)
            shown = [(finding, proof) for finding, proof in shown if finding not in left_out]
        # A whole render its checks failed is kept to look at, never what the video plays (unless forced).
        checks_failed = times is None and checks.failed and not forced
        if forced and checks.failed:
            media.notes.insert(0, "Forced past its quality gate: its findings are kept, the render is not failed.")
        await progress(95, "Saving the video")
        poster_at = timeline.poster_time(plan.document)
        poster_path = str(workdir / "poster.jpg")
        vtt_path = workdir / "captions.vtt"
        # A scene preview's captions are in its frames when burned in; it carries no track or poster of its own.
        cues = plan.document["captions"]["cues"] if times is None else []
        base = f"{_slug(spec.title)}-{render_id[:8]}" + (f"-{scene_id}" if scene_id else "")
        poster = captions = None
        frames: list = []
        async with _stage(render_id, "store", report, timings):
            if times is None:
                await encoder.extract_frame(
                    final_path, at=min(poster_at, max(plan.duration - 0.1, 0)), out_path=poster_path
                )
            if cues:
                vtt_path.write_text(
                    timeline.vtt_from_cues(cues, plan.document["captions"]["position"]), encoding="utf-8"
                )
            info = await encoder.probe(final_path)
            what = f'scene {scene_id} of the video "{spec.title}"'
            video = await assets.store_path(
                final_path,
                filename=f"{base}.mp4",
                user_id=user.id,
                source=assets.SOURCE_RENDER,
                content_type="video/mp4",
                # A video is not a picture: what it is, for a reader who opens it.
                description=(
                    f"Preview of {what}: {info.duration:.1f} s at {info.width}x{info.height}, its transitions in and "
                    "out included. Its first, middle and last frames are stored as pictures beside it."
                    if times is not None
                    else None
                ),
            )
            if times is not None:
                frames = await _scene_frames(
                    final_path,
                    times,
                    fps,
                    workdir,
                    base,
                    user.id,
                    frame_range=frame_range,
                    count=info.frame_count or frame_range[1] - frame_range[0],
                    label=what,
                    # At the preview's own size: the frames a composited screen is read on are never downscaled.
                    max_side=max(out_w, out_h),
                )
            else:
                poster = await assets.store_path(
                    poster_path, filename=f"{base}-poster.jpg", user_id=user.id, source=assets.SOURCE_RENDER
                )
                # The check frames of every scene compositing a real screen, at the render's native size.
                composited = composited_scenes(plan.document, spec, media.screens)
                if composited:
                    frames = await _check_frames(
                        final_path,
                        plan.document,
                        composited,
                        fps=fps,
                        size=(out_w, out_h),
                        count=info.frame_count or int(plan.document["frames"]),
                        workdir=workdir,
                        base=base,
                        user_id=user.id,
                        title=spec.title,
                    )
            if cues:
                captions = await assets.store_path(
                    str(vtt_path),
                    filename=f"{base}.vtt",
                    user_id=user.id,
                    source=assets.SOURCE_RENDER,
                    content_type="text/vtt",
                )
            proofs = await _store_proofs(
                shown, base=base, user_id=user.id, what=what if scene_id else f'the video "{spec.title}"'
            )
            cropped = await _store_crops(
                checks.inserts,
                base=base,
                user_id=user.id,
                what=what if scene_id else f'the video "{spec.title}"',
                size=(out_w, out_h),
            )
        stored = [
            video.id,
            poster.id if poster else None,
            captions.id if captions else None,
            *(f.id for f in frames),
            *proofs,
            *cropped,
        ]
        measured = render_preflight.measurement(
            layout,
            # Drawn frames: under motion blur each frame drew its sub-frames, what the rate is per.
            frames=(tracker.done - tracker.kept) * tracker.samples,
            # Its own frames kept from an earlier attempt, never those its other ranges' workers drew, nor those
            # taken from the render it replaced.
            frames_reused=split[1]["reused"] if split is not None else tracker.kept - tracker.inherited_frames,
            frames_inherited=tracker.inherited_frames if split is None else 0,
            inherited=tracker.inherited if split is None else None,
            seconds=frames_seconds,
            figure_frames=sum(drawing.drawn for drawing in media.drawings.values()),
            figure_frames_reused=sum(drawing.reused for drawing in media.drawings.values()),
            # Which segments those are, who drew them and what of the digest they matched on.
            segments_reused=split[1]["segments_reused"]
            if split is not None
            else render_preflight.segments_reused(tracker),
            figure_seconds=figure_seconds,
            # Never the wait on the workers drawing its other scene ranges: what its other stages took.
            other_seconds=time.monotonic() - began - frames_stage - figure_seconds,
            # Its frames by kind, each stage's seconds, its whole time and its inserts' time on screen -
            # what the next pre-flights weigh each kind, each stage and their own error by.
            kinds=tracker.kinds(),
            # By scene type too: a clip with a screen insert, a statement over a clip, a figure.
            scenes=tracker.scenes(),
            stages={**timings, "frames": frames_seconds},
            total_seconds=time.monotonic() - began,
            insert_seconds=sum(b - a for a, b in render_preflight.FrameKinds.of(plan.document).inserts),
            encoder_threads=tracker.encoder_threads,
            page_runs=tracker.runs(),
            # Its screen-insert frames read back from the worker's disk, and drawn (``insert_frames``).
            insert_frames=split[1].get("insert_frames") if split is not None else tracker.insert_frames(),
            # What explained a pace under half its pre-flight's: what the next pre-flights learn per scene type.
            pace=watch.record() if watch is not None else None,
            # The CPUs it used, and those its pages got beside the jobs sharing its worker.
            cpu_seconds=_cpu_since(cpu_began),
            contention=watch.contention() if watch is not None else None,
            # The reuse index is read for whole renders only: a preview draws a window, at draft size.
            figures=(
                reuse_index.figure_entries(
                    reuse_index.figure_inputs(spec, scenes_drawn, media.voice_digests), media.drawings
                )
                if times is None
                else None
            ),
        )
        if split is not None:
            measured["ranges"] = count
            measured["frames_joined"] = split[1]["joined"]
        if made_from is not None:
            measured.update({"kept_as": made_from.digest, "frames_total": int(info.frame_count or drawn)})
        if spliced is not None:
            # None drawn: its frames are the ready render's, never counted as drawn nor kept by an attempt.
            measured.update({"frames_spliced": int(drawn), "spliced_from": spliced_from.render_id})
        # What a whole render plays beside what it draws: each narration's take, each scene playing a fallback
        # take - said with the fix that clears it - and what its frames were drawn from.
        played = None
        if times is None:
            speeds = {line.scene_id: voice.line_speed(line, spec.voiceover) for line in media.lines}
            played = {
                **await voice_recheck.played(media.spoken, media.voice_paths, media.voice_digests, marks, speeds),
                "encoded_as": encoded_as,
            }
            media.notes.extend(voice_recheck.fallback_notes(played["fallbacks"]))
        kept_until = None
        if checks_failed and played is not None:
            # Failed at a check stage after its drawing: its frames stay for the next render of the revision.
            kept_until = await render_segments.keep_failed(render_id, encoded_as, fps=fps, size=(out_w, out_h))
            if kept_until is not None:
                played[render_segments.FRAMES_KEPT_UNTIL] = kept_until.isoformat()
        made = {
            "width": out_w,
            "height": out_h,
            "fps": fps,
            "duration_ms": int(info.duration * 1000),
            "size_bytes": video.size,
            "file_id": video.id,
            "poster_file_id": poster.id if poster else None,
            "captions_file_id": captions.id if captions else None,
            "captions_burned_in": bool(cues) and bool(plan.document["captions"]["enabled"]),
            "frame_file_ids": [str(f.id) for f in frames] or None,
            # A scene preview is a check of one scene, never what the video plays: it has no chapters.
            "chapters": timeline.chapters(spec, plan.document, media.scene_names) if times is None else None,
            "notes": (media.notes + plan.notes)[:40] or None,
            "music": music_tracks.played_record(played_bed, media.music_file_id),
            # What it drew, kept in its video's media index while it is ready.
            "asset_refs": asset_refs(source),
            "measured": measured,
        }
        ended = await finish_made(
            render_id,
            stored,
            made,
            whole=times is None,
            checks=checks,
            checks_failed=checks_failed,
            scenes_drawn=scenes_drawn,
            scenes_content=scenes_content,
            scenes_joined=scenes_joined,
            played=played,
            scenes_parts=scenes_parts,
            fps=fps,
        )
        if kept_until is not None and not ended:
            # Cancelled while it saved: nothing of it is resumed from.
            with contextlib.suppress(Exception):
                await render_segments.forget(render_id)
        return JobOutcome("done")
    except compositor.RenderCancelledError:
        await jobs.mark_cancelled("render", render_id)
        return JobOutcome("done")
    except compositor.RenderHandedOverError as exc:
        # Its worker hands it over (``render_process.supervise``): the row is the supervisor's to write.
        return JobOutcome("retry", str(exc))
    except ReadbackStalledError as exc:
        # The presence frames stood still under a live heartbeat: this attempt ends with where it stood, the next goes
        # on from the place the gate kept, and the lease is never renewed past the watchdog.
        return JobOutcome("retry", str(exc)[:2000])
    except LeaseLostError as exc:
        return JobOutcome("lease_lost", f"The render's lease is gone: {exc}"[:2000])
    except encoder.EncoderUnavailableError as exc:
        await jobs.mark_if_open("render", render_id, status=JOB_FAILED, error=str(exc), finished_at=_now(), stage=None)
        return JobOutcome("done")
    except RuntimeError as exc:
        # A media problem (a recording not ready, a file gone): the person fixes the project.
        await jobs.mark_if_open(
            "render", render_id, status=JOB_FAILED, error=str(exc)[:2000], finished_at=_now(), stage=None
        )
        return JobOutcome("done")
    except Exception as exc:
        logger.warning("Video render %s failed: %s", render_id, format_exception_message(exc), exc_info=True)
        return JobOutcome(
            "retry",
            f"The render failed: {format_exception_message(exc)}"[:2000],
            cause=render_retry.cause_of_exception(exc),
        )
    finally:
        # A working directory the supervisor gave is its own to keep or remove (``render_process``):
        # a lost lease or a retry resumes there.
        if owned:
            shutil.rmtree(workdir, ignore_errors=True)
