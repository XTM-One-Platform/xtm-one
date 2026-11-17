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

"""The words a canvas shows, read off its pixels: where a graph painted a node's label.

A graph drawn on a ``<canvas>`` paints each node's label under its circle and
keeps no element for it. ``node_locate`` finds the node by that label: it
screenshots the canvas at the page's pixel ratio and hands the picture here.
Tesseract reads it (:func:`read_words`, the ``tesseract`` binary the images
install beside ffmpeg; deterministic for one picture and one version) as
sparse text - labels scattered over a drawing, in no reading order - after
the picture is turned to grey, inverted when its ground is dark (light labels
on a dark graph read as dark ink on paper) and drawn twice as large under
pixel ratio 2 (a 10 px label is read at 20 px).

A label is the words painted next to one another on one line
(:func:`labels`): a word gap wider than :data:`LABEL_GAP` times the line's
height starts another label. :func:`find_title` finds the title a node step
resolved among them - exactly, then with its whitespace and case normalised -,
a label read as a whole before words inside a longer one.

A graph draws an edge's label as it draws no node's
(:func:`edge_label`): along its link - rotated to it, often in italic, the line
running on from both its ends. Such a label may hold a node's title ("Paris"
on the road to Paris) and is never taken for the node's.
"""

from __future__ import annotations

import asyncio
import io
import logging
import math
import os
import shutil
from dataclasses import dataclass
from typing import Any

from app.core.utils import format_exception_message

logger = logging.getLogger(__name__)

#: The OCR engine's binary.
TESSERACT = "tesseract"
#: Seconds one picture may take to read before the read is given up (a canvas reads in about 0.15 s).
OCR_SECONDS = 10.0
#: A picture shot under this pixel ratio is read at twice its size: Tesseract reads text 20 px tall and more best.
UPSCALE_BELOW_RATIO = 2.0
#: A word gap wider than this share of its line's height starts another label (a space is about a third of it).
LABEL_GAP = 1.2
#: Two words share a line when their vertical centres lie within this share of the taller one's height.
SAME_LINE = 0.5
#: A label whose baseline rises or falls more than this over its length is drawn rotated (about 3 degrees).
ROTATED_SLOPE = 0.05
#: A label whose upright strokes lean this much or more (x per y) is drawn in italic: an italic face leans 0.16 to
#: 0.25, read about a quarter short at 13 to 26 px; an upright face reads 0.
ITALIC_LEAN = 0.12
#: An edge stands upright when it runs within this of vertical (x over y: about 18 degrees), a diagonal's left out.
_UPRIGHT_EDGE = 3.0
#: The columns of a label its baseline is read in groups of, in label heights: the lowest ink of each sits on it.
_BASELINE_GROUP = 0.4
#: The fewest upright edge pixels a lean is read from.
_FEWEST_EDGES = 15
#: Beside a label, in label heights: the gap its halo leaves, then the length a line must run on for on both sides.
_HALO = 0.7
_RUN_ON = 1.0
#: A line beside a label is at most this thick, and wanders at most this much across its run (label heights).
_LINE_THICK = 0.45
_LINE_WANDER = 0.15
#: The share of a side's columns a line crosses, for the label to be drawn along it.
_LINE_COLUMNS = 0.75


@dataclass(frozen=True)
class Word:
    """A word read on a picture: its text and its box (left, top, width, height) in the picture's pixels."""

    text: str
    left: float
    top: float
    width: float
    height: float
    confidence: float

    @property
    def right(self) -> float:
        return self.left + self.width

    @property
    def middle(self) -> float:
        return self.top + self.height / 2


@dataclass(frozen=True)
class Found:
    """The words a title was read as: their box in the picture's pixels, how it matched, and how many labels did."""

    box: tuple[float, float, float, float]
    text: str
    #: ``exact`` or ``normalised`` (whitespace and case).
    how: str
    #: Whether the words are a whole label, not words inside a longer one.
    whole: bool
    #: How many places of the picture read as the title (one is acted on: the surest).
    count: int = 1


def available() -> bool:
    """Whether the OCR engine is installed on this worker."""
    return shutil.which(TESSERACT) is not None


def _prepared(picture: bytes, ratio: float) -> tuple[bytes, float]:
    """The picture as Tesseract reads it best - grey, dark ink on a light ground, 20 px text - and its scale."""
    import numpy as np
    from PIL import Image, ImageOps

    with Image.open(io.BytesIO(picture)) as image:
        grey = image.convert("L")
    if float(np.median(np.asarray(grey))) < 128:
        grey = ImageOps.invert(grey)
    factor = 2.0 if ratio < UPSCALE_BELOW_RATIO else 1.0
    if factor != 1.0:
        grey = grey.resize((round(grey.width * factor), round(grey.height * factor)), Image.Resampling.LANCZOS)
    out = io.BytesIO()
    grey.save(out, "PNG")
    return out.getvalue(), factor


def parse_tsv(tsv: str, factor: float = 1.0) -> list[Word]:
    """The words of Tesseract's ``tsv`` answer, their boxes back in the pixels of the picture shot (over *factor*)."""
    words: list[Word] = []
    for line in tsv.splitlines()[1:]:
        parts = line.split("\t")
        if len(parts) != 12 or parts[0] != "5" or not parts[11].strip():
            continue
        try:
            left, top, width, height = (float(value) / factor for value in parts[6:10])
            confidence = float(parts[10])
        except ValueError:
            continue
        if width > 0 and height > 0:
            words.append(Word(parts[11].strip(), left, top, width, height, confidence))
    return words


async def read_words(picture: bytes, ratio: float) -> list[Word] | None:
    """The words *picture* (shot at pixel *ratio*) shows, boxes in its pixels; ``None`` when no OCR engine could read
    it (none installed, or it failed)."""
    binary = shutil.which(TESSERACT)
    if binary is None:
        return None
    try:
        prepared, factor = await asyncio.to_thread(_prepared, picture, ratio)
        process = await asyncio.create_subprocess_exec(
            binary,
            "stdin",
            "stdout",
            "--psm",
            "11",
            "-l",
            "eng",
            "tsv",
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            # One thread: the read is deterministic and leaves the recording's screencast its CPU.
            env={**os.environ, "OMP_THREAD_LIMIT": "1"},
        )
    except (OSError, ValueError) as exc:
        logger.info("Canvas not read by OCR: %s", format_exception_message(exc))
        return None
    try:
        async with asyncio.timeout(OCR_SECONDS):
            stdout, stderr = await process.communicate(prepared)
    except asyncio.CancelledError:
        process.kill()
        await process.wait()
        raise
    except TimeoutError:
        process.kill()
        await process.wait()
        logger.info("Canvas not read by OCR within %g s", OCR_SECONDS)
        return None
    if process.returncode != 0:
        logger.info("Canvas not read by OCR: %s", stderr.decode("utf-8", "replace").strip()[-300:])
        return None
    return parse_tsv(stdout.decode("utf-8", "replace"), factor)


def labels(words: list[Word]) -> list[list[Word]]:
    """The labels *words* make: words on one line (:data:`SAME_LINE`), left to right, a gap wider than
    :data:`LABEL_GAP` times the line's height starting another label."""
    lines: list[list[Word]] = []
    for word in sorted(words, key=lambda w: (w.middle, w.left)):
        for line in lines:
            tallest = max(word.height, max(w.height for w in line))
            if abs(word.middle - sum(w.middle for w in line) / len(line)) <= SAME_LINE * tallest:
                line.append(word)
                break
        else:
            lines.append([word])
    out: list[list[Word]] = []
    for line in lines:
        line.sort(key=lambda w: w.left)
        height = sorted(w.height for w in line)[len(line) // 2]
        run = [line[0]]
        for word in line[1:]:
            if word.left - run[-1].right > LABEL_GAP * height:
                out.append(run)
                run = [word]
            else:
                run.append(word)
        out.append(run)
    return out


def _normalised(text: str) -> str:
    return " ".join(text.split()).casefold()


#: Why :func:`edge_label` reads a label as an edge's when a thin line runs on from both its ends.
ALONG_A_LINE = "along a line"


def box_of(words: list[Word]) -> tuple[float, float, float, float]:
    """The box (left, top, width, height) *words* cover, in the picture's pixels."""
    left, top = min(w.left for w in words), min(w.top for w in words)
    return left, top, max(w.right for w in words) - left, max(w.top + w.height for w in words) - top


def find_title(words: list[Word], title: str, *, whole_only: bool = False) -> Found | None:
    """Where *title* reads among *words*: a whole label exactly, then normalised (whitespace and case), then - unless
    *whole_only* - words inside a longer label the same two ways. Of several places, the surest is kept."""
    wanted = title.strip()
    if not wanted:
        return None
    runs = labels(words)
    for whole in (True, False) if not whole_only else (True,):
        for how, same in (
            ("exact", lambda a, b: a == b),
            ("normalised", lambda a, b: _normalised(a) == _normalised(b)),
        ):
            hits: list[list[Word]] = []
            for run in runs:
                spans = (
                    [(0, len(run))] if whole else [(i, j) for i in range(len(run)) for j in range(i + 1, len(run) + 1)]
                )
                for i, j in spans:
                    if (not whole and (i, j) == (0, len(run))) or not same(" ".join(w.text for w in run[i:j]), wanted):
                        continue
                    hits.append(run[i:j])
            if hits:
                best = max(hits, key=lambda span: (min(w.confidence for w in span), -span[0].top, -span[0].left))
                return Found(box_of(best), " ".join(w.text for w in best), how, whole, len(hits))
    return None


def edge_label(grey: Any, box: tuple[float, float, float, float]) -> str | None:
    """Why the label read in *box* (pixels of the grey picture *grey*, a 2-D ``uint8`` array) is drawn as a graph
    draws an edge's label: ``rotated`` (its baseline slopes more than :data:`ROTATED_SLOPE`), ``italic`` (its
    strokes lean :data:`ITALIC_LEAN` or more) or ``along a line`` (a thin straight line runs on from both its ends,
    past the halo a graph clears around it); ``None`` for an upright label standing free - a node's."""
    import numpy as np

    rows, cols = grey.shape
    left, top, width, height = box
    x0, y0 = max(0, int(left)), max(0, int(top))
    x1, y1 = min(cols, math.ceil(left + width)), min(rows, math.ceil(top + height))
    if x1 - x0 < 4 or y1 - y0 < 4:
        return None
    h = float(y1 - y0)
    margin = max(2, round(0.3 * h))
    xa, ya, xb, yb = max(0, x0 - margin), max(0, y0 - margin), min(cols, x1 + margin), min(rows, y1 + margin)
    around = np.ones((yb - ya, xb - xa), dtype=bool)
    around[y0 - ya : y1 - ya, x0 - xa : x1 - xa] = False
    if not around.any():
        return None
    ground = float(np.median(grey[ya:yb, xa:xb][around]))
    inside = np.abs(grey[y0:y1, x0:x1].astype(np.float32) - ground)
    contrast = float(np.percentile(inside, 95))
    if contrast < 24:
        return None
    slope = _baseline_slope(inside > 0.5 * contrast)
    if slope is not None and abs(slope) > ROTATED_SLOPE:
        return "rotated"
    lean = _lean(inside)
    if lean is not None and abs(lean) >= ITALIC_LEAN:
        return "italic"
    if _along_a_line(grey, (x0, y0, x1, y1), ground, contrast):
        return ALONG_A_LINE
    return None


def _baseline_slope(ink: Any) -> float | None:
    """The slope (y per x) of the line *ink*'s letters sit on: the lowest ink of each group of
    :data:`_BASELINE_GROUP` label heights of columns (a letter's foot, where a column alone may end inside a
    letter), fitted robustly (Theil-Sen: a descender's group left out); ``None`` under four groups."""
    import numpy as np

    rows, cols = ink.shape
    held = ink.any(axis=0)
    bottoms = np.where(held, rows - 1 - np.argmax(ink[::-1, :], axis=0), -1)
    width = max(3, round(_BASELINE_GROUP * rows))
    xs: list[float] = []
    feet: list[float] = []
    for start in range(0, cols, width):
        group = bottoms[start : start + width]
        if (group >= 0).any():
            at = int(np.argmax(group))
            xs.append(float(start + at))
            feet.append(float(group[at]))
    if len(xs) < 4:
        return None
    x, y = np.asarray(xs), np.asarray(feet)
    dx = x[None, :] - x[:, None]
    pairs = dx > 0
    return float(np.median((y[None, :] - y[:, None])[pairs] / dx[pairs]))


def _lean(ink: Any) -> float | None:
    """How far the strokes of *ink* (a label's contrast against its ground) lean, x per y, the top to the right
    positive: the median, weighted by strength, of the edges within :data:`_UPRIGHT_EDGE` of vertical (an upright
    face's stand straight, round letters balancing); ``None`` when too few edges are read."""
    import numpy as np
    from scipy import ndimage

    gx = ndimage.sobel(ink, axis=1)
    gy = ndimage.sobel(ink, axis=0)
    strength = np.hypot(gx, gy)
    if not strength.any():
        return None
    upright = (strength > 0.25 * float(strength.max())) & (np.abs(gx) > _UPRIGHT_EDGE * np.abs(gy))
    if int(np.count_nonzero(upright)) < _FEWEST_EDGES:
        return None
    leans = gy[upright] / gx[upright]
    weights = strength[upright]
    order = np.argsort(leans)
    cumulative = np.cumsum(weights[order])
    return float(leans[order][int(np.searchsorted(cumulative, cumulative[-1] / 2))])


def _along_a_line(grey: Any, box: tuple[int, int, int, int], ground: float, contrast: float) -> bool:
    """Whether a thin straight line runs on from both ends of the label in *box* (``x0, y0, x1, y1``), at its height,
    past :data:`_HALO` label heights of clear ground and for :data:`_RUN_ON` more: the link an edge's label is drawn on.
    A side the picture cuts reads no line."""
    import numpy as np

    rows, cols = grey.shape
    x0, y0, x1, y1 = box
    h = y1 - y0
    gap, run = max(2, round(_HALO * h)), max(4, round(_RUN_ON * h))
    band0, band1 = max(0, round(y0 - 0.3 * h)), min(rows, round(y1 + 0.3 * h))
    centres: list[float] = []
    for start, end in ((x0 - gap - run, x0 - gap), (x1 + gap, x1 + gap + run)):
        if start < 0 or end > cols:
            return False
        strip = np.abs(grey[band0:band1, start:end].astype(np.float32) - ground) > max(12.0, 0.15 * contrast)
        held: list[float] = []
        for column in strip.T:
            ys = np.flatnonzero(column)
            if ys.size and ys[-1] - ys[0] + 1 <= max(3.0, _LINE_THICK * h):
                held.append(band0 + (ys[0] + ys[-1]) / 2)
        if len(held) < _LINE_COLUMNS * strip.shape[1] or np.ptp(held) > max(1.5, 2 * _LINE_WANDER * h):
            return False
        centre = float(np.median(held))
        if not y0 - 0.15 * h <= centre <= y1 + 0.15 * h:
            return False
        centres.append(centre)
    return abs(centres[0] - centres[1]) <= max(2.0, 0.35 * h)
