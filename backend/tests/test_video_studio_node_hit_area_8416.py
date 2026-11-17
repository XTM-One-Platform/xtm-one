"""A node step lands on the node, not on the edge label its highlight lights with it, and reading a node never makes
the screencast paint at the canvas's size.

On a GPU worker, a ``right_click`` on a node of a layered graph resolved "by highlight" opened the relationship's menu
("headed 'uses', 2 entries"): the highlight lit the node and its relationship's label box together, and the label box,
wide and drawn solid, was thicker than the node's circle. The highlight now ranks its thick places by shape - a place
drawn like a node before an edge's label box - and keeps the others, best first, for a ``right_click`` whose menu
names something else to try. Each node read shot the canvas with a clipped screenshot, which made Chromium paint the
screencast at the clip's size (deviceWidth 1794 for 1920) every 1-2 s, frames the recorder then dropped and held the
one before in place of: the window is now shot whole and cut.
"""

from __future__ import annotations

import io

import numpy as np
import pytest
from PIL import Image

from app.services.video_studio import node_locate, pointer
from app.services.video_studio.node_locate import Entry, Identity


def _disc(shape: tuple[int, int], centre: tuple[float, float], radius: float) -> np.ndarray:
    ys, xs = np.mgrid[0 : shape[0], 0 : shape[1]]
    return (xs + 0.5 - centre[0]) ** 2 + (ys + 0.5 - centre[1]) ** 2 <= radius**2


# ── The highlight: the node's shape before an edge's label box ─────────────


def test_a_node_beside_a_thicker_edge_label_box_is_the_point():
    before = np.full((300, 600), 20, dtype=np.uint8)
    after = before.copy()
    # The node: a circle of radius 9. Its relationship: a link to a label box 80 x 26, drawn solid (thicker inside).
    after[_disc(before.shape, (120.0, 150.0), 9)] = 210
    after[149:151, 129:300] = 210
    after[137:163, 300:380] = 210
    found = node_locate.highlight_of(before, after)
    assert isinstance(found, node_locate.Highlight)
    assert found.centre == pytest.approx((120.0, 150.0), abs=1.5)
    assert found.node_shaped is True
    # The label box is kept as the next place to try.
    assert found.others and found.others[0] == pytest.approx((340.0, 150.0), abs=3.0)
    # The camera still frames all it changed.
    assert found.box[2] > 250


def test_a_highlight_with_no_part_drawn_like_a_node_says_so_and_keeps_the_thickest():
    before = np.full((300, 600), 20, dtype=np.uint8)
    after = before.copy()
    after[137:163, 300:420] = 210  # a label box only, 120 x 26
    found = node_locate.highlight_of(before, after)
    assert isinstance(found, node_locate.Highlight)
    assert found.node_shaped is False
    assert found.centre[1] == pytest.approx(150.0, abs=1.5)
    assert 300 <= found.centre[0] <= 420


def test_a_lone_node_is_still_its_own_centre():
    before = np.full((300, 500), 20, dtype=np.uint8)
    after = before.copy()
    after[_disc(before.shape, (320.0, 140.0), 14)] = 210
    found = node_locate.highlight_of(before, after)
    assert isinstance(found, node_locate.Highlight)
    assert found.centre == pytest.approx((320.0, 140.0), abs=0.5)
    assert found.node_shaped is True and found.others == ()


def test_the_roundness_of_a_shape_reads_its_row_and_column_runs():
    square = np.zeros((20, 20), dtype=bool)
    square[5:15, 5:15] = True
    assert node_locate._roundness(square, 10, 10) == 1.0
    band = np.zeros((20, 60), dtype=bool)
    band[8:12, 0:60] = True
    assert node_locate._roundness(band, 30, 10) == pytest.approx(4 / 60)
    assert node_locate._roundness(band, 30, 0) == 0.0
    assert node_locate._roundness(band, 99, 99) == 0.0


# ── The target carries the other places in CSS px ─────────────────────────


class _Page:
    """A page whose list focuses and whose canvas lights a node and its edge label when the node's entry is active."""

    viewport_size = {"width": 600, "height": 300}

    def __init__(self) -> None:
        self.lit = False
        self.shots: list[dict] = []

    async def evaluate(self, script, *_args):
        if script is node_locate._FOCUS_JS:
            self.lit = True
            return {"focused": True, "count": 1, "active": {"pos": 0, "text": "Storm-1567"}}
        if script is node_locate._ACTIVE_JS:
            return {"pos": 0, "text": "Storm-1567"}
        return None

    async def screenshot(self, **kwargs) -> bytes:
        self.shots.append(kwargs)
        grey = np.full((300, 600), 20, dtype=np.uint8)
        if self.lit:
            grey[_disc(grey.shape, (120.0, 150.0), 9)] = 210
            grey[149:151, 129:300] = 210
            grey[137:163, 300:380] = 210
        out = io.BytesIO()
        Image.fromarray(grey).convert("RGB").save(out, "PNG")
        return out.getvalue()


async def test_a_node_by_highlight_carries_the_other_places_and_the_shape_in_its_log():
    page = _Page()
    entry = Entry("Storm-1567", list=0, index=0, count=1)
    identity = Identity(entry=entry, how="exact", titles=("Storm-1567",))
    shot = await node_locate._seen(page, (0.0, 0.0, 600.0, 300.0))
    target = await node_locate._by_highlight(page, "Storm-1567", identity, shot, (600, 300))
    assert target.point == pytest.approx((120.0, 150.0), abs=1.5)
    assert target.others and target.others[0] == pytest.approx((340.0, 150.0), abs=3.0)
    assert target.located["highlight"]["node_shaped"] is True
    assert target.located["highlight"]["others"][0]["x"] == pytest.approx(340.0, abs=3.0)
    assert "drawn like a node" not in target.located["said"]


# ── A node read never shoots a clip ───────────────────────────────────────


async def test_the_canvas_is_cut_from_the_window_shot_whole_never_a_clipped_screenshot():
    page = _Page()
    picture = await node_locate._shoot(page, (100.0, 50.0, 200.0, 100.0))
    assert "clip" not in page.shots[0]
    with Image.open(io.BytesIO(picture)) as image:
        assert image.size == (200, 100)


def test_the_cut_follows_the_pixel_ratio_and_stays_in_the_picture():
    out = io.BytesIO()
    Image.new("RGB", (1200, 600), (20, 20, 20)).save(out, "PNG")
    cut = node_locate._cut(out.getvalue(), (100.0, 50.0, 200.0, 100.0), 600.0)
    with Image.open(io.BytesIO(cut)) as image:
        assert image.size == (400, 200)
    edge = node_locate._cut(out.getvalue(), (500.0, 250.0, 300.0, 300.0), 600.0)
    with Image.open(io.BytesIO(edge)) as image:
        assert image.size == (200, 100)


async def test_a_page_with_no_viewport_size_hands_the_whole_shot():
    class _Bare(_Page):
        viewport_size = None

    page = _Bare()
    picture = await node_locate._shoot(page, (100.0, 50.0, 200.0, 100.0))
    with Image.open(io.BytesIO(picture)) as image:
        assert image.size == (600, 300)


# ── The retries a wrong menu gets ─────────────────────────────────────────


def test_a_target_carries_no_other_place_by_default_and_retries_are_bounded():
    assert pointer.Target((1.0, 1.0), {}, {}).others == ()
    assert pointer.MENU_RETRIES == 2
