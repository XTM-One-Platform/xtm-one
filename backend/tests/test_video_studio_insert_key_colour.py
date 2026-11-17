"""A named key keys on the colour its surface shows, and an insert whose colours shifted fails its scene.

A video model asked for a chroma-green screen paints its own green - about
(32, 255, 30) - never the (0, 177, 64) ``key.color: "green"`` names. Read
against the name, such a surface keys half-way and the despilled footage
blends under the insert: a navy picture came out olive. What is proven, on
synthetic footage:

* a named key is the median colour of the quad's pixels of its hue on the
  insert's first footage frame (``screens.sample_key``), logged; a ``#rrggbb``
  is keyed as written; a surface showing too little of the colour keeps the
  name's own, said;
* keyed so, a navy picture on a (32, 255, 30) surface is drawn navy;
* every frame drawn is audited for what the compositing did to its colours
  (``FrameAudit.hue_rotation`` on a source with a hue, ``cast`` on one
  with none, ``black_lift``), and the render's findings fail the scene with
  the worst frame (``insert_colour_shift``); the surface is
  painted flat inside the refined quad, so the name's own colour on that
  surface no longer blends the footage under the insert;
* the sampled colour travels with the insert to a scene range's worker.
"""

from __future__ import annotations

import dataclasses
import logging

import numpy as np
from PIL import Image, ImageDraw

from app.services.video_studio import insert_checks, render_checks, screen_insert, screens

W, H = 640, 360
QUAD = ((0.3, 0.2), (0.7, 0.22), (0.69, 0.7), (0.31, 0.68))
QUAD_PX = [(x * W, y * H) for x, y in QUAD]
#: What a video model paints when asked for a chroma-green screen.
MODEL_GREEN = (32, 255, 30)
NAVY = (20, 30, 90)


def _room(surface: tuple[int, int, int]) -> np.ndarray:
    rng = np.random.default_rng(3)
    picture = Image.fromarray((rng.random((H, W, 3)) * 40 + 90).astype(np.uint8))
    ImageDraw.Draw(picture).polygon(QUAD_PX, fill=surface)
    return np.asarray(picture)


def _insert(color: str = "green") -> screen_insert.InsertSource:
    return screen_insert.InsertSource(
        "p.png",
        "image",
        QUAD,
        key=screen_insert.KeySpec(color),
        footage=screen_insert.Footage("f", "image"),
        label="Scene cat-soc: its screen insert 0",
    )


def _drawn(insert: screen_insert.InsertSource, footage: np.ndarray) -> tuple[np.ndarray, screen_insert.Layer]:
    """The frame as the page shows it: the layer over the footage, and the layer."""
    layer = screen_insert.compose(insert, Image.new("RGB", (400, 300), NAVY), (W, H), footage=footage, evidence=True)
    x0, y0, x1, y1 = layer.warped.box
    pixels = np.asarray(layer.warped.image, dtype=np.float32)
    a = pixels[..., 3:4] / 255.0
    frame = footage.astype(np.float32).copy()
    frame[y0:y1, x0:x1] = pixels[..., :3] * a + frame[y0:y1, x0:x1] * (1 - a)
    return frame, layer


def _inside(frame: np.ndarray) -> np.ndarray:
    """The quad's pixels well inside its edges."""
    return frame[110:230, 220:420].reshape(-1, 3)


def test_a_named_key_is_the_colour_its_surface_shows(caplog):
    footage = _room(MODEL_GREEN)
    with caplog.at_level(logging.INFO, logger=screen_insert.__name__):
        keyed = screen_insert.keyed_on(_insert(), footage)
    assert keyed.key.rgb is not None and max(abs(a - b) for a, b in zip(keyed.key.rgb, MODEL_GREEN, strict=True)) <= 2
    assert keyed.key.key().rgb == keyed.key.rgb and keyed.key.color == "green"
    assert "Scene cat-soc: its screen insert 0 keys on #20ff1e: the green its surface shows on its first footage frame" in caplog.text


def test_a_navy_picture_on_a_model_green_surface_is_drawn_navy():
    footage = _room(MODEL_GREEN)
    frame, layer = _drawn(screen_insert.keyed_on(_insert(), footage), footage)
    assert np.abs(_inside(frame) - NAVY).max() <= 3, "the surface blends under the insert"
    assert layer.audit.hue_rotation is not None and layer.audit.hue_rotation < 2.0 and not layer.audit.colour_shifted
    assert abs(layer.audit.black_lift) < 0.01 and layer.audit.cast is None


def test_the_names_own_colour_on_that_surface_no_longer_tints_the_insert_the_surface_painted_flat():
    """Inside the refined surface the insert covers everything but the foreground - a half-way matte blends
    nothing under it any more, where it used to turn navy olive."""
    footage = _room(MODEL_GREEN)
    # (0, 177, 64) against (32, 255, 30): a matte of about 0.6 on the surface itself ...
    raw = screens.key_matte(np.full((2, 2, 3), MODEL_GREEN, dtype=np.float32), screens.Key("green", (0, 177, 64)), 0.3)
    assert 0.4 < float(raw.mean()) < 0.75
    # ... which is no foreground: the surface is painted flat and the insert drawn whole, its colours its own.
    frame, layer = _drawn(_insert(), footage)
    assert np.abs(_inside(frame) - NAVY).max() <= 3
    assert not layer.audit.colour_shifted and layer.audit.covered == 0.0
    assert layer.audit.residual_key == 0.0 and layer.evidence is None


def test_a_hex_key_is_keyed_as_written_and_too_little_of_a_colour_keeps_the_name(caplog):
    footage = _room(MODEL_GREEN)
    hexed = screen_insert.keyed_on(_insert("#00b140"), footage)
    # Keyed on the colour as written; the surface's spread around it widens the matte's tolerance.
    assert hexed.key.rgb is None and hexed.key.key().rgb == (0, 177, 64) and hexed.key.color == "#00b140"
    assert hexed.key.spread is not None and hexed.key.matte_tolerance() > hexed.key.tolerance
    grey = _room((128, 128, 128))
    with caplog.at_level(logging.WARNING, logger=screen_insert.__name__):
        named = screen_insert.keyed_on(_insert(), grey)
    assert named.key.rgb is None and named.key.key().rgb == (0, 177, 64)
    assert "keys on the named green itself (#00b140): its surface shows too little of that colour" in caplog.text
    # Magenta and blue are sampled by their own hue, never taken for green.
    assert screens.sample_key(_room((250, 20, 200)), QUAD_PX, screens.Key("green", (0, 177, 64))) is None
    sampled = screens.sample_key(_room((250, 20, 200)), QUAD_PX, screens.Key("magenta", (230, 0, 210)))
    assert sampled is not None and max(abs(a - b) for a, b in zip(sampled.rgb, (250, 20, 200), strict=True)) <= 2


def test_a_keyed_inserts_halo_falls_on_the_surface_it_covers_around_its_mark():
    """The keyed layer covers its surface whole: its glow is laid on that surface, never hidden under the layer."""
    footage = _room(MODEL_GREEN)
    mark = Image.new("RGBA", (400, 300), (0, 0, 0, 0))
    ImageDraw.Draw(mark).ellipse((150, 100, 250, 200), fill=(15, 188, 255, 255))
    lit = screen_insert.keyed_on(dataclasses.replace(_insert(), glow=0.8, fit="contain"), footage)
    dark = dataclasses.replace(lit, glow=0.0)
    with_glow = screen_insert.compose(lit, mark, (W, H), footage=footage)
    without = screen_insert.compose(dark, mark, (W, H), footage=footage)
    pixels = np.asarray(without.warped.image, dtype=int)
    ys, xs = np.nonzero(np.abs(pixels[..., :3] - (15, 188, 255)).max(axis=-1) < 30)
    centre, radius = (int(xs.mean()), int(ys.mean())), (ys.max() - ys.min()) // 2
    beside, far = (centre[0], centre[1] + radius + 4), (8, 8)

    def luma(layer, at):
        r, g, b, _a = np.asarray(layer.warped.image, dtype=int)[at[1], at[0]]
        return 0.299 * r + 0.587 * g + 0.114 * b

    # A tint of the mark's own colour at a low alpha (``GLOW_ALPHA``): a halo, never a wash of light.
    assert luma(with_glow, beside) - luma(without, beside) > 3, "no halo on the surface beside the mark"
    assert abs(luma(with_glow, far) - luma(without, far)) < 3, "the halo lights the whole quad"
    assert with_glow.glow is not None and without.glow is None
    bloom = np.asarray(with_glow.glow, dtype=int)
    lit = bloom[..., 3] > 8
    assert lit.any()
    # One colour - the mark's - wherever it glows, and nothing past the bezel (the quad grown by GLOW_REACH).
    assert np.abs(bloom[lit][:, :3] - (15, 188, 255)).max() <= 1
    gx0, gy0, gx1, gy1 = (v // screen_insert.GLOW_SCALE for v in (QUAD_PX[0][0], QUAD_PX[0][1], QUAD_PX[2][0], QUAD_PX[2][1]))
    reach = int(screen_insert.GLOW_REACH * (QUAD_PX[1][0] - QUAD_PX[0][0]) / screen_insert.GLOW_SCALE) + 2
    outside = np.ones(lit.shape, dtype=bool)
    outside[max(0, int(gy0) - reach) : int(gy1) + reach + 1, max(0, int(gx0) - reach) : int(gx1) + reach + 1] = False
    assert not (lit & outside).any(), "the glow blooms outside the screen"


def test_a_source_with_no_hue_is_judged_by_the_cast_it_takes():
    footage = _room(MODEL_GREEN)
    insert = screen_insert.keyed_on(_insert(), footage)
    layer = screen_insert.compose(insert, Image.new("RGB", (400, 300), (240, 240, 240)), (W, H), footage=footage)
    # A grey has no hue to turn: the chroma the compositing added is its cast - none here.
    assert layer.audit.hue_rotation is None and layer.audit.cast is not None and layer.audit.cast < 1.0
    assert not layer.audit.colour_shifted
    # No footage under it: nothing to blend, nothing judged.
    bare = screen_insert.compose(insert, Image.new("RGB", (400, 300), NAVY), (W, H)).audit
    assert bare.hue_rotation is None and bare.black_lift is None and bare.cast is None and bare.size is None


def test_frames_drawn_off_their_source_s_hue_fail_the_scene_with_the_worst_frame():
    placed = {"insert/cat-soc.0": insert_checks.Placed("insert/cat-soc.0", "cat-soc", "Scene cat-soc: its screen insert 0", 2.0, 6.0, keyed=True)}
    frames = {
        str(n): {"covered": 0.0, "logo_visible": None, "hue_rotation": 140.0 if n >= 70 else 1.0, "black_lift": 0.0}
        for n in range(60, 120)
    }
    audits = {
        "insert/cat-soc.0": {
            "frames": frames,
            "evidence": {"colour": [{"badness": 28.0, "frame": 90, "path": "segments/evidence/colour.jpg"}]},
        }
    }
    [(finding, proof)] = insert_checks.findings(audits, placed, fps=30)
    assert (finding.kind, finding.severity, finding.scene_id) == (render_checks.INSERT_COLOUR_SHIFT, render_checks.FAIL, "cat-soc")
    assert finding.message.startswith(
        "insert colour shift at t=0:03.00: Scene cat-soc: its screen insert 0 is drawn with its hue turned 140 degrees against its source"
    )
    # One action: what shows through it - no key colour or tolerance, which no fixture shows moving the hue.
    assert finding.data["action"]["kind"] == insert_checks.REPLACE_SOURCE and "tolerance" not in finding.message
    assert finding.data["frames"] == 50 and finding.data["hue_rotation"] == 140.0 and finding.data["worst_at"] == 3.0
    assert proof == "segments/evidence/colour.jpg"
    assert render_checks.INSERT_COLOUR_SHIFT in render_checks.PICTURE_KINDS


def test_the_sampled_colour_travels_with_the_insert_to_a_range_worker():
    keyed = screen_insert.keyed_on(_insert(), _room(MODEL_GREEN))
    # A scene range reads its inserts back from the render's bundle (``render_ranges``): JSON, a list for the tuple.
    published = {**dataclasses.asdict(keyed.key), "rgb": list(keyed.key.rgb)}
    read = screen_insert.KeySpec(**published)
    assert read == keyed.key and read.key().rgb == keyed.key.rgb
