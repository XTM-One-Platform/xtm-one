"""Real screens composited on AI footage, checked pixel by pixel.

Five defects of a 4K render, each proven on synthetic footage without a
provider:

* a keyed insert that is not tracked keys nothing frame by frame - its bezel
  is locked and its quad filled once (``screen_insert.static_fill``), so a
  surface whose green drifts with compression draws the same pixels on every
  frame, and what enters the quad still stays in front; a tracked one keys
  through the median and a matte whose edge toggles back from one
  frame to the next (``FrameAudit.edge_hold``) is ``insert_flicker``, while
  a person crossing it holds;
* the glow is one tint of the insert's colour, kept to the bezel (proven in
  ``test_video_studio_insert_key_colour``);
* a replaced screen is carried to its bezel (``screens.bezel_corners``), the
  key left around it is ``key spill``, a screenshot is fitted by its content
  box and letterboxed in its own background (``pictures.content_box``,
  ``pictures.letterboxed``), and a ``cover`` fit that would cut a sidebar is
  refused before anything is paid;
* a source with too few pixels for its quad at the render's size is an
  ``insert_soft`` finding naming the source and what it needs
  (``screen_insert.density``, ``insert_checks.soft`` / ``soft_before`` /
  ``soft_screens``), and a recording is made at a pixel ratio of 2 by default;
* the findings block the render (``render_checks.PICTURE_KINDS``), and the
  scenes compositing a screen get check frames at the render's own size.
"""

from __future__ import annotations

import asyncio
import dataclasses
from types import SimpleNamespace

import numpy as np
import pytest
from PIL import Image, ImageDraw

from app.services.video_studio import encoder, insert_checks, pictures, render, render_checks, screen_insert, screens
from app.services.video_studio.spec import validate_take_script

GREEN = screens.Key("green", (0, 177, 64))
W, H = 640, 360
QUAD = ((0.3, 0.2), (0.7, 0.22), (0.69, 0.7), (0.31, 0.68))
QUAD_PX = [(x * W, y * H) for x, y in QUAD]


# ── Pictures: the content box, the edge colours, the letterbox ────────────


def _ui_screenshot(width: int = 800, height: int = 500, margin: int = 20) -> Image.Image:
    """A product UI: a dark sidebar on the left, a light content area, a flat margin of the UI's own background around."""
    picture = Image.new("RGB", (width, height), (245, 246, 248))
    draw = ImageDraw.Draw(picture)
    draw.rectangle((margin, margin, margin + 160, height - margin), fill=(24, 28, 40))
    for row in range(6):
        y = margin + 40 + row * 60
        draw.rectangle((margin + 200, y, width - margin - 30, y + 30), fill=(80 + row * 20, 120, 200))
    return picture


def test_the_content_box_is_the_picture_less_its_flat_margins():
    picture = _ui_screenshot()
    x0, y0, x1, y1 = pictures.content_box(picture)
    # The flat margins of the UI's background go (20 px left, top and bottom, 50 px on the right: the rows stop at
    # 750), to the pixel within the thumbnail's rounding.
    assert abs(x0 - 20) <= 2 and abs(y0 - 20) <= 2 and abs(x1 - 751) <= 2 and abs(y1 - 480) <= 2
    # A flat picture has no content: nothing a cover crop could take off it, and it is fitted whole.
    flat = Image.new("RGB", (100, 60), (30, 30, 30))
    assert pictures.content_box(flat) is None and pictures.cover_cuts(flat.size, None, 400, 400) == 0.0
    assert screens.content_fitted(flat, 400, 400, "contain")[1] == (0, 20, 100, 80)
    assert pictures.content_box(Image.new("RGBA", (100, 60), (0, 0, 0, 0))) is None
    # A margin is never trimmed past CONTENT_MAX_TRIM: a mark alone in a wide field keeps its field.
    sparse = Image.new("RGB", (400, 100), (255, 255, 255))
    ImageDraw.Draw(sparse).rectangle((300, 40, 320, 60), fill=(0, 0, 0))
    x0, y0, x1, y1 = pictures.content_box(sparse)
    assert x0 <= 400 * pictures.CONTENT_MAX_TRIM and x1 >= 320


def test_a_letterbox_continues_the_edge_it_touches_never_a_black_card():
    picture = _ui_screenshot()
    colours = pictures.edge_colours(picture)
    assert colours["top"] == (245, 246, 248) and colours["left"] == (245, 246, 248)
    # A 16:10 picture on a 4:3 screen: bands above and below, in the top and bottom edge colours.
    boxed = pictures.letterboxed(picture, 400, 300)
    assert boxed.width == 800 and boxed.height == 600
    assert boxed.getpixel((400, 5))[:3] == (245, 246, 248) and boxed.getpixel((400, 595))[:3] == (245, 246, 248)
    assert boxed.getpixel((400, 300))[:3] == picture.getpixel((400, 250))
    # A dark UI gets dark bands: each side its own edge.
    dark = Image.new("RGB", (300, 300), (18, 18, 22))
    ImageDraw.Draw(dark).rectangle((0, 290, 300, 300), fill=(90, 20, 20))
    wide = pictures.letterboxed(dark, 600, 300)
    assert wide.getpixel((5, 150))[:3] == (18, 18, 22) and wide.getpixel((595, 150))[:3] == (18, 18, 22)
    tall = pictures.letterboxed(dark, 300, 600)
    assert tall.getpixel((150, 5))[:3] == (18, 18, 22) and tall.getpixel((150, 595))[:3] == (90, 20, 20)


def test_a_cover_fit_cuts_margins_for_free_and_content_at_a_price():
    picture = _ui_screenshot()
    box = pictures.content_box(picture)
    # The screen of the picture's own proportions: nothing of the content is cut.
    assert pictures.cover_cuts(picture.size, box, 800, 500) == 0.0
    # A 4:3 screen cuts the sides (67 px each): about a tenth of the content, the sidebar's edge first.
    cut = pictures.cover_cuts(picture.size, box, 400, 300)
    assert 0.05 < cut < 0.2
    # A much wider screen cuts the top and bottom.
    assert pictures.cover_cuts(picture.size, box, 1000, 200) > 0.5


# ── The replaced screen: its bezel, its band, its matte, its content ──────


def _room(quad=QUAD_PX, *, key=GREEN, rim: int = 0, rim_shade: float = 0.6, sticker: tuple | None = None, size=(W, H)) -> np.ndarray:
    """A grey room with a flat key screen at *quad*; *rim* pixels of the key darkened by *rim_shade* around it."""
    picture = Image.new("RGB", size, (96, 92, 88))
    draw = ImageDraw.Draw(picture)
    corners = np.asarray(quad, dtype=float)
    if rim:
        grown = screens._grown(corners, float(rim))
        draw.polygon([tuple(p) for p in grown], fill=tuple(round(c * rim_shade) for c in key.rgb))
    draw.polygon([tuple(p) for p in corners], fill=key.rgb)
    if sticker:
        draw.rectangle(sticker, fill=key.rgb)
    return np.asarray(picture).copy()


def test_the_corners_are_carried_over_the_darker_key_along_the_bezel():
    frame = _room(rim=6)
    keyed = screens.key_distance(frame.astype(np.float32), GREEN) < screens.KEY_OUT
    pushed = screens.bezel_corners(keyed, np.asarray(QUAD_PX), reach=12.0)
    centre = np.asarray(QUAD_PX).mean(axis=0)
    away = np.linalg.norm(pushed - centre, axis=1) - np.linalg.norm(np.asarray(QUAD_PX) - centre, axis=1)
    assert (away > 4.0).all() and (away < 9.0).all(), away
    # Without a darker rim the corners stay (within the edge's own pixel).
    plain = screens.key_distance(_room().astype(np.float32), GREEN) < screens.KEY_OUT
    kept = screens.bezel_corners(plain, np.asarray(QUAD_PX), reach=12.0)
    assert np.abs(kept - np.asarray(QUAD_PX)).max() <= 1.5
    # A hand across an edge moves nothing: the push is the median along the edge.
    handed = _room(rim=6)
    ImageDraw.Draw(Image.fromarray(handed)).rectangle((200, 60, 240, 90), fill=(30, 26, 24))
    assert np.abs(screens.bezel_corners(keyed, np.asarray(QUAD_PX), reach=12.0) - pushed).max() < 0.01


def test_the_band_around_the_screen_reads_the_key_left_there_by_chroma():
    assert screens.residual_key_share(_room(), np.asarray(QUAD_PX), GREEN) < screens.MAX_SPILL_SHARE
    # A sticker of the key colour beside the screen, inside the 3 % band: spill.
    spilled = _room(sticker=(452, 120, 456, 200))
    assert screens.residual_key_share(spilled, np.asarray(QUAD_PX), GREEN) > screens.MAX_SPILL_SHARE
    # A darker shade of the key is still the key (chroma, never an RGB tolerance) ...
    dark = _room(sticker=(452, 120, 456, 200))
    dark[120:200, 452:456] = (0, 110, 40)
    assert screens.residual_key_share(dark, np.asarray(QUAD_PX), GREEN) > screens.MAX_SPILL_SHARE
    # ... and a grey or a blue sticker is not.
    blue = _room(sticker=(452, 120, 456, 200))
    blue[120:200, 452:456] = (20, 40, 200)
    assert screens.residual_key_share(blue, np.asarray(QUAD_PX), GREEN) < screens.MAX_SPILL_SHARE


def test_a_matte_pooled_over_five_frames_holds_while_a_blinking_one_is_flicker():
    corners = np.array([(10.0, 10.0), (210.0, 10.0), (210.0, 110.0), (10.0, 110.0)])
    base = np.ones((120, 220), dtype=np.float32)
    jitter = base.copy()
    jitter[:, 10:14] = 0.0  # the left edge read four pixels in, on alternate frames: compression noise
    smoother = screens.MatteSmoother()
    holds = []
    for index in range(12):
        # One frame in three reads the edge four pixels in: an isolated flip the median of five removes.
        matte, hold = smoother.smooth(jitter if index % 3 == 0 else base, corners)
        holds.append(hold)
    assert holds[:2] == [None, None] and min(v for v in holds[3:] if v is not None) >= screens.MIN_EDGE_HOLD
    # Once pooled, the drawn matte is the steady one: the edge no longer moves, on a flipped frame too.
    assert matte[60, 12] > 0.9 and smoother.smooth(jitter, corners)[0][60, 12] > 0.9
    # Inside the quad the matte comes back whole, its own border pixels included: nothing fades at the edge.
    steady = screens.MatteSmoother()
    for _ in range(4):
        held, _hold = steady.smooth(base, corners)
    assert held[10:110, 10:210].min() > 0.98
    # An edge read in and out on every other frame is what a median cannot settle (it alternates with it): the drawn
    # matte toggles back at its edge, and that is the flicker.
    blinking = screens.MatteSmoother()
    blink = base.copy()
    blink[:, 10:22] = 0.0
    seen = [blinking.smooth(blink if index % 2 else base, corners)[1] for index in range(12)]
    assert min(v for v in seen if v is not None) < screens.MIN_EDGE_HOLD
    # A person crossing the surface changes a new band of pixels on every frame and never the same ones back: the
    # matte holds whole, however fast they walk.
    crossing = screens.MatteSmoother()
    walked = []
    for index in range(16):
        person = base.copy()
        person[20:110, 10 + 12 * index : 46 + 12 * index] = 0.0
        walked.append(crossing.smooth(person, corners)[1])
    assert min(v for v in walked if v is not None) >= screens.MIN_EDGE_HOLD


def test_the_content_s_corners_are_taken_through_the_drawing_homography():
    corners = np.asarray(QUAD_PX)
    assert screens.content_overflow((10, 10, 390, 290), (400, 300), corners) == 0.0
    # A content box past the picture's own edges (a cover crop cut it) stands past the bezel.
    assert screens.content_overflow((-40, 10, 440, 290), (400, 300), corners) > 10.0


def test_the_measure_refuses_spill_flicker_and_cropped_content_by_name():
    measure = screens.Measure()
    measure.frame(0, spill=0.0, hold=None, overflow=0.0)
    measure.frame(1, spill=0.004, hold=0.995, overflow=0.0)
    with pytest.raises(screens.KeySpillError, match=r"key spill: 0\.40% of the band around the screen is still green at t=0\.04 s"):
        measure.judge(GREEN, 24.0)
    measure = screens.Measure()
    measure.frame(0, spill=0.0, hold=None, overflow=0.0)
    measure.frame(3, spill=0.0, hold=0.91, overflow=0.0)
    with pytest.raises(screens.KeyFlickerError, match=r"key flicker: 9\.0% of the screen's matte toggles back from one frame to the next at t=0\.12 s"):
        measure.judge(GREEN, 24.0)
    measure = screens.Measure()
    measure.frame(5, spill=0.0, hold=0.999, overflow=6.0)
    with pytest.raises(screens.ScreenContentCroppedError, match="screen content cropped: the screenshot's content stands 6 px past"):
        measure.judge(GREEN, 24.0)
    clean = screens.Measure()
    clean.frame(0, spill=0.001, hold=0.99, overflow=0.5)
    clean.judge(GREEN, 24.0)
    assert clean.record() == {"frames": 1, "spill": 0.001, "edge_hold": 0.99, "overflow": 0.5}
    assert issubclass(screens.KeySpillError, screens.ScreenLostError) and issubclass(screens.KeyFlickerError, screens.ScreenLostError)


def test_a_screenshot_is_fitted_by_its_content_box_and_cover_is_refused_when_it_cuts_it():
    picture = _ui_screenshot()
    shaped, content = screens.content_fitted(picture, 400, 300, "contain")
    # The margins went, the content is whole, the bands are the UI's own background.
    assert abs(shaped.width / shaped.height - 4 / 3) < 0.01
    bx0, by0, bx1, by1 = pictures.content_box(picture)
    x0, y0, x1, y1 = content
    assert (x1 - x0, y1 - y0) == (bx1 - bx0, by1 - by0) and x0 == 0 and shaped.width == bx1 - bx0
    assert shaped.getpixel((shaped.width // 2, 2))[:3] == (245, 246, 248)
    assert shaped.getpixel((x0 + 5, y0 + 5))[:3] == (24, 28, 40), "the sidebar is whole, at the content's top left"
    spec = screens.ScreenSpec("f", QUAD_PX, fit="cover")
    with pytest.raises(screens.ScreenError, match=r"screen\.fit: cover would cut \d+% of the screenshot's content .* at its left and right"):
        screens.check_content_fit(spec, picture)
    # A cover fit onto a screen of the content's proportions cuts margins only: allowed.
    screens.check_content_fit(screens.ScreenSpec("f", ((0, 0), (760, 0), (760, 460), (0, 460)), fit="cover"), picture)
    screens.check_content_fit(screens.ScreenSpec("f", QUAD_PX, fit="contain"), picture)
    assert screens.DEFAULT_FIT == "contain" and screens.ScreenSpec.from_params({"file_id": "f", "quad": QUAD_PX}).fit == "contain"


def _shot(tmp_path, frames: list[np.ndarray], fps: int = 24) -> str:
    names = []
    for index, frame in enumerate(frames):
        name = f"f{index:03d}.png"
        Image.fromarray(frame).save(tmp_path / name)
        names.append((index / fps, name))
    out = str(tmp_path / "shot.mp4")
    asyncio.run(
        encoder.frames_to_mezzanine(names, end_time=len(frames) / fps, out_path=out, fps=fps, crf=12, preset="ultrafast", workdir=str(tmp_path))
    )
    return out


async def _decoded(path: str) -> list[np.ndarray]:
    return [np.frombuffer(raw, dtype=np.uint8).reshape(H, W, 3) async for raw in encoder.iter_rgb_frames(path, width=W, height=H)]


@pytest.mark.skipif(not encoder.encoder_available(), reason="ffmpeg is not installed")
def test_a_replaced_screen_reaches_its_bezel_and_keeps_its_record(tmp_path):
    video = _shot(tmp_path, [_room(rim=5) for _ in range(12)])
    screenshot = _ui_screenshot()
    spec = screens.ScreenSpec("f", QUAD_PX, light=0.0, key=GREEN)
    out = str(tmp_path / "replaced.mp4")
    done = asyncio.run(screens.replace(video, out, screenshot, spec, start_size=(W, H)))
    assert done.found == done.frames and "carried to the bezel" in done.notes[0]
    assert done.record["picture_size"][0] > 0 and done.record["checks"]["frames"] == done.frames
    assert done.record["checks"]["spill"] <= screens.MAX_SPILL_SHARE
    frames = asyncio.run(_decoded(out))
    # No key colour left around the screen as drawn (the quad carried over the 5 px rim), nor in that darker rim
    # the model shaded along the bezel, which the screenshot now covers.
    bezel = screens._grown(np.asarray(QUAD_PX, dtype=float), 6.0)
    for frame in (frames[0], frames[6], frames[-1]):
        assert screens.residual_key_share(frame, bezel, GREEN) <= screens.MAX_SPILL_SHARE
        rim = screens.band_mask(frame.shape[:2], np.asarray(QUAD_PX), 4.0)
        keyed = screens.key_distance(frame.astype(np.float32), GREEN) < screens.KEY_TRACK
        assert float((keyed & rim).mean()) < 0.001, "the darker key along the bezel is still a line"


@pytest.mark.skipif(not encoder.encoder_available(), reason="ffmpeg is not installed")
def test_a_shot_with_the_key_left_beside_the_screen_fails_as_key_spill(tmp_path):
    video = _shot(tmp_path, [_room(sticker=(452, 110, 458, 200)) for _ in range(10)])
    spec = screens.ScreenSpec("f", QUAD_PX, light=0.0, key=GREEN)
    with pytest.raises(screens.KeySpillError, match="key spill"):
        asyncio.run(screens.replace(video, str(tmp_path / "out.mp4"), _ui_screenshot(), spec, start_size=(W, H)))


# ── Screen inserts: the fill, the pooled key, the audits ──────────────────


def _insert(**changes) -> screen_insert.InsertSource:
    fields = {
        "path": "p.png",
        "kind": "image",
        "quad": QUAD,
        "key": screen_insert.KeySpec("green"),
        "footage": screen_insert.Footage("f", "video", 24),
        "label": "Scene s1: its screen insert 0",
    }
    fields.update(changes)
    return screen_insert.InsertSource(**fields)


def _drifting_room(index: int, cat: bool = False) -> np.ndarray:
    """The green surface as a compressed shot shows it: its shade drifting from one frame to the next."""
    shade = 1.0 - 0.06 * (index % 3)
    key = screens.Key("green", tuple(round(c * shade) for c in GREEN.rgb))
    frame = _room(key=key)
    if cat:
        ImageDraw.Draw(picture := Image.fromarray(frame)).ellipse((300, 200, 420, 320), fill=(60, 40, 30))
        frame = np.asarray(picture).copy()
    return frame


def test_a_static_keyed_insert_fills_its_quad_once_and_never_blinks():
    first = _drifting_room(0)
    insert = screen_insert.keyed_on(_insert(fit="contain"), first)
    fill = screen_insert.static_fill(insert, first, (W, H))
    assert fill is not None and fill.box == screen_insert.bbox(screen_insert.grown_box(QUAD, W / H), W, H)
    # Locked to the bezel once: the quad drawn is within a pixel or two of the surface's own corners.
    assert np.abs(np.asarray(fill.found_px) - np.asarray(QUAD_PX)).max() < 3.0
    picture = Image.new("RGBA", (200, 300), (0, 0, 0, 0))
    ImageDraw.Draw(picture).rectangle((40, 60, 160, 240), fill=(20, 30, 90, 255))
    surface = screen_insert.Surface.of(first, QUAD_PX)
    state = screen_insert.Matting()
    drawn = []
    for index in range(6):
        layer = screen_insert.compose(
            insert, picture, (W, H), index, footage=_drifting_room(index), surface=surface, fill=fill, state=state, window_frame=index
        )
        drawn.append(np.asarray(layer.warped.image, dtype=int))
        assert layer.audit.residual_key == 0.0 and layer.audit.alignment == round(fill.alignment, 5)
    # The same pixels on every frame inside the quad, whatever the surface's shade did: the key is read once, never
    # per frame. (Its rim blends the bezel's own pixels in, and those follow the footage.)
    x0, y0, x1, y1 = layer.warped.box
    centre = np.asarray(fill.found_px).mean(axis=0)
    within = [tuple(centre + (np.asarray(corner) - centre) * 0.97 - (x0, y0)) for corner in fill.found_px]
    interior = Image.new("L", (x1 - x0, y1 - y0), 0)
    ImageDraw.Draw(interior).polygon(within, fill=255)
    inside = np.asarray(interior) == 255
    assert inside.sum() > 0.9 * 250 * 170, "the interior is most of the quad"
    for frame in drawn[1:]:
        assert np.abs(frame - drawn[0])[inside].max() <= 1, "the filled quad changes from frame to frame"
    # Keyed frame by frame instead, the surface's drift would show over the whole quad: the fill is what holds it.
    keying = screen_insert.Matting()
    keyed = [
        np.asarray(screen_insert.compose(insert, picture, (W, H), i, footage=_drifting_room(i), state=keying, window_frame=i).warped.image, dtype=int)
        for i in range(3)
    ]
    moved = np.abs(keyed[2] - keyed[0])[inside].max(axis=1)
    assert moved.max() > 4 and (moved > 2).mean() > 0.5, "a key read per frame follows the surface's drift"
    # The per-frame key (a tracked insert's) is the path whose audit says how much of the matte holds from the frame
    # before - read once two frames came before it.
    state = screen_insert.Matting()
    assert screen_insert.compose(insert, picture, (W, H), 1, footage=_drifting_room(1), state=state, window_frame=1).audit.edge_hold is None
    assert screen_insert.compose(insert, picture, (W, H), 2, footage=_drifting_room(2), state=state, window_frame=2).audit.edge_hold is None
    assert screen_insert.compose(insert, picture, (W, H), 3, footage=_drifting_room(3), state=state, window_frame=3).audit.edge_hold is not None


def test_what_enters_a_filled_quad_through_its_rim_still_stays_in_front():
    first = _drifting_room(0)
    insert = screen_insert.keyed_on(_insert(fit="cover"), first)
    fill = screen_insert.static_fill(insert, first, (W, H))
    surface = screen_insert.Surface.of(first, QUAD_PX)
    picture = Image.new("RGB", (400, 300), (20, 30, 90))
    state = screen_insert.Matting()
    layer = None
    for index in range(8):
        layer = screen_insert.compose(
            insert,
            picture,
            (W, H),
            index,
            footage=_drifting_room(index, cat=index >= 2),
            surface=surface,
            fill=fill,
            state=state,
            window_frame=index,
        )
    x0, y0, _x1, _y1 = layer.warped.box
    pixels = np.asarray(layer.warped.image, dtype=int)
    # Where the cat stands (it rises into the quad from below), the frame's own pixels - its brown -, not the
    # insert, not the fill; above it the insert.
    assert np.abs(pixels[240 - y0, 360 - x0][:3] - (60, 40, 30)).max() < 30, pixels[240 - y0, 360 - x0]
    assert np.abs(pixels[110 - y0, 250 - x0][:3] - (20, 30, 90)).max() < 10
    assert layer.audit.covered is not None and not layer.audit.covers_foreground


def test_how_much_of_a_matte_holds_is_read_on_three_frames_in_a_row_and_counts_what_toggles_back():
    state = screen_insert.Matting()
    steady = np.ones((40, 60), dtype=np.float32)
    assert state.hold(3, steady) is None
    assert state.hold(4, steady) is None, "two frames must come before"
    assert state.hold(5, steady) == 1.0
    jumped = steady.copy()
    jumped[:, :6] = 0.0  # a tenth of the matte gone ...
    assert state.hold(6, jumped) == 1.0, "a change is a move until it comes back"
    assert state.hold(7, steady) == 0.9, "... and back the frame after: the key blinked at its edge"
    assert state.hold(8, jumped) == 0.9
    assert state.hold(12, steady) is None, "a frame that does not follow the last two is not compared"
    assert state.hold(None, steady) is None
    # The matte of a frame drawn whole starts the run over.
    assert state.hold(13, steady) is None and state.hold(14, steady) is None and state.hold(15, steady) == 1.0


def test_a_cover_fit_that_cuts_the_source_s_content_is_audited_per_frame():
    source = _ui_screenshot()
    insert = _insert(key=None, footage=None, fit="cover")
    layer = screen_insert.compose(insert, source, (W, H))
    assert layer.audit.cropped is not None and layer.audit.crops
    whole = screen_insert.compose(dataclasses.replace(insert, fit="contain"), source, (W, H))
    assert whole.audit.cropped is None and not whole.audit.crops


def test_the_source_s_pixels_per_quad_pixel_name_what_a_soft_insert_needs():
    # A 1280 px recording on a 1800 px quad at 2160p: soft; the same quad at 1080p (900 px): sharp enough.
    soft = screen_insert.density((1280, 800), 1800, 1125, "cover")
    assert soft is not None and soft.soft and abs(soft.value - 1280 / 1800) < 0.01 and soft.needed == (1800, 1125)
    sharp = screen_insert.density((1280, 800), 900, 562, "cover")
    assert sharp is not None and not sharp.soft and sharp.value > 1.4
    # Contain scales by the smaller ratio: a tall picture in a wide quad is judged by its height.
    contained = screen_insert.density((1000, 2000), 1400, 1000, "contain")
    assert contained is not None and abs(contained.value - 2.0) < 0.01
    assert screen_insert.MIN_DENSITY == 0.8 and screen_insert.density((0, 0), 100, 100) is None


def test_soft_inserts_are_findings_that_name_the_source_and_block_the_render():
    placed = {
        "insert/s1.0": insert_checks.Placed(
            "insert/s1.0",
            "s1",
            "Scene s1: its screen insert 0",
            2.0,
            8.0,
            soft={"source": [1280, 800], "quad": [1400, 875], "value": 0.914, "needed": [1400, 875], "pixel_ratio": 1.5},
            source=("take", "take-1"),
        ),
        "insert/s2.0": insert_checks.Placed("insert/s2.0", "s2", "Scene s2: its screen insert 0", 9.0, 12.0, soft={"value": 1.4}),
    }
    placed["insert/s1.0"] = dataclasses.replace(placed["insert/s1.0"], soft={**placed["insert/s1.0"].soft, "value": 0.61})
    [finding] = insert_checks.soft(placed)
    assert (finding.kind, finding.severity, finding.scene_id, finding.at, finding.end) == (render_checks.INSERT_SOFT, render_checks.FAIL, "s1", 2.0, 8.0)
    assert finding.message.startswith("insert soft: Scene s1: its screen insert 0 draws take `take-1` (1280x800 px) on a quad 1400x875 px")
    assert "0.61 source pixels per pixel (at least 0.8)" in finding.message and "record it again with pixel_ratio: 1.5" in finding.message
    assert finding.data["source_kind"] == "take" and finding.data["needed"] == [1400, 875]
    # Outside a preview's window: not that preview's.
    assert insert_checks.soft(placed, offset=20.0, span=(20.0, 5.0)) == []
    for kind in (render_checks.INSERT_SOFT, render_checks.INSERT_FLICKER, render_checks.INSERT_CROPPED):
        assert kind in render_checks.PICTURE_KINDS
        assert render_checks.open_findings({"findings": [{"kind": kind, "severity": "fail"}]}, same_quality=False)


def test_a_cover_fit_cutting_content_and_a_matte_toggling_back_become_findings():
    placed = {"insert/s1.0": insert_checks.Placed("insert/s1.0", "s1", "Scene s1: its screen insert 0", 0.0, 4.0)}
    frames = {
        str(n): {
            "covered": 0.0,
            "logo_visible": None,
            "hue_rotation": 0.5,
            "black_lift": 0.001,
            "coverage": 1.0,
            "luma": 100.0,
            "source_luma": 100.0,
            "edge_hold": 0.93 if n == 20 else 0.999,
            "cropped": 0.18,
        }
        for n in range(0, 30)
    }
    audits = {
        "insert/s1.0": {
            "frames": frames,
            "evidence": {
                "flicker": [{"badness": 0.07, "frame": 20, "path": "ev/flicker.jpg"}],
                "cropped": [{"badness": 0.18, "frame": 3, "path": "ev/cropped.jpg"}],
            },
        }
    }
    found = {finding.kind: (finding, proof) for finding, proof in insert_checks.findings(audits, placed, fps=10)}
    # Over 2 % of the matte toggling back is flicker, beside the coverage and luma reversals.
    flicker, _proof = found[render_checks.INSERT_FLICKER]
    assert flicker.message.startswith("insert flickers at t=0:02.00: Scene s1: its screen insert 0's matte coverage or luma reverses")
    assert "its matte toggles back at its edge" in flicker.message and "over 2% of the matte toggling back" in flicker.message
    assert flicker.data["frames"] == 1
    assert insert_checks.flicker_frames({int(n): a for n, a in frames.items()}) == [20]
    cropped, proof = found[render_checks.INSERT_CROPPED]
    assert cropped.message.startswith("insert cropped at t=0:00.30: fit: cover cuts 18% of Scene s1: its screen insert 0's content")
    assert cropped.data["frames"] == 30 and proof == "ev/cropped.jpg"
    assert render_checks.INSERT_COLOUR_SHIFT not in found and render_checks.INSERT_FOREGROUND not in found
    steady = {n: {**a, "edge_hold": 0.999} for n, a in ((int(n), a) for n, a in frames.items())}
    assert insert_checks.flicker_frames(steady) == []


def test_a_replaced_screen_s_screenshot_is_judged_at_the_render_s_size():
    document = {
        "scenes": [
            {"id": "s1", "type": "clip", "start": 0.0, "end": 6.0, "src_w": 1280, "src_h": 720},
            {"id": "s2", "type": "clip", "start": 6.0, "end": 10.0, "src_w": 1280, "src_h": 720},
        ]
    }
    shots = {"s1": "clip-a", "s2": "clip-b"}
    screen = {"quad": [[200, 100], [1000, 110], [990, 600], [210, 590]], "start_size": [1280, 720], "picture_size": [800, 500]}
    records = {"clip-a": screen, "clip-b": {**screen, "picture_size": [2400, 1500]}}
    # At 2160p the 1280 px shot is drawn 3x: the 800 px screen is 2400 px wide, the 800 px screenshot a third of what it needs.
    [finding] = insert_checks.soft_screens(document, shots, records, (3840, 2160))
    assert (finding.kind, finding.scene_id, finding.at, finding.end) == (render_checks.INSERT_SOFT, "s1", 0.0, 6.0)
    assert "insert soft: scene s1's replaced screen draws its screenshot (800x500 px as fitted)" in finding.message and "give the shot `clip-a` a screenshot at least" in finding.message
    assert finding.data["clip_id"] == "clip-a" and finding.data["value"] < 0.4
    # At 720p the same screenshot is one to one: nothing to say.
    assert insert_checks.soft_screens(document, shots, records, (1280, 720)) == []
    # A preview's window keeps its own scenes only.
    assert insert_checks.soft_screens(document, shots, records, (3840, 2160), offset=6.0, span=(6.0, 4.0)) == []


def test_the_gate_judges_the_same_density_before_a_render_from_what_it_knows():
    from app.services.video_studio.spec import parse_video_spec

    spec = parse_video_spec(
        {
            "title": "Film",
            "freestyle": True,
            "scenes": [
                {
                    "id": "s1",
                    "type": "clip",
                    "clip_id": "00000000-0000-0000-0000-0000000000c1",
                    "overlays": [
                        {
                            "type": "screen_insert",
                            "source": {"take_id": "00000000-0000-0000-0000-0000000000a1"},
                            "quad": [[0.2, 0.2], [0.7, 0.2], [0.7, 0.7], [0.2, 0.7]],
                            "duration": 5,
                        }
                    ],
                }
            ],
        }
    )
    sources = {("take", "00000000-0000-0000-0000-0000000000a1"): (1280, 800)}
    footage = {"s1": (1920, 1080)}
    ratios = {"00000000-0000-0000-0000-0000000000a1": 1.0}
    # 1080p: the quad is 960 px wide, the recording 1280: sharp. 2160p: 1920 px wide, 0.67 per pixel: soft, record at 1.5x.
    assert insert_checks.soft_before(spec, (1920, 1080), sources=sources, footage=footage, pixel_ratios=ratios, screens_replaced={}) == []
    [(scene_id, why)] = insert_checks.soft_before(spec, (3840, 2160), sources=sources, footage=footage, pixel_ratios=ratios, screens_replaced={})
    assert scene_id == "s1" and "0.67 source pixels per pixel" in why and "record it again with pixel_ratio: 1.5" in why
    # A source of unknown size is not judged by the gate (the render judges it).
    assert insert_checks.soft_before(spec, (3840, 2160), sources={}, footage=footage, pixel_ratios={}, screens_replaced={}) == []


def test_the_host_of_a_quad_is_the_footage_as_the_frame_shows_it():
    # A clip covers the frame: a 16:9 shot in a 16:9 frame is the frame.
    assert render.host_size({"type": "clip", "src_w": 1280, "src_h": 720}, (3840, 2160)) == (3840.0, 2160.0)
    # A 4:3 shot in a 16:9 frame overflows it top and bottom.
    w, h = render.host_size({"type": "clip", "src_w": 1600, "src_h": 1200}, (1920, 1080))
    assert (w, round(h)) == (1920.0, 1440)
    # A recording is seen at its camera's closest zoom.
    w, h = render.host_size({"type": "recording", "css_w": 1600, "css_h": 900, "camera": [[0, 0.5, 0.5, 1.0], [2, 0.4, 0.4, 1.6]]}, (1920, 1080))
    assert (round(w), round(h)) == (3072, 1728)
    document = {"scenes": [{"id": "a", "inserts": [{"src": "insert/a.0"}]}, {"id": "b"}, {"id": "c"}]}
    spec = SimpleNamespace(scenes=[SimpleNamespace(id="c", type="clip", clip_id="clip-x", backdrop=None)])
    assert render.composited_scenes(document, spec, {"clip-x": {"quad": []}}) == [(0, "a"), (2, "c")]
    assert render.composited_scenes(document, spec, {}) == [(0, "a")]


def test_a_recording_is_made_at_two_device_pixels_per_css_pixel_by_default():
    script = validate_take_script({"steps": [{"action": "goto", "url": "https://example.test"}]})
    assert script["pixel_ratio"] == 2
    assert validate_take_script({"steps": [{"action": "goto", "url": "https://example.test"}], "pixel_ratio": 1.5})["pixel_ratio"] == 1.5
