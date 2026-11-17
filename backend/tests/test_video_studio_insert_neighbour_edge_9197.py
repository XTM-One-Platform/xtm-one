"""A keyed insert's layer never draws over the quad of another insert of its scene.

Synthetic frames only. The case: an AI shot with green monitors side by side, each carrying a keyed ``fit: contain``
insert, a bezel narrower than the layer's margin (``KEY_MARGIN``) between them. Insert B's margin and its fade reached
into insert A's quad: the fade carried the footage's key there (a 1-2 px green line at A's right edge on every frame)
and the opaque part repainted A's last pixels in B's bezel colour. Each audit reads its own layer over the footage, so
every gate read 0 and no key, feather or despill setting moved it.

* composited in order, the two inserts leave no key-hued column at their shared edge and A's content intact;
* B's audit reports what its layer would have drawn over A (``FrameAudit.others_cut``);
* a quad of the same surface (two inserts back to back on one screen) is never cut out.
"""

from __future__ import annotations

import numpy as np
from PIL import Image, ImageDraw

from app.services.video_studio import screen_insert, screens
from app.services.video_studio.screen_insert import Footage, InsertSource, KeySpec

W, H = 1920, 1080
GREEN = (0, 252, 0)
DARK = (24, 26, 32)
A = ((500 / W, 300 / H), (900 / W, 300 / H), (900 / W, 560 / H), (500 / W, 560 / H))
B = ((930 / W, 300 / H), (1330 / W, 300 / H), (1330 / W, 560 / H), (930 / W, 560 / H))
KEY = screens.Key("green", GREEN)


def _footage() -> np.ndarray:
    rng = np.random.default_rng(3)
    picture = Image.fromarray((rng.random((H, W, 3)) * 10 + 20).astype(np.uint8))
    draw = ImageDraw.Draw(picture)
    draw.rectangle((492, 292, 1338, 568), fill=(18, 18, 20))
    draw.rectangle((500, 300, 899, 559), fill=GREEN)
    draw.rectangle((930, 300, 1329, 559), fill=GREEN)
    # A 1 px column of the key between A's edge and the bezel.
    draw.rectangle((901, 300, 901, 559), fill=GREEN)
    return np.asarray(picture)


def _ui(width: int, height: int) -> Image.Image:
    picture = Image.new("RGB", (width, height), DARK)
    draw = ImageDraw.Draw(picture)
    for row in range(40, height - 40, 36):
        draw.rectangle((60, row, width - 200, row + 12), fill=(190, 196, 206))
    return picture


def _composite(first, quad, others, onto=None):
    insert = InsertSource(
        "ui.png",
        "image",
        quad,
        fit="contain",
        light=0.1,
        key=KeySpec("green", 0.35, 2.0),
        footage=Footage("f", "video", 24.0, "clip/s1"),
        box=tuple(screen_insert.grown_box(quad, W / H)),
        label="Scene s1: its screen insert",
        others=others,
    )
    keyed = screen_insert.keyed_on(insert, first)
    layer = screen_insert.compose(
        keyed,
        _ui(1600, 900),
        (W, H),
        96,
        footage=first,
        surface=screen_insert.Surface.of(first, [(x * W, y * H) for x, y in quad]),
        fill=screen_insert.static_fill(keyed, first, (W, H)),
        state=screen_insert.Matting(),
        window_frame=96,
    )
    x0, y0, x1, y1 = layer.warped.box
    image = np.asarray(layer.warped.image, dtype=np.float32)
    alpha = image[..., 3:4] / 255.0
    frame = (first if onto is None else onto).astype(np.float32)
    frame[y0:y1, x0:x1] = image[..., :3] * alpha + frame[y0:y1, x0:x1] * (1.0 - alpha)
    return frame, layer.audit


def _key_columns(frame: np.ndarray) -> dict[int, int]:
    hsv = screen_insert._hsv(frame[300:560, 870:960])
    keyed = screen_insert._hue_outliers(hsv, KEY, complement=False, saturation=screen_insert.RESIDUAL_MIN_SATURATION)
    return {870 + i: int(n) for i, n in enumerate(keyed.sum(axis=0)) if n}


def test_two_adjacent_keyed_inserts_leave_no_key_line_at_their_shared_edge():
    first = _footage()
    frame, audit_a = _composite(first, A, (B,))
    frame, audit_b = _composite(first, B, (A,), onto=frame)
    assert _key_columns(frame) == {}
    # A's insert reaches its own edge: B's layer drew neither the footage nor its bezel colour over it.
    assert np.abs(frame[400, 880:899] - np.array(DARK, dtype=np.float32)).max() <= 2.0
    assert audit_b.others_cut and audit_b.others_cut > 0.0
    assert not audit_a.keyed_residue and not audit_b.keyed_residue
    assert audit_b.as_dict()["others_cut"] == audit_b.others_cut


def test_without_others_the_layer_still_reaches_the_neighbour():
    """The defect as it stood: with no ``others`` the layer of B draws over A's edge."""
    first = _footage()
    frame, _audit = _composite(first, A, ())
    frame, audit_b = _composite(first, B, (), onto=frame)
    assert _key_columns(frame)
    assert audit_b.others_cut is None


def test_a_quad_of_the_same_surface_is_never_cut():
    box = (400, 250, 1000, 600)
    inside = screen_insert._coverage(np.array([(x * W, y * H) for x, y in A]), box) > 0.5
    same = ((510 / W, 310 / H), (890 / W, 310 / H), (890 / W, 550 / H), (510 / W, 550 / H))
    assert screen_insert.others_cut((same,), inside, box, (W, H)) is None
    assert screen_insert.others_cut((), inside, box, (W, H)) is None
    cut = screen_insert.others_cut((B,), inside, box, (W, H))
    assert cut is not None and not (cut & inside).any()
    assert screen_insert.DRAWING_VERSION >= 21
