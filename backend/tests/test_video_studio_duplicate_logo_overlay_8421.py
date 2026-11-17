"""A brand image overlay under the watermark is a ``duplicate_logo`` at every quality.

A statement card with no backdrop played no footage, so ``render._duplicate_logos`` never read it: a template's logo
drawn by an image overlay (``{asset: logo}``) beside the corner watermark passed a draft render, forced or not, while
the save's audit said "brand mark drawn twice". The check now reads every scene drawing a brand image overlay, inside
the overlay's window, and names the overlay as the fix.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace

import numpy as np
from PIL import Image, ImageDraw

from app.services.video_studio import logos, quality, render, render_checks

#: A draft render of a 1080p design: 1280x720, 30 fps.
FRAME_W, FRAME_H, DESIGN_H, FPS = 1280, 720, 1080, 30.0


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


def _card(*, overlay: bool, watermark: bool) -> Image.Image:
    """A statement card as a draft draws it: a light page, the logo overlay at 30 % of the width top-left (``width:
    0.3``), the watermark bottom-right as the page draws it (44 px of the design height, 4 % margin, opacity .88)."""
    rng = np.random.default_rng(5)
    frame = Image.fromarray(np.clip(rng.normal(236, 6, (FRAME_H, FRAME_W, 3)), 0, 255).astype(np.uint8), "RGB")
    if overlay:
        logo = _wordmark()
        width = round(0.3 * FRAME_W)
        shown = logo.resize((width, round(width * logo.height / logo.width)), Image.Resampling.LANCZOS)
        frame.paste(shown, (round(0.06 * FRAME_W), round(0.08 * FRAME_H)), shown)
    if watermark:
        side = round(logos.WATERMARK_HEIGHT_PX * FRAME_H / DESIGN_H)
        margin = round(logos.WATERMARK_MARGIN_SHARE * FRAME_H)
        mark = _mark().resize((side, side), Image.Resampling.LANCZOS)
        mark.putalpha(mark.getchannel("A").point(lambda value: round(value * 0.88)))
        frame.paste(mark, (FRAME_W - margin - side, FRAME_H - margin - side), mark)
    return frame


def _document(*, overlay: bool) -> dict:
    """A planned document: one statement card with no backdrop, its logo not drawn by the card (a watermarked video),
    and, when *overlay*, an image overlay of the template's logo."""
    scene: dict = {"id": "s1", "type": "statement", "start": 0.0, "end": 6.0, "show_logo": False, "backdrop": None}
    if overlay:
        scene["overlays"] = [{"type": "image", "brand": "logo", "src": "overlay/logo", "t0": 0.5, "t1": 5.5, "w": 576.0}]
    return {"watermark": "bottom-right", "height": DESIGN_H, "duration": 6.0, "fps": FPS, "scenes": [scene]}


def _run(tmp_path: Path, monkeypatch, *, overlay: bool, watermark: bool) -> tuple[list, list[str], list[int]]:
    logo_path, mark_path = tmp_path / "logo.png", tmp_path / "mark.png"
    _wordmark().save(logo_path)
    _mark().save(mark_path)
    card = tmp_path / "card.png"
    _card(overlay=overlay, watermark=watermark).save(card)
    asked: list[int] = []

    async def extract_frames(path, numbers, *, fps, out_dir):
        asked.extend(numbers)
        return dict.fromkeys(numbers, str(card))

    monkeypatch.setattr(render.encoder, "extract_frames", extract_frames)
    media = SimpleNamespace(assets=SimpleNamespace(images={"logo": str(logo_path), "mark": str(mark_path)}))
    notes: list[str] = []
    found = asyncio.run(
        render._duplicate_logos(
            SimpleNamespace(scenes=[]),
            _document(overlay=overlay),
            media,
            str(tmp_path / "draft.mp4"),
            tmp_path,
            fps=FPS,
            span=None,
            scene_id=None,
            notes=notes,
        )
    )
    return found, notes, asked


def test_a_logo_overlay_on_a_statement_under_the_watermark_fails_with_duplicate_logo_at_draft(tmp_path, monkeypatch):
    found, _notes, asked = _run(tmp_path, monkeypatch, overlay=True, watermark=True)
    assert asked, "the statement card drawing a brand overlay must be read"
    assert len(found) == 1
    finding, proof = found[0]
    assert finding.kind == render_checks.DUPLICATE_LOGO and finding.severity == render_checks.FAIL
    assert finding.scene_id == "s1"
    assert logos.WATERMARK_ROLE in finding.data["roles"] and finding.data["overlay"] is True
    assert "image overlay of the template's logo" in (finding.fix or "")
    assert Path(proof).is_file()


def test_a_forced_render_keeps_the_finding(tmp_path, monkeypatch):
    """A forced render keeps its findings rather than failing on them: the check itself never depends on force."""
    found, _notes, _asked = _run(tmp_path, monkeypatch, overlay=True, watermark=True)
    checks = render_checks.Checks()
    checks.findings.extend(finding for finding, _proof in found)
    assert checks.failed


def test_the_overlay_alone_is_one_place(tmp_path, monkeypatch):
    found, _notes, _asked = _run(tmp_path, monkeypatch, overlay=True, watermark=False)
    assert found == []


def test_a_statement_card_with_no_brand_overlay_is_not_read():
    assert render._duplicate_logo_moments(_document(overlay=False), 0) == []


def test_the_overlay_window_is_sampled_inside_the_scene():
    moments = render._duplicate_logo_moments(_document(overlay=True), 0)
    assert 3.0 in moments
    assert all(0.0 <= at <= 6.0 for at in moments)


def test_the_overlay_is_named_only_inside_its_window():
    """A ``mark`` overlay shown at 4-5 s of a scene starting at 10 s: a sample outside that window is not the
    overlay's, a sample inside names the ``mark``, not the logo."""
    scene = {"id": "s2", "start": 10.0, "overlays": [{"type": "image", "brand": "mark", "t0": 4.0, "t1": 5.0}]}
    assert render._brand_overlay_on_screen(scene, 10.9) is None
    assert render._brand_overlay_on_screen(scene, 14.5) == "mark"
    places = [("logo", (0.1, 0.1, 0.3, 0.2), 0.9), (logos.WATERMARK_ROLE, (0.9, 0.9, 0.95, 0.95), 0.8)]
    outside = logos.duplicate_logo_finding(scene, places, at=10.9, watermarked=True, overlay=None)
    assert outside is not None and "overlay" not in outside.data and "watermark off" in (outside.fix or "")
    inside = logos.duplicate_logo_finding(scene, places, at=14.5, watermarked=True, overlay="mark")
    assert inside is not None and inside.data["overlay_asset"] == "mark"
    assert "image overlay of the template's mark" in (inside.fix or "")


def test_the_report_version_moved():
    assert quality.QUALITY_REPORT_VERSION >= 46
