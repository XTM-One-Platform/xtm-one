"""restore_take: a shot keeps the takes it played before a regeneration replaced them, and one call puts one back (#10901)."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.models.video_studio import JOB_READY, JOB_RUNNING
from app.services.video_studio import clips, frame_qc

OLD = "aaaaaaaa-0000-0000-0000-000000000000"
OLD_POSTER = "bbbbbbbb-0000-0000-0000-000000000000"
NEW = "cccccccc-0000-0000-0000-000000000000"
NEW_POSTER = "dddddddd-0000-0000-0000-000000000000"
FAILED = "eeeeeeee-0000-0000-0000-000000000000"


def _media(file_id: str, poster: str) -> dict:
    return {"file_id": file_id, "poster_file_id": poster, "duration_ms": 6000, "width": 1920, "height": 1080, "has_audio": False}


def _regenerated_params() -> dict:
    """A ready shot generated again: its take kept, one attempt failed beside it."""
    return {
        clips.KEPT_TAKE: {"prompt": "old prompt", "model": "m1", "made_at": "2026-11-10T10:00:00+00:00", "frame_qc": {"motion_ssim": 0.9}},
        clips.ATTEMPTS: [{"status": "failed", "reason": "camera moved", "file_id": FAILED}],
        frame_qc.QC_KEY: {"motion_ssim": 0.5},
    }


def test_a_promoted_regeneration_keeps_the_take_it_replaces():
    params = _regenerated_params()
    deleted = clips.retire_kept_take(params, [OLD, OLD_POSTER], media=_media(OLD, OLD_POSTER))
    assert OLD not in deleted and OLD_POSTER not in deleted
    assert FAILED in deleted
    [kept] = clips.previous_takes(params)
    assert kept["file_id"] == OLD and kept["poster_file_id"] == OLD_POSTER and kept["model"] == "m1"
    assert kept["frame_qc"] == {"motion_ssim": 0.9}
    assert clips.KEPT_TAKE not in params and clips.ATTEMPTS not in params


def test_without_its_media_the_replaced_take_is_deleted_as_before():
    params = _regenerated_params()
    deleted = clips.retire_kept_take(params, [OLD, OLD_POSTER])
    assert OLD in deleted and OLD_POSTER in deleted
    assert clips.previous_takes(params) == []


def test_only_the_newest_previous_takes_are_kept():
    params: dict = {}
    dropped: list[str] = []
    for i in range(clips.MAX_PREVIOUS_TAKES + 2):
        dropped += clips.keep_previous_take(params, _media(f"{i:08d}-0000-0000-0000-000000000000", f"{i:08d}-1111-0000-0000-000000000000"))
    assert len(clips.previous_takes(params)) == clips.MAX_PREVIOUS_TAKES
    assert dropped[:2] == ["00000000-0000-0000-0000-000000000000", "00000000-1111-0000-0000-000000000000"]
    assert clips.previous_takes(params)[-1]["file_id"].startswith(f"{clips.MAX_PREVIOUS_TAKES + 1:08d}")


def test_previous_takes_are_owned_by_the_shot():
    params: dict = {}
    clips.keep_previous_take(params, _media(OLD, OLD_POSTER))
    assert OLD in clips.side_files(params) and OLD_POSTER in clips.side_files(params)
    assert OLD in clips.side_files(params, videos_only=True)


def _shot(params: dict, status: str = JOB_READY) -> SimpleNamespace:
    return SimpleNamespace(
        status=status, prompt="new prompt", model="m2", finished_at=None, params=params, **_media(NEW, NEW_POSTER)
    )


def test_restore_puts_the_previous_take_back_and_keeps_the_one_it_replaces():
    params = {frame_qc.QC_KEY: {"motion_ssim": 0.5}, "notes": ["new take"]}
    clips.keep_previous_take(params, _media(OLD, OLD_POSTER), {"prompt": "old prompt", "model": "m1", "frame_qc": {"motion_ssim": 0.9}})
    params[frame_qc.QC_KEY] = {"motion_ssim": 0.5}
    columns, notes = clips.restore_previous_take(_shot(params), OLD)
    assert columns["status"] == JOB_READY and columns["file_id"] == OLD and columns["poster_file_id"] == OLD_POSTER
    restored = columns["params"]
    assert restored[frame_qc.QC_KEY] == {"motion_ssim": 0.9}
    assert "Restored" in notes[0] and notes[0] in restored["notes"]
    [swapped] = clips.previous_takes(restored)
    assert swapped["file_id"] == NEW and swapped["model"] == "m2" and swapped["frame_qc"] == {"motion_ssim": 0.5}
    # Undone the same way.
    playing_old = SimpleNamespace(
        status=JOB_READY, prompt="old prompt", model="m1", finished_at=None, params=restored, **_media(OLD, OLD_POSTER)
    )
    again, _ = clips.restore_previous_take(playing_old, NEW)
    assert again["file_id"] == NEW and clips.previous_takes(again["params"])[0]["file_id"] == OLD


def test_restore_refuses_a_file_that_is_no_previous_take():
    with pytest.raises(clips.ClipError, match="no previous take"):
        clips.restore_previous_take(_shot({}), OLD)


def test_restore_refuses_a_shot_being_generated_again():
    params: dict = {}
    clips.keep_previous_take(params, _media(OLD, OLD_POSTER))
    with pytest.raises(clips.ClipError, match="generated again"):
        clips.restore_previous_take(_shot(params, status=JOB_RUNNING), OLD)
