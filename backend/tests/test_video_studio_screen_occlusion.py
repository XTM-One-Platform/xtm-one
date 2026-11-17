"""The screen of a shot carrying a screen insert, occluded on its take: said when it is made and at save, with a trim.

Video b56997a2 (scenes demo-xtm-one and demo-openaev): a subject crossing the keyed screen of a shot was caught only
by the render's ``insert_through_foreground`` gate, 10 to 20 minutes after the render started, and the author
regenerated blind or trimmed by eye (dev:screen-occlusion-check).

* F71: a take whose screen a hand enters on its middle frame keeps, with its foreground measure
  (``insert_keep_out.Overlap``), the first occluded frame, its second and the box covered there, the share of occluded
  frames and the ``trim_end`` one frame before it; the shot's answer says it.
* F72: a take whose screen stays clear says no occlusion and suggests no trim.
* F73: at save, a clip scene carrying a screen insert over that shot gets an advisory ``insert_occluded`` finding with
  the frame, the box and the trim (``data``); a scene already trimmed before the first occluded frame, or carrying no
  screen insert, gets none.
* F74: a screen occluded from the first frame suggests no trim: the finding says to generate the shot again.

Synthetic frames, a fake provider and an in-memory Redis only.
"""

from __future__ import annotations

import pytest

from app.models.video_studio import JOB_READY
from app.services.video_studio import encoder, frame_qc, insert_keep_out, timeline
from app.services.video_studio.quality import ContentFacts, ShotFacts, content_findings
from app.services.video_studio.render_checks import BLOCKING_SEVERITIES
from app.services.video_studio.spec import authored_spec, parse_video_spec
from tests.test_video_studio_insert_keep_out import _frames, shot  # noqa: F401
from tests.test_video_studio_regeneration import _run, redis, world  # noqa: F401

SHOT = "00000000-0000-0000-0000-00000000c071"
SOURCE = "00000000-0000-0000-0000-00000000f071"
INSERT_QUAD = [[0.3, 0.2], [0.7, 0.2], [0.7, 0.6], [0.3, 0.6]]

pytestmark = pytest.mark.usefixtures("no_default_video_template")


# ── F71 / F72: measured when the take is made ─────────────────────────────


def test_f71_a_hand_entering_the_screen_mid_take_is_said_with_its_first_frame_box_and_trim(shot, monkeypatch):  # noqa: F811
    monkeypatch.setattr(encoder, "iter_rgb_frames", _frames(0.04))
    _run(shot)
    row = shot.row
    assert row.status == JOB_READY
    measured = frame_qc.summary(row.params)["foreground_overlap"]
    # 12 frames at 24 fps, the hand from frame 6 on: half the frames, first at 0.25 s, the trim one frame before.
    assert measured["first_frame"] == 6 and measured["first_at"] == 0.25 and measured["fps"] == 24.0
    assert measured["occluded_share"] == 0.5 and measured["trim_end"] == 0.21
    x0, y0, x1, y1 = measured["first_box"]
    # The hand spans x 0.42-0.58 of the frame and rises from the screen's bottom edge (y 0.60).
    assert 0.39 <= x0 <= 0.45 and 0.55 <= x1 <= 0.61 and 0.5 <= y0 < y1 <= 0.61
    [said] = [note for note in row.params["notes"] if note.startswith("Occlusion:")]
    assert "on 50% of the take's frames, first at 0.25 s (frame 6), over x " in said
    assert said.endswith("set trim_end to 0.21 s to end the shot before it.")


def test_f72_a_take_whose_screen_stays_clear_says_no_occlusion(shot, monkeypatch):  # noqa: F811
    monkeypatch.setattr(encoder, "iter_rgb_frames", _frames(0.0))
    _run(shot)
    row = shot.row
    assert row.status == JOB_READY
    measured = frame_qc.summary(row.params)["foreground_overlap"]
    assert measured["first_frame"] is None and measured["occluded_share"] == 0.0 and measured["trim_end"] is None
    assert not [note for note in row.params["notes"] if note.startswith("Occlusion:")]


def test_a_measure_kept_by_an_earlier_release_reads_back_without_an_occlusion():
    earlier = {"frames": 12, "key": "green", "max_share": 0.04, "frame": 6, "at": 0.25, "confidence": 0.9, "frames_in_front": 6}
    overlap = insert_keep_out.Overlap.from_dict(earlier)
    assert overlap is not None and overlap.first_frame is None
    assert overlap.occlusion() is None and overlap.trim_end() is None
    assert insert_keep_out.Overlap.from_dict(overlap.as_dict()) == overlap


# ── F73 / F74: said at save ───────────────────────────────────────────────


def _occluded(first_frame: int = 6, *, frames: int = 12, max_share: float = 0.04) -> dict:
    first_at = round(first_frame / 24.0, 2)
    overlap = insert_keep_out.Overlap(
        frames, "green", max_share, first_frame, first_at, 0.9, frames - first_frame, first_frame, first_at, (0.42, 0.56, 0.58, 0.6), 24.0
    )
    return overlap.as_dict()


def _findings(occlusion: dict | None, *, trim_end: float | None = None, insert: bool = True) -> list:
    scene: dict = {"id": "demo", "type": "clip", "clip_id": SHOT}
    if trim_end is not None:
        scene["trim_end"] = trim_end
    if insert:
        scene["overlays"] = [{"type": "screen_insert", "quad": INSERT_QUAD, "source": {"file_id": SOURCE}, "duration": 4}]
    raw = {"title": "Synthetic", "freestyle": True, "scenes": [scene]}
    spec = parse_video_spec(raw)
    shots = {"demo": timeline.ClipMedia(duration=4.0, width=1920, height=1080, fps=24.0, frame_count=96)}
    document = timeline.estimate_plan(spec, {}, shots).document
    booth = ShotFacts("Booth", "ready", width=1920, height=1080, camera="unspecified", motion_ssim=0.9, occlusion=occlusion)
    facts = ContentFacts(shots={SHOT: booth}, output=(1920, 1080))
    return [f for f in content_findings(spec, document, facts, authored_spec(raw)) if f.kind == "insert_occluded"]


def test_f73_a_scene_carrying_a_screen_insert_over_an_occluded_take_gets_an_advisory_with_the_trim():
    [finding] = _findings(_occluded())
    assert finding.scene_id == "demo" and finding.severity == "advisory" and finding.group == "content"
    assert finding.severity not in BLOCKING_SEVERITIES and finding.clip_id == SHOT
    assert 'its AI shot "Booth": something stands in front of the screen its screen insert is drawn on' in finding.message
    assert "on 50% of the take's frames, first at 0.25 s (frame 6), over x 0.42-0.58, y 0.56-0.60 of the frame" in finding.message
    assert finding.message.endswith("set trim_end: 0.21 on the scene to end it one frame before, or generate the shot again in place")
    assert finding.data == {
        "first_frame": 6,
        "first_at": 0.25,
        "first_box": [0.42, 0.56, 0.58, 0.6],
        "occluded_share": 0.5,
        "max_share": 0.04,
        "trim_end": 0.21,
    }
    # Already trimmed before the first occluded frame: nothing to say. Trimmed after it: still said.
    assert _findings(_occluded(), trim_end=0.2) == []
    assert len(_findings(_occluded(), trim_end=1.0)) == 1
    # No screen insert on the scene, no measure, or a clear screen: nothing to say.
    assert _findings(_occluded(), insert=False) == []
    assert _findings(None) == []
    assert _findings(insert_keep_out.Overlap(12, "green").as_dict()) == []


def test_f74_a_screen_occluded_from_the_first_frame_suggests_generating_the_shot_again():
    occluded = _occluded(0, max_share=0.3)
    overlap = insert_keep_out.Overlap.from_dict(occluded)
    assert overlap is not None and overlap.trim_end() is None and overlap.occluded_share == 1.0
    assert "it stands there from the first frame, so no trim keeps the screen clear: regenerate the take" in (overlap.occlusion() or "")
    [finding] = _findings(occluded)
    assert finding.data["trim_end"] is None and "first at 0.00 s (frame 0)" in finding.message
    assert finding.message.endswith("no trim keeps the screen clear: generate the shot again in place")


def test_a_screen_showing_no_key_says_no_occlusion():
    unmatted = insert_keep_out.Overlap(0, None)
    assert unmatted.occlusion() is None and _findings(unmatted.as_dict()) == []
