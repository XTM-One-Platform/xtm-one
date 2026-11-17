"""A keyframe that re-quantises a held still never splits its frozen run.

The encoder's keyframes inside a hold redraw the still with a uniform low step (a mean of 0.42 grey level, 13 at
most, CI run 41510219264): one frame off, the next back to the run's own picture. The run goes on
(``frame_freeze.BLIP_SECONDS``); a picture that changes and stays changed still starts a new run.
"""

from __future__ import annotations

import numpy as np

from app.services.video_studio import frame_freeze

FPS = 30.0


def _still(level: int) -> np.ndarray:
    frame = np.full((54, 96), level, dtype=np.uint8)
    frame[10:20, 10:40] = 200
    return frame


def _requantised(frame: np.ndarray) -> np.ndarray:
    """The still redrawn by a keyframe: every pixel off by a little, a few by 13 levels."""
    out = frame.astype(np.int16) + 1
    out[0, :3] += 12
    return np.clip(out, 0, 255).astype(np.uint8)


def test_a_one_frame_keyframe_blip_keeps_the_run_whole():
    still = _still(80)
    frames = [still] * 30 + [_requantised(still)] + [still] * 40
    assert frame_freeze.frozen_runs(frames, FPS) == [(0, 71)]


def test_two_keyframes_two_frames_apart_keep_the_run_whole():
    """The CI render: I-frames at 5.600 and 5.667 s, the P-frame between them on the first one's level."""
    still = _still(80)
    blip = _requantised(still)
    frames = [still] * 32 + [blip, blip] + [still] * 68
    assert frame_freeze.frozen_runs(frames, FPS) == [(0, 102)]


def test_a_picture_that_changes_for_good_starts_a_new_run():
    first, second = _still(80), _still(120)
    frames = [first] * 30 + [second] * 30
    assert frame_freeze.frozen_runs(frames, FPS) == [(0, 30), (30, 60)]


def test_a_change_longer_than_a_blip_splits_the_run():
    still = _still(80)
    other = _still(120)
    frames = [still] * 30 + [other] * 10 + [still] * 30
    assert frame_freeze.frozen_runs(frames, FPS) == [(0, 30), (40, 70)]


def test_a_blip_inside_a_short_run_never_makes_a_finding():
    still = _still(80)
    frames = [still] * 5 + [_requantised(still)] + [still] * 5 + [_still(150)] * 3
    assert frame_freeze.frozen_runs(frames, FPS) == []
