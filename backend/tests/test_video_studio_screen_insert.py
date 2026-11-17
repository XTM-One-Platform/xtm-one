"""A picture or a video mapped onto a surface of the footage: the screen insert.

Synthetic data only: footage and pictures drawn here with Pillow, quads
written here, ids made up. What is proven:

* the spec - a ``screen_insert`` overlay takes a source and a quad whose
  corners go top left, top right, bottom right, bottom left around a convex
  surface, only on a scene that plays footage (an image scene's still among
  them), and its source is a reference of the video;
* the warp - each corner of the source lands on its corner of the quad,
  outside it is transparent, the edges are antialiased in premultiplied
  alpha (no dark fringe), a large source is reduced before it is sampled (no
  aliasing on fine detail), ``contain`` letterboxes in black, and the glow
  bleeds light beyond the quad;
* the planner - an insert plays its source from ``trim_start`` and holds its
  last frame, at its step on a recording, and one whose source is missing is
  left out with a note;
* the compositor - the frame server warps a video insert's decoded frame at
  the size the page asks, keeps the frame for its glow, counts it among the
  decoders and releases it with its scene;
* in Chromium, the page places the warped frame on the box the server drew.
"""

from __future__ import annotations

import io
import os
from types import SimpleNamespace

import numpy as np
import pytest
from PIL import Image
from pydantic import ValidationError

from app.services.video_studio import compositor, screen_insert, timeline
from app.services.video_studio.screen_insert import InsertSource
from app.services.video_studio.spec import (
    Overlay,
    pane_quads,
    parse_video_spec,
    quad_problems,
    referenced_ids,
    validate_video_spec,
)

CLIP = "00000000-0000-0000-0000-00000000c11b"
TAKE = "00000000-0000-0000-0000-0000000074ce"
PICTURE = "00000000-0000-0000-0000-00000000f11e"
#: A monitor seen a little from the left: wider at the top, its right edge shorter.
QUAD = [[0.30, 0.20], [0.80, 0.25], [0.78, 0.75], [0.32, 0.70]]


def _insert(**fields) -> dict:
    return {"type": "screen_insert", "source": {"clip_id": CLIP}, "quad": QUAD, "start": 0.5, "duration": 3, **fields}


def _spec(*scenes: dict) -> dict:
    return {"title": "Synthetic insert", "freestyle": True, "scenes": list(scenes)}


def _quadrants(width: int = 400, height: int = 300) -> Image.Image:
    """Four flat colours, one per quadrant: where each corner of the source lands is unmistakable."""
    picture = Image.new("RGB", (width, height))
    colours = {(0, 0): (255, 0, 0), (1, 0): (0, 255, 0), (1, 1): (0, 0, 255), (0, 1): (255, 255, 0)}
    for (qx, qy), colour in colours.items():
        picture.paste(colour, (qx * width // 2, qy * height // 2, (qx + 1) * width // 2, (qy + 1) * height // 2))
    return picture


def _jpeg(picture: Image.Image) -> bytes:
    buffer = io.BytesIO()
    picture.save(buffer, format="JPEG", quality=95)
    return buffer.getvalue()


# ── The spec ──────────────────────────────────────────────────────────────


def test_a_screen_insert_takes_one_source_and_a_quad():
    insert = Overlay.model_validate(_insert(trim_start=1.5, fit="contain", glow=0.4))
    assert insert.source.clip_id == CLIP and insert.quad[2] == (0.78, 0.75)
    assert (insert.trim_start, insert.fit, insert.glow) == (1.5, "contain", 0.4)
    with pytest.raises(ValidationError, match="exactly one of file_id, take_id, clip_id or asset"):
        Overlay.model_validate(_insert(source={"clip_id": CLIP, "take_id": TAKE}))
    with pytest.raises(ValidationError, match="exactly one of file_id, take_id, clip_id or asset"):
        Overlay.model_validate(_insert(source={"clip_id": CLIP, "asset": "logo"}))
    with pytest.raises(ValidationError, match="needs its source"):
        Overlay.model_validate({"type": "screen_insert", "quad": QUAD})
    with pytest.raises(ValidationError):
        Overlay.model_validate(_insert(quad=QUAD[:3]))
    with pytest.raises(ValidationError):
        Overlay.model_validate(_insert(glow=1.5))


def test_a_quad_goes_round_a_convex_surface_in_order():
    assert quad_problems([tuple(p) for p in QUAD]) == []
    # Given counter-clockwise (top left, bottom left, ...), or folded like a bow tie: refused, the order named.
    reversed_order = [QUAD[0], QUAD[3], QUAD[2], QUAD[1]]
    bow_tie = [QUAD[0], QUAD[1], QUAD[3], QUAD[2]]
    for quad in (reversed_order, bow_tie):
        with pytest.raises(ValidationError, match="top left, top right, bottom right, bottom left"):
            Overlay.model_validate(_insert(quad=quad))
    with pytest.raises(ValidationError, match="too small"):
        Overlay.model_validate(_insert(quad=[[0.5, 0.5], [0.51, 0.5], [0.51, 0.51], [0.5, 0.51]]))
    # A corner may stand outside the footage (the quality check says so), within a margin.
    Overlay.model_validate(_insert(quad=[[-0.1, 0.0], [1.05, 0.0], [1.0, 1.0], [0.0, 1.0]]))
    with pytest.raises(ValidationError):
        Overlay.model_validate(_insert(quad=[[-3, 0.0], [1.0, 0.0], [1.0, 1.0], [0.0, 1.0]]))


def test_the_fields_of_another_kind_of_overlay_are_dropped():
    insert = Overlay.model_validate(_insert(width=0.3, opacity=0.5, style="accent", subtext="x"))
    assert set(insert.model_dump(exclude_unset=True)) == {"type", "source", "quad", "start", "duration"}
    badge = Overlay.model_validate({"type": "badge", "text": "Live", "quad": QUAD, "glow": 0.5, "source": {"clip_id": CLIP}})
    assert set(badge.model_dump(exclude_unset=True)) == {"type", "text"}


def test_only_a_scene_that_plays_footage_takes_a_screen_insert():
    clip = {"type": "clip", "clip_id": CLIP, "overlays": [_insert()]}
    recording = {"type": "recording", "take_id": TAKE, "overlays": [_insert()]}
    statement = {"type": "statement", "lines": ["A line"], "backdrop": {"clip_id": CLIP}, "overlays": [_insert()]}
    # A still of a room with its screens is footage as a statement's picture backdrop is.
    image = {"type": "image", "file_id": PICTURE, "overlays": [_insert()]}
    validate_video_spec(_spec(clip, recording, statement, image))
    with pytest.raises(Exception, match="only clip, recording, image and statement"):
        validate_video_spec(_spec({"type": "title", "title": "Hello", "overlays": [_insert()]}))
    with pytest.raises(Exception, match="this statement has no backdrop"):
        validate_video_spec(_spec({"type": "statement", "lines": ["A line"], "overlays": [_insert()]}))


def test_an_inserts_source_is_a_reference_of_the_video():
    spec = validate_video_spec(
        _spec(
            {
                "type": "clip",
                "clip_id": CLIP,
                "overlays": [_insert(source={"take_id": TAKE}), _insert(source={"file_id": f"[[FILE:{PICTURE}]]"})],
            }
        )
    )
    refs = referenced_ids(spec)
    assert TAKE in refs["take"] and PICTURE in refs["file"] and CLIP in refs["clip"]


# ── The warp ──────────────────────────────────────────────────────────────


def test_each_corner_of_the_source_lands_on_its_corner_of_the_quad():
    size = (1000, 800)
    warped = screen_insert.warp(_quadrants(), [tuple(p) for p in QUAD], size, "cover")
    x0, y0, x1, y1 = warped.box
    assert (x0, y0, x1, y1) == screen_insert.bbox([tuple(p) for p in QUAD], *size) == (300, 160, 800, 600)
    pixels = np.asarray(warped.image)

    def near(corner: list[float], toward: tuple[float, float]) -> np.ndarray:
        # A point inside the quad, a few pixels from the corner toward its centre.
        x = corner[0] * size[0] + toward[0] * 12 - x0
        y = corner[1] * size[1] + toward[1] * 12 - y0
        return pixels[int(y), int(x)]

    assert tuple(near(QUAD[0], (1, 1))) == (255, 0, 0, 255)
    assert tuple(near(QUAD[1], (-1, 1))[[1, 3]]) == (255, 255)
    assert tuple(near(QUAD[2], (-1, -1))[[2, 3]]) == (255, 255)
    assert tuple(near(QUAD[3], (1, -1))[[0, 1, 3]]) == (255, 255, 255)
    # Outside the quad, inside its box: nothing.
    assert pixels[y1 - y0 - 1, 0][3] == 0


def test_the_edges_are_antialiased_in_premultiplied_alpha():
    white = Image.new("RGB", (300, 200), (255, 255, 255))
    warped = screen_insert.warp(white, [tuple(p) for p in QUAD], (1000, 800), "cover")
    pixels = np.asarray(warped.image).astype(int)
    alpha = pixels[..., 3]
    edge = (alpha > 0) & (alpha < 255)
    # A soft edge, one pixel or so wide all round...
    assert edge.sum() > 2 * (500 + 400)
    # ... whose colour stays the source's: sampled premultiplied, a fading edge never darkens to grey.
    assert pixels[edge][:, :3].min() >= 250


def test_a_large_source_is_reduced_before_it_is_sampled():
    # Single-pixel stripes, 4000 px wide, into a quad a tenth of that: sampled straight they alias into
    # bands; reduced first they average to an even grey.
    stripes = np.zeros((3000, 4000, 3), dtype=np.uint8)
    stripes[:, ::2] = 255
    warped = screen_insert.warp(Image.fromarray(stripes), [(0.1, 0.1), (0.5, 0.1), (0.5, 0.5), (0.1, 0.5)], (1000, 1000))
    interior = np.asarray(warped.image)[20:-20, 20:-20, 0].astype(float)
    assert abs(interior.mean() - 127.5) < 6 and interior.std() < 6


def test_contain_keeps_the_source_whole_on_transparency_never_a_black_card():
    # A wide source on a square surface: above and below it the surface shows through, the picture between.
    wide = Image.new("RGB", (800, 200), (0, 200, 0))
    square = [(0.2, 0.2), (0.6, 0.2), (0.6, 0.6), (0.2, 0.6)]
    pixels = np.asarray(screen_insert.warp(wide, square, (1000, 1000), "contain").image)
    middle = pixels.shape[1] // 2
    assert pixels[30, middle][3] == 0
    assert tuple(pixels[pixels.shape[0] // 2, middle]) == (0, 200, 0, 255)
    covered = np.asarray(screen_insert.warp(wide, square, (1000, 1000), "cover").image)
    assert tuple(covered[30, middle]) == (0, 200, 0, 255)


def test_the_glow_bleeds_light_beyond_the_quad():
    size = (1600, 900)
    warped = screen_insert.warp(Image.new("RGB", (320, 180), (255, 240, 200)), [tuple(p) for p in QUAD], size)
    bloom = np.asarray(screen_insert.glow(warped, size))
    assert bloom.shape[:2] == (113, 200)
    x0, y0, _x1, y1 = (v / screen_insert.GLOW_SCALE for v in warped.box)
    # Light outside the quad's box, fading with the distance from it, gone far away.
    row = int((y0 + y1) / 2)
    assert bloom[row, int(x0) - 2][3] > bloom[row, int(x0) - 6][3] > 0
    assert bloom[row, 0][3] == 0


def test_a_quad_off_the_host_draws_nothing():
    assert screen_insert.warp(_quadrants(), [(1.2, 0.1), (1.5, 0.1), (1.5, 0.4), (1.2, 0.4)], (800, 450)) is None
    assert screen_insert.host_size("3200", "9000") == (1456, 4096)
    assert screen_insert.host_size("0", "10") is None and screen_insert.host_size("x", "10") is None


# ── The planner ───────────────────────────────────────────────────────────


def _clip_media(duration: float = 6.0) -> timeline.ClipMedia:
    return timeline.ClipMedia(duration=duration, width=1920, height=1080, fps=24.0, frame_count=int(duration * 24))


def test_an_insert_plays_its_source_from_its_trim_and_holds_its_last_frame():
    spec = parse_video_spec(
        validate_video_spec(
            _spec({"id": "shot", "type": "clip", "clip_id": CLIP, "overlays": [_insert(trim_start=2.0, glow=0.3)]})
        )
    )
    key = timeline.insert_key("shot", 0)
    media = timeline.PlanMedia(
        clips={"shot": _clip_media(8.0)}, inserts={key: _clip_media(4.0)}, inserts_gathered=True
    )
    scene = timeline.plan(spec, media, fps=30, theme={}).document["scenes"][0]
    (drawn,) = scene["inserts"]
    assert (drawn["src"], drawn["kind"], drawn["fit"], drawn["glow"]) == ("insert/shot.0", "video", "cover", 0.3)
    assert (drawn["t0"], drawn["t1"]) == (0.5, 3.5)
    # 2 s of a 4 s source play from its trim, then its last frame holds to the end of the insert.
    assert drawn["time_map"] == [[0.5, 2.0], [2.5, 4.0], [3.5, 4.0]]
    assert drawn["quad"] == QUAD
    # An insert is part of the picture: never one of the overlays laid out above it.
    assert scene["overlays"] == []


def test_an_insert_whose_source_is_missing_is_left_out_and_said():
    spec = parse_video_spec(validate_video_spec(_spec({"id": "shot", "type": "clip", "clip_id": CLIP, "overlays": [_insert()]})))
    plan = timeline.plan(spec, timeline.PlanMedia(clips={"shot": _clip_media()}, inserts_gathered=True), fps=30, theme={})
    assert "inserts" not in plan.document["scenes"][0]
    assert any("screen insert 0 is left out" in note for note in plan.notes)
    # An estimate of the timeline plans no insert at all.
    estimate = timeline.plan(spec, timeline.PlanMedia(clips={"shot": _clip_media()}), fps=30, theme={})
    assert "inserts" not in estimate.document["scenes"][0] and not estimate.notes


def test_a_picture_insert_and_a_recording_step():
    spec = parse_video_spec(
        validate_video_spec(
            _spec(
                {
                    "id": "demo",
                    "type": "recording",
                    "take_id": TAKE,
                    "overlays": [_insert(source={"file_id": PICTURE}, start=0.0, at_step=1)],
                }
            )
        )
    )
    take = timeline.TakeMedia(
        take_id=TAKE,
        duration=6.0,
        css_width=1600,
        css_height=900,
        pixel_ratio=1.0,
        fps=60,
        frame_count=360,
        events=[
            {"step": 0, "action": "click", "t0": 0.5, "ta": 1.0, "t1": 1.4},
            {"step": 1, "action": "click", "t0": 3.0, "ta": 3.5, "t1": 3.9},
        ],
    )
    key = timeline.insert_key("demo", 0)
    media = timeline.PlanMedia(
        takes={TAKE: take}, inserts={key: timeline.ImageMedia(1280, 720)}, inserts_gathered=True
    )
    (drawn,) = timeline.plan(spec, media, fps=30, theme={}).document["scenes"][0]["inserts"]
    assert (drawn["kind"], drawn["src_w"], drawn["src_h"]) == ("image", 1280, 720)
    assert "time_map" not in drawn
    # It comes in when step 1 acts on screen, like a callout at that step.
    assert drawn["t0"] > 1.0


# ── A wall of panes ───────────────────────────────────────────────

WALL_SOURCES = [{"clip_id": CLIP}, {"take_id": TAKE}, {"file_id": PICTURE}]


def _wall(**fields) -> dict:
    insert = _insert(**{"panes": 3, "sources": WALL_SOURCES, **fields})
    insert.pop("source")
    return insert


def test_a_wall_takes_one_source_per_pane_on_one_quad():
    wall = Overlay.model_validate(_wall(gutter=0.02))
    assert wall.grid == (1, 3) and wall.pane_gutter == 0.02 and [p for p, _s, _q in wall.insert_panes()] == [0, 1, 2]
    assert [s.clip_id or s.take_id or s.file_id for s in wall.shown_sources] == [CLIP, TAKE, PICTURE]
    grid = Overlay.model_validate(_wall(panes=[2, 2], sources=[*WALL_SOURCES, {"clip_id": CLIP}]))
    assert grid.grid == (2, 2) and len(grid.insert_panes()) == 4 and grid.pane_gutter == 0.015
    # One insert, one source, as before: no panes, the whole quad.
    single = Overlay.model_validate(_insert())
    assert single.grid is None and single.insert_panes() == [(None, single.source, [tuple(p) for p in QUAD])]
    refused = {
        "3 x 1 panes|1 x 3 panes": _wall(sources=WALL_SOURCES[:2]),
        "drop source": {**_wall(), "source": {"clip_id": CLIP}},
        "give panes": _insert(sources=WALL_SOURCES),
        "gap between a wall's panes": _insert(gutter=0.02),
        "2 to 16 panes": _wall(panes=[7, 1], sources=WALL_SOURCES * 3),
    }
    for message, raw in refused.items():
        with pytest.raises(ValidationError, match=message):
            Overlay.model_validate(raw)
    with pytest.raises(ValidationError):
        Overlay.model_validate(_wall(gutter=0.5))


def test_the_panes_split_the_wall_in_perspective_with_a_gutter():
    flat = [(0.1, 0.1), (0.7, 0.1), (0.7, 0.4), (0.1, 0.4)]
    panes = pane_quads(flat, 1, 3, 0.02)
    # Each pane is (1 - 2 x 0.02) / 3 of the wall's width, 0.02 apart; the wall's own edges have no gutter.
    width = 0.6 * (1 - 0.04) / 3
    assert [round(pane[0][0], 4) for pane in panes] == [0.1, round(0.1 + width + 0.012, 4), round(0.1 + 2 * (width + 0.012), 4)]
    assert panes[0][0] == flat[0] and panes[2][1] == flat[1] and panes[2][2] == flat[2] and panes[0][3] == flat[3]
    assert all(quad_problems(pane) == [] for pane in panes)
    # A wall seen from the left (its right edge shorter): the far panes are drawn narrower - the split is the
    # surface's, not the footage's, where three equal thirds would stretch the near pane's picture.
    angled = [(0.2, 0.2), (0.8, 0.3), (0.8, 0.7), (0.2, 0.8)]
    seen = pane_quads(angled, 1, 3, 0.0)
    tops = [pane[1][0] - pane[0][0] for pane in seen]
    assert tops[0] > tops[1] > tops[2] and tops[0] > 0.2 > tops[2]
    assert seen[0][0] == angled[0] and seen[2][2] == angled[2]
    rows = pane_quads(flat, 2, 2, 0.0)
    assert rows[0][2] == rows[3][0] == (0.4, 0.25)


def test_a_wall_is_planned_pane_by_pane():
    spec = parse_video_spec(validate_video_spec(_spec({"id": "shot", "type": "clip", "clip_id": CLIP, "overlays": [_wall()]})))
    parts = timeline.insert_sources(spec)
    assert list(parts) == ["insert/shot.0.0", "insert/shot.0.1", "insert/shot.0.2"]
    assert [(p.kind, p.ref, p.index, p.pane) for p in parts.values()] == [
        ("clip", CLIP, 0, 0),
        ("take", TAKE, 0, 1),
        ("file", PICTURE, 0, 2),
    ]
    assert timeline.insert_scene("insert/shot.0.2") == "shot" and timeline.insert_label("insert/shot.0.2") == "its screen insert 0 (pane 3)"
    refs = referenced_ids(validate_video_spec(_spec({"type": "clip", "clip_id": CLIP, "overlays": [_wall()]})))
    assert TAKE in refs["take"] and PICTURE in refs["file"]
    # Pane 2's source could not be read: the other two are drawn, each on its own part of the quad.
    media = timeline.PlanMedia(
        clips={"shot": _clip_media(8.0)},
        inserts={"insert/shot.0.0": _clip_media(4.0), "insert/shot.0.2": timeline.ImageMedia(1280, 720)},
        inserts_gathered=True,
    )
    plan = timeline.plan(spec, media, fps=30, theme={})
    drawn = plan.document["scenes"][0]["inserts"]
    quads = pane_quads([tuple(p) for p in QUAD], 1, 3, 0.015)
    assert [d["src"] for d in drawn] == ["insert/shot.0.0", "insert/shot.0.2"]
    assert [d["quad"] for d in drawn] == [[list(p) for p in quads[0]], [list(p) for p in quads[2]]]
    assert all((d["t0"], d["t1"]) == (0.5, 3.5) for d in drawn)
    assert any("its screen insert 0 (pane 2) is left out" in note for note in plan.notes)


# ── The compositor ────────────────────────────────────────────────────────


class _Route:
    def __init__(self, url: str) -> None:
        self.request = SimpleNamespace(url=url)
        self.answers: list[dict] = []

    async def fulfill(self, **answer) -> None:
        self.answers.append(answer)

    async def abort(self, reason: str) -> None:
        self.answers.append({"aborted": reason})


async def test_the_frame_server_warps_a_video_inserts_frame_at_the_size_asked(monkeypatch, tmp_path):
    footage = _jpeg(_quadrants(640, 360))
    asked: list[int] = []

    async def frame(self, index: int) -> bytes:
        asked.append(index)
        return footage

    monkeypatch.setattr(compositor._SceneDecoder, "frame", frame)
    insert = InsertSource(str(tmp_path / "take.mp4"), "video", tuple(tuple(p) for p in QUAD), fps=30.0, glow=0.5)
    server = compositor.FrameServer(compositor.CompositorAssets(inserts={"insert/shot.0": insert}), "<html></html>")
    route = _Route(f"{compositor.ORIGIN}/insert/shot.0/12.png?w=1000&h=800")
    await server.handle(route)
    (answer,) = route.answers
    assert answer["status"] == 200 and answer["content_type"] == "image/png"
    drawn = Image.open(io.BytesIO(answer["body"]))
    assert drawn.size == (500, 440) and drawn.mode == "RGBA"
    # Its glow is the same frame's, drawn once: the decoder is not asked again.
    glow = _Route(f"{compositor.ORIGIN}/insert/shot.0/12.glow.png?w=1000&h=800")
    await server.handle(glow)
    assert Image.open(io.BytesIO(glow.answers[0]["body"])).size == (125, 100)
    assert asked == [12]
    assert server.open_decoders == 1
    await server.release(["insert/shot.0"])
    assert server.open_decoders == 0
    # Nothing is served for a size the page did not give, nor for an insert the render does not have.
    for url in ("insert/shot.0/12.png", "insert/other.0/1.png?w=10&h=10"):
        missing = _Route(f"{compositor.ORIGIN}/{url}")
        await server.handle(missing)
        assert missing.answers[0]["status"] == 404


async def test_a_picture_insert_is_read_once_and_has_no_glow_unless_asked(tmp_path):
    path = tmp_path / "poster.png"
    _quadrants().save(path)
    insert = InsertSource(str(path), "image", tuple(tuple(p) for p in QUAD))
    server = compositor.FrameServer(compositor.CompositorAssets(inserts={"insert/a.1": insert}), "<html></html>")
    route = _Route(f"{compositor.ORIGIN}/insert/a.1/0.png?w=800&h=450")
    await server.handle(route)
    assert route.answers[0]["status"] == 200
    glow = _Route(f"{compositor.ORIGIN}/insert/a.1/0.glow.png?w=800&h=450")
    await server.handle(glow)
    assert glow.answers[0]["status"] == 404


def test_a_video_insert_counts_among_the_decoders_and_is_released_with_its_scene():
    document = {
        "fps": 30,
        "scenes": [
            {
                "id": "a",
                "type": "clip",
                "src": "clip/a",
                "start": 0.0,
                "end": 4.0,
                "inserts": [{"src": "insert/a.0", "kind": "video"}, {"src": "insert/a.1", "kind": "image"}],
            },
            {"id": "b", "type": "clip", "src": "clip/b", "start": 3.5, "end": 8.0},
        ],
    }
    # The clip, its video insert and the next clip during the crossfade: three decoders.
    assert compositor.decoder_budget(document) == 3
    assert compositor.release_schedule(document) == [(121, ["clip/a", "insert/a.0", "insert/a.1"])]


# ── In Chromium: the page places the frame on the server's box ─────────────


@pytest.mark.chromium
async def test_the_page_places_the_warped_frame_on_the_box_the_server_drew(tmp_path):
    try:
        from playwright.async_api import async_playwright
    except ImportError:  # pragma: no cover - the backend image ships Playwright
        pytest.skip("Playwright is not installed")
    from app.services.headless_render import RENDER_CHROMIUM_ARGS
    from app.services.video_studio.compositor_page import ORIGIN, page_html

    path = tmp_path / "poster.png"
    _quadrants().save(path)
    insert = InsertSource(str(path), "image", tuple(tuple(p) for p in QUAD))
    server = compositor.FrameServer(compositor.CompositorAssets(inserts={"insert/shot.0": insert}), page_html(""))
    footage = _jpeg(Image.new("RGB", (640, 360), (40, 40, 40)))

    async def handle(route) -> None:
        if "/clip/" in route.request.url:
            await route.fulfill(status=200, body=footage, content_type="image/jpeg")
        else:
            await server.handle(route)

    document = {
        "version": 1,
        "width": 640,
        "height": 360,
        "fps": 30,
        "duration": 2.0,
        "frames": 60,
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
                "time_map": [[0, 0], [2, 2]],
                "src_fps": 30,
                "src_frames": 60,
                "src_w": 640,
                "src_h": 360,
                "transition_in": {"type": "cut", "duration": 0},
                "overlays": [],
                "inserts": [
                    {"src": "insert/shot.0", "kind": "image", "quad": QUAD, "fit": "cover", "glow": 0.0, "t0": 0.0, "t1": 2.0}
                ],
                "start": 0.0,
                "end": 2.0,
            }
        ],
    }
    try:
        async with async_playwright() as pw:
            browser = await pw.chromium.launch(headless=True, args=list(RENDER_CHROMIUM_ARGS))
            try:
                context = await browser.new_context(viewport={"width": 640, "height": 360}, device_scale_factor=2)
                await context.route("**/*", handle)
                page = await context.new_page()
                await page.goto(f"{ORIGIN}/index.html", wait_until="load")
                await page.evaluate("doc => window.__xtm.load(doc)", document)
                await page.evaluate("i => window.__xtm.render(i)", 10)
                placed = await page.evaluate(
                    "() => { const r = document.querySelector('img.ins').getBoundingClientRect();"
                    " return [r.left, r.top, r.width, r.height]; }"
                )
            finally:
                await browser.close()
    except Exception as exc:
        text = str(exc)
        if "Executable doesn't exist" not in text and "playwright install" not in text.lower():
            raise
        if os.environ.get("REQUIRE_CHROMIUM") == "1":
            pytest.fail(f"Playwright's Chromium is required here and is not installed: {exc}")
        pytest.skip("Playwright's Chromium is not installed (python -m playwright install chromium)")
    # The host is the whole 640x360 frame at a device scale of 2: the server drew the box at 1280x720.
    x0, y0, x1, y1 = screen_insert.bbox([tuple(p) for p in QUAD], 1280, 720)
    assert placed == pytest.approx([x0 / 2, y0 / 2, (x1 - x0) / 2, (y1 - y0) / 2], abs=0.01)
