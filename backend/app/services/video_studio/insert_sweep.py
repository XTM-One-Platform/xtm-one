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

"""A screen insert looked at whole, in one picture per scene (dev:video-render-wide-insert-gate).

A render's insert gates read every frame its layer is drawn on (``insert_checks.coverage``); its report keeps the
region of each insert at 100 % on three frames only. :func:`sheets` lays out the region of every insert of a scene
at a fixed step through its span - each tile the MP4's own pixels at 100 %, never scaled, its time, its frame and the
gates that frame fails on its own under it, a failing tile framed in red - as one contact sheet per scene, with the
table of what each tile measured: what ``get_video_insert_sweep`` returns, so an agent looks at a whole insert in one
picture instead of one ``save_video_frame`` call per second.

Pure drawing over frames handed in (:data:`FrameReader`): the tool reads them off the render's MP4
(``encoder.extract_frames``); the tests hand synthetic ones.
"""

from __future__ import annotations

import math
from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from PIL import Image, ImageDraw

from app.services.video_studio.render_checks import clock

#: The step between two tiles when none is asked (seconds).
DEFAULT_STEP_SECONDS = 1.0
#: The shortest step asked (seconds): finer than this, a sweep is a video, not a sheet.
MIN_STEP_SECONDS = 0.1
#: The tiles one insert's sheet holds at most: past it, the step is widened to fit (said).
MAX_TILES = 60
#: Tiles across a sheet.
COLUMNS = 4
#: The label strip under each tile, its margin and the red frame of a failing tile (pixels).
LABEL_PX, GAP_PX, FAIL_PX = 22, 6, 4
BACKGROUND, LABEL_COLOUR, FAIL_COLOUR = (24, 24, 28), (235, 235, 235), (230, 40, 40)
#: The frame of a tile whose gates its insert passes under their tolerance (``coverage.under_tolerance``,
#: ``insert_checks.reconcile_coverage``): an advisory, never the red of a finding.
ADVISORY_COLOUR = (240, 170, 40)

#: The name a tile's label gives a frame-level gate: the finding it feeds, so the red tile names what the
#: render's findings name; a gate absent here keeps its own name.
GATE_LABELS = {
    "matte_halo": "insert_matte_halo",
    "matte_lag": "insert_matte_lag",
    "occluded": "insert_occluded",
}
#: The render findings a tile at their frame of their scene is framed in red with, whatever its insert's gates.
SCENE_FINDINGS = ("duplicate_logo",)


def gate_label(gate: str) -> str:
    """What a failing tile's label calls *gate* (:data:`GATE_LABELS`)."""
    return GATE_LABELS.get(gate, gate)


#: ``(output frames) -> {frame: picture file}`` of the video swept, at its own size.
FrameReader = Callable[[list[int]], Awaitable[dict[int, str]]]


@dataclass(frozen=True)
class Tile:
    """One moment of an insert in its sheet: its time in the MP4, its frame and the gates that frame fails on its own."""

    at: float
    frame: int
    gates: tuple[str, ...] = ()
    #: Whether every gate of the tile is one its insert passes under its tolerance: no finding of the render, framed amber.
    advisory: bool = False

    def row(self) -> dict:
        out = {"at": round(self.at, 3), "frame": self.frame, "gates": list(self.gates)}
        if self.gates:
            out["verdict"] = "advisory" if self.advisory else "finding"
        return out


@dataclass
class InsertSweep:
    """One insert swept: its key, label, region box (0-1 of the frame), its tiles and its step."""

    key: str
    label: str
    box: tuple[float, float, float, float]
    step: float
    tiles: list[Tile] = field(default_factory=list)
    widened: bool = False
    coverage: dict | None = None

    def as_dict(self) -> dict:
        out = {
            "insert": self.key,
            "label": self.label,
            "box": [round(v, 5) for v in self.box],
            "step_seconds": round(self.step, 3),
            "tiles": [tile.row() for tile in self.tiles],
        }
        if self.widened:
            out["step_widened"] = True
        if self.coverage is not None:
            out["coverage"] = {k: v for k, v in self.coverage.items() if k != "over_frames"}
        return out


@dataclass
class SceneSheet:
    """The contact sheet of one scene's inserts: its picture file and what each tile measured."""

    scene_id: str
    path: str
    inserts: list[InsertSweep]

    def as_dict(self) -> dict:
        return {"scene_id": self.scene_id, "inserts": [sweep.as_dict() for sweep in self.inserts]}


def step_of(asked: Any) -> float:
    """The step asked, in seconds (:data:`DEFAULT_STEP_SECONDS` when none), never under :data:`MIN_STEP_SECONDS`."""
    try:
        value = float(asked) if asked not in (None, "") else DEFAULT_STEP_SECONDS
    except (TypeError, ValueError):
        value = DEFAULT_STEP_SECONDS
    if not math.isfinite(value):
        value = DEFAULT_STEP_SECONDS
    return max(MIN_STEP_SECONDS, value)


def _box_of(report: dict) -> tuple[float, float, float, float] | None:
    for crop in report.get("crops") or []:
        box = crop.get("box") if isinstance(crop, dict) else None
        if isinstance(box, list | tuple) and len(box) == 4:
            x0, y0, x1, y1 = (min(1.0, max(0.0, float(v))) for v in box)
            if x1 > x0 and y1 > y0:
                return x0, y0, x1, y1
    return None


def _span_of(report: dict) -> tuple[float, float] | None:
    """``(from, to)`` in the MP4 the insert is swept over: where its crops were cut (its scene on screen alone), else
    between its first and last crop."""
    within = report.get("cropped_within")
    if isinstance(within, list | tuple) and len(within) == 2 and float(within[1]) >= float(within[0]):
        return float(within[0]), float(within[1])
    times = [float(c["at"]) for c in report.get("crops") or [] if isinstance(c, dict) and c.get("at") is not None]
    return (min(times), max(times)) if times else None


def _failing(report: dict, rate: float) -> tuple[dict[int, tuple[str, ...]], int]:
    """``MP4 frame -> gates`` of the frames the insert's coverage names failing, and the shift from its output frames to
    the MP4's (a scene preview's MP4 starts at its scene)."""
    read = report.get("coverage") if isinstance(report.get("coverage"), dict) else {}
    shift = 0
    if read.get("worst_frame") is not None and read.get("worst_at") is not None:
        shift = int(read["worst_frame"]) - round(float(read["worst_at"]) * rate)
    over = read.get("over_frames") if isinstance(read.get("over_frames"), dict) else {}
    return {int(n) - shift: tuple(gates) for n, gates in over.items()}, shift


def plan(report: dict, *, rate: float, step: float) -> InsertSweep | None:
    """The tiles of one insert *report* (``insert_checks.reports``) at *step* seconds through its span, its worst frame
    always among them; ``None`` for an insert without a region box or a span."""
    box, span = _box_of(report), _span_of(report)
    if box is None or span is None:
        return None
    rate = max(float(rate), 1.0)
    first, last = span
    widened = False
    if (last - first) / step + 1 > MAX_TILES:
        step, widened = (last - first) / (MAX_TILES - 1), True
    failing, _shift = _failing(report, rate)
    frames: set[int] = set()
    count = math.floor((last - first) / step + 1e-9) + 1
    for index in range(count):
        frames.add(round((first + index * step) * rate))
    frames.add(round(last * rate))
    read = report.get("coverage") if isinstance(report.get("coverage"), dict) else {}
    if read.get("worst_at") is not None:
        frames.add(round(float(read["worst_at"]) * rate))
    # A gate its insert passes under its tolerance frames the tile as an advisory; a gate a finding holds, as a failure.
    under = set((read.get("under_tolerance") or {}) if isinstance(read.get("under_tolerance"), dict) else ())
    tiles = [
        Tile(n / rate, n, gates := failing.get(n, ()), bool(gates) and bool(under) and all(g in under for g in gates))
        for n in sorted(frames)
    ]
    return InsertSweep(
        str(report.get("key") or ""), str(report.get("label") or report.get("key") or ""), box, step, tiles, widened, read or None
    )


def _marked(sweep: InsertSweep, scene_id: str, findings: Iterable[dict], rate: float) -> InsertSweep:
    """*sweep* with each render finding of :data:`SCENE_FINDINGS` on its scene named on the tile nearest its time."""
    for finding in findings:
        if not isinstance(finding, dict) or finding.get("kind") not in SCENE_FINDINGS or str(finding.get("scene_id") or "") != scene_id:
            continue
        if not sweep.tiles or finding.get("at") is None:
            continue
        frame = round(float(finding["at"]) * max(float(rate), 1.0))
        nearest = min(range(len(sweep.tiles)), key=lambda i: abs(sweep.tiles[i].frame - frame))
        tile = sweep.tiles[nearest]
        if finding["kind"] not in tile.gates:
            sweep.tiles[nearest] = Tile(tile.at, tile.frame, (*tile.gates, str(finding["kind"])), False)
    return sweep


def _crop(path: str, box: tuple[float, float, float, float]) -> Image.Image:
    with Image.open(path) as picture:
        picture.load()
        width, height = picture.size
        x0, y0, x1, y1 = box
        left, top = math.floor(x0 * width), math.floor(y0 * height)
        right, bottom = max(left + 1, math.ceil(x1 * width)), max(top + 1, math.ceil(y1 * height))
        return picture.convert("RGB").crop((left, top, min(right, width), min(bottom, height)))


def draw(sweeps: list[InsertSweep], pictures: dict[int, str], out_path: str) -> str | None:
    """One sheet of *sweeps* (a scene's inserts, one block of rows each): every tile the region of its frame at 100 %
    (*pictures*: ``frame -> file``), its label under it, a failing one framed in red; ``None`` when no frame was read."""
    blocks: list[tuple[InsertSweep, list[tuple[Tile, Image.Image]]]] = []
    for sweep in sweeps:
        cut = [(tile, _crop(pictures[tile.frame], sweep.box)) for tile in sweep.tiles if tile.frame in pictures]
        if cut:
            blocks.append((sweep, cut))
    if not blocks:
        return None
    width = max(min(COLUMNS, len(cut)) * (max(i.width for _t, i in cut) + GAP_PX) + GAP_PX for _s, cut in blocks)
    heights = []
    for _sweep, cut in blocks:
        rows = math.ceil(len(cut) / COLUMNS)
        heights.append(LABEL_PX + rows * (max(i.height for _t, i in cut) + LABEL_PX + GAP_PX) + GAP_PX)
    sheet = Image.new("RGB", (width, sum(heights)), BACKGROUND)
    pen = ImageDraw.Draw(sheet)
    top = 0
    for (sweep, cut), height in zip(blocks, heights, strict=True):
        pen.text((GAP_PX, top + 4), f"{sweep.label} - every {sweep.step:g} s, 100% of the frame's pixels", fill=LABEL_COLOUR)
        tile_w, tile_h = max(i.width for _t, i in cut), max(i.height for _t, i in cut)
        for index, (tile, picture) in enumerate(cut):
            x = GAP_PX + (index % COLUMNS) * (tile_w + GAP_PX)
            y = top + LABEL_PX + (index // COLUMNS) * (tile_h + LABEL_PX + GAP_PX)
            sheet.paste(picture, (x, y))
            colour = ADVISORY_COLOUR if tile.advisory else FAIL_COLOUR
            if tile.gates:
                pen.rectangle((x, y, x + picture.width - 1, y + picture.height - 1), outline=colour, width=FAIL_PX)
            gates = ", ".join(gate_label(g) for g in tile.gates) + (" (advisory)" if tile.advisory else "")
            words = f"{clock(tile.at)}  f{tile.frame}" + (f"  {gates}" if tile.gates else "")
            pen.text((x, y + picture.height + 4), words, fill=colour if tile.gates else LABEL_COLOUR)
        top += height
    sheet.save(out_path, format="PNG")
    return out_path


async def sheets(
    reports: Iterable[dict],
    read: FrameReader,
    *,
    rate: float,
    step: float,
    out_dir: str,
    scene: str | None = None,
    findings: Iterable[dict] = (),
) -> tuple[list[SceneSheet], list[str]]:
    """The contact sheet of each scene's inserts in *reports* (its scene *scene* only, when given), the frames read in
    one call of *read*; and the inserts left out, each with why. A render finding of :data:`SCENE_FINDINGS` among
    *findings* (``render.checks``) frames the tile of its scene nearest its time in red, named."""
    by_scene: dict[str, list[InsertSweep]] = {}
    skipped: list[str] = []
    findings = list(findings)
    for report in reports:
        scene_id = str(report.get("scene_id") or "")
        if scene is not None and scene_id != scene:
            continue
        sweep = plan(report, rate=rate, step=step)
        if sweep is None:
            skipped.append(f"{report.get('label') or report.get('key')}: no region box kept for it in this render's checks")
            continue
        by_scene.setdefault(scene_id, []).append(_marked(sweep, scene_id, findings, rate))
    wanted = sorted({tile.frame for sweeps in by_scene.values() for sweep in sweeps for tile in sweep.tiles})
    pictures = await read(wanted) if wanted else {}
    out: list[SceneSheet] = []
    for scene_id, sweeps in by_scene.items():
        path = draw(sweeps, pictures, str(Path(out_dir) / f"sweep-{len(out):02d}.png"))
        if path is None:
            skipped.extend(f"{sweep.label}: no frame of it could be read off the MP4" for sweep in sweeps)
            continue
        out.append(SceneSheet(scene_id, path, sweeps))
    return out, skipped
