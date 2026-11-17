"""The corner watermark at its drawn size is one of the ``duplicate_logo`` places.

The page draws ``#watermark`` 44 px high on the design canvas (``compositor_page``), about 2 % of the frame's width,
under the smallest size the whole-frame search (``logos.brand_marks_on``) reaches. A picture of the wordmark filling
most of the frame, with that watermark drawn bottom-right, must give ``duplicate_logo`` with the fix
``watermark: false``; the same picture with the watermark off must not.
"""

from __future__ import annotations

import numpy as np
from PIL import Image, ImageDraw

from app.services.video_studio import logos, render_checks

#: A 720p draft of a 1080p design, as a scene preview draws it.
FRAME_W, FRAME_H, DESIGN_H = 1280, 720, 1080


def _mark() -> Image.Image:
    """The brand's bare mark (RGBA): a two-colour ring, transparent around it."""
    mark = Image.new("RGBA", (120, 120), (0, 0, 0, 0))
    pen = ImageDraw.Draw(mark)
    pen.ellipse((4, 4, 116, 116), fill=(20, 60, 200, 255))
    pen.ellipse((34, 34, 86, 86), fill=(240, 120, 30, 255))
    return mark


def _wordmark() -> Image.Image:
    """The brand's logo (RGBA): the mark, then a word drawn as bars of two colours."""
    logo = Image.new("RGBA", (600, 120), (0, 0, 0, 0))
    logo.paste(_mark(), (0, 0), _mark())
    pen = ImageDraw.Draw(logo)
    for index in range(7):
        x = 150 + index * 64
        pen.rectangle((x, 20 + (index % 3) * 8, x + 40, 100), fill=(20, 60, 200, 255) if index % 2 else (40, 40, 40, 255))
    return logo


def _frame(*, watermark: bool) -> Image.Image:
    """A picture scene: the wordmark at 86 % of the frame's width on a light textured page, the watermark drawn as the
    page draws it (44 px of the design height, 4 % margin, bottom-right, opacity .88)."""
    rng = np.random.default_rng(11)
    frame = Image.fromarray(np.clip(rng.normal(236, 6, (FRAME_H, FRAME_W, 3)), 0, 255).astype(np.uint8), "RGB")
    logo = _wordmark()
    width = round(0.86 * FRAME_W)
    shown = logo.resize((width, round(width * logo.height / logo.width)), Image.Resampling.LANCZOS)
    frame.paste(shown, ((FRAME_W - shown.width) // 2, (FRAME_H - shown.height) // 2), shown)
    if watermark:
        side = round(logos.WATERMARK_HEIGHT_PX * FRAME_H / DESIGN_H)
        margin = round(logos.WATERMARK_MARGIN_SHARE * FRAME_H)
        mark = _mark().resize((side, side), Image.Resampling.LANCZOS)
        alpha = mark.getchannel("A").point(lambda value: round(value * 0.88))
        mark.putalpha(alpha)
        frame.paste(mark, (FRAME_W - margin - side, FRAME_H - margin - side), mark)
    return frame


def _marks() -> dict:
    return {"logo": _wordmark(), "logo_mark": _mark()}


def _scene(watermark: bool | None) -> tuple[dict, dict]:
    scene: dict = {"id": "logo_watermark_on", "type": "image"}
    if watermark is False:
        scene["watermark"] = False
    return {"watermark": "bottom-right", "height": DESIGN_H, "scenes": [scene]}, scene


def test_the_watermark_is_drawn_under_the_whole_frame_search():
    side = logos.WATERMARK_HEIGHT_PX * FRAME_H / DESIGN_H
    assert side / FRAME_W < logos.MARK_MIN_SHARE


def test_a_wordmark_filling_the_frame_is_matched():
    places = logos.brand_marks_on(_frame(watermark=False), {"logo": _wordmark()})
    assert len(places) == 1
    x0, _y0, x1, _y1 = places[0][1]
    assert x1 - x0 > 0.7
    # With the bare mark searched too, the mark inside the wordmark is the wordmark: still one place.
    assert len(logos.brand_marks_on(_frame(watermark=False), _marks())) == 1


def test_the_watermark_is_found_where_and_at_the_size_the_page_draws_it():
    found = logos.watermark_on(_frame(watermark=True), _mark(), "bottom-right", design_height=DESIGN_H)
    assert found is not None and found[0] == logos.WATERMARK_ROLE
    x0, y0, x1, _y1 = found[1]
    assert x0 > 0.9 and y0 > 0.85 and (x1 - x0) * FRAME_W < 40
    assert logos.watermark_on(_frame(watermark=False), _mark(), "bottom-right", design_height=DESIGN_H) is None
    # The other corner holds nothing.
    assert logos.watermark_on(_frame(watermark=True), _mark(), "top-left", design_height=DESIGN_H) is None


def test_the_logo_in_the_picture_under_the_watermark_is_a_duplicate_logo_naming_watermark_false():
    document, scene = _scene(None)
    assert logos.scene_watermarked(document, scene)
    frame = _frame(watermark=True)
    places = logos.with_watermark(
        logos.brand_marks_on(frame, _marks()), logos.watermark_on(frame, _mark(), "bottom-right", design_height=DESIGN_H)
    )
    finding = logos.duplicate_logo_finding(scene, places, at=2.0, watermarked=True)
    assert finding is not None and finding.kind == render_checks.DUPLICATE_LOGO
    assert finding.severity == render_checks.FAIL
    assert logos.WATERMARK_ROLE in finding.data["roles"] and len(finding.data["boxes"]) == 2
    assert "watermark: false" in (finding.fix or "")


def test_watermark_false_on_the_scene_stays_green():
    document, scene = _scene(False)
    assert not logos.scene_watermarked(document, scene)
    places = logos.brand_marks_on(_frame(watermark=False), _marks())
    assert logos.duplicate_logo_finding(scene, places, at=2.0, watermarked=False) is None


def test_the_watermark_alone_is_one_place():
    watermark = ("watermark", (0.95, 0.9, 0.97, 0.94), 0.95)
    assert logos.with_watermark([], watermark) == [watermark]
    # A whole-frame place over the watermark is the watermark.
    assert logos.with_watermark([("logo_mark", (0.951, 0.9, 0.97, 0.94), 0.9)], watermark) == [watermark]
    assert logos.with_watermark([("logo", (0.1, 0.4, 0.9, 0.6), 0.97)], None) == [("logo", (0.1, 0.4, 0.9, 0.6), 0.97)]
