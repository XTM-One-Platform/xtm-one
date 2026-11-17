"""#9590: a canvas transition composited from its copies keeps what the page holds still over the canvas (a toolbar,
a floating button the grid of ``over`` boxes did not catch) as the browser painted it on every composited frame, so the
chrome is never drawn at a second offset from the copy's box (``canvas_alone.static_layers``)."""

from __future__ import annotations

from collections import OrderedDict
from pathlib import Path

import numpy as np
from PIL import Image

from app.services.video_studio import canvas_alone, capture_rate

#: A 800x450 CSS px window at pixel ratio 2; the canvas stands at (100, 50), 500x300 CSS px. A toolbar the page draws
#: over the canvas's bottom and a floating button are not in the copies' ``over`` boxes.
RATIO = 2.0
CANVAS = (100.0, 50.0, 500.0, 300.0)
SURFACE = "100,50,500,300"
TOOLBAR = (240.0, 320.0, 140.0, 16.0)
BUTTON = (548.0, 80.0, 24.0, 16.0)
BLOCK = (120, 90)
#: Where the copies carry the toolbar and the button: 2 CSS px off the page's, a copy mis-registered on them by less
#: than the run check refuses (``canvas_alone.check``), as in the takes of the report.
SHIFT = 2


def _device(v: float) -> int:
    return round(v * RATIO)


def _left(t: float) -> float:
    return 40 + 300 * min(max((t - 1.0) / 1.5, 0.0), 1.0)


def _chrome(picture: np.ndarray, dx: float, dy: float, origin: tuple[float, float], scale: float) -> None:
    for (x, y, w, h), colour in ((TOOLBAR, (240, 200, 40)), (BUTTON, (40, 200, 240))):
        x0, y0 = round((x + dx - origin[0]) * scale), round((y + dy - origin[1]) * scale)
        picture[y0 : y0 + round(h * scale), x0 : x0 + round(w * scale)] = colour


def _painted(t: float) -> np.ndarray:
    picture = np.full((900, 1600, 3), 12, dtype=np.uint8)
    x, y, w, h = CANVAS
    picture[_device(y) : _device(y + h), _device(x) : _device(x + w)] = 30
    left, top = x + _left(t), y + 60
    picture[_device(top) : _device(top + BLOCK[1]), _device(left) : _device(left + BLOCK[0])] = (220, 230, 240)
    _chrome(picture, 0, 0, (0.0, 0.0), RATIO)
    return picture


def _copy(t: float) -> np.ndarray:
    picture = np.full((300, 500, 3), 30, dtype=np.uint8)
    left = round(_left(t))
    picture[60 : 60 + BLOCK[1], left : left + BLOCK[0]] = (220, 230, 240)
    _chrome(picture, SHIFT, SHIFT, CANVAS[:2], 1.0)
    return picture


def _frames(workdir: Path, times: list[float]) -> list[tuple[float, str]]:
    out = []
    for index, t in enumerate(times):
        name = f"f{index:05d}.png"
        Image.fromarray(_painted(t)).save(workdir / name)
        out.append((round(t, 4), name))
    return out


def _copies(workdir: Path, times: list[float]) -> list[canvas_alone.AloneFrame]:
    out = []
    for index, t in enumerate(times):
        name = f"c{index:06d}.png"
        Image.fromarray(_copy(t)).save(workdir / name)
        out.append(canvas_alone.AloneFrame(round(t, 4), SURFACE, "canvas#graph", CANVAS, (), name))
    return out


def _events() -> list[dict]:
    canvases = [["canvas#graph", list(CANVAS)]]
    return [
        {"step": 0, "action": "wait", "t0": 0.0, "ta": 0.0, "t1": 1.0, "ok": True, "canvases": canvases},
        {
            "step": 1,
            "action": "click",
            "label": "Layered layout",
            "t0": 1.0,
            "ta": 1.0,
            "t1": 2.7,
            "ok": True,
            "canvases": canvases,
        },
        {"step": 2, "action": "wait", "t0": 2.7, "ta": 2.7, "t1": 3.2, "ok": True, "canvases": canvases},
    ]


PAINTED = [0.3, 0.6, 0.9, *(1.0 + k / 8 for k in range(12)), 2.6, 2.8]
COPIED = [0.35, *(1.0 + k / 30 for k in range(46))]


def _read(workdir: Path, name: str) -> np.ndarray:
    with Image.open(workdir / name) as image:
        return np.asarray(image.convert("RGB"), dtype=np.int16)


def _box(picture: np.ndarray, box: tuple[float, float, float, float]) -> np.ndarray:
    x, y, w, h = box
    return picture[_device(y) : _device(y + h), _device(x) : _device(x + w)]


def test_chrome_held_still_over_a_composited_canvas_is_drawn_at_one_place_on_every_frame(tmp_path):
    frames = _frames(tmp_path, PAINTED)
    done = canvas_alone.composite(
        frames, _copies(tmp_path, COPIED), _events(), fps=30, pixel_ratio=RATIO, workdir=str(tmp_path)
    )

    [run] = done.runs
    assert run["composited"] is True, run
    assert 0 < run["static"] <= canvas_alone.STATIC_MOST
    composited = [name for _t, name in done.frames if name.startswith(capture_rate.COMPOSITED_PREFIX)]
    assert len(composited) == 46
    # On every frame of the take (composited ones are JPEG: read on the mean of each strip) the toolbar and the button
    # stand where the page painted them, and the strip the copy carries them at (SHIFT px lower and to the right)
    # shows the canvas: no frame draws the chrome at a second offset.
    for _t, name in done.frames:
        picture = _read(tmp_path, name)
        for (x, y, w, h), colour in ((TOOLBAR, (240, 200, 40)), (BUTTON, (40, 200, 240))):
            inside = (x + 1, y + 1, w - SHIFT - 2, h - SHIFT - 2)
            below = (x + SHIFT + 0.5, y + h + 0.5, w - 1, SHIFT - 1)
            right = (x + w + 0.5, y + SHIFT + 0.5, SHIFT - 1, h - 1)
            assert np.abs(_box(picture, inside).mean(axis=(0, 1)) - colour).max() < 12, (name, "inside")
            for strip in (below, right):
                assert np.abs(_box(picture, strip).mean(axis=(0, 1)) - 30).max() < 12, (name, strip)
    # The canvas still moves from its copies where nothing is held over it.
    name = next(name for t, name in done.frames if abs(t - 1.7) < 1e-3)
    picture = _read(tmp_path, name)
    top = _device(CANVAS[1] + 60 + BLOCK[1] / 2)
    assert np.abs(picture[top, _device(CANVAS[0] + _left(1.7) + BLOCK[0] - 5)] - (220, 230, 240)).max() < 16


def test_a_moving_canvas_never_reads_as_a_static_layer(tmp_path):
    frames = _frames(tmp_path, PAINTED)
    copies = _copies(tmp_path, COPIED)
    [run] = canvas_alone.alone_runs(copies, _events())
    found = canvas_alone.static_layers(run, frames, workdir=str(tmp_path), ratio=RATIO, scaled=OrderedDict())
    assert found is not None
    mask, share = found
    # The block's path across the canvas is never held still; the toolbar's edge the copy draws elsewhere is.
    top = _device(60 + BLOCK[1] / 2)
    assert not mask[top, _device(_left(1.5) + BLOCK[0] / 2)]
    x, y, _w, h = TOOLBAR
    assert mask[_device(y - CANVAS[1] + h / 2), _device(x - CANVAS[0]) + 1]
    # Far from any chrome, the canvas's own background is never masked: the copy draws it as painted.
    assert not mask[_device(250), _device(20)]
    assert share < canvas_alone.STATIC_MOST


def test_a_run_with_one_frame_painted_about_it_tells_no_static_layer(tmp_path):
    frames = _frames(tmp_path, [0.3])
    copies = _copies(tmp_path, COPIED)
    [run] = canvas_alone.alone_runs(copies, _events())
    assert canvas_alone.static_layers(run, frames, workdir=str(tmp_path), ratio=RATIO, scaled=OrderedDict()) is None
