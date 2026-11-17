"""accept_attempt: a failed take whose only text findings lie under its screen insert is promoted (#10056)."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.models.video_studio import JOB_FAILED, JOB_READY
from app.services.video_studio import clips, frame_qc

QUAD = [[0.30, 0.20], [0.70, 0.20], [0.70, 0.60], [0.30, 0.60]]
UNDER = [0.40, 0.30, 0.10, 0.05]  # centre 0.45, 0.325: on the screen
OUTSIDE = [0.05, 0.80, 0.10, 0.05]  # centre 0.10, 0.825: off the screen


@pytest.fixture(autouse=True)
def _insert(monkeypatch):
    monkeypatch.setattr(clips, "carries_insert", lambda params: bool(params.get("insert")))


def _record(*boxes: list[float]) -> dict:
    return {
        "status": "failed",
        "invented": [{"word": f"Xqzt{i}", "frame": "middle_frame", "box": box} for i, box in enumerate(boxes)],
        "failure": None,
    }


def _ready_clip(record: dict, *, insert: bool = True, reason: str = "text drift: the video model redrew ...") -> SimpleNamespace:
    attempt = {
        "attempt": 2,
        "status": JOB_FAILED,
        "reason": reason,
        "file_id": "11111111-1111-1111-1111-111111111111",
        "poster_file_id": "22222222-2222-2222-2222-222222222222",
        "duration_ms": 6000,
        "width": 1920,
        "height": 1080,
        "has_audio": False,
        "frame_qc": {frame_qc.TEXT_CHECK: record},
    }
    params = {
        "insert": insert,
        clips.SURFACE: {"quad": QUAD},
        clips.KEPT_TAKE: {"prompt": "p", "model": "m"},
        clips.ATTEMPTS: [attempt],
    }
    return SimpleNamespace(
        status=JOB_READY, file_id="aaaaaaaa-0000-0000-0000-000000000000", poster_file_id="bbbbbbbb-0000-0000-0000-000000000000",
        params=params, error=None,
    )


def test_a_failed_attempt_whose_only_findings_lie_under_the_insert_is_promoted():
    clip = _ready_clip(_record(UNDER))
    columns, notes = clips.promote_attempt(clip, "11111111-1111-1111-1111-111111111111")
    assert columns["status"] == JOB_READY and columns["file_id"] == "11111111-1111-1111-1111-111111111111"
    assert columns["duration_ms"] == 6000 and columns["width"] == 1920
    params = columns["params"]
    assert params[clips.ATTEMPTS] == [] and clips.KEPT_TAKE not in params
    assert "aaaaaaaa-0000-0000-0000-000000000000" in params[clips.REPLACED_MEDIA]
    text = params[frame_qc.QC_KEY][frame_qc.TEXT_CHECK]
    assert text["status"] == "accepted" and text["invented"] == [] and len(text["under_insert"]) == 1
    assert params[clips.ACCEPTED_ATTEMPT]["file_id"] == "11111111-1111-1111-1111-111111111111"
    assert "covers" in notes[0]


def test_a_finding_outside_the_screen_refuses_the_promotion():
    clip = _ready_clip(_record(UNDER, OUTSIDE))
    with pytest.raises(clips.ClipError, match="outside the screen"):
        clips.promote_attempt(clip, "11111111-1111-1111-1111-111111111111")


def test_a_failed_word_without_a_box_counts_as_outside():
    record = {"status": "failed", "invented": [], "failure": {"word": "Filigran", "seen": "Filigarn", "frame": "last_frame"}}
    with pytest.raises(clips.ClipError, match="Filigarn"):
        clips.promote_attempt(_ready_clip(record), "11111111-1111-1111-1111-111111111111")


def test_a_take_that_failed_for_another_reason_is_refused():
    clip = _ready_clip({"status": "passed"}, reason="content drift: a cow in front of the screen")
    with pytest.raises(clips.ClipError, match="another reason"):
        clips.promote_attempt(clip, "11111111-1111-1111-1111-111111111111")


def test_a_shot_without_a_screen_insert_is_refused():
    with pytest.raises(clips.ClipError, match="no screen insert"):
        clips.promote_attempt(_ready_clip(_record(UNDER), insert=False), "11111111-1111-1111-1111-111111111111")


def test_an_unknown_file_is_refused():
    with pytest.raises(clips.ClipError, match="no failed take"):
        clips.promote_attempt(_ready_clip(_record(UNDER)), "99999999-9999-9999-9999-999999999999")


def test_the_failed_take_of_a_failed_shot_is_promoted_in_place():
    params = {"insert": True, clips.SURFACE: {"quad": QUAD}, frame_qc.QC_KEY: {frame_qc.TEXT_CHECK: _record(UNDER)}}
    clip = SimpleNamespace(
        status=JOB_FAILED, file_id="cccccccc-0000-0000-0000-000000000000", poster_file_id=None, params=params,
        error="text drift: ...",
    )
    columns, _notes = clips.promote_attempt(clip, "cccccccc-0000-0000-0000-000000000000")
    assert columns["status"] == JOB_READY and columns["error"] is None
    assert "file_id" not in columns
    assert columns["params"][frame_qc.QC_KEY][frame_qc.TEXT_CHECK]["status"] == "accepted"


def test_the_quality_report_version_moved_with_the_accepted_takes():
    """A take promoted clears its invented words under the insert: a report kept before must not be read back."""
    from app.services.video_studio import quality

    assert quality.QUALITY_REPORT_VERSION >= 54


def test_end_attempt_keeps_the_size_and_length_of_the_failed_take():
    params = {clips.KEPT_TAKE: {"prompt": "p"}, "attempt": 1}
    made = {"file_id": "f", "poster_file_id": "p", "duration_ms": 5000, "width": 1280, "height": 720, "has_audio": True}
    clips.end_attempt(params, status=JOB_FAILED, reason="text drift", made=made)
    kept = params[clips.ATTEMPTS][-1]
    assert (kept["duration_ms"], kept["width"], kept["height"], kept["has_audio"]) == (5000, 1280, 720, True)
