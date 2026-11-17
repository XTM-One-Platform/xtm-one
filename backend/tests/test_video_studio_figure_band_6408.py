"""A figure scene is centred in the band between the top safe margin and its caption pill.

The brand film's loop figure, drawn flat with a still camera, was centred in the room above the subtitles' reserve -
two lines, a gap - so its bottom sat at 71% of the frame and the band between it and the one-line pill under it was
empty: it read as off-centre. What is proven, in the geometry the figure page draws with (``figure-build.ts``'s
``framing``, mirrored by ``figure_fit.framing``):

* a 1700x650 figure on a 1920x1080 scene with pill captions is centred within 1% of the band between 4% of the height
  and the caption pill's top, at its full width;
* captions at the top mirror it, and a scene without subtitles stays centred on the frame;
* the save line says where the figure spans.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.services.video_studio import figure_fit, figures
from app.services.video_studio.compositor_page import (
    CAPTION_EDGE_OFFSET,
    CAPTION_FONT_PX,
    CAPTION_LINE_HEIGHT,
    CAPTION_PADDING_PX,
)
from app.services.video_studio.spec import parse_video_spec, validate_video_spec

FRAME = (1920, 1080)
FIGURE = (0.0, 0.0, 1700.0, 650.0)
FLOW = {"type": "process_flow", "steps": [{"title": "Collect"}, {"title": "Enrich"}, {"title": "Act"}]}


def _band(**captions) -> dict:
    scene = {"id": "loop", "type": "figure", "block_spec": FLOW, "camera": "still", "narration": "We collect, we enrich, we act."}
    spec = parse_video_spec(validate_video_spec({"title": "Brand film", "captions": captions, "scenes": [scene]}))
    return figures.caption_safe_area(spec, spec.scenes[0], FRAME[1])


def _pill_top() -> float:
    """The top of the tallest pill the page draws at the bottom: two lines, its padding, its offset from the edge."""
    box = 2 * CAPTION_FONT_PX * CAPTION_LINE_HEIGHT + CAPTION_PADDING_PX["pill"]
    return FRAME[1] - (FRAME[1] * CAPTION_EDGE_OFFSET["bottom"] + box)


def test_a_figure_is_centred_between_the_top_margin_and_the_caption_pill():
    safe = _band(style="pill")
    _x0, top, _x1, bottom = figure_fit.drawn_box(FIGURE, FRAME, safe, 1.0, None)
    band_top, band_bottom = 0.04 * FRAME[1], _pill_top()
    assert safe == {"top": pytest.approx(band_top, abs=0.1), "bottom": pytest.approx(FRAME[1] - band_bottom, abs=0.1)}
    middle = (top + bottom) / 2
    assert abs(middle - (band_top + band_bottom) / 2) <= 0.01 * FRAME[1]
    # At its full width: the frame's width less the page's margin, never shrunk to the band's height.
    assert (_x1 - _x0) == pytest.approx(FRAME[0] * (1 - 2 * figure_fit.FRAME_MARGIN))
    # The band under it is no longer empty: as much room under it as above it, to the pill.
    assert band_bottom - bottom == pytest.approx(top - band_top, abs=1.0)


def test_captions_at_the_top_mirror_the_band_and_a_silent_scene_is_centred_on_the_frame():
    above = _band(style="pill", position="top")
    assert above["bottom"] == pytest.approx(0.04 * FRAME[1]) and above["top"] > 150
    _x0, top, _x1, bottom = figure_fit.drawn_box(FIGURE, FRAME, above, 1.0, None)
    assert abs((top + bottom) / 2 - (above["top"] + FRAME[1] - above["bottom"]) / 2) <= 0.01 * FRAME[1]
    _x0, top, _x1, bottom = figure_fit.drawn_box(FIGURE, FRAME, figures.NO_SAFE_AREA, 1.0, None)
    assert (top + bottom) / 2 == pytest.approx(FRAME[1] / 2)


def test_the_save_line_says_where_the_figure_spans():
    safe = _band(style="pill")
    scene = SimpleNamespace(id="loop", build="sequence", camera="still", reveal="even", dwell=1.0, motion="drift", perspective="flat")
    described = {"steps": [{"labels": ["Whole"], "box": {"x": 0, "y": 0, "w": 1700, "h": 650}, "settle": 0.6}], "figure": {"x": 0, "y": 0, "w": 1700, "h": 650}}
    fit = figures.plan_fit(described, [0.35], scene=scene, kind="process_flow", length=8.0, safe=safe, viewport=FRAME)
    assert fit is not None
    _x0, top, _x1, bottom = figure_fit.drawn_box(FIGURE, FRAME, safe, 1.0, None)
    assert fit.spans == pytest.approx((top / FRAME[1], bottom / FRAME[1]))
    line = fit.line("loop")
    assert line.endswith(
        f"Its figure spans {top / FRAME[1]:.0%} to {bottom / FRAME[1]:.0%} of the frame's height, centred between 4% and "
        f"{_pill_top() / FRAME[1]:.0%}."
    )
