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

"""Brightness dips: the whole frame going dark for a moment outside any transition.

A recording's highlight once drew its dim over the whole stage (theme background, overlays, burnt-in subtitles
included): the finished video read 27 -> 12 -> 25 in mean luma over about a second, right after the scene's
crossfade in, and no check said it. This gate reads the finished MP4 only - the grey frames the frozen-picture watch
already decodes (``frame_freeze.check_frames``), one mean luma per frame - and says each dip: the frame's mean luma
falling under :data:`DIP_RATIO` of its value :data:`LOOKBACK_SECONDS` earlier and coming back within
:data:`RETURN_SECONDS`, outside every declared transition and the video's own fades, every cut between two scenes,
an author's ``spotlight`` overlay and the cards whose motion is their design.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from app.services.video_studio.render_checks import BRIGHTNESS_DIP, WARN, Finding, clock

#: A frame whose mean luma falls under this share of the reference (the frame LOOKBACK_SECONDS before) is dimmed:
#: the eye reads 30 % darker over half a second as the picture going dark, not as the content changing.
DIP_RATIO = 0.7
#: How far back the reference is: a fade-like dim of the whole frame is over well within it.
LOOKBACK_SECONDS = 0.5
#: The dip is over once the luma is back to this share of the reference ...
RECOVERED_RATIO = 0.9
#: ... within this long of the reference: a picture that stays darker is a new picture (a darker page), never a dip.
RETURN_SECONDS = 2.0
#: A reference darker than this (0-255 grey) is a dark picture already, where a dip is the encoder's noise.
MIN_REFERENCE_LUMA = 8.0
#: The scenes whose own motion may dim the frame by design (a card's reveal, a motion design): never a dip.
DESIGNED_TYPES = frozenset({"title", "outro", "statement", "motion"})
#: What to change for a dip: nothing in the spec asks for one.
DIP_REMEDY = (
    "something drawn over the whole frame darkens it outside any transition (a highlight's or an overlay's dim): "
    "preview the scene again, and report the render with this frame if its spec asks for no such dim"
)


@dataclass(frozen=True)
class Dip:
    """A dip read on the frames: the reference frame before it, the darkest frame, the frame it is back on."""

    reference: int
    darkest: int
    back: int
    before: float
    minimum: float
    after: float


def dips(lumas: Sequence[float], rate: float) -> list[Dip]:
    """Every dip of the mean *lumas* of a video read at *rate* frames a second (``nan``: a frame not read)."""
    lookback = max(1, round(LOOKBACK_SECONDS * rate))
    within = max(lookback + 1, round(RETURN_SECONDS * rate))
    found: list[Dip] = []
    i = lookback
    count = len(lumas)
    while i < count:
        reference = i - lookback
        before = lumas[reference]
        # A comparison with a frame not read (nan) is false: never a dip across an unjudged stretch.
        if not (before >= MIN_REFERENCE_LUMA and lumas[i] < DIP_RATIO * before):
            i += 1
            continue
        back = next(
            (k for k in range(i + 1, min(count, reference + within + 1)) if lumas[k] >= RECOVERED_RATIO * before),
            None,
        )
        # A frame not read anywhere from the reference to the return: the dip is not judged.
        if back is None or any(math.isnan(lumas[k]) for k in range(reference, back)):
            i += 1
            continue
        darkest = min(range(i, back), key=lambda k: lumas[k])
        found.append(Dip(reference, darkest, back, float(before), float(lumas[darkest]), float(lumas[back])))
        i = back + 1
    return found


def _excused(document: dict, start: float, end: float) -> bool:
    """Whether the span *start*-*end* of *document* (its own seconds) meets a transition, a cut or a spotlight."""
    from app.services.video_studio.frame_freeze import blended_windows

    if any(a < end and start < b for a, b in blended_windows(document)):
        return True
    for scene in document.get("scenes") or []:
        scene_start = float(scene.get("start") or 0.0)
        # Every declared transition in (a slide, a wipe, a zoom as much as a crossfade) is its own window.
        seconds = float((scene.get("transition_in") or {}).get("duration") or 0.0)
        if seconds > 0 and scene_start < end and start < scene_start + seconds:
            return True
        # A cut from one picture to another (and back) is the edit, never a dim.
        if scene_start and start < scene_start < end:
            return True
        for overlay in scene.get("overlays") or []:
            if isinstance(overlay, dict) and overlay.get("type") == "spotlight":
                a = scene_start + float(overlay.get("t0") or 0.0)
                b = scene_start + float(overlay.get("t1") or 0.0)
                if a < end and start < b:
                    return True
    return False


def findings(
    lumas: Sequence[float], rate: float, document: dict, *, offset: float = 0.0, shown: Any = None
) -> list[Finding]:
    """A ``brightness_dip`` finding for each dip of *lumas* (read at *rate*) that nothing in *document* explains,
    timed in the MP4 (*offset*: where it starts in *document*, a preview's), its scene the one its darkest frame shows
    (*shown*: ``frame_freeze.Shown``, the document's at *rate* by default), with the luma before, at its darkest and
    after, and the second of its darkest frame (``minimum_at``) for its proof."""
    from app.services.video_studio.frame_freeze import Shown

    if shown is None:
        shown = Shown.of(document, float(document.get("fps") or rate), rate, offset=offset)
    out: list[Finding] = []
    for dip in dips(lumas, rate):
        start, end = dip.reference / rate, dip.back / rate
        if _excused(document, start + offset, end + offset):
            continue
        scene = shown.scene(dip.darkest)
        if scene.get("type") in DESIGNED_TYPES:
            continue
        share = round(100 * dip.minimum / dip.before)
        out.append(
            Finding(
                BRIGHTNESS_DIP,
                start,
                end,
                scene.get("id"),
                f"the whole frame dims to {share}% of its brightness ({clock(start)} to {clock(end)}) in a "
                f"{scene.get('type') or 'scene'} outside any transition, mean luma {dip.before:.1f} -> "
                f"{dip.minimum:.1f} -> {dip.after:.1f}: {DIP_REMEDY}",
                WARN,
                {
                    "before": round(dip.before, 2),
                    "minimum": round(dip.minimum, 2),
                    "after": round(dip.after, 2),
                    "minimum_at": round(dip.darkest / rate, 3),
                    "scene_type": scene.get("type"),
                },
                code="brightness_dip",
                params={"share": str(share), "start": clock(start), "end": clock(end)},
            )
        )
    return out
