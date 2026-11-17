"""Screen inserts matted, alpha sources, logos verified, the picture gate.

On synthetic footage, no provider: a green screen with a cat in front of it
keyed and despilled; a surface without a key, a hand entering it through its
edge kept in front while what the screen itself shows is covered; a PNG
logo drawn on transparency with a halo around its own shape; a picture with
a card baked in found padded; the frame server's audit of each frame drawn,
kept per segment and read back as the render's findings with their frames;
a logo on a curved surface drawn on its local warp and a take whose logo is
not the asset failed with its frame; the editor's preview build drawn by the
same compositor and matched with the render's frame at three moments; the
spec, the gate and the files a render owns.
"""

from __future__ import annotations

import asyncio
import dataclasses
import json
import uuid
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest
from PIL import Image, ImageDraw
from pydantic import ValidationError

from app.services.video_studio import (
    compositor,
    encoder,
    insert_builds,
    insert_checks,
    logos,
    media,
    render_checks,
    render_retention,
    screen_insert,
    timeline,
)
from app.services.video_studio.render_checks import FAIL, Finding
from app.services.video_studio.spec import InsertKey, Overlay, parse_video_spec

FFMPEG = pytest.mark.skipif(not encoder.encoder_available(), reason="ffmpeg is not installed")
CLIP = "00000000-0000-0000-0000-0000000061c1"
TAKE = "00000000-0000-0000-0000-0000000061c2"
PICTURE = "00000000-0000-0000-0000-0000000061c3"
W, H = 640, 360
QUAD = ((0.3, 0.2), (0.7, 0.22), (0.69, 0.7), (0.31, 0.68))
QUAD_PX = np.array([(x * W, y * H) for x, y in QUAD])
GREEN = (0, 177, 64)
INSERT = (30, 120, 220)


def _room(surface: tuple[int, int, int], *, cat: bool = False, seed: int = 1, inside: tuple[int, int, int] | None = None) -> np.ndarray:
    """A textured room with a flat surface on its wall; a dark cat in front of its lower edge; something on the screen."""
    rng = np.random.default_rng(seed)
    picture = Image.fromarray((rng.random((H, W, 3)) * 40 + 90).astype(np.uint8))
    draw = ImageDraw.Draw(picture)
    draw.polygon([tuple(p) for p in QUAD_PX], fill=surface)
    if inside is not None:
        # What the model drew on the screen, well inside its edges.
        draw.rectangle((300, 120, 380, 170), fill=inside)
    if cat:
        draw.ellipse((260, 200, 420, 400), fill=(60, 40, 30))
    return np.asarray(picture)


def _layer(insert: screen_insert.InsertSource, footage: np.ndarray, surface=None, picture=None) -> screen_insert.Layer:
    shown = picture or Image.new("RGBA", (400, 300), (*INSERT, 255))
    return screen_insert.compose(insert, shown, (W, H), footage=footage, surface=surface, evidence=True)


def _at(layer: screen_insert.Layer, x: int, y: int) -> tuple[int, ...]:
    x0, y0, _x1, _y1 = layer.warped.box
    return tuple(int(v) for v in np.asarray(layer.warped.image)[y - y0, x - x0])


# ── 1. The matte ──────────────────────────────────────────────────────────


def test_a_keyed_insert_is_drawn_on_the_key_alone_and_what_stands_in_front_stays_in_front_despilled():
    insert = screen_insert.InsertSource("p.png", "image", QUAD, key=screen_insert.KeySpec("green"), footage=screen_insert.Footage("f"))
    footage = _room(GREEN, cat=True).copy()
    # The camera's blur bleeds the key into the cat's edge: green spill on its fur.
    footage[230:236, 300:380] = (70, 120, 60)
    layer = _layer(insert, footage)
    # On the surface: the insert. On the cat: the cat, the layer opaque over it (the footage despilled there).
    assert _at(layer, 340, 120)[:3] == INSERT and _at(layer, 340, 120)[3] == 255
    cat = _at(layer, 340, 245)
    assert cat[3] == 255 and abs(cat[0] - 60) < 6 and abs(cat[2] - 30) < 6
    spill = _at(layer, 340, 233)
    # Despilled: its green taken out along the key's chromaticity, no green fringe.
    assert spill[1] <= max(spill[0], spill[2]) + 8, spill
    # Nothing of the insert is drawn over the cat: the frame's audit agrees.
    assert layer.audit.covered == 0.0 and not layer.audit.covers_foreground


def test_without_a_key_what_enters_the_surface_through_its_edge_stays_in_front_and_the_screen_s_own_change_is_covered():
    insert = screen_insert.InsertSource("p.png", "image", QUAD, footage=screen_insert.Footage("f"))
    first = _room((200, 190, 180))
    surface = screen_insert.Surface.of(first, QUAD_PX)
    # A cat walks in over the lower edge, and the model draws something new in the middle of the screen.
    later = _room((200, 190, 180), cat=True, inside=(250, 30, 30))
    layer = _layer(insert, later, surface)
    assert _at(layer, 340, 245)[3] == 0, "the cat is in front: the insert is not drawn there"
    assert _at(layer, 340, 140)[:3] == INSERT and _at(layer, 340, 140)[3] == 255, "the screen's own drawing is covered"
    assert _at(layer, 340, 100)[3] == 255
    assert layer.audit.covered == 0.0
    # The same surface, nothing in front: drawn whole (a level of LANCZOS ringing a few pixels from the quad's edge).
    whole = _layer(insert, _room((200, 190, 180), seed=1), surface)
    assert _at(whole, 340, 245)[3] >= 253


def test_an_exposure_change_or_a_surface_that_moved_off_its_reference_is_no_occluder():
    first = _room((200, 190, 180))
    surface = screen_insert.Surface.of(first, QUAD_PX)
    darker = (first.astype(np.float32) * 0.8).astype(np.uint8)
    assert not surface.occluded(darker, QUAD_PX).any()
    # A camera an untracked quad does not follow: the whole rim changed - the surface moved, nothing came in front.
    moved = np.roll(first, (6, 8), axis=(0, 1))
    assert not surface.occluded(moved, QUAD_PX).any()


def test_a_key_is_one_of_three_names_or_a_hex_colour_with_its_tolerance_and_feather():
    assert InsertKey(color="Green").color == "green"
    assert InsertKey(color="#12AB34").color == "#12ab34"
    assert (InsertKey().tolerance, InsertKey().feather) == (0.3, 1.5)
    for wrong in ("teal", "#12ab3", "rgb(0,1,2)"):
        with pytest.raises(ValidationError, match="a key's color is green, blue, magenta or a #rrggbb colour"):
            InsertKey(color=wrong)
    with pytest.raises(ValidationError):
        InsertKey(tolerance=2)
    assert screen_insert.KeySpec("#12ab34").key().rgb == (0x12, 0xAB, 0x34)
    assert screen_insert.KeySpec("magenta").key().rgb == (230, 0, 210)


def test_a_wider_tolerance_takes_more_of_a_tinted_surface_for_the_key():
    from app.services.video_studio import screens

    # A screen lit yellower than its key: a tight tolerance keeps it out, a wide one takes it for the surface.
    tinted = np.full((4, 4, 3), (90, 170, 40), dtype=np.float32)
    key = screens.Key("green", GREEN)
    assert screens.key_matte(tinted, key, 0.15).max() == 0.0 and screens.key_matte(tinted, key, 0.6).min() > 0.3


# ── 2. Alpha sources ──────────────────────────────────────────────────────


def _mark() -> Image.Image:
    """A logo on transparency: an emblem and a wordmark bar, nothing around them."""
    mark = Image.new("RGBA", (400, 120), (0, 0, 0, 0))
    draw = ImageDraw.Draw(mark)
    draw.ellipse((10, 10, 110, 110), fill=(255, 255, 255, 255))
    draw.rectangle((140, 45, 390, 75), fill=(255, 255, 255, 255))
    return mark


def test_a_png_keeps_its_alpha_and_contain_pads_with_transparency_never_black():
    insert = screen_insert.InsertSource("p.png", "image", QUAD, fit="contain")
    layer = screen_insert.compose(insert, _mark(), (W, H))
    alpha = np.asarray(layer.warped.image)[..., 3]
    # A wide logo on a squarer surface: above and below it, and between its emblem and its bar, the surface shows.
    x0, y0, _x1, _y1 = layer.warped.box
    assert alpha[100 - y0, 340 - x0] == 0 and alpha.max() == 255
    assert (alpha == 255).mean() < 0.4


def test_the_glow_follows_the_marks_silhouette_not_the_quads_rectangle():
    insert = screen_insert.InsertSource("p.png", "image", QUAD, fit="contain", glow=0.6)
    layer = screen_insert.compose(insert, _mark(), (W, H))
    bloom = np.asarray(layer.glow.resize((W, H)), dtype=np.float32)[..., 3]
    # Around the wordmark: light. In a corner of the quad, far from the mark: almost none.
    near, corner = bloom[int(0.44 * H), int(0.62 * W)], bloom[int(0.24 * H), int(0.33 * W)]
    assert near > 4 * max(corner, 1.0), (near, corner)


def test_a_picture_with_a_card_baked_in_is_padded_and_a_transparent_one_or_a_header_bar_is_not():
    # The mark on a grey card letterboxed in black: bands that are not the card's own sides.
    card = Image.new("RGBA", (400, 300), (0, 0, 0, 255))
    ImageDraw.Draw(card).rectangle((0, 80, 399, 219), fill=(90, 90, 96, 255))
    card.paste(_mark(), (0, 90), _mark())
    assert screen_insert.padded_sides(card, "contain", 400, 300) == "top and bottom"
    assert screen_insert.padded_sides(_mark(), "contain", 400, 300) is None
    # A plate of one colour the mark sits on is one surface, and a cover fit is never judged.
    plate = Image.new("RGBA", (400, 300), (0, 0, 0, 255))
    plate.paste(_mark(), (0, 90), _mark())
    assert screen_insert.padded_sides(plate, "contain", 400, 300) is None
    white = Image.new("RGBA", (400, 300), (255, 255, 255, 255))
    white.paste(_mark().resize((120, 36)), (140, 132))
    assert screen_insert.padded_sides(white.convert("RGB"), "cover", 400, 300) is None
    ui = Image.new("RGB", (400, 300), (255, 255, 255))
    draw = ImageDraw.Draw(ui)
    draw.rectangle((0, 40, 400, 300), fill=(20, 30, 40))
    for row in range(40, 300, 8):
        draw.line((0, row, 400, row), fill=(200, 100, 50))
    assert screen_insert.padded_sides(ui, "cover", 400, 300) is None


def test_a_brand_mark_is_a_source_by_its_role_each_its_own_file():
    insert = Overlay.model_validate({"type": "screen_insert", "source": {"asset": "logo_mark"}, "quad": [list(p) for p in QUAD]})
    assert insert.source.asset == "logo_mark" and insert.source.file_id is None
    with pytest.raises(ValidationError, match="exactly one of file_id, take_id, clip_id or asset"):
        Overlay.model_validate({"type": "screen_insert", "source": {"asset": "logo", "file_id": PICTURE}, "quad": [list(p) for p in QUAD]})
    spec = parse_video_spec(
        {
            "title": "T",
            "scenes": [
                {
                    "id": "cape",
                    "type": "clip",
                    "clip_id": CLIP,
                    "overlays": [
                        {"type": "screen_insert", "source": {"asset": "logo"}, "quad": [list(p) for p in QUAD]},
                        {"type": "screen_insert", "source": {"asset": "logo_mark"}, "quad": [list(p) for p in QUAD]},
                        # A keyed wall: each pane its own mark, every pane matted with the wall's key.
                        {
                            "type": "screen_insert",
                            "panes": 2,
                            "sources": [{"asset": "logo"}, {"asset": "logo_mark"}],
                            "quad": [list(p) for p in QUAD],
                            "key": {"color": "green"},
                        },
                    ],
                }
            ],
        }
    )
    parts = timeline.insert_sources(spec)
    assert (parts["insert/cape.0"].kind, parts["insert/cape.0"].ref) == ("asset", "logo")
    panes = [parts["insert/cape.2.0"], parts["insert/cape.2.1"]]
    assert [(p.kind, p.ref) for p in panes] == [("asset", "logo"), ("asset", "logo_mark")]
    assert all(p.overlay.key.color == "green" for p in panes) and panes[0].quad != panes[1].quad
    palette = {"logo_file_id": "full", "logo_light_file_id": None, "mark_file_id": None}
    # The full logo is the logo; the template has no mark, so the mark is none - never the logo standing for it.
    assert timeline.brand_asset_files(spec, palette) == {"logo": "full", "logo_light": None, "logo_mark": None}


def test_an_edit_naming_a_mark_drops_the_file_the_source_named():
    from app.services.video_studio.projects import _merge

    merged = _merge({"source": {"file_id": PICTURE}}, {"source": {"asset": "logo"}})
    assert merged["source"] == {"asset": "logo"}


# ── 3/7. The frame server audits what it draws ────────────────────────────


class _Route:
    def __init__(self, url: str) -> None:
        self.request = SimpleNamespace(url=url)
        self.answer: dict = {}

    async def fulfill(self, **kwargs):
        self.answer = kwargs


def test_the_frame_server_mattes_on_the_footage_under_the_insert_and_audits_each_frame(tmp_path):
    still = tmp_path / "backdrop.png"
    Image.fromarray(_room(GREEN, cat=True)).save(still)
    picture = tmp_path / "insert.png"
    Image.new("RGB", (400, 300), INSERT).save(picture)
    key = "insert/cat-soc.0"
    insert = screen_insert.InsertSource(
        str(picture), "image", QUAD, key=screen_insert.KeySpec("green"), footage=screen_insert.Footage(str(still), "image"), label="Scene cat-soc: its screen insert 0"
    )
    assets = compositor.CompositorAssets(inserts={key: insert})
    server = compositor.FrameServer(assets, "<html></html>", evidence_dir=tmp_path / "evidence")

    async def ask(n: int) -> _Route:
        route = _Route(f"{compositor.ORIGIN}/{key}/0.png?w={W}&h={H}&n={n}")
        await server.handle(route)
        return route

    first = asyncio.run(ask(7))
    assert first.answer["status"] == 200
    drawn = screen_insert.decode(first.answer["body"])
    x0, y0, _x1, _y1 = screen_insert.bbox(list(QUAD), W, H)
    assert int(np.asarray(drawn)[245 - y0, 340 - x0][0]) < 80, "the cat is drawn back in front"
    asyncio.run(ask(8))
    taken = server.take_audits(0, 10)
    assert set(taken[key]["frames"]) == {"7", "8"} and taken[key]["frames"]["7"]["covered"] == 0.0
    assert server.take_audits(0, 10) == {}


def test_the_audits_of_a_segment_are_kept_beside_it_and_read_back_merged(tmp_path):
    directory = tmp_path / compositor.SEGMENTS_DIR
    (directory / compositor.EVIDENCE_DIR).mkdir(parents=True)
    (directory / compositor.EVIDENCE_DIR / "proof.jpg").write_bytes(b"jpeg")
    first = {"insert/a.0": {"frames": {"3": {"covered": 0.2, "logo_visible": None}}, "evidence": {"covered": {"badness": 0.2, "frame": 3, "file": "proof.jpg"}}}}
    second = {"insert/a.0": {"frames": {"12": {"covered": 0.0, "logo_visible": None}}, "evidence": {}}}
    for (a, b), part in (((0, 10), first), ((10, 20), second)):
        (directory / (Path(compositor.segment_name(a, b)).stem + compositor.AUDIT_SUFFIX)).write_text(json.dumps(part), encoding="utf-8")
    merged = compositor.segment_audits(directory, [(0, 10), (10, 20), (20, 30)])
    assert merged["insert/a.0"]["frames"] == {3: {"covered": 0.2, "logo_visible": None}, 12: {"covered": 0.0, "logo_visible": None}}
    assert merged["insert/a.0"]["evidence"]["covered"][0]["path"].endswith("proof.jpg")


def _placed(**extra) -> dict:
    return {
        "insert/cat-soc.0": insert_checks.Placed(
            "insert/cat-soc.0", "cat-soc", "Scene cat-soc: its screen insert 0", 2.0, 5.0, extra.get("padded"), extra.get("logo")
        )
    }


def test_frames_drawn_over_the_foreground_fail_the_scene_with_the_time_and_the_frame():
    audits = {
        "insert/cat-soc.0": {
            "frames": {48: {"covered": 0.0}, 60: {"covered": 0.034}, 61: {"covered": 0.05}},
            "evidence": {"covered": [{"badness": 0.05, "frame": 61, "path": "/w/e.jpg"}]},
        }
    }
    [(finding, proof)] = insert_checks.findings(audits, _placed(), fps=24)
    assert finding.kind == render_checks.INSERT_FOREGROUND and finding.severity == FAIL and finding.scene_id == "cat-soc"
    assert finding.at == pytest.approx(2.5) and proof == "/w/e.jpg"
    assert finding.message.startswith("insert over foreground at t=0:02.54: Scene cat-soc: its screen insert 0")
    # A scene preview's MP4 starts later: its times are its own.
    [(shifted, _proof)] = insert_checks.findings(audits, _placed(), fps=24, offset=1.0, span=(1.0, 6.0))
    assert shifted.at == pytest.approx(1.5)


def test_a_brand_mark_that_stops_showing_is_lost_and_a_padded_picture_fails():
    audits = {"insert/cat-soc.0": {"frames": {50: {"covered": None, "logo_visible": 0.97}, 51: {"covered": None, "logo_visible": 0.6}}, "evidence": {}}}
    found = insert_checks.findings(audits, _placed(logo="logo", padded="top and bottom"), fps=25)
    kinds = {f.kind: f for f, _p in found}
    assert kinds[render_checks.LOGO_LOST].message.startswith("logo lost at t=0:02.04: Scene cat-soc: its screen insert 0 shows only 60%")
    assert kinds[render_checks.INSERT_PADDED].message.startswith("insert padded: Scene cat-soc: its screen insert 0's picture holds flat bands top and bottom")
    # A preview of another part of the video says nothing of a padded insert it does not show.
    assert not insert_checks.findings({}, _placed(padded="left and right"), fps=25, offset=10.0, span=(10.0, 3.0))


def test_a_picture_finding_refuses_the_next_render_and_its_frame_goes_with_the_render():
    finding = Finding(render_checks.INSERT_FOREGROUND, 2.5, 2.6, "cat-soc", "insert over foreground at t=0:02.50", FAIL, {"frame_file_id": str(uuid.uuid4())})
    checks = render_checks.Checks(findings=[finding]).as_dict()
    assert render_checks.open_findings(checks, same_quality=False) == checks["findings"]
    assert "(frame: file `" in render_checks.finding_line(checks["findings"][0])
    assert render_checks.failure_summary(render_checks.Checks(findings=[finding])).startswith("The render failed its picture check")
    seen = render_checks.record_seen({}, {"cat-soc": "d1"}, uuid.uuid4(), checks=checks)
    assert seen["cat-soc"]["passed"] is False and seen["cat-soc"]["picture"][0]["kind"] == render_checks.INSERT_FOREGROUND
    row = SimpleNamespace(file_id=uuid.uuid4(), poster_file_id=None, captions_file_id=None, frame_file_ids=None, checks=checks)
    assert uuid.UUID(finding.data["frame_file_id"]) in render_retention.render_files(row)


def test_a_surface_lost_is_a_finding_at_its_scene_s_time():
    from app.services.video_studio.insert_tracking import InsertTrackLostError

    error = InsertTrackLostError("Scene demo: its screen insert 0: surface lost at t=0.42 s", frame=16, seconds=0.67, scene_id="demo", scene_seconds=0.42)
    finding = insert_checks.surface_lost(error, {"scenes": [{"id": "demo", "start": 3.0}]})
    assert (finding.kind, finding.scene_id, finding.severity) == (render_checks.SURFACE_LOST, "demo", FAIL)
    assert finding.at == pytest.approx(3.42) and finding.data["footage_frame"] == 16


def test_the_page_names_the_footage_frame_and_the_output_frame_of_a_matted_insert():
    from app.services.video_studio.compositor_page import PAGE_CSS, PAGE_JS

    # A sub-frame of motion blur names the output frame it is drawn for.
    assert "(mt ? `&n=${S.frame}` : '')" in PAGE_JS and "S.frame = Math.round(index);" in PAGE_JS
    assert "mt && mt.fps ? clamp(Math.round((at || 0) * mt.fps)" in PAGE_JS
    # The editor's preview plays the build, and never paints a black card behind a contain fit.
    assert "previewBuild(host, ins) || previewInsert(host, ins, w, h)" in PAGE_JS
    assert ".insprev { position: absolute; left: 0; top: 0; overflow: hidden; transform-origin: 0 0; background: transparent; }" in PAGE_CSS


@pytest.mark.chromium
@FFMPEG
@pytest.mark.parametrize("keyed", [True, False], ids=["keyed", "occlusion"])
def test_a_render_keeps_the_cat_in_front_of_an_insert_and_hands_back_its_audits(tmp_path, keyed):
    """The render's own pipeline: the compositor page in Chromium, the frame server matting, the encoder."""
    import os

    from app.services.video_studio.spec import QUALITY_PRESETS

    folder = tmp_path / "frames"
    folder.mkdir()
    names = []
    for index in range(30):
        room = Image.fromarray(_room(GREEN, seed=1))
        if index >= 15:
            ImageDraw.Draw(room).ellipse((260, 200, 420, 400), fill=(60, 40, 30))
        room.save(folder / f"f{index:02d}.png")
        names.append((index / 30, f"f{index:02d}.png"))
    footage = str(tmp_path / "shot.mp4")
    asyncio.run(encoder.frames_to_mezzanine(names, end_time=1.0, out_path=footage, fps=30, crf=12, preset="ultrafast", workdir=str(folder)))
    picture = tmp_path / "insert.png"
    Image.new("RGB", (400, 300), INSERT).save(picture)
    key = "insert/shot.0"
    # The box the page and the driver place every frame on: the quad grown, as ``render._matte_inserts`` sets it.
    box = screen_insert.grown_box(QUAD, W / H)
    insert = screen_insert.InsertSource(
        str(picture),
        "image",
        QUAD,
        key=screen_insert.KeySpec("green") if keyed else None,
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
        "theme": {},
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
    audits: dict = {}
    try:
        video = asyncio.run(
            compositor.render_frames(
                document,
                assets,
                preset=QUALITY_PRESETS["draft"],
                output_size=(W, H),
                workdir=str(tmp_path / "render"),
                font_css="",
                pages=1,
                audits=audits,
                # The insert region cut from the frames as captured: first, middle and last at full opacity.
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

    async def read() -> list[np.ndarray]:
        return [np.frombuffer(raw, dtype=np.uint8).reshape(H, W, 3) async for raw in encoder.iter_rgb_frames(video, width=W, height=H)]

    frames = asyncio.run(read())
    assert len(frames) == 30
    # Before the cat: the insert on the whole surface. With it: the cat in front, the insert around it.
    early, late = frames[5], frames[25]
    assert np.abs(early[245, 340].astype(int) - INSERT).max() < 40, early[245, 340]
    assert np.abs(late[245, 340].astype(int) - (60, 40, 30)).max() < 40, late[245, 340]
    assert np.abs(late[110, 340].astype(int) - INSERT).max() < 40, late[110, 340]
    # Every frame drawn was audited, by its output frame: nothing drawn over the cat, the quad on its bezel.
    frames_audited = audits[key]["frames"]
    assert set(frames_audited) == set(range(30)) and all(a["covered"] == 0.0 for a in frames_audited.values())
    assert all(a["coverage"] is not None and a["refined"] is not None for a in frames_audited.values())
    # The crops: the insert region at the render's resolution, cut where the page placed it, 100 %.
    crops = audits[key]["crops"]
    assert set(crops) == {3, 15, 23}
    x0, y0, x1, y1 = screen_insert.bbox(box, W, H)
    for frame, path in crops.items():
        crop = Image.open(path)
        assert abs(crop.size[0] - (x1 - x0)) <= 2 and abs(crop.size[1] - (y1 - y0)) <= 2, (frame, crop.size)
        assert np.abs(np.asarray(crop.convert("RGB"))[110 - y0, 340 - x0].astype(int) - INSERT).max() < 40


# ── 5. Logos ──────────────────────────────────────────────────────────────


def test_a_curved_surface_gets_a_local_warp_and_a_flat_one_none():
    origin = np.array([[100.0, 100.0], [300.0, 100.0], [300.0, 200.0], [100.0, 200.0]])
    rng = np.random.default_rng(2)
    reference = rng.uniform([80, 80], [320, 220], (80, 2))
    homography = np.eye(3)
    # A mug's side: the middle of the logo bulges toward the camera, its edges stay.
    bend = np.column_stack([np.zeros(len(reference)), 6.0 * np.sin(np.pi * (reference[:, 0] - 80) / 240)])
    curved = SimpleNamespace(reference_points=reference, points=reference + bend)
    mesh = logos._local_warp(curved, homography, origin, (400, 300))
    assert mesh is not None and mesh.shape == (logos.MESH_CELLS + 1, logos.MESH_CELLS + 1, 2)
    middle = mesh[logos.MESH_CELLS // 2, logos.MESH_CELLS // 2] * [400, 300]
    assert middle[1] - 150.0 > 3.0
    flat = SimpleNamespace(reference_points=reference, points=reference.copy())
    assert logos._local_warp(flat, homography, origin, (400, 300)) is None
    # Drawn cell by cell on it, the logo's own pixels cover the bent place.
    layer = logos._mesh_layer(_mark(), mesh, (400, 300))
    assert layer is not None and np.asarray(layer.image)[..., 3].max() == 255


def test_the_drawn_back_logo_is_compared_with_the_asset_where_it_is():
    grey = np.tile(np.linspace(0, 255, 32), (32, 1))
    weight = np.ones_like(grey)
    assert logos.masked_ssim(grey, grey, weight) == pytest.approx(1.0)
    assert logos.masked_ssim(grey, 255 - grey, weight) < logos.MIN_SSIM
    # Where the logo is transparent there is nothing to compare.
    assert logos.masked_ssim(grey, 255 - grey, np.zeros_like(grey)) == 1.0


@FFMPEG
def test_a_take_whose_pixels_at_the_logo_are_not_the_asset_fails_with_the_frame(tmp_path):
    folder = tmp_path / "frames"
    folder.mkdir()
    names = []
    for index in range(6):
        Image.fromarray(_room((200, 190, 180), seed=index % 2)).save(folder / f"f{index}.png")
        names.append((index / 24, f"f{index}.png"))
    video = str(tmp_path / "take.mp4")
    asyncio.run(encoder.frames_to_mezzanine(names, end_time=6 / 24, out_path=video, fps=24, crf=12, preset="ultrafast", workdir=str(folder)))
    box = (240, 80, 400, 200)
    # What was drawn there, as the composite keeps it: stripes the take does not show.
    shape, _ = logos._thumbnail(np.zeros((H, W, 3), dtype=np.uint8), box)
    other = np.full(shape.shape, 255.0)
    other[:, ::4] = 0.0
    expected = [[(box, other, np.ones_like(other))] * 6]
    with pytest.raises(logos.LogoLostError, match=r"^logo lost: the brand logo at t=0\.00 s \(frame 0\) of the shot: what the take shows") as lost:
        asyncio.run(logos._verify(video, expected, [logos.Logo(_mark(), "the brand logo")], width=W, height=H, fps=24))
    assert lost.value.frame_jpeg.startswith(b"\xff\xd8")


# ── 4. The editor's preview build ─────────────────────────────────────────


def _preview_spec(**overlay) -> object:
    return parse_video_spec(
        {
            "title": "T",
            "scenes": [
                {
                    "id": "demo",
                    "type": "clip",
                    "clip_id": CLIP,
                    "overlays": [{"type": "screen_insert", "source": {"take_id": TAKE}, "quad": [list(p) for p in QUAD], **overlay}],
                }
            ],
        }
    )


def _preview_document() -> dict:
    payload = {"src": "insert/demo.0", "kind": "video", "quad": [list(p) for p in QUAD], "fit": "cover", "glow": 0.2, "t0": 0.5, "t1": 1.5, "time_map": [[0.5, 0.0], [1.5, 1.0]]}
    return {"scenes": [{"id": "demo", "type": "clip", "src": "clip/demo", "kind": "video", "time_map": [[0.0, 0.0], [3.0, 3.0]], "inserts": [payload]}]}


def test_a_preview_builds_each_insert_over_footage_and_its_digest_follows_what_it_draws():
    spec = _preview_spec(key={"color": "green"}, track=True)
    ids = {"clip/demo": "footage-file", "insert/demo.0": "take-file"}
    [build] = insert_builds.builds_of(_preview_document(), ids, spec)
    assert (build.footage_file, build.source_file, build.source_kind, build.track) == ("footage-file", "take-file", "video", True)
    assert build.key_spec == {"color": "green", "tolerance": 0.3, "feather": 1.5}
    assert build.digest() == insert_builds.InsertBuild(**{**build.__dict__}).digest()
    assert build.digest() != insert_builds.InsertBuild(**{**build.__dict__, "t1": 1.6}).digest()
    # Over a picture, or with a file the preview has not resolved, the stage maps the source as before.
    assert insert_builds.builds_of(_preview_document(), {"clip/demo": "footage-file"}, spec) == []


def test_a_build_drawn_plays_in_the_preview_and_one_missing_is_drawn_and_said_building(monkeypatch):
    spec = _preview_spec()
    played = SimpleNamespace(ids={"clip/demo": "footage-file", "insert/demo.0": "take-file"}, urls={})
    played.put = lambda key, url, identity: played.urls.__setitem__(key, url)
    reader = SimpleNamespace(id=uuid.uuid4())
    started: list[str] = []

    async def drawn(_digest):
        return {"duration": 1.0, "width": 960, "height": 540, "fps": 15, "frames": 15}

    async def missing(_digest):
        return None

    async def prepare(builds):
        started.extend(b.key for b in builds)
        return {b.digest() for b in builds}

    monkeypatch.setattr(media, "mint_insert_token", lambda digest, user_id: (f"tok-{digest[:6]}", 0))
    monkeypatch.setattr(insert_builds, "read_build", drawn)
    document = _preview_document()
    assert asyncio.run(insert_builds.attach(document, spec, played, reader, draw=True)) == set()
    assert document["scenes"][0]["inserts"][0]["build"] == {"fps": 15, "duration": 1.0, "t0": 0.5}
    assert "/api/v1/video-studio/insert-build/tok-" in played.urls["build/insert/demo.0"]
    monkeypatch.setattr(insert_builds, "read_build", missing)
    monkeypatch.setattr(insert_builds, "prepare", prepare)
    document = _preview_document()
    assert asyncio.run(insert_builds.attach(document, spec, played, reader, draw=True)) == {"demo"}
    assert started == ["insert/demo.0"] and "build" not in document["scenes"][0]["inserts"][0]


def test_a_keyed_or_tracked_insert_shown_without_its_build_is_said_approximate(monkeypatch: pytest.MonkeyPatch) -> None:
    played = SimpleNamespace(ids={"clip/demo": "footage-file", "insert/demo.0": "take-file"}, urls={})
    played.put = lambda key, url, identity: played.urls.__setitem__(key, url)
    reader = SimpleNamespace(id=uuid.uuid4())

    async def drawn(_digest: str) -> dict[str, float]:
        return {"duration": 1.0, "width": 960, "height": 540, "fps": 15, "frames": 15}

    async def missing(_digest: str) -> None:
        return None

    async def prepare(builds: list[insert_builds.InsertBuild]) -> set[str]:
        return {b.digest() for b in builds}

    monkeypatch.setattr(media, "mint_insert_token", lambda digest, user_id: (f"tok-{digest[:6]}", 0))
    monkeypatch.setattr(insert_builds, "prepare", prepare)
    monkeypatch.setattr(insert_builds, "read_build", missing)
    # Tracked, with no quads a render followed for it: said approximate.
    document = _preview_document()
    asyncio.run(insert_builds.attach(document, _preview_spec(track=True), played, reader, draw=True))
    scene = document["scenes"][0]
    assert scene["inserts"][0]["approximate"] is True and scene["preview_inserts_approximate"] is True
    # Keyed only: its key is tested in the page (preview_key), drawn as the render draws it, nothing said.
    document = _preview_document()
    asyncio.run(insert_builds.attach(document, _preview_spec(key={"color": "green"}), played, reader, draw=True))
    assert document["scenes"][0]["inserts"][0]["preview_key"]["rgb"] == [0, 177, 64]
    assert "preview_inserts_approximate" not in document["scenes"][0]
    # Its build drawn: drawn as the render draws it, nothing said.
    monkeypatch.setattr(insert_builds, "read_build", drawn)
    document = _preview_document()
    asyncio.run(
        insert_builds.attach(document, _preview_spec(key={"color": "green"}, track=True), played, reader, draw=True)
    )
    assert "approximate" not in document["scenes"][0]["inserts"][0]
    assert "preview_inserts_approximate" not in document["scenes"][0]
    # Neither keyed nor tracked: the mapped source is the render's picture, nothing said even without a build.
    monkeypatch.setattr(insert_builds, "read_build", missing)
    document = _preview_document()
    asyncio.run(insert_builds.attach(document, _preview_spec(), played, reader, draw=True))
    assert "preview_inserts_approximate" not in document["scenes"][0]


def test_an_insert_token_names_a_build_and_nothing_else(monkeypatch):
    from app.core.config import settings

    monkeypatch.setattr(settings, "secret_key", settings.secret_key or "test-secret-key-for-insert-tokens-0123456789")
    digest = "a" * 64
    reader = uuid.uuid4()
    token, _expires = media.mint_insert_token(digest, reader)
    assert media.decode_insert_token(token) == (digest, reader)
    figure, _ = media.mint_figure_token(digest, reader)
    with pytest.raises(media.MediaTokenError):
        media.decode_insert_token(figure)
    with pytest.raises(media.MediaTokenError):
        media.decode_figure_token(token)


@FFMPEG
def test_the_preview_build_is_the_render_s_compositor_keyed_moving_on_transparency(tmp_path, monkeypatch):
    # The footage: a green screen, a cat walking in from below; the source: a picture that changes each frame.
    folder = tmp_path / "frames"
    folder.mkdir()
    footage_frames, source_frames = [], []
    for index in range(24):
        room = Image.fromarray(_room(GREEN, seed=1))
        if index >= 12:
            ImageDraw.Draw(room).ellipse((260, 260 - 4 * (index - 12), 420, 460), fill=(60, 40, 30))
        room.save(folder / f"r{index:02d}.png")
        footage_frames.append((index / 24, f"r{index:02d}.png"))
        Image.new("RGB", (320, 240), (10 * index, 120, 220)).save(folder / f"s{index:02d}.png")
        source_frames.append((index / 24, f"s{index:02d}.png"))
    footage, source = str(tmp_path / "footage.mp4"), str(tmp_path / "source.mp4")
    for frames, out in ((footage_frames, footage), (source_frames, source)):
        asyncio.run(encoder.frames_to_mezzanine(frames, end_time=1.0, out_path=out, fps=24, crf=12, preset="ultrafast", workdir=str(folder)))

    async def download(file_id, base):
        target = str(base) + ".mp4"
        Path(target).write_bytes(Path(footage if file_id == "footage-file" else source).read_bytes())
        return target, SimpleNamespace(content_type="video/mp4")

    kept: dict[str, bytes] = {}

    class _Storage:
        async def upload_path(self, path, name, _type):
            kept[name] = Path(path).read_bytes()

        async def upload_to(self, data, name, _type):
            kept[name] = data

    from app.services import file_storage as storage_module

    monkeypatch.setattr(insert_builds, "_download", download)
    monkeypatch.setattr(storage_module, "file_storage", _Storage())
    build = insert_builds.InsertBuild(
        key="insert/demo.0",
        scene_id="demo",
        footage_file="footage-file",
        source_file="take-file",
        source_kind="video",
        quad=QUAD,
        t0=0.0,
        t1=1.0,
        scene_map=((0.0, 0.0), (1.0, 1.0)),
        source_map=((0.0, 0.0), (1.0, 1.0)),
        key_spec={"color": "green", "tolerance": 0.3, "feather": 1.5},
    )
    digest = build.digest()
    asyncio.run(insert_builds._draw(build, digest))
    meta = json.loads(kept[insert_builds.build_meta_object(digest)])
    assert (meta["fps"], meta["frames"], meta["width"]) == (insert_builds.BUILD_FPS, 15, insert_builds.BUILD_WIDTH)
    webm = tmp_path / "build.webm"
    webm.write_bytes(kept[insert_builds.build_object(digest)])
    probe = asyncio.run(encoder.run(encoder.ffprobe_path(), ["-v", "error", "-show_streams", str(webm)]))[0].decode()
    assert "codec_name=vp9" in probe and "alpha_mode=1" in probe.replace("TAG:alpha_mode", "alpha_mode")
    # Read back with its alpha: transparent off the surface, the source moving on it, the cat kept in front.
    width, height = meta["width"], meta["height"]
    raw = asyncio.run(
        encoder.run(encoder.ffmpeg_path(), ["-v", "error", "-c:v", "libvpx-vp9", "-i", str(webm), "-f", "rawvideo", "-pix_fmt", "rgba", "-"])
    )[0]
    frames = np.frombuffer(raw, dtype=np.uint8).reshape(-1, height, width, 4)
    assert len(frames) == 15
    sx, sy = width / W, height / H
    off, surface, cat = (int(40 * sx), int(40 * sy)), (int(340 * sx), int(110 * sy)), (int(340 * sx), int(245 * sy))
    assert frames[0][off[1], off[0], 3] < 10
    assert frames[0][surface[1], surface[0], 3] > 240 and abs(int(frames[0][surface[1], surface[0], 2]) - 220) < 25
    assert int(frames[12][surface[1], surface[0], 0]) - int(frames[0][surface[1], surface[0], 0]) > 80, "the source plays"
    late = frames[14][cat[1], cat[0]]
    assert late[3] > 240 and late[0] < 110 and late[2] < 90, f"the cat stays in front: {late}"


def _chart() -> Image.Image:
    """A source with structure a push changes: squares and a diagonal, no colour near the key."""
    chart = Image.new("RGB", (400, 300), (235, 235, 240))
    draw = ImageDraw.Draw(chart)
    for row in range(0, 300, 50):
        for col in range(0, 400, 50):
            if (row + col) // 50 % 2:
                draw.rectangle((col, row, col + 49, row + 49), fill=INSERT)
    draw.line((0, 0, 399, 299), fill=(200, 40, 40), width=9)
    return chart


@pytest.mark.chromium
@FFMPEG
def test_the_preview_build_and_the_render_draw_a_keyed_pushed_insert_alike_at_three_moments(tmp_path, monkeypatch):
    """#11754: the editor's preview of a keyed, pushed insert (its build over the footage, as the stage plays it)
    matches a frame of the render at the same time: before the push, mid-push with the cat in front, at the push's
    peak. SSIM over the insert's surface, the cat in front in both, no key colour left on the surface."""
    import os

    from app.services.video_studio import frame_qc
    from app.services.video_studio.spec import QUALITY_PRESETS

    folder = tmp_path / "frames"
    folder.mkdir()
    names = []
    for index in range(30):
        room = Image.fromarray(_room(GREEN, seed=1))
        if index >= 15:
            ImageDraw.Draw(room).ellipse((260, 200, 420, 400), fill=(60, 40, 30))
        room.save(folder / f"f{index:02d}.png")
        names.append((index / 30, f"f{index:02d}.png"))
    footage = str(tmp_path / "shot.mp4")
    asyncio.run(
        encoder.frames_to_mezzanine(
            names, end_time=1.0, out_path=footage, fps=30, crf=12, preset="ultrafast", workdir=str(folder)
        )
    )
    picture = tmp_path / "chart.png"
    _chart().save(picture)
    zoom = {"start": 0.2, "end": 0.8, "scale": 1.3, "focus": [0.5, 0.5]}

    # The preview: the build the editor's stage plays over the footage.
    async def download(file_id, base):
        source = footage if file_id == "footage-file" else str(picture)
        target = str(base) + Path(source).suffix
        Path(target).write_bytes(Path(source).read_bytes())
        return target, SimpleNamespace(content_type="video/mp4" if file_id == "footage-file" else "image/png")

    kept: dict[str, bytes] = {}

    class _Storage:
        async def upload_path(self, path, name, _type):
            kept[name] = Path(path).read_bytes()

        async def upload_to(self, data, name, _type):
            kept[name] = data

    from app.services import file_storage as storage_module

    monkeypatch.setattr(insert_builds, "_download", download)
    monkeypatch.setattr(storage_module, "file_storage", _Storage())
    build = insert_builds.InsertBuild(
        key="insert/shot.0",
        scene_id="shot",
        footage_file="footage-file",
        source_file="chart-file",
        source_kind="image",
        quad=QUAD,
        t0=0.0,
        t1=1.0,
        scene_map=((0.0, 0.0), (1.0, 1.0)),
        key_spec={"color": "green"},
        zoom=zoom,
    )
    digest = build.digest()
    asyncio.run(insert_builds._draw(build, digest))
    meta = json.loads(kept[insert_builds.build_meta_object(digest)])
    webm = tmp_path / "build.webm"
    webm.write_bytes(kept[insert_builds.build_object(digest)])
    raw = asyncio.run(
        encoder.run(
            encoder.ffmpeg_path(),
            ["-v", "error", "-c:v", "libvpx-vp9", "-i", str(webm), "-f", "rawvideo", "-pix_fmt", "rgba", "-"],
        )
    )[0]
    built = np.frombuffer(raw, dtype=np.uint8).reshape(-1, meta["height"], meta["width"], 4)

    # The render: the compositor page in Chromium, the insert prepared as ``render._prepare_inserts`` prepares it.
    key = "insert/shot.0"
    box = screen_insert.grown_box(QUAD, W / H)
    insert = screen_insert.InsertSource(
        str(picture),
        "image",
        QUAD,
        key=screen_insert.KeySpec.of({"color": "green"}),
        footage=screen_insert.Footage(footage, "video", 30.0, "clip/shot"),
        first_frame=0,
        plate=screen_insert.plate_frames(0, 29),
        label="Scene shot: its screen insert 0",
        box=tuple(box),
        zoom=screen_insert.Zoom.of(zoom),
    )
    first = asyncio.run(compositor.first_footage(insert, (W, H)))
    insert = screen_insert.keyed_on(insert, first)
    insert = dataclasses.replace(insert, subject=asyncio.run(compositor.read_subject(insert, 29)))
    assets = compositor.CompositorAssets(
        sources={"clip/shot": compositor.FrameSource(footage, 30.0)}, inserts={key: insert}
    )
    payload = {
        "src": key,
        "kind": "image",
        "quad": [list(p) for p in QUAD],
        "fit": "cover",
        "glow": 0.0,
        "t0": 0.0,
        "t1": 1.0,
    }
    payload.update({"matte": {"fps": 30.0, "frames": 30}, "box": [list(p) for p in box], "zoom": zoom})
    scene = {
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
    document = {
        "version": 1,
        "width": W,
        "height": H,
        "fps": 30,
        "duration": 1.0,
        "frames": 30,
        "fade_in": 0.0,
        "fade_out": 0.0,
        "theme": {},
        "captions": {"enabled": False, "style": "pill", "position": "bottom", "cues": []},
        "watermark": "none",
        "scenes": [scene],
    }
    try:
        video = asyncio.run(
            compositor.render_frames(
                document,
                assets,
                preset=QUALITY_PRESETS["draft"],
                output_size=(W, H),
                workdir=str(tmp_path / "render"),
                font_css="",
                pages=1,
                audits={},
            )
        )
    except Exception as exc:
        text = str(exc)
        if "Executable doesn't exist" not in text and "playwright install" not in text.lower():
            raise
        if os.environ.get("REQUIRE_CHROMIUM") == "1":
            pytest.fail(f"Playwright's Chromium is required here and is not installed: {exc}")
        pytest.skip("Playwright's Chromium is not installed (python -m playwright install chromium)")

    async def read(path: str) -> list[np.ndarray]:
        return [
            np.frombuffer(raw, dtype=np.uint8).reshape(H, W, 3)
            async for raw in encoder.iter_rgb_frames(path, width=W, height=H)
        ]

    rendered, plates = asyncio.run(read(video)), asyncio.run(read(footage))
    # The surface's inner rectangle, a few pixels in from the quad's edges: the insert alone, never the room.
    xs, ys = QUAD_PX[:, 0], QUAD_PX[:, 1]
    inner = (int(sorted(xs)[1]) + 6, int(sorted(ys)[1]) + 6, int(sorted(xs)[2]) - 6, int(sorted(ys)[2]) - 6)
    # Before the push (0.2 s), mid-push with the cat in front (0.6 s), at the push's peak (13/15 s).
    for index in (3, 9, 13):
        seconds = index / insert_builds.BUILD_FPS
        frame = round(seconds * 30)
        layer = Image.fromarray(built[index], "RGBA").resize((W, H), Image.Resampling.BILINEAR)
        preview = Image.alpha_composite(Image.fromarray(plates[frame]).convert("RGBA"), layer).convert("RGB")
        render = Image.fromarray(rendered[frame])
        similarity = frame_qc.ssim(preview.crop(inner), render.crop(inner))
        assert similarity > 0.8, f"{seconds:.2f} s: the preview and the render differ on the insert (SSIM {similarity})"
        for name, shot in (("preview", np.asarray(preview)), ("render", rendered[frame])):
            surface = shot[inner[1] : inner[3], inner[0] : inner[2]].astype(int)
            keyed = (surface[..., 1] > 140) & (surface[..., 0] < 60) & (surface[..., 2] < 110)
            assert keyed.mean() < 0.005, (
                f"{seconds:.2f} s, {name}: {100 * keyed.mean():.1f}% of the surface is key colour"
            )
            if frame >= 15:
                cat = shot[245, 340].astype(int)
                assert np.abs(cat - (60, 40, 30)).max() < 40, f"{seconds:.2f} s, {name}: the cat is covered: {cat}"
