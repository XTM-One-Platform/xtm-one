"""A pocket of the key left between a subject and the bezel's corner is keyed out, and a pocket left fails.

On synthetic footage, no provider, no browser: a green screen, a brown wedge (an ear) rising over its lower-left
corner from the first frame on, leaving a small green pocket between it and the bezel. The static keyed insert reads
what stands in front by its occlusion against a clean plate, whose mask is closed and hole-filled into one body: the
pocket fell inside it, the insert was not drawn there and the margin clamp turned it teal, while the residual key, the
halo and the rim all read green. What is proven:

* the insert is drawn inside the pocket (``screen_insert.key_pockets`` in ``compose``), and the residual key stays 0;
* the same pocket deliberately left undrawn is counted as residual key (``FrameAudit.residual_pocket``) and fails
  ``insert_residual_key`` with its frame named, whatever its share;
* a subject with no pocket of the key is untouched: nothing counted, nothing failed.
"""

from __future__ import annotations

import numpy as np
from PIL import Image, ImageDraw, ImageFilter

from app.services.video_studio import insert_checks, quality, screen_insert
from app.services.video_studio.render_checks import INSERT_RESIDUAL_KEY
from app.services.video_studio.screen_insert import Footage, InsertSource, KeySpec

W, H = 960, 540
GREEN = (32, 230, 40)
BROWN = (110, 75, 50)
BEZEL = (28, 28, 32)
SCREEN = (300, 130, 660, 400)
#: The ear: over the screen's left edge and its bottom edge, and over the bezel's corner.
EAR = [(200, 290), (300, 320), (380, 400), (390, 480), (200, 480)]
#: The gap in it at the corner: the screen's corner (about 31 x 41 px of green) and a sliver of bezel round it.
POCKET = [(291, 349), (291, 409), (343, 409)]
INSERT_COLOUR = (200, 40, 160)
KEY = "insert/s1.0"
LABEL = "Scene s1: its screen insert 0"
FPS = 24.0


def _room(seed: int, *, ear: bool = True, pocket: bool = True) -> np.ndarray:
    rng = np.random.default_rng(seed)
    picture = Image.fromarray((rng.random((H, W, 3)) * 20 + 95).astype(np.uint8))
    draw = ImageDraw.Draw(picture)
    x0, y0, x1, y1 = SCREEN
    draw.rectangle((x0 - 8, y0 - 8, x1 + 8, y1 + 8), fill=BEZEL)
    draw.rectangle((x0, y0, x1, y1), fill=GREEN)
    if ear:
        mask = Image.new("L", (W, H), 0)
        ImageDraw.Draw(mask).polygon(EAR, fill=255)
        if pocket:
            ImageDraw.Draw(mask).polygon(POCKET, fill=0)
        picture = Image.composite(Image.new("RGB", (W, H), BROWN), picture, mask.filter(ImageFilter.GaussianBlur(1.5)))
    picture = picture.filter(ImageFilter.GaussianBlur(0.8))
    frame = np.asarray(picture).astype(np.float32) + rng.normal(0.0, 2.0, (H, W, 3))
    return np.clip(frame, 0, 255).astype(np.uint8)


def _insert() -> InsertSource:
    x0, y0, x1, y1 = SCREEN
    quad = tuple((x / W, y / H) for x, y in ((x0, y0), (x1, y0), (x1, y1), (x0, y1)))
    return InsertSource(
        "p.png",
        "image",
        quad,
        key=KeySpec("green", 0.3, 3.0),
        footage=Footage("f", "video", FPS, "clip/s1"),
        box=tuple(screen_insert.grown_box(quad, W / H)),
        label=LABEL,
    )


def _static(frames: list[np.ndarray]) -> list:
    """Each frame of the static keyed insert as the frame server draws it: its fill and its surface read once, on a
    clean plate (the first frame), the subject in front on every later one."""
    insert = screen_insert.keyed_on(_insert(), frames[0])
    fill = screen_insert.static_fill(insert, frames[0], (W, H))
    assert fill is not None
    surface = screen_insert.Surface.of(frames[0], [(x * W, y * H) for x, y in insert.quad])
    state = screen_insert.Matting()
    picture = Image.new("RGB", (1600, 900), INSERT_COLOUR)
    layers = [
        screen_insert.compose(insert, picture, (W, H), n, footage=frame, surface=surface, fill=fill, state=state, window_frame=n)
        for n, frame in enumerate(frames)
    ]
    assert all(layer is not None for layer in layers)
    return layers


def _frames(**room) -> list[np.ndarray]:
    return [_room(0, ear=False)] + [_room(seed, **room) for seed in range(1, 7)]


def _at(layer, x: int, y: int) -> np.ndarray:
    x0, y0, _x1, _y1 = layer.warped.box
    return np.asarray(layer.warped.image, dtype=int)[y - y0, x - x0, :3]


def _findings(layers: list) -> list:
    audits = {"frames": {str(n): layer.audit.as_dict() for n, layer in enumerate(layers)}, "evidence": {}}
    placed = {KEY: insert_checks.Placed(KEY, "s1", LABEL, 0.0, len(layers) / FPS)}
    return [finding for finding, _proof in insert_checks.findings({KEY: audits}, placed, fps=FPS)]


def _near(pixel: np.ndarray, colour: tuple) -> bool:
    return bool(np.abs(pixel - np.array(colour)).max() <= 16)


def test_the_insert_is_drawn_in_a_pocket_of_the_key_beside_the_subject():
    layers = _static(_frames())
    for layer in layers[1:]:
        # Inside the pocket, at the screen's corner, deeper than the edge ring: the insert, never the key turned teal.
        assert _near(_at(layer, 306, 392), INSERT_COLOUR), _at(layer, 306, 392)
        assert _near(_at(layer, 312, 393), INSERT_COLOUR), _at(layer, 312, 393)
        # The subject stays in front.
        assert _near(_at(layer, 330, 380), BROWN), _at(layer, 330, 380)
        assert not layer.audit.residual_pocket
        assert layer.audit.residual_key == 0.0
    assert not [f for f in _findings(layers) if f.kind == INSERT_RESIDUAL_KEY]


def test_a_pocket_of_the_key_left_undrawn_fails_the_residual_key(monkeypatch):
    # The matte leaves the pocket in front (``key_out_pockets`` a no-op): the gate alone reads it.
    monkeypatch.setattr(screen_insert, "key_out_pockets", lambda visible, foreground, inside, matte, reach: (visible, foreground))
    layers = _static(_frames())
    for layer in layers[1:]:
        assert not _near(_at(layer, 306, 392), INSERT_COLOUR), _at(layer, 306, 392)
        assert layer.audit.residual_pocket and layer.audit.residual_pocket > 0.0
        assert layer.audit.keyed_residue
    [finding] = [f for f in _findings(layers) if f.kind == INSERT_RESIDUAL_KEY]
    assert finding.severity == "fail"
    assert finding.data["pocket"] >= 1
    assert isinstance(finding.data["frame"], int) and finding.data["frame"] >= 1
    assert "pocket of the key's colour" in finding.message


def test_a_subject_with_no_pocket_of_the_key_is_untouched():
    layers = _static(_frames(pocket=False))
    for layer in layers[1:]:
        assert layer.audit.residual_pocket is None
        assert _near(_at(layer, 330, 380), BROWN)
        assert _near(_at(layer, 500, 250), INSERT_COLOUR)
    assert not [f for f in _findings(layers) if f.kind == INSERT_RESIDUAL_KEY]


def test_the_pocket_counts_only_what_is_deeper_than_the_matte_s_edge():
    inside = np.ones((40, 40), dtype=bool)
    foreground = np.zeros((40, 40), dtype=bool)
    foreground[:, :20] = True
    keyed = np.zeros((40, 40), dtype=bool)
    keyed[:, 18:20] = True  # a fringe two pixels wide along the subject's edge
    assert not screen_insert.key_pockets(foreground, inside, keyed, 3).any()
    keyed[5:15, 5:15] = True  # a pocket ten pixels across
    pocket = screen_insert.key_pockets(foreground, inside, keyed, 3)
    assert pocket[10, 10] and not pocket[30, 19]


def test_the_versions_move_with_the_pocket():
    assert screen_insert.DRAWING_VERSION >= 19 and quality.QUALITY_REPORT_VERSION >= 44
