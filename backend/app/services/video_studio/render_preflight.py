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

"""What a whole render will take, said before it starts: its frames x pixels against the worker that draws them.

``render_video(quality=ultra)`` - 3840x2160 at 60 frames a second - had never
been attempted when the brand film of 20 Oct 2026 was produced: nothing said
whether the worker could draw it, nor how long it would take. A render is
estimated from its plan before it is queued (:func:`preflight`, from
``projects.start_render``):

* **On what.** A render draws on pages - one Chromium page each, piping its
  frames into an encoder of its own (``compositor.render_frames``) - as many
  as ``VIDEO_STUDIO_RENDER_PAGES`` says (0, the default: the worker's CPUs
  less one, :func:`auto_pages`), never more than the worker's CPUs nor
  than its memory ceiling holds (:func:`layout`). An x264 encoder at 2160p
  holds its lookahead whatever its threads (1.8 to 2.0 GiB, measured): 4
  pages fit 16 GiB, the other CPUs of an 8-CPU worker left to their
  encoders' threads. The render uses the same layout.
* **How long.** Its frames at the rate one page draws them at this quality,
  times the pages, each page counted for the CPUs it has (:data:`PAGE_CPUS` at
  most): measured on this platform's last renders at that quality, else at
  the others scaled by their pixels (:func:`measured_rates`,
  ``VideoRender.measured``), else on the CI run of
  ``test_video_studio_ultra_render_chromium`` (:data:`DEFAULT_PAGE_RATE`). The
  answer says every input: the frames, the seconds a frame takes on a
  page and where that rate comes from (:attr:`Rates.source`), the pages, the
  CPUs, the duration, the cap and, past it, the CPUs that bring it under
  (:func:`needs`). Only what the render will draw is counted: a figure
  whose drawing an earlier render kept beside the video - at that quality or
  sharper - for the same inputs is played as it is (``reuse_index``), and the
  answer says how many. A video with a finished render at the quality is
  priced at what that render measured (``render_history.OwnRender``):
  every scene type it drew, and its pages at the parallel efficiency they had
  over the whole run; the correction by the last estimates' errors is read on
  the same worker shape (``render_history.Shape``), and once
  ``render_pace.PACE_SHARE`` of the frames are drawn the pace they measured
  replaces the estimate (``render_pace.eta_of``: the pace and the
  history live beside this module). A scene type that render did not draw is priced from the
  platform's measure of its type, and said (:func:`unlearned_words`);
  a pace under half the estimate's is explained (``render_pace``). The
  estimate is priced against its worker's committed load and said beside an
  idle worker's (:func:`with_load`, ``cpu_share``).
* **In scene ranges.** Past :data:`RENDER_DEADLINE_SECONDS` on one
  worker, the render is drawn in scene ranges, each on a worker of its own
  (:func:`ranges_for`, ``render_ranges``): the cap applies per range.
* **Memory.** The pages and their encoders (:func:`memory_needed`), against
  the ceiling the worker holds a render to (``render_process.memory_ceiling``),
  which only the worker can read: it publishes it with its pages and CPUs
  (:func:`publish_worker`).
* **The stall window.** A page draws one frame at a time: one frame longer
  than half the stall window (``VIDEO_STUDIO_RENDER_STALL_SECONDS``) would stop
  the render. The lease is no bound of its own: it is renewed from a
  thread while the render moves, whatever a frame takes.
* **The wall.** A render runs for :data:`RENDER_DEADLINE_SECONDS` at most. An
  ultra render may run up to :data:`ULTRA_WALL_SECONDS`: it closes a segment
  every 10 % of its frames at most, kept beside it, and a new attempt resumes
  from the last one (``compositor.segment_frames``, ``render_segments``).

What would stop the render refuses it (:attr:`Preflight.problems`, a quality
gate reason ``force`` may override) - unless this platform already rendered
that quality whole, which is better evidence than an estimate: then it is
said, never refused. A refusal says, in the same message, what the worker
needs (the CPUs, the memory) and then the highest quality that fits this
worker now (:func:`highest_fitting`), for the person to choose: the video and
its quality are never changed for them.
"""

from __future__ import annotations

import dataclasses
import functools
import itertools
import json
import logging
import math
import os
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

from app.core.config import settings
from app.core.utils import format_exception_message, safe_uuid
from app.services.video_studio import reuse_index
from app.services.video_studio.render_history import (
    ERRORS_READ,
    VIDEO_RENDERS_READ,
    Missed,
    OwnRender,
    Shape,
    estimate_error,
    estimate_errors,
    own_render,
    read_back,
)

if TYPE_CHECKING:
    from app.services.video_studio import page_schedule

logger = logging.getLogger(__name__)

_MIB = 1 << 20
#: What one pixel-frame costs at each preset, relative to ``high`` (x264 ``slow``, JPEG frames): a PNG frame and its
#: decode cost more, a faster x264 preset less.
PIXEL_COST = {"draft": 0.45, "standard": 0.7, "high": 1.0, "ultra": 1.3}
#: The CPUs a page is counted for at most: what was measured. A CPU more may serve its encoder's threads, which
#: no render measured yet - the estimate does not count on it.
PAGE_CPUS = 1
#: Weighted pixel-frames one page with :data:`PAGE_CPUS` CPUs draws and encodes a second, until this platform has
#: measured a render: the slowest ultra run of ``test_video_studio_ultra_render_chromium`` (20 Oct 2026, a 2 vCPU
#: runner, 2 pages) drew 1,788 frames of 3840x2160 in 832 s, 2.15 frames a second.
DEFAULT_PAGE_RATE = 2.15 * 3840 * 2160 * PIXEL_COST["ultra"] / 2
#: How much faster a figure frame draws than a composed one: 2.2 to 2.7 against 1.07 to 1.33 frames a second a
#: page on those runs.
FIGURE_SPEED = 2.0
#: The seconds the other stages take (the media, the sound, storing) for each second of video, until measured: that
#: run's 24 s for its 30 s, less the check stage's (:data:`DEFAULT_CHECK_PER_MEGAPIXEL_FRAME`, 9.5 s of them).
DEFAULT_OTHER_PER_SECOND = 0.5
#: What a frame of each kind (:data:`FRAME_KINDS`) costs a page against one at :data:`DEFAULT_PAGE_RATE` (0.93 s a
#: 2160p frame on a page of that CI run, its encoder's pace), until this platform measured renders by kind.
#: One page drawing the fixture of ``test_video_studio_page_peak_chromium`` at 2160p (21 Oct 2026) took 0.08 s for
#: a plain frame and 0.13 s for one in a transition - the page draws both scenes -, both under x264's pace, and
#: 1.8 s for one with a matted screen insert (its matte, its warp, its audit, its PNG at the host's size), 2.1 s in
#: a transition.
DEFAULT_KIND_COST = {"plain": 1.0, "transition": 1.0, "insert": 2.0, "transition_insert": 2.3}
#: The fewest frames of a kind a render must have drawn for its rate of that kind to count.
_MIN_KIND_FRAMES = 30
#: What a page spends on each segment beside its frames, per million pixels of its frames: its encoder
#: opened, then flushing its lookahead once the frames stop, and the page's own start. One 2160p page of the fixture
#: ran 706 s for frames that took it 682 s, over 6 segments: 4 s a segment, 0.48 s a million pixels.
DEFAULT_SEGMENT_PER_MEGAPIXEL = 0.48
#: The seconds the screen inserts' preparation takes (their quads found, followed, matted) for each second
#: an insert is on screen, until measured.
DEFAULT_INSERT_PER_SECOND = 0.5
#: The seconds the check stage takes (the MP4 watched frame by frame, its sound heard) for each frame of a
#: million pixels, until measured: 30 s of 3840x2160 at 60 fps watched for frozen pictures in 9.5 s (21 Oct 2026).
DEFAULT_CHECK_PER_MEGAPIXEL_FRAME = 0.00064
#: Pages fewer than the worker's CPUs times this are contended (:func:`contended`): each encoder runs on
#: several threads beside a page that draws on one, and a rate measured on pages that filled their worker is not theirs.
CONTENDED_SHARE = 0.5
#: The rate of contended pages against the rate measured on uncontended ones, until this platform measured
#: contended renders (:func:`contended`): the 4K render of 21 Oct 2026, on 3 pages of 8 CPUs, took 47 min
#: for the 30 an estimate from full pages said - 0.64 of the speed counted on, all of it taken as contention until
#: a contended render is measured here.
DEFAULT_CONTENTION = 0.64
#: A render's estimate is corrected by the median of its last estimates' errors, within these bounds.
CORRECTION_BOUNDS = (0.5, 3.0)
#: What the estimate is made of: an estimate kept by another model is never held against this one's errors. 3: each
#: frame weighed by its scene type. 4: a video's own last render at the quality prices the scene types it drew
#: with its pages' parallel efficiency, and scene types are priced at what the renders of the same worker shape
#: measured first.
ESTIMATE_MODEL = 4
#: Memory of the render process and one Chromium browser, whatever is drawn ...
PROCESS_BASE = 450 * _MIB
#: ... and of each browser more: pages whose frames are begun each draw in a browser of their own
#: (``compositor.render_frames``), pages screenshot share one (:func:`browsers_of`). Measured in the worker image
#: (Chromium's headless shell, ``RENDER_CHROMIUM_ARGS`` and ``BEGIN_FRAME_ARGS``): a browser more with its page holds
#: 43 MiB over a page more in the same browser, a blank page and one drawing at 3840x2160 alike - what a page draws
#: is its own (:data:`PAGE_BASE`, the page peaks); to the 16 MiB above.
BROWSER_BASE = 48 * _MIB
#: Memory of one page, whatever its size: its renderer and the decoders it keeps open.
PAGE_BASE = 350 * _MIB
#: Memory per output pixel of one page: its renderer's surfaces, its screenshots and the frames its server decodes.
PAGE_BYTES_PER_PIXEL = 80
#: Memory per output pixel of one encoder, by x264 preset: its lookahead and reference frames, in the proportional
#: share the supervisor holds a render to (``resources.tree_memory``). ``slow``: 2.0 GiB at 3840x2160, measured
#: alone (ffmpeg 9.0, crf 15, PNG frames: 1.79 to 2.00 GiB with 1, 2, 4 or 8 threads) and beside its page (the 30 s
#: ultra render of ``test_video_studio_ultra_render_chromium``: 6.4 GiB on two pages, 4.1 to 4.4 GiB of it ffmpeg).
#: ``medium``: a 116.7 s standard render held 2.1 to 2.4 GiB on two 1920x1080 pages (dev-docs "Flat memory").
#: ``veryfast`` (draft) is unmeasured: ``medium`` scaled by its shorter lookahead.
ENCODER_BYTES_PER_PIXEL = {"veryfast": 60, "medium": 210, "slow": 265}
#: Memory of one screen insert playing a video, on one page, whatever the output size: its source's ffmpeg
#: decoder - 55 to 60 MiB a page for a 1920x1080 source, measured - and its frame decoded and warped by the page's
#: frame server. Measured on a 6 s clip scene of a chroma-green screen, three keyed 1920x1080 video inserts and one
#: picture insert, drawn by ``compositor.render_frames`` in Chromium (draft): 300 to 400 MiB more than the same
#: scene without them on one 1280x720 page, 480 to 530 MiB on two, 310 MiB on one 1920x1080 page.
INSERT_VIDEO_BASE = 90 * _MIB
#: Memory per output pixel of one screen insert on one page: the footage frame decoded at the host's size,
#: the matte, the despill and the layer composed on it, its glow at 1/8, its PNG and the page's decode of it; and
#: the five raw mattes of the matte's window (float32, the insert's box at most) and the warp read at
#: twice the box's size, 20 bytes a pixel more, estimated from the arrays held, not measured again.
INSERT_BYTES_PER_PIXEL = 60
#: Memory per output pixel of one page with any matted insert: the footage frames the frame server keeps at
#: the host's size for the matte's window (``compositor.UNDER_FRAMES``, RGB), shared by every insert of the page.
INSERT_FOOTAGE_BYTES_PER_PIXEL = 12
#: The share of the memory ceiling the pages fill at most where a page's memory is modelled: the rest is the margin
#: of an estimate. A page measured (:data:`PAGE_PEAK_FILE`) keeps :data:`PAGE_MARGIN` over its peak instead.
MEMORY_HEADROOM = 0.9
#: The peaks one page reached, measured: one page drawing the 60 s mixed video of
#: ``tests/video_studio_render_fixture.py`` - its renderer, its encoder and decoders, what its frame server and the
#: driver held for it - by ``test_video_studio_page_peak_chromium`` in the ``Video Studio ultra render`` workflow,
#: which fails when a run measures outside this peak's margin.
PAGE_PEAK_FILE = Path(__file__).with_name("data") / "render_page_peak.json"
#: What a page's budget keeps over its measured peak.
PAGE_MARGIN = 0.2
#: What one more x264 thread holds per output pixel at ``slow``: 2.06 GiB with 1 thread, 2.33 GiB with 2,
#: 2.45 with 4, 2.61 with 8 at 3840x2160 (ffmpeg, crf 15, PNG frames on a pipe, keyframes every 120 frames), 80 MiB a
#: thread: what a page whose encoder runs on more or fewer threads than measured holds more or less.
ENCODER_THREAD_BYTES_PER_PIXEL = 10
#: A render runs this long at most (``jobs._deadline``).
RENDER_DEADLINE_SECONDS = 4 * 3600
#: An ultra render runs this long at most, in segments a new attempt resumes from.
ULTRA_WALL_SECONDS = 8 * 3600
#: What one scene range costs besides its frames: reading the render's drawing inputs, opening its pages.
RANGE_OVERHEAD_SECONDS = 60.0
#: The qualities from the sharpest down: the order a refusal looks for one that fits.
_SHARPEST_FIRST = ("ultra", "high", "standard", "draft")
#: The most CPUs a refusal says a worker needs: past them, no worker is sized to draw it in time.
_MOST_CPUS = 256
#: The pages a scene preview draws on at most: seconds of 720p30, beside the whole render it runs next to,
#: on the pages its own memory ceiling is sized for (``render_process.preview_memory_ceiling``).
PREVIEW_PAGES = 2
#: Set on a scene preview's process that draws on one page: started again after its memory ceiling stopped
#: it - never for want of its worker's memory.
ONE_PAGE_ENV = "XTM_RENDER_ONE_PAGE"
#: Why a scene preview's process draws on one page: the note its answer says.
ONE_PAGE_NOTE_ENV = "XTM_RENDER_ONE_PAGE_NOTE"
#: The memory a render's process lays its pages out in, in bytes: the room its supervisor read on its
#: worker as it launched it (``render_process.launch_room``), a preview's admission ceiling.
ROOM_ENV = "XTM_RENDER_MEMORY_ROOM"
#: Where a scene preview came to be drawn: routed from another worker, or after waiting for its pages - the
#: note its answer says.
ADMISSION_NOTE_ENV = "XTM_RENDER_ADMISSION_NOTE"
#: The pages a whole render's process draws on at most when it runs again after its memory ceiling stopped it
#: (``render_retry.lighter_pages``): never the layout that failed, run again unchanged.
RETRY_PAGES_ENV = "XTM_RENDER_RETRY_PAGES"
#: Where the worker publishes what it holds a render to, and for how long it stands.
_WORKER_KEY = "xtm:video-studio:render-worker"
_WORKER_TTL_SECONDS = 3600
_PUBLISH_EVERY_SECONDS = 300.0
#: ``[monotonic time, the frame capture published]`` of the last publication (:func:`publish_worker`).
_published_at: list = []
#: The whole renders a rate is measured on, newest first.
_MEASURED = 20
#: The fewest frames a measured drawing must hold to set a rate: opening the pages costs seconds of its own.
MIN_MEASURED_FRAMES = 120


def wall_seconds(quality: str | None) -> int:
    """How long a render of *quality* may run, its attempts together."""
    return ULTRA_WALL_SECONDS if quality == "ultra" else RENDER_DEADLINE_SECONDS


@dataclass(frozen=True)
class Layout:
    """What a render draws on: its pages, each with its encoder, and the CPUs they share."""

    pages: int
    cpus: int
    #: The pages it would draw on with memory enough (``VIDEO_STUDIO_RENDER_PAGES``, bounded by the CPUs).
    wanted: int

    def as_dict(self) -> dict:
        return {"pages": self.pages, "cpus": self.cpus}


@dataclass(frozen=True)
class InsertLoad:
    """The screen inserts one page draws at once at most: those playing a video, those showing a picture."""

    videos: int = 0
    pictures: int = 0

    @classmethod
    def of_scenes(cls, scenes: list[list[str]]) -> InsertLoad:
        """The most *scenes* - each the kinds of its inserts, ``video`` or another, in order - show at once.

        A page draws one scene at a time but for a transition, which shows a
        scene and the next: of each kind, the most a scene alone or two in a
        row show (an upper bound of every moment).
        """
        loads = [cls(sum(kind == "video" for kind in kinds), sum(kind != "video" for kind in kinds)) for kinds in scenes]
        moments = [*loads, *(cls(a.videos + b.videos, a.pictures + b.pictures) for a, b in itertools.pairwise(loads))]
        return cls(max((m.videos for m in moments), default=0), max((m.pictures for m in moments), default=0))

    @classmethod
    def of(cls, document: dict) -> InsertLoad:
        """The most a planned *document* shows at once (each insert's ``kind`` as the plan placed it)."""
        return cls.of_scenes(
            [[str(item.get("kind") or "image") for item in scene.get("inserts") or []] for scene in document.get("scenes") or []]
        )

    @classmethod
    def of_spec(cls, spec: Any, scene_id: str | None = None) -> InsertLoad:
        """The most a parsed *spec* shows at once, read before anything is planned; *scene_id*: its preview's window.

        A recording or an AI shot plays a video, a brand mark is a picture, and
        a file - a video or a picture, unknown until it is read - counts as a
        video: never less than the plan places.
        """
        from app.services.video_studio import timeline

        scenes = list(spec.scenes)
        if scene_id is not None:
            ids = [scene.id for scene in scenes]
            if scene_id not in ids:
                return cls()
            first, end = timeline.preview_window(len(scenes), ids.index(scene_id))
            scenes = scenes[first:end]
        # A quad the render finds on the footage is drawn too: counted where it stands until then.
        parts = timeline.insert_sources(timeline.with_quads(spec, {}, stand_in=True))
        return cls.of_scenes(
            [
                ["image" if part.kind == "asset" else "video" for key, part in parts.items() if timeline.insert_scene(key) == scene.id]
                for scene in scenes
            ]
        )

    def __bool__(self) -> bool:
        return bool(self.videos or self.pictures)


def insert_memory(size: tuple[int, int], inserts: InsertLoad | None) -> int:
    """What *inserts* add to one page drawing at *size*: a video's decoder, and each one's working set.

    The page's frame server composes an insert on the footage frame decoded at
    the host's size - its matte, the despill, the layer and its glow at 1/8 -
    and the page decodes the layer it is sent; a video insert also holds the
    decoder of its source.
    """
    if not inserts:
        return 0
    pixels = size[0] * size[1]
    return (
        inserts.videos * (INSERT_VIDEO_BASE + pixels * INSERT_BYTES_PER_PIXEL)
        + inserts.pictures * (pixels * INSERT_BYTES_PER_PIXEL)
        + pixels * INSERT_FOOTAGE_BYTES_PER_PIXEL
    )


@dataclass(frozen=True)
class PagePeak:
    """What one page held at most, measured (:data:`PAGE_PEAK_FILE`): drawing what, on what, and when.

    *peak* with the fixture's screen inserts on screen (:attr:`inserts` at once at most), *plain* with none: a page
    drawing a video without inserts is held to the second.
    """

    size: tuple[int, int]
    x264_preset: str
    threads: int
    inserts: InsertLoad
    peak: int
    plain: int
    measured: str = ""
    on: str = ""
    fixture: str = ""

    def applies(self, size: tuple[int, int], x264_preset: str) -> bool:
        """Whether it says what a page at *size* with *x264_preset* holds: the same encoder, the same short side."""
        return x264_preset == self.x264_preset and min(size) == min(self.size)

    def need(self, size: tuple[int, int], inserts: InsertLoad | None = None, threads: int | None = None) -> int:
        """What a page at *size* holds at its peak: the measured one scaled by its pixels, its encoder on *threads*
        threads (the measured ones by default), and an insert more than measured counted as one is modelled."""
        pixels, measured = size[0] * size[1], self.size[0] * self.size[1]
        held = self.peak if inserts else self.plain
        need = PAGE_BASE + max(0, held - PAGE_BASE) * pixels / max(measured, 1)
        if inserts:
            videos = max(0, inserts.videos - self.inserts.videos)
            pictures = max(0, inserts.pictures - self.inserts.pictures)
            need += videos * (INSERT_VIDEO_BASE + pixels * INSERT_BYTES_PER_PIXEL) + pictures * pixels * INSERT_BYTES_PER_PIXEL
        if threads:
            need += (int(threads) - self.threads) * pixels * ENCODER_THREAD_BYTES_PER_PIXEL
        return max(PAGE_BASE, math.ceil(need))

    def basis(self) -> str:
        """``measured on 21 Oct 2026: one page drawing ... at 3840x2160 ...``: where the peak comes from, in words."""
        shown = f"{self.inserts.videos} video and {self.inserts.pictures} picture insert(s) at once at most"
        return (
            f"measured{f' on {self.measured}' if self.measured else ''}: one page drawing {self.fixture or 'a mixed video'} "
            f"at {self.size[0]}x{self.size[1]} ({shown}), its encoder on {self.threads} threads"
        )


@functools.cache
def page_peaks() -> tuple[PagePeak, ...]:
    """The page peaks measured (:data:`PAGE_PEAK_FILE`); none when it cannot be read: every page is modelled then."""
    try:
        raw = json.loads(PAGE_PEAK_FILE.read_text(encoding="utf-8"))
        return tuple(
            PagePeak(
                size=(int(item["size"][0]), int(item["size"][1])),
                x264_preset=str(item["x264_preset"]),
                threads=int(item["threads"]),
                inserts=InsertLoad(int(item["inserts"]["videos"]), int(item["inserts"]["pictures"])),
                peak=int(item["peak_bytes"]),
                plain=int(item["plain_bytes"]),
                measured=str(item.get("measured") or ""),
                on=str(item.get("on") or ""),
                fixture=str(item.get("fixture") or ""),
            )
            for item in raw.get("peaks") or []
        )
    except (OSError, ValueError, KeyError, TypeError, IndexError) as exc:
        logger.warning("Render page peaks not read (%s): every page's memory is modelled", format_exception_message(exc))
        return ()


def page_peak(size: tuple[int, int], x264_preset: str) -> PagePeak | None:
    """The measured peak of a page at *size* with *x264_preset* (:meth:`PagePeak.applies`); ``None``: modelled."""
    return next((peak for peak in page_peaks() if peak.applies(size, x264_preset)), None)


def page_memory(
    size: tuple[int, int], x264_preset: str, inserts: InsertLoad | None = None, *, threads: int | None = None
) -> int:
    """One page drawing at *size*, its encoder on *threads* threads, and the screen inserts it draws at once at most
    (:class:`InsertLoad`): its measured peak (:func:`page_peak`), else the per-pixel model."""
    measured = page_peak(size, x264_preset)
    if measured is not None:
        return measured.need(size, inserts, threads)
    per_pixel = PAGE_BYTES_PER_PIXEL + ENCODER_BYTES_PER_PIXEL.get(x264_preset, ENCODER_BYTES_PER_PIXEL["slow"])
    return PAGE_BASE + size[0] * size[1] * per_pixel + insert_memory(size, inserts)


def page_budget(
    size: tuple[int, int], x264_preset: str, inserts: InsertLoad | None = None, *, threads: int | None = None
) -> int:
    """What a layout holds one page to: its measured peak and :data:`PAGE_MARGIN`, or its model within
    :data:`MEMORY_HEADROOM` of the ceiling."""
    need = page_memory(size, x264_preset, inserts, threads=threads)
    if page_peak(size, x264_preset) is not None:
        return math.ceil(need * (1 + PAGE_MARGIN))
    return math.ceil(need / MEMORY_HEADROOM)


def browsers_of(pages: int) -> int:
    """The Chromium browsers *pages* pages draw in: one each where their frames are begun - two pages begun in
    one browser stall together (``compositor.render_frames``) -, one for them all where they are screenshot.
    Read as the render process is told (``compositor.FRAME_CAPTURE_ENV``), else as this worker's probe settled
    (``compositor.known_capture``), else as ``VIDEO_STUDIO_FRAME_CAPTURE`` asks: a web process pricing a render it
    cannot probe counts the browsers asked for."""
    from app.services.video_studio import compositor

    handed = os.environ.get(compositor.FRAME_CAPTURE_ENV, "")
    if handed in ("begin_frame", "screenshot"):
        mode = handed
    else:
        known = compositor.known_capture()
        mode = known.mode if known is not None else compositor.frame_capture_mode()
    return max(1, int(pages)) if mode == "begin_frame" else 1


def memory_needed(
    size: tuple[int, int], *, pages: int, x264_preset: str, inserts: InsertLoad | None = None, cpus: int | None = None
) -> int:
    """The peak memory of a render's processes drawing at *size* on *pages* pages with *x264_preset*, its inserts in;
    with *cpus*, each page's encoder on its share of them (:func:`encoder_threads`). Each browser past the first
    (:func:`browsers_of`) holds :data:`BROWSER_BASE`."""
    threads = encoder_threads(pages, cpus) if cpus else None
    browsers = (browsers_of(pages) - 1) * BROWSER_BASE
    return PROCESS_BASE + browsers + pages * page_memory(size, x264_preset, inserts, threads=threads)


def memory_budget(
    size: tuple[int, int], *, pages: int, x264_preset: str, inserts: InsertLoad | None = None, cpus: int | None = None
) -> int:
    """The memory ceiling a render on *pages* pages needs: the processes' base - a browser more for each page begun on
    its own - and each page's budget."""
    if page_peak(size, x264_preset) is None:
        return math.ceil(memory_needed(size, pages=pages, x264_preset=x264_preset, inserts=inserts) / MEMORY_HEADROOM)
    threads = encoder_threads(pages, cpus) if cpus else None
    browsers = (browsers_of(pages) - 1) * BROWSER_BASE
    return PROCESS_BASE + browsers + pages * page_budget(size, x264_preset, inserts, threads=threads)


def auto_pages(cpus: int) -> int:
    """The pages ``VIDEO_STUDIO_RENDER_PAGES=0`` (the default) draws on: the worker's CPUs less one, at least one.

    The CPU left is the worker's own: its event loop, the render's supervisor and lease, the jobs beside the
    render. Each page has a CPU of its own; its encoder's threads take what the pages leave.
    """
    return max(1, int(cpus) - 1)


def encoder_threads(pages: int, cpus: int | None = None) -> int:
    """The x264 threads each of *pages* pages' encoders runs: its page's share of the worker's *cpus*, one at least.

    Every encoder used to start a thread per CPU of the worker: four pages on eight CPUs ran 32 x264 threads, each
    holding its frames, beside the pages that draw them.
    """
    from app.services.video_studio import resources

    return max(1, (int(cpus) if cpus else resources.cpu_budget()) // max(1, int(pages)))


#: What a frame shows, by what it costs a page to draw: one scene, two scenes over the transition between
#: them (the page draws both), a screen insert composed on its scene (its matte, its warp, its PNG), or both.
FRAME_KINDS = ("plain", "transition", "insert", "transition_insert")


#: What a scene type adds to the scene's own type (:func:`scene_class`): a statement over a moving shot or a
#: file, a screen insert composed on its footage.
_OVER_SHOT, _OVER_FILE, _WITH_INSERT = "+clip", "+file", "+insert"
#: Where the cost of a scene type no render or preview of this platform measured comes from.
UNMEASURED_SCENE = "never measured here: weighed by its frames' kinds"


def scene_class(kind: str, *, backdrop: str | None = None, inserted: bool = False) -> str:
    """The scene type a scene's frames are timed and weighed under: its *kind* (``clip``, ``statement``,
    ``figure``...), ``+clip`` or ``+file`` over the *backdrop* it plays (``clip`` for a shot, ``file`` for an
    upload), ``+insert`` with a screen insert composed on it - ``clip+insert``, ``statement+clip``, ``figure``."""
    name = str(kind or "scene")
    if backdrop == "clip":
        name += _OVER_SHOT
    elif backdrop:
        name += _OVER_FILE
    return name + (_WITH_INSERT if inserted else "")


def scene_words(name: str) -> str:
    """``clip with a screen insert``, ``statement over a clip``: a scene type (:func:`scene_class`) as an answer says it."""
    kind, *added = name.split("+")
    said = kind
    if "clip" in added:
        said += " over a clip"
    elif "file" in added:
        said += " over a video or picture file"
    if "insert" in added:
        said += " with a screen insert"
    return said


def scene_classes(spec: Any) -> dict[str, str]:
    """``scene id -> scene type`` (:func:`scene_class`) of every scene of the parsed *spec*: one reading for the
    pre-flight and the render, whatever the planned document makes of a scene (a figure drawn plays as a shot)."""
    found: dict[str, str] = {}
    for scene in getattr(spec, "scenes", None) or []:
        backdrop = getattr(scene, "backdrop", None)
        under = None if backdrop is None else ("clip" if getattr(backdrop, "clip_id", None) else "file")
        inserted = any(getattr(overlay, "type", None) == "screen_insert" for overlay in getattr(scene, "overlays", None) or [])
        found[str(scene.id or "")] = scene_class(scene.type, backdrop=under, inserted=inserted)
    return found


@dataclass(frozen=True)
class FrameKinds:
    """Which kind (:data:`FRAME_KINDS`) each frame of a planned document is: its transitions' and its inserts' spans;
    and which scene type (:func:`scene_class`): its scenes' spans."""

    fps: float
    frames: int
    transitions: tuple[tuple[float, float], ...] = ()
    inserts: tuple[tuple[float, float], ...] = ()
    #: The sub-frames each frame draws (motion blur, ``motion_design.blur_samples``): what a page draws counts
    #: them, never the frames the video holds.
    samples: int = 1
    #: ``(start, end, scene type)`` of each scene, in timeline order.
    scenes: tuple[tuple[float, float, str], ...] = ()

    @classmethod
    def of(
        cls,
        document: dict,
        inserts: list[tuple[float, float]] | None = None,
        *,
        quality: str | None = None,
        classes: dict[str, str] | None = None,
    ) -> FrameKinds:
        """*document*'s transitions in, and the spans of the screen inserts it places - or *inserts*, for a plan that
        places none yet (``insert_spans``: a pre-flight's estimate); drawn at *quality*, its motion blur's sub-frames.
        Each scene's type is *classes*' (:func:`scene_classes`), else read off the document."""
        from app.services.video_studio.motion_design import blur_samples

        scenes = list(document.get("scenes") or [])
        crossings = []
        for scene in scenes[1:]:
            start = float(scene.get("start") or 0.0)
            seconds = float((scene.get("transition_in") or {}).get("duration") or 0.0)
            if seconds > 0:
                crossings.append((start, start + seconds))
        if inserts is None:
            from app.services.video_studio.timeline import insert_drawn

            # Where each layer is drawn: a keyed insert's whole scene.
            inserts = [
                tuple(float(scene.get("start") or 0.0) + at for at in insert_drawn(item))
                for scene in scenes
                for item in scene.get("inserts") or []
                if isinstance(item, dict)
            ]
        placed = tuple((a, b) for a, b in inserts if b > a)
        spans = []
        for scene in scenes:
            start, end = float(scene.get("start") or 0.0), float(scene.get("end") or 0.0)
            name = (classes or {}).get(str(scene.get("id") or ""))
            if name is None:
                backdrop = scene.get("backdrop") if isinstance(scene.get("backdrop"), dict) else {}
                under = {"video": "clip", "image": "file"}.get(str(backdrop.get("kind") or "")) if backdrop else None
                name = scene_class(str(scene.get("type") or ""), backdrop=under, inserted=any(a < end and b > start for a, b in placed))
            spans.append((start, end, name))
        return cls(
            float(document.get("fps") or 30.0),
            int(document.get("frames") or 0),
            tuple(crossings),
            placed,
            blur_samples((document.get("theme") or {}).get("motion"), quality),
            tuple(spans),
        )

    def kind(self, frame: int) -> str:
        at = frame / max(self.fps, 1.0)
        crossing = any(a <= at < b for a, b in self.transitions)
        inserted = any(a <= at < b for a, b in self.inserts)
        if crossing:
            return "transition_insert" if inserted else "transition"
        return "insert" if inserted else "plain"

    def scene(self, frame: int) -> str | None:
        """The scene type of *frame*: the scene coming in over a transition, the last one past the end."""
        at = frame / max(self.fps, 1.0)
        for start, end, name in reversed(self.scenes):
            if start <= at < end:
                return name
        return self.scenes[-1][2] if self.scenes and at >= self.scenes[-1][0] else None

    def labels(self, first: int, end: int) -> list[str]:
        """The kind of each frame of ``[first, end)``, as :meth:`kind` says it, read in one pass over the spans."""
        first, end = max(0, first), max(first, end)
        fps = max(self.fps, 1.0)

        def covered(spans: tuple[tuple[float, float], ...]) -> list[bool]:
            marks = [0] * (end - first + 1)
            for a, b in spans:
                low, high = max(first, _first_frame_at(a, fps)), min(end, _first_frame_at(b, fps))
                if high > low:
                    marks[low - first] += 1
                    marks[high - first] -= 1
            running, inside = 0, []
            for mark in marks[:-1]:
                running += mark
                inside.append(running > 0)
            return inside

        crossing, inserted = covered(self.transitions), covered(self.inserts)
        return [
            ("transition_insert" if shown else "transition") if crossed else ("insert" if shown else "plain")
            for crossed, shown in zip(crossing, inserted, strict=True)
        ]

    def counts(self, first: int = 0, end: int | None = None) -> dict[str, int]:
        """How many frames of ``[first, end)`` a page draws of each kind, each one :attr:`samples` times."""
        found = dict.fromkeys(FRAME_KINDS, 0)
        for kind in self.labels(first, self.frames if end is None else min(end, self.frames)):
            found[kind] += self.samples
        return found

    def scene_counts(self, first: int = 0, end: int | None = None) -> dict[tuple[str, str | None], int]:
        """How many frames of ``[first, end)`` a page draws of each ``(kind, scene type)``, each :attr:`samples` times."""
        found: dict[tuple[str, str | None], int] = {}
        for frame in range(max(0, first), self.frames if end is None else min(end, self.frames)):
            key = (self.kind(frame), self.scene(frame))
            found[key] = found.get(key, 0) + self.samples
        return found


def _first_frame_at(seconds: float, fps: float) -> int:
    """The first frame whose time (``frame / fps``) is at or past *seconds*: where a span of :class:`FrameKinds` starts."""
    frame = max(0, math.ceil(seconds * fps))
    while frame > 0 and (frame - 1) / fps >= seconds:
        frame -= 1
    while frame / fps < seconds:
        frame += 1
    return frame


def insert_spans(spec: Any, document: dict) -> list[tuple[float, float]]:
    """``(from, to)`` in the video of each screen insert *spec* asks, where *document* plans its scene.

    A pre-flight plans the video before its inserts' sources are read (``projects.estimate``): its document places
    none, so they are read off the spec - from the overlay's start in its scene, for its duration, within the scene.
    """
    spans = []
    for scene, planned in zip(spec.scenes, document.get("scenes") or [], strict=False):
        start, end = float(planned.get("start") or 0.0), float(planned.get("end") or 0.0)
        for overlay in getattr(scene, "overlays", None) or []:
            if getattr(overlay, "type", None) != "screen_insert":
                continue
            begin = start + float(getattr(overlay, "start", 0.0) or 0.0)
            spans.append((begin, min(end, begin + float(getattr(overlay, "duration", 0.0) or 0.0))))
    return spans


def preview_pages() -> int:
    """The pages a scene preview asks: :data:`PREVIEW_PAGES`, or fewer when ``VIDEO_STUDIO_RENDER_PAGES`` says so."""
    configured = int(settings.video_studio_render_pages)
    return min(PREVIEW_PAGES, configured) if configured > 0 else PREVIEW_PAGES


def preview_drawn_pages() -> int:
    """The pages a scene preview's process draws on: :func:`preview_pages`, one when its supervisor says so.

    The supervisor sets :data:`ONE_PAGE_ENV` on the process it starts again
    after the ceiling stopped the first, and on a preview its worker's
    memory held on one page only: on one page the insert layers are
    composed one after the other, never side by side.
    """
    return 1 if os.environ.get(ONE_PAGE_ENV) else preview_pages()


def retry_pages() -> int | None:
    """The pages this whole render's process draws on at most (:data:`RETRY_PAGES_ENV`): its run again after the
    memory ceiling; ``None`` for any other run, laid out as :func:`layout` reads the worker."""
    raw = (os.environ.get(RETRY_PAGES_ENV) or "").strip()
    return max(1, int(raw)) if raw.isdigit() else None


def retry_pages_note() -> str | None:
    """What a whole render drawn on fewer pages after its memory ceiling says; ``None`` for any other run."""
    pages = retry_pages()
    if pages is None:
        return None
    return (
        f"Drawn on {pages} page{'s' if pages != 1 else ''} at most: its first run passed its memory ceiling, so it ran "
        "again on a lighter layout."
    )


def preview_one_page_note() -> str | None:
    """Why this scene preview's process draws on one page (:data:`ONE_PAGE_NOTE_ENV`); ``None`` on its pages."""
    if not os.environ.get(ONE_PAGE_ENV):
        return None
    return (os.environ.get(ONE_PAGE_NOTE_ENV) or "").strip() or None


def preview_admission_note() -> str | None:
    """Where this scene preview's process came to be drawn (:data:`ADMISSION_NOTE_ENV`); ``None``: at once,
    on the worker that first took it."""
    return (os.environ.get(ADMISSION_NOTE_ENV) or "").strip() or None


def launch_ceiling(fallback: int) -> int:
    """The memory this render's process lays its pages out in: :data:`ROOM_ENV`, what its supervisor read
    on its worker as it launched it; *fallback* for a process no supervisor started."""
    raw = (os.environ.get(ROOM_ENV) or "").strip()
    return int(raw) if raw.isdigit() else fallback


def layout_event(drawn: Layout, size: tuple[int, int], *, x264_preset: str, inserts: InsertLoad | None = None) -> dict:
    """What a render's process tells its supervisor of the pages it laid out: their budget is what the
    render holds of its worker from then on, the rest of its room free for scene previews."""
    budget = memory_budget(size, pages=drawn.pages, x264_preset=x264_preset, inserts=inserts, cpus=drawn.cpus)
    return {"type": "layout", "pages": drawn.pages, "wanted": drawn.wanted, "budget": int(budget)}


def layout(
    size: tuple[int, int],
    *,
    x264_preset: str,
    ceiling: int | None,
    pages: int | None = None,
    cpus: int | None = None,
    inserts: InsertLoad | None = None,
) -> Layout:
    """The pages a render at *size* draws on under *ceiling*, on a worker of *cpus* CPUs (its CPU quota).

    *pages* is ``VIDEO_STUDIO_RENDER_PAGES`` (0: :func:`auto_pages`); never more than the CPUs, nor than the ceiling
    holds (:func:`memory_budget`: each page its measured peak and its margin, its encoder on its share of the CPUs,
    with the screen *inserts* it draws at once at most, :class:`InsertLoad`), at least one. Nothing a quality
    depends on changes for it.
    """
    from app.services.video_studio import resources

    configured = int(settings.video_studio_render_pages) if pages is None else int(pages)
    cpus = max(1, int(cpus) if cpus else (configured if configured > 0 else resources.cpu_budget()))
    wanted = max(1, min(configured if configured > 0 else auto_pages(cpus), cpus))
    drawn = wanted
    if ceiling:
        drawn = next(
            (
                count
                for count in range(wanted, 0, -1)
                if memory_budget(size, pages=count, x264_preset=x264_preset, inserts=inserts, cpus=cpus) <= ceiling
            ),
            1,
        )
    return Layout(pages=drawn, cpus=cpus, wanted=wanted)


@dataclass(frozen=True)
class Rates:
    """What one page draws a second at this quality, and what was measured to say it."""

    #: Weighted pixel-frames a second one page with :data:`PAGE_CPUS` CPUs draws and encodes.
    page_rate: float
    #: Renders of this platform it was measured on (0: :data:`DEFAULT_PAGE_RATE`), of them at this very quality.
    measured: int = 0
    measured_quality: int = 0
    #: Whole renders of this platform at this very quality ready among the last ones: evidence it renders here.
    rendered_here: int = 0
    #: Seconds the other stages take for each second of video (the media, the sound, storing).
    other_per_second: float = DEFAULT_OTHER_PER_SECOND
    #: Weighted pixel-frames a second one page draws of each kind (:data:`FRAME_KINDS`), where measured; a
    #: kind not measured costs :data:`DEFAULT_KIND_COST` of :attr:`page_rate`.
    kind_rates: dict = field(default_factory=dict)
    #: Seconds the screen inserts' preparation takes per second of insert on screen, and the check stage per frame of
    #: a million pixels.
    insert_per_second: float = DEFAULT_INSERT_PER_SECOND
    check_per_megapixel_frame: float = DEFAULT_CHECK_PER_MEGAPIXEL_FRAME
    #: What a page spends on each segment beside its frames, per million pixels (:data:`DEFAULT_SEGMENT_PER_MEGAPIXEL`).
    segment_per_megapixel: float = DEFAULT_SEGMENT_PER_MEGAPIXEL
    #: Whether the renders measured drew on contended pages (:func:`contended`): ``None`` when none says.
    contended: bool | None = None
    #: The last estimates of this model at this quality on the worker shape asked, from the same kind of history,
    #: against what their renders took (:class:`Missed`, newest first): what corrects the next one
    #: (:attr:`correction`).
    errors: tuple = ()
    #: Weighted pixel-frames a second one page draws of each scene type (:func:`scene_class`), in the regime of:
    #: attr:`contended`, measured on this platform's renders and scene previews; and how many measured each.
    scene_rates: dict = field(default_factory=dict)
    scene_measured: dict = field(default_factory=dict)
    #: Weighted pixel-frames a second the figures stage draws on its one page, and the drawings measured.
    figure_rate: float | None = None
    figure_measured: int = 0
    #: The kinds of :attr:`kind_rates` read off the previous render of the same video (:func:`video_kind_rates`)
    #: rather than the platform's median: its screen-insert frames, whose cost depends on the insert.
    video_kinds: tuple = ()
    #: The last finished render of the same video at this quality (:func:`with_own_render`): every scene type
    #: it drew is in :attr:`scene_rates` at what it measured, and its pages' parallel efficiency times the drawing.
    own: OwnRender | None = None
    #: The scene types of :attr:`scene_rates` priced at what whole renders at this quality on the worker shape asked
    #: measured (:func:`measured_rates`), and how many did: what a scene type's estimate learns from first.
    scene_here: dict = field(default_factory=dict)
    #: The kind of history an estimate made of these rates is priced from (:func:`priced_from`); empty: read
    #: off :attr:`own` and the scene types it draws (:attr:`costs_from`).
    priced: str = ""

    @property
    def correction(self) -> float:
        """What the next estimate is multiplied by: the median of ``took / said`` of :attr:`errors`, within bounds."""
        ratios = [float(item[1]) / float(item[0]) for item in self.errors if item[0] > 0 and item[1] > 0]
        if not ratios:
            return 1.0
        low, high = CORRECTION_BOUNDS
        return min(high, max(low, _median(ratios)))

    @property
    def efficiency(self) -> float | None:
        """The parallel efficiency the video's own last render measured (:func:`parallel_efficiency`), else ``None``."""
        return self.own.efficiency if self.own is not None else None

    @property
    def costs_from(self) -> str:
        """The kind of history these rates price an estimate from (:func:`priced_from`) when :func:`rates_of` read it
        for the scene types of the video; else ``video`` with :attr:`own`, ``platform`` without."""
        return self.priced or ("video" if self.own is not None else "platform")

    def kind_rate(self, kind: str) -> float:
        """The weighted pixel-frames a second one page draws of *kind*: measured, else :attr:`page_rate` at its cost."""
        measured = self.kind_rates.get(kind)
        if measured:
            return float(measured)
        return self.page_rate / DEFAULT_KIND_COST.get(kind, 1.0)

    def frame_rate(self, kind: str | None, scene: str | None = None) -> float:
        """The weighted pixel-frames a second one page draws of a frame of *kind* in a scene of type *scene*:
        the scene type's measured rate, else the kind's (:meth:`kind_rate`), else :attr:`page_rate`."""
        measured = self.scene_rates.get(scene) if scene else None
        if measured:
            return float(measured)
        return self.kind_rate(kind) if kind else self.page_rate

    def scene_basis(self, scene: str, quality: str | None = None) -> str:
        """``measured on 3 renders or previews of this platform``, or that the scene type never was and
        where its frames' kinds are costed from (:meth:`basis` at *quality*); ``measured on the last ultra render of
        this video, `1a2b3c4d` on 8 pages`` or ``measured on 2 ultra renders on this worker shape`` first. What
        comes before its first comma is its short form (:meth:`Preflight.scenes_line`)."""
        at = f"{quality} " if quality else ""
        if self.own is not None and scene in self.own.scene_rates:
            return f"measured on the last {at}render of this video, `{self.own.short}` on {self.own.pages} pages"
        here = int(self.scene_here.get(scene) or 0)
        if here and self.scene_rates.get(scene):
            return f"measured on {here} {at}render{'s' if here != 1 else ''} on this worker shape"
        count = int(self.scene_measured.get(scene) or 0)
        if count and self.scene_rates.get(scene):
            return f"measured on {count} render{'s' if count != 1 else ''} or preview{'s' if count != 1 else ''} of this platform"
        return f"{UNMEASURED_SCENE}, {self.basis(quality)}" if quality else UNMEASURED_SCENE

    @property
    def source(self) -> str:
        """Where the rate comes from: ``measured`` at this quality, ``scaled`` from the others, ``default``."""
        if self.measured_quality:
            return "measured"
        return "scaled" if self.measured else "default"

    def basis(self, quality: str) -> str:
        """``measured on 3 ultra renders of this platform``: the rate's source, in the words an answer says it."""
        if self.measured_quality:
            count = self.measured_quality
            return f"measured on {count} {quality} render{'s' if count != 1 else ''} of this platform"
        if self.measured:
            count = self.measured
            return (
                f"measured on {count} render{'s' if count != 1 else ''} of this platform at other qualities, "
                "scaled by their pixels"
            )
        return "the CI measurement: no render of this platform measured yet"


#: The kinds of history an estimate is priced from (:func:`priced_from`): what its errors are read per.
PRICED_WORDS = {
    "video": "from its video's own last render",
    "shape": "from the renders on this worker shape",
    "platform": "from the platform's renders",
}


def priced_from(rates: Rates | None, scene_types: Any) -> str:
    """The kind of history an estimate of a video drawing *scene_types* is priced from: ``video`` when its
    video's own last render at the quality prices it (:attr:`Rates.own`), ``shape`` when the renders on its worker
    shape measured any of its scene types (:attr:`Rates.scene_here`), else ``platform``. Estimates priced from one
    err differently than estimates priced from another: the errors correcting one are read among its own kind."""
    if rates is None:
        return "platform"
    if rates.own is not None:
        return "video"
    return "shape" if any(name in rates.scene_here for name in scene_types or ()) else "platform"


def _page_share(pages: int, cpus: int) -> float:
    """The CPUs one of *pages* pages has on *cpus* CPUs, :data:`PAGE_CPUS` at most."""
    return min(float(PAGE_CPUS), cpus / max(pages, 1))


def contended(pages: int, cpus: int) -> bool:
    """Whether *pages* pages are fewer than :data:`CONTENDED_SHARE` of the worker's *cpus*.

    Laid out by its memory, a 4K render of 21 Oct 2026 drew on 3 of an 8-CPU worker's pages, at 2.77 frames a
    second: 47 min for the 30 its estimate said, made as if its pages had filled the worker.
    """
    return pages < cpus * CONTENDED_SHARE


def frames_per_second(
    rates: Rates,
    *,
    size: tuple[int, int],
    quality: str,
    pages: int,
    cpus: int,
    kind: str | None = None,
    scene: str | None = None,
) -> float:
    """Frames a second *pages* pages draw at *quality* and *size* on *cpus* CPUs - of *kind* (:data:`FRAME_KINDS`)
    in a scene of type *scene* (:meth:`Rates.frame_rate`) when given, else at :attr:`Rates.page_rate` -,
    contended pages at the rate contended pages measured (:data:`DEFAULT_CONTENTION` of the others' until one did,
    the CI rate being one of the others)."""
    cost = PIXEL_COST.get(quality, 1.0) * size[0] * size[1]
    rate = rates.frame_rate(kind, scene)
    asked, measured = contended(pages, cpus), bool(rates.contended)
    if asked and not measured:
        rate *= DEFAULT_CONTENTION
    elif measured and not asked:
        rate /= DEFAULT_CONTENTION
    return rate * pages * _page_share(pages, cpus) / PAGE_CPUS / max(cost, 1.0)


def page_costs(
    rates: Rates,
    *,
    size: tuple[int, int],
    quality: str,
    pages: int,
    cpus: int,
    samples: int = 1,
    frame_kinds: FrameKinds | None = None,
) -> page_schedule.FrameCosts:
    """What one of *pages* pages takes for a frame of each kind - its *samples* sub-frames - and for each segment it
    opens and closes, at *rates*: the prices the compositor shares a render's frames out by before it measured
    its own, and the pre-flight replays its schedule with. With *frame_kinds*, each kind at the mean of what its frames'
    scene types cost (:func:`frame_costs`): the same total as frame by frame."""
    from app.services.video_studio import page_schedule

    each = frame_costs(rates, size=size, quality=quality, pages=pages, cpus=cpus)
    counted: dict[str, list[float]] = {}
    for (kind, scene), count in (frame_kinds.scene_counts() if frame_kinds is not None else {}).items():
        spent = counted.setdefault(kind, [0.0, 0.0])
        spent[0] += count * each(kind, scene)
        spent[1] += count
    seconds = {
        kind: max(1, samples) * (counted[kind][0] / counted[kind][1] if counted.get(kind, [0, 0])[1] else each(kind))
        for kind in FRAME_KINDS
    }
    sources = {
        kind: "video" if kind in rates.video_kinds else "platform" if kind in rates.kind_rates or rates.measured else "default"
        for kind in FRAME_KINDS
    }
    return page_schedule.FrameCosts(seconds, rates.segment_per_megapixel * size[0] * size[1] / 1e6, sources)


def page_run_seconds(
    frame_kinds: FrameKinds, rates: Rates, *, size: tuple[int, int], quality: str, pages: int, cpus: int
) -> list[float]:
    """When each page draws its last frame of *frame_kinds*: the compositor's own schedule replayed
    (``compositor.plan_segments`` into segments, ``page_schedule.PageWork``: runs of the same cost, then the frames
    left taken by the pages that are idle, ``page_schedule.simulate``), each frame at what a page takes for its kind,
    each segment at what a page spends on one beside its frames, its screen inserts warmed as if matted.

    The render takes as long as its slowest page, never its pages' average. Each frame costs what its scene type
    measured, else its kind (:func:`page_costs`).
    """
    from app.services.video_studio import compositor, page_schedule

    total, fps = frame_kinds.frames, max(1, round(frame_kinds.fps))
    pieces = compositor.plan_segments(0, total, compositor.segment_frames(total, fps), [])
    costs = page_costs(
        rates, size=size, quality=quality, pages=pages, cpus=cpus, samples=frame_kinds.samples, frame_kinds=frame_kinds
    )
    rate = max(frame_kinds.fps, 1.0)
    warm = [(_first_frame_at(a, rate), _first_frame_at(b, rate)) for a, b in frame_kinds.inserts]
    work = page_schedule.PageWork(pieces, pages, page_schedule.FramePrices(frame_kinds, 0, total), costs, warm=warm)
    return page_schedule.simulate(work)


def frame_costs(rates: Rates, *, size: tuple[int, int], quality: str, pages: int, cpus: int) -> Any:
    """``(kind, scene type) -> seconds`` one of *pages* pages takes for such a frame, each pair computed once."""
    found: dict[tuple[str | None, str | None], float] = {}

    def each(kind: str | None, scene: str | None = None) -> float:
        if (kind, scene) not in found:
            drawn = frames_per_second(rates, size=size, quality=quality, pages=pages, cpus=cpus, kind=kind, scene=scene)
            found[(kind, scene)] = pages / max(drawn, 1e-9)
        return found[(kind, scene)]

    return each


def drawing_seconds(
    rates: Rates, *, kinds: dict, size: tuple[int, int], quality: str, pages: int, cpus: int
) -> float:
    """How long *pages* pages draw frames of *kinds* (``{kind: frames}``, :meth:`FrameKinds.counts`; ``None``: frames
    of no known kind, at :attr:`Rates.page_rate`) side by side."""
    return sum(
        count / max(frames_per_second(rates, size=size, quality=quality, pages=pages, cpus=cpus, kind=kind), 1e-9)
        for kind, count in kinds.items()
        if count
    )


def measurement(
    drawn: Layout,
    *,
    frames: int,
    seconds: float,
    figure_seconds: float = 0.0,
    other_seconds: float = 0.0,
    frames_reused: int = 0,
    figure_frames: int = 0,
    figure_frames_reused: int = 0,
    figures: list[dict] | None = None,
    segments_reused: dict | None = None,
    kinds: dict | None = None,
    stages: dict | None = None,
    total_seconds: float | None = None,
    insert_seconds: float = 0.0,
    encoder_threads: int = 0,
    page_runs: list | None = None,
    scenes: dict | None = None,
    insert_frames: dict | None = None,
    frames_inherited: int = 0,
    inherited: list[dict] | None = None,
    pace: dict | None = None,
    cpu_seconds: float | None = None,
    contention: dict | None = None,
) -> dict:
    """What a render records of its drawing (``VideoRender.measured``): what the next pre-flights estimate from.

    *frames* are the composed frames this attempt drew in *seconds* - the rate - and *frames_reused* those it kept
    from the segments a former attempt closed, *segments_reused* which they are (:func:`segments_reused`);
    *figure_frames* the figure frames it drew and *figure_frames_reused* those a kept drawing served; *figures* the
    reuse index's entries (``reuse_index.figure_entries``). A render drawn in scene ranges adds the frames its other
    ranges' workers drew (``frames_joined``). *kinds*, ``{kind: [frames, page-seconds]}`` of the frames
    drawn (``compositor.DrawProgress.kinds``); *stages*, the seconds of each stage; *total_seconds*, the attempt's
    whole time; *insert_seconds*, how long its screen inserts are on screen; *encoder_threads*, each page encoder's;
    *page_runs*, each page's ``[segments, frame seconds, page seconds]`` (``compositor.DrawProgress.runs``).
    *scenes*, ``{scene type: [frames, page-seconds]}`` of the frames drawn (:func:`scene_class`,
    ``compositor.DrawProgress.scenes``) - what the next pre-flights weigh a frame of each scene type by. And
    *insert_frames*, ``{cached, drawn, share}`` of its screen-insert frames read back from the worker's disk
    (``compositor.DrawProgress.insert_frames``): a render that read any back is no measure of its whole rate, its
    stages or its estimate's error (:func:`_read_back`); its kinds and scene types leave those frames out already.
    *frames_inherited*, the frames taken from the render of an older revision it replaced, and
    *inherited*, by run (``compositor.DrawProgress.inherited``: the render, its revision, the frames and how many of
    each scene) - kept, so never in *frames_reused* nor in the rate. *pace*, what explained a pace under
    half its pre-flight's (``render_pace.PaceWatch.record``) - its time is then no error of its estimate
    (:func:`_error`), and drawn on a shared worker it prices its video's next pre-flights with the scene types it
    found over their cost only (:meth:`OwnRender.of`). *cpu_seconds*, the CPU its process tree used -
    what a scene preview is counted for beside a render (``cpu_share.measured_cpus``) -, and *contention*, the CPUs a
    whole render's pages got beside the jobs sharing its worker (``render_pace.PaceWatch.contention``): its time
    corrects the next estimates at the share it got (``cpu_share.idle_equivalent``).
    """
    return {
        **drawn.as_dict(),
        "frames": int(frames),
        "frames_reused": int(frames_reused),
        "seconds": round(max(0.0, seconds), 1),
        "figure_frames": int(figure_frames),
        "figure_frames_reused": int(figure_frames_reused),
        "figure_seconds": round(max(0.0, figure_seconds), 1),
        "other_seconds": round(max(0.0, other_seconds), 1),
        **({"figures": figures} if figures else {}),
        **({"segments_reused": segments_reused} if segments_reused else {}),
        **({"kinds": kinds} if kinds else {}),
        **({"stages": {name: round(float(value), 1) for name, value in stages.items()}} if stages else {}),
        **({"total_seconds": round(max(0.0, total_seconds), 1)} if total_seconds is not None else {}),
        **({"insert_seconds": round(max(0.0, insert_seconds), 2)} if insert_seconds else {}),
        **({"encoder_threads": int(encoder_threads)} if encoder_threads else {}),
        **({"page_runs": [list(run) for run in page_runs]} if page_runs else {}),
        **({"scenes": {name: list(drawn) for name, drawn in scenes.items()}} if scenes else {}),
        **({"insert_frames": dict(insert_frames)} if insert_frames else {}),
        **({"frames_inherited": int(frames_inherited)} if frames_inherited else {}),
        **(
            {"inherited": [{key: run[key] for key in ("render", "revision", "frames", "segments", "scenes")} for run in inherited]}
            if inherited
            else {}
        ),
        **({"pace": dict(pace)} if pace else {}),
        **({"cpu_seconds": round(cpu_seconds, 1)} if cpu_seconds else {}),
        **({"contention": dict(contention)} if contention else {}),
    }


def segments_reused(*trackers: Any) -> dict | None:
    """The segments the *trackers* (``compositor.DrawProgress``) kept from an attempt before: ``{segments: [{frames,
    segments, revision, attempt}], matched: {part: digest}}`` - who drew them and the parts of the drawing's digest
    they matched on (``compositor.drawing_inputs``); ``None`` when nothing was kept."""
    runs = [run for tracker in trackers for run in getattr(tracker, "reused", None) or []]
    if not runs:
        return None
    matched: dict[str, str] = {}
    for tracker in trackers:
        matched.update(getattr(tracker, "matched", None) or {})
    return {"segments": runs, "matched": matched}


def inherited_scenes(measured: dict) -> str:
    """``revision 99, render `id`: s01 600, s02 1,200 frames`` of what a render took from the renders it replaced
    (``measured["inherited"]``)."""
    by_render: dict[tuple[str, Any], dict[str, int]] = {}
    for run in measured.get("inherited") or []:
        scenes = by_render.setdefault((str(run.get("render")), run.get("revision")), {})
        for scene, count in (run.get("scenes") or {}).items():
            scenes[scene] = scenes.get(scene, 0) + int(count)
    return "; ".join(
        f"revision {revision}, render `{render}`: " + ", ".join(f"{scene} {count:,}" for scene, count in scenes.items()) + " frames"
        for (render, revision), scenes in by_render.items()
    )


#: The parts of a drawing's digest a render's answer names one by one; the rest are counted.
NAMED_PARTS = 8


def reused_line(reused: dict | None, *, compact: bool = False) -> str:
    """``frames 0-300 (1 segment) drawn for revision 12 ...; its digest matched on the document a1b2, ...``: what a
    render kept of the segments an attempt before drew (:func:`segments_reused`); empty for none.

    *compact* says it in one summary - the frames, segments and attempts - for the compact answer of
    ``get_video_jobs``: a render resumed over many builds kept dozens of runs (issue #10480)."""
    if not isinstance(reused, dict) or not reused.get("segments"):
        return ""
    if compact:
        frames = segments = 0
        attempts: set[str] = set()
        for run in reused["segments"]:
            first, end = (run.get("frames") or [0, 0])[:2]
            frames += max(0, int(end) - int(first))
            segments += int(run.get("segments") or 1)
            if run.get("attempt"):
                attempts.add(str(run["attempt"]))
        by = f" by {len(attempts)} earlier attempt{'s' if len(attempts) != 1 else ''}" if attempts else ""
        return f"{frames:,} frames in {segments} segment{'s' if segments != 1 else ''}{by}, each listed in the full report"
    runs = []
    for run in reused["segments"]:
        first, end = (run.get("frames") or [0, 0])[:2]
        count = int(run.get("segments") or 1)
        drawn = f" drawn for revision {run['revision']}" if run.get("revision") is not None else ""
        by = f" by the attempt {run['attempt']}" if run.get("attempt") else ""
        runs.append(f"frames {first}-{end} ({count} segment{'s' if count != 1 else ''}){drawn}{by}")
    matched = reused.get("matched") or {}
    inserts = [name for name in matched if name.startswith("insert ")]
    named = [name for name in matched if not name.startswith(("source ", "image "))][: max(NAMED_PARTS, len(inserts))]
    rest = len(matched) - len(named)
    parts = ", ".join(f"{name} {matched[name][:8]}" for name in named) + (f" and {rest} more" if rest > 0 else "")
    return f"{'; '.join(runs)} - its digest matched on {parts} (an insert's part covers every field it is drawn by, its key included)"


@dataclass(frozen=True)
class Infra:
    """Advice on the platform's sizing a pre-flight gives: said in the pre-flight of the first render that
    needs it, then only when its numbers (:attr:`key`) change - never in a render's own measurements."""

    #: ``pages`` (the memory ceiling holds fewer pages than the CPUs), ``cap`` (past the 4 h a render may run).
    kind: str
    #: The numbers it is said for: the same key said before is not said again.
    key: str
    text: str
    #: Whether this pre-flight says it (:func:`with_infra_said`).
    said: bool = True

    def as_dict(self) -> dict:
        return {"kind": self.kind, "key": self.key, "text": self.text, "said": self.said}


@dataclass(frozen=True)
class Preflight:
    """A render's estimate, as its answer says it: every input it is made of."""

    quality: str
    frames: int
    size: tuple[int, int]
    pages: int
    cpus: int
    #: Expected wall time on one worker, and the renders of this platform the rate was measured on (0: the CI rate).
    seconds: float
    measured: int
    #: Of them at this very quality.
    measured_quality: int
    #: Whole renders of this platform at this very quality: evidence that beats the estimate.
    measured_here: int
    #: Seconds one page takes for one frame.
    frame_seconds: float
    memory_bytes: int
    #: The worker's memory ceiling, ``None`` when no worker published one.
    ceiling_bytes: int | None
    #: How long it may run, its attempts together.
    wall_seconds: int = RENDER_DEADLINE_SECONDS
    problems: list[str] = field(default_factory=list)
    #: What it said that refuses nothing (an ultra render running past 4 h in segments, its scene ranges).
    notes: list[str] = field(default_factory=list)
    #: Its advice on the platform's sizing (:class:`Infra`): never among its notes, said once.
    infra: list[Infra] = field(default_factory=list)
    #: Where the rate comes from (:attr:`Rates.source`), and in words (:meth:`Rates.basis`).
    rate_source: str = "default"
    basis: str = ""
    #: The figure frames, drawn first on one page.
    figure_frames: int = 0
    #: The seconds of what is not the composed frames: the figures, the media, the sound, the checks.
    fixed_seconds: float = 0.0
    #: The scene ranges it is drawn in, each on a worker of its own (1: drawn whole), and how long one takes.
    ranges: int = 1
    range_seconds: float | None = None
    #: What a worker needs to draw it - whole, or one of its ranges - under the cap (:func:`needs`): its CPUs, and
    #: the memory ceiling when the worker's holds too few pages; ``None`` within the cap, or when no CPU count would.
    cpus_needed: int | None = None
    memory_needed_bytes: int | None = None
    #: How long it takes on a worker of :attr:`cpus_needed` CPUs.
    needed_seconds: float | None = None
    #: The highest quality that fits this worker now, offered by a refusal (:func:`highest_fitting`).
    fits: dict | None = None
    #: Its figure scenes, those an earlier render's drawing serves (``reuse_index``) and their frames.
    figures: int = 0
    figures_reused: int = 0
    figure_frames_reused: int = 0
    #: What one page holds at its peak here (:func:`page_memory`), the budget the layout holds it to, where the peak
    #: comes from; the pages the worker draws on with memory enough, and how long the render takes on them.
    page_peak_bytes: int = 0
    page_budget_bytes: int = 0
    page_basis: str = ""
    wanted_pages: int = 0
    full_pages_seconds: float | None = None
    #: Its frames of each kind (:data:`FRAME_KINDS`) and what a page takes for one of each, the inserts'
    #: preparation and the check stage.
    kinds: dict = field(default_factory=dict)
    kind_seconds: dict = field(default_factory=dict)
    #: The kinds of :attr:`kind_seconds` priced at what the previous render of the same video measured.
    video_kinds: tuple = ()
    insert_seconds: float = 0.0
    check_seconds: float = 0.0
    contended: bool = False
    #: The estimate before its correction, the correction and the errors it is made of (:attr:`Rates.correction`).
    raw_seconds: float = 0.0
    correction: float = 1.0
    errors: tuple = ()
    #: Why it is drawn in ranges: ``cap`` (past the 4 h), ``split`` (past ``VIDEO_STUDIO_RENDER_SPLIT_MINUTES``).
    split_reason: str = ""
    #: ``(spoken, narrated)``: its narrations planned at the length they were spoken, of those it has.
    narrations: tuple = (0, 0)
    #: How the render worker takes its frames, as its probe at start settled (``compositor.FrameCapture.words``):
    #: said in :meth:`line`; empty when screenshots are asked and nothing was probed.
    capture: str = ""
    #: Its frames of each scene type (:func:`scene_class`), what a page takes for one of each and where that
    #: comes from (:meth:`Rates.scene_basis`); the slowest of them, what the estimate is quoted from.
    scenes: dict = field(default_factory=dict)
    scene_seconds: dict = field(default_factory=dict)
    scene_basis: dict = field(default_factory=dict)
    slowest: str = ""
    #: How many figure drawings measured the figures stage's rate: ``0``, :data:`FIGURE_SPEED` is assumed.
    figure_measured: int = 0
    #: Where its frames' costs come from (:func:`priced_from`): ``video``, its video's own last render at the
    #: quality - that render, the pages it drew on, their parallel efficiency -; ``shape``, the renders on its worker
    #: shape for some of its scene types; ``platform`` otherwise.
    costs_from: str = "platform"
    own_render: str = ""
    own_pages: int = 0
    efficiency: float | None = None
    #: The worker shape it is made for (:class:`Shape`): what the errors correcting it were read per.
    shape: Shape | None = None
    #: What a page takes for a drawn frame (a sub-frame under motion blur) of each scene type, before the correction:
    #: what the render's own measure of each is set against once it is drawn (:func:`said_of`).
    scene_raw_seconds: dict = field(default_factory=dict)
    #: The scene types it draws that its video's own last render at the quality does not price: ``{scene
    #: type: {frames, why, basis}}`` - ``why`` ``not drawn`` (a scene added since) or ``shared`` (drawn on a shared
    #: worker, ``render_pace.learned_scenes``) -, each priced from the platform's measured cost of its type instead.
    unlearned: dict = field(default_factory=dict)
    #: Its worker's committed load and what it takes beside it (``cpu_share.Shared.as_dict``): empty on an idle
    #: worker, or where the load could not be read.
    load: dict = field(default_factory=dict)

    @property
    def shared(self) -> Any:
        """Its ``cpu_share.Shared`` when its worker has a committed load, else ``None``."""
        from app.services.video_studio.cpu_share import Shared

        found = Shared.of(self.load)
        return found if found is not None and found.jobs else None

    @property
    def said_seconds(self) -> float:
        """What it says it takes: beside its worker's committed load (:attr:`shared`), else on an idle worker
        (:attr:`expected_seconds`, what a refusal, its ranges and its corrections are planned on)."""
        shared = self.shared
        return shared.seconds if shared is not None else self.expected_seconds

    @property
    def slowest_seconds(self) -> float:
        """What a page takes for a frame of its slowest scene type, else for its average frame."""
        return float(self.scene_seconds.get(self.slowest) or self.frame_seconds) if self.slowest else self.frame_seconds

    @property
    def minutes(self) -> int:
        return max(1, math.ceil(self.seconds / 60))

    @property
    def cap_seconds(self) -> int:
        """What one worker draws a render - or one of its ranges - within."""
        return RENDER_DEADLINE_SECONDS

    @property
    def expected_seconds(self) -> float:
        """How long it should take: on one worker, or with each of its ranges on a worker of its own."""
        if self.ranges > 1 and self.range_seconds is not None:
            return self.fixed_seconds + self.range_seconds
        return self.seconds

    @property
    def refuses(self) -> bool:
        """Whether its problems refuse the render: none was ever rendered whole here at this quality."""
        return bool(self.problems) and self.measured_here == 0

    def drawn_on(self) -> str:
        """``4 pages (8 CPUs)``."""
        return f"{self.pages} page{'s' if self.pages != 1 else ''} ({self.cpus} CPU{'s' if self.cpus != 1 else ''})"

    def inputs(self) -> str:
        """``7,860 frames at 3840x2160, 0.93 s a frame on each of 4 pages (8 CPUs) - <its rate's source>``; with its
        scene types, quoted from the slowest: ``8,462 frames at 3840x2160, its slowest scenes (clip with a
        screen insert, 1,240 frames) at 5.60 s a frame on each of 4 pages (8 CPUs) - measured on 2 renders ...``."""
        if self.slowest and self.slowest in self.scene_seconds:
            count = int(self.scenes.get(self.slowest) or 0)
            return (
                f"{self.frames:,} frames at {self.size[0]}x{self.size[1]}, its slowest scenes ({scene_words(self.slowest)}, "
                f"{count:,} frames) at {self.slowest_seconds:.2f} s a frame on each of {self.drawn_on()} - "
                f"{self.scene_basis.get(self.slowest) or self.basis or 'the CI measurement'}"
            )
        return (
            f"{self.frames:,} frames at {self.size[0]}x{self.size[1]}, {self.frame_seconds:.2f} s a frame on each of "
            f"{self.drawn_on()} - {self.basis or 'the CI measurement'}"
        )

    def scenes_line(self) -> str:
        """``By scene type: 1,240 frames of clip with a screen insert at 5.60 s each on a page (measured on 2 renders
        or previews of this platform), 3,000 of title at 0.31 s (never measured here: weighed by its frames' kinds).``"""
        shown = sorted(self.scenes.items(), key=lambda item: -float(self.scene_seconds.get(item[0]) or 0.0))
        if len(shown) < 2:
            return ""
        bases = [(self.scene_basis.get(name) or UNMEASURED_SCENE).split(", ")[0] for name, _count in shown]
        # Every scene type measured on the same renders: said once.
        shared = bases[0] if len(set(bases)) == 1 and bases[0].startswith("measured") else ""
        parts = [
            f"{int(count):,}{' frames' if index == 0 else ''} of {scene_words(name)} at "
            f"{float(self.scene_seconds.get(name) or 0.0):.2f} s{' each on a page' if index == 0 else ''}"
            + ("" if shared else f" ({bases[index]})")
            for index, (name, count) in enumerate(shown)
        ]
        return f"By scene type{f', all {shared}' if shared else ''}: {_joined(parts)}."

    def line(self) -> str:
        """``Expected to take about 41 min: 7,860 frames at 3840x2160, 0.93 s a frame on each of 4 pages ...``."""
        memory = f"about {_gib(self.memory_bytes)} of memory"
        if self.ceiling_bytes is not None:
            memory += f" of the worker's {_gib(self.ceiling_bytes)} ceiling"
        shared = self.shared
        took = shared.headline() if shared is not None else f"about {_duration(self.expected_seconds)}"
        said = f"Expected to take {took}: {self.inputs()}"
        if self.fixed_seconds >= 30:
            said += f", plus about {_duration(self.fixed_seconds)} for the figures, the media, the sound and the checks"
        said += f"; {memory}."
        if shared is not None:
            said += " " + shared.words()
        if self.kinds:
            said += " " + self.kinds_line()
        if by_scene := self.scenes_line():
            said += " " + by_scene
        if own := self.own_line():
            said += " " + own
        if unlearned := unlearned_words(self.quality, self.own_render, self.unlearned):
            said += " " + unlearned
        spoken, narrated = (*tuple(self.narrations), 0, 0)[:2]
        if narrated:
            said += (
                f" {spoken} of its {narrated} narration{'s' if narrated != 1 else ''} planned at the length "
                f"{'it was' if spoken == 1 else 'they were'} spoken"
                + (", the rest at the reading pace until spoken." if spoken < narrated else ".")
            )
        if self.page_peak_bytes and self.pages >= max(self.wanted_pages, 1):
            said += f" It draws on all {self.pages} of the worker's pages: {self.peak_words()}."
        if self.figures_reused:
            said += (
                f" {self.figures_reused} of its {self.figures} figure{'s' if self.figures != 1 else ''} "
                f"({self.figure_frames_reused:,} frames) {'is' if self.figures_reused == 1 else 'are'} kept from an "
                "earlier render and not drawn again."
            )
        if self.errors or self.shape is not None:
            said += " " + self.errors_line()
        if self.capture:
            said += " " + self.capture
        for sentence in (*self.notes, *self.problems):
            said += " " + sentence
        return said

    def infra_once(self) -> list[str]:
        """The advice on the platform's sizing this pre-flight says: the :class:`Infra` it was the first to
        need, or whose numbers changed since it was last said."""
        return [item.text for item in self.infra if item.said]

    def peak_words(self) -> str:
        """``each page peaks at about 2.9 GiB (measured on ...), held to 3.5 GiB with its 20% margin``."""
        if self.page_basis.startswith("measured"):
            return (
                f"each page peaks at about {_gib(self.page_peak_bytes)} ({self.page_basis}), held to "
                f"{_gib(self.page_budget_bytes)} with its {round(PAGE_MARGIN * 100)}% margin"
            )
        return f"each page holds about {_gib(self.page_peak_bytes)} ({self.page_basis or 'modelled'})"

    def kinds_line(self) -> str:
        """``Of its 7,317 frames, 4,120 are plain, 397 in transitions and 2,800 with a screen insert (0.93, 1.49 and 1.86 s
        each on a page); about 6 min for its inserts' preparation, 3 min for its checks.``"""
        words = {"plain": "plain", "transition": "in transitions", "insert": "with a screen insert", "transition_insert": "with an insert in a transition"}
        shown = [(kind, int(self.kinds.get(kind) or 0)) for kind in FRAME_KINDS if self.kinds.get(kind)]
        if not shown:
            return ""
        listed = _joined([f"{count:,} {words[kind]}" for kind, count in shown])
        timed = _joined([f"{float(self.kind_seconds.get(kind) or 0.0):.2f}" for kind, _count in shown])
        said = f"Of its {self.frames:,} frames, {listed} ({timed} s each on a page{', contended' if self.contended else ''})"
        own = [words[kind] for kind, _count in shown if kind in self.video_kinds]
        if own:
            said += f", those {_joined(own)} at what the previous render of this video measured"
        stages = [
            f"about {_duration(seconds)} {what}"
            for seconds, what in ((self.insert_seconds, "for its inserts' preparation"), (self.check_seconds, "for its checks"))
            if seconds >= 30
        ]
        return said + ("; " + ", ".join(stages) if stages else "") + "."

    def own_line(self) -> str:
        """``Every scene type the last ultra render of this video (`1a2b3c4d`) drew is priced at what it measured on its
        8 pages, and its frames over the whole run at its pages' parallel efficiency, 0.86 ...``; empty when its costs
        are the platform's."""
        if self.costs_from != "video" or not self.own_render:
            return ""
        said = (
            f"Every scene type the last {self.quality} render of this video (`{self.own_render[:8]}`) drew is priced at "
            f"what it measured on its {self.own_pages} page{'s' if self.own_pages != 1 else ''}"
        )
        if self.efficiency:
            said += (
                f", and its frames at the parallel efficiency its pages had over the whole run, {self.efficiency:.2f} "
                "(its frames a second over the run divided by its pages, against what a page took for a frame)"
            )
        return said + ", before any correction."

    def errors_line(self) -> str:
        """``Its last 3 ultra estimates on this worker shape (8 pages of 9 CPUs, ...), priced from the platform's renders
        like this one - renders `1a2b3c4d`, `...` and `...` - said 30 min, 12 min and 41 min for renders that took 47 min,
        13 min and 40 min: this one is corrected by x1.08, their median.``; that none was, on a known shape."""
        alike = PRICED_WORDS.get(self.costs_from, PRICED_WORDS["platform"])
        shape = f" on this worker shape ({self.shape.words()})" if self.shape is not None else ""
        if not self.errors:
            return (
                f"Not corrected: no earlier {self.quality} estimate of this model{shape}, priced {alike} like this one, "
                "was followed by a whole render yet."
            )
        said = _joined([_duration(item[0]) for item in self.errors])
        took = _joined([_duration(item[1]) for item in self.errors])
        named = _joined([f"`{str(item[2])[:8]}`" for item in self.errors if len(item) > 2 and item[2]])
        count = len(self.errors)
        plural = "s" if count != 1 else ""
        return (
            f"Its last {count} {self.quality} estimate{plural}{shape}, priced {alike} like this one"
            f"{f' - render{plural} {named} -' if named else ''} said {said} for render{plural} that took {took}: this one "
            f"is corrected by x{self.correction:.2f}, their median."
        )

    def as_dict(self) -> dict:
        return {
            "quality": self.quality,
            "frames": self.frames,
            "figure_frames": self.figure_frames,
            "size": list(self.size),
            "pages": self.pages,
            "cpus": self.cpus,
            # What the render card's ETA reads: beside its worker's committed load; on an idle worker apart.
            "expected_seconds": round(self.said_seconds),
            "idle_seconds": round(self.expected_seconds),
            "load": dict(self.load) if self.shared is not None else None,
            "one_worker_seconds": round(self.seconds),
            "cap_seconds": self.cap_seconds,
            "measured_renders": self.measured,
            "measured_at_quality": self.measured_quality,
            "rate_source": self.rate_source,
            "basis": self.basis,
            "frame_seconds": round(self.frame_seconds, 2),
            "fixed_seconds": round(self.fixed_seconds),
            "memory_bytes": self.memory_bytes,
            "memory_ceiling_bytes": self.ceiling_bytes,
            "wall_seconds": self.wall_seconds,
            "ranges": self.ranges,
            "range_seconds": round(self.range_seconds) if self.range_seconds is not None else None,
            "cpus_needed": self.cpus_needed,
            "memory_needed_bytes": self.memory_needed_bytes,
            "needed_seconds": round(self.needed_seconds) if self.needed_seconds is not None else None,
            "fits": self.fits,
            "problems": list(self.problems),
            "notes": list(self.notes),
            "infra": [item.as_dict() for item in self.infra],
            "figures": self.figures,
            "figures_reused": self.figures_reused,
            "figure_frames_reused": self.figure_frames_reused,
            "model": ESTIMATE_MODEL,
            "raw_seconds": round(self.raw_seconds),
            "correction": round(self.correction, 3),
            "errors": [[round(item[0]), round(item[1]), *([str(item[2])] if len(item) > 2 else [])] for item in self.errors],
            "costs_from": self.costs_from,
            "own_render": self.own_render or None,
            "own_pages": self.own_pages or None,
            "efficiency": round(self.efficiency, 3) if self.efficiency else None,
            "shape": self.shape.as_dict() if self.shape is not None else None,
            "scene_raw_seconds": {name: round(value, 3) for name, value in self.scene_raw_seconds.items()},
            "unlearned_scenes": {name: dict(item) for name, item in self.unlearned.items()},
            "page_peak_bytes": self.page_peak_bytes,
            "page_budget_bytes": self.page_budget_bytes,
            "page_basis": self.page_basis,
            "wanted_pages": self.wanted_pages,
            "full_pages_seconds": round(self.full_pages_seconds) if self.full_pages_seconds is not None else None,
            "kinds": dict(self.kinds),
            "kind_seconds": {kind: round(value, 3) for kind, value in self.kind_seconds.items()},
            "video_kinds": list(self.video_kinds),
            "insert_seconds": round(self.insert_seconds),
            "check_seconds": round(self.check_seconds),
            "contended": self.contended,
            "split_reason": self.split_reason,
            "narrations": list(self.narrations),
            "capture": self.capture,
            "scenes": dict(self.scenes),
            "scene_seconds": {name: round(value, 3) for name, value in self.scene_seconds.items()},
            "scene_basis": dict(self.scene_basis),
            "slowest": self.slowest,
            "slowest_seconds": round(self.slowest_seconds, 2),
            "figure_measured": self.figure_measured,
        }


def unlearned_words(quality: str, own_render: str, unlearned: Any) -> str:
    """``Not learned from this video's own history, priced from the platform's instead: figure (1,200 frames, which
    its last ultra render `1a2b3c4d` did not draw) - measured on 3 renders or previews of this platform.``
    (:attr:`Preflight.unlearned`); empty when its video's own last render priced every scene type it draws."""
    if not isinstance(unlearned, dict) or not unlearned:
        return ""
    last = f"its last {quality} render" + (f" `{str(own_render)[:8]}`" if own_render else "")
    said = []
    for name, item in sorted(unlearned.items(), key=lambda entry: -int((entry[1] or {}).get("frames") or 0)):
        item = item if isinstance(item, dict) else {}
        why = "drew on a shared worker, its measure the worker's share as much as its cost" if item.get("why") == "shared" else "did not draw"
        said.append(
            f"{scene_words(str(name))} ({int(item.get('frames') or 0):,} frames, which {last} {why}) - "
            f"{item.get('basis') or UNMEASURED_SCENE}"
        )
    return f"Not learned from this video's own history, priced from the platform's instead: {'; '.join(said)}."


def _joined(items: list[str]) -> str:
    """``a, b and c``."""
    return ", ".join(items[:-1]) + f" and {items[-1]}" if len(items) > 1 else (items[0] if items else "")


def _gib(value: int) -> str:
    return f"{value / (1 << 30):.1f} GiB"


def _duration(seconds: float) -> str:
    """``41 min``, ``6 h 12 min``: a duration as an answer says it."""
    minutes = max(1, math.ceil(max(0.0, seconds) / 60))
    if minutes < 60:
        return f"{minutes} min"
    hours, rest = divmod(minutes, 60)
    return f"{hours} h {rest} min" if rest else f"{hours} h"


def ranges_for(
    drawing_seconds: float, *, scenes: int | None = None, max_ranges: int | None = None, within: float | None = None
) -> int:
    """The scene ranges a render whose frames take *drawing_seconds* on one worker is drawn in.

    As few as keep each range within *within* seconds - :data:`RENDER_DEADLINE_SECONDS` by default, its
    :data:`RANGE_OVERHEAD_SECONDS` included -, at most ``VIDEO_STUDIO_RENDER_MAX_RANGES`` and one a scene at most - a
    range starts on a scene. 1: drawn whole.
    """
    most = int(settings.video_studio_render_max_ranges) if max_ranges is None else int(max_ranges)
    if scenes is not None:
        most = min(most, int(scenes))
    if most <= 1:
        return 1
    room = max(RANGE_OVERHEAD_SECONDS, (within or RENDER_DEADLINE_SECONDS) - RANGE_OVERHEAD_SECONDS)
    return max(1, min(most, math.ceil(drawing_seconds / room)))


def split_seconds() -> float:
    """How long a render may take on the pages it opens before it is split across workers: 0, never for it."""
    return max(0.0, float(settings.video_studio_render_split_minutes) * 60.0)


def needs(
    rates: Rates,
    *,
    frames: int,
    fixed_seconds: float,
    size: tuple[int, int],
    quality: str,
    x264_preset: str,
    ceiling: int | None,
    inserts: InsertLoad | None = None,
) -> tuple[int | None, int | None, float | None]:
    """``(CPUs, memory ceiling, seconds)``: the worker that draws *frames* after *fixed_seconds* within the cap.

    The fewest CPUs whose pages on ``VIDEO_STUDIO_RENDER_PAGES=0`` (:func:`auto_pages`) draw them in time; the
    memory ceiling - each page with its screen *inserts* - is said only when *ceiling* holds fewer of those pages.
    ``(None, None, None)`` when *fixed_seconds* alone fill the cap, or no worker of :data:`_MOST_CPUS` CPUs draws
    them in time.
    """
    room = RENDER_DEADLINE_SECONDS - fixed_seconds
    if room <= 0:
        return None, None, None
    for cpus in range(1, _MOST_CPUS + 1):
        pages = auto_pages(cpus)
        rate = frames_per_second(rates, size=size, quality=quality, pages=pages, cpus=cpus)
        if rate > 0 and frames / rate <= room:
            memory = memory_budget(size, pages=pages, x264_preset=x264_preset, inserts=inserts, cpus=cpus)
            return (
                cpus,
                memory if ceiling is not None and memory > ceiling else None,
                fixed_seconds + frames / rate,
            )
    return None, None, None


def _remedy(said: Preflight, configured_pages: int) -> str:
    """What the worker needs for *said* to fit, in the settings an administrator changes."""
    from app.services.video_studio.render_process import memory_to_give

    if said.cpus_needed is None:
        return (
            f"no worker draws it within {RENDER_DEADLINE_SECONDS // 3600} h: what is not its frames - the figures, the "
            f"media, the sound - alone takes about {_duration(said.fixed_seconds)}"
        )
    whom = f"each of {said.ranges} workers" if said.ranges > 1 else "the worker"
    what = (
        f"{whom} needs {said.cpus_needed} CPUs - a CPU request and limit of {said.cpus_needed}, with "
        "VIDEO_STUDIO_RENDER_PAGES=0 drawing on as many pages as they and its memory hold"
    )
    if said.memory_needed_bytes is not None:
        what += f", and {memory_to_give(said.memory_needed_bytes, said.quality)}"
    if 0 < configured_pages < auto_pages(said.cpus):
        what += f" (VIDEO_STUDIO_RENDER_PAGES={configured_pages} draws on {said.pages} of its {said.cpus} CPUs now)"
    done = f"{_duration(said.needed_seconds)} a range" if said.ranges > 1 else _duration(said.needed_seconds or 0.0)
    return f"{what} - and draws it in about {done}"


def estimate(
    *,
    frames: int,
    size: tuple[int, int],
    figure_seconds: float = 0.0,
    pages: int | None = None,
    cpus: int | None = None,
    quality: str = "ultra",
    rate: Rates | None = None,
    ceiling: int | None = None,
    scenes: int | None = None,
    max_ranges: int | None = None,
    reuse: reuse_index.Served | None = None,
    inserts: InsertLoad | None = None,
    kinds: dict[str, int] | None = None,
    insert_seconds: float = 0.0,
    workers: int | None = None,
) -> Preflight:
    """The estimate of a whole render of *frames* frames at *size*, from its plan alone.

    *pages* and *cpus* are the worker's (``VIDEO_STUDIO_RENDER_PAGES`` and its CPU budget); *rate*, what this
    platform measured (:func:`measured_rates`); *figure_seconds*, the figure scenes' length, drawn first at the
    output's size on one page - less what *reuse* serves of them (``reuse_index.served``); *ceiling*, the
    worker's memory ceiling; *scenes*, how many it has (a scene range starts on one); *max_ranges*,
    ``VIDEO_STUDIO_RENDER_MAX_RANGES`` by default; *inserts*, the screen inserts a page draws at once at most
    (:class:`InsertLoad`).

    *kinds*, its frames of each kind (:meth:`FrameKinds.counts`; all plain when not given), each drawn
    at what a page measured for that kind; *insert_seconds*, how long its screen inserts are on screen, their
    preparation's term; its check stage's term from its frames and pixels; the result corrected by the errors of
    this platform's last estimates (:attr:`Rates.correction`); *workers*, the workers there now: a render longer
    than ``VIDEO_STUDIO_RENDER_SPLIT_MINUTES`` on the pages it opens is split across them.

    *kinds* placed (:class:`FrameKinds`) with their scenes' types weighs each frame by what a page
    measured for its scene type (:meth:`Rates.frame_rate`), its kind's cost only for a scene type never measured
    here; the estimate is quoted from the slowest scene type the video holds (:attr:`Preflight.slowest`), and the
    figures stage is timed at the rate its drawings measured (:attr:`Rates.figure_rate`).

    *rate* holding its video's own last render at the quality (:attr:`Rates.own`) has every scene type
    that render drew at what it measured, and its frames' page-seconds are shared by the pages at the parallel
    efficiency that render's pages had over the whole run (:func:`parallel_efficiency`) - never faster than the
    compositor's schedule replayed -, before the correction; the estimate keeps the worker shape it is made for
    (:class:`Shape`), what the next one's errors are read per.

    A scene type that render did not draw - a scene added since - or drew on a shared worker is priced
    at the platform's measured cost of its type, and the estimate says which (:attr:`Preflight.unlearned`).
    """
    from app.services.video_studio import compositor
    from app.services.video_studio.render_process import memory_to_give
    from app.services.video_studio.spec import QUALITY_PRESETS

    preset = QUALITY_PRESETS[quality]
    rates = rate or Rates(DEFAULT_PAGE_RATE)
    reuse = reuse or reuse_index.Served()
    configured = int(settings.video_studio_render_pages) if pages is None else int(pages)
    drawn = layout(size, x264_preset=preset.x264_preset, ceiling=ceiling, pages=pages, cpus=cpus, inserts=inserts)
    # Its frames where they are (:class:`FrameKinds`): each page's run of them, the slowest page the render's time.
    # Their counts only: each kind's frames shared out evenly. Without kinds, every frame at the rate of an average
    # one (:attr:`Rates.page_rate`).
    placed = kinds if isinstance(kinds, FrameKinds) else None
    counts = placed.counts() if placed is not None else kinds
    counted: dict = {kind: int(count) for kind, count in counts.items() if count} if counts else {None: frames}
    counted_frames = max(1, sum(counted.values()))
    # A frame's time by kind leaves out what a page spends on each segment; an average rate has it in.
    per_segment = rates.segment_per_megapixel * size[0] * size[1] / 1e6 if counts else 0.0
    segments = math.ceil(frames / max(1, compositor.segment_frames(frames, preset.fps)))
    scene_counts = placed.scene_counts() if placed is not None else {}

    def drawing_on(count: int) -> float:
        if placed is not None:
            scheduled = max(page_run_seconds(placed, rates, size=size, quality=quality, pages=count, cpus=drawn.cpus), default=0.0)
            if not rates.efficiency:
                return scheduled
            # The pages as the video's own last render had them over its whole run: its frames' page-seconds
            # at its parallel efficiency, never faster than the schedule replayed.
            each = frame_costs(rates, size=size, quality=quality, pages=count, cpus=drawn.cpus)
            spent = sum(drawn_frames * each(kind, name) for (kind, name), drawn_frames in scene_counts.items())
            return max(scheduled, spent / (count * rates.efficiency))
        shared = drawing_seconds(rates, kinds=counted, size=size, quality=quality, pages=count, cpus=drawn.cpus) * frames / counted_frames
        return shared + math.ceil(segments / max(1, count)) * per_segment

    per_page = frames_per_second(rates, size=size, quality=quality, pages=1, cpus=drawn.cpus)
    # The figures stage draws on one page: at the rate its drawings measured, else FIGURE_SPEED composed frames.
    figure_fps = (
        rates.figure_rate / max(PIXEL_COST.get(quality, 1.0) * size[0] * size[1], 1.0)
        if rates.figure_rate
        else per_page * FIGURE_SPEED
    )
    kept_seconds = min(max(0.0, reuse.seconds), max(0.0, figure_seconds))
    figure_frames = math.ceil((max(0.0, figure_seconds) - kept_seconds) * preset.fps)
    video_seconds = frames / max(preset.fps, 1)
    inserting = rates.insert_per_second * max(0.0, insert_seconds)
    checking = rates.check_per_megapixel_frame * frames * size[0] * size[1] / 1e6
    fixed = figure_frames / max(figure_fps, 1e-9) + rates.other_per_second * video_seconds + inserting + checking
    correction = rates.correction
    raw_drawing = drawing_on(drawn.pages)
    raw = fixed + raw_drawing
    drawing, fixed_said = raw_drawing * correction, fixed * correction
    seconds = raw * correction
    full = (fixed + drawing_on(drawn.wanted)) * correction if drawn.pages < drawn.wanted else None
    # The pages draw side by side: each takes ``pages`` times as long for one frame as the render does.
    frame_seconds = drawn.pages * drawing / max(frames, 1)
    kind_seconds = {
        kind: correction
        * drawn.pages
        / max(frames_per_second(rates, size=size, quality=quality, pages=drawn.pages, cpus=drawn.cpus, kind=kind), 1e-9)
        for kind in counted
    }
    # Each scene type's frames and what a page takes for one of them: the slowest is what the estimate says.
    scene_frames: dict[str, int] = {}
    scene_time: dict[str, float] = {}
    if placed is not None and placed.scenes:
        each = frame_costs(rates, size=size, quality=quality, pages=drawn.pages, cpus=drawn.cpus)
        for (kind, name), count in scene_counts.items():
            if name is not None:
                scene_frames[name] = scene_frames.get(name, 0) + count
                scene_time[name] = scene_time.get(name, 0.0) + count * each(kind, name)
    samples = max(1, placed.samples if placed is not None else 1)
    scene_seconds = {name: correction * scene_time[name] * samples / max(count, 1) for name, count in scene_frames.items()}
    # A drawn frame's cost before the correction: what its render's own measure of each scene type is set against.
    scene_raw = {name: scene_time[name] / max(count, 1) for name, count in scene_frames.items()}
    slowest = max(scene_seconds, key=scene_seconds.__getitem__) if scene_seconds else ""
    # The scene types its video's own last render does not price: a scene added since, one it drew on a shared
    # worker - each at the platform's measured cost of its type (:meth:`Rates.frame_rate`), and said.
    own = rates.own
    unlearned = {
        name: {
            "frames": math.ceil(count / samples),
            "why": "shared" if name in own.shared else "not drawn",
            "basis": rates.scene_basis(name, quality),
        }
        for name, count in scene_frames.items()
        if own is not None and name not in own.scene_rates
    }
    # What the video's frames cost against the average frame's: the CPUs it needs are counted on the frames it has.
    average = frames / max(frames_per_second(rates, size=size, quality=quality, pages=drawn.pages, cpus=drawn.cpus), 1e-9)
    weight = raw_drawing / max(average, 1e-9) if counts and raw_drawing > 0 else 1.0
    threads = encoder_threads(drawn.pages, drawn.cpus)
    memory = memory_needed(size, pages=drawn.pages, x264_preset=preset.x264_preset, inserts=inserts, cpus=drawn.cpus)
    peak = page_peak(size, preset.x264_preset)
    page_need = page_memory(size, preset.x264_preset, inserts, threads=threads)
    page_held = page_budget(size, preset.x264_preset, inserts, threads=threads)
    basis = peak.basis() if peak is not None else "modelled: no page measured at this quality yet"
    stall = float(settings.video_studio_render_stall_seconds)
    wall = wall_seconds(quality)
    cap = RENDER_DEADLINE_SECONDS
    problems: list[str] = []
    notes: list[str] = []
    infra: list[Infra] = []
    one_page = memory_budget(size, pages=1, x264_preset=preset.x264_preset, inserts=inserts, cpus=drawn.cpus)
    if ceiling is not None and one_page > ceiling:
        problems.append(
            f"It needs about {_gib(memory)} of memory even on one page ({_gib(one_page)} with its margin), past the "
            f"{_gib(ceiling)} the worker holds a render to. The video and its quality need no change: the worker "
            f"needs the memory - {memory_to_give(one_page, quality)}."
        )
    elif drawn.pages < drawn.wanted:
        every = memory_budget(size, pages=drawn.wanted, x264_preset=preset.x264_preset, inserts=inserts, cpus=drawn.cpus)
        held = "its encoder's lookahead and its screen inserts included" if inserts else "its encoder's lookahead included"
        plural = "s" if drawn.pages != 1 else ""
        notes.append(
            f"It draws on {drawn.pages} page{plural} at {size[0]}x{size[1]}, as many as its memory ceiling holds "
            f"({_gib(page_need)} each at its peak, {held}; {basis}; held to {_gib(page_held)} with its margin)."
        )
        # The platform's sizing: said in the first pre-flight that needs it, again only when it changes.
        infra.append(
            Infra(
                "pages",
                f"pages:{quality}:{size[0]}x{size[1]}:{drawn.pages}/{drawn.wanted}:{_mib(ceiling)}:{_mib(every)}",
                f"The worker's memory ceiling ({_gib(ceiling or 0)}) holds {drawn.pages} of its {drawn.wanted} pages at "
                f"{size[0]}x{size[1]}: this render takes about {_duration(seconds)} on {drawn.pages} page{plural}, "
                f"against about {_duration(full or seconds)} on all {drawn.wanted}. To draw on every one, "
                f"{memory_to_give(math.ceil(every), quality)}.",
            )
        )
    # Its slowest frame, never its average one, against the stall window.
    longest_frame = max(frame_seconds, scene_seconds.get(slowest, 0.0))
    if stall > 0 and longest_frame > stall / 2:
        problems.append(
            f"A frame would take about {longest_frame:.0f} s on a page, too close to the {stall:.0f} s the worker waits "
            "for a render to move (VIDEO_STUDIO_RENDER_STALL_SECONDS)."
        )
    ranges, range_seconds, split_reason = 1, None, ""
    within = split_seconds()
    if seconds > cap:
        # Past the cap on one worker: drawn in scene ranges, each on a worker of its own.
        ranges = ranges_for(drawing, scenes=scenes, max_ranges=max_ranges)
        split_reason = "cap" if ranges > 1 else ""
    elif within and seconds > within and (workers or 0) > 1:
        # Longer than the split on the pages it opens: across the workers there, each a range of its own.
        most = int(settings.video_studio_render_max_ranges) if max_ranges is None else int(max_ranges)
        ranges = ranges_for(drawing, scenes=scenes, max_ranges=min(most, int(workers or 1)), within=within)
        split_reason = "split" if ranges > 1 else ""
    if ranges > 1:
        range_seconds = drawing / ranges + RANGE_OVERHEAD_SECONDS
    said = Preflight(
        quality=quality,
        frames=frames,
        size=size,
        pages=drawn.pages,
        cpus=drawn.cpus,
        seconds=seconds,
        measured=rates.measured,
        measured_quality=rates.measured_quality,
        measured_here=rates.rendered_here,
        frame_seconds=frame_seconds,
        memory_bytes=memory,
        ceiling_bytes=ceiling,
        wall_seconds=wall,
        rate_source=rates.source,
        basis=rates.basis(quality),
        figure_frames=figure_frames,
        fixed_seconds=fixed_said,
        ranges=ranges,
        range_seconds=range_seconds,
        figures=reuse.figures,
        figures_reused=reuse.served,
        figure_frames_reused=math.ceil(kept_seconds * preset.fps),
        page_peak_bytes=page_need,
        page_budget_bytes=page_held,
        page_basis=basis,
        wanted_pages=drawn.wanted,
        full_pages_seconds=full,
        kinds=dict(counted) if kinds else {},
        kind_seconds=kind_seconds if kinds else {},
        video_kinds=tuple(kind for kind in counted if kind in rates.video_kinds) if kinds else (),
        insert_seconds=inserting * correction,
        check_seconds=checking * correction,
        contended=contended(drawn.pages, drawn.cpus),
        raw_seconds=raw,
        correction=correction,
        errors=tuple(rates.errors),
        split_reason=split_reason,
        scenes={name: math.ceil(count / samples) for name, count in scene_frames.items()},
        scene_seconds=scene_seconds,
        scene_basis={name: rates.scene_basis(name, quality) for name in scene_frames},
        slowest=slowest,
        figure_measured=rates.figure_measured if rates.figure_rate else 0,
        costs_from=rates.priced or priced_from(rates, scene_frames),
        own_render=rates.own.render_id if rates.own is not None else "",
        own_pages=rates.own.pages if rates.own is not None else 0,
        efficiency=rates.efficiency,
        shape=Shape.of(drawn.pages, drawn.cpus, ceiling),
        scene_raw_seconds=scene_raw,
        unlearned=unlearned,
    )
    longest = range_seconds if range_seconds is not None else seconds
    if longest > cap:
        # What a worker needs to draw it - whole, or one of its ranges - within the cap, its frames at what they cost.
        cpus_needed, memory_for, needed_seconds = needs(
            rates,
            frames=math.ceil(frames * weight / ranges),
            fixed_seconds=RANGE_OVERHEAD_SECONDS if ranges > 1 else fixed,
            size=size,
            quality=quality,
            x264_preset=preset.x264_preset,
            ceiling=ceiling,
            inserts=inserts,
        )
        said = dataclasses.replace(
            said, cpus_needed=cpus_needed, memory_needed_bytes=memory_for, needed_seconds=needed_seconds
        )
    if ranges > 1:
        past = (
            f"past the {cap // 3600} h a render may run"
            if split_reason == "cap"
            else f"past the {_duration(within)} a render is split at (VIDEO_STUDIO_RENDER_SPLIT_MINUTES), with "
            f"{int(workers or 0)} workers there"
        )
        notes.append(
            f"On {drawn.pages} page{'s' if drawn.pages != 1 else ''} of one worker that is about {_duration(seconds)}, "
            f"{past}: it is drawn in {ranges} scene ranges of about {_duration(range_seconds or 0.0)} each, each on a "
            "worker of its own, and joined with its sound once. A range no other worker takes is drawn by the render "
            "itself, after its own."
        )
    if longest > wall:
        split = f", even in {ranges} scene ranges of about {_duration(longest)} each" if ranges > 1 else ""
        problems.append(
            f"It would take about {_duration(seconds)} on this worker ({said.inputs()}), past the {wall // 3600} h a "
            f"render at {quality} may run{split}. The video and its quality need no change: "
            f"{_remedy(said, configured)}."
        )
    elif longest > cap:
        what = "Each of its ranges runs" if ranges > 1 else "It runs"
        notes.append(
            f"{what} past {cap // 3600} h in segments: each closes at most 10% of its frames, kept beside the render, "
            f"and a new attempt resumes from the last one, within the {wall // 3600} h an ultra render may run."
        )
        infra.append(
            Infra(
                "cap",
                f"cap:{quality}:{said.cpus_needed}:{_mib(said.memory_needed_bytes)}:{ranges}",
                f"To draw it within {cap // 3600} h, {_remedy(said, configured)}.",
            )
        )
    return dataclasses.replace(said, problems=problems, notes=notes, infra=infra)


def _mib(value: int | None) -> int:
    """*value* bytes in MiB rounded up to the hundred, as the advice says it; ``0`` for none."""
    return math.ceil((value or 0) / _MIB / 100) * 100


def with_infra_said(said: Preflight, before: set[str]) -> Preflight:
    """*said* with each of its :class:`Infra` said only when its key is not in *before*: the keys of the
    advice the last pre-flight that gave any recorded (:func:`infra_said_before`)."""
    import dataclasses

    return dataclasses.replace(said, infra=[dataclasses.replace(item, said=item.key not in before) for item in said.infra])


async def infra_said_before(db: Any, quality: str) -> set[str]:
    """The keys of the advice on the platform's sizing (:class:`Infra`) the latest whole render at *quality* whose
    pre-flight gave any recorded: what a new pre-flight does not say again."""
    from sqlalchemy import select

    from app.models.video_studio import VideoRender

    rows = (
        await db.execute(
            select(VideoRender.preflight)
            .where(VideoRender.scene_id.is_(None), VideoRender.quality == quality, VideoRender.preflight.is_not(None))
            .order_by(VideoRender.created_at.desc())
            .limit(_MEASURED)
        )
    ).all()
    for (recorded,) in rows:
        items = recorded.get("infra") if isinstance(recorded, dict) else None
        if items:
            return {str(item.get("key")) for item in items if isinstance(item, dict)}
    return set()


async def record_preview_infra(db: Any, render_id: str, advice: Infra) -> None:
    """Keep *advice* on the scene preview *render_id* (``preflight.infra``): what to give its worker so its
    previews start at once (``render_process.reserve_advice``), or the preview lane a second worker
    (``preview_lane.lane_advice``), said only when the latest earlier preview that gave advice of its kind gave other
    numbers. Advice of another kind on the preview is kept. A preview is queued with no pre-flight; nothing else
    writes it."""
    import dataclasses

    from sqlalchemy import select

    from app.models.video_studio import VideoRender

    key = safe_uuid(render_id)
    row = await db.get(VideoRender, key) if key is not None else None
    if row is None:
        return
    rows = (
        await db.execute(
            select(VideoRender.preflight)
            .where(VideoRender.scene_id.is_not(None), VideoRender.id != row.id, VideoRender.preflight.is_not(None))
            .order_by(VideoRender.created_at.desc())
            .limit(_MEASURED)
        )
    ).all()
    before: set[str] = set()
    for (recorded,) in rows:
        items = recorded.get("infra") if isinstance(recorded, dict) else None
        of_kind = [item for item in items or [] if isinstance(item, dict) and item.get("kind") == advice.kind]
        if of_kind:
            before = {str(item.get("key")) for item in of_kind}
            break
    item = dataclasses.replace(advice, said=advice.key not in before)
    kept = [
        other
        for other in ((row.preflight or {}).get("infra") or [])
        if isinstance(other, dict) and other.get("kind") != advice.kind
    ]
    row.preflight = {**(row.preflight or {}), "infra": [*kept, item.as_dict()]}
    await db.commit()


def infra_of(render: Any) -> list[str]:
    """The advice on the platform's sizing *render*'s answer says: what its pre-flight - a scene
    preview's admission - was the first to need."""
    recorded = getattr(render, "preflight", None)
    items = recorded.get("infra") if isinstance(recorded, dict) else None
    return [str(item["text"]) for item in items or [] if isinstance(item, dict) and item.get("said") and item.get("text")]


def _measured_page_rate(kind: str, width: int, height: int, measured: Any) -> float | None:
    """The weighted pixel-frames a second one page with :data:`PAGE_CPUS` CPUs drew in *measured*; ``None`` if unread."""
    if not isinstance(measured, dict):
        return None
    try:
        frames, seconds = int(measured["frames"]), float(measured["seconds"])
        pages, cpus = max(1, int(measured["pages"])), max(1, int(measured["cpus"]))
    except (KeyError, TypeError, ValueError):
        return None
    if frames < MIN_MEASURED_FRAMES or seconds <= 0:
        return None
    pixels = frames / seconds * width * height * PIXEL_COST.get(kind, 1.0)
    return pixels / pages * PAGE_CPUS / _page_share(pages, cpus)


def _median(values: list[float]) -> float:
    ordered = sorted(values)
    return ordered[len(ordered) // 2]


def _measured_kind_rates(kind: str, width: int, height: int, measured: dict) -> dict[str, float]:
    """``{frame kind: weighted pixel-frames a second}`` one page with :data:`PAGE_CPUS` CPUs drew of each kind in
    *measured* (its ``kinds``): the kinds it drew :data:`_MIN_KIND_FRAMES` frames of at least."""
    try:
        pages, cpus = max(1, int(measured["pages"])), max(1, int(measured["cpus"]))
    except (KeyError, TypeError, ValueError):
        return {}
    share = _page_share(pages, cpus)
    found = {}
    for frame_kind, drawn in (measured.get("kinds") or {}).items():
        try:
            frames, seconds = int(drawn[0]), float(drawn[1])
        except (IndexError, TypeError, ValueError):
            continue
        if frame_kind in FRAME_KINDS and frames >= _MIN_KIND_FRAMES and seconds > 0:
            found[frame_kind] = frames / seconds * width * height * PIXEL_COST.get(kind, 1.0) * PAGE_CPUS / share
    return found


def measured_scene_rates(kind: str, width: int, height: int, measured: dict) -> dict[str, float]:
    """``{scene type: weighted pixel-frames a second}`` one page with :data:`PAGE_CPUS` CPUs drew of each scene type
    in *measured* (its ``scenes``, :func:`scene_class`): those it drew :data:`_MIN_KIND_FRAMES` frames of."""
    try:
        pages, cpus = max(1, int(measured["pages"])), max(1, int(measured["cpus"]))
    except (KeyError, TypeError, ValueError):
        return {}
    share = _page_share(pages, cpus)
    found = {}
    for name, drawn in (measured.get("scenes") or {}).items():
        try:
            frames, seconds = int(drawn[0]), float(drawn[1])
        except (IndexError, TypeError, ValueError):
            continue
        if frames >= _MIN_KIND_FRAMES and seconds > 0:
            found[str(name)] = frames / seconds * width * height * PIXEL_COST.get(kind, 1.0) * PAGE_CPUS / share
    return found


def measured_figure_rate(kind: str, width: int, height: int, measured: dict) -> float | None:
    """Weighted pixel-frames a second the figures stage drew on its one page in *measured*, ``None`` for one
    that drew fewer than :data:`_MIN_KIND_FRAMES` figure frames."""
    try:
        frames, seconds = int(measured.get("figure_frames") or 0), float(measured.get("figure_seconds") or 0.0)
    except (TypeError, ValueError):
        return None
    if frames < _MIN_KIND_FRAMES or seconds <= 0:
        return None
    return frames / seconds * width * height * PIXEL_COST.get(kind, 1.0)


def _in_regime(rate: float, measured_contended: bool, contended_target: bool) -> float:
    """*rate* measured on contended pages or not, as pages of the *contended_target* regime draw (:data:`DEFAULT_CONTENTION`)."""
    if measured_contended == contended_target:
        return rate
    return rate / DEFAULT_CONTENTION if measured_contended else rate * DEFAULT_CONTENTION


def stage_rates(measured: dict, width: int, height: int, duration: float) -> dict[str, float]:
    """What *measured*'s stages took per their unit: ``other`` per second of video (the media, the sound,
    storing), ``inserts`` per second of insert on screen, ``checks`` per frame of a million pixels."""
    stages = measured.get("stages") if isinstance(measured.get("stages"), dict) else None
    if not stages or duration <= 0:
        # A render that recorded no stages: its checks and its inserts' preparation are in its other seconds.
        return {"lumped": float(measured.get("other_seconds") or 0) / duration} if duration > 0 else {}
    found = {"other": sum(float(stages.get(name) or 0) for name in ("media", "audio", "mux", "store")) / duration}
    if float(measured.get("insert_seconds") or 0) > 0 and "inserts" in stages:
        found["inserts"] = float(stages["inserts"]) / float(measured["insert_seconds"])
    watched = sum(int(measured.get(key) or 0) for key in ("frames", "frames_reused", "frames_joined", "frames_inherited"))
    if watched and "checks" in stages:
        found["checks"] = float(stages["checks"]) / (watched * width * height / 1e6)
    spent = [
        (float(page) - float(drawn)) / int(segments)
        for segments, drawn, page in (measured.get("page_runs") or [])
        if int(segments) > 0 and float(page) >= float(drawn)
    ]
    if spent:
        found["segments"] = _median(spent) / (width * height / 1e6)
    return found


async def measured_rates(
    db: Any, quality: str, *, contended_pages: bool | None = None, shape: Shape | None = None
) -> Rates | None:
    """What one page drew a second in this platform's last whole renders, at *quality* when any was measured there.

    Each render records what its drawing measured (``VideoRender.measured``): its frames, in how long, on how many
    pages and CPUs - its frames by kind, its stages and its whole time. The median, so one render slowed
    by a busy worker never sets the estimate. With *contended_pages* (:func:`contended`), the renders drawn in the
    same regime when any was. ``None`` before any. The errors of the last estimates are read per worker shape
    (:func:`estimate_errors`).

    What a frame of each scene type costs (:attr:`Rates.scene_rates`) and the figures stage's rate are read
    from the last whole renders and the last scene previews alike, at any quality - per weighted pixel, each in the
    regime of the rate returned: the preview that drew a clip scene with a screen insert measured what the whole
    render will pay for it. With *shape*, a scene type whole renders at *quality* on its pages and CPUs measured is
    priced at what they measured (:attr:`Rates.scene_here`): what a render's estimate of a scene type learns
    from, before the median across qualities and previews.
    """
    from sqlalchemy import select

    from app.models.video_studio import JOB_READY, VideoRender

    previews = (
        await db.execute(
            select(
                VideoRender.quality,
                VideoRender.width,
                VideoRender.height,
                VideoRender.duration_ms,
                VideoRender.measured,
                VideoRender.scene_id,
            )
            .where(
                VideoRender.scene_id.is_not(None),
                VideoRender.status == JOB_READY,
                VideoRender.width.is_not(None),
                VideoRender.height.is_not(None),
            )
            .order_by(VideoRender.finished_at.desc())
            .limit(_MEASURED)
        )
    ).all()
    rows = (
        await db.execute(
            select(
                VideoRender.quality,
                VideoRender.width,
                VideoRender.height,
                VideoRender.duration_ms,
                VideoRender.measured,
            )
            .where(
                VideoRender.scene_id.is_(None),
                VideoRender.status == JOB_READY,
                VideoRender.width.is_not(None),
                VideoRender.height.is_not(None),
            )
            .order_by(VideoRender.finished_at.desc())
            .limit(_MEASURED)
        )
    ).all()
    #: ``(rate, its kinds' rates, contended, at this quality)`` of each render measured.
    drawn: list[tuple[float, dict, bool, bool]] = []
    stages: dict[str, list[float]] = {}
    #: ``(its scene types' rates, its figures stage's rate, contended, a whole render at this quality on *shape*)`` of
    #: each render and preview measured.
    by_scene: list[tuple[dict, float | None, bool, bool]] = []
    rendered_here = 0

    def regime_of(measured: dict) -> bool:
        try:
            return contended(max(1, int(measured.get("pages") or 1)), max(1, int(measured.get("cpus") or 1)))
        except (TypeError, ValueError):
            return False

    def on_shape(kind: str, measured: dict) -> bool:
        # Its pages and CPUs read as ``estimate_errors`` reads them: one malformed row never drops the history.
        if shape is None or kind != quality:
            return False
        try:
            return (int(measured.get("pages") or 0), int(measured.get("cpus") or 0)) == (shape.pages, shape.cpus)
        except (TypeError, ValueError):
            return False

    for kind, width, height, _duration_ms, measured, *rest in previews:
        if not (rest and isinstance(rest[0], str)) or not isinstance(measured, dict):
            continue
        scene_rates = measured_scene_rates(kind, width, height, measured)
        figure_rate = measured_figure_rate(kind, width, height, measured)
        if scene_rates or figure_rate:
            by_scene.append((scene_rates, figure_rate, regime_of(measured), False))
    for kind, width, height, duration_ms, measured, *_rest in rows:
        rendered_here += kind == quality
        if not isinstance(measured, dict):
            continue
        timed = (measured_scene_rates(kind, width, height, measured), measured_figure_rate(kind, width, height, measured))
        if read_back(measured):
            # Its insert frames read back drawn: its scene types left them out, its whole rate and its time did not.
            by_scene.append((*timed, regime_of(measured), on_shape(kind, measured)))
            continue
        rate = _measured_page_rate(kind, width, height, measured)
        if rate is None:
            continue
        regime = regime_of(measured)
        drawn.append((rate, _measured_kind_rates(kind, width, height, measured), regime, kind == quality))
        by_scene.append((*timed, regime, on_shape(kind, measured)))
        for name, value in stage_rates(measured, width, height, (duration_ms or 0) / 1000).items():
            stages.setdefault(name, []).append(value)

    def scene_terms(target: bool) -> dict:
        """The scene types' and the figures stage's rates of *by_scene*, each as pages of the *target* regime draw: a
        scene type the whole renders on *shape* measured at what they did, else at the median of them all."""
        rates: dict[str, list[float]] = {}
        here: dict[str, list[float]] = {}
        figures: list[float] = []
        for scene_rates, figure_rate, regime, local in by_scene:
            for name, value in scene_rates.items():
                rates.setdefault(name, []).append(_in_regime(value, regime, target))
                if local:
                    here.setdefault(name, []).append(_in_regime(value, regime, target))
            if figure_rate:
                figures.append(figure_rate)
        return {
            "scene_rates": {name: _median(here.get(name) or values) for name, values in rates.items()},
            "scene_measured": {name: len(here.get(name) or values) for name, values in rates.items()},
            "scene_here": {name: len(values) for name, values in here.items()},
            "figure_rate": _median(figures) if figures else None,
            "figure_measured": len(figures),
        }

    if not drawn:
        if not rendered_here and not by_scene:
            return None
        return Rates(DEFAULT_PAGE_RATE, rendered_here=rendered_here, **scene_terms(False))
    used = [item for item in drawn if item[3]] or drawn
    same = [item for item in used if item[2] == contended_pages] if contended_pages is not None else []
    used = same or used
    kind_rates = {
        frame_kind: _median(values)
        for frame_kind in FRAME_KINDS
        if (values := [item[1][frame_kind] for item in used if frame_kind in item[1]])
    }
    staged = bool(stages.get("other"))
    if staged:
        other, inserting, checking = (
            _median(stages["other"]),
            _median(stages["inserts"]) if stages.get("inserts") else DEFAULT_INSERT_PER_SECOND,
            _median(stages["checks"]) if stages.get("checks") else DEFAULT_CHECK_PER_MEGAPIXEL_FRAME,
        )
    else:
        # Only renders that lumped their stages: their checks and inserts are in the one rate, counted once.
        other = _median(stages["lumped"]) if stages.get("lumped") else DEFAULT_OTHER_PER_SECOND
        inserting = checking = 0.0 if stages.get("lumped") else None
    regime = sum(1 for item in used if item[2]) * 2 > len(used)
    return Rates(
        page_rate=_median([item[0] for item in used]),
        measured=len(used),
        measured_quality=sum(1 for item in used if item[3]),
        rendered_here=rendered_here,
        other_per_second=other,
        kind_rates=kind_rates,
        insert_per_second=DEFAULT_INSERT_PER_SECOND if inserting is None else inserting,
        check_per_megapixel_frame=DEFAULT_CHECK_PER_MEGAPIXEL_FRAME if checking is None else checking,
        segment_per_megapixel=_median(stages["segments"]) if stages.get("segments") else DEFAULT_SEGMENT_PER_MEGAPIXEL,
        contended=regime,
        **scene_terms(regime),
    )


async def publish_worker() -> None:
    """Publish, from the worker, what it holds a render to - at most every :data:`_PUBLISH_EVERY_SECONDS`."""
    from app.core.redis import redis_client
    from app.services.video_studio import cpu_lanes, resources
    from app.services.video_studio.compositor import known_capture
    from app.services.video_studio.render_process import memory_ceiling
    from app.services.video_studio.spec import QUALITY_PRESETS

    now = time.monotonic()
    capture = known_capture()
    if _published_at and now - _published_at[0] < _PUBLISH_EVERY_SECONDS and _published_at[1:] == [capture]:
        return
    # Published again as soon as its frame capture is probed or demoted: the pre-flight says it.
    _published_at[:] = [now, capture]
    capacity = {
        "memory_ceiling": memory_ceiling(),
        # An ultra render has a ceiling of its own: each quality's, for its pre-flight.
        "memory_ceilings": {quality: memory_ceiling(quality) for quality in QUALITY_PRESETS},
        "pages": int(settings.video_studio_render_pages),
        # The CPUs its quota grants, not the node's: what its pages share.
        "cpus": resources.cpu_budget(),
        # How its renders take their frames, as its probe settled (``compositor.FrameCapture``).
        "capture": capture.as_dict() if capture is not None else None,
        # Where its whole renders draw against its other jobs: a cgroup leaf, a raised priority, or neither.
        "render_lane": cpu_lanes.expected().as_dict(),
        "at": datetime.now(UTC).isoformat(),
    }
    try:
        await (await redis_client()).set(_WORKER_KEY, json.dumps(capacity), ex=_WORKER_TTL_SECONDS)
    except Exception as exc:  # the estimate says the ceiling is unknown
        logger.debug("Render worker capacity not published: %s", format_exception_message(exc))


async def worker_capacity() -> dict | None:
    """What the render worker last published (:func:`publish_worker`), ``None`` when none did in the last hour."""
    from app.core.redis import redis_client

    try:
        raw = await (await redis_client()).get(_WORKER_KEY)
    except Exception as exc:
        logger.debug("Render worker capacity not read: %s", format_exception_message(exc))
        return None
    try:
        value = json.loads(raw) if raw else None
    except ValueError:
        return None
    return value if isinstance(value, dict) else None


def _worker(capacity: dict, quality: str) -> tuple[int | None, int | None, int | None]:
    """``(pages, cpus, memory ceiling at quality)`` the worker published; ``None`` for what it did not."""
    configured = settings.video_studio_render_memory_mb * _MIB if settings.video_studio_render_memory_mb > 0 else None
    ceiling = capacity.get("memory_ceiling") if isinstance(capacity.get("memory_ceiling"), int) else configured
    ceilings = capacity.get("memory_ceilings")
    if isinstance(ceilings, dict) and isinstance(ceilings.get(quality), int):
        ceiling = ceilings[quality]
    pages = capacity.get("pages") if isinstance(capacity.get("pages"), int) else None
    cpus = capacity.get("cpus") if isinstance(capacity.get("cpus"), int) else None
    return pages, cpus, ceiling


#: The kinds of frame the previous render of the same video prices: a screen insert's frames, whose cost
#: depends on the insert - its matte, its track, its key - far more than on the platform.
VIDEO_KINDS = ("insert", "transition_insert")


async def video_kind_rates(db: Any, video_id: object) -> dict[str, float]:
    """``{kind: weighted pixel-frames a second}`` of each of :data:`VIDEO_KINDS` the newest whole render of
    *video_id* that drew :data:`_MIN_KIND_FRAMES` frames of it measured, at any quality: a rate is per
    weighted pixel, so a 1080p render of the video prices its 2160p one. ``{}`` when none did."""
    from sqlalchemy import select

    from app.models.video_studio import JOB_READY, VideoRender

    project_id = safe_uuid(str(video_id)) if video_id is not None else None
    if project_id is None:
        return {}
    rows = (
        await db.execute(
            select(VideoRender.quality, VideoRender.width, VideoRender.height, VideoRender.measured)
            .where(
                VideoRender.project_id == project_id,
                VideoRender.scene_id.is_(None),
                VideoRender.status == JOB_READY,
                VideoRender.width.is_not(None),
                VideoRender.height.is_not(None),
            )
            .order_by(VideoRender.finished_at.desc().nulls_last())
            .limit(VIDEO_RENDERS_READ)
        )
    ).all()
    found: dict[str, float] = {}
    for quality, width, height, measured in rows:
        if not isinstance(measured, dict):
            continue
        drawn = _measured_kind_rates(quality, width, height, measured)
        found.update({kind: drawn[kind] for kind in VIDEO_KINDS if kind in drawn and kind not in found})
        if len(found) == len(VIDEO_KINDS):
            break
    return found


def with_video_kinds(rates: Rates | None, own: dict[str, float]) -> Rates | None:
    """*rates* with the kinds the previous render of the video measured (*own*, :func:`video_kind_rates`) in place of
    the platform's."""
    if not own:
        return rates
    base = rates or Rates(DEFAULT_PAGE_RATE)
    return dataclasses.replace(
        base, kind_rates={**base.kind_rates, **own}, video_kinds=tuple(kind for kind in VIDEO_KINDS if kind in own)
    )


def with_own_render(rates: Rates | None, own: OwnRender | None) -> Rates | None:
    """*rates* with every scene type *own* drew at what it measured - in the regime of *rates* -, its stages and its
    figures stage at what it measured where it did, and its pages' parallel efficiency: what the next estimate
    of the same video at the same quality is made of, before the platform's median across videos."""
    if own is None:
        return rates
    base = rates or Rates(DEFAULT_PAGE_RATE)
    regime = bool(base.contended)
    stages = own.stages
    # A render that lumped its stages: the platform's terms stay.
    timed = "other" in stages
    return dataclasses.replace(
        base,
        scene_rates={**base.scene_rates, **{name: _in_regime(rate, own.contended, regime) for name, rate in own.scene_rates.items()}},
        own=own,
        other_per_second=stages["other"] if timed else base.other_per_second,
        insert_per_second=stages.get("inserts", base.insert_per_second) if timed else base.insert_per_second,
        check_per_megapixel_frame=stages.get("checks", base.check_per_megapixel_frame) if timed else base.check_per_megapixel_frame,
        segment_per_megapixel=stages.get("segments", base.segment_per_megapixel),
        figure_rate=own.figure_rate or base.figure_rate,
        figure_measured=max(base.figure_measured, 1) if own.figure_rate else base.figure_measured,
    )


async def rates_of(
    db: Any,
    quality: str,
    *,
    contended_pages: bool | None = None,
    video_id: object = None,
    shape: Shape | None = None,
    scene_types: Any = None,
) -> Rates | None:
    """What this platform measured at *quality* (:func:`measured_rates`, the scene types measured on *shape* first),
    its insert frames at what the previous render of *video_id* measured when it did (:func:`with_video_kinds`), and
    every scene type its last render at *quality* drew at what that render measured (:func:`with_own_render`);
    with *shape*, the errors of the last estimates made for it from the same kind of history as a video
    drawing *scene_types* is priced from (:func:`priced_from`, :func:`estimate_errors`). Each read under a savepoint:
    a history that cannot be read never fails the render or the start that asked."""
    rates = None
    try:
        async with db.begin_nested():
            rates = await measured_rates(db, quality, contended_pages=contended_pages, shape=shape)
    except Exception as exc:
        logger.info("Video render rates not read: %s", format_exception_message(exc))
    kinds: dict[str, float] = {}
    own = None
    if video_id is not None:
        try:
            async with db.begin_nested():
                kinds = await video_kind_rates(db, video_id)
        except Exception as exc:
            logger.info("Video render rates of its video not read: %s", format_exception_message(exc))
        try:
            async with db.begin_nested():
                own = await own_render(db, video_id, quality)
        except Exception as exc:
            logger.info("Video render measures of its video at %s not read: %s", quality, format_exception_message(exc))
    rates = with_own_render(with_video_kinds(rates, kinds), own)
    if shape is None:
        return rates
    source = priced_from(rates, scene_types)
    errors: tuple[Missed, ...] = ()
    try:
        async with db.begin_nested():
            errors = await estimate_errors(db, quality, shape=shape, costs_from=source)
    except Exception as exc:
        logger.info("Video render estimate errors not read: %s", format_exception_message(exc))
    return dataclasses.replace(rates or Rates(DEFAULT_PAGE_RATE), errors=errors, priced=source)


async def workers_present() -> int | None:
    """How many workers that draw whole renders are there now (``worker_presence.count``), ``None`` when unknown:
    what a render is split across past ``VIDEO_STUDIO_RENDER_SPLIT_MINUTES``. A worker that draws scene
    previews only (``VIDEO_STUDIO_RENDER_CONCURRENCY=0``) takes no range, and is not counted."""
    from app.services import worker_presence
    from app.services.video_studio.jobs import RENDER_ADAPTER

    try:
        return await worker_presence.count(claiming=RENDER_ADAPTER)
    except Exception as exc:  # unknown: a render is split only past the 4 h it may run
        logger.debug("Workers present not read: %s", format_exception_message(exc))
        return None


def document_kinds(spec: Any, document: dict, fps: float, quality: str | None = None) -> tuple[FrameKinds, float]:
    """``(its frames' kinds, seconds of insert on screen)`` of a plan's *document* drawn at *fps* and
    *quality* (its motion blur's sub-frames).

    A pre-flight's plan places no screen insert (``projects.estimate``): they are read off *spec*
    (:func:`insert_spans`); a render's own plan places them.
    """
    import dataclasses

    placed = any((scene.get("inserts") or []) for scene in document.get("scenes") or [])
    spans = None if placed else insert_spans(spec, document)
    # Each scene's type read off the spec, as the render times it.
    found = FrameKinds.of(document, spans, quality=quality, classes=scene_classes(spec))
    frames = math.ceil(float(document.get("duration") or 0.0) * fps)
    timed = dataclasses.replace(found, fps=float(fps), frames=frames)
    return timed, sum(b - a for a, b in timed.inserts)


async def _served(db: Any, effective: dict, **kwargs: Any) -> reuse_index.Served | None:
    try:
        # Read under a savepoint: an index that cannot be read counts every figure, never fails the start.
        async with db.begin_nested():
            return await reuse_index.served(db, effective, **kwargs)
    except Exception as exc:
        logger.info("Video render reuse index not read for a pre-flight: %s", format_exception_message(exc))
        return None


async def _estimate_at(
    db: Any,
    spec: Any,
    plan: Any,
    capacity: dict,
    quality: str,
    *,
    effective: dict | None = None,
    video_id: object = None,
    user: Any = None,
) -> Preflight:
    """The estimate of *spec*, planned as *plan*, rendered at *quality* on the worker that published *capacity*.

    With the video's *effective* spec, its *video_id* and the *user* who renders it, the figures the reuse index
    serves at *quality* are left out (``reuse_index.served``).
    """
    from app.services.video_studio.spec import QUALITY_PRESETS, output_size

    preset = QUALITY_PRESETS[quality]
    fps = spec.format.fps or preset.fps
    size = output_size(spec.format.aspect_ratio, preset.short_side)
    figures = {
        parsed.id or "": float(scene["end"]) - float(scene["start"])
        for scene, parsed in zip(plan.document["scenes"], spec.scenes, strict=False)
        if parsed.type == "figure"
    }
    reuse = None
    if figures and effective is not None and video_id is not None:
        reuse = await _served(db, effective, video_id=str(video_id), user=user, size=size, fps=fps, lengths=figures)
    pages, cpus, ceiling = _worker(capacity, quality)
    inserts = InsertLoad.of_spec(spec)
    drawn = layout(size, x264_preset=preset.x264_preset, ceiling=ceiling, pages=pages, cpus=cpus, inserts=inserts)
    kinds, insert_seconds = document_kinds(spec, plan.document, fps, quality)
    said = estimate(
        frames=math.ceil(plan.duration * fps),
        size=size,
        figure_seconds=sum(figures.values()),
        pages=pages,
        cpus=cpus,
        quality=quality,
        rate=await rates_of(
            db,
            quality,
            contended_pages=contended(drawn.pages, drawn.cpus),
            video_id=video_id,
            shape=Shape.of(drawn.pages, drawn.cpus, ceiling),
            scene_types={name for _start, _end, name in kinds.scenes},
        ),
        ceiling=ceiling,
        scenes=len(plan.document.get("scenes") or []),
        reuse=reuse or reuse_index.Served(figures=len(figures)),
        inserts=inserts,
        kinds=kinds,
        insert_seconds=insert_seconds,
        workers=await workers_present(),
    )
    return dataclasses.replace(said, capture=capture_words(capacity))


def capture_words(capacity: dict) -> str:
    """What the worker that published *capacity* said of its frame capture (``compositor.FrameCapture``)."""
    from app.services.video_studio.compositor import FrameCapture

    found = FrameCapture.of(capacity.get("capture"))
    return found.words() if found is not None else ""


async def highest_fitting(db: Any, spec: Any, plan: Any, capacity: dict, refused: Preflight, **reuse: Any) -> Preflight:
    """*refused* with the highest quality below it that fits this worker now offered in its last problem.

    What the worker needs comes first in every problem: the offer is the person's to take - an agent asks them,
    never renders at another quality on its own.     Nothing fits: *refused* as it is. *reuse* are
    :func:`_estimate_at`'s: each quality's own figures served. The time offered is priced against the worker's committed
    load (:func:`with_load`), as an estimate that fits is.
    """
    import dataclasses

    from app.services.video_studio import cpu_share
    from app.services.video_studio.spec import QUALITY_PRESETS

    lower = _SHARPEST_FIRST[_SHARPEST_FIRST.index(refused.quality) + 1 :] if refused.quality in _SHARPEST_FIRST else ()
    for quality in lower:
        said = await _estimate_at(db, spec, plan, capacity, quality, **reuse)
        if said.refuses:
            continue
        shared = cpu_share.Shared.of((await with_load(db, said)).load)
        seconds = shared.seconds if shared is not None else said.expected_seconds
        preset = QUALITY_PRESETS[quality]
        fits = {
            "quality": quality,
            "output": f"{preset.short_side}p{preset.fps}",
            "expected_seconds": round(seconds),
        }
        offer = (
            f"Meanwhile {quality} ({fits['output']}) fits this worker now, in about {_duration(seconds)} "
            f"(render_video quality='{quality}'): the person's choice, never made for them."
        )
        problems = [*refused.problems[:-1], f"{refused.problems[-1]} {offer}"]
        return dataclasses.replace(refused, problems=problems, fits=fits)
    return refused


async def with_load(db: Any, said: Preflight) -> Preflight:
    """*said* priced against its worker's committed load: the scene previews and recordings running or queued
    there, and the jobs of its own process running there (assignment runs, sub-agent tasks), at their measured
    CPU (``cpu_share.committed_load``), beside the CPUs its pages reserve, in the render lane the worker gives
    (``cpu_share.priced``, ``cpu_lanes``). As it is on an idle worker, or where the load cannot be read."""
    from app.services.video_studio import cpu_lanes, cpu_share, jobs, render_process

    found = await cpu_share.committed_load(db, preview_cpus=float(render_process.preview_cpus()))
    if not found:
        return said
    lane = cpu_lanes.Lane.of((await worker_capacity() or {}).get("render_lane"))
    shared = cpu_share.priced(
        said.expected_seconds,
        cpus=said.cpus,
        pages=said.pages,
        jobs=found,
        recording_slots=jobs.slots(jobs.TAKE_ADAPTER),
        lane=lane,
    )
    return dataclasses.replace(said, load=shared.as_dict())


async def preflight(db: Any, effective: dict, *, quality: str, video_id: object = None, user: Any = None) -> Preflight:
    """The estimate of a whole render of the *effective* spec at *quality*, before it is queued.

    A refusal offers the highest quality that fits this worker (:func:`highest_fitting`). With the video's
    *video_id* and the *user* who renders it, the figures the reuse index serves are left out.
    """
    from app.services.video_studio import preview, projects
    from app.services.video_studio.spec import parse_video_spec

    spec = parse_video_spec(effective)
    # Its narrations at the length they were spoken where they were: the frames the render will draw.
    voices = await preview.spoken_lengths(db, spec, user) if user is not None else {}
    plan = await projects.estimate(db, effective, voices=voices)
    capacity = await worker_capacity() or {}
    reuse = {"effective": effective, "video_id": video_id, "user": user}
    said = await _estimate_at(db, spec, plan, capacity, quality, **reuse)
    if said.refuses:
        said = await highest_fitting(db, spec, plan, capacity, said, **reuse)
    else:
        said = await with_load(db, said)
    import dataclasses

    if said.infra:
        # Said once: what the last pre-flight that gave advice on the sizing said is not said again.
        try:
            async with db.begin_nested():
                said = with_infra_said(said, await infra_said_before(db, quality))
        except Exception as exc:  # unread: said, as before
            logger.info("Video render advice said before not read: %s", format_exception_message(exc))
    narrated = sum(1 for scene in spec.scenes if scene.narration) if spec.voiceover.enabled else 0
    return dataclasses.replace(said, narrations=(len(voices), narrated))


async def limits(db: Any) -> dict[str, dict]:
    """What each quality renders on this platform's worker, said before a render is asked (``video_studio_capabilities``).

    ``output``, its ``pages`` on its ``cpus``, its ``memory_mb`` ceiling, the hours it may run and ``max_duration_s``,
    the longest video with no figure it renders within them - on one worker, or in up to ``max_ranges`` scene
    ranges each on a worker of its own - at the rate the pre-flight uses.
    """
    from app.services.video_studio.spec import QUALITY_PRESETS, output_size

    capacity = await worker_capacity() or {}
    most = max(1, int(settings.video_studio_render_max_ranges))
    said: dict[str, dict] = {}
    for quality, preset in QUALITY_PRESETS.items():
        pages, cpus, ceiling = _worker(capacity, quality)
        size = output_size("16:9", preset.short_side)
        rates = await rates_of(db, quality) or Rates(DEFAULT_PAGE_RATE)
        drawn = layout(size, x264_preset=preset.x264_preset, ceiling=ceiling, pages=pages, cpus=cpus)
        rate = frames_per_second(rates, size=size, quality=quality, pages=drawn.pages, cpus=drawn.cpus)
        drawing_per_second = preset.fps / max(rate, 1e-9)
        wall = wall_seconds(quality)
        longest = wall / (drawing_per_second + rates.other_per_second)
        if most > 1:
            # Its frames shared out between the ranges, the other stages the render's own.
            split = min(
                (RENDER_DEADLINE_SECONDS - RANGE_OVERHEAD_SECONDS) * most / drawing_per_second,
                RENDER_DEADLINE_SECONDS / max(rates.other_per_second, 1e-9),
            )
            longest = max(longest, split)
        said[quality] = {
            "output": f"{preset.short_side}p {preset.fps} fps",
            "pages": drawn.pages,
            "cpus": drawn.cpus,
            "memory_mb": ceiling // _MIB if ceiling else None,
            "max_render_hours": wall // 3600,
            "max_ranges": most,
            "max_duration_s": int(min(settings.video_studio_max_video_seconds, longest)),
            "frame_seconds": round(drawn.pages / max(rate, 1e-9), 2),
            "rate_from": rates.basis(quality),
        }
    return said


def scene_timings(measured: dict) -> list[tuple[str, int, float]]:
    """``[(scene type, frames, seconds a frame took a page)]`` of *measured*'s ``scenes``, slowest first."""
    found = []
    for name, drawn in (measured.get("scenes") or {}).items():
        try:
            frames, seconds = int(drawn[0]), float(drawn[1])
        except (IndexError, TypeError, ValueError):
            continue
        if frames > 0:
            found.append((str(name), frames, seconds / frames))
    return sorted(found, key=lambda item: -item[2])


def _expected_line(expected: dict) -> str:
    """The estimate a render keeps (:meth:`Preflight.as_dict`), with every input it was made of, beside its
    worker's committed load when it had one."""
    from app.services.video_studio.cpu_share import Shared

    shared = Shared.of(expected.get("load"))
    took = shared.headline() if shared is not None and shared.jobs else f"about {_duration(float(expected['expected_seconds']))}"
    said = f"The pre-flight expected {took}"
    inputs: list[str] = []
    if expected.get("frames"):
        size = expected.get("size") or []
        at = f" at {size[0]}x{size[1]}" if len(size) == 2 else ""
        inputs.append(f"{int(expected['frames']):,} frames{at}")
    if expected.get("pages"):
        each = f"{float(expected['frame_seconds']):.2f} s a frame on each of " if expected.get("frame_seconds") else ""
        pages, cpus = int(expected["pages"]), expected.get("cpus")
        on = f"{pages} page{'s' if pages != 1 else ''}"
        if cpus is not None:
            on += f" ({cpus} CPU{'s' if cpus != 1 else ''})"
        inputs.append(f"{each}{on}")
    if inputs:
        said += ": " + ", ".join(inputs)
    if expected.get("basis"):
        said += f" - {expected['basis']}"
    if expected.get("figures_reused"):
        said += f", {expected['figures_reused']} of its {expected.get('figures')} figures kept from an earlier render"
    said += "."
    slowest = expected.get("slowest")
    if slowest and expected.get("slowest_seconds"):
        # What it was quoted from - its slowest scene type, at what a page was expected to take for one.
        count = int((expected.get("scenes") or {}).get(slowest) or 0)
        said += (
            f" Its slowest scenes, {scene_words(str(slowest))} ({count:,} frames), were expected at "
            f"{float(expected['slowest_seconds']):.2f} s a frame on a page ({(expected.get('scene_basis') or {}).get(slowest) or UNMEASURED_SCENE})."
        )
    if unlearned := unlearned_words(str(expected.get("quality") or ""), str(expected.get("own_render") or ""), expected.get("unlearned_scenes")):
        said += " " + unlearned
    if int(expected.get("ranges") or 1) > 1:
        said += (
            f" Drawn in {expected['ranges']} scene ranges of about {_duration(float(expected.get('range_seconds') or 0))} "
            f"each, each on a worker of its own (about {_duration(float(expected.get('one_worker_seconds') or 0))} on one)."
        )
    if expected.get("cpus_needed"):
        said += (
            f" Within the {int(expected.get('cap_seconds') or RENDER_DEADLINE_SECONDS) // 3600} h a render is planned "
            f"in, a worker needs {expected['cpus_needed']} CPUs."
        )
    return said


def said_of(render: Any, *, compact: bool = False) -> str:
    """What ``get_video_jobs`` says of a render's estimate and of what it measured; empty for neither.

    What it drew is said beside what it reused: an attempt that resumed from the segments of an earlier
    one drew only the rest, and is never read as the whole render - which segments, who drew them and the parts
    of the digest they matched on (:func:`reused_line`); and a render that kept none says so.

    Once ``render_pace.PACE_SHARE`` of its frames are drawn, the pace they measured replaces the
    pre-flight's estimate (``render_pace.eta_of``); a finished render sets what a frame of each scene type took a page against what its
    estimate priced it at - what the next pre-flights price that scene type at.

    A pace under half the pre-flight's is explained (``render_pace.slow_words``) - while it draws, from
    what its render keeps on its ``preflight``; once drawn, from its ``measured``.

    Its estimate beside its worker's committed load is said beside an idle worker's, and once drawn the
    CPUs its pages got beside the jobs sharing its worker, with its time on an idle worker
    (``cpu_share.contention_words``) - what the next estimates are corrected by.

    *compact* says the segments it kept in one summary (:func:`reused_line`), for a compact answer.
    """
    from app.services.video_studio import cpu_share, render_pace

    expected = getattr(render, "preflight", None)
    measured = getattr(render, "measured", None)
    parts: list[str] = []
    eta = render_pace.eta_of(render)
    if eta is not None and eta["source"] == "measured" and (paced_line := render_pace.pace_line(eta, getattr(render, "started_at", None))):
        parts.append(paced_line)
    elif isinstance(expected, dict) and expected.get("expected_seconds") is not None:
        parts.append(_expected_line(expected))
    if eta is not None and isinstance(expected, dict) and (slow := render_pace.slow_words(expected.get("slow"))):
        parts.append(slow)
    if isinstance(measured, dict) and (measured.get("frames") or measured.get("frames_reused") or measured.get("frames_inherited")):
        drawn = int(measured.get("frames") or 0)
        seconds = float(measured.get("seconds") or 0)
        speed = f" in {math.ceil(seconds / 60)} min, {drawn / seconds:.2f} frames/s" if seconds > 0 and drawn else ""
        which = reused_line(measured.get("segments_reused"), compact=compact)
        kept = (
            f"; {int(measured['frames_reused']):,} more were kept from the segments an earlier attempt of this render "
            f"finished{f' ({which})' if which else ''}"
            if measured.get("frames_reused")
            else "; no frame was kept from an earlier attempt"
        )
        if measured.get("frames_joined"):
            kept += f"; {int(measured['frames_joined']):,} were drawn by the workers of its other scene ranges"
        if measured.get("frames_inherited"):
            kept += f"; {int(measured['frames_inherited']):,} were kept from the render it replaced ({inherited_scenes(measured)})"
        parts.append(f"It drew {drawn:,} frames on {measured.get('pages')} pages ({measured.get('cpus')} CPUs){speed}{kept}.")
    if isinstance(measured, dict) and (measured.get("figure_frames") or measured.get("figure_frames_reused")):
        parts.append(
            f"Its figures: {int(measured.get('figure_frames') or 0):,} frames drawn, "
            f"{int(measured.get('figure_frames_reused') or 0):,} served by drawings kept by an earlier render or attempt."
        )
    if read_back(measured):
        inserts = measured["insert_frames"]
        parts.append(
            f"Its screen inserts: {int(inserts.get('cached') or 0):,} frames read back as an earlier page, attempt or "
            f"render of this worker drew them, {int(inserts.get('drawn') or 0):,} drawn."
        )
    if isinstance(measured, dict) and (timed := scene_timings(measured)):
        # What a frame of each scene type took a page - what the next pre-flights weigh it by -, against what
        # its estimate priced it at.
        priced = expected.get("scene_raw_seconds") if isinstance(expected, dict) else None
        priced = priced if isinstance(priced, dict) else {}
        shown = timed[:4]
        listed = _joined([f"{scene_words(name)} {seconds:.2f} s ({frames:,} frames{_against(priced.get(name), seconds)})" for name, frames, seconds in shown])
        parts.append(f"A frame took a page, by scene type: {listed}.")
        if any(priced.get(name) for name, _frames, _seconds in shown):
            at = getattr(render, "quality", None)
            parts.append(
                f"The next pre-flights{f' at {at}' if at else ''} price each of these scene types at what this render "
                "measured: those of this video at once, those of other videos on the same worker shape."
            )
    if isinstance(measured, dict) and (slow := render_pace.slow_words(measured.get("pace"), finished=True)):
        parts.append(slow)
    if isinstance(measured, dict) and (
        shared := cpu_share.contention_words(measured.get("contention"), float(measured.get("total_seconds") or 0.0), expected)
    ):
        # The CPUs its pages got beside the jobs sharing its worker, and its time on an idle worker.
        parts.append(shared)
    error = estimate_error(expected, measured) if isinstance(measured, dict) else None
    if error is not None and isinstance(expected, dict) and expected.get("expected_seconds"):
        # What this estimate missed by, which the next estimates at this quality are corrected by - an idle
        # worker's estimate against its time on an idle worker.
        said, took = float(expected.get("idle_seconds") or expected["expected_seconds"]), error[1]
        on = " on an idle worker" if cpu_share.idle_equivalent(1.0, measured.get("contention")) < 1.0 else ""
        parts.append(
            f"It took {_duration(took)}{on} against the {_duration(said)} said ({(took - said) / said:+.0%}): the next "
            f"estimates at this quality on the same worker shape are corrected by the errors of the last {ERRORS_READ}."
        )
    return " ".join(parts)


def _against(said: Any, took: float) -> str:
    """``, against 1.20 s estimated: x4.51`` - a scene type's measure set against its estimate; empty without."""
    try:
        priced = float(said)
    except (TypeError, ValueError):
        return ""
    return f", against {priced:.2f} s estimated: x{took / priced:.2f}" if priced > 0 else ""
