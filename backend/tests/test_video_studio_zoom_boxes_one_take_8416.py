"""The boxes over a canvas are judged across every wheel zoom of one take.

A box the zoom carries (a label a graph lays over a node) is drawn with the canvas, but each wheel step
judged its boxes alone: on a short or low-contrast step :func:`scroll_pacing.riding_boxes` could not tell, and the take
read "36 box(es) over its canvas held as painted" on that step while the other wheel steps drew the same labels with
the canvas, split or offset labels in the render. What is proven here:

* :func:`scroll_pacing.carried_boxes` names, on a zoom of a canvas another zoom of the take found labels riding, the
  boxes it held that neither stand still on it nor stand at the same place on another zoom that held them (a toolbar);
* nothing is carried across two different canvases, nor when no zoom of the take found anything riding;
* :func:`scroll_pacing._pace_zoom` never carries a box that stands still on its own zoom, and draws a carried box with
  the canvas, saying ``carried``;
* the step's words say it.
"""

from __future__ import annotations

from app.services.video_studio import scroll_pacing
from tests.test_video_studio_canvas_zoom_6430 import BOX_CSS, FPS, RATIO, TOOLBAR_CSS, _take, _world

CANVAS = tuple(BOX_CSS)
OTHER_CANVAS = (1700.0, 60.0, 200.0, 840.0)
TOOLBAR = (40.0, 80.0, 468.0, 54.0)
LABEL_A = (600.0, 300.0, 120.0, 24.0)
LABEL_B = (900.0, 500.0, 120.0, 24.0)


def _zoom(box, over, riding=(), standing=()) -> list:
    """One paced canvas zoom as :func:`scroll_pacing.pace` keeps it: ``[scroll, kept, drawn, used, serial, seen]``."""
    scroll = scroll_pacing.Scroll(span=(1.0, 2.0), log=[(1.0, 0.0), (2.0, 1.0)], scroller="canvas", box=box, over=list(over))
    seen = {"over": list(over), "riding": set(riding), "standing": set(standing)}
    return [scroll, {"paced": True}, [(1.5, "z.jpg")], {3}, 0, seen]


def test_a_box_held_on_one_wheel_step_rides_when_another_step_of_the_take_found_labels_riding():
    # Step A: the labels ride, the toolbar held. Step B: the same canvas, its labels moved by the zoom, all held.
    a = _zoom(CANVAS, [TOOLBAR, LABEL_A], riding={1})
    b = _zoom(CANVAS, [TOOLBAR, LABEL_B, (LABEL_B[0] + 200, LABEL_B[1], 90.0, 24.0)])
    found = scroll_pacing.carried_boxes([a, b])
    # The toolbar stands at the same place on step A, held there: never carried. Both labels of step B are.
    assert found == {1: frozenset({1, 2})}


def test_a_box_standing_still_on_its_own_step_is_never_carried():
    a = _zoom(CANVAS, [LABEL_A], riding={0})
    b = _zoom(CANVAS, [TOOLBAR, LABEL_B], standing={0})
    assert scroll_pacing.carried_boxes([a, b]) == {1: frozenset({1})}


def test_nothing_is_carried_across_canvases_or_when_nothing_rides():
    a = _zoom(CANVAS, [LABEL_A], riding={0})
    b = _zoom(OTHER_CANVAS, [LABEL_B])
    assert scroll_pacing.carried_boxes([a, b]) == {}
    c = _zoom(CANVAS, [LABEL_A])
    d = _zoom(CANVAS, [LABEL_B])
    assert scroll_pacing.carried_boxes([c, d]) == {}
    # A zoom that drew nothing (not paced) carries nothing and lends nothing.
    e = _zoom(CANVAS, [LABEL_A], riding={0})
    e[2] = []
    assert scroll_pacing.carried_boxes([e, _zoom(CANVAS, [LABEL_B])]) == {}


def test_pace_zoom_never_carries_a_standing_box_and_says_what_it_carried(tmp_path):
    frames, events = _take(tmp_path, _world())
    [scroll] = scroll_pacing.read_scrolls(events[0]["scrolls"][0])
    assert [list(box) for box in scroll.over] == [TOOLBAR_CSS]
    times = [t for t, _name in frames]
    reader = scroll_pacing.Keyframes(str(tmp_path), frames)
    from concurrent.futures import ThreadPoolExecutor
    from pathlib import Path

    seen: dict = {}
    with ThreadPoolExecutor(max_workers=2) as saver:
        kept, drawn, _used = scroll_pacing._pace_zoom(
            scroll, reader, times, fps=FPS, ratio=RATIO, out_dir=Path(tmp_path), serial=0, saver=saver, carry=frozenset({0}), seen=seen
        )
    assert drawn and kept["paced"], kept
    # The toolbar stands still on this zoom: held, whatever another step said.
    assert seen["standing"] == {0}
    assert "carried" not in kept and kept.get("overlays") == 1, kept


def test_the_step_says_the_boxes_it_carried():
    entry = {"paced": True, "fps": 30.0, "scroller": "canvas", "riding": 36, "carried": 36}
    said = scroll_pacing._how(entry, 12.0)
    assert "36 box(es) over its canvas moving with it drawn with the canvas (36 as on the take's other zooms of it)" in said
    assert "held as painted" not in said
