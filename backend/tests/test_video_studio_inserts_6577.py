"""Screen inserts measured where they are drawn, at every quality, and found off pure green.

Render ac413cea of video b56997a2 (high, 1920x1080 at 60 fps, motion blur on) reported the OpenAEV insert "no bezel
found under the quad" and "its source missing from 480 of 480 encoded frames", XTM One and OpenCTI "not drawn on 462 /
385 frames the page draws it on", and infra_once lines on four frames - while its crops showed every insert drawn and
its scenes' previews (drawn at standard, no motion blur) read the bezel 0.05% off and every frame present. Under motion
blur the page asks a matted insert's frame once per address, and its first sub-frame - never audited - asked it: the
centre sub-frame, the one a frame is judged by, asked nothing and was never audited. Synthetic frames only - no
provider; the render through Chromium where it is installed:

* F42 a 1.9:1 quad: a 1600x842 window at ``target_render``;
* F43 a high render of a scene a standard preview read green: every frame audited as the preview audits it - the same
  verdicts, the bezel found -, its crops cut 0-1 of the decoded frame, on the screen, at any capture size;
* F44 a box read off its insert: ``insert_measurement_lost``, the gate red, naming the frame, the box and the size it
  was read at; an insert drawn by a render whose frames found none of the bezel its save and its preview found: the
  same block, naming the size each read it at - never its source missing from N of N frames;
* F45 an insert truly absent for 10 frames: ``insert_missing`` blocks, never infra_once; F46 one frame the page drew
  and the encoder dropped: the one harness defect left;
* F51 a ``#2fbf4a`` screen, and one painted a shaded yellow green under 0.45 of saturation: found, the quad within 0.5%,
  the hue said.
"""

from __future__ import annotations

import asyncio
import io
import os

import numpy as np
import pytest
from PIL import Image, ImageDraw

from app.services.video_studio import (
    compositor,
    encoder,
    insert_checks,
    motion_design,
    render_checks,
    screen_insert,
    screens,
    surface_refine,
)
from app.services.video_studio.spec import parse_video_spec
from tests.test_video_studio_insert_matte import GREEN, INSERT, QUAD, QUAD_PX, H, W, _room

# ── F42: a take recorded at the drawn screen's proportions ────────────────


def _wide_screen_spec() -> dict:
    """A 16:9 shot covering the frame, a TV on it whose quad is 1.9:1 at any render size."""
    top, bottom = 0.25, 0.25 + 0.5 * 1920 / 1080 / 1.9
    return {
        "title": "Booth",
        "freestyle": True,
        "scenes": [
            {
                "id": "s1",
                "type": "clip",
                "clip_id": "00000000-0000-0000-0000-00000000c642",
                "overlays": [
                    {
                        "type": "screen_insert",
                        "source": {"take_id": "00000000-0000-0000-0000-00000000a642"},
                        "quad": [[0.25, top], [0.75, top], [0.75, bottom], [0.25, bottom]],
                        "fit": "contain",
                        "duration": 6,
                    }
                ],
            }
        ],
    }


@pytest.mark.parametrize("size", [(1920, 1080), (2560, 1440), (3840, 2160)], ids=["1080p", "1440p", "2160p"])
def test_f42_a_quad_of_1_9_to_1_is_recorded_as_a_1600x842_window(size):
    spec = parse_video_spec(_wide_screen_spec())
    surfaces = insert_checks.surfaces_at(spec, size, {"s1": (1920, 1080)}, scene_id="s1")
    quad_w, quad_h = surfaces[0].quad
    assert quad_w / quad_h == pytest.approx(1.9, abs=0.002)
    window = insert_checks.quad_window(surfaces, (1600, 900))
    assert window == (1600, 842)
    recording = insert_checks.recording_for(surfaces, window)
    assert recording is not None and recording.viewport[1] * 1600 == recording.viewport[0] * 842
    # Drawn with fit: contain, its frames leave no band inside the quad - where a 16:9 window left one.
    from app.services.video_studio.screen_insert import contained_quad

    corners = np.array([(0.0, 0.0), (quad_w, 0.0), (quad_w, quad_h), (0.0, quad_h)])

    def banded(frame: tuple[int, int]) -> float:
        inner = contained_quad(frame, corners)
        return 1.0 - abs(screens.area(inner)) / abs(screens.area(corners)) if inner is not None else 0.0

    assert banded(recording.frame) < 0.002
    assert banded((1600 * 2, 900 * 2)) > 0.06


def test_f42_a_window_held_within_what_a_recording_takes():
    tall = [insert_checks.Surface("insert/s1.0", (800.0, 1000.0), 1.0, "contain")]
    # A portrait screen: its height reaches the largest window, its width gives way.
    assert insert_checks.quad_window(tall, (1600, 900)) == (1280, 1600)
    banner = [insert_checks.Surface("insert/s1.0", (4000.0, 400.0), 1.0, "contain")]
    assert insert_checks.quad_window(banner, (1600, 900)) == (2560, 480)
    assert insert_checks.quad_window([], (1600, 900)) == (1600, 900)


# ── F51: a flat green screen off pure green ───────────────────────────────

FRAME = (1280, 720)
#: Plate 00de87ae's TV: x 0.265-0.555, y 0.155-0.455 of the frame.
TV = (0.265, 0.155, 0.555, 0.455)


def _plate(colour: tuple[int, int, int], *, shade: float = 0.0) -> tuple[np.ndarray, list[tuple[float, float]]]:
    """A dim room with a flat TV painted *colour*, lit *shade* brighter at its top than at its bottom, a dark bezel round
    it; the frame and the TV's corners (0-1)."""
    picture = Image.new("RGB", FRAME, (62, 58, 54))
    draw = ImageDraw.Draw(picture)
    x0, y0, x1, y1 = (round(TV[0] * FRAME[0]), round(TV[1] * FRAME[1]), round(TV[2] * FRAME[0]), round(TV[3] * FRAME[1]))
    draw.rectangle((x0 - 14, y0 - 14, x1 + 14, y1 + 14), fill=(24, 24, 28))
    draw.rectangle((640, 500, 1100, 700), fill=(120, 96, 70))
    pixels = np.asarray(picture).astype(np.float32)
    rows = np.linspace(1.0 + shade, 1.0 - shade, y1 - y0)[:, None, None]
    pixels[y0:y1, x0:x1] = np.clip(np.asarray(colour, dtype=np.float32)[None, None, :] * rows, 0, 255)
    corners = [(x0 / FRAME[0], y0 / FRAME[1]), (x1 / FRAME[0], y0 / FRAME[1]), (x1 / FRAME[0], y1 / FRAME[1]), (x0 / FRAME[0], y1 / FRAME[1])]
    return pixels.astype(np.uint8), corners


def _off(quad, truth) -> float:
    """The mean corner distance between two quads (0-1), a share of the frame's width."""
    return float(np.mean([np.hypot((a[0] - b[0]) * FRAME[0], (a[1] - b[1]) * FRAME[1]) for a, b in zip(quad, truth, strict=True)])) / FRAME[0]


@pytest.mark.parametrize(
    ("colour", "shade", "hue"),
    [((0x2F, 0xBF, 0x4A), 0.0, 131.0), ((114, 158, 92), 0.08, 100.0)],
    ids=["2fbf4a", "shaded-yellow-green"],
)
def test_f51_a_flat_green_screen_off_pure_green_is_found_and_its_hue_said(colour, shade, hue):
    pixels, truth = _plate(colour, shade=shade)
    found = screens.found_screen(Image.fromarray(pixels))
    assert found.key == "green" and found.hue == pytest.approx(hue, abs=2.0)
    bezel = surface_refine.measure_bezel(pixels)
    assert bezel is not None and bezel.found_by == "frame" and bezel.as_dict()["hue"] == pytest.approx(hue, abs=2.0)
    assert _off(bezel.quad, truth) < 0.005
    # Seeded by hand on it, the quad snaps to that bezel.
    seeded = surface_refine.measure_bezel(pixels, given=[(x + 0.004, y - 0.003) for x, y in truth])
    assert seeded is not None and _off(seeded.quad, truth) < 0.005


def test_f51_the_shaded_screen_is_one_the_detector_missed_by_its_distance_to_the_key():
    import colorsys

    # Under the old threshold (0.45) and the key's own hue band (142 +- 40 degrees): never a candidate.
    h, s, _v = colorsys.rgb_to_hsv(114 / 255, 158 / 255, 92 / 255)
    assert s < 0.45 and h * 360.0 < 142.0 - 40.0
    assert screens.in_hue_band(np.array([h * 360.0]), screens.hue_band(("green", (0, 177, 64))))[0]
    # A surface under 2% of the frame is no screen, whatever its colour.
    small = np.full((720, 1280, 3), 60, dtype=np.uint8)
    small[300:330, 600:640] = (0x2F, 0xBF, 0x4A)
    with pytest.raises(screens.ScreenError, match="No screen found"):
        screens.found_screen(Image.fromarray(small))


# ── F43: a high render reads its inserts as its scene's preview does ───────

KEY = "insert/booth.0"


class _Route:
    def __init__(self, url: str):
        self.request = type("Request", (), {"url": url})()
        self.answer: dict = {}

    async def fulfill(self, **kwargs):
        self.answer = kwargs


def _server(tmp_path, name: str) -> compositor.FrameServer:
    """A page's frame server for a keyed static insert over a still footage - the frames whose address changes with
    nothing but the output frame they are drawn for."""
    still = tmp_path / "plate.png"
    if not still.exists():
        Image.fromarray(_room(GREEN)).save(still)
        Image.new("RGB", (400, 300), INSERT).save(tmp_path / "ui.png")
    insert = screen_insert.InsertSource(
        str(tmp_path / "ui.png"),
        "image",
        QUAD,
        key=screen_insert.KeySpec("green"),
        footage=screen_insert.Footage(str(still), "image"),
        label="Scene booth: its screen insert 0",
    )
    return compositor.FrameServer(compositor.CompositorAssets(inserts={KEY: insert}), "<html></html>", evidence_dir=tmp_path / name)


def _page(server: compositor.FrameServer, frames: range, offsets: list[float], *, centre: bool = True) -> dict:
    """*frames* drawn as ``compositor.render_frames`` drives a page, each over its sub-frames *offsets*: the page names
    the output frame it draws (``S.frame = Math.round(index)``) and asks a frame of the insert only for a new address
    (``setImg``); on the centre sub-frame the driver reads ``insertRects()`` - and, *centre*, audits what the centre
    shows. The audits handed on."""
    box = screen_insert.bbox(screen_insert.grown_box(QUAD, W / H), W, H)
    rects = {KEY: [box[0], box[1], box[2] - box[0], box[3] - box[1], 1.0]}
    shown = None

    async def draw() -> None:
        nonlocal shown
        for frame in frames:
            for offset in offsets:
                server.shutter = bool(offset)
                address = f"{compositor.ORIGIN}/{KEY}/0.png?w={W}&h={H}&n={round(frame + offset)}"
                if address != shown:
                    await server.handle(_Route(address))
                    shown = address
                server.shutter = False
                if not offset and centre and server.unaudited(frame):
                    await server.centre_shown(frame, rects)

    asyncio.run(draw())
    return server.take_audits(frames.start, frames.stop)


def test_f43_a_high_render_audits_every_frame_its_standard_preview_audits(tmp_path):
    frames = range(10, 22)
    blur = motion_design.blur_offsets(motion_design.BLUR_SAMPLES)
    assert len(blur) == 3 and 0.0 in blur and blur[0] < 0.0
    preview = _page(_server(tmp_path, "preview"), frames, [0.0])[KEY]["frames"]
    render = _page(_server(tmp_path, "render"), frames, blur)[KEY]["frames"]
    # Every frame audited at high as the standard preview audits it: the same frames, the same readings.
    assert set(render) == set(preview) == {str(n) for n in frames}
    assert all(render[n] == preview[n] for n in preview)
    # So its report says what the preview's says: the bezel found, every frame drawn, the same verdicts.
    placed = {KEY: insert_checks.Placed(KEY, "booth", "Scene booth: its screen insert 0", 0.0, 1.0)}
    said = [insert_checks.reports({KEY: {"frames": audited}}, placed, fps=60)[0] for audited in (preview, render)]
    assert said[0]["seed_error"] is not None and said[0]["seed_error"] == said[1]["seed_error"]
    assert said[0]["verdicts"] == said[1]["verdicts"] and said[1]["frames"] == len(frames)
    lines = [[measured.line() for measured in insert_checks.classify(report)] for report in said]
    assert lines[0] == lines[1] and lines[1][0].startswith("fact: bezel found, the quad ")


def test_f43_without_the_centre_audited_a_high_render_audits_no_frame_of_a_still_insert(tmp_path):
    # Render ac413cea's OpenAEV insert: its first sub-frame asks each frame, never audited; the centre asks nothing.
    blur = motion_design.blur_offsets(motion_design.BLUR_SAMPLES)
    taken = _page(_server(tmp_path, "before"), range(10, 22), blur, centre=False)
    assert not (taken.get(KEY) or {}).get("frames")


def test_f43_a_crop_is_cut_0_1_of_the_decoded_frame_on_the_screen_at_any_capture_size(tmp_path):
    corners = np.array([(0.403, 0.13), (0.805, 0.13), (0.805, 0.52), (0.403, 0.52)])
    stage = (1920, 1080)
    # The page places the insert's box in CSS pixels of its 1920x1080 stage, at every quality.
    box = screen_insert.bbox(screen_insert.grown_box(tuple(map(tuple, corners)), 16 / 9, margin=0.01), *stage)
    rect = [box[0], box[1], box[2] - box[0], box[3] - box[1]]
    cut: dict[tuple[int, int], dict] = {}
    for size in ((1920, 1080), (3840, 2160)):
        server = compositor.FrameServer(
            compositor.CompositorAssets(), "<html></html>", evidence_dir=tmp_path / f"{size[0]}", crops={KEY: [5]}, presence={5: [KEY]}
        )
        picture = Image.new("RGB", size, (60, 58, 54))
        draw = ImageDraw.Draw(picture)
        px = [(x * size[0], y * size[1]) for x, y in corners]
        draw.polygon(px, fill=(200, 40, 40))
        shot = io.BytesIO()
        picture.save(shot, format="PNG")
        server.place(5, {KEY: rect}, stage)
        asyncio.run(server.crop(5, shot.getvalue(), {KEY: rect}, stage))
        taken = server.take_audits(0, 10)[KEY]
        cut[size] = {"box": taken["crop_boxes"]["5"], "placed": taken["placed"]["5"][:4], "frame": taken["crop_frame"]}
        cut[size]["path"] = tmp_path / f"{size[0]}" / taken["crops"]["5"]
    one, two = cut[(1920, 1080)], cut[(3840, 2160)]
    # One coordinate system: the same box in 0-1 units at both sizes, the one the page placed - never doubled.
    assert one["box"] == pytest.approx(two["box"], abs=1e-3) and two["box"] == pytest.approx(two["placed"], abs=1e-3)
    assert max(two["box"]) <= 1.0 and two["box"][2] < 0.9 and two["frame"] == [3840, 2160]
    # The crops on the screen: the 2160p one twice the 1080p one's pixels, the screen at its centre.
    small, large = Image.open(one["path"]), Image.open(two["path"])
    assert abs(large.size[0] - 2 * small.size[0]) <= 2 and abs(large.size[1] - 2 * small.size[1]) <= 2
    assert large.getpixel((large.size[0] // 2, large.size[1] // 2)) == (200, 40, 40)


def test_f43_the_driver_audits_the_centre_whenever_an_insert_waits_for_it():
    import inspect

    from app.services.video_studio import compositor_page

    # The page names the output frame on every sub-frame and asks again only for another address.
    assert "S.frame = Math.round(index);" in compositor_page.PAGE_JS
    assert "if (!url || img.dataset.src === url) return;" in compositor_page.PAGE_JS
    source = inspect.getsource(compositor.render_frames)
    assert "page.server.unaudited(frame)" in source and "await page.server.centre_shown(frame, rects or {})" in source


@pytest.mark.chromium
@pytest.mark.skipif(not encoder.encoder_available(), reason="ffmpeg is not installed")
def test_f43_a_high_render_with_motion_blur_through_chromium_audits_every_frame_and_crops_on_the_screen(tmp_path):
    """The render's own pipeline at high, motion blur on: the compositor page in Chromium, the frame server, the encoder."""
    from app.services.video_studio.spec import QUALITY_PRESETS

    folder = tmp_path / "frames"
    folder.mkdir()
    names = []
    for index in range(30):
        Image.fromarray(_room(GREEN, seed=1)).save(folder / f"f{index:02d}.png")
        names.append((index / 30, f"f{index:02d}.png"))
    footage = str(tmp_path / "shot.mp4")
    asyncio.run(encoder.frames_to_mezzanine(names, end_time=1.0, out_path=footage, fps=30, crf=12, preset="ultrafast", workdir=str(folder)))
    picture = tmp_path / "insert.png"
    Image.new("RGB", (400, 300), INSERT).save(picture)
    key = "insert/shot.0"
    box = screen_insert.grown_box(QUAD, W / H)
    insert = screen_insert.InsertSource(
        str(picture),
        "image",
        QUAD,
        key=screen_insert.KeySpec("green"),
        footage=screen_insert.Footage(footage, "video", 30.0, "clip/shot"),
        first_frame=0,
        label="Scene shot: its screen insert 0",
        box=tuple(box),
    )
    assets = compositor.CompositorAssets(sources={"clip/shot": compositor.FrameSource(footage, 30.0)}, inserts={key: insert})
    payload = {"src": key, "kind": "image", "quad": [list(p) for p in QUAD], "fit": "cover", "glow": 0.0, "t0": 0.0, "t1": 1.0}
    payload["matte"] = {"fps": 30.0, "frames": 30}
    payload["box"] = [list(p) for p in box]
    document = {
        "version": 1,
        "width": W,
        "height": H,
        "fps": 30,
        "duration": 1.0,
        "frames": 30,
        "fade_in": 0.0,
        "fade_out": 0.0,
        "theme": {"motion": motion_design.resolve_motion({"motion_blur": "on"})},
        "captions": {"enabled": False, "style": "pill", "position": "bottom", "cues": []},
        "watermark": "none",
        "scenes": [
            {
                "id": "shot",
                "type": "clip",
                "src": "clip/shot",
                "fit": "cover",
                "time_map": [[0, 0], [1, 1]],
                "src_fps": 30,
                "src_frames": 30,
                "src_w": W,
                "src_h": H,
                "transition_in": {"type": "cut", "duration": 0},
                "overlays": [],
                "inserts": [payload],
                "start": 0.0,
                "end": 1.0,
            }
        ],
    }
    assert motion_design.blur_samples(document["theme"]["motion"], "high") == 3
    audits: dict = {}
    try:
        asyncio.run(
            compositor.render_frames(
                document,
                assets,
                preset=QUALITY_PRESETS["high"],
                output_size=(W, H),
                workdir=str(tmp_path / "render"),
                font_css="",
                pages=1,
                audits=audits,
                crops={key: {3, 15, 23}},
            )
        )
    except Exception as exc:
        text = str(exc)
        if "Executable doesn't exist" not in text and "playwright install" not in text.lower():
            raise
        if os.environ.get("REQUIRE_CHROMIUM") == "1":
            pytest.fail(f"Playwright's Chromium is required here and is not installed: {exc}")
        pytest.skip("Playwright's Chromium is not installed (python -m playwright install chromium)")
    # Every frame audited by its centre sub-frame, as a standard render audits it: the quad on its bezel.
    frames_audited = audits[key]["frames"]
    assert set(frames_audited) == set(range(30))
    assert all(a["coverage"] is not None and a["refined"] is not None for a in frames_audited.values())
    # The crops: cut 0-1 of the frame as captured, the screen at their centre.
    x0, y0, x1, y1 = screen_insert.bbox(box, W, H)
    centre = QUAD_PX.mean(axis=0)
    for frame, path in audits[key]["crops"].items():
        crop = Image.open(path).convert("RGB")
        assert abs(crop.size[0] - (x1 - x0)) <= 2 and abs(crop.size[1] - (y1 - y0)) <= 2, (frame, crop.size)
        at = np.asarray(crop)[round(centre[1]) - y0, round(centre[0]) - x0].astype(int)
        assert np.abs(at - INSERT).max() < 40, (frame, at)


# ── F44-F46: what the presence gate says of an insert it cannot read ──────


def _presence(render_, document, frames, *, shift: float = 0.0, flat: tuple[int, ...] = (), unmeasured=None, kept=None) -> insert_checks.Presence:
    """The presence gate over *render_*'s encoded *frames*, the boxes the page placed moved *shift* of the frame's width
    (a box read in another coordinate system), the frames *flat* drawn with no insert - its quad one flat colour; the
    frames a finding shows kept in *kept*."""
    from tests.video_studio_insert_fixture import BEZEL, SCREEN

    encoded = {n: render_.frame(n) for n in frames}
    for n in flat:
        picture = Image.fromarray(encoded[n])
        ImageDraw.Draw(picture).polygon([tuple(p) for p in SCREEN], fill=BEZEL)
        encoded[n] = np.asarray(picture)
    for audit in render_.audits.values():
        placed_at = audit.get("placed") or {}
        for n, spot in placed_at.items():
            placed_at[n] = [spot[0] + shift, spot[1], spot[2] + shift, spot[3], spot[4]]
    placed = insert_checks.placed_inserts(document, render_.inserts)
    gate = insert_checks.PresenceGate(
        render_.audits, placed, document, render_.inserts, fps=render_.document["fps"], unmeasured=unmeasured, evidence_dir=kept
    )
    for n, picture in encoded.items():
        gate.read(n, picture, {need: render_.pictures[need[0]] for need in gate.needs(n)})
    return gate.result()


def _tail():
    from tests.test_video_studio_insert_tail import CUT, _painted, _Render
    from tests.video_studio_insert_fixture import clip_document

    document = clip_document(CUT)
    return document, _Render(document), _painted(document, 0)


def test_f44_a_box_read_off_its_insert_is_its_measurement_lost_and_the_gate_red(tmp_path):
    document, render_, painted = _tail()
    result = _presence(render_, document, range(painted[0], painted[-1] + 1), shift=0.08, kept=tmp_path)
    [(lost, path)] = [hit for hit in result.findings if hit[0].data["insert"] == "insert/a.0"]
    # Its measurement lost, naming the frame, the box and the size it was read at - never its source missing from N of N.
    assert lost.kind == render_checks.INSERT_MEASUREMENT_LOST and lost.severity == render_checks.FAIL
    assert lost.data["frame"] == painted[0] and len(lost.data["box"]) == 4 and lost.data["why"] == "moved"
    assert lost.data["read_at"] == [480, 270] and Image.open(path).size == (480, 270)
    # Where the gate found its source: the box's shift back, in the quad's width (0.08 of the frame, the quad 0.375 of it).
    assert lost.data["moved"][0] == pytest.approx(-0.08 / 0.375, abs=0.03) and abs(lost.data["moved"][1]) < 0.03
    assert f"read on encoded frame {painted[0]} at box [" in lost.message and "decoded at 480x270" in lost.message
    assert "of the quad's width left of it" in lost.message
    assert not [hit for hit in result.findings if hit[0].kind == render_checks.INSERT_MISSING]
    # A block of the video, never the harness's.
    kept, defects = render_checks.harness_defects([lost], {})
    checks = render_checks.Checks(findings=kept, harness=defects)
    assert kept == [lost] and defects == [] and checks.failed
    assert render_checks.open_findings(checks.as_dict(), same_quality=False)
    # Its report says the measurement lost, never a presence counted on another region.
    report = {"presence": result.by_key["insert/a.0"], "missing_frames": 40}
    lines = [m.line() for m in insert_checks.classify(report, {render_checks.INSERT_MEASUREMENT_LOST})]
    assert lines[0].startswith("finding (insert_measurement_lost): measurement lost: its source is not under the box read on")
    assert not [line for line in lines if "not drawn on" in line or ("its source" in line and "encoded frame(s)" in line)]
    assert not [line for line in lines if "no bezel found" in line]


def test_f44_the_same_box_read_where_it_is_drawn_holds_its_measurement():
    document, render_, painted = _tail()
    result = _presence(render_, document, range(painted[0], painted[-1] + 1))
    assert result.findings == [] and "lost" not in result.by_key["insert/a.0"]


def test_f44_an_insert_whose_bezel_the_save_and_the_preview_found_and_the_render_did_not_is_its_measurement_lost(tmp_path):
    document, render_, painted = _tail()
    frames = range(painted[0], painted[-1] + 1)
    for n in frames:
        render_.layers(n)
    # The render's frames found no bezel - as ac413cea's audits said of the OpenAEV insert - where the save and the
    # preview of its scene found it 0.05% off at 1920x1080.
    for audit in render_.audits["insert/a.0"]["frames"].values():
        for field in insert_checks.BEZEL_READING_FIELDS:
            audit.pop(field, None)
    passed = {
        "insert/a.0": [
            {"stage": "preview", "id": "5fc492aa", "gates": {"alignment": True}, "alignment": 0.0005, "measured_at": [1920, 1080]},
            {"stage": "save", "id": "revision 136", "gates": {"alignment": True}, "alignment": 0.0005, "measured_at": [1920, 1080], "where": "frame 0 (0.00 s) of its footage"},
        ]
    }
    placed = insert_checks.placed_inserts(document, render_.inserts)
    lost = insert_checks.unmeasured(render_.audits, placed, passed=passed)
    assert set(lost) == {"insert/a.0"} and lost["insert/a.0"]["drawn_at"] == [480, 270]
    assert [bezel["by"] for bezel in lost["insert/a.0"]["earlier"]] == ["preview 5fc492aa", "save revision 136"]
    result = _presence(render_, document, frames, unmeasured=lost, kept=tmp_path)
    [(finding, path)] = [hit for hit in result.findings if hit[0].data["insert"] == "insert/a.0"]
    assert finding.kind == render_checks.INSERT_MEASUREMENT_LOST and finding.severity == render_checks.FAIL
    # The frame it names, kept as the gate decoded it: what its crop is cut from.
    assert Image.open(path).size == (480, 270)
    assert finding.data["frame"] == painted[0] and finding.data["why"] == "unmeasured" and finding.data["read_at"] == [480, 270]
    # The size each read it at: the preview's and the save's, the render's frames, the encoded frame.
    assert "as preview 5fc492aa measured it at 1920x1080" in finding.message
    assert "as save revision 136 measured it on frame 0 (0.00 s) of its footage at 1920x1080" in finding.message
    assert f"frame(s) it audited of it at 480x270 - read on encoded frame {painted[0]} at box [" in finding.message
    # Nothing else said of it: no source missing from N of N, no frame not drawn filed on its own.
    assert result.by_key["insert/a.0"]["lost"]["why"] == "unmeasured" and result.by_key["insert/a.0"]["frames"] == 0
    missing = {n: a for n, a in render_.audits["insert/a.0"]["frames"].items() if n % 3}
    shown = insert_checks.findings({"insert/a.0": {**render_.audits["insert/a.0"], "frames": missing}}, placed, fps=24, unmeasured=lost)
    assert not [hit for hit in shown if hit[0].data.get("insert") == "insert/a.0" and hit[0].kind == render_checks.INSERT_MISSING]


def test_f44_a_render_that_found_its_bezel_or_an_insert_never_measured_before_is_not_lost():
    document, render_, painted = _tail()
    for n in range(painted[0], painted[-1] + 1):
        render_.layers(n)
    placed = insert_checks.placed_inserts(document, render_.inserts)
    passed = {"insert/a.0": [{"stage": "preview", "id": "p", "alignment": 0.0005, "measured_at": [1920, 1080]}]}
    # Its own frames found the bezel: measured.
    assert insert_checks.unmeasured(render_.audits, placed, passed=passed) == {}
    for audit in render_.audits["insert/a.0"]["frames"].values():
        for field in insert_checks.BEZEL_READING_FIELDS:
            audit.pop(field, None)
    # Nothing found it before either (a quad nothing measured): drawn where the quad is, a fact.
    assert insert_checks.unmeasured(render_.audits, placed, passed={}) == {}
    # A join reading set aside for the save's measure: that measure stands.
    render_.audits["insert/a.0"]["bezel_stored"] = {"alignment": 0.0005, "measured_at": [1920, 1080], "by": "preview p"}
    assert insert_checks.unmeasured(render_.audits, placed, passed=passed) == {}


def test_f45_an_insert_truly_absent_for_10_frames_is_insert_missing_and_blocks():
    document, render_, painted = _tail()
    gone = tuple(painted[10:20])
    result = _presence(render_, document, range(painted[0], painted[-1] + 1), flat=gone)
    [(missing, _path)] = result.findings
    assert missing.kind == render_checks.INSERT_MISSING and missing.data["frames"] == 10 and missing.data["frame"] == gone[0]
    # Nothing in the spec causes it - and on 10 frames it is the video's, never the harness's said once.
    assert missing.data["action"]["kind"] == render_checks.UNEXPLAINED_ACTION and not render_checks.is_unexplained(missing)
    kept, defects = render_checks.harness_defects([missing], {})
    assert kept == [missing] and defects == [] and render_checks.Checks(findings=kept).failed


def test_f45_an_insert_missing_from_its_first_frames_on_is_missing_never_lost():
    document, render_, painted = _tail()
    gone = tuple(painted[:12])
    result = _presence(render_, document, range(painted[0], painted[-1] + 1), flat=gone)
    [(missing, _path)] = result.findings
    # Its source found nowhere around the quad read: the insert missing there, the measurement held.
    assert missing.kind == render_checks.INSERT_MISSING and missing.data["frames"] == 12
    assert "lost" not in result.by_key["insert/a.0"]


def test_f46_one_frame_the_page_drew_and_the_encoder_dropped_is_the_one_harness_defect_left():
    document, render_, painted = _tail()
    result = _presence(render_, document, range(painted[0], painted[-1] + 1), flat=(painted[12],))
    [(dropped, _path)] = result.findings
    assert dropped.kind == render_checks.INSERT_MISSING and dropped.data["frames"] == 1
    # One frame: the harness's defect, said once to the administrator, blocking nothing.
    assert render_checks.is_unexplained(dropped)
    kept, defects = render_checks.harness_defects([dropped], {})
    assert kept == [] and defects == [dropped] and dropped.data["said"] is True
    assert not render_checks.Checks(findings=kept, harness=defects).failed
    assert render_checks.harness_line(dropped.as_dict()).startswith("render harness, ")


def test_a_layer_not_drawn_on_several_frames_is_insert_missing_and_on_one_a_flicker_of_the_harness():
    from app.services.video_studio.render_checks import HARNESS_MAX_FRAMES

    assert HARNESS_MAX_FRAMES == 1
    many = render_checks.Finding("insert_missing", 1.0, 2.0, "s1", "m", data={"action": {"kind": "nothing"}, "absent": 462})
    one = render_checks.Finding("insert_flicker", 1.0, 1.0, "s1", "m", data={"action": {"kind": "nothing"}, "absent": 1})
    assert not render_checks.is_unexplained(many) and render_checks.is_unexplained(one)
    # A layer not drawn is the presence gate's to say, off the encoded frame: the flicker check files none from
    # the page log, on several frames or one. On what the gate reads, several frames are missing there and one frame a
    # flicker (``test_video_studio_insert_infra_notes_6596``).
    placed = insert_checks.Placed("insert/s1.0", "s1", "Scene s1: its screen insert 0", 0.0, 2.0)
    frames = {n: {"coverage": 1.0} for n in range(0, 60) if n not in (20, 21, 22)}
    assert insert_checks._compositing_findings("insert/s1.0", placed, frames, {}, lambda n: n / 30, None, []) == []
