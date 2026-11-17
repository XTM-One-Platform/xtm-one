"""A tracked insert placed by two sources is blended where the source switches. No footage: quads given.

On a keyed green screen in a drifting AI shot, a tracked insert is placed frame by frame by the tracker and by the
bezel read (``insert_tracking``). Both lie on the surface, within the alignment gate (0.5% of the frame's width), but
they can stand up to that far apart: at each switch the drawn quad stepped by it on one frame, and the scene preview
failed ``insert_jitter`` (gate 0.1%) on frames that came in pairs, at the switches.

* the step of a switch, beyond the surface's own drift through it, is spread over ``BLEND_FRAMES`` frames: no
  frame-to-frame step at a switch past the jitter gate, and every frame of the ramp between the two sources (so within
  the alignment gate);
* a track placed by one source throughout is unchanged (an untracked or single-source insert draws as before);
* the save and the render say the spans each source placed.
"""

from __future__ import annotations

import numpy as np

from app.services.video_studio import insert_checks, insert_tracking
from app.services.video_studio.surface_refine import ALIGNMENT_MAX_SHARE

#: The tracking size's width (pixels): a share of the frame's width is read against it.
W, H = 960, 540
#: Frames of the shot, and the camera's drift over them (1% of the width over 8 s at 24 fps).
COUNT, DRIFT = 192, 0.01
#: The screen's corners on the first frame (pixels).
SCREEN = np.array([[315.0, 150.0], [645.0, 153.75], [642.75, 341.25], [317.25, 337.5]])
#: How far the bezel read stands from the tracker's quad: just inside the alignment gate.
OFF = 0.0045 * W


def _truth(frame: int) -> np.ndarray:
    return SCREEN - [DRIFT * W * frame / (COUNT - 1), 0.0]


def _two_sources(by_bezel: list[bool]) -> list[np.ndarray]:
    """The tracker's quad on the screen; the bezel read's :data:`OFF` to its right on the frames it placed."""
    return [_truth(n) + ([OFF, 0.0] if bezel else [0.0, 0.0]) for n, bezel in enumerate(by_bezel)]


def _audits(quads) -> dict[int, dict]:
    return {n: {"refined": [[x / W, y / H] for x, y in q], "size": [W, H]} for n, q in enumerate(quads)}


def _steps(quads) -> list[float]:
    """Each frame's step beyond the drift, mean over the corners, a share of the width (as ``quad_jitter`` reads it)."""
    drift = np.array([DRIFT * W / (COUNT - 1), 0.0])
    return [float(np.linalg.norm(quads[n] - quads[n - 1] + drift, axis=1).mean()) / W for n in range(1, len(quads))]


def _sources() -> list[bool]:
    """Mostly the bezel read, as the issue's save said it (21 frames by the tracker, the rest by the bezel read): the
    tracker in short spans, one of them a single frame."""
    by_bezel = [True] * COUNT
    for first, last in ((0, 0), (30, 37), (90, 90), (140, 150)):
        for n in range(first, last + 1):
            by_bezel[n] = False
    return by_bezel


def test_unblended_switches_fail_the_jitter_gate():
    """What the preview failed on: the raw two-source quads step by the gap between the sources at each switch."""
    jitter = insert_checks.quad_jitter(_audits(_two_sources(_sources())))
    assert jitter is not None and jitter.share > insert_checks.JITTER_MAX_SHARE


def test_a_switch_is_blended_under_the_jitter_gate_and_within_the_alignment_gate():
    by_bezel = _sources()
    raw = _two_sources(by_bezel)
    steady = insert_tracking.blended(raw, by_bezel)
    assert max(_steps(steady)) <= insert_checks.JITTER_MAX_SHARE, max(_steps(steady))
    jitter = insert_checks.quad_jitter(_audits(steady))
    assert jitter is not None and jitter.share <= insert_checks.JITTER_MAX_SHARE and jitter.past == ()
    # Every frame between the two sources: never farther from the screen than the farther of them.
    for n, quad in enumerate(steady):
        off = float(np.linalg.norm(quad - _truth(n), axis=1).max()) / W
        assert off <= OFF / W + 1e-9 < ALIGNMENT_MAX_SHARE, (n, off)


def test_a_track_placed_by_one_source_is_unchanged():
    quads = _two_sources([False] * COUNT)
    assert all(np.array_equal(a, b) for a, b in zip(insert_tracking.blended(quads, [False] * COUNT), quads, strict=True))
    bezel = _two_sources([True] * COUNT)
    assert all(np.array_equal(a, b) for a, b in zip(insert_tracking.blended(bezel, [True] * COUNT), bezel, strict=True))


def test_the_spans_each_source_placed_are_said():
    quads = tuple(tuple((0.3, 0.3) for _ in range(4)) for _ in range(12))
    track = insert_tracking.InsertTrack(10, quads, (0.3, 0.3, 0.3, 0.3), (14, 15, 16, 21), held_before=2)
    assert track.spans() == [("held", 10, 11), ("tracker", 12, 13), ("bezel read", 14, 16), ("tracker", 17, 20), ("bezel read", 21, 21)]
    assert track.said_spans() == (
        "frames 10-11 held, 12-13 by the tracker, 14-16 by the bezel read, 17-20 by the tracker, 21 by the bezel read; "
        f"3 source switches blended over {insert_tracking.BLEND_FRAMES} frames"
    )
    alone = insert_tracking.InsertTrack(0, quads, (0.3, 0.3, 0.3, 0.3))
    assert alone.said_spans() == "frames 0-11 by the tracker"
