"""A keyed screen insert never leaves a seam of its key's colour across its quad, and the gate never files one.

Synthetic frames only. The case: a ``fit: contain`` insert keyed on a flat green monitor, its source 2088 x 1152 drawn
into a quad of about 499 x 270 at 1080p, read with a 2 px full-height column of the footage's green inside the quad
that the residual-key gate filed as "the source's own key hue".

* drawn at three source sizes, the quad shows no straight run of the key's colour down or across it;
* a straight thin run of the key's hue across the quad (``screen_insert.key_seams``) is counted as residual key
  whatever mask it falls in - the source's key hue grown around a green line of its own, what stands in front -, the
  source's own key hue drawn at that very place aside; a subject wider than a seam is none;
* a frame with a seam fails the residual-key gate whatever its share of the quad, said as the compositing's.
"""

from __future__ import annotations

import numpy as np
import pytest
from PIL import Image, ImageDraw

from app.services.video_studio import insert_checks, render_checks, screen_insert, screens
from app.services.video_studio.screen_insert import Footage, InsertSource, KeySpec

W, H = 1920, 1080
QUAD = ((0.370, 0.286), (0.630, 0.286), (0.630, 0.536), (0.370, 0.535))
GREEN = (0, 252, 0)


def _footage() -> np.ndarray:
    rng = np.random.default_rng(3)
    picture = Image.fromarray((rng.random((H, W, 3)) * 10 + 20).astype(np.uint8))
    draw = ImageDraw.Draw(picture)
    corners = [(x * W, y * H) for x, y in QUAD]
    draw.polygon([tuple(p) for p in screen_insert._grown_px(np.array(corners), 8.0)], fill=(18, 18, 20))
    draw.polygon(corners, fill=GREEN)
    return np.asarray(picture)


def _ui(width: int, height: int) -> Image.Image:
    picture = Image.new("RGB", (width, height), (24, 26, 32))
    draw = ImageDraw.Draw(picture)
    for row in range(40, height - 40, 36):
        draw.rectangle((60, row, width - 200, row + 12), fill=(190, 196, 206))
    return picture


def _composited(width: int, height: int) -> tuple[np.ndarray, screen_insert.FrameAudit]:
    first = _footage()
    insert = InsertSource(
        "ui.png",
        "image",
        QUAD,
        fit="contain",
        light=0.1,
        key=KeySpec("green", 0.35, 2.0),
        footage=Footage("f", "video", 24.0, "clip/s1"),
        box=tuple(screen_insert.grown_box(QUAD, W / H)),
        label="Scene s1: its screen insert 0",
    )
    keyed = screen_insert.keyed_on(insert, first)
    layer = screen_insert.compose(
        keyed,
        _ui(width, height),
        (W, H),
        96,
        footage=first,
        surface=screen_insert.Surface.of(first, [(x * W, y * H) for x, y in QUAD]),
        fill=screen_insert.static_fill(keyed, first, (W, H)),
        state=screen_insert.Matting(),
        window_frame=96,
    )
    x0, y0, x1, y1 = layer.warped.box
    image = np.asarray(layer.warped.image, dtype=np.float32)
    alpha = image[..., 3:4] / 255.0
    frame = first.astype(np.float32)
    frame[y0:y1, x0:x1] = image[..., :3] * alpha + frame[y0:y1, x0:x1] * (1.0 - alpha)
    return frame, layer.audit


@pytest.mark.parametrize("size", [(2088, 1152), (1920, 1080), (2560, 1440)], ids=["2088x1152", "1920x1080", "2560x1440"])
def test_a_keyed_contain_insert_leaves_no_run_of_its_key_down_or_across_its_quad(size):
    frame, audit = _composited(*size)
    qx0, qx1 = round(QUAD[0][0] * W) + 3, round(QUAD[1][0] * W) - 3
    qy0, qy1 = round(QUAD[0][1] * H) + 3, round(QUAD[3][1] * H) - 3
    cut = frame[qy0:qy1, qx0:qx1]
    r, g, b = cut[..., 0], cut[..., 1], cut[..., 2]
    green = (g > 80) & (g > r + 30) & (g > b + 30)
    assert int(green.sum(axis=0).max()) == 0 and int(green.sum(axis=1).max()) == 0
    assert audit.residual_key == 0.0 and audit.residual_seam is None and not audit.keyed_residue
    assert audit.band_share and audit.band_fill in screen_insert.FILLED_BANDS


# ── A seam counted whatever mask it falls in ──────────────────────────────


QW, QH = 240, 130
KEY = screens.Key("green", (0, 252, 0))


def _plate() -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """``(shown, composed, inside)``: a dark UI source drawn over a whole quad, the composed frame the same."""
    shown = np.full((QH, QW, 3), (24.0, 26.0, 32.0), dtype=np.float32)
    shown[20:24, 20:200] = (190.0, 196.0, 206.0)
    inside = np.ones((QH, QW), dtype=bool)
    inside[:, :4] = inside[:, -4:] = False
    inside[:4, :] = inside[-4:, :] = False
    return shown, shown.copy(), inside


def _residual(shown, composed, inside, *, foreground=None):
    masks = screen_insert.gate_masks(
        shown, np.ones(inside.shape), KEY, inside, contain_band=None, foreground=foreground, margin=None, scale=1.0, feather=2.0
    )
    hsv = screen_insert._hsv(composed)
    keyed = screen_insert._hue_outliers(hsv, KEY, complement=False, saturation=screen_insert.RESIDUAL_MIN_SATURATION)
    seams = screen_insert.key_seams(keyed & ~masks.source_own, inside, 1.0)
    quad = int(inside.sum())
    unseamed, _left = screen_insert._residual_key(hsv, KEY, masks.residual_zone(inside), quad)
    share, _left = screen_insert._residual_key(hsv, KEY, masks.residual_zone(inside) | seams, quad)
    return unseamed, share, seams


def test_a_seam_beside_the_source_s_own_green_line_is_counted_never_filed_as_its_hue():
    shown, composed, inside = _plate()
    # The source's own green line, one pixel wide, down its middle: its own key hue, grown by SOURCE_HUE_GROW_PX ...
    shown[:, 120] = composed[:, 120] = (40.0, 200.0, 60.0)
    # ... and a 2 px column of the footage's green beside it, inside that grown mask - which the gate used to excuse.
    composed[4:-4, 121:123] = (0.0, 140.0, 0.0)
    unseamed, share, seams = _residual(shown, composed, inside)
    assert unseamed == 0.0
    assert seams[:, 121:123][inside[:, 121:123]].all() and not seams[:, 120].any()
    assert share == pytest.approx(2 * (QH - 8) / inside.sum(), abs=1e-4)


def test_a_seam_under_what_stands_in_front_is_counted_too():
    shown, composed, inside = _plate()
    composed[4:-4, 60:62] = (0.0, 150.0, 0.0)
    front = np.zeros(inside.shape, dtype=bool)
    front[:, 55:70] = True
    unseamed, share, _seams = _residual(shown, composed, inside, foreground=front)
    assert unseamed == 0.0 and share > 0.0


def test_the_source_s_own_full_height_green_line_and_a_wide_subject_are_no_seams():
    shown, composed, inside = _plate()
    # A green rule the source itself draws down the quad: what it shows, never a seam.
    shown[:, 90:92] = composed[:, 90:92] = (30.0, 220.0, 50.0)
    # A green-lit subject 20 px wide standing the quad's height: wider than a seam.
    composed[4:-4, 150:170] = (10.0, 160.0, 20.0)
    _unseamed, _share, seams = _residual(shown, composed, inside)
    assert not seams.any()


def test_a_frame_with_a_seam_fails_the_residual_key_gate_whatever_its_share():
    key = "insert/s1.0"
    placed = {key: insert_checks.Placed(key, "s1", "Scene s1: its screen insert 0", 0.0, 260 / 24)}
    frames = {str(n): {"residual_key": 0.001, "residual_seam": 0.001} for n in range(260)}
    [(finding, _proof)] = insert_checks.findings({key: {"frames": frames, "evidence": {}}}, placed, fps=24)
    assert finding.kind == render_checks.INSERT_RESIDUAL_KEY and finding.severity == render_checks.FAIL
    assert "a straight run of the key's colour across its quad on 260 frames" in finding.message
    assert finding.data["seam"] == 260 and finding.data["action"]["kind"] == insert_checks.NOTHING
    assert screen_insert.FrameAudit(residual_key=0.001, residual_seam=0.001).keyed_residue
    assert not screen_insert.FrameAudit(residual_key=0.001).keyed_residue
    # Frames audited before the seam was read are never joined to frames audited after.
    assert screen_insert.DRAWING_VERSION >= 11
