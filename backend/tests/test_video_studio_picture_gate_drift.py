"""A picture check never flips a passed render to failed on identical pixels (dev:render-picture-check-gate-drift).

* ``duplicate_logo``: a statement's white lettering over dimmed footage, under the corner watermark, is one mark - the
  watermark - never a second brand mark matched by the shape of a flat white wordmark.
* ``insert_matte_halo`` / ``insert_matte_lag``: frames whose mask of what stands in front is guessed (low
  ``front_confidence``) never block; only such frames failing is a warning that says so.
* A picture gate newer than a ready render of the same revision, drawn alike, that passed it is an advisory naming it
  ("new gate since"); a drawing that moved keeps the block.
"""

from __future__ import annotations

import numpy as np
from PIL import Image, ImageDraw, ImageFont

from app.services.video_studio import insert_checks, logos, render_checks, screen_insert

DESIGN_H = 1080


def _wordmark() -> Image.Image:
    """A flat white wordmark (RGBA), as a dark template's logo often is."""
    mark = Image.new("RGBA", (400, 80), (0, 0, 0, 0))
    ImageDraw.Draw(mark).text((4, 4), "FILIGRAN", fill=(255, 255, 255, 255), font=ImageFont.load_default(size=64))
    return mark


def _ring() -> Image.Image:
    """A two-colour mark (RGBA)."""
    mark = Image.new("RGBA", (120, 60), (0, 0, 0, 0))
    pen = ImageDraw.Draw(mark)
    pen.ellipse((4, 4, 56, 56), fill=(230, 60, 40, 255))
    pen.rectangle((64, 14, 116, 46), fill=(30, 90, 220, 255))
    return mark


def _statement(watermark: Image.Image) -> Image.Image:
    """A 720p statement over footage: textured footage dimmed, white lettering, the watermark bottom-right."""
    rng = np.random.default_rng(3)
    frame = Image.fromarray(np.clip(rng.normal(110, 30, (720, 1280, 3)), 0, 255).astype(np.uint8), "RGB").convert("RGBA")
    frame = Image.alpha_composite(frame, Image.new("RGBA", frame.size, (0, 0, 0, 150)))
    pen = ImageDraw.Draw(frame)
    pen.text((140, 260), "Threat intel at scale", fill=(255, 255, 255, 255), font=ImageFont.load_default(size=96))
    pen.text((140, 400), "FILIGRAN for all", fill=(255, 255, 255, 255), font=ImageFont.load_default(size=72))
    rgb = frame.convert("RGB")
    h = round(logos.WATERMARK_HEIGHT_PX * 720 / DESIGN_H)
    w = round(h * watermark.width / watermark.height)
    small = watermark.resize((w, h))
    margin = round(logos.WATERMARK_MARGIN_SHARE * 720)
    rgb.paste(small, (1280 - margin - w, 720 - margin - h), small)
    return rgb


def test_one_watermark_and_a_statement_over_footage_is_one_mark():
    marks = {"logo": _wordmark(), "logo_mark": _ring()}
    frame = _statement(_ring())
    places = logos.with_watermark(
        logos.brand_marks_on(frame, marks), logos.watermark_on(frame, _ring(), "bottom-right", design_height=DESIGN_H)
    )
    assert len(places) == 1 and places[0][0] == logos.WATERMARK_ROLE
    scene = {"id": "s4", "type": "statement", "backdrop": "clip"}
    assert logos.duplicate_logo_finding(scene, places, at=20.395, watermarked=True) is None


def test_a_flat_mark_painted_whole_on_the_footage_is_still_found():
    rng = np.random.default_rng(5)
    frame = Image.fromarray(np.clip(rng.normal(90, 20, (720, 1280, 3)), 0, 255).astype(np.uint8), "RGB")
    shown = _wordmark().resize((480, 96))
    frame.paste(shown, (400, 300), shown)
    places = logos.brand_marks_on(frame, {"logo": _wordmark()})
    assert len(places) == 1 and places[0][2] >= logos.MARK_MIN_SHAPE_MATCH


def _frames(confidence: float | None, *, halo: float = 0.3, lag: float = 7.0, count: int = 40) -> dict[int, dict]:
    return {
        n: {"matte_halo": halo, "matte_halo_region": 0.001, "matte_lag": lag, "front_confidence": confidence}
        for n in range(count)
    }


def _placed() -> insert_checks.Placed:
    return insert_checks.Placed("insert/s2.0", "s2", "Scene s2: a screen insert", 0.0, 2.0)


def _matte(frames: dict[int, dict]) -> list:
    return [f for f, _ in insert_checks._matte_findings("insert/s2.0", _placed(), frames, {}, lambda n: n / 30)]


def test_matte_gates_on_guessed_masks_alone_are_warnings_that_say_so():
    found = _matte(_frames(screen_insert.LOW_FRONT_CONFIDENCE / 2))
    kinds = {f.kind: f for f in found}
    assert set(kinds) == {render_checks.INSERT_MATTE_HALO, render_checks.INSERT_MATTE_LAG}
    for finding in found:
        assert finding.severity == render_checks.WARN
        assert finding.data["guessed_mask"] is True and "guessed" in finding.message


def test_matte_gates_on_sure_masks_keep_blocking():
    found = _matte(_frames(0.9))
    assert found and all(f.severity == render_checks.FAIL for f in found)
    assert all("guessed_mask" not in f.data for f in found)


def _checks() -> render_checks.Checks:
    return render_checks.Checks(
        findings=[
            render_checks.Finding(render_checks.INSERT_MATTE_HALO, 7.12, 9.0, "s2", "halo", data={"insert": "insert/s2.0"}),
            render_checks.Finding(render_checks.DUPLICATE_LOGO, 20.395, None, "s4", "brand mark in 3 places"),
        ]
    )


WHOLE = {"s2": "d2", "s4": "d4"}
RELEASE = {"drawing": 9, "insert_build": 4}


def _earlier(*, whole=WHOLE, release=RELEASE, findings=()) -> list[dict]:
    return [
        {
            "id": "5eb54d60-0373-447c-8e19-f260a1f11d2c",
            "finished_at": "2026-11-02T18:00:00+00:00",
            "checks": {"findings": list(findings)},
            "drawing": {"whole": whole, "release": release},
        }
    ]


def test_identical_frames_and_a_newer_checker_never_flip_green_to_blocked():
    checks = _checks()
    assert checks.failed
    settled = render_checks.settle_new_gates(checks, _earlier(), whole=dict(WHOLE), release=dict(RELEASE))
    assert len(settled) == 2 and not checks.failed
    for finding in checks.findings:
        assert finding.severity == render_checks.WARN
        assert finding.message.startswith("new gate since render 5eb54d60")
        assert finding.data["new_gate_since"]["render_id"].startswith("5eb54d60")
    assert any("advisories" in note for note in checks.notes)


def test_a_redrawn_insert_or_another_drawing_release_keeps_blocking():
    for earlier in (_earlier(whole={"s2": "other", "s4": "d4"}), _earlier(release={"drawing": 8, "insert_build": 4})):
        checks = _checks()
        assert render_checks.settle_new_gates(checks, earlier, whole=dict(WHOLE), release=dict(RELEASE)) == []
        assert checks.failed


def test_a_previous_render_that_failed_a_picture_gate_settles_nothing():
    failed = {"kind": render_checks.DUPLICATE_LOGO, "severity": render_checks.FAIL, "at": 1.0, "scene_id": "s4", "message": "m"}
    checks = _checks()
    assert render_checks.settle_new_gates(checks, _earlier(findings=[failed]), whole=dict(WHOLE), release=dict(RELEASE)) == []
    assert checks.failed
