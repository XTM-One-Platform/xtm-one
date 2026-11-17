"""Picture gates never block on pixels a passed render drew alike (dev:render-picture-check-gate-drift-2).

* A failing picture finding on a frame the same, under encoder noise, as a ready render's that passed is an advisory
  "new gate since render <id>", even when the digests or the drawing release moved; a frame drawn anew keeps the block.
* ``insert_matte_halo`` / ``insert_matte_lag`` on an insert whose mask is guessed somewhere block only on a halo patch
  a viewer sees (``MATTE_HALO_VISIBLE_REGION``); a visible patch on a sure mask still blocks.
* ``duplicate_logo``: a shape matched with no contrast in its box (a faint reflection) is no mark; the proof frame
  carries the matched crops.
* The stored checks name the quality report version they were judged at.
"""

from __future__ import annotations

import io

import numpy as np
from PIL import Image, ImageDraw, ImageFont

from app.services.video_studio import insert_checks, logos, quality, render_checks, screen_insert

READY = "5eb54d60-0373-447c-8e19-f260a1f11d2c"


def _footage(seed: int = 7) -> np.ndarray:
    rng = np.random.default_rng(seed)
    return np.clip(rng.normal(90, 25, (360, 640, 3)), 0, 255).astype(np.uint8)


def _noisy(frame: np.ndarray, seed: int = 11) -> np.ndarray:
    """*frame* through an encoder's noise: under a grey level or two on every pixel."""
    rng = np.random.default_rng(seed)
    return np.clip(frame.astype(np.int16) + rng.integers(-2, 3, frame.shape), 0, 255).astype(np.uint8)


def test_the_same_frame_under_encoder_noise_is_alike_and_a_new_patch_is_not():
    frame = _footage()
    assert render_checks.frames_alike(frame, _noisy(frame))
    patched = frame.copy()
    patched[100:116, 200:216] = 10  # a 16 px halo patch drawn anew: 0.1 % of the frame
    assert not render_checks.frames_alike(patched, _noisy(frame))
    assert not render_checks.frames_alike(frame, frame[:200])


def _checks() -> render_checks.Checks:
    return render_checks.Checks(
        findings=[
            render_checks.Finding(render_checks.INSERT_MATTE_HALO, 7.4, 9.0, "s2", "halo", data={"worst_at": 7.45}),
            render_checks.Finding(render_checks.DUPLICATE_LOGO, 20.39, None, "s4", "brand mark in 3 places"),
        ]
    )


def _ready(**extra) -> dict:
    return {
        "id": READY,
        "finished_at": "2026-11-02T18:00:00+00:00",
        "checks": {"findings": []},
        "drawing": {"whole": {"s2": "old"}, "release": {"drawing": 17, "insert_build": 9}},
        "file_id": "3315a8b1-18b9-49c5-b569-db5c10d03c7e",
        **extra,
    }


def test_a_finding_on_frames_a_passed_render_drew_alike_is_an_advisory_even_after_the_digests_moved():
    checks = _checks()
    ready = _ready()
    settled = render_checks.settle_new_gates(
        checks, [ready], whole={"s2": "new"}, release={"drawing": 18, "insert_build": 9}, alike={0: ready}
    )
    assert [f.kind for f in settled] == [render_checks.INSERT_MATTE_HALO]
    halo, logo = checks.findings
    assert halo.severity == render_checks.WARN and halo.message.startswith("new gate since render 5eb54d60")
    assert halo.data["new_gate_since"] == {"render_id": READY, "finished_at": ready["finished_at"], "matched": "frames"}
    assert halo.data["worst_at"] == 7.45  # the frame evidence stays
    # The frame that differs (no entry in alike) keeps its block.
    assert logo.severity == render_checks.FAIL and checks.failed
    assert any("encoder noise" in note for note in checks.notes)


def test_a_render_that_failed_its_picture_gates_settles_nothing_even_on_alike_frames():
    failed = {"kind": render_checks.INSERT_MATTE_HALO, "severity": render_checks.FAIL, "at": 7.4, "message": "m"}
    ready = _ready(checks={"findings": [failed]})
    checks = _checks()
    assert render_checks.settle_new_gates(checks, [ready], whole={}, release={}, alike={0: ready, 1: ready}) == []
    assert all(f.severity == render_checks.FAIL for f in checks.findings)


def _frames(*, confidence: float, region: float, lag: float = 7.0, count: int = 40) -> dict[int, dict]:
    return {
        n: {"matte_halo": 0.99, "matte_halo_region": region, "matte_lag": lag, "front_confidence": confidence}
        for n in range(count)
    }


def _matte(frames: dict[int, dict]) -> list:
    where = insert_checks.Placed("insert/s2.1", "s2", "Scene s2: screen insert 2", 0.0, 2.0)
    return [f for f, _ in insert_checks._matte_findings("insert/s2.1", where, frames, {}, lambda n: n / 60)]


def test_invisible_patches_on_an_insert_whose_mask_is_guessed_somewhere_never_block():
    frames = _frames(confidence=0.9, region=0.00002)
    frames[0]["front_confidence"] = 0.07  # dark subject on a dark surface on one frame
    found = _matte(frames)
    assert {f.kind for f in found} == {render_checks.INSERT_MATTE_HALO, render_checks.INSERT_MATTE_LAG}
    for finding in found:
        assert finding.severity == render_checks.WARN
        assert finding.data["unreliable_mask"] is True
        assert "key: green" in finding.message


def test_a_visible_patch_or_a_wide_lag_still_blocks_on_an_unreliable_mask():
    frames = _frames(confidence=0.9, region=0.002, lag=12.0)
    frames[0]["front_confidence"] = 0.07
    found = {f.kind: f for f in _matte(frames)}
    assert found[render_checks.INSERT_MATTE_HALO].severity == render_checks.FAIL
    assert found[render_checks.INSERT_MATTE_LAG].severity == render_checks.FAIL
    lag_only = _frames(confidence=0.9, region=0.00002, lag=12.0)
    lag_only[0]["front_confidence"] = 0.07
    lagged = {f.kind: f for f in _matte(lag_only)}
    assert lagged[render_checks.INSERT_MATTE_LAG].severity == render_checks.FAIL


def test_invisible_patches_on_a_sure_mask_keep_blocking():
    found = _matte(_frames(confidence=0.9, region=0.00002))
    assert found and all(f.severity == render_checks.FAIL for f in found)
    assert screen_insert.LOW_FRONT_CONFIDENCE > 0.07


def _wordmark() -> Image.Image:
    mark = Image.new("RGBA", (400, 80), (0, 0, 0, 0))
    ImageDraw.Draw(mark).text((4, 4), "FILIGRAN", fill=(255, 255, 255, 255), font=ImageFont.load_default(size=64))
    return mark


def test_a_faint_reflection_of_a_mark_is_no_mark_and_a_painted_one_is():
    base = np.full((720, 1280, 3), 60, dtype=np.uint8)
    alpha = np.asarray(_wordmark().resize((480, 96)), dtype=np.float64)[..., 3:4] / 255.0
    faint = base.astype(np.float64)
    faint[400:496, 400:880] += alpha * 6  # a reflection on a glossy floor: the shape, six grey levels of contrast
    places = logos.brand_marks_on(Image.fromarray(faint.astype(np.uint8)), {"logo": _wordmark()})
    assert places == []
    painted = base.astype(np.float64)
    painted[400:496, 400:880] += alpha * 150
    places = logos.brand_marks_on(Image.fromarray(painted.astype(np.uint8)), {"logo": _wordmark()})
    assert len(places) == 1


def test_the_duplicate_logo_proof_frame_carries_the_matched_crops():
    frame = Image.fromarray(_footage())
    shown = Image.open(io.BytesIO(logos.boxed_frame_jpeg(frame, [(0.1, 0.1, 0.3, 0.2), (0.6, 0.7, 0.8, 0.8)])))
    # The crops sit under the frame, scaled down together when they are wider than it.
    assert shown.width == 640 and shown.height > 360 + 40


def test_the_checks_name_the_quality_report_version_they_were_judged_at():
    stored = render_checks.Checks().as_dict()
    assert stored["quality_report_version"] == quality.QUALITY_REPORT_VERSION
    assert quality.QUALITY_REPORT_VERSION >= 42
