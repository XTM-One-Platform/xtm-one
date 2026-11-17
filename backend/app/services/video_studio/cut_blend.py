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

"""A frame at a cut drawn as the two scenes at once: the picture gate of every cut join (point 2 of issue #8721).

A cut joins two scenes with no frame of both: the scene before to its last frame, the scene coming in from its first
(``frame_owners``). A frame that is a mix of the two - a fraction of a frame at the join, a shutter of motion blur
reaching across it - is a double exposure the audience sees at once, yet no gate read it: ``double_images`` reads
recording scenes alone and never across a cut, and the frozen-picture watch decodes at most 30 frames a second, so
one frame of a 60 fps render is not even read.

This gate reads the encoded MP4 itself, at the render's own frame rate, on the frames around each cut join only -
``join - 2`` to ``join + 1``, ``join`` the first frame of the scene coming in - and judges the last frame before the
cut and the first after it, each against its two neighbours (``double_frames.read_blend``): a frame that reads as a
mix of them is a ``double_image`` finding with its frame, its second and the scene pair. A render and a scene preview
(``render_video_scene``) run it alike on the joins their MP4 plays; a crossfade, a fade or any transition that lasts is
never read here.

A clean pass is said as well as a finding (issue #8935): :func:`read_cuts` answers a :class:`CutCheck`, the joins it read
beside its findings, stored on the render's checks as ``checks.cuts`` and said in one ``Cuts:`` line of its facts, so a
render whose cuts are clean is told from one whose gate never ran.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import numpy as np

from app.services.video_studio.double_frames import read_blend
from app.services.video_studio.frame_owners import FrameOwners
from app.services.video_studio.render_checks import DOUBLE_IMAGE, FINDINGS, PASSED, SKIPPED, WARN, Finding, clock

#: The frames read around a cut: from two before its first frame to one after it.
BEFORE, AFTER = 2, 1
#: The mix of the two neighbours a frame at a cut must be to be a double exposure: under the first or past the last,
#: one picture reads alone (an encoder's ringing on the frame after a cut reads about 0.05).
BLEND_RANGE = (0.2, 0.8)
#: The share of the neighbours' difference a mix may leave unexplained (``double_frames.MAX_RESIDUAL``).
MAX_RESIDUAL = 0.25
#: What to change for a blended frame at a cut.
REMEDY = (
    "the frame at a cut mixes the two scenes: render again on this build, and report the render with this frame if a "
    "cut still draws one (the cut is placed on a whole frame and no shutter crosses it)"
)


@dataclass(frozen=True)
class Join:
    """A cut join on the encoded frames: the first frame of the scene coming in, its index, and the scene pair."""

    frame: int
    index: int
    before: str
    after: str


def all_cut_joins(owners: FrameOwners) -> list[Join]:
    """Every cut join of *owners*, wherever it lies: a join no transition that lasts covers."""
    out = []
    for index in range(1, len(owners.spans)):
        if owners.blended[index]:
            continue
        frame = owners.spans[index][0]
        if owners.spans[index - 1][1] <= frame:
            out.append(Join(frame, index, owners.ids[index - 1], owners.ids[index]))
    return out


def cut_joins(owners: FrameOwners, *, first: int = 0, end: int | None = None) -> list[Join]:
    """The cut joins of *owners* whose frames ``join - BEFORE`` to ``join + AFTER`` all lie in ``[first, end)``."""
    stop = owners.total if end is None else min(end, owners.total)
    return [join for join in all_cut_joins(owners) if join.frame - BEFORE >= first and join.frame + AFTER < stop]


@dataclass
class CutCheck:
    """What the cut gate read of an MP4 (:func:`read_cuts`): the joins it read, those it left and why, its findings."""

    fps: float
    joins: list[dict] = field(default_factory=list)
    skipped: list[dict] = field(default_factory=list)
    findings: list[Finding] = field(default_factory=list)

    def as_summary(self, *, read_on: str = "final MP4") -> dict:
        """``checks.cuts``: ``{status, fps, joins_read, joins: [{frame, seconds, scenes}], skipped, findings,
        read_on}``; *status* ``passed`` with every join clean or none to read, ``findings`` with a double image."""
        return {
            "status": FINDINGS if self.findings else PASSED,
            "fps": round(self.fps, 3),
            "joins_read": len(self.joins),
            "joins": self.joins[:40],
            "skipped": self.skipped[:40],
            "findings": len(self.findings),
            "read_on": read_on,
        }


def not_read(reason: str) -> dict:
    """``checks.cuts`` of a gate that could not run: *reason* said in its ``Cuts:`` line."""
    return {"status": SKIPPED, "reason": reason}


def _fps_said(fps: Any) -> str:
    return f"{fps:g}" if isinstance(fps, int | float) else str(fps)


def summary_line(cuts: dict) -> str | None:
    """The ``Cuts:`` fact of a render's answer (``render_checks.check_lines``); ``None`` with no cut check stored."""
    status = cuts.get("status")
    if not status:
        return None
    if status == SKIPPED:
        return f"Cuts: not read ({cuts.get('reason') or 'no reason recorded'})."
    read, skipped = int(cuts.get("joins_read") or 0), [s for s in cuts.get("skipped") or [] if isinstance(s, dict)]
    where = f" on the {cuts['read_on']}" if cuts.get("read_on") else ""
    if not read and not skipped:
        return f"Cuts: no cut join{where}."
    line = f"Cuts: {read} join{'s' if read != 1 else ''} read at {_fps_said(cuts.get('fps'))} fps{where}"
    found = int(cuts.get("findings") or 0)
    if read:
        line += (
            f", {found} frame{'s' if found != 1 else ''} of two scenes (listed below)" if found else ", no frame of two scenes"
        )
    if skipped:
        why = sorted({str(s.get("why") or "") for s in skipped} - {""})
        line += f"; {len(skipped)} not read" + (f" ({'; '.join(why)})" if why else "")
    return line + "."


def mixed(before: Any, frame: Any, after: Any) -> float | None:
    """How much of *after* the *frame* is a mix of *before* and *after* (grey arrays), ``None`` when it is no mix:
    one picture alone, or a picture of its own the two do not explain."""
    read = read_blend(before, frame, after)
    if read is None:
        return None
    low, high = BLEND_RANGE
    return read.blend if low < read.blend < high and read.residual < MAX_RESIDUAL else None


def judge(join: Join, frames: list[Any], fps: float, *, offset: float = 0.0) -> Finding | None:
    """The finding of *join* read on its *frames* (``join - BEFORE`` to ``join + AFTER``, grey), ``None`` when its
    frames are clean; *offset*: the second of the document the MP4 starts at (a scene preview's)."""
    if len(frames) < BEFORE + AFTER + 1:
        return None
    # The last frame before the cut, between the one before it and the first after; the first after, between the last
    # before and the one after it.
    reads = [
        (join.frame - 1, mixed(frames[0], frames[1], frames[2])),
        (join.frame, mixed(frames[1], frames[2], frames[3])),
    ]
    hits = [(frame, blend) for frame, blend in reads if blend is not None]
    if not hits:
        return None
    frame, blend = max(hits, key=lambda hit: min(hit[1], 1 - hit[1]))
    at = max(0.0, frame / fps - offset)
    start, end = at, at + 1 / fps
    return Finding(
        DOUBLE_IMAGE,
        round(start, 3),
        round(end, 3),
        join.after if frame >= join.frame else join.before,
        f"the frame at the cut from {join.before} to {join.after} draws two pictures at once "
        f"({clock(start)} to {clock(end)}), frame {frame} at {clock(at)} a {round(100 * blend)}% mix of the two "
        f"scenes: {REMEDY}",
        WARN,
        {
            "frames": 1,
            "frame": frame,
            "frame_at": round(at, 3),
            "blend": round(blend, 3),
            "join": "cut",
            "scenes": [join.before, join.after],
        },
        code="double_image",
        params={"frames": 1, "start": clock(start), "end": clock(end)},
    )


async def check_cuts(
    final_path: str, document: dict, *, size: tuple[int, int], offset: float = 0.0, seconds: float | None = None
) -> list[Finding]:
    """The ``double_image`` findings of the cut joins *final_path* plays (:func:`read_cuts`)."""
    return (await read_cuts(final_path, document, size=size, offset=offset, seconds=seconds)).findings


async def read_cuts(
    final_path: str, document: dict, *, size: tuple[int, int], offset: float = 0.0, seconds: float | None = None
) -> CutCheck:
    """The cut joins *final_path* plays, read at the document's frame rate on grey copies *size* pixels wide and high,
    with their ``double_image`` findings; *offset* and *seconds*: the part of *document* the MP4 plays (a preview's)."""
    from app.services.video_studio import encoder

    owners = FrameOwners.of(document)
    fps = owners.fps
    first = round(offset * fps)
    end = None if seconds is None else first + int(seconds * fps)
    check = CutCheck(fps)
    readable = cut_joins(owners, first=first, end=end)
    stop = owners.total if end is None else min(end, owners.total)
    for join in all_cut_joins(owners):
        if join in readable:
            continue
        if join.frame <= first or join.frame >= stop:
            # A join the MP4 does not play across (a preview starting on it, or of another part): neither read nor missed.
            continue
        check.skipped.append(
            {"frame": join.frame, "scenes": [join.before, join.after], "why": "too close to the start or end of the MP4"}
        )
    for join in readable:
        raw = await encoder.gray_frames(
            final_path,
            start=max(0.0, (join.frame - BEFORE) / fps - offset),
            seconds=(BEFORE + AFTER + 1) / fps,
            fps=round(fps),
            size=size,
            out_path=f"{final_path}.cut-{join.frame}.gray",
        )
        frames = [np.frombuffer(chunk, dtype=np.uint8).reshape(size[1], size[0]) for chunk in raw]
        scenes = [join.before, join.after]
        if len(frames) < BEFORE + AFTER + 1:
            check.skipped.append({"frame": join.frame, "scenes": scenes, "why": "fewer frames decoded than the join needs"})
            continue
        check.joins.append({"frame": join.frame, "seconds": round(max(0.0, join.frame / fps - offset), 3), "scenes": scenes})
        if (finding := judge(join, frames, fps, offset=offset)) is not None:
            check.findings.append(finding)
    return check
