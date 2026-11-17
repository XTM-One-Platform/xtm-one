"""A tracked insert drawn through the compositor follows a smoothly drifting surface smoothly. No provider: synthetic.

A tracked insert's quad is snapped to the bezel on every frame (``screen_insert._locked``, the refiner without its
temporal prior: a tracker's correction is a real one). The bezel read on one frame stands up to a pixel or so off the
one on the next - the edge's fit on encoder noise and a sub-pixel drift rasterised in whole pixels -, and drawn as read
it stepped past the jitter gate where the footage moved smoothly, however smooth the track (``insert_tracking.blended``).
These tests run the path the scene preview and the render run (``screen_insert.compose`` frame after frame with a
``Matting`` state, read back by ``insert_checks.quad_jitter``), never the fusion helper alone:

* a keyed surface drifting about half a pixel a frame, its track carrying a pixel of read noise, gives no
  ``insert_jitter`` frame past the gate, every frame within the alignment gate;
* the same surface jumping by a few pixels, the track following it, still steps past the gate there;
* the same, the bezel read itself a pixel off from one frame to the next, still draws no jitter.
"""

from __future__ import annotations

import dataclasses

import numpy as np
import pytest
from PIL import Image, ImageDraw

from app.services.video_studio import insert_checks, screen_insert, surface_refine
from app.services.video_studio.insert_tracking import InsertTrack
from app.services.video_studio.surface_refine import ALIGNMENT_MAX_SHARE

W, H = 960, 540
GREEN = (32, 255, 30)
FPS = 30.0
#: Frames composed, the drift per frame (pixels, across).
COUNT, SPEED = 36, 0.5
#: The surface's corners on the first frame (pixels).
SCREEN = np.array([(300.0, 130.0), (660.0, 140.0), (655.0, 400.0), (305.0, 392.0)])


def _truth(frame: int, jump_at: int | None = None, jump: float = 0.0) -> np.ndarray:
    moved = SCREEN + np.array([SPEED * frame, 0.0])
    return moved + np.array([jump, 0.0]) if jump_at is not None and frame >= jump_at else moved


def _frame(surface: np.ndarray, seed: int) -> np.ndarray:
    """A textured room, a dark bezel, a flat green surface at *surface* (sub-pixel corners), the encoder's noise."""
    rng = np.random.default_rng(seed)
    picture = Image.fromarray((np.random.default_rng(1).random((H, W, 3)) * 30 + 90).astype(np.uint8))
    draw = ImageDraw.Draw(picture)
    centre = surface.mean(axis=0)
    draw.polygon([tuple(p) for p in centre + (surface - centre) * 1.05], fill=(18, 20, 24))
    draw.polygon([tuple(p) for p in surface], fill=GREEN)
    frame = np.asarray(picture).astype(np.float32) + rng.normal(0.0, 5.0, (H, W, 3)).astype(np.float32)
    return np.clip(frame, 0, 255).astype(np.uint8)


def _ui() -> Image.Image:
    picture = Image.new("RGB", (1300, 940), (22, 26, 34))
    draw = ImageDraw.Draw(picture)
    for row in range(40, 900, 28):
        draw.rectangle((60, row, 1200, row + 10), fill=(200, 205, 215))
    return picture


def _track(truths: list[np.ndarray], noise: float) -> InsertTrack:
    """The track a tracker and a bezel read would give: the surface, a read's noise of up to *noise* pixels."""
    rng = np.random.default_rng(3)
    quads = []
    for quad in truths:
        placed = quad + rng.uniform(-noise, noise, size=(4, 2))
        quads.append(tuple((float(x) / W, float(y) / H) for x, y in placed))
    xs = [x for quad in quads for x, _y in quad]
    ys = [y for quad in quads for _x, y in quad]
    return InsertTrack(0, tuple(quads), (min(xs), min(ys), max(xs), max(ys)))


def _composed(truths: list[np.ndarray], *, tracked: bool, noise: float = 0.0) -> dict[int, dict]:
    """Every frame composed as the frame server composes it (one ``Matting`` state, the matte window read ahead)."""
    frames = [_frame(quad, seed=10 + n) for n, quad in enumerate(truths)]
    insert = screen_insert.InsertSource(
        "p.png",
        "image",
        tuple((float(x) / W, float(y) / H) for x, y in truths[0]),
        key=screen_insert.KeySpec("green"),
        footage=screen_insert.Footage("f", "video", FPS, "clip/demo"),
        track=_track(truths, noise) if tracked else None,
        label="Scene demo: its screen insert 0",
    )
    insert = screen_insert.keyed_on(insert, frames[0])
    picture, state, audits = _ui(), screen_insert.Matting(), {}
    for n, frame in enumerate(frames):
        ahead = [(n + k, frames[n + k]) for k in (1, 2) if n + k < len(frames)]
        layer = screen_insert.compose(insert, picture, (W, H), n, footage=frame, state=state, footage_ahead=ahead)
        assert layer is not None
        audits[n] = layer.audit.as_dict()
    return audits


def _worst_alignment(audits: dict[int, dict], truths: list[np.ndarray]) -> float:
    return max(
        float(np.linalg.norm(np.array(audits[n]["refined"]) * [W, H] - truths[n], axis=1).mean()) / W for n in audits
    )


@pytest.fixture
def noisy_bezel_read(monkeypatch):
    """The bezel read on real footage: each frame's bezel found up to a pixel off the next one's (the edge's fit on a
    soft, compressed edge), however smoothly the surface moves - what a clean synthetic edge never shows."""
    rng = np.random.default_rng(11)
    refine = surface_refine.refine

    def noisy(*args, **kwargs):
        found = refine(*args, **kwargs)
        if not found.found:
            return found
        return dataclasses.replace(
            found, quad=np.asarray(found.quad, dtype=float) + rng.uniform(-1.0, 1.0, size=(4, 2))
        )

    monkeypatch.setattr(surface_refine, "refine", noisy)


def test_a_noisy_bezel_read_never_steps_a_tracked_insert(noisy_bezel_read):
    truths = [_truth(n) for n in range(COUNT)]
    audits = _composed(truths, tracked=True, noise=1.0)
    jitter = insert_checks.quad_jitter(audits)
    assert jitter is not None
    assert jitter.past == (), (jitter.share, jitter.past)
    assert _worst_alignment(audits, truths) < ALIGNMENT_MAX_SHARE


def test_a_tracked_insert_on_a_smooth_drift_draws_no_jitter():
    truths = [_truth(n) for n in range(COUNT)]
    audits = _composed(truths, tracked=True, noise=1.0)
    jitter = insert_checks.quad_jitter(audits)
    assert jitter is not None
    assert jitter.past == (), (jitter.share, jitter.past)
    assert _worst_alignment(audits, truths) < ALIGNMENT_MAX_SHARE


def test_a_tracked_insert_on_a_real_jump_still_steps_past_the_gate():
    truths = [_truth(n, jump_at=18, jump=6.0) for n in range(COUNT)]
    audits = _composed(truths, tracked=True)
    jitter = insert_checks.quad_jitter(audits)
    assert jitter is not None and jitter.share > insert_checks.JITTER_MAX_SHARE
    assert any(abs(frame - 18) <= 1 for frame in jitter.past), jitter.past
