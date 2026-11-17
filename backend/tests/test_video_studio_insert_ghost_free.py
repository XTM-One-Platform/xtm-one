"""A still subject that moves off its plate position leaves no ghost, and a patchy subject is one body.

Observed on the cats film: the still zone's filled reference was a blur of the surface around it; when the
cat moved its head off its plate position the uncovered screen differed from the blur, read as a change in the entry
zone, and the insert was left undrawn there - a hard black patch beside the cat. And a cat read patchily over a dark UI
(motion blur) let the insert show through its fur. Synthetic: a dark screen, a blob touching its bottom edge on most
frames of the plate, then a frame where the blob has moved aside; a frame with the blob drawn as a sieve.
"""

from __future__ import annotations

import numpy as np
from PIL import Image, ImageDraw, ImageFilter
from scipy import ndimage

from app.services.video_studio import screen_insert

W, H = 640, 360
QUAD = np.array([(120, 40), (520, 40), (520, 260), (120, 260)], dtype=float)


def _frame(seed: int, *, head_x: float = 300.0, sieve: bool = False) -> np.ndarray:
    rng = np.random.default_rng(seed)
    picture = Image.fromarray((rng.random((H, W, 3)) * 20 + 90).astype(np.uint8))
    draw = ImageDraw.Draw(picture)
    draw.polygon([tuple(p) for p in screen_insert._grown_px(QUAD, 6.0)], fill=(24, 24, 28))
    draw.polygon([tuple(p) for p in QUAD], fill=(6, 8, 12))
    draw.ellipse((head_x - 10, 250, head_x + 50, 330), fill=(180, 120, 60))
    draw.ellipse((head_x, 205, head_x + 40, 262), fill=(190, 130, 70))
    frame = np.asarray(picture.filter(ImageFilter.GaussianBlur(0.8))).astype(np.float32)
    if sieve:
        # The head's interior pocked with surface-coloured holes, as motion blur over a dark UI reads.
        holes = np.random.default_rng(seed + 7).random((H, W)) < 0.25
        ys, xs = np.mgrid[0:H, 0:W]
        inside_head = ((xs - head_x - 20) ** 2 / 20**2 + (ys - 233) ** 2 / 28**2) < 0.6
        frame[holes & inside_head] = (6, 8, 12)
    frame += np.random.default_rng(seed + 1000).normal(0.0, 2.0, frame.shape).astype(np.float32)
    return np.clip(frame, 0, 255).astype(np.uint8)


def _head_truth(frame: np.ndarray, grid: tuple[int, int]) -> np.ndarray:
    rim = screen_insert._rim_mask(grid)
    plane = screen_insert.rectified(frame, QUAD, grid).astype(np.float32) @ np.array([0.299, 0.587, 0.114])
    head = (plane > 60) & ~rim
    return ndimage.binary_fill_holes(ndimage.binary_closing(head, iterations=3)) & ~rim


def _plate() -> screen_insert.Surface:
    frames = [(_frame(seed, head_x=300.0 + sway), QUAD) for seed, sway in enumerate((0.0, 2.0, -1.0, 3.0, 0.5, -2.0))]
    frames.append((_frame(9, head_x=420.0), QUAD))  # one plate frame where the head has moved aside
    return screen_insert.Surface.of_plate(frames)


def test_no_ghost_where_the_still_subject_moved_off() -> None:
    surface = _plate()
    rim = screen_insert._rim_mask(surface.grid)
    assert surface.still is not None and surface.still.any()
    moved = _frame(30, head_x=420.0)
    front = surface.occluded(moved, QUAD) & ~rim
    truth = _head_truth(moved, surface.grid)
    # Nothing in front where the head stood on the plate and stands no more.
    ghost = front & surface.still & ~truth
    assert ghost.sum() <= 0.05 * surface.still.sum(), (ghost.sum(), surface.still.sum())
    # The head is still read where it is now.
    assert float((front & truth).sum()) / float(truth.sum()) > 0.85


def test_a_patchy_subject_is_one_body() -> None:
    surface = _plate()
    rim = screen_insert._rim_mask(surface.grid)
    patchy = _frame(31, head_x=300.0, sieve=True)
    front = surface.occluded(patchy, QUAD) & ~rim
    truth = _head_truth(patchy, surface.grid)
    holes = ndimage.binary_fill_holes(front) & ~front
    assert holes.sum() == 0
    assert float((front & truth).sum()) / float(truth.sum()) > 0.9


def test_surface_grid_and_feather() -> None:
    assert screen_insert.SURFACE_SIDE >= 512
    assert screen_insert.OCCLUSION_FEATHER <= 1.0


def test_an_unkeyed_insert_is_drawn_at_nothing_past_the_subject_s_outermost_pixel() -> None:
    # At 1080p the occlusion's feather is one pixel, and so is the edge ring it leaves soft: a ring twice as deep drew
    # the insert at a few percent over the subject's next pixel in, seen through its edge.
    size = (3 * W, 3 * H)
    quad_px = 3 * QUAD

    def at_1080p(frame: np.ndarray) -> np.ndarray:
        return np.asarray(Image.fromarray(frame).resize(size, Image.Resampling.BILINEAR))

    sways = (0.0, 2.0, -1.0, 3.0, 0.5, -2.0)
    surface = screen_insert.Surface.of_plate([(at_1080p(_frame(seed, head_x=300.0 + sway)), quad_px) for seed, sway in enumerate(sways)])
    footage = at_1080p(_frame(40))
    insert = screen_insert.InsertSource(
        "p.png",
        "image",
        tuple((float(x) / size[0], float(y) / size[1]) for x, y in quad_px),
        footage=screen_insert.Footage("f", "video", 30.0, "clip/demo"),
    )
    layer = screen_insert.compose(insert, Image.new("RGB", (1200, 660), (200, 205, 215)), size, 0, footage=footage, surface=surface)
    assert layer is not None
    box = layer.warped.box
    front = surface.front(footage, quad_px, box, screen_insert.OCCLUSION_FEATHER).foreground
    alpha = np.asarray(layer.warped.image)[..., 3]
    assert alpha.shape == front.shape
    within = ndimage.binary_erosion(screen_insert._coverage(quad_px, box) > 0.5, iterations=6)
    assert (front & within).sum() > 1000
    assert alpha[ndimage.binary_erosion(front & within, iterations=1)].max() == 0
