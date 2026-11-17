"""A node whose highlight lights many converging edges with it is pressed where they converge, not on the edges.

On a layered layout, a node with twenty or more relationships converging on it highlights them with it: the links
fan out under the node, and the region they enclose with the far layer (its nodes, the line they stand on) fills
into one solid blob whose thickest places lie between the links. Every place the highlight resolved was on that
blob, the card read there named a relationship ("uses") or a far node, and the step failed; a ``right_click``
opened the canvas menu. Every lit link has the node at one end: each link pixel votes along its own direction, the
place most votes meet is the node, and it is tried first, the thick places after it.
"""

from __future__ import annotations

import numpy as np
import pytest

from app.services.video_studio import node_locate


def _disc(shape: tuple[int, int], centre: tuple[float, float], radius: float) -> np.ndarray:
    ys, xs = np.mgrid[0 : shape[0], 0 : shape[1]]
    return (xs + 0.5 - centre[0]) ** 2 + (ys + 0.5 - centre[1]) ** 2 <= radius**2


def _line(canvas: np.ndarray, start: tuple[int, int], end: tuple[int, int], width: int, value: int) -> None:
    steps = int(max(abs(end[0] - start[0]), abs(end[1] - start[1]))) + 1
    t = np.linspace(0.0, 1.0, steps)
    xs = np.rint(start[0] + t * (end[0] - start[0])).astype(int)
    ys = np.rint(start[1] + t * (end[1] - start[1])).astype(int)
    half = width // 2
    for dx in range(-half, width - half):
        for dy in range(-half, width - half):
            canvas[np.clip(ys + dy, 0, canvas.shape[0] - 1), np.clip(xs + dx, 0, canvas.shape[1] - 1)] = value


def _layered(*, links: int = 22, closed: bool = True, labels: bool = True) -> tuple[np.ndarray, np.ndarray]:
    """A node at (400, 100), radius 10, whose *links* relationships fan out to a layer at y=380; *closed*: the
    far layer's line lit too (the blob filled), *labels*: each relationship's label box lit at its middle."""
    before = np.full((450, 800), 20, dtype=np.uint8)
    after = before.copy()
    node = (400, 100)
    after[_disc(before.shape, node, 10)] = 210
    for index in range(links):
        end = (100 + index * 28, 380)
        _line(after, node, end, 3, 210)
        after[_disc(before.shape, end, 7)] = 210
        if labels:
            mx, my = (node[0] + end[0]) // 2, (node[1] + end[1]) // 2
            after[my - 7 : my + 7, mx - 18 : mx + 18] = 210
    if closed:
        _line(after, (100, 380), (100 + (links - 1) * 28, 380), 3, 210)
    return before, after


@pytest.mark.parametrize("closed", [True, False])
@pytest.mark.parametrize("labels", [True, False])
def test_a_node_with_many_converging_links_is_its_own_glyph(closed: bool, labels: bool):
    before, after = _layered(closed=closed, labels=labels)
    found = node_locate.highlight_of(before, after)
    assert isinstance(found, node_locate.Highlight)
    assert found.centre == pytest.approx((400.0, 100.0), abs=3.0)
    assert found.node_shaped is True
    assert found.links >= 10


def test_the_glyph_comes_first_and_the_thick_places_follow():
    before, after = _layered()
    found = node_locate.highlight_of(before, after)
    assert isinstance(found, node_locate.Highlight)
    assert found.others, "the blob's thick places stay as the next places to try"
    assert all(abs(x - 400.0) > 3.0 or abs(y - 100.0) > 3.0 for x, y in found.others)


def test_a_ring_drawn_around_a_node_still_reads_as_the_node():
    before = np.full((300, 500), 20, dtype=np.uint8)
    after = before.copy()
    after[_disc(before.shape, (250.0, 150.0), 16) & ~_disc(before.shape, (250.0, 150.0), 12)] = 210
    found = node_locate.highlight_of(before, after)
    assert isinstance(found, node_locate.Highlight)
    assert found.centre == pytest.approx((250.0, 150.0), abs=1.0)
    assert found.node_shaped is True


def _hub_beside(*, radius: int, glyph: str, ratio: int = 1) -> tuple[np.ndarray, np.ndarray]:
    """A node at (150, 250) whose 22 links fan out to a layer at x=780, each with its label box half way, the far
    layer's line lit; the node's glyph (*radius*) unchanged by the highlight (``unlit``) or ringed (``ring``): the
    links stop at its rim (the layered layout of #9313)."""
    before = np.full((500 * ratio, 900 * ratio), 20, dtype=np.uint8)
    after = before.copy()
    node = (150 * ratio, 250 * ratio)
    for index in range(22):
        end = (780 * ratio, (30 + index * 20) * ratio)
        _line(after, node, end, 2 * ratio, 210)
        after[_disc(before.shape, end, 7 * ratio)] = 210
        mx, my = (node[0] + end[0]) // 2, (node[1] + end[1]) // 2
        after[my - 7 * ratio : my + 7 * ratio, mx - 18 * ratio : mx + 18 * ratio] = 210
    _line(after, (780 * ratio, 30 * ratio), (780 * ratio, 450 * ratio), 3 * ratio, 210)
    glyph_area = _disc(before.shape, node, radius * ratio)
    after[glyph_area] = before[glyph_area]
    if glyph == "ring":
        after[glyph_area & ~_disc(before.shape, node, (radius - 3) * ratio)] = 210
    return before, after


@pytest.mark.parametrize("glyph", ["unlit", "ring"])
@pytest.mark.parametrize("radius", [8, 14, 20])
@pytest.mark.parametrize("ratio", [1, 2])
def test_the_links_meet_at_the_glyph_centre_not_at_its_rim(glyph: str, radius: int, ratio: int):
    # Before #9313 the point stood 6 to 18 px off the node, where the links leave its glyph, or (a 20 px ring) no
    # point was taken and a label box was pressed.
    before, after = _hub_beside(radius=radius, glyph=glyph, ratio=ratio)
    found = node_locate.highlight_of(before, after, ratio=float(ratio))
    assert isinstance(found, node_locate.Highlight)
    assert found.centre == pytest.approx((150.0 * ratio, 250.0 * ratio), abs=2.0 * ratio)
    assert found.links >= 20
    assert found.unconverged == ""


def test_why_no_convergence_was_taken_is_said():
    before = np.full((300, 600), 20, dtype=np.uint8)
    after = before.copy()
    after[137:163, 300:420] = 210
    found = node_locate.highlight_of(before, after)
    assert isinstance(found, node_locate.Highlight)
    assert found.links == 0
    assert "straight link" in found.unconverged


def test_parallel_links_meet_nowhere():
    before = np.full((300, 600), 20, dtype=np.uint8)
    after = before.copy()
    for row in range(40, 260, 30):
        after[row : row + 2, 100:500] = 210
    found = node_locate._convergence(node_locate._changed(before, after), 1.0)
    assert isinstance(found, str)


def test_a_lone_edge_label_box_is_no_glyph():
    before = np.full((300, 600), 20, dtype=np.uint8)
    after = before.copy()
    after[137:163, 300:420] = 210
    found = node_locate.highlight_of(before, after)
    assert isinstance(found, node_locate.Highlight)
    assert found.node_shaped is False
    assert found.links == 0


def test_a_node_with_one_link_keeps_its_thickest_place():
    before = np.full((300, 600), 20, dtype=np.uint8)
    after = before.copy()
    after[_disc(before.shape, (120.0, 150.0), 9)] = 210
    after[149:151, 129:300] = 210
    found = node_locate.highlight_of(before, after)
    assert isinstance(found, node_locate.Highlight)
    assert found.links == 0
    assert found.centre == pytest.approx((120.0, 150.0), abs=1.5)


@pytest.mark.parametrize("radius", [1, 2, 3, 5, 8, 13])
def test_the_opening_by_distance_is_the_binary_opening_by_a_disc(radius: int):
    """``_opened`` stands for ``ndimage.binary_opening`` with a disc: the same pixels, without the per-pixel
    structuring element that took 36 s and 1.5 GB on the layered highlight above and starved the CI runner."""
    from scipy import ndimage

    rng = np.random.default_rng(radius)
    for _trial in range(20):
        mask = ndimage.binary_dilation(rng.random((70, 90)) > 0.97, iterations=int(rng.integers(1, 8)))
        mask[int(rng.integers(0, 70)), :] = True
        want = ndimage.binary_opening(mask, structure=node_locate._disc_element(radius))
        assert np.array_equal(node_locate._opened(mask, radius), want)
