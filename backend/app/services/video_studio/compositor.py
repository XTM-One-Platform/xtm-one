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

"""Drawing a timeline frame by frame in headless Chromium.

The compositor page (``compositor_page.py``) draws any frame of the timeline;
this module drives it:

* **One origin, answered here.** The page is loaded from
  ``https://studio.render/index.html`` and every request to that origin is
  fulfilled by :class:`FrameServer` - the frames of a recording or a clip, a
  slide or an image, the logo. Every other request is aborted, so nothing a
  scene shows can reach the network.
* **Frames decoded forward.** A recording's frames come from its mezzanine
  through an ffmpeg decoder read forward (``encoder.FrameDecoder``): the time
  maps are monotonic, so one pass serves a scene; a jump (a cut pause) opens
  the decoder again at the frame asked for.
* **Chunks in parallel.** The frames are cut into segments, and the segments
  into contiguous runs of about the same cost, each drawn by its own page and
  encoded by its own ffmpeg with the same settings, then joined without
  re-encoding (``encoder.concat``). A page done with its run takes what the
  others have left - a piece no page started, else the second half of the
  piece with the most work left, cut on a frame (``page_schedule``).
  The page is a pure function of time, so the pieces agree at their seams.
  The pages are as many as the worker's CPUs and its memory ceiling hold
  (``render_preflight.layout``): an x264 encoder at 2160p holds its
  lookahead whatever its threads.
* **Segments kept.** A page encodes its run as segments of
  :data:`SEGMENT_SECONDS`, each closed and named by its frames once whole,
  under :data:`SEGMENTS_DIR` with a digest of what they were drawn from:
  the document and every source, picture and screen insert the frame
  server reads, each file by what it holds and each insert by every field
  it is drawn by - its key among them (:func:`drawing_inputs`). A
  render stopped and run again in the same working directory (a stall
  resumed in place, ``render_process``) draws only the segments it had not
  finished; a digest that changed draws everything again. Each segment
  closed records the revision and the attempt that drew it
  (:data:`DRAWN_SUFFIX`), which the render's answer says of what it kept.
  Each segment closed is stored beside the render too, so an attempt on another worker -
  after a deploy - resumes from them (``render_segments``). Once every
  frame is encoded, the silent video is recorded with its drawing's digest
  (:data:`ENCODED_FILE`): a resume past the encode keeps it whole and
  only checks it again, never drawing or joining a frame.
* **Exact frames.** Each frame is drawn (``window.__xtm.render``), its images
  decoded, then captured with CDP ``Page.captureScreenshot`` at the device
  scale of the output (1 for 1080p from a 1920x1080 design canvas, 2 for 4K)
  and piped into the encoder.
* **Memory follows the screen, never the length.** A frame goes from
  the screenshot to the encoder's input before the next is drawn. A source's
  decoder opens when its scene first asks for a frame and closes once the
  scene has ended (:func:`release_schedule`), its stills dropped with it; a
  page never keeps more decoders open than the timeline shows sources at once
  (:func:`decoder_budget`: two for a crossfade), and the page drops the layer
  of a scene that has ended.
* **Screen inserts, warped here.** A scene's screen insert is asked as
  ``insert/<key>/<source frame>.png?w=&h=`` (and ``.glow.png``): its source
  frame - decoded like a scene's - is mapped onto its quad at the size the
  page draws it (``screen_insert``), in a thread, and the page places it.
  A pushed insert names its own second (``&o=``, milliseconds after
  it came in): its source is cut to the window the push frames then
  (``screen_insert.zoomed``). A matted insert names the footage
  frame under it (``&f=``) and the
  output frame it is drawn for (``&n=``): its matte reads that footage frame,
  decoded by the scene's own decoder, and each frame drawn is audited
  (``screen_insert.FrameAudit``); the audits and the worst frame of each
  failure are kept beside the segment they were drawn in (:data:`AUDIT_SUFFIX`),
  so a resumed render keeps them with its frames. The matte of a frame reads
  the :data:`MATTE_LOOKAHEAD` footage frames after it too, decoded
  ahead and kept for the page (``_SceneDecoder``'s recent frames); and the
  frames named as a screen insert's first, middle and last (``crops``) have
  the insert's region cut out of the captured frame, where the page placed
  it (``window.__xtm.insertRects``), at the render's resolution
  (:meth:`FrameServer.crop`), handed back with the audits. A frame of an
  insert is drawn once per worker: kept on its disk with its audit by what
  its pixels are drawn from (:func:`insert_drawn_by`, ``insert_frames``),
  and read back by any page, attempt or render that shows it again at the
  same size - the matte's window then read behind the frame as well as
  ahead of it, so a frame comes out the same whichever page draws it.
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import functools
import hashlib
import json
import logging
import math
import re
import shutil
import time
from collections import OrderedDict
from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

from app.core.utils import format_exception_message
from app.services.video_studio import (
    encoder,
    frame_owners,
    insert_frames,
    motion_design,
    page_schedule,
    render_pace,
    render_preflight,
    resources,
    screen_insert,
    timeline,
)
from app.services.video_studio.compositor_page import ORIGIN, page_html
from app.services.video_studio.screen_insert import InsertSource, host_size
from app.services.video_studio.spec import RenderPreset

logger = logging.getLogger(__name__)

#: A forward jump longer than this many frames reopens the decoder at the
#: target instead of decoding every frame in between.
_REOPEN_AFTER_FRAMES = 240
#: The decoded frames a scene decoder keeps behind its position: a matte reads two frames ahead of the one
#: the page shows and the page then asks for them.
RECENT_FRAMES = 4
#: How many footage frames past the one shown a matted insert's matte reads: the median's half window ...
MATTE_LOOKAHEAD = 2
#: ... and how many decoded footage frames a page keeps at the host's size: the one shown, the two read ahead, one back.
UNDER_FRAMES = 4
_FRAME_TIMEOUT_SECONDS = 60.0
#: The decoders a page keeps open at least: a crossfade shows two sources.
MIN_OPEN_DECODERS = 2
_VIDEO_PREFIXES = ("take/", "clip/", "backdrop/")


class RenderCancelledError(RuntimeError):
    """The render was cancelled while drawing."""


class RenderHandedOverError(Exception):
    """The render process closed its open segments at the frame each page reached and stopped drawing: its worker
    hands the render over (:func:`hand_over_now`). Never a cancel: the next attempt resumes from those segments."""


#: Set in a render process once its worker hands the render over (``render_process.supervise``, SIGUSR1): each page
#: closes the segment it draws at the frame it reached, stores it, and draws no more.
_HANDING_OVER = {"set": False}


def hand_over_now() -> None:
    """Close every open segment at the frame its page reached and stop drawing (:class:`RenderHandedOverError`)."""
    _HANDING_OVER["set"] = True


def handing_over() -> bool:
    return _HANDING_OVER["set"]


class ChromiumError(Exception):
    """The headless Chromium a render draws in failed it: a protocol call refused or past its timeout, a page
    or a browser gone. Nothing in the video causes it, so the render runs again (``render_retry``)."""


@dataclass
class FrameSource:
    """A scene's moving picture: a recording's mezzanine or a clip."""

    path: str
    fps: float


@dataclass
class CompositorAssets:
    #: ``"take/<scene id>"`` / ``"clip/<scene id>"`` / ``"backdrop/<scene id>"`` -> its media.
    sources: dict[str, FrameSource] = field(default_factory=dict)
    #: ``"image/<scene id>"`` -> a still (slide or image), ``"backdrop/<scene id>"``, and ``"logo"``.
    images: dict[str, str] = field(default_factory=dict)
    #: ``"insert/<scene id>.<n>"`` -> a screen insert's source, surface and glow.
    inserts: dict[str, InsertSource] = field(default_factory=dict)
    #: ``path -> what the file holds``: ``file:<id>`` for a file downloaded by its id, ``sha256:<digest>`` for one
    #: the render made (:func:`file_identity` reads and keeps the digest of one not named here).
    origins: dict[str, str] = field(default_factory=dict)


class _SceneDecoder:
    """One source decoded forward; the frames of *keep* ``(first, last)`` kept once decoded (a looping end).

    A scene that loops its footage's last second asks for those frames again
    and again, two at once while it crossfades: kept, they are served
    without decoding the source backward.
    """

    def __init__(self, source: FrameSource, keep: tuple[int, int] | None = None) -> None:
        self.source = source
        self.keep = keep
        self._kept: dict[int, bytes] = {}
        #: The last frames decoded, by index: a matte read two frames ahead comes back to them at no cost.
        self._recent: dict[int, bytes] = {}
        self._decoder: encoder.FrameDecoder | None = None
        self._frames: Any = None
        self.index = -1
        self.current: bytes | None = None

    async def _open(self, index: int) -> None:
        await self.close()
        start = max(index, 0) / max(self.source.fps, 1.0)
        self._decoder = encoder.FrameDecoder(self.source.path, start=start)
        await self._decoder.open()
        self._frames = self._decoder.frames()
        self.index = index - 1

    async def frame(self, index: int) -> bytes:
        if index in self._kept:
            return self._kept[index]
        if index in self._recent:
            return self._recent[index]
        if self._decoder is None or index < self.index or index > self.index + _REOPEN_AFTER_FRAMES:
            await self._open(index)
        while self.index < index:
            try:
                self.current = await anext(self._frames)
            except StopAsyncIteration:
                break
            self.index += 1
            if self.current is not None:
                self._recent[self.index] = self.current
                for old in [n for n in self._recent if n < self.index - RECENT_FRAMES]:
                    self._recent.pop(old)
            if self.keep is not None and self.keep[0] <= self.index <= self.keep[1] and self.current is not None:
                self._kept[self.index] = self.current
        if self.current is None:
            raise RuntimeError(f"no frame {index} in {Path(self.source.path).name}")
        return self.current

    async def close(self) -> None:
        if self._decoder is not None:
            await self._decoder.close()
        self._decoder = None
        self._frames = None
        self._kept.clear()
        self._recent.clear()


class FrameServer:
    """Answers the compositor page's requests to its own origin, for one page.

    At most *max_open* decoders are open at once: past it, the one used least
    recently is closed (it reopens at its frame if its scene asks again).
    """

    def __init__(
        self,
        assets: CompositorAssets,
        html: str,
        *,
        max_open: int = MIN_OPEN_DECODERS,
        keep: dict[str, tuple[int, int]] | None = None,
        evidence_dir: Path | None = None,
        crops: dict[str, Iterable[int]] | None = None,
        guard: dict[int, list[str]] | None = None,
        presence: dict[int, list[str]] | None = None,
        whole: dict[str, tuple[int, int]] | None = None,
    ) -> None:
        self.assets = assets
        self.html = html.encode("utf-8")
        self.max_open = max(1, max_open)
        #: ``source key -> (first, last)`` frames a looping scene plays again (``timeline.loop_frames``).
        self.keep = dict(keep or {})
        #: The most decoders this page had open at once.
        self.peak_open = 0
        self._decoders: OrderedDict[str, _SceneDecoder] = OrderedDict()
        # One decode at a time per page: a decoder reads forward, and closing
        # one (released or evicted) must never race a read of it.
        self._lock = asyncio.Lock()
        self._images: dict[str, bytes] = {}
        #: A screen insert's last frame, ``key -> ((its drawing's folder, its frame's name), png, glow png, its audit,
        #: read back)`` (the frame's name holds how much of it shows), and an image insert's picture.
        self._insert_frames: dict[str, tuple] = {}
        #: A matted insert's last frame asked, ``key -> (the output frame it named, its audit, its evidence pictures)``:
        #: under motion blur a frame's centre sub-frame asks nothing when it shows the frame a sub-frame before it asked
        #: (the same address), and is audited by it (:meth:`centre_shown`).
        self._asked: dict[str, tuple[int, Any, dict]] = {}
        self._insert_pictures: dict[str, Any] = {}
        #: A matted insert's surface on its first footage frame, by key and host size; a still footage's pixels.
        self._surfaces: dict[tuple[str, int, int], Any] = {}
        self._stills: dict[tuple[str, int, int], Any] = {}
        #: A matted insert's frame-to-frame state (``screen_insert.Matting``: its bezel lock, its last mattes),
        #: by key and host size: a page draws its frames in order.
        self._matting: dict[tuple[str, int, int], Any] = {}
        #: A static keyed insert's filled quad (``screen_insert.Fill``), by key and host size: read once off its
        #: first footage frame, drawn on every frame - nothing keyed frame by frame.
        self._fills: dict[tuple[str, int, int], Any] = {}
        #: A static unkeyed insert's quad locked to its bezel (``screen_insert.Lock``), by key and host size: read once
        #: off its first footage frame like a fill.
        self._locks: dict[tuple[str, int, int], Any] = {}
        #: Each insert's frames on this worker's disk (``insert_frames``), by key: drawn once by any page or render.
        self._kept: dict[str, insert_frames.Frames] = {}
        #: The insert frames this page read back from the worker's disk, and those it drew; and of the output frame
        #: being drawn (:meth:`start_frame`), its insert frames read back and drawn.
        self.insert_cached = 0
        self.insert_drawn = 0
        self.frame_read = 0
        self.frame_drawn = 0
        #: The footage frames last decoded under a matted insert, ``((source, frame, w, h), pixels)``:
        #: every insert of the frame the page shows reads them, never a copy of its own, and the frames read ahead for
        #: a matte's window are the next frames shown.
        self._under: list[tuple[tuple, Any]] = []
        #: Where the frames a failing audit shows are written; ``None``: none is.
        self.evidence_dir = evidence_dir
        #: ``key -> output frame -> audit`` of the matted inserts drawn, until :meth:`take_audits` hands them on.
        self.audits: dict[str, dict[int, Any]] = {}
        #: ``(key, kind) -> (how bad, output frame, file)`` of the worst frame kept as a failure's evidence.
        self.evidence: dict[tuple[str, str], tuple[float, int, str]] = {}
        #: ``key -> the output frames`` whose insert region is kept as a 100 % crop, and ``key -> frame ->
        #: file`` of the crops taken, until :meth:`take_audits` hands them on.
        self.crops_wanted: dict[str, set[int]] = {key: set(frames) for key, frames in (crops or {}).items()}
        self.crops: dict[str, dict[int, str]] = {}
        #: Each crop's box, 0-1 of the frame (``key -> frame -> [x0, y0, x1, y1]``), and the size of the frame the crops
        #: of an insert were cut from: what the answer says they are 100 % of.
        self.crop_boxes: dict[str, dict[int, list[float]]] = {}
        self.crop_frame: dict[str, list[int]] = {}
        #: The output frames the render's guard reads (``insert_checks.guard_frames``) and the keyed inserts drawn
        #: on each; where the page placed each insert on them (``key -> frame -> [x0, y0, x1, y1, shown]``, 0-1 of the
        #: frame, *shown* how much of it the frame shows) and its scene's frame drawn alone (``key -> frame -> {file,
        #: box}``, :meth:`keep_alone`), until :meth:`take_audits` hands them on.
        self.guard: dict[int, list[str]] = {int(frame): list(keys) for frame, keys in (guard or {}).items()}
        self.placements: dict[str, dict[int, list[float]]] = {}
        self.alone: dict[str, dict[int, dict]] = {}
        #: The output frames the presence gate reads once encoded (``insert_checks.presence_frames``): every frame
        #: an insert's scene is painted on, where the page placed it is kept as on a guard's.
        self.presence: dict[int, list[str]] = {int(frame): list(keys) for frame, keys in (presence or {}).items()}
        #: ``key -> output frame -> [[x0, y0, x1, y1], ...]`` (0-1 of the frame) on a presence frame: what its scene draws
        #: over the insert there (the page's ``insertRects``) - its text, its chips, the subtitles -, which the gate
        #: leaves out of the quad it reads - until :meth:`take_audits` hands them on as ``over``.
        self.covers: dict[str, dict[int, list[list[float]]]] = {}
        #: ``key -> output frame -> {"black": file, "white": file}`` (:meth:`keep_blend`): on a presence frame that
        #: shows an insert blended - a transition's frame -, its scene painted black and white, what the gate unblends
        #: the encoded frame with - until :meth:`take_audits` hands them on.
        self.blends: dict[str, dict[int, dict]] = {}
        #: ``key -> [first, end)`` output frames where an insert that runs to its scene's end shows whole
        #: (``timeline.whole_frames``), and ``key -> output frame -> what was wrong there`` (:meth:`overran`): asked past
        #: its scene's painted end, or partly shown inside it - until :meth:`take_audits` hands them on.
        self.whole: dict[str, tuple[int, int]] = {key: (int(a), int(b)) for key, (a, b) in (whole or {}).items()}
        self.overruns: dict[str, dict[int, dict]] = {}
        #: Whether the page draws a sub-frame of motion blur away from its frame's centre, set by the
        #: driver before each one: what it asks then is drawn, never audited nor judged - a frame is its centre's, and
        #: the shutter of a frame next to a cut reaches into the scene beside it (``frame_owners``).
        self.shutter = False

    @property
    def open_decoders(self) -> int:
        return len(self._decoders)

    def start_frame(self) -> None:
        """An output frame begins: what its screen inserts are read back or drawn as is counted from now."""
        self.frame_read = self.frame_drawn = 0

    @property
    def frame_reused(self) -> bool:
        """Whether every screen-insert frame the output frame showed was read back drawn, and it showed one."""
        return self.frame_read > 0 and self.frame_drawn == 0

    async def _decoder(self, key: str, source: FrameSource) -> _SceneDecoder:
        decoder = self._decoders.get(key)
        if decoder is not None:
            self._decoders.move_to_end(key)
            return decoder
        while len(self._decoders) >= self.max_open:
            _key, evicted = self._decoders.popitem(last=False)
            await evicted.close()
        decoder = self._decoders[key] = _SceneDecoder(source, self.keep.get(key))
        self.peak_open = max(self.peak_open, len(self._decoders))
        return decoder

    async def handle(self, route: Any) -> None:
        url = route.request.url
        if not url.startswith(ORIGIN + "/"):
            await route.abort("blockedbyclient")
            return
        parsed = urlparse(url)
        path = parsed.path.lstrip("/")
        try:
            if path == "index.html":
                await route.fulfill(status=200, body=self.html, content_type="text/html; charset=utf-8")
                return
            parts = path.split("/")
            if len(parts) == 3 and parts[0] == "insert" and parts[2].endswith(".png"):
                await self._insert(route, f"insert/{parts[1]}", parts[2], parse_qs(parsed.query))
                return
            if len(parts) == 3 and parts[0] in ("take", "clip", "backdrop") and parts[2].endswith(".jpg"):
                key = f"{parts[0]}/{parts[1]}"
                source = self.assets.sources.get(key)
                if source is None:
                    await route.fulfill(status=404, body=b"")
                    return
                index = int(parts[2][:-4])
                async with self._lock:
                    data = await (await self._decoder(key, source)).frame(index)
                await route.fulfill(
                    status=200, body=data, content_type="image/jpeg", headers={"Cache-Control": "no-store"}
                )
                return
            image_key = path
            image_path = self.assets.images.get(image_key)
            if image_path:
                data = self._images.get(image_key)
                if data is None:
                    data = await asyncio.to_thread(Path(image_path).read_bytes)
                    self._images[image_key] = data
                await route.fulfill(status=200, body=data, content_type=_image_type(image_path))
                return
            await route.fulfill(status=404, body=b"")
        except Exception as exc:
            logger.debug("Compositor request %s failed: %s", path, format_exception_message(exc))
            with contextlib.suppress(Exception):  # the page may be closing
                await route.fulfill(status=500, body=b"")

    async def _insert(self, route: Any, key: str, name: str, query: dict[str, list[str]]) -> None:
        """A frame of a screen insert, mapped onto its quad at the size the page asks, or its glow."""
        insert = self.assets.inserts.get(key)
        size = host_size((query.get("w") or [""])[0], (query.get("h") or [""])[0])
        glow = name.endswith(".glow.png")
        stem = name[: -len(".glow.png")] if glow else name[: -len(".png")]
        if insert is None or size is None or not stem.isdigit():
            await route.fulfill(status=404, body=b"")
            return
        index = int(stem) if insert.kind == "video" else 0
        # A tracked insert is drawn into its quad on the footage frame the page shows; a matted one
        # reads that frame, and says which output frame it is drawn for.
        asked = (query.get("f") or [""])[0]
        footage = int(asked) if (insert.track is not None or insert.footage is not None) and asked.isdigit() else None
        shown = (query.get("n") or [""])[0]
        output = int(shown) if shown.isdigit() and not self.shutter else None
        # A pushed insert names its own second, in milliseconds after it came in: the push's scale then.
        into = (query.get("o") or [""])[0]
        seconds = int(into) / 1000.0 if insert.zoom is not None and into.isdigit() else None
        scale = round(insert.zoom.scale_at(seconds), 4) if seconds is not None else 1.0
        # A keyed insert held on its surface names how much of it shows, in permille: the rest of the surface
        # is painted in the bezel's colour.
        shows = (query.get("v") or [""])[0]
        visibility = min(int(shows), 1000) / 1000.0 if shows.isdigit() else 1.0
        if output is not None and key in self.whole:
            # An insert that runs to its scene's end: never asked on a frame its scene does not own,
            # never partly shown once in - the bezel's colour there is the insert gone from its scene.
            first, end = self.whole[key]
            if output >= end:
                self.overran([{"src": key, "frame": output, "why": "painted_past"}])
            elif output >= first and visibility < 1.0:
                self.overran([{"src": key, "frame": output, "why": "partly_shown", "visibility": visibility}])
        async with self._lock:
            # Its scene and its insert (the key), how it is drawn (its drawing's folder, ``insert_drawn_by``), the
            # frame's time in that scene's time base (its source's frame and its footage's), the host's size, the push
            # and how much of it shows: a layer is never served for anything else.
            frames = self._frames_of(key, insert)
            name = insert_frames.frame_name(index, footage, size, scale, visibility)
            drawn_as = (frames.folder.name, name)
            # The page asks for a frame and its glow one after the other: the last one drawn is kept, and a still
            # drawn again for another output frame is audited for that frame too.
            cached = self._insert_frames.get(key)
            if cached is not None and cached[0] == drawn_as:
                drawn, bloom, audit, read_back, pictures = cached[1:]
                if output is not None and audit is not None:
                    self.audits.setdefault(key, {})[output] = audit
                if read_back:
                    self.frame_read += 1
            else:
                # Drawn before on this worker - by another page, an attempt before, another render at this size -:
                # read back with its audit, nothing decoded or composed.
                kept = await asyncio.to_thread(frames.get, name, evidence=self.evidence_dir is not None)
                if kept is not None:
                    self.insert_cached += 1
                    self.frame_read += 1
                    drawn, bloom, pictures = kept.drawn, kept.glow, kept.evidence
                    audit = screen_insert.FrameAudit.from_dict(kept.audit) if kept.audit is not None else None
                    if audit is not None and output is not None:
                        await self._audited(key, output, audit, pictures)
                else:
                    drawn, bloom, audit, pictures = await self._draw_insert(
                        key, insert, index, footage, size, seconds, output, frames, name, visibility
                    )
                self._insert_frames[key] = (drawn_as, drawn, bloom, audit, kept is not None, pictures)
            if shown.isdigit() and audit is not None:
                self._asked[key] = (int(shown), audit, pictures)
        body = bloom if glow else drawn
        if body is None:
            await route.fulfill(status=404, body=b"")
            return
        await route.fulfill(status=200, body=body, content_type="image/png", headers={"Cache-Control": "no-store"})

    def _matting_of(self, key: str, size: tuple[int, int]) -> screen_insert.Matting:
        """A matted insert's frame-to-frame state at *size* on this page, made the first time it is asked."""
        state = self._matting.get((key, *size))
        if state is None:
            state = self._matting[(key, *size)] = screen_insert.Matting()
        return state

    def _frames_of(self, key: str, insert: InsertSource) -> insert_frames.Frames:
        """*insert*'s frames on this worker's disk, keyed by what its pixels are drawn from (:func:`insert_drawn_by`)."""
        frames = self._kept.get(key)
        if frames is None:
            frames = self._kept[key] = insert_frames.Frames(insert_frames.folder_key(insert_drawn_by(insert, self.assets)))
        return frames

    async def _draw_insert(
        self,
        key: str,
        insert: InsertSource,
        index: int,
        footage: int | None,
        size: tuple[int, int],
        seconds: float | None,
        output: int | None,
        frames: insert_frames.Frames,
        name: str,
        visibility: float = 1.0,
    ) -> tuple[bytes, bytes | None, Any, dict[str, Any]]:
        """Frame *index* of *insert* composed over footage frame *footage* (``screen_insert.compose``) with *visibility*
        of it shown, audited for output frame *output* when matted, and kept on the worker's disk as *name*:
        ``(png, glow png, audit, its evidence pictures)``."""
        found = insert.footage
        behind: list[tuple[int, Any]] = []
        if found is not None and found.kind == "video" and footage is not None:
            # The frames before the one shown that this page has not read (it starts here, or its last frames were
            # read back drawn), decoded first: the matte's window is the same whichever page draws the frame.
            behind = await self._footage_behind(insert, footage, size, self._matting_of(key, size))
        if insert.kind == "video":
            data = await (await self._decoder(key, FrameSource(insert.path, insert.fps))).frame(index)
            picture = await asyncio.to_thread(screen_insert.decode, data)
        else:
            picture = self._insert_pictures.get(key)
            if picture is None:
                picture = self._insert_pictures[key] = await asyncio.to_thread(screen_insert.read_picture, insert.path)
        if seconds is not None:
            picture = await asyncio.to_thread(screen_insert.zoomed, picture, insert.zoom, seconds)
        under, surface, fill, lock = await self._matte_of(key, insert, footage, size)
        state = self._matting_of(key, size) if under is not None else None
        # The frames after the one shown: the matte's median window is centred on it.
        window = behind + await self._footage_ahead(insert, footage, size) if under is not None else []
        evidence = self.evidence_dir is not None
        layer = await asyncio.to_thread(
            screen_insert.compose,
            insert,
            picture,
            size,
            footage,
            footage=under,
            surface=surface,
            fill=fill,
            lock=lock,
            evidence=evidence,
            state=state,
            footage_ahead=window,
            visibility=visibility,
        )
        audit = None
        pictures: dict[str, Any] = {}
        if layer is None:
            empty = screen_insert.png(_empty_pixel())
            drawn, bloom = empty, (empty if insert.glow > 0 else None)
        else:
            drawn, bloom = await asyncio.to_thread(layer.pngs)
            if under is not None:
                audit = layer.audit
                pictures = layer.pictures()
                if output is not None:
                    await self._audited(key, output, audit, pictures)
        self.insert_drawn += 1
        self.frame_drawn += 1
        await asyncio.to_thread(frames.put, name, drawn, bloom, audit, pictures, evidenced=evidence)
        return drawn, bloom, audit, pictures

    async def _footage_behind(self, insert: InsertSource, frame: int, size: tuple[int, int], state: Any) -> list[tuple[int, Any]]:
        """The footage frames before *frame* its matte's median window reaches, at *size*, that *state* has not read:
        never before the frame the insert comes in on, as a page drawing on from it would have none."""
        found = insert.footage
        behind = []
        for other in range(frame - MATTE_LOOKAHEAD, frame):
            if other < max(insert.first_frame, 0) or other in state.raw:
                continue
            try:
                behind.append((other, await self._footage_pixels(found, other, size)))
            except RuntimeError:  # a frame the footage does not have: the window is what is left of it
                continue
        return behind

    async def _footage_frame(self, insert: InsertSource, frame: int | None, size: tuple[int, int]) -> Any:
        """The footage under a matted insert at *size* (``H x W x 3`` uint8): the frame the page shows, or its still."""
        found = insert.footage
        if found is None:
            return None
        if found.kind != "video":
            cached = self._stills.get((found.path, *size))
            if cached is None:
                cached = self._stills[(found.path, *size)] = await asyncio.to_thread(_still_pixels, found.path, size)
            return cached
        if frame is None:
            return None
        return await self._footage_pixels(found, frame, size)

    async def _footage_pixels(self, found: Any, frame: int, size: tuple[int, int]) -> Any:
        """One footage frame at *size*, decoded by the scene's own decoder; the last few kept (a matte reads
        the frames ahead of the one shown, which the page shows next)."""
        held = (found.key or found.path, frame, *size)
        for key, pixels in self._under:
            if key == held:
                return pixels
        source = self.assets.sources.get(found.key or "") or FrameSource(found.path, found.fps)
        data = await (await self._decoder(found.key or f"footage/{found.path}", source)).frame(frame)
        pixels = await asyncio.to_thread(_frame_pixels, data, size)
        self._under.append((held, pixels))
        del self._under[:-UNDER_FRAMES]
        return pixels

    async def _footage_ahead(self, insert: InsertSource, frame: int | None, size: tuple[int, int]) -> list[tuple[int, Any]]:
        """The :data:`MATTE_LOOKAHEAD` footage frames after *frame* at *size*, ``(frame, pixels)`` each, as far as
        the footage goes: the matte's median window reaches past the frame shown."""
        found = insert.footage
        if found is None or found.kind != "video" or frame is None:
            return []
        ahead = []
        for offset in range(1, MATTE_LOOKAHEAD + 1):
            try:
                ahead.append((frame + offset, await self._footage_pixels(found, frame + offset, size)))
            except RuntimeError:  # the footage ends here: the window is what is left of it
                break
        return ahead

    async def _matte_of(self, key: str, insert: InsertSource, frame: int | None, size: tuple[int, int]) -> tuple[Any, Any, Any, Any]:
        """``(footage under the insert, its surface's reference, its fill, its lock)`` for a matted insert; ``None``
        each otherwise.

        An unkeyed insert reads its surface's reference (``screen_insert.Surface``: what enters through its rim is in
        front) and, untracked, its quad locked to its bezel (``screen_insert.static_lock``); a keyed one that is not
        tracked reads its filled quad (``screen_insert.static_fill``), and keys nothing frame by frame - all
        read once per host size, off its first footage frame, the reference off its plate too (its clean
        surface: :func:`plate_footage`); a tracked keyed insert neither: its key is read frame by frame.
        """
        under = await self._footage_frame(insert, frame, size)
        if under is None:
            return None, None, None, None
        if insert.key is not None and insert.track is not None:
            return under, None, None, None
        held = (key, *size)
        surface, fill, lock = self._surfaces.get(held), self._fills.get(held), self._locks.get(held)
        wants_fill = insert.key is not None and held not in self._fills
        wants_lock = insert.key is None and insert.track is None and held not in self._locks
        if surface is None or wants_fill or wants_lock:
            first = await first_footage(insert, size) if insert.footage.kind == "video" else under
            if surface is None:
                # Its clean surface: the frame it comes in on and the rest of its plate, what passes in front
                # of them taken out.
                readings = await plate_footage(insert, size, first)
                surface = self._surfaces[held] = await asyncio.to_thread(screen_insert.Surface.of_plate, readings)
            if wants_fill:
                fill = self._fills[held] = await asyncio.to_thread(screen_insert.static_fill, insert, first, size)
            if wants_lock:
                lock = self._locks[held] = await asyncio.to_thread(screen_insert.static_lock, insert, first, size)
        return under, surface, fill, lock

    async def _audited(self, key: str, output: int, audit: Any, pictures: dict[str, Any]) -> None:
        """Record *audit* for output frame *output*; a failing frame worse than the one kept becomes the evidence:
        *pictures* (``kind -> PIL image`` as drawn, or the file ``insert_frames`` kept it as) of ``proof``,
        ``residual_crop``, ``halo_crop``, ``foreground_crop``, ``rim_crop``, ``through_crop`` and - the key's hue past
        the quad shown on the whole frame - ``spill_frame``."""
        self.audits.setdefault(key, {})[output] = audit
        proof = pictures.get("proof")
        if self.evidence_dir is None or proof is None:
            return
        for kind, badness in (
            ("covered", audit.covered if audit.covers_foreground else None),
            ("edge_line", audit.edge_line if audit.draws_edge_line else None),
            ("through", audit.through if audit.through_foreground else None),
            ("matte_halo", audit.matte_halo if audit.matte_haloed else None),
            ("ghost", 1.0 - audit.front_confidence if audit.ghosted and audit.front_confidence is not None else None),
            ("spill", audit.key_spill if audit.spills_key else None),
            ("logo", 1.0 - audit.logo_visible if audit.logo_lost else None),
            (
                "colour",
                screen_insert.colour_badness(audit.black_lift, audit.hue_rotation, audit.cast)
                if audit.colour_shifted
                else None,
            ),
            ("alignment", audit.alignment if audit.misaligned else None),
            ("residual", audit.residual_key if audit.keyed_residue else None),
            ("soft", 1.0 - audit.sharpness if audit.soft else None),
            ("halo", audit.halo if audit.haloed else None),
            ("rim", audit.rim if audit.rimmed else None),
            ("flicker", 1.0 - audit.edge_hold if audit.flickers else None),
            ("cropped", audit.cropped if audit.crops else None),
        ):
            if badness is None or badness <= self.evidence.get((key, kind), (-1.0, 0, ""))[0]:
                continue
            path = self.evidence_dir / f"{_slug_key(key)}-{kind}-{output:07d}.jpg"
            # The key past the quad is shown on the whole frame: what the box's proof never holds.
            shown = pictures.get("spill_frame") if kind == "spill" and pictures.get("spill_frame") is not None else proof
            await asyncio.to_thread(_save_evidence, shown, path)
            self.evidence[(key, kind)] = (badness, output, str(path))
            # The side where most of a residual key sits, the worst stretch of a halo, the insert drawn
            # over what stands in front, the worst stretch of a grey rim, the source seen through what
            # stands in front: unmarked and lossless, at 100 %.
            for found, crop_kind in (
                ("residual", "residual_crop"),
                ("halo", "halo_crop"),
                ("covered", "foreground_crop"),
                ("edge_line", "foreground_crop"),
                ("rim", "rim_crop"),
                ("through", "through_crop"),
            ):
                crop = pictures.get(crop_kind)
                if kind == found and crop is not None:
                    strip = self.evidence_dir / f"{_slug_key(key)}-{crop_kind}-{output:07d}.png"
                    await asyncio.to_thread(_save_evidence, crop, strip, "PNG")
                    self.evidence[(key, crop_kind)] = (badness, output, str(strip))

    def overran(self, found: Iterable[dict]) -> None:
        """Keep what a frame did to an insert that runs to its scene's end: the page's ``overruns()`` - its scene
        painted at or past its painted end - and what this server was asked (:meth:`_insert`), by insert and frame."""
        for record in found:
            key, frame = record.get("src"), record.get("frame")
            if not key or not isinstance(frame, int | float):
                continue
            kept = self.overruns.setdefault(str(key), {}).setdefault(int(frame), {})
            kept.update({name: value for name, value in record.items() if name not in ("src", "frame")})

    def unaudited(self, frame: int) -> bool:
        """Whether a matted insert was asked for output *frame* by a sub-frame of motion blur and not yet audited for it:
        the frame's centre sub-frame showed what it asked, or showed nothing of it (:meth:`centre_shown`)."""
        return any(n == frame and frame not in self.audits.get(key, {}) for key, (n, _audit, _pictures) in self._asked.items())

    async def centre_shown(self, frame: int, rects: dict[str, Any]) -> None:
        """Audit for output *frame* each matted insert its centre sub-frame shows (*rects*, the page's ``insertRects()``)
        without having asked for it: the page asks again only for another address, and a sub-frame of motion
        blur before the centre asked for the same one - the same drawing, which the centre shows. An insert the centre
        does not show is never audited for the frame."""
        for key in rects:
            asked = self._asked.get(key)
            if asked is None or asked[0] != frame or frame in self.audits.get(key, {}):
                continue
            await self._audited(key, frame, asked[1], asked[2])

    def place(self, frame: int, rects: dict[str, Any], stage: tuple[float, float]) -> None:
        """Keep where the page placed each screen insert on an output *frame* the guard or the presence gate
        reads: *rects* are the page's ``insertRects()`` in CSS pixels of the *stage* ``(w, h)``, kept 0-1 of the
        frame with how much of it shows - and, on a presence frame, the boxes of what its scene draws over it."""
        if frame not in self.guard and frame not in self.presence:
            return
        for key, rect in rects.items():
            self.placements.setdefault(key, {})[frame] = [*_box_of(rect, stage), round(float(rect[4]) if len(rect) > 4 else 1.0, 3)]
            if frame in self.presence and len(rect) > 5 and rect[5]:
                self.covers.setdefault(key, {})[frame] = [_box_of(box, stage) for box in rect[5]]

    def blend_scenes(self, frame: int, rects: dict[str, Any]) -> list[str]:
        """The scenes a presence frame shows an insert of blended: the page's ``insertRects()`` *rects* say at
        least ``insert_checks.PRESENCE_MIN_SHOWN`` of it shows, and less than all of it - a transition's frame, the
        presence gate reads it unblended (:meth:`keep_blend`)."""
        from app.services.video_studio.insert_checks import BLEND_WHOLE, PRESENCE_MIN_SHOWN

        if self.evidence_dir is None:
            return []
        scenes: set[str] = set()
        for key in self.presence.get(frame, ()):
            rect = rects.get(key)
            if rect is not None and len(rect) > 4 and PRESENCE_MIN_SHOWN <= float(rect[4]) < BLEND_WHOLE:
                scenes.add(timeline.insert_scene(key))
        return sorted(scenes)

    async def keep_blend(self, frame: int, scene_id: str, tone: str, shot: bytes) -> None:
        """Keep output *frame* with the scene *scene_id* painted *tone* (``black`` or ``white``, the page's ``tone()``),
        at most ``insert_checks.PRESENCE_WIDTH`` wide - the width the gate reads the encoded frame at - for each
        of the scene's inserts the gate reads there."""
        from app.services.video_studio.insert_checks import PRESENCE_WIDTH

        if self.evidence_dir is None:
            return
        path = self.evidence_dir / f"blend-{_slug_key(scene_id)}-{frame:07d}-{tone}.png"
        await asyncio.to_thread(_save_scaled, shot, path, PRESENCE_WIDTH)
        for key in self.presence.get(frame, ()):
            if timeline.insert_scene(key) == scene_id:
                self.blends.setdefault(key, {}).setdefault(frame, {})[tone] = path.name

    def guard_scenes(self, frame: int) -> list[str]:
        """The scenes whose keyed inserts a guard's output *frame* reads, each drawn alone for it (:meth:`keep_alone`)."""
        return sorted({timeline.insert_scene(key) for key in self.guard.get(frame, ())})

    async def keep_alone(self, frame: int, scene_id: str, shot: bytes, rects: dict[str, Any], stage: tuple[float, float]) -> None:
        """Keep the scene *scene_id* drawn alone on a guard's output *frame* (the page's ``alone()``): *shot* as
        captured, and where the page placed each of its keyed inserts on it (*rects*, ``insertRects()``) - an insert it
        placed nowhere kept with no box."""
        if self.evidence_dir is None:
            return
        path = self.evidence_dir / f"alone-{_slug_key(scene_id)}-{frame:07d}.png"
        await asyncio.to_thread(_write_shot, path, shot)
        for key in self.guard.get(frame, ()):
            if timeline.insert_scene(key) == scene_id:
                box = _box_of(rects[key], stage) if key in rects else None
                self.alone.setdefault(key, {})[frame] = {"file": path.name, "box": box}

    async def crop(self, frame: int, shot: bytes, rects: dict[str, Any], stage: tuple[float, float]) -> None:
        """Keep the insert regions of output *frame* wanted as 100 % crops, cut from *shot* (the frame as
        captured, the render's resolution); *rects* are the page's ``insertRects()`` in CSS pixels of the *stage*
        ``(w, h)``. One coordinate system: each box 0-1 of the frame (:func:`_box_of`, the one :meth:`place`
        keeps), multiplied by the decoded shot's own size - never a device scale or a quality preset."""
        if self.evidence_dir is None:
            return
        wanted = [key for key, frames in self.crops_wanted.items() if frame in frames and key in rects]
        if not wanted:
            return
        for key in wanted:
            path = self.evidence_dir / f"{_slug_key(key)}-crop-{frame:07d}.png"
            cut = await asyncio.to_thread(_save_crop, shot, _box_of(rects[key], stage), path)
            if cut is not None:
                (left, top, right, bottom), (width, height) = cut
                self.crops.setdefault(key, {})[frame] = str(path)
                self.crop_boxes.setdefault(key, {})[frame] = [
                    round(left / width, 5),
                    round(top / height, 5),
                    round(right / width, 5),
                    round(bottom / height, 5),
                ]
                self.crop_frame[key] = [width, height]

    def take_audits(self, first: int, end: int) -> dict:
        """The audits of the output frames ``[first, end)``, their evidence and their crops, handed on (a segment
        closed) and forgotten."""
        taken: dict[str, dict] = {}
        for key, frames in self.audits.items():
            inside = {n: audit for n, audit in frames.items() if first <= n < end}
            for n in inside:
                frames.pop(n)
            if inside:
                taken[key] = {
                    "frames": {str(n): a.as_dict() for n, a in inside.items()},
                    "evidence": {
                        kind: {"badness": round(bad, 4), "frame": n, "file": Path(path).name}
                        for (k, kind), (bad, n, path) in self.evidence.items()
                        if k == key and first <= n < end
                    },
                }
        for marked in [mark for mark, (_bad, n, _path) in self.evidence.items() if first <= n < end]:
            self.evidence.pop(marked)
        for key, cropped in self.crops.items():
            inside_crops = {n: path for n, path in cropped.items() if first <= n < end}
            for n in inside_crops:
                cropped.pop(n)
            if inside_crops:
                held = taken.setdefault(key, {"frames": {}, "evidence": {}})
                held["crops"] = {str(n): Path(path).name for n, path in inside_crops.items()}
                boxes = self.crop_boxes.get(key) or {}
                held["crop_boxes"] = {str(n): boxes.pop(n) for n in inside_crops if n in boxes}
                if key in self.crop_frame:
                    held["crop_frame"] = self.crop_frame[key]
        for name, kept in (
            ("placed", self.placements),
            ("alone", self.alone),
            ("overrun", self.overruns),
            ("blend", self.blends),
            ("over", self.covers),
        ):
            for key, by_frame in kept.items():
                inside_kept = {n: value for n, value in by_frame.items() if first <= n < end}
                for n in inside_kept:
                    by_frame.pop(n)
                if inside_kept:
                    taken.setdefault(key, {"frames": {}, "evidence": {}})[name] = {str(n): value for n, value in inside_kept.items()}
        return taken

    async def jumped(self) -> None:
        """The page draws from another place of the timeline: what a matted insert carries from one frame to
        the next - its bezel lock, its mattes, the footage under it, its last layer - is not the next frame's."""
        async with self._lock:
            self._matting.clear()
            self._insert_frames.clear()
            self._asked.clear()
            self._under.clear()

    async def release(self, keys: Iterable[str]) -> None:
        """Close the decoders and drop the stills of *keys*: their scene is over."""
        async with self._lock:
            self._under.clear()
            for key in keys:
                self._images.pop(key, None)
                self._insert_frames.pop(key, None)
                self._asked.pop(key, None)
                self._insert_pictures.pop(key, None)
                for held in [held for held in self._surfaces if held[0] == key]:
                    self._surfaces.pop(held)
                for held in [held for held in self._matting if held[0] == key]:
                    self._matting.pop(held)
                for held in [held for held in self._fills if held[0] == key]:
                    self._fills.pop(held)
                for held in [held for held in self._locks if held[0] == key]:
                    self._locks.pop(held)
                insert = self.assets.inserts.get(key)
                if insert is not None and insert.footage is not None:
                    for held in [held for held in self._stills if held[0] == insert.footage.path]:
                        self._stills.pop(held)
                decoder = self._decoders.pop(key, None)
                if decoder is not None:
                    await decoder.close()
        # What the scene's inserts were composed in goes back to the system, never kept for the next scene.
        await asyncio.to_thread(resources.release_freed_memory)

    async def close(self) -> None:
        async with self._lock:
            for decoder in self._decoders.values():
                await decoder.close()
            self._decoders.clear()
            self._images.clear()
            self._insert_frames.clear()
            self._asked.clear()
            self._insert_pictures.clear()
            self._surfaces.clear()
            self._matting.clear()
            self._fills.clear()
            self._locks.clear()
            self._stills.clear()
            self._under.clear()


def _box_of(rect: Any, stage: tuple[float, float]) -> list[float]:
    """*rect* (``[x, y, w, h, ...]`` in CSS pixels of the *stage* ``(w, h)``) as ``[x0, y0, x1, y1]``, 0-1 of the frame."""
    x, y, w, h = (float(v) for v in rect[:4])
    width, height = max(float(stage[0]), 1.0), max(float(stage[1]), 1.0)
    return [round(x / width, 5), round(y / height, 5), round((x + w) / width, 5), round((y + h) / height, 5)]


def _write_shot(path: Path, shot: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(shot)


def _save_scaled(shot: bytes, path: Path, width: int) -> None:
    """The captured frame *shot* saved as a PNG at *path*, at most *width* wide."""
    import io

    from PIL import Image

    path.parent.mkdir(parents=True, exist_ok=True)
    with Image.open(io.BytesIO(shot)) as opened:
        picture = opened.convert("RGB")
    if picture.width > width:
        picture = picture.resize((width, max(1, round(picture.height * width / picture.width))), Image.Resampling.LANCZOS)
    picture.save(path, format="PNG")


#: How close to a pixel's edge (pixels) a crop's 0-1 box edge is taken as on it (:func:`_save_crop`).
_CROP_SNAP = 0.05


def _save_crop(shot: bytes, box: Any, path: Path) -> tuple[tuple[int, int, int, int], tuple[int, int]] | None:
    """*box* (``[x0, y0, x1, y1]``, 0-1 of the frame) of the captured frame *shot* saved as a PNG at *path*, at the
    decoded frame's own size, untouched: the box cut, in the frame's pixels, and the frame's size; ``None`` for a box
    off the frame."""

    try:
        x0, y0, x1, y1 = (float(v) for v in box[:4])
    except (TypeError, ValueError):
        return None
    picture = screen_insert.decode(shot).convert("RGB")
    width, height = picture.size
    # A box kept to 5 decimals lands within a few hundredths of a pixel of the pixel edge it was placed on.
    left, top = max(0, math.floor(x0 * width + _CROP_SNAP)), max(0, math.floor(y0 * height + _CROP_SNAP))
    right, bottom = min(width, math.ceil(x1 * width - _CROP_SNAP)), min(height, math.ceil(y1 * height - _CROP_SNAP))
    if right - left < 2 or bottom - top < 2:
        return None
    path.parent.mkdir(parents=True, exist_ok=True)
    picture.crop((left, top, right, bottom)).save(path, format="PNG", compress_level=3)
    return (left, top, right, bottom), picture.size


def _empty_pixel() -> Any:
    from PIL import Image

    return Image.new("RGBA", (1, 1), (0, 0, 0, 0))


def _frame_pixels(data: bytes, size: tuple[int, int]) -> Any:
    """A decoded footage frame (JPEG) at the host's *size*, as the page draws it there (``H x W x 3`` uint8)."""
    import numpy as np
    from PIL import Image

    picture = screen_insert.decode(data).convert("RGB")
    if picture.size != size:
        picture = picture.resize(size, Image.Resampling.BILINEAR)
    return np.asarray(picture)


def _still_pixels(path: str, size: tuple[int, int]) -> Any:
    """A still backdrop under a matted insert, at the host's *size*."""
    with open(path, "rb") as source:
        return _frame_pixels(source.read(), size)


async def first_footage(insert: InsertSource, size: tuple[int, int]) -> Any:
    """The footage frame a matted insert comes in on, at *size*: its surface's reference, decoded on its own.

    A still backdrop is its own first frame.
    """
    footage = insert.footage
    if footage.kind != "video":
        return await asyncio.to_thread(_still_pixels, footage.path, size)
    decoder = encoder.FrameDecoder(footage.path, start=max(insert.first_frame, 0) / max(footage.fps, 1.0))
    try:
        await decoder.open()
        async for data in decoder.frames():
            return await asyncio.to_thread(_frame_pixels, data, size)
    finally:
        await decoder.close()
    raise RuntimeError(f"{insert.label or 'A screen insert'}: its footage has no frame {insert.first_frame}.")


async def plate_footage(insert: InsertSource, size: tuple[int, int], first: Any) -> list[tuple[Any, Any]]:
    """The footage frames a matted insert's clean surface is read on (``screen_insert.Surface.of_plate``), at
    *size*, each with its quad in pixels: *first* - the frame it comes in on, as :func:`first_footage` read it - then
    the rest of its plate (``InsertSource.plate``), each decoded on its own. A frame its footage does not have is left
    out; a still backdrop is its own plate."""
    width, height = size

    def corners(frame: int) -> list[tuple[float, float]]:
        return [(x * width, y * height) for x, y in insert.quads(frame)[0]]

    readings = [(first, corners(insert.first_frame))]
    footage = insert.footage
    if footage is None or footage.kind != "video":
        return readings
    for frame in insert.plate:
        if frame == insert.first_frame:
            continue
        decoder = encoder.FrameDecoder(footage.path, start=frame / max(footage.fps, 1.0))
        try:
            await decoder.open()
            async for data in decoder.frames():
                readings.append((await asyncio.to_thread(_frame_pixels, data, size), corners(frame)))
                break
        finally:
            await decoder.close()
    return readings


async def read_subject(insert: InsertSource, last_frame: int) -> Any:
    """What stands in front of a keyed *insert*'s surface over its clip, by colour (``screen_insert.subject_of``):
    its footage from the frame it comes in on to *last_frame*, ``screen_insert.SUBJECT_FRAMES`` of them at most spread
    over it, ``screen_insert.SUBJECT_WIDTH`` wide at most, each on its quad for that frame (a tracked insert's from its
    track), decoded on its own. ``None`` for an insert not keyed on a video, or a subject too small to say anything."""
    footage = insert.footage
    if insert.key is None or footage is None or footage.kind != "video":
        return None
    first = max(insert.first_frame, 0)
    count = max(1, int(last_frame) - first + 1)
    step = max(1, math.ceil(count / screen_insert.SUBJECT_FRAMES))
    decoder = encoder.FrameDecoder(footage.path, start=first / max(footage.fps, 1.0), max_width=screen_insert.SUBJECT_WIDTH)
    readings = []
    try:
        await decoder.open()
        index = 0
        async for data in decoder.frames():
            if index >= count:
                break
            if index % step == 0:
                pixels = await asyncio.to_thread(_decoded_pixels, data)
                quad, within = insert.quads(first + index)
                readings.append((pixels, quad, within))
            index += 1
    finally:
        await decoder.close()
    if not readings:
        return None
    if insert.key.rgb is None and insert.key.spread is None:
        insert = await asyncio.to_thread(screen_insert.keyed_on, insert, readings[0][0])
    key = insert.key
    return await asyncio.to_thread(
        screen_insert.subject_of, readings, key.key(), key.matte_tolerance(), feather=key.feather, still=insert.track is None
    )


def _decoded_pixels(data: bytes) -> Any:
    """A decoded footage frame (JPEG) at its own size (``H x W x 3`` uint8)."""
    import numpy as np

    return np.asarray(screen_insert.decode(data).convert("RGB"))


def _slug_key(key: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]+", "_", key)


def _save_evidence(picture: Any, path: Path, kind: str = "JPEG") -> None:
    """*picture* written at *path* as *kind*; a picture ``insert_frames`` kept is a file of that kind already, copied."""
    path.parent.mkdir(parents=True, exist_ok=True)
    if isinstance(picture, Path):
        shutil.copyfile(picture, path)
        return
    if kind == "JPEG":
        picture.save(path, format="JPEG", quality=88)
    else:
        picture.save(path, format=kind)


def _image_type(path: str) -> str:
    suffix = Path(path).suffix.lower()
    return {
        ".png": "image/png",
        ".jpg": "image/jpeg",
        ".jpeg": "image/jpeg",
        ".webp": "image/webp",
        ".svg": "image/svg+xml",
        # A motion scene's Lottie animation, fetched by the page as JSON.
        ".json": "application/json",
    }.get(suffix, "application/octet-stream")


# ── Segments kept across a resume ────────────────────────────────

#: Seconds of frames one encoder writes before it closes its file: what a resumed render draws again at most, per page.
SEGMENT_SECONDS = 10
#: A segment never holds more than this share of a render's frames: a checkpoint every 10% at least.
CHECKPOINT_SHARE = 0.1


def segment_frames(total: int, fps: int) -> int:
    """The frames of one segment of a render of *total* frames: :data:`SEGMENT_SECONDS`, at most a tenth of them.

    Never under a second of frames: a segment opens an encoder of its own.
    """
    return max(1, fps, min(fps * SEGMENT_SECONDS, math.ceil(total * CHECKPOINT_SHARE)))
#: The directory, in a render's working directory, of its finished segments and of what they were drawn from.
SEGMENTS_DIR = "segments"
#: Beside a segment, the audits of the matted inserts it drew; the frames they show, under :data:`EVIDENCE_DIR`.
AUDIT_SUFFIX = ".audits.json"
#: Beside each closed segment, who drew it (:func:`render_frames`'s ``drawn_for``): the revision and the attempt, read
#: back when a later attempt keeps the segment (:func:`kept_from`) and stored with it (``render_segments.keep``). It
#: also records the render that drew it and what its frames are drawn from (``window``, :class:`WindowDigests`):
#: what a render replacing it takes it by (``render_segments.take_over``).
DRAWN_SUFFIX = ".drawn.json"
DRAWN_FIELDS = ("revision", "attempt")
EVIDENCE_DIR = "evidence"
_PLAN_FILE = "plan.json"
_SEGMENT_RE = re.compile(r"^seg-(\d{7})-(\d{7})\.mp4$")
#: The parts of an insert's audits keyed by output frame (:func:`segment_audits`): their keys read back as numbers.
_FRAME_KEYED = ("frames", "crops", "crop_boxes", "placed", "alone", "overrun", "over")
#: In a render's working directory, the silent video its frames were encoded into and what it was drawn from:
#: what a resume after the encode keeps whole (:func:`encoded_video`), never drawing or joining a frame again.
ENCODED_FILE = "encoded.json"
#: The scene ranges' working directories inside a render's (``render._draw_in_ranges``): their segments too.
_RANGE_DIRS = "range-*"


def segment_ranges(start: int, end: int, size: int) -> list[tuple[int, int]]:
    """``[a, b)`` segments of at most *size* frames covering ``[start, end)``."""
    size = max(1, size)
    return [(a, min(a + size, end)) for a in range(start, end, size)]


def plan_segments(start: int, end: int, size: int, finished: Iterable[tuple[int, int]]) -> list[tuple[int, int]]:
    """Every segment of ``[start, end)`` in order: the *finished* ones kept, what they leave cut in *size* frames.

    An attempt drawing on other pages than the one before keeps every segment it closed whole.
    """
    plan: list[tuple[int, int]] = []
    at = start
    for first, last in sorted(piece for piece in finished if start <= piece[0] < piece[1] <= end):
        if first < at:
            continue  # overlaps a segment kept already
        plan.extend(segment_ranges(at, first, size))
        plan.append((first, last))
        at = last
    plan.extend(segment_ranges(at, end, size))
    return plan


def page_runs(todo: list[tuple[int, int]], pages: int) -> list[list[tuple[int, int]]]:
    """*todo* in contiguous runs of about the same frames, one a page: at most *pages*, never more than the segments.

    A page draws its run frame after frame, so each frame it draws follows the one before it, as on one page. A render
    shares its pieces out by their cost (``page_schedule.PageWork``).
    """
    return page_schedule.contiguous_runs(todo, pages)


def segment_name(first: int, end: int) -> str:
    return f"seg-{first:07d}-{end:07d}.mp4"


def _finished(directory: Path) -> dict[tuple[int, int], Path]:
    """The whole segments in *directory*, by their ``(first, end)`` frames."""
    found: dict[tuple[int, int], Path] = {}
    if not directory.is_dir():
        return found
    for path in directory.iterdir():
        match = _SEGMENT_RE.match(path.name)
        if match and path.stat().st_size > 0:
            found[(int(match.group(1)), int(match.group(2)))] = path
    return found


def same_drawing(directory: Path, digest: str) -> bool:
    """Whether *directory* holds what a former attempt drew of the same drawing (*digest*); made anew, empty, if not.

    The one rule of what a resumed render keeps: the compositor's
    segments, and a figure scene's frames and video (``figures.draw``).
    """
    plan = directory / _PLAN_FILE
    try:
        if json.loads(plan.read_text(encoding="utf-8")).get("digest") == digest:
            return True
    except (OSError, ValueError, AttributeError):
        pass
    shutil.rmtree(directory, ignore_errors=True)
    directory.mkdir(parents=True, exist_ok=True)
    plan.write_text(json.dumps({"digest": digest}), encoding="utf-8")
    return False


def kept_segments(directory: Path, digest: str) -> dict[tuple[int, int], Path]:
    """The segments a former attempt finished of the same drawing (*digest*); none, the directory made anew, otherwise."""
    return _finished(directory) if same_drawing(directory, digest) else {}


def segment_audits(directory: Path, segments: Iterable[tuple[int, int]]) -> dict[str, dict]:
    """The audits of *segments* kept in *directory* (:data:`AUDIT_SUFFIX`), merged by insert.

    ``key -> {"frames": {output frame: audit}, "evidence": {kind: [{badness, frame, path}]}, "crops": {output frame:
    path}, "crop_boxes": {output frame: [x0, y0, x1, y1]}, "crop_frame": [w, h], "placed": {output frame: [x0, y0,
    x1, y1, shown]}, "alone": {output frame: {file, box, path}}}``; a segment whose insert drew no matted frame has
    none. The crops are the insert region at its first, middle and last frame, at the render's resolution:
    each with its box, 0-1 of the frame, and the frame's size; ``placed`` and ``alone`` where the page
    placed it on each frame the guard reads and how much of it showed, and its scene drawn alone on that frame;
    ``blend`` ``{output frame: {"black": path, "white": path}}``, its scene painted black and white on each
    presence frame that blends it (``FrameServer.keep_blend``); ``over`` ``{output frame: [[x0, y0, x1, y1],
    ...]}``, what its scene draws over it on each presence frame (``FrameServer.place``).
    """
    merged: dict[str, dict] = {}
    for first, end in segments:
        path = directory / (Path(segment_name(first, end)).stem + AUDIT_SUFFIX)
        try:
            found = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        for key, part in (found or {}).items():
            into = merged.setdefault(key, {"evidence": {}, **{name: {} for name in _FRAME_KEYED}})
            into["frames"].update({int(n): audit for n, audit in (part.get("frames") or {}).items()})
            for kind, proof in (part.get("evidence") or {}).items():
                into["evidence"].setdefault(kind, []).append({**proof, "path": str(directory / EVIDENCE_DIR / proof["file"])})
            into["crops"].update({int(n): str(directory / EVIDENCE_DIR / name) for n, name in (part.get("crops") or {}).items()})
            into["crop_boxes"].update({int(n): box for n, box in (part.get("crop_boxes") or {}).items()})
            into["placed"].update({int(n): box for n, box in (part.get("placed") or {}).items()})
            into["over"].update({int(n): boxes for n, boxes in (part.get("over") or {}).items()})
            into["overrun"].update({int(n): found for n, found in (part.get("overrun") or {}).items()})
            into["alone"].update(
                {int(n): {**shot, "path": str(directory / EVIDENCE_DIR / shot["file"])} for n, shot in (part.get("alone") or {}).items()}
            )
            into.setdefault("blend", {}).update(
                {
                    int(n): {tone: str(directory / EVIDENCE_DIR / name) for tone, name in toned.items()}
                    for n, toned in (part.get("blend") or {}).items()
                }
            )
            if part.get("crop_frame"):
                into["crop_frame"] = part["crop_frame"]
    return merged


def _segment_frames(directory: Path) -> int:
    if not (directory / _PLAN_FILE).is_file():
        return 0
    return sum(end - first for first, end in _finished(directory))


def frames_kept(workdir: str | Path) -> int:
    """How many frames a render's working directory keeps for a resume: all of them once encoded
    (:func:`encoded_video`), else the finished segments it drew - its scene ranges' included."""
    root = Path(workdir)
    encoded = _encoded(root)
    if encoded is not None:
        return int(encoded.get("frames") or 0)
    return sum(_segment_frames(directory) for directory in (root / SEGMENTS_DIR, *root.glob(f"{_RANGE_DIRS}/{SEGMENTS_DIR}")))


def _encoded(root: Path) -> dict | None:
    """The encoded video *root* keeps (:data:`ENCODED_FILE`), while its file is the one recorded; ``None`` otherwise."""
    try:
        kept = json.loads((root / ENCODED_FILE).read_text(encoding="utf-8"))
        video = root / str(kept["video"])
        if video.is_file() and video.stat().st_size == int(kept["size"]):
            return kept
    except (OSError, ValueError, KeyError, TypeError):
        pass
    return None


def keep_encoded(workdir: str | Path, digest: str, video_path: str, frames: int, audits: dict) -> None:
    """Record the silent video *video_path* of *workdir* as the *frames* encoded from the drawing *digest*
    (:func:`drawing_digest`), with the insert *audits* they gave: what :func:`encoded_video` hands a resume.
    Best effort: a video not recorded is drawn from its kept segments again."""
    root = Path(workdir)
    try:
        video = Path(video_path).resolve().relative_to(root.resolve())
        record = {"digest": digest, "video": video.as_posix(), "size": Path(video_path).stat().st_size, "frames": int(frames), "audits": audits}
        partial = root / f"{ENCODED_FILE}.part"
        partial.write_text(json.dumps(record), encoding="utf-8")
        partial.replace(root / ENCODED_FILE)
    except (OSError, ValueError, TypeError) as exc:
        logger.warning("Render frames: the encoded video %s not kept for a resume: %s", video_path, format_exception_message(exc))


def encoded_video(workdir: str | Path, digest: str) -> tuple[str, dict] | None:
    """``(silent video, its insert audits)`` an attempt before encoded in *workdir* from the drawing *digest*
    (:func:`keep_encoded`); ``None`` when there is none, or of another drawing - forgotten then, never counted
    as kept (:func:`frames_kept`)."""
    root = Path(workdir)
    kept = _encoded(root)
    if kept is None or kept.get("digest") != digest:
        (root / ENCODED_FILE).unlink(missing_ok=True)
        return None
    audits = {
        key: {**part, **{name: {int(n): value for n, value in (part.get(name) or {}).items()} for name in _FRAME_KEYED if name in part}}
        for key, part in (kept.get("audits") or {}).items()
    }
    return str(root / kept["video"]), audits


def _short(value: object) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, default=str).encode("utf-8")).hexdigest()[:16]


def file_identity(assets: CompositorAssets | None, path: str) -> str:
    """What the file at *path* holds (``CompositorAssets.origins``): the file it was downloaded from, else its digest.

    The digest of a file the render made (a slide, a figure, a picture drawn from an SVG) is read once and kept on
    *assets*; a scene range's worker names the same file by the same digest (``render_ranges.fetch``).
    """
    if assets is not None and path in assets.origins:
        return assets.origins[path]
    try:
        digest = hashlib.sha256()
        with open(path, "rb") as source:
            for chunk in iter(lambda: source.read(1 << 20), b""):
                digest.update(chunk)
        found = f"sha256:{digest.hexdigest()}"
    except OSError:
        found = f"unread:{Path(path).name}"
    if assets is not None:
        assets.origins[path] = found
    return found


def _insert_inputs(insert: InsertSource, assets: CompositorAssets, placed: list[dict]) -> dict:
    """Every field a screen insert's frames are drawn by (:func:`insert_drawn_by`), what the findings call it, and
    *placed* - its payloads in the document: when it shows, its source's trim (the time map)."""
    return {**insert_drawn_by(insert, assets), "label": insert.label, "placed": placed}


def insert_drawn_by(insert: InsertSource, assets: CompositorAssets | None) -> dict:
    """What a screen insert's pixels are drawn from, whenever it shows: its file, quad, fit, key, glow, light, track,
    matte footage and box, its push, its edge despill and its subject's colours when set, and
    how the release draws it (``screen_insert.DRAWING_VERSION``). The key of its frames on the worker's disk
    (``insert_frames``)."""
    import dataclasses

    track, footage = insert.track, insert.footage
    return {
        "drawing": screen_insert.DRAWING_VERSION,
        "file": file_identity(assets, insert.path),
        "kind": insert.kind,
        "quad": [list(point) for point in insert.quad],
        "fps": insert.fps,
        "fit": insert.fit,
        "glow": insert.glow,
        "light": insert.light,
        "key": dataclasses.asdict(insert.key) if insert.key is not None else None,
        "track": (
            None
            if track is None
            else {"first": track.first, "quads": [[list(p) for p in quad] for quad in track.quads], "box": list(track.box)}
            if hasattr(track, "quads")
            else repr(track)
        ),
        "footage": (
            None
            if footage is None
            else {"file": file_identity(assets, footage.path), "kind": footage.kind, "fps": footage.fps, "key": footage.key}
        ),
        "first_frame": insert.first_frame,
        **({"plate": list(insert.plate)} if insert.plate else {}),
        "logo": insert.logo,
        "box": [list(point) for point in insert.box] if insert.box is not None else None,
        **({"zoom": insert.zoom.as_dict()} if insert.zoom is not None else {}),
        **({"despill": insert.despill.as_dict()} if insert.despill != screen_insert.Despill() else {}),
        **({"subject": insert.subject.as_dict()} if insert.subject is not None else {}),
        **({"others": [[list(point) for point in quad] for quad in insert.others]} if insert.others else {}),
    }


def drawing_inputs(
    document: dict,
    preset: RenderPreset,
    output_size: tuple[int, int],
    frame_range: tuple[int, int],
    assets: CompositorAssets | None = None,
) -> dict[str, str]:
    """What a render's frames are drawn from, part by part: ``name -> its digest`` (:func:`drawing_digest`).

    The planned document, the quality's preset, the output size and the frames drawn - and what the document names
    without holding it: each source, picture and screen insert the frame server reads (*assets*), each file by what
    it holds (:func:`file_identity`), each screen insert with every field it is drawn by (``insert <key>``: its key's
    colour, tolerance and feather among them). A field changed is a part changed, and the segments drawn before it
    are never kept.
    """
    placed: dict[str, list[dict]] = {}
    for scene in document.get("scenes") or []:
        for payload in scene.get("inserts") or []:
            if isinstance(payload, dict) and payload.get("src"):
                placed.setdefault(str(payload["src"]), []).append(payload)
    parts = {
        "document": _short(document),
        "preset": _short(repr(preset)),
        "size": _short(list(output_size)),
        "range": _short(list(frame_range)),
    }
    if assets is not None:
        for key, source in sorted(assets.sources.items()):
            parts[f"source {key}"] = _short({"file": file_identity(assets, source.path), "fps": source.fps})
        for key, path in sorted(assets.images.items()):
            parts[f"image {key}"] = _short(file_identity(assets, path))
        for key, insert in sorted(assets.inserts.items()):
            parts[f"insert {key}"] = _short(_insert_inputs(insert, assets, placed.get(key, [])))
    return parts


def digest_of(parts: dict[str, str]) -> str:
    return hashlib.sha256(json.dumps(parts, sort_keys=True).encode("utf-8")).hexdigest()


@functools.cache
def drawing_release() -> str:
    """How this build draws a frame, as one digest: the compositor page (its CSS, its script and the Lottie player it
    loads), the screen inserts' drawing (``screen_insert.DRAWING_VERSION``) and build
    (``insert_builds.BUILD_VERSION``), and the Playwright release that pins the Chromium drawing it.

    What a window's digest holds instead of the build's version (:class:`WindowDigests`): a rollout that changes none
    of them draws the same frames, so a render it hands to a worker of the next build takes back the segments it
    stored (``render_segments.take_back``) and resumes where it stood, never from frame 0. Any of them changed and
    every window changes: segments drawn before are never joined to frames drawn after.
    """
    from importlib import metadata

    from app.services.video_studio import compositor_page, insert_builds

    try:
        playwright = metadata.version("playwright")
    except metadata.PackageNotFoundError:
        playwright = "absent"
    parts = {
        "page": _short(compositor_page.PAGE_CSS + compositor_page.PAGE_JS + compositor_page.LOTTIE_JS),
        "page html": _short(compositor_page.page_html("")),
        "insert drawing": str(screen_insert.DRAWING_VERSION),
        "insert build": str(insert_builds.BUILD_VERSION),
        "playwright": playwright,
    }
    return digest_of(parts)


def drawing_digest(
    document: dict,
    preset: RenderPreset,
    output_size: tuple[int, int],
    frame_range: tuple[int, int],
    assets: CompositorAssets | None = None,
) -> str:
    """What a render's frames are drawn from (:func:`drawing_inputs`): the same digest, the same frames."""
    return digest_of(drawing_inputs(document, preset, output_size, frame_range, assets))


def kept_from(directory: Path, pieces: Iterable[tuple[int, int]]) -> list[dict]:
    """Who drew the segments *pieces* of *directory* kept: ``{"frames": [first, end], "segments": n, **record}``.

    The record is each segment's :data:`DRAWN_SUFFIX` file - the revision and the attempt that drew it -, consecutive
    segments of the same record joined; a segment without one (drawn by a release before it) records ``None``.
    """
    runs: list[dict] = []
    for first, end in sorted(pieces):
        try:
            read = json.loads((directory / (Path(segment_name(first, end)).stem + DRAWN_SUFFIX)).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            read = None
        record = {name: read.get(name) if isinstance(read, dict) else None for name in DRAWN_FIELDS}
        last = runs[-1] if runs else None
        if last is not None and last["frames"][1] == first and all(last[name] == record[name] for name in DRAWN_FIELDS):
            last["frames"][1] = end
            last["segments"] += 1
        else:
            runs.append({"frames": [first, end], "segments": 1, **record})
    return runs


# ── What a window of frames is drawn from ─────────────────────────


def _named_keys(value: Any, keys: set[str], found: set[str]) -> None:
    """Every key of *keys* a string of *value* (a part of a planned document) names, added to *found*."""
    if isinstance(value, str):
        if value in keys:
            found.add(value)
    elif isinstance(value, dict):
        for item in value.values():
            _named_keys(item, keys, found)
    elif isinstance(value, list):
        for item in value:
            _named_keys(item, keys, found)


#: What the page reads of the scene before the one on screen: whether it brands itself (``brandsItself`` in
#: ``compositor_page``), which decides whether the watermark is shown.
BRANDING_FIELDS = ("type", "show_logo", "brands")
#: ... and whether its spec turns the watermark off (``noWatermark``): read into the digest only when set, so a
#: window whose scene before says nothing of it keeps its digest.
WATERMARK_FIELD = "watermark"


class WindowDigests:
    """What the frames ``[first, end)`` of a planned document are drawn from, as one digest (:meth:`of`).

    The page draws a frame from the scenes on screen at its time - each whole,
    with its place in the video and among the scenes: a scene moved draws
    other frames -, whether the scene before them brands itself
    (:data:`BRANDING_FIELDS`), the caption cues over those scenes (shown, and
    the room each scene keeps clear of them) and, when the watermark stands on
    the captions' edge, every cue (it is lifted above the tallest), the look
    of the whole video (its size, rate, theme and watermark), the fades and
    the last frame only where the window reaches them, and the files those
    scenes name - with each file no scene names (the logo, the mark) - by
    what they hold (:func:`file_identity`, a screen insert by every field it
    is drawn by). With the quality's preset, the output size and how the build
    draws (:func:`drawing_release`, never the build's version), two windows of
    the same digest draw the same frames, whatever the rest of their documents:
    what a render takes from the render of an older revision it replaced
    (``render_segments.take_over``), and what a render takes back of its own
    on a worker of the next build (``render_segments.take_back``).
    """

    def __init__(
        self, document: dict, preset: RenderPreset, output_size: tuple[int, int], assets: CompositorAssets | None = None
    ) -> None:
        self.document = document
        self.assets = assets
        self.fps = float(document.get("fps") or 0.0) or 1.0
        self.scenes = list(document.get("scenes") or [])
        self.common = {
            "look": _short({key: document.get(key) for key in ("version", "width", "height", "fps", "theme", "watermark")}),
            "captions": _short({key: (document.get("captions") or {}).get(key) for key in ("enabled", "style", "position")}),
            "preset": _short(repr(preset)),
            "size": _short(list(output_size)),
            # How this build draws a frame, never the build itself: a rollout that draws alike keeps the windows.
            "release": drawing_release(),
        }
        captions = document.get("captions") or {}
        self.cues = list(captions.get("cues") or [])
        watermark = str(document.get("watermark") or "none")
        on_their_edge = watermark != "none" and ("top" in watermark) == (captions.get("position") == "top")
        self.every_cue = _short(self.cues) if captions.get("enabled") and on_their_edge else ""
        self.keys: set[str] = set()
        self.unnamed: set[str] = set()
        if assets is not None:
            self.keys = set(assets.sources) | set(assets.images) | set(assets.inserts)
            named: set[str] = set()
            _named_keys(self.scenes, self.keys, named)
            self.unnamed = self.keys - named

    def shown(self, first: int, end: int) -> list[int]:
        """The indexes of the scenes on screen over frames ``[first, end)``, a sub-frame either side."""
        since, until = (first - 1) / self.fps, end / self.fps
        last = len(self.scenes) - 1
        return [
            index
            for index, scene in enumerate(self.scenes)
            if float(scene["start"]) <= until and (float(scene["end"]) >= since or index == last)
        ]

    def inputs(self, first: int, end: int) -> dict[str, str]:
        """``name -> digest`` of what frames ``[first, end)`` are drawn from."""
        document = self.document
        since, until = (first - 1) / self.fps, end / self.fps
        last = len(self.scenes) - 1
        shown = self.shown(first, end)
        scenes = [{"index": index, "last": index == last, **self.scenes[index]} for index in shown]
        before = self.scenes[shown[0] - 1] if shown and shown[0] > 0 else {}
        if scenes:
            since = min(since, *(float(scene["start"]) for scene in scenes))
            until = max(until, *(float(scene["end"]) for scene in scenes))
        cues = [cue for cue in self.cues if float(cue[0]) <= until and float(cue[1]) >= since]
        duration, frames = float(document.get("duration") or 0.0), int(document.get("frames") or 0)
        fade_in, fade_out = float(document.get("fade_in") or 0.0), float(document.get("fade_out") or 0.0)
        edges: dict[str, float] = {}
        if (first - 1) / self.fps < fade_in:
            edges["fade_in"] = fade_in
        if end / self.fps >= duration - fade_out - 1.0 / self.fps or end >= frames - 1:
            edges.update(fade_out=fade_out, duration=duration, frames=frames)
        parts = {
            **self.common,
            "frames": _short([first, end]),
            "scenes": _short(scenes),
            "before": _short(
                {key: before.get(key) for key in BRANDING_FIELDS}
                | ({WATERMARK_FIELD: before[WATERMARK_FIELD]} if WATERMARK_FIELD in before else {})
            ),
            "cues": _short(cues),
            "every cue": self.every_cue,
            "edges": _short(edges),
        }
        assets = self.assets
        if assets is None:
            return parts
        named: set[str] = set()
        _named_keys(scenes, self.keys, named)
        placed: dict[str, list[dict]] = {}
        for scene in scenes:
            for payload in scene.get("inserts") or []:
                if isinstance(payload, dict) and payload.get("src"):
                    placed.setdefault(str(payload["src"]), []).append(payload)
        for key in sorted(named | self.unnamed):
            if key in assets.sources:
                source = assets.sources[key]
                parts[f"source {key}"] = _short({"file": file_identity(assets, source.path), "fps": source.fps})
            if key in assets.images:
                parts[f"image {key}"] = _short(file_identity(assets, assets.images[key]))
            if key in assets.inserts:
                parts[f"insert {key}"] = _short(_insert_inputs(assets.inserts[key], assets, placed.get(key, [])))
        return parts

    def of(self, first: int, end: int) -> str:
        """The digest of what frames ``[first, end)`` are drawn from (:meth:`inputs`)."""
        return digest_of(self.inputs(first, end))

    def scene_frames(self, first: int, end: int) -> dict[str, int]:
        """``scene id -> frames`` of ``[first, end)``, each frame counted to the last scene started by its time."""
        counted: dict[str, int] = {}
        starts = [math.ceil(float(scene["start"]) * self.fps) for scene in self.scenes]
        for index, scene in enumerate(self.scenes):
            begin = max(first, starts[index] if index else 0)
            stop = min(end, starts[index + 1] if index + 1 < len(starts) else end)
            if stop > begin:
                key = str(scene.get("id") or f"scene {index + 1}")
                counted[key] = counted.get(key, 0) + stop - begin
        return counted


# ── What is on screen ─────────────────────────────────────────────────────


def _inserts(scene: dict) -> list[dict]:
    return [item for item in scene.get("inserts") or [] if isinstance(item, dict) and isinstance(item.get("src"), str)]


def scene_media(scene: dict) -> list[str]:
    """The keys of the media a planned scene shows: its picture, its backdrop and its screen inserts."""
    keys = []
    if isinstance(scene.get("src"), str):
        keys.append(scene["src"])
    backdrop = scene.get("backdrop")
    if isinstance(backdrop, dict) and isinstance(backdrop.get("src"), str):
        keys.append(backdrop["src"])
    keys.extend(item["src"] for item in _inserts(scene))
    return keys


def _video_media(scene: dict) -> list[str]:
    keys = [key for key in scene_media(scene) if key.startswith(_VIDEO_PREFIXES)]
    return keys + [item["src"] for item in _inserts(scene) if item.get("kind") == "video"]


def release_schedule(document: dict) -> list[tuple[int, list[str]]]:
    """``(frame, keys)`` in frame order: a scene's media are released from the first frame after it ended.

    The last scene is never released: the page keeps it on screen to the end.
    """
    fps = float(document["fps"])
    scenes = list(document.get("scenes") or [])
    schedule = [
        (math.floor(float(scene["end"]) * fps) + 1, keys) for scene in scenes[:-1] if (keys := scene_media(scene))
    ]
    return sorted(schedule, key=lambda entry: entry[0])


def decoder_budget(document: dict) -> int:
    """The decoders a page needs open at once: the most video sources on screen together, at least two."""
    edges: list[tuple[float, int]] = []
    scenes = list(document.get("scenes") or [])
    for index, scene in enumerate(scenes):
        count = len(_video_media(scene))
        if not count:
            continue
        end = math.inf if index == len(scenes) - 1 else float(scene["end"])
        edges.append((float(scene["start"]), count))
        edges.append((end, -count))
    # An end and a start at the same instant: the ending scene leaves first.
    edges.sort(key=lambda edge: (edge[0], edge[1]))
    peak = current = 0
    for _at, delta in edges:
        current += delta
        peak = max(peak, current)
    return max(MIN_OPEN_DECODERS, peak)


def matted_spans(document: dict, assets: CompositorAssets) -> list[tuple[int, int]]:
    """The output frames ``[first, end)`` of each matted screen insert of *document*: its footage read under it
    frame after frame, its matte and bezel lock carried from one frame to the next by the page that draws it - over
    every frame its layer is drawn on, a keyed insert's whole scene (``timeline.insert_drawn``)."""
    fps = max(float(document.get("fps") or 0.0), 1.0)
    spans = []
    for scene in document.get("scenes") or []:
        start = float(scene.get("start") or 0.0)
        for payload in _inserts(scene):
            insert = assets.inserts.get(payload.get("src") or "")
            if insert is None or insert.footage is None:
                continue
            drawn_from, drawn_to = timeline.insert_drawn(payload)
            begin = math.ceil((start + drawn_from) * fps)
            spans.append((begin, max(begin, math.ceil((start + drawn_to) * fps))))
    return spans


def scenes_at(document: dict, frames: Iterable[int]) -> list[str]:
    """``"scene 7 (clip)"`` for each scene on screen at any of *frames*, in timeline order."""
    fps = float(document["fps"]) or 1.0
    scenes = list(document.get("scenes") or [])
    shown: set[int] = set()
    for frame in frames:
        at = frame / fps
        for index, scene in enumerate(scenes):
            if at >= float(scene["start"]) and (at < float(scene["end"]) or index == len(scenes) - 1):
                shown.add(index)
    return [f"scene {index + 1} ({scenes[index].get('type')})" for index in sorted(shown)]


@dataclass
class DrawProgress:
    """Where the pages of a render are: what its progress reports and a failure names."""

    total: int = 0
    done: int = 0
    #: The frames a former attempt finished, kept rather than drawn again.
    kept: int = 0
    #: Those segments and who drew them (:func:`kept_from`), and the parts of the digest they matched on
    #: (:func:`drawing_inputs`): what the render's answer says it reused.
    reused: list[dict] = field(default_factory=list)
    matched: dict[str, str] = field(default_factory=dict)
    #: Of the frames kept, those taken from the render of an older revision it replaced
    #: (``render_segments.take_over``): how many, and ``{render, revision, frames: [first, end], scenes: {id: frames}}``
    #: by run of segments.
    inherited_frames: int = 0
    inherited: list[dict] = field(default_factory=list)
    #: Of the frames kept, the render's own segments stored under an earlier digest of its drawing and taken back on
    #: this worker (``render_segments.take_back``), by run of segments as :attr:`inherited`.
    taken_back: list[dict] = field(default_factory=list)
    #: Page index -> the frame it is drawing.
    positions: dict[int, int] = field(default_factory=dict)
    #: The most decoders one page had open at once.
    peak_open_decoders: int = 0
    #: The frames drawn of each kind (``render_preflight.FRAME_KINDS``) and the page-seconds they took, each
    #: from its drawing to its encoder taking it: what the next pre-flights weigh a frame of each kind by.
    kind_frames: dict[str, int] = field(default_factory=dict)
    kind_seconds: dict[str, float] = field(default_factory=dict)
    #: The same by scene type (``render_preflight.scene_class``): a clip with a screen insert, a statement over
    #: a clip, a figure - what the next pre-flights weigh a frame of each scene type by.
    scene_frames: dict[str, int] = field(default_factory=dict)
    scene_seconds: dict[str, float] = field(default_factory=dict)
    #: The x264 threads each page's encoder ran (``render_preflight.encoder_threads``).
    encoder_threads: int = 0
    #: Each page's run: ``[segments, seconds its frames took, seconds the page ran]`` - what a page spends
    #: beside its frames (its start, each segment's encoder opened and flushed) is the difference.
    page_runs: dict[int, list[float]] = field(default_factory=dict)
    #: The sub-frames each frame drew (motion blur): a rate is per drawn sub-frame.
    samples: int = 1
    #: What a frame of each kind costs a page before this attempt measured its own (``page_schedule.FrameCosts``):
    #: the previous render of its video's, else the platform's.
    prior: page_schedule.FrameCosts | None = None
    #: The pieces its pages draw (``page_schedule.PageWork``), once laid out: its frames left and its ETA.
    work: page_schedule.PageWork | None = None
    #: Its frames a second over the last minute.
    rate: page_schedule.Rate = field(default_factory=page_schedule.Rate)
    #: When each page found nothing left worth taking, and when the last frame was drawn (:meth:`idle`); the frames
    #: drawn uncaptured before a piece started inside a matted insert, and their seconds.
    idle_at: dict[int, float] = field(default_factory=dict)
    drawn_at: float | None = None
    warm_frames: int = 0
    warm_seconds: float = 0.0
    #: The screen-insert frames its pages read back from the worker's disk (``insert_frames``), and those they drew.
    insert_cached: int = 0
    insert_drawn: int = 0
    #: Of the frames timed, those whose screen inserts were all read back drawn, by kind and by scene type: they price
    #: this attempt's own schedule (:meth:`costs`), never what :meth:`kinds` and :meth:`scenes` hand the next
    #: pre-flights - a frame that draws its inserts costs what drawing them costs.
    reused_kind_frames: dict[str, int] = field(default_factory=dict)
    reused_kind_seconds: dict[str, float] = field(default_factory=dict)
    reused_scene_frames: dict[str, int] = field(default_factory=dict)
    reused_scene_seconds: dict[str, float] = field(default_factory=dict)
    #: The seconds its pages take for the frames left, as the last status line said (:meth:`status`): its ETA.
    seconds_left: float | None = None

    def insert_frames(self) -> dict | None:
        """``{cached, drawn, share}`` of its screen-insert frames - *share* the part read back drawn -; ``None`` for a
        render that showed none."""
        total = self.insert_cached + self.insert_drawn
        if not total:
            return None
        return {"cached": self.insert_cached, "drawn": self.insert_drawn, "share": round(self.insert_cached / total, 4)}

    def drawing(self, document: dict) -> list[str]:
        return scenes_at(document, self.positions.values())

    def timed(self, kind: str, seconds: float, page: int | None = None, *, scene: str | None = None, reused: bool = False) -> None:
        """A frame of *kind* (and *scene* type) drawn in *seconds* on *page*; *reused* when its screen inserts were all
        read back drawn (``insert_frames``)."""
        self.kind_frames[kind] = self.kind_frames.get(kind, 0) + self.samples
        self.kind_seconds[kind] = self.kind_seconds.get(kind, 0.0) + max(0.0, seconds)
        if scene is not None:
            self.scene_frames[scene] = self.scene_frames.get(scene, 0) + self.samples
            self.scene_seconds[scene] = self.scene_seconds.get(scene, 0.0) + max(0.0, seconds)
        if reused:
            self.reused_kind_frames[kind] = self.reused_kind_frames.get(kind, 0) + self.samples
            self.reused_kind_seconds[kind] = self.reused_kind_seconds.get(kind, 0.0) + max(0.0, seconds)
            if scene is not None:
                self.reused_scene_frames[scene] = self.reused_scene_frames.get(scene, 0) + self.samples
                self.reused_scene_seconds[scene] = self.reused_scene_seconds.get(scene, 0.0) + max(0.0, seconds)
        if page is not None:
            self.page_runs.setdefault(page, [0, 0.0, 0.0])[1] += max(0.0, seconds)

    def warmed(self, page: int, frames: int, seconds: float) -> None:
        """*frames* drawn uncaptured on *page* before a piece: a page's frame time, never a kind's frames."""
        self.warm_frames += frames
        self.warm_seconds += max(0.0, seconds)
        self.page_runs.setdefault(page, [0, 0.0, 0.0])[1] += max(0.0, seconds)

    def costs(self) -> page_schedule.FrameCosts:
        """Each kind at what this attempt drew of it, else at :attr:`prior` (``page_schedule.FrameCosts.priced``)."""
        per = max(1, self.samples)
        drawn = {kind: (frames // per, self.kind_seconds.get(kind, 0.0)) for kind, frames in self.kind_frames.items()}
        return page_schedule.FrameCosts.priced(self.prior, drawn)

    def status(self, percent: int) -> str:
        """``Drawing frames (87%): 1,104 left, 7.4 frames/s over the last minute, ETA 17:42 UTC (in about 2 min 30 s)``:
        the frames no page started, its pace over the last minute and the time its pages take for the rest,
        each kind at what it costs now."""
        if self.work is None:
            return f"Drawing frames ({percent}%)"
        self.work.costs = self.costs()
        self.seconds_left = self.work.seconds_left()
        return page_schedule.status_line(
            percent,
            left=self.work.frames_left(),
            per_second=self.rate.per_second(),
            seconds_left=self.seconds_left,
        )

    def pace(self) -> render_pace.Pace | None:
        """What its frames measured, once ``render_pace.PACE_SHARE`` of those this attempt draws are drawn:
        the status line's ETA (:meth:`status`), what replaces its pre-flight's estimate; ``None`` before."""
        to_draw = self.total - self.kept
        drawn = self.done - self.kept
        if self.seconds_left is None or to_draw <= 0 or drawn < max(1, math.ceil(render_pace.PACE_SHARE * to_draw)):
            return None
        return render_pace.Pace(drawn=drawn, frames=self.total, per_second=self.rate.per_second(), seconds_left=self.seconds_left)

    def idle(self) -> dict[int, float]:
        """``{page: seconds}`` each page had nothing left worth taking while another still drew."""
        if self.drawn_at is None:
            return {}
        return {page: round(max(0.0, self.drawn_at - at), 1) for page, at in sorted(self.idle_at.items())}

    def ran(self, page: int, segments: int, seconds: float) -> None:
        run = self.page_runs.setdefault(page, [0, 0.0, 0.0])
        run[0], run[2] = int(segments), round(max(0.0, seconds), 2)

    def runs(self) -> list[list[float]]:
        """``[[segments, frame seconds, page seconds], ...]`` of the pages that drew."""
        return [[int(run[0]), round(run[1], 2), run[2]] for _page, run in sorted(self.page_runs.items()) if run[0]]

    def kinds(self) -> dict[str, list[float]]:
        """``{kind: [frames, page-seconds]}`` of the frames this attempt drew, those whose screen inserts were
        all read back drawn left out."""
        return _drawn_only(self.kind_frames, self.kind_seconds, self.reused_kind_frames, self.reused_kind_seconds)

    def scenes(self) -> dict[str, list[float]]:
        """``{scene type: [frames, page-seconds]}`` of the frames this attempt drew (``render_preflight.scene_class``),
        those whose screen inserts were all read back drawn left out."""
        return _drawn_only(self.scene_frames, self.scene_seconds, self.reused_scene_frames, self.reused_scene_seconds)


def _drawn_only(frames: dict[str, int], seconds: dict[str, float], reused: dict[str, int], reused_seconds: dict[str, float]) -> dict[str, list[float]]:
    """``{name: [frames, page-seconds]}`` of *frames* and *seconds* less the *reused* ones; a name left with none goes."""
    drawn = {}
    for name in sorted(frames):
        count = frames[name] - reused.get(name, 0)
        if count > 0:
            drawn[name] = [count, round(max(0.0, seconds.get(name, 0.0) - reused_seconds.get(name, 0.0)), 2)]
    return drawn


ProgressFn = Callable[[int], Awaitable[None]]


async def _together(*work: Awaitable[Any]) -> list[Any]:
    """Run *work* side by side, results in order; the first to fail stops the others, its exception raised as is.

    A page that fails stops the others at once, rather than leaving them to draw until the browser closes.
    """
    tasks = [asyncio.ensure_future(item) for item in work]
    try:
        done, _pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_EXCEPTION)
        for task in done:
            if not task.cancelled() and task.exception() is not None:
                raise task.exception()
        return [task.result() for task in tasks]
    finally:
        for task in tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)


async def _on_browsers(capture: str, pages: int, chunk: Callable[[Any, int], Awaitable[None]]) -> None:
    """Draw *pages* pages side by side, each by ``chunk(browser, index)``: in a browser of its own where their frames
    are begun - begin-frame control drives a browser's one display, and two pages begun in one browser stall together
    (measured on the render fixture) -, all in one browser where they are screenshot. What the memory model
    counts (``render_preflight.browsers_of``)."""
    from playwright.async_api import async_playwright

    from app.services.headless_render import RENDER_CHROMIUM_ARGS

    async with async_playwright() as pw:
        if capture == "begin_frame":

            async def alone(index: int) -> None:
                browser = await pw.chromium.launch(headless=True, args=[*RENDER_CHROMIUM_ARGS, *BEGIN_FRAME_ARGS])
                try:
                    await chunk(browser, index)
                finally:
                    await browser.close()

            await _together(*(alone(index) for index in range(pages)))
            return
        browser = await pw.chromium.launch(headless=True, args=list(RENDER_CHROMIUM_ARGS))
        try:
            await _together(*(chunk(browser, index) for index in range(pages)))
        finally:
            await browser.close()


#: The Chromium switches a page drawn frame by frame needs (``VIDEO_STUDIO_FRAME_CAPTURE``): a frame produced
#: only when one is asked (``HeadlessExperimental.beginFrame``), every compositor stage run before it is drawn,
#: nothing animated, scrolled or decoded on a thread of its own - so the frame captured is the page drawn whole.
BEGIN_FRAME_ARGS: tuple[str, ...] = (
    "--enable-begin-frame-control",
    "--run-all-compositor-stages-before-draw",
    "--disable-new-content-rendering-timeout",
    "--disable-threaded-animation",
    "--disable-threaded-scrolling",
    "--disable-checker-imaging",
    "--disable-image-animation-resync",
)


def frame_capture_mode() -> str:
    """``begin_frame`` or ``screenshot``: the capture ``VIDEO_STUDIO_FRAME_CAPTURE`` asks for on this platform.

    ``auto`` asks for ``begin_frame`` where Chromium offers it - its headless shell on Linux - and screenshots
    elsewhere. What a render uses is the worker's probe of it (:func:`worker_capture`): ``begin_frame`` only where
    the compositor's Chromium began a frame within :data:`CAPTURE_PROBE_SECONDS`, and a page that cannot be driven
    frame by frame takes screenshots whatever is asked (:func:`_frame_page`).
    """
    import sys

    from app.core.config import settings

    mode = str(settings.video_studio_frame_capture or "screenshot").strip().lower()
    if mode == "auto":
        return "begin_frame" if sys.platform.startswith("linux") else "screenshot"
    return "begin_frame" if mode == "begin_frame" else "screenshot"


#: How long the probe of a worker's capture waits for a begun frame with a picture on the compositor's
#: Chromium: one that begins frames answers in milliseconds once its renderer is up ...
CAPTURE_PROBE_SECONDS = 2.0
#: ... tried this long again before the worker settles on screenshots: a Chromium starting cold on a busy worker
#: misses the first ...
CAPTURE_PROBE_TRIES = (CAPTURE_PROBE_SECONDS, 5.0)
#: ... and a worker on screenshots where begun frames are asked - its probe without one, or demoted by a render's - is
#: probed again this long after, in the background: never held to screenshots until it restarts.
CAPTURE_REPROBE_SECONDS = 30 * 60.0
#: The longest a try takes, its Chromium's launch included: past it, the try had no begun frame.
_CAPTURE_PROBE_LIMIT_SECONDS = 30.0
#: Set by the worker on each render process it starts: the capture its probe settled on (:func:`worker_capture`),
#: so a render never probes, nor asks for begun frames its worker's Chromium did not give.
FRAME_CAPTURE_ENV = "XTM_RENDER_FRAME_CAPTURE"


@dataclass(frozen=True)
class FrameCapture:
    """How the renders of this worker take their frames: what was asked, what its probe settled on, and why."""

    #: What ``VIDEO_STUDIO_FRAME_CAPTURE`` asks for here (:func:`frame_capture_mode`).
    asked: str
    #: What its renders use: ``begin_frame`` only where the probe began a frame, else ``screenshot``.
    mode: str
    #: What the probe saw - ``a begun frame in 0.21 s``, ``no begun frame within 2 s`` -, or what failed a render's
    #: begun frames since (:func:`demote_capture`); empty when nothing was probed (screenshots asked).
    reason: str = ""
    #: Whether a render's begun frames failed on this worker after its probe passed.
    demoted: bool = False
    #: When it was settled (``time.monotonic``): a screenshot capture where begun frames are asked is probed again
    #: :data:`CAPTURE_REPROBE_SECONDS` after.
    settled_at: float = field(default_factory=time.monotonic, compare=False)

    @property
    def probed(self) -> bool:
        return self.asked == "begin_frame"

    @property
    def stale(self) -> bool:
        """Whether it is due a new probe: screenshots where begun frames are asked, settled long enough ago."""
        return (
            self.asked == "begin_frame"
            and self.mode == "screenshot"
            and time.monotonic() - self.settled_at >= CAPTURE_REPROBE_SECONDS
        )

    def as_dict(self) -> dict:
        return {"asked": self.asked, "mode": self.mode, "reason": self.reason, "demoted": self.demoted}

    @classmethod
    def of(cls, data: Any) -> FrameCapture | None:
        if not isinstance(data, dict) or data.get("mode") not in ("begin_frame", "screenshot"):
            return None
        asked = data.get("asked") if data.get("asked") in ("begin_frame", "screenshot") else data["mode"]
        return cls(asked, data["mode"], str(data.get("reason") or ""), bool(data.get("demoted")))

    def words(self) -> str:
        """What the pre-flight says of it, once probed: ``Frames are begun one by one on the render worker (its probe
        at start: a begun frame in 0.21 s).``; empty when screenshots are asked and nothing was probed."""
        if not self.probed:
            return ""
        if self.mode == "begin_frame":
            return f"Frames are begun one by one on the render worker (its probe at start: {self.reason})."
        if self.demoted:
            return (
                f"Frames are taken by screenshot on the render worker: its probe began frames, then a render's begun "
                f"frames failed ({self.reason}), so it takes screenshots until it restarts."
            )
        return (
            f"Frames are taken by screenshot on the render worker: VIDEO_STUDIO_FRAME_CAPTURE asks for begun frames, "
            f"and its probe at start had {self.reason}."
        )


#: This process's capture once probed (:func:`worker_capture`), and the probe running.
_CAPTURE: list[FrameCapture] = []
_CAPTURE_PROBE: list[asyncio.Task] = []


def known_capture() -> FrameCapture | None:
    """This worker's capture when its probe has ended (:func:`worker_capture`); ``None`` while it runs or before."""
    if _CAPTURE and _CAPTURE[0].asked == frame_capture_mode():
        return _CAPTURE[0]
    return None


def start_capture_probe() -> None:
    """Probe the capture this worker's renders use - at its start, or again once what it settled on is
    :attr:`FrameCapture.stale` - in a task of its own: nothing waits for it."""
    known = known_capture()
    if (known is None or known.stale) and not (_CAPTURE_PROBE and not _CAPTURE_PROBE[0].done()):
        _CAPTURE_PROBE[:] = [asyncio.create_task(_probe_and_keep())]


async def stop_capture_probe() -> None:
    """Cancel the probe still running (the worker's shutdown): its Chromium closed, never left behind."""
    running = _CAPTURE_PROBE[0] if _CAPTURE_PROBE and not _CAPTURE_PROBE[0].done() else None
    if running is not None:
        running.cancel()
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await running
    _CAPTURE_PROBE.clear()


async def worker_capture() -> FrameCapture:
    """How this worker's renders take their frames, probed once per process (:func:`probe_capture`).

    ``begin_frame`` only where ``VIDEO_STUDIO_FRAME_CAPTURE`` asks for it and the compositor's own Chromium began a
    frame with a picture within one of :data:`CAPTURE_PROBE_TRIES`; else ``screenshot``, the capture every Chromium
    takes: a capture change never fails a render. A render's begun frames that fail later demote the worker
    (:func:`demote_capture`). Screenshots where begun frames are asked are probed again in the background once
    :attr:`FrameCapture.stale`; the render asking meanwhile draws with what is known.
    """
    known = known_capture()
    if known is not None:
        if known.stale:
            start_capture_probe()
        return known
    running = _CAPTURE_PROBE[0] if _CAPTURE_PROBE and not _CAPTURE_PROBE[0].done() else None
    if running is not None and running is not asyncio.current_task():
        return await asyncio.shield(running)
    return await _probe_and_keep()


async def _probe_and_keep() -> FrameCapture:
    found = await probe_capture()
    _CAPTURE[:] = [found]
    log = logger.info if found.mode == found.asked else logger.warning
    log("Render frame capture: %s (asked %s%s)", found.mode, found.asked, f"; {found.reason}" if found.reason else "")
    return found


def demote_capture(reason: str) -> None:
    """Take screenshots on this worker from now on: a render's begun frames failed here (*reason*)."""
    known = known_capture()
    if known is None or known.mode != "begin_frame":
        return
    _CAPTURE[:] = [FrameCapture(known.asked, "screenshot", reason[:300], demoted=True)]
    logger.warning("Render frame capture: screenshots on this worker from now on: begun frames failed (%s)", reason[:300])


async def render_capture() -> str:
    """The capture a render process draws with: its worker's (:data:`FRAME_CAPTURE_ENV`), else this process's own
    probe - a render drawn outside a worker's render process."""
    import os

    handed = os.environ.get(FRAME_CAPTURE_ENV, "")
    if handed in ("begin_frame", "screenshot"):
        return handed if frame_capture_mode() == "begin_frame" else "screenshot"
    return (await worker_capture()).mode


async def probe_capture() -> FrameCapture:
    """Begin one frame on the compositor's Chromium (its render switches and :data:`BEGIN_FRAME_ARGS`) and wait for its
    picture, once per wait of :data:`CAPTURE_PROBE_TRIES` until one comes (a cold start misses the
    first); screenshots asked are never probed. A Chromium that cannot begin a frame at all is not tried again."""
    asked = frame_capture_mode()
    if asked != "begin_frame":
        return FrameCapture(asked, "screenshot")
    missed: list[str] = []
    for wait in CAPTURE_PROBE_TRIES:
        try:
            seconds = await asyncio.wait_for(_probe_begun_frame(wait), _CAPTURE_PROBE_LIMIT_SECONDS)
        except TimeoutError:
            missed.append(f"{wait:g} s")
            continue
        except Exception as exc:  # a Chromium that cannot begin a frame: screenshots, never a failed render
            return FrameCapture(asked, "screenshot", f"no begun frame: {format_exception_message(exc)[:200]}")
        after = f" (none within {', then '.join(missed)} before)" if missed else ""
        return FrameCapture(asked, "begin_frame", f"a begun frame in {seconds:.2f} s{after}")
    return FrameCapture(asked, "screenshot", f"no begun frame within {', then '.join(missed)}")


async def _probe_begun_frame(wait: float = CAPTURE_PROBE_SECONDS) -> float:
    """Seconds to the first begun frame with a picture on a page of the compositor's Chromium; :class:`TimeoutError`
    past *wait* seconds (the page made and its renderer started included)."""
    from playwright.async_api import async_playwright

    from app.services.headless_render import RENDER_CHROMIUM_ARGS, RENDER_SERVICE_WORKERS

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True, args=[*RENDER_CHROMIUM_ARGS, *BEGIN_FRAME_ARGS])
        try:
            context = await browser.new_context(
                viewport={"width": 320, "height": 180}, service_workers=RENDER_SERVICE_WORKERS
            )
            began = time.monotonic()
            page = await _frame_page(browser, context, timeout=wait)
            if page is None:
                raise TimeoutError
            cdp = await context.new_cdp_session(page)
            shade = 0
            while True:
                left = wait - (time.monotonic() - began)
                if left <= 0:
                    raise TimeoutError
                # Something changed on screen each time: a begun frame with damage carries its picture.
                shade = (shade + 37) % 256
                await asyncio.wait_for(
                    page.evaluate("c => { document.documentElement.style.background = `rgb(${c},0,0)`; }", shade), left
                )
                done = await asyncio.wait_for(
                    cdp.send("HeadlessExperimental.beginFrame", {"screenshot": {"format": "png"}}),
                    max(0.01, wait - (time.monotonic() - began)),
                )
                if done.get("screenshotData"):
                    return time.monotonic() - began
        finally:
            await browser.close()


def chromium_failed(exc: BaseException) -> bool:
    """Whether *exc* is the render's Chromium failing it (:class:`ChromiumError`): a Playwright error - a protocol
    call refused, a page or a browser gone - or a protocol call past its timeout."""
    return isinstance(exc, TimeoutError) or type(exc).__module__.split(".", 1)[0] == "playwright"


async def _frame_page(browser: Any, context: Any, *, timeout: float = _FRAME_TIMEOUT_SECONDS) -> Any | None:
    """A page of *context* whose frames are produced only when one is asked (``enableBeginFrameControl``), or
    ``None`` when this Chromium cannot drive one within *timeout*: a page so created that cannot begin a frame never
    paints at all, so it is closed and the render takes screenshots on an ordinary page."""
    probe = await context.new_page()
    root = None
    try:
        session = await context.new_cdp_session(probe)
        context_id = (await session.send("Target.getTargetInfo"))["targetInfo"]["browserContextId"]
        await session.detach()
        root = await browser.new_browser_cdp_session()
        async with context.expect_page(timeout=min(15.0, timeout) * 1000) as created:
            await root.send(
                "Target.createTarget", {"url": "about:blank", "browserContextId": context_id, "enableBeginFrameControl": True}
            )
        page = await created.value
        check = await context.new_cdp_session(page)
        try:
            await asyncio.wait_for(check.send("HeadlessExperimental.beginFrame", {}), timeout)
        except Exception:
            with contextlib.suppress(Exception):
                await page.close()
            raise
        finally:
            with contextlib.suppress(Exception):
                await check.detach()
        return page
    except Exception as exc:
        logger.info("Render page: frames cannot be begun here (%s); screenshots are taken", format_exception_message(exc))
        return None
    finally:
        if root is not None:
            with contextlib.suppress(Exception):
                await root.detach()
        with contextlib.suppress(Exception):
            await probe.close()


#: What a page's renderer is told once a scene has ended: collect its garbage, then drop what it caches - the
#: decoded pictures and the resources of the scene gone - as under memory pressure. Each is best effort.
_PAGE_RELEASE = (("HeapProfiler.collectGarbage", {}), ("Memory.simulatePressureNotification", {"level": "critical"}))


async def _release_page_caches(page: _Page) -> None:
    """Release what *page*'s renderer keeps of the scenes that ended: a page holds a scene's worth at most."""
    for method, params in _PAGE_RELEASE:
        try:
            await asyncio.wait_for(page.cdp.send(method, params), _FRAME_TIMEOUT_SECONDS)
        except Exception as exc:  # a renderer that does not offer it keeps its caches: the render goes on
            logger.debug("Render page %d: %s not sent: %s", page.index, method, format_exception_message(exc))


#: How many times a frame is begun for its picture before the render gives up on the page.
_PICTURE_BEGINS = 30


async def _capture(page: _Page, shot_params: dict) -> bytes:
    """The frame *page* draws now, as its encoder takes it: begun and painted whole (``beginFrame``), else a screenshot.

    A begun frame with nothing changed on screen has no picture of its own: it is the one before. A page whose
    frames are begun never paints by itself, so it is never asked for a screenshot (one would wait forever); while
    its renderer starts up it answers begins without a picture, damaged or not, and is begun again until it gives one.
    """
    if page.begin_frame:
        for _ in range(_PICTURE_BEGINS):
            done = await asyncio.wait_for(
                page.cdp.send("HeadlessExperimental.beginFrame", {"screenshot": shot_params}), _FRAME_TIMEOUT_SECONDS
            )
            data = done.pop("screenshotData", None)
            if data:
                page.last_shot = base64.b64decode(data)
                return page.last_shot
            if page.last_shot is not None and not done.get("hasDamage"):
                return page.last_shot
        raise RuntimeError(f"Render page {page.index}: {_PICTURE_BEGINS} frames begun without a picture")
    shot = await asyncio.wait_for(page.cdp.send("Page.captureScreenshot", shot_params), _FRAME_TIMEOUT_SECONDS)
    # The frame goes to its encoder as bytes once: its base64 text is dropped as soon as it is decoded.
    captured = base64.b64decode(shot.pop("data"))
    del shot
    return captured


@dataclass
class _Page:
    """One page of a render: its Chromium context, its frame server and its place in the release schedule."""

    index: int
    server: FrameServer
    context: Any = None
    page: Any = None
    cdp: Any = None
    #: The next entry of the release schedule it has not passed.
    due: int = 0
    #: Whether its frames are begun one by one (``HeadlessExperimental.beginFrame``), and the last one
    #: captured: a frame that changes nothing on screen is that one again.
    begin_frame: bool = False
    last_shot: bytes | None = None


async def render_frames(
    document: dict,
    assets: CompositorAssets,
    *,
    preset: RenderPreset,
    output_size: tuple[int, int],
    workdir: str,
    font_css: str,
    pages: int,
    progress: ProgressFn | None = None,
    cancelled: Callable[[], Awaitable[bool]] | None = None,
    tracker: DrawProgress | None = None,
    on_frame: Callable[[], None] | None = None,
    frame_range: tuple[int, int] | None = None,
    audits: dict | None = None,
    render_id: str | None = None,
    crops: dict[str, Iterable[int]] | None = None,
    drawn_for: dict | None = None,
    scene_classes: dict[str, str] | None = None,
    frame_costs: page_schedule.FrameCosts | None = None,
    guard: dict[int, list[str]] | None = None,
    predecessors: Iterable[tuple[str, int]] = (),
    presence: dict[int, list[str]] | None = None,
) -> str:
    """Draw the frames of *document* on *pages* pages and encode them; the path of the silent video.

    The segments left to draw are shared out between the pages in
    contiguous runs of about the same cost, each page encoding its own, and a
    page done with its run takes what the others have left, down to the second
    half of a segment cut on a frame (``page_schedule.PageWork``): each
    frame priced by its kind at *frame_costs* - the previous render of its
    video's, else the platform's - until this render measured its own.
    *frame_range* ``(first, end)`` draws only those frames (a scene preview);
    every frame otherwise. *on_frame* is called after each frame is
    encoded (the render's lease, ``lease.FrameLease.frame_drawn``); what it
    raises stops the render. The segments a former attempt finished in
    *workdir* for the same drawing - the same document and the same *assets*,
    field by field (:func:`drawing_inputs`) - are kept, never drawn again
    (:func:`kept_segments`), the *tracker* saying which and who drew
    them (*drawn_for*: the revision and the attempt each segment closed
    records, :data:`DRAWN_SUFFIX`); with *render_id*, each segment closed is stored
    beside the render too, with its audits, and an attempt on another worker
    takes them (``render_segments``). *audits*, when given, gets what
    the matted screen inserts' frames showed (:func:`segment_audits`),
    kept segments included. *crops* names, per screen insert, the output
    frames whose insert region is cut from the captured frame at the render's
    resolution (``FrameServer.crop``), handed back in *audits*. Each
    frame is timed under its kind and its scene's type, *scene_classes*' when
    given (``render_preflight.scene_classes``). *guard* names the
    output frames the render's guard reads and the keyed inserts drawn on
    each (``insert_checks.guard_frames``): where the page places each
    insert on them and how much of it shows, and each of their scenes drawn
    alone on them, are handed back in *audits* too (``placed``, ``alone``).
    *predecessors* ``(render id, revision)`` are the renders of older
    revisions this one replaced: each segment they stored whose
    frames are drawn from what the same frames are drawn from here
    (:class:`WindowDigests`) is taken, never drawn again, and the *tracker*
    says which, by scene (``inherited``).
    *presence* names the output frames the presence gate reads
    (``insert_checks.presence_frames``): where the page places each insert on
    them is handed back too. A frame that paints a scene past the painted end
    of an insert that runs to its end, or asks that insert partly shown once
    in or on a frame its scene does not own (``timeline.whole_frames``), is
    handed back as its ``overrun`` - judged, like every audit, on the frame's
    centre sub-frame alone under motion blur (``FrameServer.shutter``).
    """

    from app.services.headless_render import RENDER_SERVICE_WORKERS
    from app.services.video_studio import render_segments

    design_w, design_h = int(document["width"]), int(document["height"])
    out_w, out_h = output_size
    scale = out_w / design_w
    first, end = frame_range or (0, int(document["frames"]))
    first, end = max(0, first), min(int(document["frames"]), end)
    total = max(1, end - first)
    fps = int(document["fps"])
    segments_dir = Path(workdir) / SEGMENTS_DIR
    parts = await asyncio.to_thread(drawing_inputs, document, preset, output_size, (first, end), assets)
    digest = digest_of(parts)
    finished = await asyncio.to_thread(kept_segments, segments_dir, digest)
    if render_id is not None and await render_segments.restore(render_id, digest, segments_dir):
        finished = await asyncio.to_thread(_finished, segments_dir)
    windows = WindowDigests(document, preset, output_size, assets)
    state = tracker if tracker is not None else DrawProgress()
    if render_id is not None:
        # Its own segments stored under an earlier digest: a worker change made a file of the render again,
        # named by its bytes; each segment whose own frames are drawn from the same inputs is taken back, not drawn.
        state.taken_back = await render_segments.take_back(render_id, digest, segments_dir, (first, end), windows, set(finished))
        if state.taken_back:
            finished = await asyncio.to_thread(_finished, segments_dir)
    taken: set[tuple[int, int]] = set()
    if render_id is not None and (predecessors := list(predecessors)):
        state.inherited = await render_segments.take_over(
            predecessors, render_id, digest, segments_dir, (first, end), windows, set(finished)
        )
        if state.inherited:
            finished = await asyncio.to_thread(_finished, segments_dir)
            taken = {tuple(piece) for run in state.inherited for piece in run["pieces"]}
            state.inherited_frames = sum(b - a for a, b in taken)
    stored: list[asyncio.Task] = []
    pieces = plan_segments(first, end, segment_frames(total, fps), finished)
    html = page_html(font_css)
    schedule = release_schedule(document)
    budget = decoder_budget(document)
    kept = timeline.loop_frames(document)
    whole = timeline.whole_frames(document)
    state.total = total
    state.done = state.kept = sum(b - a for a, b in pieces if (a, b) in finished)
    if own := [piece for piece in pieces if piece in finished and piece not in taken]:
        state.reused = await asyncio.to_thread(kept_from, segments_dir, own)
        state.matched = parts
        logger.info(
            "Render frames: %d of %d kept from an attempt before (%s), its digest matching on %s; drawn again from there",
            sum(b - a for a, b in own),
            total,
            "; ".join(
                f"frames {run['frames'][0]}-{run['frames'][1]} drawn for revision {run.get('revision')} by the attempt of {run.get('attempt')}"
                for run in state.reused
            ),
            ", ".join(f"{name} {value}" for name, value in parts.items()),
        )
    kinds = render_preflight.FrameKinds.of(document, classes=scene_classes)
    # Motion blur: each frame the average of sub-frames drawn over a film shutter, at high and ultra only.
    samples = state.samples = motion_design.blur_samples((document.get("theme") or {}).get("motion"), preset.name)
    offsets = motion_design.blur_offsets(samples)
    # The seconds of each cut: a sub-frame never drawn across one (``frame_owners.shutter_at``).
    cuts = frame_owners.cut_times(document)
    final_frame = max(0, int(document["frames"]) - 1)
    # The pieces the pages draw: runs of the same cost, then what the others have left, each frame priced by
    # its kind - at what this attempt draws of it once it drew enough, at *frame_costs* until then.
    state.prior = (
        frame_costs
        if frame_costs is not None
        else page_schedule.FrameCosts(segment=render_preflight.DEFAULT_SEGMENT_PER_MEGAPIXEL * out_w * out_h / 1e6)
    )
    work = state.work = page_schedule.PageWork(
        [piece for piece in pieces if piece not in finished],
        pages,
        page_schedule.FramePrices(kinds, first, end),
        state.costs(),
        warm=matted_spans(document, assets),
        kept=[piece for piece in pieces if piece in finished],
    )
    # Each page's encoder runs on its page's share of the CPUs, never on every CPU of the worker.
    threads = state.encoder_threads = render_preflight.encoder_threads(max(1, work.pages))
    if work.pages:
        logger.info(
            "Render frames: %d to draw on %d pages, %d encoder threads each, each frame priced by its kind (%s)",
            total - state.done,
            work.pages,
            threads,
            ", ".join(f"{kind} {work.costs.of(kind):.2f} s ({work.costs.sources.get(kind)})" for kind in render_preflight.FRAME_KINDS),
        )
    shot_params: dict = {"format": preset.frame_format, "optimizeForSpeed": True}
    if preset.frame_format == "jpeg":
        shot_params["quality"] = preset.jpeg_quality
    done_lock = asyncio.Lock()
    last_report = 0.0

    async def report(n: int) -> None:
        nonlocal last_report
        async with done_lock:
            state.done += n
            now = time.monotonic()
            state.rate.note(now, state.done)
            if progress and (now - last_report > 2.0 or state.done >= total):
                last_report = now
                await progress(int(100 * state.done / max(total, 1)))

    async def flushing() -> None:
        # A segment's encoder flushing its lookahead draws no frame: still the render's work, for its lease.
        if on_frame is not None:
            on_frame()

    async def arrive(page: _Page, frame: int) -> None:
        """*page* at *frame*: the media of the scenes ended by then released."""
        ended: list[str] = []
        while page.due < len(schedule) and schedule[page.due][0] <= frame:
            ended.extend(schedule[page.due][1])
            page.due += 1
        if ended:
            await page.server.release(ended)
            await _release_page_caches(page)
        state.positions[page.index] = frame

    async def warm_up(page: _Page, first_frame: int, frames: int) -> None:
        """The *frames* frames before *first_frame* drawn on *page*, uncaptured: what one page drawing on would
        have carried into it - a matted insert's bezel lock and the window of its matte. Their audits are the page's
        that draws them."""
        began = time.monotonic()
        # No evidence frame written for them: the page drawing them writes its own, under the same names.
        evidence, page.server.evidence_dir = page.server.evidence_dir, None
        try:
            for frame in range(first_frame - frames, first_frame):
                await arrive(page, frame)
                for offset in offsets:
                    at = min(max(frame + offset, 0.0), float(final_frame)) if offset else frame
                    at = frame_owners.shutter_at(frame, at, fps, cuts) if offset else frame
                    page.server.shutter = bool(offset)
                    await asyncio.wait_for(page.page.evaluate("i => window.__xtm.render(i)", at), _FRAME_TIMEOUT_SECONDS)
                if on_frame is not None:
                    on_frame()
        finally:
            page.server.evidence_dir = evidence
            page.server.shutter = False
        page.server.take_audits(first_frame - frames, first_frame)
        state.warmed(page.index, frames, time.monotonic() - began)

    async def draw(page: _Page, frame: int) -> list[bytes]:
        """Frame *frame* drawn and captured on *page* - each of its sub-frames under motion blur -, its ended scenes'
        media released first."""
        await arrive(page, frame)
        page.server.start_frame()
        shots: list[bytes] = []
        blended: list[str] = []
        for offset in offsets:
            # Never before the first frame nor past the last: the shutter opens on what the video shows.
            at = min(max(frame + offset, 0.0), float(final_frame)) if offset else frame
            # Never across a cut: a frame next to one is one scene's picture, never a mix of the two.
            at = frame_owners.shutter_at(frame, at, fps, cuts) if offset else frame
            page.server.shutter = bool(offset)
            try:
                await asyncio.wait_for(page.page.evaluate("i => window.__xtm.render(i)", at), _FRAME_TIMEOUT_SECONDS)
            finally:
                page.server.shutter = False
            captured = await _capture(page, shot_params)
            read = not offset and (
                frame in page.server.guard
                or frame in page.server.presence
                or any(frame in frames for frames in page.server.crops_wanted.values())
            )
            if read or (not offset and page.server.unaudited(frame)):
                rects = await asyncio.wait_for(page.page.evaluate("() => window.__xtm.insertRects()"), _FRAME_TIMEOUT_SECONDS)
                # What the centre shows of a matted insert a sub-frame before it asked for, audited for this frame.
                await page.server.centre_shown(frame, rects or {})
            if read:
                # The insert regions of this frame, as the page places them, cut from the frame as captured, and
                # kept for the guard and the presence gate that read it once encoded.
                page.server.place(frame, rects or {}, (design_w, design_h))
                await page.server.crop(frame, captured, rects or {}, (design_w, design_h))
                if frame in page.server.presence:
                    blended = page.server.blend_scenes(frame, rects or {})
            shots.append(captured)
        if blended:
            # Each scene a presence frame blends an insert of, painted black then white: the presence gate
            # unblends the encoded frame with them. Never encoded, like a scene drawn alone.
            encoded = page.last_shot
            for scene_id in blended:
                for tone in ("black", "white"):
                    toned = await asyncio.wait_for(
                        page.page.evaluate("([i, id, w]) => window.__xtm.tone(i, id, w)", [frame, scene_id, tone == "white"]),
                        _FRAME_TIMEOUT_SECONDS,
                    )
                    if toned:
                        shot = await _capture(page, {"format": "png", "optimizeForSpeed": True})
                        await page.server.keep_blend(frame, scene_id, tone, shot)
            page.last_shot = encoded
        if frame in page.server.guard and page.server.evidence_dir is not None:
            # Each scene of a guard's frame with a keyed insert, drawn alone: its own finished frame as the page
            # composes it, never blended with the scene beside it - what the guard reads for the key. Never encoded:
            # the frame a begun page repeats when nothing changes stays the last one encoded.
            encoded = page.last_shot
            for scene_id in page.server.guard_scenes(frame):
                rects = await asyncio.wait_for(
                    page.page.evaluate("([i, id]) => window.__xtm.alone(i, id)", [frame, scene_id]), _FRAME_TIMEOUT_SECONDS
                )
                shot = await _capture(page, {"format": "png", "optimizeForSpeed": True})
                await page.server.keep_alone(frame, scene_id, shot, rects or {}, (design_w, design_h))
            page.last_shot = encoded
        return shots

    async def segment(page: _Page, piece: page_schedule.Piece) -> None:
        """The frames of *piece* drawn by *page* and encoded to their segment, named by the frames it ends with and
        renamed whole once closed: another page may take its tail meanwhile (``page_schedule.PageWork.take``)."""
        if work.left_off.get(page.index, piece.first) != piece.first:
            # Elsewhere on the timeline: the scenes released from the start, nothing an insert carried kept.
            page.due = 0
            await page.server.jumped()
        if warm := work.preroll(page.index, piece.first):
            await warm_up(page, piece.first, warm)
        writing = segments_dir / f"seg-{piece.first:07d}.part.mp4"
        pipe = encoder.FramePipeEncoder(
            out_path=str(writing),
            fps=fps,
            frame_format=preset.frame_format,
            crf=preset.crf,
            preset=preset.x264_preset,
            width=out_w,
            height=out_h,
            threads=threads,
            subframes=samples,
        )
        pending = 0
        try:
            await pipe.start()
            while piece.next < piece.end:
                if handing_over():
                    # Handed over: the segment ends at the frame reached, closed and stored like a whole one,
                    # so the next attempt resumes there and not at the start of the segment.
                    piece.end = piece.next
                    break
                frame = piece.next
                piece.next += 1
                if cancelled is not None and frame % 30 == 0 and await cancelled():
                    raise RenderCancelledError("The render was cancelled.")
                began = time.monotonic()
                for shot in await draw(page, frame):
                    await asyncio.wait_for(pipe.write(shot), _FRAME_TIMEOUT_SECONDS)
                state.timed(
                    kinds.kind(frame), time.monotonic() - began, page.index, scene=kinds.scene(frame), reused=page.server.frame_reused
                )
                if on_frame is not None:
                    on_frame()
                pending += 1
                if pending >= 15:
                    await report(pending)
                    pending = 0
            if piece.end <= piece.first:
                await pipe.abort()
                return
            await pipe.finish(on_wait=flushing)
        except BaseException:
            await pipe.abort()
            raise
        target = segments_dir / segment_name(piece.first, piece.end)
        # The frames the page painted a scene on past the painted end of an insert that runs to its end.
        page.server.overran(await asyncio.wait_for(page.page.evaluate("() => window.__xtm.overruns()"), _FRAME_TIMEOUT_SECONDS) or [])
        audited = page.server.take_audits(piece.first, piece.end)
        if audited:
            await asyncio.to_thread(
                (segments_dir / (target.stem + AUDIT_SUFFIX)).write_text, json.dumps(audited), encoding="utf-8"
            )
        window = await asyncio.to_thread(windows.of, piece.first, piece.end)
        record = {name: (drawn_for or {}).get(name) for name in DRAWN_FIELDS}
        await asyncio.to_thread(
            (segments_dir / (target.stem + DRAWN_SUFFIX)).write_text,
            json.dumps({**record, "render": render_id, "window": window}, default=str),
            encoding="utf-8",
        )
        writing.replace(target)
        if render_id is not None:
            stored.append(asyncio.create_task(render_segments.keep(render_id, digest, target)))
        if pending:
            await report(pending)

    async def chunk(browser: Any, index: int) -> None:
        """The pieces one page of its own draws in turn: its run, then what the other pages left, until nothing left
        is worth taking (``page_schedule.PageWork.take``), each kind priced at what it costs by then."""
        opened = time.monotonic()
        page = _Page(
            index,
            FrameServer(
                assets,
                html,
                max_open=budget,
                keep=kept,
                evidence_dir=segments_dir / EVIDENCE_DIR,
                crops=crops,
                guard=guard,
                presence=presence,
                whole=whole,
            ),
        )
        page.context = await browser.new_context(
            viewport={"width": design_w, "height": design_h},
            device_scale_factor=scale,
            service_workers=RENDER_SERVICE_WORKERS,
        )
        try:
            await page.context.route("**/*", page.server.handle)
            framed = await _frame_page(browser, page.context) if capture == "begin_frame" else None
            page.begin_frame = framed is not None
            page.page = framed or await page.context.new_page()
            await page.page.goto(f"{ORIGIN}/index.html", wait_until="load")
            await page.page.evaluate("doc => window.__xtm.load(doc)", document)
            page.cdp = await page.context.new_cdp_session(page.page)
            drawn = 0
            while not handing_over():
                work.costs = state.costs()
                piece = work.take(index)
                if piece is None:
                    break
                await segment(page, piece)
                drawn += 1
            state.idle_at[index] = time.monotonic()
            state.ran(index, drawn, time.monotonic() - opened)
        finally:
            state.positions.pop(index, None)
            state.peak_open_decoders = max(state.peak_open_decoders, page.server.peak_open)
            state.insert_cached += page.server.insert_cached
            state.insert_drawn += page.server.insert_drawn
            await page.server.close()
            with contextlib.suppress(Exception):  # best effort
                await page.context.close()

    capture = await render_capture() if work.pages else "screenshot"
    try:
        if work.pages:
            await _on_browsers(capture, work.pages, chunk)
        state.drawn_at = time.monotonic()
        if work.pages:
            logger.info(
                "Render frames: %d pieces on %d pages, %d taken whole from another page's run and %d cut on a frame, "
                "%d frames drawn uncaptured into a matted insert (%.0f s); each page's seconds without a frame left "
                "while another drew: %s",
                len(work.plan()) - len(work.kept),
                work.pages,
                work.moved,
                work.cuts,
                state.warm_frames,
                state.warm_seconds,
                state.idle(),
            )
        if (inserts := state.insert_frames()) is not None:
            logger.info(
                "Render frames: %d screen-insert frames read back from this worker's disk, %d drawn: %.0f%% reused",
                inserts["cached"],
                inserts["drawn"],
                100 * inserts["share"],
            )
    except Exception as exc:
        if not chromium_failed(exc):
            raise
        # Nothing in the video does this: said as Chromium's, the render runs again (``render_retry``).
        raise ChromiumError(
            f"The render's headless Chromium failed ({capture} capture): {format_exception_message(exc)}"
        ) from exc
    finally:
        # The segments closed are stored whatever ends the drawing: a resumed attempt needs them most then.
        if stored:
            await asyncio.gather(*stored, return_exceptions=True)
    if handing_over():
        raise RenderHandedOverError("The render was handed over: its open segments closed at the frames reached.")
    pieces = work.plan()
    parts = [str(segments_dir / segment_name(a, b)) for a, b in pieces]
    if audits is not None:
        audits.update(await asyncio.to_thread(segment_audits, segments_dir, pieces))
    video_path = str(Path(workdir) / "video.mp4")
    await encoder.concat(parts, out_path=video_path, workdir=str(segments_dir))
    return video_path
