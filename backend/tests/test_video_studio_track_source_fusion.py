"""A tracked insert's bezel read is fused into its track: a smooth footage gives a smooth quad. No footage: quads given.

On a slowly drifting keyed screen, the tracker places most frames and the bezel read isolated ones; each read restarts
the tracker from its own quad, read noise included. Blended switch by switch, the quad kept a staircase of the reads'
noise and stepped past the jitter gate at the switches, and the gate's fix asked for the shot again although the
footage moved smoothly (``insert_tracking.blended``, ``insert_checks._jitter_action``).

* the motion the tracker saw is kept, the offset between the sources low-passed: a linear drift with a noisy read
  every ~20 frames passes ``insert_jitter`` and its second difference stays under the gate, within the alignment gate;
* a real jump of the footage still fails ``insert_jitter`` and still says the surface jumps;
* a jitter by a switch of source names the track, not the shot; the frames near a switch are said.
"""

from __future__ import annotations

import numpy as np

from app.services.video_studio import insert_checks, insert_tracking
from app.services.video_studio.surface_refine import ALIGNMENT_MAX_SHARE

#: The tracking size (pixels): a share of the frame's width is read against its width.
W, H = 960, 540
#: Frames of the shot, the drift per frame (pixels), and how often the bezel read places a frame.
COUNT, SPEED, EVERY = 168, 0.5, 20
#: How far the bezel read stands from the tracker's quad (pixels, across): both on the screen, within the alignment
#: gate, the read's noise of up to 1 px per corner on top.
READ_OFF = 1.2
#: The screen's corners on the first frame (pixels).
SCREEN = np.array([[315.0, 150.0], [645.0, 153.75], [642.75, 341.25], [317.25, 337.5]])


def _truth(frame: int) -> np.ndarray:
    return SCREEN + np.array([SPEED * frame, 0.0])


def _followed(jump_at: int | None = None, jump: float = 0.0) -> tuple[list[np.ndarray], list[bool]]:
    """What the tracker and the bezel read place: the read every :data:`EVERY` frames, :data:`READ_OFF` to the right
    with up to 1 px of noise per corner, the tracker on the screen elsewhere (two switches per read); a footage that
    jumps by *jump* pixels at *jump_at*."""
    rng = np.random.default_rng(7)

    def truth(n: int) -> np.ndarray:
        return _truth(n) + ([jump, 0.0] if jump_at is not None and n >= jump_at else [0.0, 0.0])

    quads, by_bezel = [truth(0)], [False]
    for n in range(1, COUNT):
        if n % EVERY == 0:
            quads.append(truth(n) + np.array([READ_OFF, 0.0]) + rng.uniform(-1.0, 1.0, size=(4, 2)))
            by_bezel.append(True)
        else:
            quads.append(truth(n))
            by_bezel.append(False)
    return quads, by_bezel


def _audits(quads, switch_frames=()) -> dict[int, dict]:
    return {
        n: {
            "refined": [[x / W, y / H] for x, y in q],
            "size": [W, H],
            **({"track_switch": True} if n in switch_frames else {}),
        }
        for n, q in enumerate(quads)
    }


def _second_differences(quads) -> list[float]:
    """Each frame's quad against the midpoint of its neighbours, mean over the corners, a share of the width."""
    return [
        float(np.linalg.norm(quads[n] - (quads[n - 1] + quads[n + 1]) / 2, axis=1).mean()) / W
        for n in range(1, len(quads) - 1)
    ]


def test_a_smooth_drift_with_noisy_reads_passes_the_jitter_gate():
    quads, by_bezel = _followed()
    raw = insert_checks.quad_jitter(_audits(quads))
    assert raw is not None and raw.past, "the raw two-source track steps past the gate at the reads"
    fused = insert_tracking.blended(quads, by_bezel)
    assert max(_second_differences(fused)) <= insert_checks.JITTER_MAX_SHARE, max(_second_differences(fused))
    jitter = insert_checks.quad_jitter(_audits(fused))
    assert jitter is not None and jitter.past == ()
    # Every frame stays on the screen: within the alignment gate.
    worst = max(float(np.linalg.norm(quad - _truth(n), axis=1).mean()) / W for n, quad in enumerate(fused))
    assert worst < ALIGNMENT_MAX_SHARE, worst


def test_a_real_jump_of_the_footage_still_fails_and_says_the_surface_jumps():
    quads, by_bezel = _followed(jump_at=70, jump=5.0)
    fused = insert_tracking.blended(quads, by_bezel)
    jitter = insert_checks.quad_jitter(_audits(fused))
    assert jitter is not None and jitter.share > insert_checks.JITTER_MAX_SHARE and 70 in jitter.past
    # Its bezel is where it is drawn on both frames: the footage jumped.
    frames = _audits(fused)
    for n in (jitter.before, jitter.frame):
        frames[n]["alignment"] = 0.0004
    assert insert_checks._jitter_action(frames, jitter)["text"].startswith("the surface itself jumps")


def test_a_jitter_by_a_switch_of_source_names_the_track():
    quads, _by_bezel = _followed()
    jitter = insert_checks.quad_jitter(_audits(quads))
    assert jitter is not None
    frames = _audits(quads, switch_frames={jitter.before, jitter.frame})
    for n in (jitter.before, jitter.frame):
        frames[n]["alignment"] = 0.0004
    chosen = insert_checks._jitter_action(frames, jitter)
    assert chosen["kind"] == insert_checks.GIVE_QUAD and "track switches" in chosen["text"]
    # The same step off any switch: the footage is blamed as before.
    plain = insert_checks._jitter_action(
        _audits(quads) | {n: frames[n] | {"track_switch": None} for n in frames}, jitter
    )
    assert "surface itself jumps" in plain["text"]


def test_the_frames_near_a_switch_are_said():
    quads = tuple(tuple((0.3, 0.3) for _ in range(4)) for _ in range(60))
    track = insert_tracking.InsertTrack(100, quads, (0.3, 0.3, 0.3, 0.3), (130,), held_before=2)
    assert track.near_switch(130) and track.near_switch(131) and track.near_switch(130 - insert_tracking.BLEND_FRAMES)
    assert not track.near_switch(115) and not track.near_switch(102) and not track.near_switch(None)
    assert not insert_tracking.InsertTrack(0, quads, (0.3, 0.3, 0.3, 0.3)).near_switch(10)
