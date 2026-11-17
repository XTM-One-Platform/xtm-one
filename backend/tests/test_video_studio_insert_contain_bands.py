"""A keyed contain insert fills its bands, the edge of what stands in front keeps no halo of the key, and the
residual-key advice never loops the author on key settings.

On synthetic footage, no provider, no browser:

* a green screen the model painted toward cyan (hue 150 degrees): the clamp despill turns it cyan, still the key's
  hue to the gates. Left in the bands a contain fit leaves inside the quad, it failed the residual-key gate at the
  same share whatever the tolerance; filled under the source, the bands show the source's own edge colour, the
  bezel's dark colour or black, and the insert's report says the band's share and what filled it - a mark on
  transparency keeps its board;
* hair crossing that screen: the clamp left cyan on its soft edge at any feather, and the edge despill takes it off;
  the hue the footage itself has on what stands in front is neither despilled nor counted by the halo gate;
* a residual key in the contain bands, or one inside the quad an earlier drawing at other key settings left the
  same, is said so - never as a tolerance to widen; each drawing keeps what its keyed inserts left for the next.
"""

from __future__ import annotations

import dataclasses

import numpy as np
from PIL import Image, ImageDraw, ImageFilter

from app.services.video_studio import compositor, insert_checks, render_checks, screen_insert, screens
from app.services.video_studio.render_checks import INSERT_HALO, INSERT_RESIDUAL_KEY
from app.services.video_studio.screen_insert import Footage, InsertSource, KeySpec

W, H = 960, 540
#: A "chroma green" the model painted leaning to cyan: the clamp despill leaves (30, 130, 130), hue 180, within the
#: gates' 30 degrees of the key's 150.
CYANISH = (30, 230, 130)
BEZEL = (28, 28, 32)
#: The monitor's screen in the footage, pixels: a 4:3 quad, so a 16:9 source fitted inside leaves a quarter of it.
SCREEN = (300, 130, 660, 400)
KEY = "insert/s1.0"
LABEL = "Scene s1: its screen insert 0"
EDGE = (24, 28, 36)


def _quad() -> tuple:
    x0, y0, x1, y1 = SCREEN
    return tuple((x / W, y / H) for x, y in ((x0, y0), (x1, y0), (x1, y1), (x0, y1)))


QUAD = _quad()


def _room(*, seed: int, shade: tuple = CYANISH, bezel: tuple = BEZEL, hair: int = 0, hair_colour: tuple = (95, 70, 45)) -> np.ndarray:
    """A noisy wall, the monitor's bezel, its screen keyed in *shade*; *hair* px of a head rising into the screen
    through its bottom edge, strands and all, blended into the screen over a few pixels as a lens and the encoder do."""
    rng = np.random.default_rng(seed)
    picture = Image.fromarray((rng.random((H, W, 3)) * 20 + 95).astype(np.uint8))
    draw = ImageDraw.Draw(picture)
    x0, y0, x1, y1 = SCREEN
    draw.rectangle((x0 - 8, y0 - 8, x1 + 8, y1 + 8), fill=bezel)
    draw.rectangle((x0, y0, x1, y1), fill=shade)
    picture = picture.filter(ImageFilter.GaussianBlur(1.0))
    if hair:
        top = y1 - hair
        head = Image.new("L", (W, H), 0)
        mask = ImageDraw.Draw(head)
        mask.ellipse((430, top, 550, top + 200), fill=255)
        strands = np.random.default_rng(99)
        for _ in range(60):
            sx = int(strands.integers(425, 555))
            mask.line((sx, top + 30, sx + int(strands.integers(-25, 25)), top - int(strands.integers(5, 40))), fill=255, width=2)
        picture = Image.composite(Image.new("RGB", (W, H), hair_colour), picture, head.filter(ImageFilter.GaussianBlur(4.0)))
    return np.asarray(picture).copy()


def _insert(tolerance: float = 0.4, feather: float = 3.0, *, fit: str = "contain") -> InsertSource:
    return InsertSource(
        "p.png",
        "image",
        QUAD,
        fit=fit,
        key=KeySpec("green", tolerance, feather),
        footage=Footage("f", "video", 24.0, "clip/s1"),
        box=tuple(screen_insert.grown_box(QUAD, W / H)),
        label=LABEL,
    )


def _screenshot(edge: tuple = EDGE) -> Image.Image:
    """A 16:9 dashboard on an opaque dark ground: its edge one flat colour."""
    picture = Image.new("RGB", (1600, 900), edge)
    draw = ImageDraw.Draw(picture)
    for row in range(80, 820, 40):
        draw.rectangle((120, row, 1480, row + 14), fill=(210, 215, 225))
    return picture


def _photo() -> Image.Image:
    """A 16:9 opaque picture whose edges are no one colour: a gradient across it."""
    ramp = np.linspace(40, 220, 1600, dtype=np.float32)[None, :, None].repeat(900, axis=0).repeat(3, axis=2)
    ramp[..., 2] = 120
    return Image.fromarray(ramp.astype(np.uint8), "RGB")


def _mark() -> Image.Image:
    """A 16:9 mark on transparency: nothing around its emblem."""
    picture = Image.new("RGBA", (1600, 900), (0, 0, 0, 0))
    ImageDraw.Draw(picture).ellipse((600, 250, 1000, 650), fill=(15, 188, 255, 255))
    return picture


def _static(insert: InsertSource, frames: list[np.ndarray], picture: Image.Image) -> list:
    """Each frame of a static keyed insert as the frame server draws it: its fill and its surface read once."""
    insert = screen_insert.keyed_on(insert, frames[0])
    fill = screen_insert.static_fill(insert, frames[0], (W, H))
    assert fill is not None
    surface = screen_insert.Surface.of(frames[0], [(x * W, y * H) for x, y in insert.quad])
    state = screen_insert.Matting()
    return [
        screen_insert.compose(insert, picture, (W, H), n, footage=frame, surface=surface, fill=fill, state=state, window_frame=n)
        for n, frame in enumerate(frames)
    ]


def _keyed(insert: InsertSource, frames: list[np.ndarray], picture: Image.Image) -> list:
    """Each frame keyed frame by frame (a tracked insert's path)."""
    insert = screen_insert.keyed_on(insert, frames[0])
    state = screen_insert.Matting()
    return [screen_insert.compose(insert, picture, (W, H), n, footage=frame, state=state, window_frame=n) for n, frame in enumerate(frames)]


def _at(layer, x: int, y: int) -> np.ndarray:
    x0, y0, _x1, _y1 = layer.warped.box
    return np.asarray(layer.warped.image, dtype=int)[y - y0, x - x0, :3]


def _placed(insert: InsertSource) -> dict:
    return {
        KEY: insert_checks.Placed(
            KEY, "s1", LABEL, 0.0, 1.0, keyed=True, fit=insert.fit, key_settings=insert_checks.key_settings(insert.key)
        )
    }


def _audits(layers: list) -> dict:
    return {KEY: {"frames": {str(n): layer.audit.as_dict() for n, layer in enumerate(layers)}, "evidence": {}}}


# ── 1. The contain bands ──────────────────────────────────────────────────


def test_the_contain_bands_of_a_keyed_insert_show_the_source_s_edge_never_the_surface():
    frames = [_room(seed=10 + n) for n in range(4)]
    for tolerance in (0.4, 0.88):
        for layers in (_static(_insert(tolerance), frames, _screenshot()), _keyed(_insert(tolerance), frames, _screenshot())):
            audit = layers[-1].audit
            assert audit.residual_key == 0.0 and audit.residual_where is None
            # A 16:9 source in a 4:3 quad: a quarter of it is band, filled with the screenshot's own dark edge.
            assert abs(audit.band_share - 0.25) < 0.01 and audit.band_fill == "source edge" and audit.band_colour == "#181C24"
            top, bottom = (480, SCREEN[1] + 8), (480, SCREEN[3] - 8)
            assert np.abs(_at(layers[-1], *top) - EDGE).max() <= 3, _at(layers[-1], *top)
            assert np.abs(_at(layers[-1], *bottom) - EDGE).max() <= 3, _at(layers[-1], *bottom)


def test_left_to_the_surface_the_bands_failed_at_one_share_whatever_the_tolerance_and_are_said_so(monkeypatch):
    # As it was: under the source, the surface with the key clamped out - cyan on this screen.
    monkeypatch.setattr(screen_insert, "_backdrop", lambda flat, *_rest: (flat, None, None))
    frames = [_room(seed=20)]
    shares = set()
    for tolerance in (0.4, 0.6, 0.88):
        insert = _insert(tolerance)
        [layer] = _static(insert, frames, _screenshot())
        assert set(layer.audit.residual_where) == {"contain"}, layer.audit.residual_where
        assert screen_insert.worst_side(layer.audit.residual_where) in ("top", "bottom")
        shares.add(layer.audit.residual_key)
    assert len(shares) == 1 and next(iter(shares)) > 0.2, "no key setting reached the bands"
    found = [finding for finding, _proof in insert_checks.findings(_audits([layer]), _placed(insert), fps=24) if finding.kind == INSERT_RESIDUAL_KEY]
    [finding] = found
    assert "in the contain bands inside the quad along its" in finding.message
    assert "the surface showing through the contain bands: use a source of the quad's proportions" in finding.message
    assert "widen the key's tolerance" not in finding.message and finding.data["action"]["kind"] == insert_checks.REPLACE_SOURCE


def test_a_varied_edge_fills_with_the_bezel_s_dark_colour_a_light_bezel_with_black_and_a_mark_keeps_its_board():
    frames = [_room(seed=30 + n) for n in range(3)]
    [*_rest, photo] = _static(_insert(), frames, _photo())
    assert photo.audit.band_fill == "bezel" and photo.audit.residual_key == 0.0
    assert np.abs(_at(photo, 480, SCREEN[1] + 8) - BEZEL).max() <= 6, _at(photo, 480, SCREEN[1] + 8)
    # A bezel too light to stand for the screen's black: black, never the key's hue.
    light = [_room(seed=40 + n, bezel=(200, 200, 205)) for n in range(3)]
    [*_rest, white] = _keyed(_insert(), light, _photo())
    assert white.audit.band_fill == "black" and white.audit.band_colour == "#000000"
    assert _at(white, 480, SCREEN[1] + 8).max() <= 6
    # A mark on transparency keeps the keyed surface around it - its board, never a card - as it always did; what
    # the clamp leaves of this screen there is said where it sits, with a fix that is not the key.
    [*_rest, mark] = _static(_insert(), frames, _mark())
    assert mark.audit.band_fill == "surface" and mark.audit.band_colour is None
    assert _at(mark, 480, SCREEN[1] + 8).mean() > 60, "a dark card behind the mark"
    assert "contain" in (mark.audit.residual_where or {})
    chosen = insert_checks._residual_action(mark.audit.residual_where)
    assert chosen["kind"] == insert_checks.REPLACE_SOURCE and "opaque" in chosen["text"]


def test_the_report_and_the_answer_say_the_band_and_what_filled_it():
    insert = _insert()
    layers = _static(insert, [_room(seed=50 + n) for n in range(3)], _screenshot())
    [report] = insert_checks.reports(_audits(layers), _placed(insert), fps=24)
    assert report["contain_band"]["filled_with"] == "source edge" and report["contain_band"]["colour"] == "#181C24"
    assert abs(report["contain_band"]["share"] - 0.25) < 0.01
    assert report["key_settings"] == {"color": "green", "tolerance": 0.4, "feather": 3.0} and report["residual_inside"] == 0.0
    [line] = render_checks.insert_lines(render_checks.Checks(inserts=[report]).as_dict())
    assert "% of the quad, filled with the source's own edge colour #181C24" in line and "contain bands " in line
    # Unkeyed, the footage shows in the bands, as it always did, and the report says so.
    plain = screen_insert.compose(dataclasses.replace(insert, key=None, box=None), _screenshot(), (W, H))
    assert plain.audit.band_fill == "footage" and plain.audit.band_colour is None and abs(plain.audit.band_share - 0.25) < 0.01


# ── 2. The edge of what stands in front ───────────────────────────────────


def _ui() -> Image.Image:
    picture = Image.new("RGB", (400, 300), EDGE)
    draw = ImageDraw.Draw(picture)
    for row in range(30, 270, 20):
        draw.rectangle((30, row, 370, row + 8), fill=(210, 215, 225))
    return picture


def test_hair_crossing_the_screen_keeps_no_halo_whatever_the_feather(monkeypatch):
    frames = [_room(seed=60 + n, hair=70 if n else 0) for n in range(6)]
    for feather in (3.0, 10.0):
        layers = _keyed(_insert(0.4, feather, fit="cover"), frames, _ui())
        assert all(layer.audit.halo is not None and layer.audit.halo <= screen_insert.MAX_HALO_SHARE for layer in layers[1:]), [
            layer.audit.halo for layer in layers
        ]
        assert layers[-1].audit.edge_despill > 0 and layers[-1].audit.halo_own == 0.0
        found = insert_checks.findings(_audits(layers), _placed(_insert(0.4, feather, fit="cover")), fps=24)
        assert INSERT_HALO not in {finding.kind for finding, _proof in found}
    # Without the edge pass the clamp leaves the key's hue on the hair's soft edge at every feather: the report.
    monkeypatch.setattr(screen_insert, "edge_despill", lambda colours, edge, foreground, key, shows=None: (colours, None))
    for feather in (3.0, 10.0):
        layers = _keyed(_insert(0.4, feather, fit="cover"), frames, _ui())
        assert all(layer.audit.halo > screen_insert.MAX_HALO_SHARE for layer in layers[1:]), [layer.audit.halo for layer in layers]


def _standing(collar: tuple) -> tuple[np.ndarray, np.ndarray, screens.Key]:
    """A person standing in front of the screen from row 60 down, brown, with a *collar* of their own colour on their
    left; the screen above them."""
    key = screens.Key("green", CYANISH)
    region = np.empty((200, 300, 3), dtype=np.float32)
    region[:] = CYANISH
    foreground = np.zeros((200, 300), dtype=bool)
    foreground[60:, 40:260] = True
    region[foreground] = (95, 70, 45)
    region[60:, 40:90] = collar
    return region, foreground, key


def test_the_hue_the_footage_itself_has_is_neither_despilled_nor_blamed_on_the_insert():
    # A collar the scene lights green (hue 131, within the gate's 30 degrees of the key's 150), and a cyan fringe the
    # clamp left on the brown hair beside it.
    region, foreground, key = _standing((40, 150, 60))
    raw = region.copy()
    raw[58:64, 100:260] = (60, 150, 90)
    edge = screen_insert.edge_of(raw, foreground, key, 3.0)
    assert edge is not None
    # The collar's green runs on into the person at its saturation; the fringe falls off into the brown hair.
    assert edge.own_key[61, 60] and not edge.own_key[61, 200], "the collar's edge is the footage's own colour, the fringe on the hair is not"
    drawn = raw.copy()
    drawn[58:64, 100:260] = (30, 130, 130)
    cleaned, share = screen_insert.edge_despill(drawn, edge, foreground, key)
    assert share is not None and share > 0
    assert np.abs(cleaned[61, 200] - (95, 70, 45)).max() < 15, cleaned[61, 200]
    assert np.array_equal(cleaned[61, 60], drawn[61, 60]), "the collar keeps its own green"
    # The gate, on the frame as the audience would see it: the insert around the person, the collar's own green on its
    # edge. Counted, the collar was blamed on the insert; left out, nothing is - and how much was left out is said.
    seen = region.copy()
    seen[~foreground] = EDGE
    hsv = screen_insert._hsv(seen)
    inside = np.ones(foreground.shape, dtype=bool)
    blamed = screen_insert._halo_share(hsv, foreground, inside, key, 1.0).share
    read = screen_insert._halo_share(hsv, foreground, inside, key, 1.0, own=screen_insert.edge_of(region, foreground, key, 3.0))
    assert blamed > screen_insert.MAX_HALO_SHARE
    assert read.share is not None and read.share <= screen_insert.MAX_HALO_SHARE and read.own > 0.1
    audit = {"halo": 0.08, "halo_own": read.own}
    frames = {str(n): audit for n in range(insert_checks.HALO_MIN_FRAMES)}
    found = insert_checks.findings({KEY: {"frames": frames, "evidence": {}}}, {KEY: insert_checks.Placed(KEY, "s1", LABEL, 0.0, 1.0)}, fps=24)
    [finding] = [f for f, _p in found if f.kind == INSERT_HALO]
    assert "of the edge is the footage's own colour there and is not counted" in finding.message


# ── 3. The advice ─────────────────────────────────────────────────────────


def _residual(where: dict, settings: dict, earlier: list[dict] | None, fit: str = "cover", light: float = 0.15):
    placed = {KEY: insert_checks.Placed(KEY, "s1", LABEL, 0.0, 1.0, light=light, keyed=True, fit=fit, key_settings=settings, drawn="rest-a")}
    audit = {"residual_key": insert_checks.inside_share(where), "residual_where": where}
    found = insert_checks.findings({KEY: {"frames": {"0": audit}, "evidence": {}}}, placed, fps=24, earlier={KEY: earlier} if earlier else None)
    [finding] = [f for f, _p in found if f.kind == INSERT_RESIDUAL_KEY]
    return finding


NOW = {"color": "green", "tolerance": 0.88, "feather": 9.0}
BEFORE = {"color": "green", "tolerance": 0.4, "feather": 3.0}


def _drawn(settings: dict, residual: float, *, light: float = 0.15, rest: str = "rest-a") -> dict:
    """An earlier drawing of the insert as a scene's preview keeps it (``render_checks.insert_measures``)."""
    return {"key_settings": settings, "light": light, "fit": "cover", "rest": rest, "measures": {"residual_key": residual}, "render_id": "r1"}


def test_a_residual_key_no_key_setting_moves_is_said_so_never_as_a_tolerance_to_widen():
    inside = {"inside": {"bottom": 0.012}}
    # The same to 3 decimals at another key: not a setting, and what leaves it where it sits.
    still = _residual(inside, NOW, [_drawn(BEFORE, 0.0121)])
    assert "not a setting: source content - the surface showing through the source's transparent pixels inside the quad" in still.message
    assert (
        "(the same 0.012 at key green, tolerance 0.88, feather 9, light 0.15 as at key green, tolerance 0.4, feather 3, "
        "light 0.15, render `r1`)" in still.message
    )
    assert "widen the key's tolerance" not in still.message and still.data["unchanged"]["render_id"] == "r1"
    # At another light alone, the same.
    lit = _residual(inside, NOW, [_drawn(NOW, 0.012, light=0.08)])
    assert lit.message.count("not a setting:") == 1 and "light 0.08, render `r1`" in lit.message
    # In the contain bands, the bands are named.
    banded = _residual({"contain": {"bottom": 0.012}}, NOW, [_drawn(BEFORE, 0.012)], "contain")
    assert "not a setting: source content - the surface showing through the contain bands - use a source" in banded.message
    # A residual the last settings moved, a drawing at the same settings, or one whose other fields differ is no
    # evidence: the plain action stands - never a tolerance to widen, which no fixture shows moving it.
    moved = _residual(inside, NOW, [_drawn(BEFORE, 0.03)])
    same = _residual(inside, NOW, [_drawn(NOW, 0.012)])
    other = _residual(inside, NOW, [_drawn(BEFORE, 0.012, rest="rest-b")])
    for finding in (moved, same, other, _residual(inside, NOW, None)):
        assert "give the insert a picture opaque there" in finding.message and "unchanged" not in finding.data
        assert "tolerance" not in finding.fix and finding.data["action"]["kind"] == insert_checks.REPLACE_SOURCE
    # An older drawing holds the residual inside the quad alone, at its key: compared with that.
    legacy = _residual(inside, NOW, [{"key_settings": BEFORE, "fit": "cover", "inside": 0.0121, "render_id": "r0"}])
    assert "not a setting:" in legacy.message and "render `r0`" in legacy.message


def test_each_drawing_keeps_what_its_inserts_measured_for_the_next():
    report = {
        "key": KEY,
        "label": LABEL,
        "frames": 3,
        "residual_key": 0.012,
        "drawn_by": {"fit": "contain", "light": 0.15, "key": BEFORE},
        "drawn_rest": "rest-a",
        "measures": {"residual_key": 0.012, "flicker": 0.0},
        "key_settings": BEFORE,
        "fit": "contain",
        "residual_inside": 0.012,
    }
    neighbour = {**report, "key": "insert/s2.0"}
    unmeasured = {"key": "insert/s1.1", "label": LABEL, "frames": 3, "residual_key": None}
    checks = render_checks.Checks(inserts=[report, neighbour, unmeasured]).as_dict()
    previews = render_checks.record_seen(None, {"s1": "digest-a"}, "r1", checks=checks, revision=4)
    assert previews["s1"]["drawings"][0]["revision"] == 4
    assert previews["s1"]["drawings"][0]["inserts"] == {
        KEY: {
            "key_settings": BEFORE,
            "light": 0.15,
            "fit": "contain",
            "rest": "rest-a",
            "measures": {"residual_key": 0.012, "flicker": 0.0},
            "inside": 0.012,
        }
    }
    # A later drawing at other settings keeps both, newest first, each with the render that drew it.
    later = render_checks.Checks(inserts=[{**report, "drawn_by": {**report["drawn_by"], "key": NOW}}]).as_dict()
    previews = render_checks.record_seen(previews, {"s1": "digest-b"}, "r2", checks=later)
    earlier = render_checks.earlier_measures(previews)
    assert [(r["render_id"], r["key_settings"]["tolerance"]) for r in earlier[KEY]] == [("r2", 0.88), ("r1", 0.4)]
    assert render_checks.earlier_measures(None) == {}


def test_a_render_s_segments_drawn_by_another_drawing_of_the_insert_are_never_joined():
    parts = compositor._insert_inputs(_insert(), compositor.CompositorAssets(), [])
    assert parts["drawing"] == screen_insert.DRAWING_VERSION >= 10
