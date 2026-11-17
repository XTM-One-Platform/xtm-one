"""A node step on a node an earlier step confirmed presses where the page named it first (#9381).

On a GPU recording lane a ``click`` on a hub located by its highlight was confirmed by the card under the cursor at
(847, 624); a later ``right_click`` on the same hub, once two nodes were selected (their links highlighted), resolved
its highlight onto a lit relationship at (988, 606), whose card read 'indicates': nothing was pressed. The recorder
now keeps, per label, the place the page's card or menu named the node at, and a later step on the same node tries
it first (``pointer.remembered``), the point read now kept as the next place; the place is forgotten on a step that
pans, zooms or re-lays the view. The highlight's log says which place it chose and why no convergence was taken.
"""

from __future__ import annotations

from app.services.video_studio import capture, pointer
from tests.test_video_studio_node_hover_check_8441 import NAMES, _Page, _Recorder


def _target(point, others=(), by="highlight") -> pointer.Target:
    return pointer.Target(
        point,
        {},
        {"node": "Storm-1567"},
        located={"by": by, "point": {"x": point[0], "y": point[1]}, "said": "its highlight on focus"},
        names=NAMES,
        others=others,
    )


def test_a_highlight_point_gives_way_to_the_place_an_earlier_step_confirmed():
    target = pointer.remembered(_target((988.0, 606.0), ((900.0, 500.0),)), (847.0, 624.0), 7)
    assert target.point == (847.0, 624.0)
    # The point read now is the next place, before the highlight's others.
    assert target.others == ((988.0, 606.0), (900.0, 500.0))
    assert target.located["remembered"] == {
        "x": 847.0,
        "y": 624.0,
        "step": 7,
        "resolved": {"x": 988.0, "y": 606.0},
    }
    assert target.located["point"] == {"x": 847.0, "y": 624.0}
    assert "step 7" in target.located["said"]


def test_the_confirmed_place_is_not_repeated_among_the_others():
    target = pointer.remembered(_target((988.0, 606.0), ((847.0, 624.0), (900.0, 500.0))), (847.0, 624.0), 7)
    assert target.others == ((988.0, 606.0), (900.0, 500.0))


def test_a_node_read_by_ocr_or_a_hook_keeps_its_point():
    for by in ("ocr", "hook"):
        target = _target((988.0, 606.0), by=by)
        assert pointer.remembered(target, (847.0, 624.0), 7) is target


def test_the_same_place_changes_nothing():
    target = _target((847.0, 624.0))
    assert pointer.remembered(target, (847.0, 624.0), 7) is target


async def test_the_remembered_place_naming_the_node_is_pressed_without_trying_the_edge():
    page = _Page({(847.0, 624.0): "Storm-1567", (988.0, 606.0): "indicates"})
    recorder = _Recorder(page)
    target = pointer.remembered(_target((988.0, 606.0)), (847.0, 624.0), 7)
    await recorder.move_to(*target.point)
    tried, after = await recorder._hover_to_node(target, [], 0)
    assert after.point == (847.0, 624.0) and page.at == (847.0, 624.0)
    assert tried == [{"x": 847.0, "y": 624.0, "title": "Storm-1567", "node": True}]


async def test_a_node_moved_since_goes_on_to_the_point_read_now():
    # The view re-laid without a step the recorder forgets on: the card at the old place names something else.
    page = _Page({(847.0, 624.0): "uses", (988.0, 606.0): "Storm-1567"})
    recorder = _Recorder(page)
    target = pointer.remembered(_target((988.0, 606.0)), (847.0, 624.0), 7)
    await recorder.move_to(*target.point)
    tried, after = await recorder._hover_to_node(target, [], 0)
    assert after.point == (988.0, 606.0)
    assert [entry["node"] for entry in tried] == [False, True]


def test_the_steps_that_move_the_view_forget_the_places():
    assert {"goto", "drag", "wheel", "scroll"} <= capture._VIEW_MOVERS
    # A click or a right-click selects or opens a menu: the graph's view stays.
    assert not {"click", "right_click", "double_click", "hover"} & capture._VIEW_MOVERS
