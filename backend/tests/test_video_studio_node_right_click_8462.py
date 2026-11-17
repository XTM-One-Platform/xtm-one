"""A node step never presses on another node, a node is resolved in seconds on an animated canvas, and a GPU page's
canvas copies keep up with the take's rate.

Production retest of #8445 on a GPU recording lane (1920x1080 at pixel ratio 1.5, 30 fps): a ``right_click`` on
``node {label: 'Storm-1567'}`` ended ready with "no menu opened" while the card open was another node's; the node took
about 30 s to resolve (the highlight stage waited for a canvas whose links animate to hold still, up to the step's
deadline); and the capture line read "10 copies skipped, every encoder behind". Now a card naming something else at
every place tried fails the step before the button (``pointer.hover_missed``), a located node's right-click that opens
no menu fails it (``pointer.node_menu_check``), the highlight stage runs once the canvas is nearly still or after
``node_locate.HIGHLIGHT_AFTER`` seconds (``node_locate.settled_enough``), and a page whose encoders are half behind
draws its next copies at the software ratio and quality (``canvas_alone.LOWER_AT``).
"""

from __future__ import annotations

import numpy as np

from app.services.video_studio import canvas_alone, canvas_ocr, capture_rate, node_locate, pointer

NAMES = ("Storm-1567", "Intrusion Set Storm-1567")


def _target(by: str = "highlight") -> pointer.Target:
    return pointer.Target((1108.0, 375.0), {}, {"node": "Storm-1567"}, located={"by": by}, names=NAMES)


# -- 1. the step lands on the named node or fails ------------------------------------------------------------------


def test_no_menu_on_a_located_node_fails_the_step():
    checked, why = pointer.node_menu_check(None, _target(), (1108.0, 375.0))
    assert checked is None and why is not None
    assert why.startswith("no menu opened on the node 'Storm-1567': the point it resolved by highlight, (1108, 375)")


def test_a_menu_naming_the_node_passes_and_a_wrong_one_fails_as_before():
    named = {"items": ["Expand"], "titles": ["Intrusion Set Storm-1567"]}
    assert pointer.node_menu_check(named, _target(), (1.0, 1.0)) == ({"title": "Intrusion Set Storm-1567", "node": True}, None)
    wrong = {"items": ["Expand"], "titles": ["targets"]}
    assert pointer.node_menu_check(wrong, _target(), (1.0, 1.0)) == pointer.menu_check(wrong, _target())


def test_no_menu_on_a_point_or_an_element_is_not_checked():
    unlocated = pointer.Target((1.0, 1.0), {}, {"at": {"x": 1, "y": 1}})
    assert pointer.node_menu_check(None, unlocated, (1.0, 1.0)) == (None, None)


def test_cards_of_other_things_at_every_place_stop_the_button():
    tried = [
        {"x": 1108.0, "y": 375.0, "title": "targets", "node": False},
        {"x": 990.0, "y": 671.0, "title": "[T1562.001] Disable or Modify Tools", "node": False},
        {"x": 937.0, "y": 577.0, "title": None, "node": False},
    ]
    why = pointer.hover_missed(tried, _target())
    assert why is not None and "never names the node 'Storm-1567' at the 3 place(s) resolved by highlight" in why
    assert "'targets', '[T1562.001] Disable or Modify Tools'" in why and why.endswith("nothing was pressed")


def test_a_card_naming_the_node_or_no_card_at_all_lets_the_button_go():
    assert pointer.hover_missed([], _target()) is None
    assert pointer.hover_missed([{"x": 1.0, "y": 1.0, "title": None, "node": False}], _target()) is None
    named = [{"x": 1.0, "y": 1.0, "title": "targets", "node": False}, {"x": 2.0, "y": 2.0, "title": "Storm-1567", "node": True}]
    assert pointer.hover_missed(named, _target()) is None


# -- 2. node resolution in seconds on an animated canvas -----------------------------------------------------------


def _grey(changed_share: float) -> tuple[np.ndarray, np.ndarray]:
    before = np.zeros((100, 100), dtype=np.uint8)
    after = before.copy()
    after.flat[: int(changed_share * after.size)] = 200
    return before, after


def test_a_still_canvas_is_read_at_once():
    before, after = _grey(0.0)
    assert node_locate.settled_enough(before, after, 0.0)


def test_a_canvas_whose_links_animate_is_read_once_nearly_still():
    before, after = _grey(0.01)
    assert not node_locate.still(before, after)
    assert not node_locate.settled_enough(before, after, 0.5)
    assert node_locate.settled_enough(before, after, node_locate.NEARLY_STILL_AFTER)


def test_a_canvas_moving_anyhow_is_read_within_the_bound():
    before, after = _grey(0.3)
    assert not node_locate.settled_enough(before, after, node_locate.HIGHLIGHT_AFTER - 0.1)
    assert node_locate.settled_enough(before, after, node_locate.HIGHLIGHT_AFTER)
    assert node_locate.HIGHLIGHT_AFTER < 5.0 < 15.0


async def test_ocr_is_not_repeated_on_a_canvas_nearly_still_since_a_miss_and_drift_is_measured_from_the_read(monkeypatch):
    base = np.zeros((100, 100), dtype=np.uint8)
    shots = []
    for share in (0.0, 0.01, 0.015, 0.03):
        grey = base.copy()
        grey.flat[: int(share * grey.size)] = 200
        shots.append(grey)
    served = iter(shots)
    reads: list[int] = []

    async def seen(_page, box, _over=None):
        return node_locate._Shot(box, [], b"png", 1.0, next(served))

    async def read_words(_picture, _ratio):
        reads.append(1)
        return []

    monkeypatch.setattr(node_locate, "_seen", seen)
    monkeypatch.setattr(canvas_ocr, "read_words", read_words)
    canvas = {"box": [0, 0, 100, 100]}
    shot = await node_locate._read_canvas(None, canvas, ("Storm-1567",))
    for _ in range(3):
        shot = await node_locate._read_canvas(None, canvas, ("Storm-1567",), known=shot)
    # Animated by 1 % then 1.5 % of its pixels from the read: no new read; 3 % from it: read again.
    assert len(reads) == 2


# -- 3. copies keep up on a GPU ------------------------------------------------------------------------------------


def test_a_gpu_page_draws_its_copies_smaller_once_its_encoders_are_half_behind():
    gpu = capture_rate.GlBackend(capture_rate.GPU_GL_ARGS, gpu=True)
    script = canvas_alone.tap_script(
        30, "(el) => 'c'", ratio=gpu.alone_ratio, quality=gpu.alone_quality, encoders=gpu.alone_encoders
    )
    assert f"LOW_RATIO = {canvas_alone.ALONE_RATIO!r}, LOW_QUALITY = {canvas_alone.ALONE_QUALITY!r}" in script
    assert f"LOWER = Math.ceil(FLIGHT * {canvas_alone.LOWER_AT!r})" in script
    assert "const lower = RATIO > LOW_RATIO && flight >= LOWER;" in script
    assert "lower ? Math.min(QUALITY, LOW_QUALITY) : QUALITY" in script
    assert "__" not in script.replace("__xtm", "")
    assert 0 < canvas_alone.LOWER_AT < 1


def test_a_software_page_never_lowers_below_its_own_ratio():
    script = canvas_alone.tap_script(30, "(el) => 'c'")
    assert "RATIO = 1.0," in script and "LOW_RATIO = 1.0," in script


async def test_the_copies_drawn_smaller_are_kept_and_said(tmp_path):
    class _Lowered:
        async def evaluate(self, *_args):
            return {"frames": [], "off": None, "busy": 0, "lowered": 12}

    tap = canvas_alone.Tap(_Lowered(), str(tmp_path), origin=0.0, max_copies=10)
    await tap.drain()
    kept = await tap.stop()
    assert kept["lowered"] == 12 and "busy" not in kept
    said = canvas_alone.capture_words({"alone": {**kept, "runs": [{"composited": True}]}})
    assert said.endswith("; 12 copies drawn smaller to keep up") and "every encoder behind" not in said
