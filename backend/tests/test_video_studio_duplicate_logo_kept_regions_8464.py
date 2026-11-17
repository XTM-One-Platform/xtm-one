"""A region an AI shot keeps from its start picture is not a brand mark unless the brand's mark is in it.

``generate_video_clip`` covers a start picture's unreadable lettering with ``keep_regions`` (drawn back from the
picture in every frame). ``duplicate_logo`` read those patches of third-party lettering as a second brand mark and
blocked the scene, the watermark off or not, and its fix ("generate the shot again") could never clear it: the new
take re-applies the same regions. A place over a kept region now counts only when the template's mark is matched
inside it at the frame's own resolution; a real second logo still blocks, and the finding names the file and the score
of each place.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace

import numpy as np
from PIL import Image, ImageDraw

from app.services.video_studio import logos, quality, render, render_checks

FRAME_W, FRAME_H, FPS = 1280, 720, 30.0
#: Where the subject wearing the brand's logo stands, and where the banner a kept region covers hangs (0-1 boxes, both
#: of the logo's 2:1 shape in pixels: 256x128 and 320x160).
LOGO_BOX = (0.10, 0.40, 0.30, 0.40 + 128 / FRAME_H)
BANNER_BOX = (0.60, 0.08, 0.85, 0.08 + 160 / FRAME_H)
#: The region the shot keeps, ``[x, y, width, height]`` shares, around the banner.
KEPT = [[0.58, 0.06, 0.29, 0.27]]


def _logo() -> Image.Image:
    """A two-colour brand mark (RGBA): a disc and a bar, transparent around them."""
    mark = Image.new("RGBA", (240, 120), (0, 0, 0, 0))
    pen = ImageDraw.Draw(mark)
    pen.ellipse((8, 8, 112, 112), fill=(230, 60, 40, 255))
    pen.rectangle((128, 28, 232, 92), fill=(30, 90, 220, 255))
    pen.rectangle((152, 48, 208, 72), fill=(250, 250, 250, 255))
    return mark


def _paste(frame: Image.Image, picture: Image.Image, box: tuple[float, float, float, float]) -> None:
    x0, y0, x1, y1 = (round(v * s) for v, s in zip(box, (FRAME_W, FRAME_H, FRAME_W, FRAME_H), strict=True))
    shown = picture.resize((x1 - x0, y1 - y0), Image.Resampling.LANCZOS)
    frame.paste(shown, (x0, y0), shown if shown.mode == "RGBA" else None)


def _banner() -> Image.Image:
    """A third-party event banner: small dark lettering on a light strip, nothing of the brand's mark."""
    banner = Image.new("RGB", (320, 58), (236, 232, 220))
    pen = ImageDraw.Draw(banner)
    for index in range(14):
        x = 10 + index * 22
        pen.rectangle((x, 16 + (index % 3) * 3, x + 12, 42), fill=(30, 30, 30))
    return banner


def _street(*, banner_is_logo: bool) -> Image.Image:
    """A street frame: textured ground, the subject's logo, and the kept banner (or a second logo where it hangs)."""
    rng = np.random.default_rng(11)
    frame = Image.fromarray(np.clip(rng.normal(110, 14, (FRAME_H, FRAME_W, 3)), 0, 255).astype(np.uint8), "RGB")
    _paste(frame, _logo(), LOGO_BOX)
    _paste(frame, _logo() if banner_is_logo else _banner(), BANNER_BOX)
    return frame


def _places() -> list[tuple[str, tuple[float, float, float, float], float]]:
    """What the whole-frame search answers on such a frame: the subject's logo and the banner, both past its bar."""
    return [("logo", LOGO_BOX, 0.97), ("logo", BANNER_BOX, 0.88)]


def test_a_place_over_a_kept_region_is_recognised():
    assert logos.in_kept_region(BANNER_BOX, KEPT)
    assert not logos.in_kept_region(LOGO_BOX, KEPT)
    assert not logos.in_kept_region(BANNER_BOX, None)


def test_kept_lettering_is_cleared_and_a_kept_logo_stays():
    marks = {"logo": _logo()}
    kept, cleared = logos.confirmed_places(_street(banner_is_logo=False), marks, _places(), KEPT)
    assert [box for _role, box, _score in kept] == [LOGO_BOX]
    assert len(cleared) == 1 and cleared[0][1] == BANNER_BOX and cleared[0][3] < logos.KEPT_MIN_MATCH and cleared[0][4]
    kept, cleared = logos.confirmed_places(_street(banner_is_logo=True), marks, _places(), KEPT)
    assert len(kept) == 2 and cleared == []


def test_the_watermark_is_never_cleared_by_a_kept_region():
    places = [("logo", LOGO_BOX, 0.97), (logos.WATERMARK_ROLE, BANNER_BOX, 0.9)]
    kept, cleared = logos.confirmed_places(_street(banner_is_logo=False), {"logo": _logo()}, places, KEPT)
    assert len(kept) == 2 and cleared == []


def test_the_finding_names_the_file_and_score_of_each_place_and_the_kept_region():
    scene = {"id": "s1", "type": "clip"}
    finding = logos.duplicate_logo_finding(
        scene, _places(), at=1.0, watermarked=False, clip_id="c1", regions=KEPT, assets={"logo": "logo.png"}
    )
    assert finding is not None
    assert finding.data["matched"] == [
        {"role": "logo", "asset": "logo.png", "score": 0.97, "kept_region": False},
        {"role": "logo", "asset": "logo.png", "score": 0.88, "kept_region": True},
    ]
    assert "keep_regions" in (finding.fix or "") and "generate the shot again" not in (finding.fix or "")
    assert "logo 0.97" in finding.message


def _run(
    tmp_path: Path, monkeypatch, *, banner_is_logo: bool, regions: list | None, scene_watermark: bool = False
) -> tuple[list, list[str]]:
    logo_path = tmp_path / "logo.png"
    _logo().save(logo_path)
    picture = tmp_path / "frame.png"
    _street(banner_is_logo=banner_is_logo).save(picture)

    async def extract_frames(path, numbers, *, fps, out_dir):
        return dict.fromkeys(numbers, str(picture))

    monkeypatch.setattr(render.encoder, "extract_frames", extract_frames)
    # The whole-frame search is pinned to what it answers on such a frame: this test is about the kept region.
    monkeypatch.setattr(render.logos, "brand_marks_on", lambda frame, marks: _places())
    clip = SimpleNamespace(id="s1", type="clip", clip_id="c1")
    media = SimpleNamespace(
        assets=SimpleNamespace(images={"logo": str(logo_path)}), kept_regions={"c1": regions} if regions else {}
    )
    document = {
        "watermark": "bottom-right",
        "height": 1080,
        "duration": 4.0,
        "fps": FPS,
        "scenes": [
            {"id": "s1", "type": "clip", "start": 0.0, "end": 4.0, **({} if scene_watermark else {"watermark": False})}
        ],
    }
    notes: list[str] = []
    found = asyncio.run(
        render._duplicate_logos(
            SimpleNamespace(scenes=[clip]), document, media, str(tmp_path / "p.mp4"), tmp_path,
            fps=FPS, span=None, scene_id=None, notes=notes,
        )
    )
    return found, notes


def test_a_shot_keeping_banner_lettering_with_the_watermark_off_previews_with_no_duplicate_logo(tmp_path, monkeypatch):
    found, notes = _run(tmp_path, monkeypatch, banner_is_logo=False, regions=KEPT)
    assert found == []
    assert any("region its AI shot keeps" in note for note in notes)


def test_the_brand_logo_twice_in_the_footage_with_the_watermark_off_is_a_fact(tmp_path, monkeypatch):
    """dev:duplicate-logo-gate-false-positive-after-8465: the page draws no mark over the scene, so both logos are the
    footage's own, a note and no finding."""
    found, notes = _run(tmp_path, monkeypatch, banner_is_logo=True, regions=KEPT)
    assert found == []
    assert any("places of its footage" in note and "logo.png" in note for note in notes)


def test_the_brand_logo_twice_under_the_watermark_still_blocks_inside_a_kept_region(tmp_path, monkeypatch):
    found, _notes = _run(tmp_path, monkeypatch, banner_is_logo=True, regions=KEPT, scene_watermark=True)
    assert len(found) == 1
    finding, proof = found[0]
    assert finding.kind == render_checks.DUPLICATE_LOGO and finding.severity == render_checks.FAIL
    assert [m["asset"] for m in finding.data["matched"]] == ["logo.png", "logo.png"]
    assert Path(proof).is_file()


def test_without_kept_regions_a_banner_with_no_mark_is_never_reported(tmp_path, monkeypatch):
    """The banner reached the whole-frame bar (pinned here) but the brand's mark is not on the box itself."""
    found, notes = _run(tmp_path, monkeypatch, banner_is_logo=False, regions=None, scene_watermark=True)
    assert found == []
    assert any("no brand mark there" in note for note in notes)


def test_only_a_mark_the_page_draws_makes_a_duplicate():
    document = {"watermark": "bottom-right"}
    assert logos.page_draws_brand(document, {"type": "clip"}) is True
    assert logos.page_draws_brand(document, {"type": "clip", "watermark": False}) is False
    assert logos.page_draws_brand(document, {"type": "clip", "watermark": False}, overlay="logo") is True
    assert logos.page_draws_brand({"watermark": "none"}, {"type": "statement", "show_logo": True}) is True


def test_the_report_version_moved():
    assert quality.QUALITY_REPORT_VERSION >= 48
