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

"""A finished take repaired without recording it again.

A take is recorded once and its frames repaired three times on the way to its MP4: the canvases the screencast fell
short on composited from their copies alone (``canvas_alone.composite``), its scrolls, zooms and transitions
drawn again at its rate from the keyframes the browser painted (``scroll_pacing.pace``), its capture measured
(``capture_rate.measure``). Each of those passes improves, and a take recorded before keeps what the pass
did at its time. Repairing it runs the passes of today on what the take was made from, into a new take, the old one
kept.

Two sources, best first:

* **Its keyframes** (:func:`pack_keyframes`): a take that has them keeps what its recorder handed over before any
  pass - the screencast's keyframes, the canvases' copies alone, the scroll offset each keyframe painted, its raw
  events (each scroll's samples, each transition's windows), its capture summary, duration, pixel ratio, viewport and
  pointer path - as one ``keyframes.zip`` take file (``capture.keyframes``). A repair from it is exact.
* **Its MP4** (:func:`select_painted`), for a take without keyframes: its 60 fps frames decoded, only those that changed
  kept (a hold is one frame), the frames a paced span drew dropped but those at the rate its page painted it, the
  frames of an off-scale run dropped (the frame before holds), each scroll's raw samples rebuilt from the log
  it kept (:func:`raw_events`). Approximate by nature - a re-encode at crf 14, no canvas copied alone -: its answer
  names what it could not repair.

Nothing here needs a GPU: the passes run on the CPU like the take did.
"""

from __future__ import annotations

import copy
import json
import math
import zipfile
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from app.services.video_studio import canvas_alone
from app.services.video_studio.take_window import FRAME_LIST_KEY

#: The version of the archive's manifest: a reader refuses one it does not know.
KEYFRAMES_VERSION = 1
#: The archive's file name, and the manifest's inside it.
KEYFRAMES_NAME = "keyframes.zip"
MANIFEST = "manifest.json"
#: What an event of a finished take holds that its passes wrote (``scroll_pacing.summarise``, ``capture_rate.measure``):
#: dropped from the raw events a repair from the MP4 rebuilds, the passes writing them again.
PASS_FIELDS = frozenset(
    {"scroll_cadence", "painted_fps", "fps", "scrolled_element", "capture_fps", "canvas_motion", "synthesised"}
)
#: The ``pixels`` a rebuilt wheel's turns are said in (``scroll_pacing._wheel`` keeps a turn off a straight line by
#: half a pixel): its shares kept to 1/2000 of the scroll.
_WHEEL_PIXELS = 1000.0
#: The id a scroll container rebuilt from a kept log is named by in its samples.
_ELEMENT_ID = "repaired"


#: Where a take's ``capture`` names its keyframes file (:func:`pack_keyframes`, a ``SOURCE_TAKE`` file it owns).
CAPTURE_KEY = "keyframes"
#: Where a queued repair's ``capture`` names the take it repairs (:func:`repair_source`), and where the take it made
#: says what it repaired (:func:`repaired`, :func:`residual`): ``{of, from, spans, residual}``.
REPAIR_KEY = "repair_of"
REPAIRED_KEY = "repair"
#: Where a take still running names what the next worker finishes it from once its capture is done and its
#: keyframes stored (:func:`resume_point`): ``{notes, layout, fps_fallback}``, beside ``capture.keyframes``.
RESUME_KEY = "resume"
#: What a finished take's ``capture`` holds that its passes and its save wrote (``capture_rate.measure``): dropped from
#: the recorder's summary a repair from its MP4 starts from (``alone``: no canvas was copied alone into an MP4).
MEASURED_FIELDS = frozenset(
    {
        "fps",
        "rates",
        "low",
        "stepping",
        "off_scale",
        "sparse",
        "alone",
        CAPTURE_KEY,
        FRAME_LIST_KEY,
        REPAIR_KEY,
        REPAIRED_KEY,
        RESUME_KEY,
    }
)
#: A repair from the MP4 tells a frame that changed on grey thumbnails this wide, by a level difference above this.
_THUMB_WIDTH = 240
_CHANGED_LEVELS = 3
#: The two routes of a repair (``capture.repair.from``).
FROM_KEYFRAMES = "keyframes"
FROM_MP4 = "mp4"


def keyframes_file(capture: dict | None) -> str | None:
    """The id of the keyframes file a take's *capture* names (``capture.keyframes``); ``None`` without one."""
    found = (capture or {}).get(CAPTURE_KEY) if isinstance(capture, dict) else None
    return str(found) if found else None


def repair_source(capture: dict | None) -> str | None:
    """The id of the take a queued take repairs (``capture.repair_of``); ``None``: a take to record."""
    found = capture.get(REPAIR_KEY) if isinstance(capture, dict) else None
    return str(found) if found else None


def resume_point(capture: dict | None) -> dict | None:
    """What a take its worker's stop tore down after its capture is finished from (``capture.resume``, with the id of
    its stored keyframes as ``keyframes``); ``None``: a take to record from its script."""
    if not isinstance(capture, dict):
        return None
    found, kept = capture.get(RESUME_KEY), keyframes_file(capture)
    if not isinstance(found, dict) or not kept:
        return None
    return {**found, "keyframes": kept}


def hold_source(source: Any) -> None:
    """A recording a repair is queued from (*source*, a ``VideoTake``) used now (not committed): its retention counts
    from the repair, and the sweep keeps it while the repair runs (``retention.repaired_now``)."""
    from datetime import UTC, datetime

    source.last_used_at = datetime.now(UTC)


def recorded_summary(capture: dict | None) -> dict:
    """What the recorder said of a finished take's capture (its GL, backend, pixel ratio, warm-up, what it opened on),
    without what its passes and its save wrote (:data:`MEASURED_FIELDS`): what a repair from its MP4 measures again."""
    return {k: copy.deepcopy(v) for k, v in (capture or {}).items() if k not in MEASURED_FIELDS}


class KeyframesError(ValueError):
    """A keyframes archive that cannot be read: not one, a version unknown, a file it names missing."""


@dataclass
class Source:
    """What a take was made from (:func:`unpack_keyframes`): its keyframes, in the workdir they were unpacked to."""

    frames: list[tuple[float, str]]
    events: list[dict]
    duration: float
    pixel_ratio: float
    viewport: tuple[int, int]
    fps: int
    cursor: list[list[float]] = field(default_factory=list)
    painted: dict[str, float] = field(default_factory=dict)
    capture: dict = field(default_factory=dict)
    alone: list[canvas_alone.AloneFrame] = field(default_factory=list)


def _alone_dict(copy_: canvas_alone.AloneFrame) -> dict:
    return {
        "t": copy_.t,
        "surface": copy_.surface,
        "canvas": copy_.canvas,
        "clip": list(copy_.clip),
        "over": [list(box) for box in copy_.over],
        "file": copy_.file,
    }


def _alone_frame(raw: dict) -> canvas_alone.AloneFrame:
    return canvas_alone.AloneFrame(
        float(raw["t"]),
        str(raw["surface"]),
        str(raw["canvas"]),
        tuple(float(v) for v in raw["clip"]),  # type: ignore[arg-type]
        tuple(tuple(float(v) for v in box) for box in raw.get("over") or []),  # type: ignore[misc]
        str(raw["file"]),
    )


def pack_keyframes(
    out_path: str | Path,
    *,
    workdir: str | Path,
    frames: Sequence[tuple[float, str]],
    events: list[dict],
    duration: float,
    pixel_ratio: float,
    viewport: tuple[int, int],
    fps: int,
    cursor: list | None = None,
    painted: dict[str, float] | None = None,
    capture: dict | None = None,
    alone: Sequence[canvas_alone.AloneFrame] = (),
) -> int:
    """Write what a take was made from to *out_path* (a zip, its JPEGs stored as they are): the keyframes *frames*
    (``(seconds, file name in workdir)``) and the canvases' copies *alone*, every file read from *workdir*, and a
    manifest of the rest. *events* must be the recorder's, before any pass rewrote them. Answers the files packed."""
    root = Path(workdir)
    names = list(dict.fromkeys([name for _t, name in frames] + [c.file for c in alone]))
    manifest = {
        "version": KEYFRAMES_VERSION,
        "frames": [[round(float(t), 6), name] for t, name in frames],
        "events": events,
        "duration": float(duration),
        "pixel_ratio": float(pixel_ratio),
        "viewport": [int(viewport[0]), int(viewport[1])],
        "fps": int(fps),
        "cursor": cursor or [],
        "painted": painted or {},
        "capture": capture or {},
        "alone": [_alone_dict(c) for c in alone],
    }
    packed = 0
    with zipfile.ZipFile(out_path, "w", compression=zipfile.ZIP_STORED) as archive:
        archive.writestr(MANIFEST, json.dumps(manifest, separators=(",", ":"), default=str))
        for name in names:
            path = root / name
            if path.is_file():
                archive.write(path, arcname=f"frames/{name}")
                packed += 1
    return packed


def unpack_keyframes(zip_path: str | Path, workdir: str | Path) -> Source:
    """The take *zip_path* packs (:func:`pack_keyframes`), its files written to *workdir*."""
    root = Path(workdir)
    try:
        with zipfile.ZipFile(zip_path) as archive:
            manifest = json.loads(archive.read(MANIFEST))
            if manifest.get("version") != KEYFRAMES_VERSION:
                raise KeyframesError(f"keyframes version {manifest.get('version')!r} is not {KEYFRAMES_VERSION}")
            for info in archive.infolist():
                if not info.filename.startswith("frames/") or info.is_dir():
                    continue
                name = Path(info.filename).name
                if not name or name != info.filename[len("frames/") :]:
                    raise KeyframesError(f"unexpected file {info.filename!r} in the keyframes")
                (root / name).write_bytes(archive.read(info))
    except (zipfile.BadZipFile, KeyError, json.JSONDecodeError, OSError) as exc:
        raise KeyframesError(f"not a keyframes archive: {exc}") from exc
    frames = [(float(t), str(name)) for t, name in manifest["frames"]]
    missing = [name for _t, name in frames if not (root / name).is_file()]
    if missing:
        raise KeyframesError(f"{len(missing)} keyframe(s) missing, the first {missing[0]!r}")
    return Source(
        frames=frames,
        events=list(manifest.get("events") or []),
        duration=float(manifest["duration"]),
        pixel_ratio=float(manifest["pixel_ratio"]),
        viewport=(int(manifest["viewport"][0]), int(manifest["viewport"][1])),
        fps=int(manifest["fps"]),
        cursor=list(manifest.get("cursor") or []),
        painted={str(k): float(v) for k, v in (manifest.get("painted") or {}).items()},
        capture=dict(manifest.get("capture") or {}),
        alone=[_alone_frame(raw) for raw in manifest.get("alone") or []],
    )


# ── Raw events rebuilt from a finished take's (the MP4 route) ─────────────────


def raw_scroll(kept: dict) -> dict | None:
    """The recorder's entry (``scroll_pacing.read_scrolls``'s ``{t, samples, names?, surface?, wheel?}``) of a scroll a
    finished take kept (``{t, scroller, log, element?, box?, progress?, wheel?, anchor?, over?, ...}``), rebuilt from its
    log: a window's offsets, a scroll container's (its box and name kept), a canvas's zoom (its 2D transform's scale,
    or the wheel's delta). ``None`` for an entry without a span or a log of two samples."""
    span, log = kept.get("t"), kept.get("log")
    if not (isinstance(span, list | tuple) and len(span) == 2 and isinstance(log, list) and len(log) >= 2):
        return None
    try:
        start, end = float(span[0]), float(span[1])
        samples_log = [(float(t), float(value)) for t, value in log]
    except (TypeError, ValueError):
        return None
    raw: dict[str, Any] = {"t": [start, end]}
    scroller = kept.get("scroller") or "window"
    if scroller == "canvas":
        box = kept.get("box")
        if not (isinstance(box, list | tuple) and len(box) == 4):
            return None
        if kept.get("progress") == "transform":
            ticks = [[t, 0.0, [math.exp(v), 0.0, 0.0, math.exp(v), 0.0, 0.0]] for t, v in samples_log]
        else:
            ticks = [[t, v, None] for t, v in samples_log]
        x, y, w, h = (float(v) for v in box)
        anchor = kept.get("anchor")
        surface: dict[str, Any] = {
            "box": [x, y, w, h],
            "at": list(anchor) if isinstance(anchor, list | tuple) and len(anchor) == 2 else [x + w / 2, y + h / 2],
            "ticks": ticks,
        }
        if isinstance(kept.get("over"), list):
            surface["over"] = [list(b) for b in kept["over"] if isinstance(b, list | tuple) and len(b) == 4]
        raw["samples"] = [[start, 0.0, 0.0], [end, 0.0, 0.0]]
        raw["surface"] = surface
        return raw
    if scroller == "element":
        box = kept.get("box")
        if not (isinstance(box, list | tuple) and len(box) == 4):
            return None
        boxed = [float(v) for v in box]
        raw["samples"] = [[t, 0.0, 0.0, [[_ELEMENT_ID, v, 0.0, boxed]]] for t, v in samples_log]
        raw["names"] = {_ELEMENT_ID: kept.get("element") or ""}
    else:
        raw["samples"] = [[t, v, 0.0] for t, v in samples_log]
    wheel = kept.get("wheel")
    if isinstance(wheel, list) and len(wheel) >= 2:
        raw["wheel"] = {
            "t0": 0.0,
            "pixels": _WHEEL_PIXELS,
            "turns": [[float(t), float(share) * _WHEEL_PIXELS] for t, share in wheel],
        }
    return raw


def raw_events(events: list[dict]) -> list[dict]:
    """A finished take's *events* as its recorder logged them, near enough for its passes to run again: each scroll
    (:func:`raw_scroll`; one that cannot be rebuilt is kept as it was, never paced again), each transition window back to
    ``[start, end, element]``, what the passes wrote (:data:`PASS_FIELDS`) dropped. *events* is not changed."""
    out = []
    for event in events:
        rebuilt = {k: copy.deepcopy(v) for k, v in event.items() if k not in PASS_FIELDS}
        if isinstance(event.get("scrolls"), list):
            rebuilt["scrolls"] = [
                raw_scroll(kept) or copy.deepcopy(kept) if isinstance(kept, dict) else kept for kept in event["scrolls"]
            ]
        if isinstance(event.get("motion"), list):
            windows = []
            for window in event["motion"]:
                span = window.get("t") if isinstance(window, dict) else window
                if isinstance(span, list | tuple) and len(span) >= 2:
                    element = window.get("element") if isinstance(window, dict) else (span[2] if len(span) > 2 else "")
                    windows.append([float(span[0]), float(span[1]), element or ""])
            rebuilt["motion"] = windows
        out.append(rebuilt)
    return out


# ── The painted frames of an MP4 (the MP4 route) ──────────────────────────────


@dataclass(frozen=True)
class Span:
    """A stretch of a finished take a pass drew (``[start, end]`` seconds of the take) and the rate its page painted it
    at (keyframes a second): only frames at that rate are kept of it."""

    start: float
    end: float
    painted: float


def paced_spans(events: list[dict]) -> list[Span]:
    """The stretches a finished take's scrolls and transitions were drawn over (``paced``), each with the rate its
    page painted it at (``painted``)."""
    spans = []
    for event in events:
        for entry in [*(event.get("scrolls") or []), *(event.get("motion") or [])]:
            if not isinstance(entry, dict) or not entry.get("paced"):
                continue
            span = entry.get("t")
            try:
                start, end = float(span[0]), float(span[1])
                painted = float(entry.get("painted") or 0.0)
            except (TypeError, ValueError, IndexError):
                continue
            if end > start:
                spans.append(Span(start, end, painted))
    return sorted(spans, key=lambda s: s.start)


def fault_spans(capture: dict | None) -> list[tuple[float, float]]:
    """The stretches of off-scale frames a finished take kept (``capture.off_scale``)."""
    found = []
    for raw in (capture or {}).get("off_scale") or []:
        try:
            start, end = (float(v) for v in raw["t"])
        except (KeyError, TypeError, ValueError):
            continue
        found.append((start, end))
    return found


#: The precision an off-scale run's bounds are stored with (``round(t, 3)``): half a millisecond either way; a
#: frame at 60 fps is 16.7 ms from the next.
_FAULT_TOLERANCE = 5e-4


def in_fault(t: float, faults: Sequence[tuple[float, float]]) -> bool:
    """Whether the frame at *t* seconds lies in an off-scale run of *faults* (:func:`fault_spans`). A run is half-open:
    its start is its first off-scale frame, its end the first valid frame after it (``capture_rate.capture_fault``).
    Both bounds are read at the precision they were stored with (:data:`_FAULT_TOLERANCE`): the start widened, the end
    narrowed, so the first valid frame after a run is never taken for it. Both routes of a repair drop by it."""
    return any(a - _FAULT_TOLERANCE <= t < b - _FAULT_TOLERANCE for a, b in faults)


def select_painted(
    times: Sequence[float],
    changed: Sequence[bool],
    *,
    spans: Sequence[Span] = (),
    faults: Sequence[tuple[float, float]] = (),
) -> list[int]:
    """The frames of a finished take's MP4 (decoded at *times*, each one *changed* from the one before or not) to
    keep as its keyframes: the first, and each that changed; inside a paced span (*spans*) only those nearest the
    instants its page painted at (its rate from its start, its last frame kept), never one a pass drew between; none
    inside an off-scale run (*faults*: the frame before holds)."""
    eps = 1e-6
    picked: set[int] = set()
    for index, (t, moved) in enumerate(zip(times, changed, strict=True)):
        if index != 0 and not moved:
            continue
        if in_fault(t, faults):
            continue
        if any(s.start - eps <= t <= s.end + eps for s in spans):
            continue
        picked.add(index)
    for span in spans:
        inside = [i for i, t in enumerate(times) if span.start - eps <= t <= span.end + eps]
        if not inside:
            continue
        wanted = [span.start]
        if span.painted > 0:
            step = 1.0 / span.painted
            k = 1
            while span.start + k * step < span.end - eps:
                wanted.append(span.start + k * step)
                k += 1
        wanted.append(span.end)
        for instant in wanted:
            nearest = min(inside, key=lambda i: abs(times[i] - instant))
            if not in_fault(times[nearest], faults):
                picked.add(nearest)
    return sorted(picked)


def drop_faults(frames: Sequence[tuple[float, str]], faults: Sequence[tuple[float, float]]) -> list[tuple[float, str]]:
    """A take's keyframes *frames* without those inside an off-scale run (*faults*, :func:`fault_spans`): the frame
    before holds over the run, the first frame always kept - what the MP4 route's :func:`select_painted` keeps too."""
    return [(t, name) for index, (t, name) in enumerate(frames) if index == 0 or not in_fault(t, faults)]


def painted_count(route: str, *, recorded_frames: int | None, kept: int) -> int | None:
    """The frames a repaired take says its page painted (``captured_frames``, what ``scroll_pacing.take_rate`` divides
    by its length): from its keyframes, the *kept* keyframes (what its browser painted, less an off-scale run); from its
    MP4, the count the take repaired recorded (*recorded_frames*), ``None`` when it recorded none (unknown, as the take
    repaired said) - never the frames chosen off its MP4, which leave a hold out."""
    if route == FROM_MP4:
        return int(recorded_frames) if recorded_frames else None
    return int(kept)


class _Changes:
    """Whether each grey thumbnail of a decoded MP4 changed from the one before (the first always), one at a time: only
    the one before is held."""

    def __init__(self) -> None:
        self.before: Any = None

    def __call__(self, thumb: bytes) -> bool:
        import numpy as np

        now = np.frombuffer(thumb, dtype=np.uint8).astype(np.int16)
        before, self.before = self.before, now
        return before is None or now.shape != before.shape or bool(np.any(np.abs(now - before) > _CHANGED_LEVELS))


def changed_flags(thumbs: Iterable[bytes]) -> list[bool]:
    """Whether each grey thumbnail of a decoded MP4 changed from the one before (the first always): a pixel more than
    :data:`_CHANGED_LEVELS` apart, above the noise of its crf 14 encode. *thumbs* is read one at a time."""
    changed = _Changes()
    return [changed(thumb) for thumb in thumbs]


async def painted_from_mp4(
    mp4_path: str | Path,
    workdir: str | Path,
    *,
    fps: float,
    size: tuple[int, int],
    events: list[dict],
    capture: dict | None,
) -> list[tuple[float, str]]:
    """The keyframes of a finished take recorded before its keyframes were kept, read off its MP4 *mp4_path* (*fps*
    frames a second, *size* its frame in pixels): its frames compared on grey thumbnails, those :func:`select_painted`
    keeps written to *workdir* as lossless PNGs, ``(seconds, file name)``. Decoded on the CPU, a frame at a time."""
    from app.services.video_studio import encoder

    width = _THUMB_WIDTH
    height = max(2, round(width * size[1] / max(size[0], 1)))
    # Streamed: a 300 s take is 18,000 thumbnails, only the one before is held.
    moved = _Changes()
    changed = [moved(thumb) async for thumb in encoder.iter_gray_frames(str(mp4_path), width=width, height=height, fps=fps)]
    times = [index / fps for index in range(len(changed))]
    picked = select_painted(times, changed, spans=paced_spans(events), faults=fault_spans(capture))
    files = await encoder.extract_frames(str(mp4_path), picked, fps=fps, out_dir=str(workdir))
    return [(round(index / fps, 6), Path(files[index]).name) for index in picked if index in files]


#: What an ffmpeg log says, read as the one reason a failed frame extraction gives, first match wins.
_EXTRACTION_REASONS = (
    ("cannot allocate memory", "out of memory on this worker"),
    ("error while parsing expression", "expression too long"),
    ("invalid data found", "its MP4 cannot be decoded"),
    ("no such file", "its MP4 is gone"),
)


def extraction_failure(error: BaseException | str) -> str:
    """The one line a repair says when the frames of its MP4 could not be read: ``frame extraction failed:``
    and a known reason of the ffmpeg log *error*, else its last non-empty line cut to 160 characters - never the log."""
    text = str(error)
    lowered = text.lower()
    reason = next((said for needle, said in _EXTRACTION_REASONS if needle in lowered), None)
    if reason is None:
        lines = [line.strip() for line in text.splitlines() if line.strip()]
        reason = (lines[-1] if lines else "ffmpeg failed")[:160]
    return f"Frame extraction failed: {reason}. The recording repaired is kept; record it again."


# ── What a repair says it did ─────────────────────────────────────────────────


def repaired(events: list[dict], faults: Sequence[tuple[float, float]] = ()) -> list[dict]:
    """The stretches a repair drew again (*events* once paced again: each scroll and transition ``paced``) and those
    whose off-scale frames it dropped (*faults*, the MP4 route: the frame before holds), in time order."""
    found = [
        {"t": [round(s.start, 3), round(s.end, 3)], "what": "drawn again at its rate", "painted": round(s.painted, 1)}
        for s in paced_spans(events)
    ]
    found += [{"t": [round(a, 3), round(b, 3)], "what": "off-scale frames dropped, the frame before holds"} for a, b in faults]
    return sorted(found, key=lambda entry: entry["t"][0])


def repair_origin(route: str, source_capture: dict | None) -> str:
    """Where the frames a repair draws from first came from: its MP4 (*route* :data:`FROM_MP4`), or the keyframes
    of the take it repairs - themselves read off an MP4 when that take was a repair from one, at any depth of a chain
    of repairs (``capture.repair.origin``, else ``from``). A take recorded keeps its browser's keyframes."""
    if route == FROM_MP4:
        return FROM_MP4
    found = (source_capture or {}).get(REPAIRED_KEY) if isinstance(source_capture, dict) else None
    if isinstance(found, dict) and FROM_MP4 in (found.get("origin"), found.get("from")):
        return FROM_MP4
    return FROM_KEYFRAMES


def residual(
    route: str,
    *,
    recorded: dict | None,
    notes: Sequence[str],
    origin: str | None = None,
    events: Sequence[dict] | None = None,
    fps: float = 0.0,
) -> list[str]:
    """What a repair could not remove: from its MP4 (*route* :data:`FROM_MP4`), that its frames are a decode of an
    encode and that no canvas was copied alone (when the take had canvases, *recorded*); from keyframes an earlier
    repair read off an MP4 (*origin* :data:`FROM_MP4`, :func:`repair_origin`), the same; then what the take it made
    still says of its motion (*notes*, ``capture_rate.motion_notes``); then each transition of the take it made
    (*events* at its rate *fps*) the save check still finds ``synthesised_transition``: a redraw from keyframes draws
    no frame its page did not paint, so such a span is never repaired and is said as one to record again (#10892)."""
    out: list[str] = []
    if route != FROM_MP4 and origin == FROM_MP4:
        out.append(
            "its keyframes were read off an MP4 by an earlier repair (an encode at crf 14), not the frames its browser "
            "painted: record it again for an exact take"
        )
    if route == FROM_MP4:
        out.append("its frames are read off its MP4 (an encode at crf 14), not the frames its browser painted")
        had = recorded or {}
        if had.get("alone") or any(isinstance(s, dict) and s.get("canvas") for s in had.get("stepping") or []):
            out.append("its canvases were not copied alone into its MP4: a canvas transition that stepped steps still")
    out += [note for note in notes if note]
    if transitions := _synthesised_residual(events, fps):
        out.append(transitions)
    return out


def _synthesised_residual(events: Sequence[dict] | None, fps: float) -> str:
    """The transitions of a repaired take (*events*, *fps*) over ``scroll_pacing.SYNTHESISED_SHARE_MAX`` drawn from
    keyframes or held then cut, in the save check's words; empty when none."""
    from app.services.video_studio.scroll_pacing import (
        SYNTHESISED_SHARE_MAX,
        synthesised_transitions,
        synthesised_words,
    )

    made = synthesised_transitions(list(events or []), fps=float(fps or 0.0))
    if not made:
        return ""
    return (
        f"{synthesised_words(made[:6])}: its page painted too few frames there (over {round(SYNTHESISED_SHARE_MAX * 100)}% "
        "of the frames at the take's rate drawn from keyframes), which a repair cannot add; the save check refuses it "
        "as synthesised_transition: record it again"
    )


def repair_words(capture: dict | None) -> str:
    """What a repaired take says of its repair: the take it repaired, from what, the stretches repaired, the residual
    (to record again); empty for a take not repaired."""
    found = (capture or {}).get(REPAIRED_KEY) if isinstance(capture, dict) else None
    if not isinstance(found, dict):
        return ""
    exact = found.get("from") == FROM_KEYFRAMES and found.get("origin") != FROM_MP4
    source = (
        "its keyframes (exact)"
        if exact
        else "its keyframes, read off an MP4 by an earlier repair (approximate)"
        if found.get("from") == FROM_KEYFRAMES
        else "its MP4 (approximate)"
    )
    spans = found.get("spans") or []
    said = "; ".join(f"{s['t'][0]:.1f}-{s['t'][1]:.1f} s {s['what']}" for s in spans[:12]) or "no stretch needed it"
    more = f" (+{len(spans) - 12} more)" if len(spans) > 12 else ""
    left = found.get("residual") or []
    tail = ("Residual, record it again for these: " + "; ".join(left)) if left else "No residual: nothing to record again."
    return f"Repaired from recording `{found.get('of')}` from {source}: {said}{more}. {tail}."

