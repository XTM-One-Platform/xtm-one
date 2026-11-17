"""A node step proves it acted on its node, and its locator reads neither an edge's label nor a hover card.

On OpenCTI's production graph a ``right_click`` on ``node {label: 'Storm-1567'}`` opened a relationship's menu, then
the empty canvas's: the point the locator resolved was not on the node, and the step that clicked the menu entry two
steps later was the one that failed. The menu is now checked against the node's title where it opens
(``pointer.menu_check``); OCR leaves out labels drawn as an edge's (``canvas_ocr.edge_label``) and what the page draws
over the canvas; the highlight stage reads the change shared by moving onto the node and off it, once the list's
active entry is the node's; the action log says the stage and the point of every node step. The real Chromium and
Tesseract: ``test_video_studio_node_menu_6690_chromium.py``.
"""

from __future__ import annotations

import io

import numpy as np
import pytest
from PIL import Image, ImageDraw, ImageFont

from app.services.video_studio import canvas_ocr, jobs, node_locate, pointer
from app.services.video_studio.node_locate import Entry

GROUND, INK, LINE = 11, 240, 120


def _label(text: str, size: int, *, angle: float = 0.0, lean: float = 0.0, line: bool = False):
    """A grey picture of *text* drawn as a graph draws it - upright, rotated by *angle* degrees, leaning *lean* (x per
    y, an italic), or on a horizontal line it clears a halo around - and the box of its ink."""
    font = ImageFont.load_default(size=size)
    picture = Image.new("L", (600, 200), GROUND)
    if line:
        ImageDraw.Draw(picture).line([(40, 100), (560, 100)], fill=LINE, width=max(1, size // 8))

    def drawn(halo: int) -> Image.Image:
        tile = Image.new("L", (400, 120), 0)
        ImageDraw.Draw(tile).text((200, 60), text, font=font, anchor="mm", fill=255, stroke_width=halo, stroke_fill=255)
        if lean:
            tile = tile.transform(tile.size, Image.Transform.AFFINE, (1, lean, -lean * 60, 0, 1, 0), Image.Resampling.BICUBIC)
        if angle:
            tile = tile.rotate(angle, resample=Image.Resampling.BICUBIC)
        return tile

    if line:
        picture.paste(GROUND, (100, 40), drawn(max(2, size // 4)))
    picture.paste(INK, (100, 40), drawn(0))
    grey = np.asarray(picture, dtype=np.uint8)
    ys, xs = np.nonzero(np.abs(grey.astype(np.int16) - INK) < 60)
    return grey, (float(xs.min()), float(ys.min()), float(np.ptp(xs) + 1), float(np.ptp(ys) + 1))


# ── An edge's label is never a node's ─────────────────────────────────────


@pytest.mark.parametrize("text", ["Paris", "Storm-1567", "Intrusion Set Storm-1567", "Spying gang", "APT28"])
@pytest.mark.parametrize("size", [13, 18, 26])
def test_an_upright_label_standing_free_is_a_nodes(text, size):
    grey, box = _label(text, size)
    assert canvas_ocr.edge_label(grey, box) is None


@pytest.mark.parametrize(
    ("drawn", "why"),
    [
        ({"angle": 5}, "rotated"),
        ({"angle": -12}, "rotated"),
        ({"lean": 0.22}, "italic"),
        ({"line": True}, "along a line"),
    ],
)
@pytest.mark.parametrize("text", ["Paris", "Storm-1567", "Wizard Spider"])
@pytest.mark.parametrize("size", [13, 26])
def test_a_label_drawn_along_its_link_rotated_or_in_italic_is_an_edges(text, size, drawn, why):
    grey, box = _label(text, size, **drawn)
    assert canvas_ocr.edge_label(grey, box) == why


def test_a_label_the_picture_cuts_at_its_sides_reads_no_line():
    grey, (left, top, width, height) = _label("Paris", 18, line=True)
    cut = grey[:, int(left) - 4 : int(left + width) + 4]
    assert canvas_ocr.edge_label(cut, (4.0, top, width, height)) is None


def _shot(grey: np.ndarray) -> node_locate._Shot:
    return node_locate._Shot((0.0, 0.0, float(grey.shape[1]), float(grey.shape[0])), [], b"", 1.0, grey)


def test_the_label_holding_the_title_drawn_as_an_edges_is_left_out_and_said():
    grey, (left, top, width, height) = _label("Berlin", 18, line=True)
    words = [
        canvas_ocr.Word("Berlin", left, top, width, height, 96.0),
        canvas_ocr.Word("Paris", 20.0, 20.0, 40.0, height, 96.0),
    ]
    shot = _shot(grey)
    kept = node_locate._edge_labels(shot, words, ("Berlin",))
    assert [word.text for word in kept] == ["Paris"]
    assert shot.left_out == ["'Berlin' along a line"]
    assert node_locate._unread(shot) == "no box by OCR but in an edge's label ('Berlin' along a line)"


# ── The menu a right-click on a node opened ───────────────────────────────


def _node(*names: str, by: str = "ocr") -> pointer.Target:
    return pointer.Target((812.0, 433.0), {}, {"node": names[0]}, located={"by": by}, names=names)


@pytest.mark.parametrize(
    "titles",
    [
        ["Storm-1567"],
        ["storm-1567"],
        ["Intrusion Set Storm-1567"],
        ["Storm-15\u2026"],
        # A node of a larger selection: the selection's section comes first.
        ["Selection", "Storm-1567", "Graph"],
    ],
)
def test_a_menu_headed_with_the_node_passes(titles):
    target = _node("Storm-1567", "Intrusion Set Storm-1567")
    checked, failed = pointer.menu_check({"items": ["Select entity and neighbours"], "titles": titles}, target)
    assert failed is None and checked == {"title": titles[0], "node": True}


def test_a_menu_headed_with_something_else_fails_naming_the_title_the_point_and_the_stage():
    menu = {"items": ["Select all", "Select by type", "Clear selection", "Show the hidden", "Ungroup all", "Reset"], "titles": ["Graph"]}
    checked, failed = pointer.menu_check(menu, _node("Storm-1567", "Intrusion Set Storm-1567"))
    assert checked == {"title": "Graph", "node": False}
    assert failed == (
        "the menu it opened is headed 'Graph', 6 entries, not the node 'Storm-1567': the point it resolved by ocr, "
        "(812, 433), is not on the node"
    )
    _checked, said = pointer.menu_check({"items": ["Open", "Select this relationship"], "titles": ["Uses", "Graph"]}, _node("Paris", by="highlight"))
    assert said is not None and said.startswith("the menu it opened is headed 'Uses' (then 'Graph'), 2 entries")
    assert "resolved by highlight" in said


def test_a_menu_without_a_title_or_no_menu_is_not_checked():
    assert pointer.menu_check({"items": ["Select its neighbours"]}, _node("Berlin")) == (None, None)
    assert pointer.menu_check(None, _node("Berlin")) == (None, None)
    assert pointer.menu_check({"items": [], "titles": ["Graph"]}, pointer.Target((1.0, 1.0), {}, {})) == (None, None)


def test_the_names_a_node_goes_by_are_its_label_its_titles_and_its_entry():
    identity = node_locate.match_label(
        "Storm-1567", [Entry("Intrusion Set Storm-1567, 6 relationships", list=0, index=0, count=1)]
    ).identity
    assert node_locate.names_of("Storm-1567", identity) == ("Storm-1567", "Intrusion Set Storm-1567")
    assert node_locate.names_of(" Paris ", None) == ("Paris",)


def test_the_point_is_marked_on_the_canvas_attached_to_a_failure():
    out = io.BytesIO()
    Image.new("RGB", (400, 200), (11, 15, 26)).save(out, "PNG")
    marked = node_locate.marked(out.getvalue(), (100.0, 80.0, 200.0, 100.0), (150.0, 130.0))
    with Image.open(io.BytesIO(marked)) as image:
        # Pixel ratio 2: (150, 130) of the window is (100, 100) of the shot, left clear, its cross's arms 14 px off.
        assert image.getpixel((100, 100)) == (11, 15, 26)
        assert image.getpixel((60, 100)) != (11, 15, 26) and image.getpixel((100, 140)) != (11, 15, 26)
    assert node_locate.marked(b"not a picture", (0.0, 0.0, 1.0, 1.0), (0.0, 0.0)) == b"not a picture"


# ── The highlight: the node's change alone ────────────────────────────────


def _disc(shape: tuple[int, int], centre: tuple[float, float], radius: float) -> np.ndarray:
    ys, xs = np.mgrid[0 : shape[0], 0 : shape[1]]
    return (xs + 0.5 - centre[0]) ** 2 + (ys + 0.5 - centre[1]) ** 2 <= radius**2


def test_the_change_shared_onto_the_node_and_off_it_leaves_its_neighbours_out():
    ground = np.full((300, 600), 20, dtype=np.uint8)
    before, node, after = ground.copy(), ground.copy(), ground.copy()
    # A larger neighbour active first, the node next, a third entry after.
    before[_disc(ground.shape, (100.0, 100.0), 30)] = 210
    node[_disc(ground.shape, (400.0, 150.0), 14)] = 210
    after[_disc(ground.shape, (520.0, 240.0), 20)] = 210
    assert node_locate.highlight_of(before, node).centre == pytest.approx((100.0, 100.0), abs=0.5)
    found = node_locate.highlight_of(before, node, also=after)
    assert isinstance(found, node_locate.Highlight) and found.centre == pytest.approx((400.0, 150.0), abs=0.5)


def test_what_the_page_draws_over_the_canvas_never_counts():
    before = np.full((300, 600), 20, dtype=np.uint8)
    after = before.copy()
    after[_disc(before.shape, (100.0, 200.0), 12)] = 210
    after[40:140, 300:540] = 180  # the hover card the active entry opened
    found = node_locate.highlight_of(before, after, masked=[(300.0, 40.0, 240.0, 100.0)])
    assert isinstance(found, node_locate.Highlight) and found.centre == pytest.approx((100.0, 200.0), abs=0.5)
    alone = before.copy()
    alone[40:140, 300:540] = 180
    assert node_locate.highlight_of(before, alone, masked=[(300.0, 40.0, 240.0, 100.0)]) == (
        "nothing changed on the canvas outside what the page draws over it"
    )


@pytest.mark.parametrize("width", [2, 8])
def test_the_links_a_node_redraws_with_it_never_pull_it_off_the_node(width):
    before = np.full((300, 600), 20, dtype=np.uint8)
    after = before.copy()
    after[_disc(before.shape, (150.0, 150.0), 16)] = 210
    after[150 - width // 2 : 150 + width - width // 2, 166:580] = 210  # its link drawn brighter, to the right only
    found = node_locate.highlight_of(before, after, ratio=2.0)
    assert isinstance(found, node_locate.Highlight)
    assert found.centre == pytest.approx((150.0, 150.0), abs=1.0)
    # The camera still frames all it changed.
    assert found.box[2] > 400


def test_a_ring_drawn_around_a_node_reads_as_the_node():
    before = np.full((300, 600), 20, dtype=np.uint8)
    after = before.copy()
    ring = _disc(before.shape, (300.0, 120.0), 22) & ~_disc(before.shape, (300.0, 120.0), 19)
    after[ring] = 210
    after[118:122, 322:460] = 210  # a link leaving it
    found = node_locate.highlight_of(before, after)
    assert isinstance(found, node_locate.Highlight) and found.centre == pytest.approx((300.0, 120.0), abs=1.0)


@pytest.mark.parametrize(
    ("place", "count", "beside"),
    [(5, 8, (4, 6)), (0, 8, (1, 2)), (7, 8, (6, 5)), (1, 3, (0, 2)), (0, 2, None), (3, 3, None)],
)
def test_the_entries_moved_to_around_the_node(place, count, beside):
    assert node_locate._beside(place, count) == beside


class _ListPage:
    """A page whose graph list stops its active entry at *stop*: ArrowDown goes no further."""

    def __init__(self, entries: list[str], stop: int) -> None:
        self.entries, self.stop, self.active = entries, stop, -1
        self.keyboard = self

    async def press(self, key: str) -> None:
        self.active = min(self.active + 1, self.stop) if key == "ArrowDown" else max(self.active - 1, 0)

    async def evaluate(self, script: str, args=None):
        if script is node_locate._FOCUS_JS:
            return {"focused": True, "active": {"pos": -1, "text": ""}, "count": len(self.entries)}
        if script is node_locate._ACTIVE_JS:
            return {"pos": self.active, "text": self.entries[self.active]}
        return None

    async def screenshot(self, **_kwargs) -> bytes:
        out = io.BytesIO()
        Image.new("RGB", (60, 40), (20, 20, 20)).save(out, "PNG")
        return out.getvalue()


async def test_the_highlight_is_read_only_once_the_lists_active_entry_is_the_nodes():
    entries = [f"City {name}, 1 relationships" for name in ("Paris", "Berlin", "Madrid", "Vienna", "Lisbon", "Geneva")]
    page = _ListPage(entries, stop=3)
    before = _shot(np.full((40, 60), 20, dtype=np.uint8))
    found, picture, logged = await node_locate._highlight(page, Entry(entries[5], list=0, index=5, count=6, pos=5), before)
    assert found == "the list's active entry stopped on 'City Vienna', not on the node's ('City Geneva')"
    assert picture is None and logged["keys"] >= 5


async def test_a_list_that_reaches_the_node_reads_it_moving_onto_it_and_off_it():
    entries = [f"City {name}, 1 relationships" for name in ("Paris", "Berlin", "Madrid", "Vienna")]
    page = _ListPage(entries, stop=3)
    before = _shot(np.full((40, 60), 20, dtype=np.uint8))
    found, picture, logged = await node_locate._highlight(page, Entry(entries[1], list=0, index=1, count=4, pos=1), before)
    # Paris, then Berlin, then Madrid active: three shots, the keys of each move counted.
    began, ended = logged.pop("probed_at")
    assert began <= ended
    assert logged == {"keys": 1 + 1 + 1, "shots": 3} and picture
    assert found == "nothing changed on the canvas"


# ── The action log ────────────────────────────────────────────────────────


def test_the_action_log_says_the_stage_the_point_and_the_menu_of_a_node_step():
    event = {
        "step": 2,
        "action": "right_click",
        "point": {"x": 812.4, "y": 433.0},
        "located": {"by": "highlight", "point": {"x": 812.4, "y": 433.0}},
        "menu": {"items": ["Open in a new tab", "Select this relationship"], "titles": ["Uses"]},
        "menu_check": {"title": "Uses", "node": False},
    }
    assert jobs.located_words(event) == (
        "node located by highlight at (812, 433); its menu headed 'Uses', 2 entries, not the node's"
    )
    assert jobs.located_words({**event, "menu": None}) == "node located by highlight at (812, 433); no menu opened"
    assert jobs.located_words({"action": "click", "located": {"by": "ocr"}, "point": {"x": 1, "y": 2}}) == (
        "node located by ocr at (1, 2)"
    )
    assert jobs.located_words({"action": "click", "point": {"x": 1, "y": 2}}) == ""
    take = type("Take", (), {"events": [event, {"step": 3, "action": "wait", "ta": 1.0}]})()
    assert [line["located"] for line in jobs.take_timeline(take)] == [jobs.located_words(event), ""]
