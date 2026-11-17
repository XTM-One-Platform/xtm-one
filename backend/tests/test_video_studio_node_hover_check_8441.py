"""A node step checks what the page shows under the cursor before it presses, reads the canvas by OCR once, and a
GPU worker's page encodes its canvas copies on several threads.

On a GPU recording lane a ``right_click`` on a hub node located by its highlight landed on a relationship (its
menu headed 'targets' or 'uses'), each node took about 20 s to locate, and the canvas copies of a re-layout ran no
faster than the screencast painted it. The recorder now reads the card the page shows while the cursor rests
(``pointer.hover_check``) and moves to the node's next candidate place before any button when that card names
something else; a canvas still since an OCR read that found nothing is not read again
(``node_locate._read_canvas``); the label of a hub read upright with links crossing it is a candidate place
(``_Shot.crossed``); and a GPU page encodes its copies on ``canvas_alone.GPU_ENCODERS`` threads, the copies it had
to skip counted on the capture line.
"""

from __future__ import annotations

import numpy as np
import pytest

from app.services.video_studio import canvas_alone, canvas_ocr, capture, capture_rate, node_locate, pointer

NAMES = ("Storm-1567", "Intrusion Set Storm-1567")
CARD = (1016.0, 543.0, 300.0, 180.0)


def _card(title: str, box=CARD) -> tuple[str, str, tuple[float, float, float, float]]:
    return (f"DIV|{title}|{','.join(str(round(v)) for v in box)}", title, box)


# -- the hover card under the cursor --------------------------------------------------------------------------------


def test_a_card_naming_the_node_passes():
    read = pointer.hover_check([], [_card("Storm-1567")], (1000.0, 527.0), NAMES)
    assert read is not None and read["node"] is True and read["title"] == "Storm-1567"


def test_a_card_headed_by_a_relationship_type_fails():
    read = pointer.hover_check([], [_card("targets")], (1000.0, 527.0), NAMES)
    assert read == {"title": "targets", "node": False, "key": _card("targets")[0]}


def test_no_card_checks_nothing():
    assert pointer.hover_check([], [], (1000.0, 527.0), NAMES) is None


def test_an_element_shown_before_the_rest_is_not_the_card():
    toast = _card("Saved", (20.0, 20.0, 200.0, 40.0))
    assert pointer.hover_check([toast], [toast], (30.0, 30.0), NAMES) is None


def test_an_element_shown_far_from_the_cursor_is_not_the_card():
    far = _card("targets", (1500.0, 900.0, 300.0, 100.0))
    assert pointer.hover_check([], [far], (100.0, 100.0), NAMES) is None


def test_the_card_read_at_the_place_before_is_stale():
    old = _card("targets")
    assert pointer.hover_check([], [old], (1000.0, 527.0), NAMES, stale=old[0]) is None


def test_the_shown_script_is_bounded():
    assert "__" not in pointer._SHOWN_JS.replace("__xtm", "")
    assert "[role=menu]" in pointer._SHOWN_JS and "querySelector('canvas')" in pointer._SHOWN_JS


class _Page:
    """A page whose hover card names what lies under the cursor: *cards* maps a point to its card's title."""

    def __init__(self, cards: dict[tuple[float, float], str]) -> None:
        self.cards, self.at = cards, (0.0, 0.0)
        self.mouse = self

    async def move(self, x: float, y: float) -> None:
        self.at = (x, y)

    async def evaluate(self, script: str, *_args):
        if script is pointer._SHOWN_JS:
            near = min(self.cards, key=lambda p: abs(p[0] - self.at[0]) + abs(p[1] - self.at[1]), default=None)
            if near is None or abs(near[0] - self.at[0]) + abs(near[1] - self.at[1]) > 2:
                return []
            title = self.cards[near]
            box = [near[0] + 16, near[1] + 16, 300, 180]
            return [[f"DIV|{title}|{box}", title, box]]
        return None


class _Recorder:
    """What ``_hover_to_node`` needs of the recorder: the cursor and its rest."""

    _xy = staticmethod(capture._Recorder._xy)
    _hover_to_node = capture._Recorder._hover_to_node

    def __init__(self, page: _Page) -> None:
        self.page = page

    async def move_to(self, x: float, y: float) -> None:
        await self.page.move(x, y)

    async def _hover(self, _ms: int) -> None:
        return None


def _target(point, others) -> pointer.Target:
    return pointer.Target(point, {}, {"node": "Storm-1567"}, located={"by": "highlight"}, names=NAMES, others=others)


async def test_a_relationship_under_the_cursor_moves_it_to_the_node_before_the_button():
    page = _Page({(990.0, 671.0): "targets", (1196.0, 321.0): "Storm-1567"})
    recorder = _Recorder(page)
    await recorder.move_to(990.0, 671.0)
    first = _target((990.0, 671.0), ((1196.0, 321.0), (800.0, 400.0)))
    tried, target = await recorder._hover_to_node(first, [], 0)
    assert target.point == (1196.0, 321.0) and page.at == (1196.0, 321.0)
    # The first place stays a candidate of the menu check, after the one the card named.
    assert target.others == ((990.0, 671.0), (800.0, 400.0))
    assert tried == [
        {"x": 990.0, "y": 671.0, "title": "targets", "node": False},
        {"x": 1196.0, "y": 321.0, "title": "Storm-1567", "node": True},
    ]


async def test_no_place_naming_the_node_leaves_the_cursor_where_the_step_fails():
    page = _Page({(990.0, 671.0): "targets", (800.0, 400.0): "uses"})
    recorder = _Recorder(page)
    await recorder.move_to(990.0, 671.0)
    tried, target = await recorder._hover_to_node(_target((990.0, 671.0), ((800.0, 400.0),)), [], 0)
    # A card was read: the step fails before the button where the cursor stands, no glide back (#8476).
    assert target.point == (990.0, 671.0) and page.at == (800.0, 400.0)
    assert [entry["node"] for entry in tried] == [False, False]


async def test_a_page_without_hover_card_is_not_checked():
    page = _Page({})
    recorder = _Recorder(page)
    tried, target = await recorder._hover_to_node(_target((990.0, 671.0), ((800.0, 400.0),)), [], 0)
    assert tried == [] and target.point == (990.0, 671.0)


class _Clock:
    """What ``_searched`` needs of the recorder: the take's clock."""

    _searched = capture._Recorder._searched

    def __init__(self, now: float) -> None:
        self.t = now

    def now(self) -> float:
        return self.t


def test_the_search_before_the_place_naming_the_node_is_logged():
    # #9590: the first place named an edge, the second the node: the rest on the first is the recorder's search, up to
    # the cursor's arrival on the node (the clock less the rest on it).
    tried = [{"title": "indicates", "node": False}, {"title": "Storm-1567", "node": True}]
    assert _Clock(43.8)._searched(tried, 43.35, 250) == [43.35, 43.55]


def test_no_search_when_the_first_place_names_the_node_or_none_does():
    clock = _Clock(43.8)
    assert clock._searched([{"title": "Storm-1567", "node": True}], 43.35, 250) is None
    assert clock._searched([{"title": "targets", "node": False}, {"title": "uses", "node": False}], 43.35, 250) is None
    assert clock._searched([], 43.35, 250) is None


def test_node_buttons_are_the_pressing_actions():
    assert {"click", "double_click", "right_click", "drag"} == capture._NODE_BUTTONS
    assert pointer.HOVER_RETRIES >= pointer.MENU_RETRIES


# -- one OCR read a node -------------------------------------------------------------------------------------------


async def test_a_canvas_still_since_an_ocr_miss_is_not_read_again(monkeypatch):
    grey = np.zeros((100, 200), dtype=np.uint8)
    reads: list[int] = []

    async def seen(_page, box, _over=None):
        return node_locate._Shot(box, [], b"png", 1.0, grey.copy())

    async def read_words(_picture, _ratio):
        reads.append(1)
        return []

    monkeypatch.setattr(node_locate, "_seen", seen)
    monkeypatch.setattr(canvas_ocr, "read_words", read_words)
    canvas = {"box": [0, 0, 200, 100]}
    first = await node_locate._read_canvas(None, canvas, ("Storm-1567",))
    first.crossed.append((1.0, 2.0, 3.0, 4.0))
    second = await node_locate._read_canvas(None, canvas, ("Storm-1567",), known=first)
    assert len(reads) == 1 and second.found is None and second.crossed == [(1.0, 2.0, 3.0, 4.0)]
    moved = await node_locate._read_canvas(None, {"box": [0, 0, 200, 101]}, ("Storm-1567",), known=first)
    assert len(reads) == 2 and moved.crossed == []


# -- a hub's label crossed by its links ----------------------------------------------------------------------------


def _word(text: str, left: float, top: float = 50.0) -> canvas_ocr.Word:
    return canvas_ocr.Word(text, left, top, 8.0 * len(text), 12.0, 95.0)


def test_a_label_left_out_for_crossing_lines_is_a_candidate_place(monkeypatch):
    shot = node_locate._Shot((0.0, 0.0, 400.0, 200.0), [], b"", 1.0, np.zeros((200, 400), dtype=np.uint8))
    monkeypatch.setattr(canvas_ocr, "edge_label", lambda _grey, _box: canvas_ocr.ALONG_A_LINE)
    kept = node_locate._edge_labels(shot, [_word("Storm-1567", 100.0)], ("Storm-1567",))
    assert kept == [] and len(shot.crossed) == 1 and shot.left_out


def test_a_rotated_label_is_not_a_candidate_place(monkeypatch):
    shot = node_locate._Shot((0.0, 0.0, 400.0, 200.0), [], b"", 1.0, np.zeros((200, 400), dtype=np.uint8))
    monkeypatch.setattr(canvas_ocr, "edge_label", lambda _grey, _box: "rotated")
    node_locate._edge_labels(shot, [_word("Storm-1567", 100.0)], ("Storm-1567",))
    assert shot.crossed == []


async def test_the_crossed_label_comes_first_when_no_highlight_place_is_node_shaped(monkeypatch):
    shot = node_locate._Shot((0.0, 0.0, 1000.0, 800.0), [], b"", 1.0, np.zeros((800, 1000), dtype=np.uint8))
    shot.crossed.append((180.0, 300.0, 80.0, 12.0))
    highlight = node_locate.Highlight(
        (600.0, 500.0), (550.0, 450.0, 100.0, 100.0), 0.01, node_shaped=False, others=((700.0, 600.0),)
    )

    async def lit(_page, _entry, _shot):
        return highlight, b"after", {}

    monkeypatch.setattr(node_locate, "_highlight", lit)
    entry = node_locate.Entry(
        "Intrusion Set Storm-1567, 21 relationships", list=0, index=0, name="Elements of the graph"
    )
    identity = node_locate.match_label("Storm-1567", [entry]).identity
    assert identity is not None
    target = await node_locate._by_highlight(None, "Storm-1567", identity, shot, (1920, 1080))
    expected = node_locate.node_point((180.0, 300.0, 80.0, 12.0))[0]
    assert target.point == expected
    assert target.others[0] == (600.0, 500.0) and (700.0, 600.0) in target.others
    assert target.located["highlight"]["labelled"] == 1


# -- the canvas copies on a GPU ------------------------------------------------------------------------------------


def test_a_gpu_page_encodes_its_copies_on_several_threads():
    gpu = capture_rate.GlBackend(capture_rate.GPU_GL_ARGS, gpu=True)
    software = capture_rate.GlBackend(capture_rate.SOFTWARE_GL_ARGS, gpu=False)
    assert canvas_alone.GPU_ENCODERS > 1 and canvas_alone.gpu_encoders(8) == canvas_alone.GPU_ENCODERS
    assert gpu.alone_encoders == canvas_alone.gpu_encoders()
    assert software.alone_encoders == canvas_alone.ENCODERS == 1
    script = canvas_alone.tap_script(30, "(el) => 'c'", ratio=gpu.alone_ratio, quality=gpu.alone_quality, encoders=3)
    assert f"ENCODERS = 3, FLIGHT = {canvas_alone.flight_depth(3)};" in script
    assert "at = id % encoders.length" in script
    assert "encoders[at].postMessage" in script
    assert "__" not in script.replace("__xtm", "")


@pytest.mark.parametrize("encoders", [0, 1])
def test_one_encoder_at_least(encoders):
    assert "ENCODERS = 1," in canvas_alone.tap_script(30, "(el) => 'c'", encoders=encoders)


async def test_the_copies_skipped_are_kept_and_said(tmp_path):
    class _Busy:
        async def evaluate(self, *_args):
            return {"frames": [], "off": None, "busy": 7}

    tap = canvas_alone.Tap(_Busy(), str(tmp_path), origin=0.0, max_copies=10)
    await tap.drain()
    kept = await tap.stop()
    assert kept["busy"] == 7
    runs = [{"composited": False, "why": canvas_alone.NO_FASTER}]
    said = canvas_alone.capture_words({"alone": {**kept, "runs": runs}})
    assert said.endswith("; 7 copies skipped, every encoder behind")
    assert "skipped" not in canvas_alone.capture_words({"alone": {"runs": [{"composited": True}]}})
