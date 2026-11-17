"""A wheel turns over what scrolls, and a band a row off is no blink.

* A ``wheel`` (or ``scroll``) step by a distance on an app shell whose content scrolls in an inner container under a
  fixed top bar turned where the cursor stood: over the bar, every turn was lost and the take said "moved nothing".
  The recorder now asks the page whether the point's scroll chain (or the window) can move that way; when not, the
  cursor glides to the visible centre of the page's largest scroll container that can and the turns land there, the
  move logged (``wheel_moved``). A cursor already over a scrollable point stays.
* The band blink self-check compared a band's rows frame to frame by mean grey difference only: a band a
  decoded MP4 (a take repaired from it) drew one device row off read as a blink. A shift of up to
  :data:`scroll_pacing.BLINK_SHIFT` row is the band in place; a header pushed by several rows and back still blinks.
"""

from __future__ import annotations

import time
from types import SimpleNamespace

import numpy as np

from app.services.video_studio import capture, scroll_pacing
from app.services.video_studio.spec import parse_take_script, validate_take_script

BAR = (800.0, 30.0)
CONTAINER_CENTRE = [800.0, 480.0]


class _ShellPage:
    """An app shell: a fixed bar at the top, the content scrolling in ``div.MuiBox-root`` under it."""

    def __init__(self, answer) -> None:
        self.answer = answer
        self.asked: list = []
        self.turns: list[tuple[float, float]] = []
        self.moves: list[tuple[float, float]] = []
        self.mouse = self

    async def wheel(self, _x: float, y: float) -> None:
        self.turns.append((time.monotonic(), y))

    async def move(self, x: float, y: float) -> None:
        self.moves.append((x, y))

    async def evaluate(self, script: str, arg=None):
        if script == capture._WHEEL_TARGET_JS:
            self.asked.append(arg)
            if isinstance(self.answer, Exception):
                raise self.answer
            return self.answer
        return None


def _recorder(page: _ShellPage, at: tuple[float, float] = BAR):
    recorder = capture._Recorder.__new__(capture._Recorder)
    recorder.page = page
    recorder.script = SimpleNamespace(pace="natural", fps=30)
    recorder.x, recorder.y = at
    recorder.scrolls = []
    recorder.cursor = []
    recorder.t_start = time.time()
    recorder.pace = (0.0, 0.0, 0.0)

    async def settled() -> None:
        return None

    recorder._wait_scroll_end = settled
    return recorder


def _step(by: int):
    return parse_take_script(validate_take_script({"steps": [{"action": "wheel", "by": by, "seconds": 0.3}]})).steps[0]


async def test_a_wheel_over_a_fixed_bar_glides_to_the_inner_container_first():
    page = _ShellPage({"point": CONTAINER_CENTRE, "element": "div.MuiBox-root.css-1wkysct"})
    recorder = _recorder(page)
    await recorder._scroll(_step(600))
    assert page.asked == [[BAR[0], BAR[1], 1]]
    assert (recorder.x, recorder.y) == tuple(CONTAINER_CENTRE)
    assert page.moves and page.moves[-1] == tuple(CONTAINER_CENTRE)
    [logged] = recorder.scrolls
    assert logged["wheel_moved"] == {"to": CONTAINER_CENTRE, "element": "div.MuiBox-root.css-1wkysct"}
    assert sum(y for _t, y in page.turns) == 600


async def test_a_wheel_up_asks_for_the_upward_direction():
    page = _ShellPage(None)
    await _recorder(page)._scroll(_step(-300))
    assert page.asked == [[BAR[0], BAR[1], -1]]


async def test_a_wheel_over_a_point_that_scrolls_stays_where_the_cursor_is():
    for answer in (None, {"none": True}, RuntimeError("navigating")):
        page = _ShellPage(answer)
        recorder = _recorder(page)
        await recorder._scroll(_step(600))
        assert (recorder.x, recorder.y) == BAR and page.moves == []
        [logged] = recorder.scrolls
        assert "wheel_moved" not in logged
        assert sum(y for _t, y in page.turns) == 600


def test_the_page_script_names_the_container_it_moves_to():
    assert "__NAME__" not in capture._WHEEL_TARGET_JS
    assert "elementFromPoint" in capture._WHEEL_TARGET_JS and "overflowY" in capture._WHEEL_TARGET_JS


# ── the band blink self-check and a band a row off ───────────────────────


def _header() -> np.ndarray:
    rng = np.random.default_rng(11)
    return (rng.random((28, 120)) * 200).astype(np.float32)


TIMES = [20.03, 20.067, 20.1, 20.133, 20.167]


def test_a_band_one_row_off_and_back_is_no_blink():
    header = _header()
    for d in (1, -1):
        off = np.roll(header, d, axis=0)
        assert scroll_pacing.band_blinks([header, header, off, header, header], TIMES) == []


def test_a_band_pushed_by_several_rows_and_back_still_blinks():
    header = _header()
    pushed = np.roll(header, -8, axis=0)
    assert scroll_pacing.band_blinks([header, header, pushed, header, header], TIMES) == [20.1]


def test_encoder_noise_alone_is_no_blink():
    header = _header()
    rng = np.random.default_rng(3)
    noisy = header + rng.normal(0, 3, header.shape).astype(np.float32)
    assert scroll_pacing.band_blinks([header, header, noisy, header, header], TIMES) == []
