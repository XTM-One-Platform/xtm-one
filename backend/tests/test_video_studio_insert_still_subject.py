"""A subject standing still in front of an unkeyed surface over the whole plate is read as in front (#NNNN).

Observed on the cats film: a cat sat on the desk under a dark screen for the whole shot was in the plate's median,
so the occluder read it as surface and the insert was drawn over its head on every frame, the gate green. Synthetic:
a dark screen, a bright blob touching its bottom edge on every frame (its top swaying a little), no key.
"""

from __future__ import annotations

import numpy as np
from PIL import Image, ImageDraw, ImageFilter

from app.services.video_studio import screen_insert

W, H = 640, 360
QUAD = np.array([(120, 40), (520, 40), (520, 260), (120, 260)], dtype=float)


def _frame(seed: int, *, sway: float = 0.0, subject: bool = True, ui: bool = False) -> np.ndarray:
    rng = np.random.default_rng(seed)
    picture = Image.fromarray((rng.random((H, W, 3)) * 20 + 90).astype(np.uint8))
    draw = ImageDraw.Draw(picture)
    draw.polygon([tuple(p) for p in screen_insert._grown_px(QUAD, 6.0)], fill=(24, 24, 28))
    draw.polygon([tuple(p) for p in QUAD], fill=(6, 8, 12))
    if ui:
        draw.rectangle((140, 60, 500, 240), fill=(40, 60, 90))
    if subject:
        # A cat's silhouette: a body on the desk below the screen, its head inside the quad, touching the bottom edge.
        draw.ellipse((290, 250, 350, 330), fill=(180, 120, 60))
        draw.ellipse((300 + sway, 205 + sway * 0.4, 340 + sway, 262 + sway * 0.4), fill=(190, 130, 70))
    frame = np.asarray(picture.filter(ImageFilter.GaussianBlur(0.8))).astype(np.float32)
    frame += np.random.default_rng(seed + 1000).normal(0.0, 2.0, frame.shape).astype(np.float32)
    return np.clip(frame, 0, 255).astype(np.uint8)


def _truth(frame: np.ndarray, grid: tuple[int, int]) -> np.ndarray:
    rim = screen_insert._rim_mask(grid)
    plane = screen_insert.rectified(frame, QUAD, grid).astype(np.float32) @ np.array([0.299, 0.587, 0.114])
    return (plane > 60) & ~rim


def test_still_subject_is_read_in_front_on_every_frame() -> None:
    plate = [(_frame(seed, sway=sway), QUAD) for seed, sway in enumerate((0.0, 2.0, -1.0, 3.0, 0.5, -2.0, 1.0))]
    surface = screen_insert.Surface.of_plate(plate)
    rim = screen_insert._rim_mask(surface.grid)
    assert surface.still is not None and surface.still.any()
    for seed, sway in ((20, 1.5), (21, -1.5), (22, 0.0)):
        frame = _frame(seed, sway=sway)
        front = surface.occluded(frame, QUAD) & ~rim
        truth = _truth(frame, surface.grid)
        assert truth.any()
        recall = float((front & truth).sum()) / float(truth.sum())
        assert recall > 0.85, recall


def test_still_subject_read_on_a_first_frame_alone() -> None:
    surface = screen_insert.Surface.of(_frame(5), QUAD)
    rim = screen_insert._rim_mask(surface.grid)
    front = surface.occluded(_frame(6, sway=1.0), QUAD) & ~rim
    truth = _truth(_frame(6, sway=1.0), surface.grid)
    assert float((front & truth).sum()) / float(truth.sum()) > 0.85


def test_a_plain_surface_holds_no_still_subject() -> None:
    surface = screen_insert.Surface.of(_frame(7, subject=False), QUAD)
    assert surface.still is None
    rim = screen_insert._rim_mask(surface.grid)
    assert not (surface.occluded(_frame(8, subject=False), QUAD) & ~rim).any()


def test_what_the_screen_shows_is_no_still_subject() -> None:
    # A UI the model drew on the screen, touching its edges and holding most of it, is the surface.
    surface = screen_insert.Surface.of(_frame(9, subject=False, ui=True), QUAD)
    assert surface.still is None
