"""The browser tool's actions for a rendered UX review.

``viewport`` resizes the session's page within bounds, ``console`` returns what
the page logged since the last ``navigate`` (bounded, page errors included),
``hover`` moves the pointer over an element, ``drag`` presses, moves and
releases it with real input events, and ``screenshot`` with a
``selector`` captures that element only. Nothing here launches a browser: a
fake page records what the tool asked of it.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.services import browser_tool
from app.services.builtin_tools import build_browser_tool
from app.tools.base import ToolCall, ToolResult

URL = "https://app.example.com/agents"


class FakePage:
    """The slice of a Playwright page these actions drive, with its event emitters."""

    def __init__(self) -> None:
        self.url = URL
        self.listeners: dict[str, list] = {}
        # ``set_viewport_size`` resizes the page at once: ``window.innerWidth`` x ``innerHeight`` read it back (#9967).
        self.set_viewport_size = AsyncMock(side_effect=self._resize)
        self.hover = AsyncMock()
        self.screenshot = AsyncMock(return_value=b"page")
        # ``evaluate`` answers the reachable hover point (``None``: no point, the plain hover runs).
        # ``bounding_box`` answers the element's box (``None``: not visible, captured as is).
        self.element = SimpleNamespace(
            screenshot=AsyncMock(return_value=b"element"),
            evaluate=AsyncMock(return_value=None),
            bounding_box=AsyncMock(return_value=None),
        )
        self.viewport_size = {"width": 1440, "height": 900}
        # What ``_FULL_PAGE_HEIGHT_JS`` answers (``None``: no measure, the plain capture runs).
        self.full_page_height: object = None
        self.evaluate = AsyncMock(side_effect=self._evaluate)
        self.mouse = SimpleNamespace(move=AsyncMock(), down=AsyncMock(), up=AsyncMock())
        self.locator = MagicMock(return_value=SimpleNamespace(first=self.element))
        self.goto = AsyncMock()
        self.title = AsyncMock(return_value="Agents")

    def _resize(self, size: dict) -> None:
        self.viewport_size = dict(size)

    def _evaluate(self, script: str, *args):
        if script in (browser_tool._PAGE_SIZE_JS, browser_tool._PAGE_SIZE_NOW_JS):
            return [self.viewport_size["width"], self.viewport_size["height"]]
        if script == browser_tool._FULL_PAGE_HEIGHT_JS:
            return self.full_page_height
        return None

    def on(self, event: str, handler) -> None:
        self.listeners.setdefault(event, []).append(handler)

    def emit(self, event: str, payload) -> None:
        for handler in self.listeners.get(event, []):
            handler(payload)


def _message(kind: str, text: str, url: str = "https://app.example.com/assets/index.js", line: int = 12):
    return SimpleNamespace(type=kind, text=text, location={"url": url, "lineNumber": line, "columnNumber": 3})


@pytest.fixture
def page(monkeypatch):
    fake = FakePage()
    session = browser_tool.BrowserSession(pw=None, browser=None, context=None, page=fake)
    session.documents.watch(fake)
    session.console.watch(fake)
    monkeypatch.setattr(browser_tool, "_get_or_create_session", AsyncMock(return_value=session))

    async def allow(url: str) -> str:
        return url

    monkeypatch.setattr(browser_tool, "validate_outbound_url_async", allow)
    return fake


async def _call(arguments: dict) -> ToolResult:
    _defs, executor = build_browser_tool("agent:conv")
    return await executor.execute_tool(ToolCall(id="call-1", name="browser", arguments=arguments))


# ── The contract ────────────────────────────────────────────────────────────


def test_the_schema_offers_the_review_actions_and_their_arguments():
    defs, _executor = build_browser_tool("agent:conv")
    schema = defs[0].input_schema
    for action in ("hover", "drag", "viewport", "console"):
        assert action in schema["properties"]["action"]["enum"]
        assert f"- {action}:" in defs[0].description
    assert schema["properties"]["width"]["type"] == "integer"
    assert schema["properties"]["height"]["type"] == "integer"
    assert schema["properties"]["dx"]["type"] == schema["properties"]["dy"]["type"] == "number"
    assert schema["properties"]["steps"]["type"] == "integer"
    assert "screenshot" in schema["properties"]["selector"]["description"]
    assert schema["required"] == ["action"]


# ── viewport ──────────────────────────────────────────────────────────────


@pytest.mark.parametrize(("width", "height"), [(1440, 900), (1024, 768), (320, 240), (3840, 2160), (1440.0, 900)])
async def test_the_viewport_is_applied_to_the_page(page, width, height):
    result = await _call({"action": "viewport", "width": width, "height": height})
    assert not result.is_error
    page.set_viewport_size.assert_awaited_once_with({"width": int(width), "height": int(height)})
    assert f"{int(width)} x {int(height)}" in result.content


@pytest.mark.parametrize(
    ("width", "height"),
    [(319, 768), (1024, 239), (3841, 900), (1440, 2161), (1440.5, 900), ("1440", 900), (True, 900), (float("nan"), 900)],
)
async def test_a_viewport_out_of_bounds_or_not_whole_is_refused_before_the_page(page, width, height):
    result = await _call({"action": "viewport", "width": width, "height": height})
    assert result.is_error
    assert "viewport" in result.content
    page.set_viewport_size.assert_not_awaited()


async def test_a_viewport_needs_both_sides(page):
    result = await _call({"action": "viewport", "width": 1440})
    assert result.is_error
    assert result.content == "Error: 'width' and 'height' are required for the viewport action."


@pytest.fixture
def fake_clock(monkeypatch):
    """The settle wait's clock and pause in fake time: a pause advances the clock, nothing sleeps."""
    now = [0.0]

    async def pause(seconds: float) -> None:
        now[0] += seconds

    monkeypatch.setattr(browser_tool, "_paint_clock", lambda: now[0])
    monkeypatch.setattr(browser_tool, "_paint_sleep", pause)
    return now


async def test_the_viewport_answers_once_the_page_reports_the_new_size(page, fake_clock):
    # The page runs its resize late: the old size is read twice before the new one (#9967).
    reads = iter([[1440, 900], [1440, 900]])

    def lagging(script: str, *args):
        if script == browser_tool._PAGE_SIZE_NOW_JS:
            return next(reads, [page.viewport_size["width"], page.viewport_size["height"]])
        if script == browser_tool._PAGE_SIZE_JS:
            return [page.viewport_size["width"], page.viewport_size["height"]]
        return None

    page.evaluate.side_effect = lagging
    result = await _call({"action": "viewport", "width": 1024, "height": 800})
    assert not result.is_error
    assert "the page reports 1024 x 800 px" in result.content
    reads_now = [c for c in page.evaluate.await_args_list if c.args[0] == browser_tool._PAGE_SIZE_NOW_JS]
    assert len(reads_now) == 2 + browser_tool._VIEWPORT_STABLE_READS
    assert 0 < fake_clock[0] < browser_tool._VIEWPORT_SETTLE_SECONDS


@pytest.mark.parametrize("width", [1440, 1024, 1440, 1024, 1440, 1024, 1440, 1024, 1440, 1024])
async def test_an_evaluate_right_after_the_viewport_reads_the_new_width(page, fake_clock, width):
    result = await _call({"action": "viewport", "width": width, "height": 800})
    assert f"the page reports {width} x 800 px" in result.content
    assert await page.evaluate(browser_tool._PAGE_SIZE_JS) == [width, 800]


async def test_one_matching_read_then_the_old_width_is_not_an_answer(page, fake_clock):
    # The page read the new width once, then the old one again before it held (#10308): the action answers only once
    # the size holds on consecutive reads.
    reads = iter([[1440, 900], [1024, 800], [1440, 900], [1440, 900]])

    def flapping(script: str, *args):
        if script == browser_tool._PAGE_SIZE_NOW_JS:
            return next(reads, [1024, 800])
        return [1024, 800]

    page.evaluate.side_effect = flapping
    result = await _call({"action": "viewport", "width": 1024, "height": 800})
    assert "the page reports 1024 x 800 px" in result.content
    reads_now = [c for c in page.evaluate.await_args_list if c.args[0] == browser_tool._PAGE_SIZE_NOW_JS]
    assert len(reads_now) == 4 + browser_tool._VIEWPORT_STABLE_READS


async def test_an_evaluate_on_a_page_that_lost_the_size_applies_it_again_first(page, fake_clock):
    await _call({"action": "viewport", "width": 1440, "height": 900})
    page.viewport_size = {"width": 1024, "height": 900}  # the page fell back to the old width after the answer
    page.set_viewport_size.reset_mock()
    page.evaluate.side_effect = lambda script, *args: (
        [page.viewport_size["width"], page.viewport_size["height"]] if script == browser_tool._PAGE_SIZE_NOW_JS
        else page.viewport_size["width"]
    )
    result = await _call({"action": "evaluate", "expression": "window.innerWidth"})
    assert not result.is_error
    page.set_viewport_size.assert_awaited_once_with({"width": 1440, "height": 900})
    assert "1440" in result.content
    assert "Note: the viewport" not in result.content


async def test_an_evaluate_on_a_page_that_never_takes_the_size_says_so(page, fake_clock):
    await _call({"action": "viewport", "width": 1440, "height": 900})
    page.set_viewport_size.side_effect = None  # the page keeps 1024 whatever is asked
    page.evaluate.side_effect = lambda script, *args: (
        [1024, 900] if script in (browser_tool._PAGE_SIZE_NOW_JS, browser_tool._PAGE_SIZE_JS) else 1024
    )
    result = await _call({"action": "evaluate", "expression": "window.innerWidth"})
    assert not result.is_error
    assert "the viewport is 1440 x 900 px but the page reported 1024 x 900 px" in result.content


async def test_a_page_that_runs_no_frame_still_answers_with_its_size(page, fake_clock, monkeypatch):
    # A busy SPA re-rendering after the resize runs no animation frame for seconds: the size is read without one
    # and the frame wait is bounded, never reported as 0 x 0 (#10037).
    monkeypatch.setattr(browser_tool, "_VIEWPORT_PAINT_SECONDS", 0.01)

    async def never_paints(script: str, *args):
        if script == browser_tool._PAGE_SIZE_JS:
            await asyncio.Event().wait()
        if script == browser_tool._PAGE_SIZE_NOW_JS:
            return [page.viewport_size["width"], page.viewport_size["height"]]
        return None

    page.evaluate.side_effect = never_paints
    result = await asyncio.wait_for(_call({"action": "viewport", "width": 1024, "height": 800}), 5)
    assert not result.is_error
    assert "the page reports 1024 x 800 px" in result.content
    assert "0 x 0" not in result.content


async def test_one_stuck_read_never_holds_the_whole_wait(page, fake_clock, monkeypatch):
    # The first read hangs; it is cut at the read budget and the next one answers (#10037).
    monkeypatch.setattr(browser_tool, "_VIEWPORT_READ_SECONDS", 0.01)
    calls = {"now": 0}

    async def first_read_hangs(script: str, *args):
        if script == browser_tool._PAGE_SIZE_NOW_JS:
            calls["now"] += 1
            if calls["now"] == 1:
                await asyncio.Event().wait()
            return [page.viewport_size["width"], page.viewport_size["height"]]
        return [page.viewport_size["width"], page.viewport_size["height"]]

    page.evaluate.side_effect = first_read_hangs
    result = await asyncio.wait_for(_call({"action": "viewport", "width": 1024, "height": 800}), 5)
    assert "the page reports 1024 x 800 px" in result.content
    assert calls["now"] == 1 + browser_tool._VIEWPORT_STABLE_READS


async def test_a_page_that_never_reports_the_size_is_named_at_the_deadline(page, fake_clock):
    page.evaluate.side_effect = lambda script, *args: [1440, 900]
    result = await _call({"action": "viewport", "width": 1024, "height": 800})
    assert not result.is_error
    assert "still reports 1440 x 900 px" in result.content
    assert fake_clock[0] == pytest.approx(browser_tool._VIEWPORT_SETTLE_SECONDS)
    assert browser_tool._VIEWPORT_SETTLE_SECONDS == 10.0
    page.set_viewport_size.assert_awaited_once_with({"width": 1024, "height": 800})


async def test_a_size_read_that_fails_is_retried_then_named_as_unread_never_0_x_0(page, fake_clock):
    page.evaluate.side_effect = RuntimeError("Execution context was destroyed")
    result = await _call({"action": "viewport", "width": 1024, "height": 800})
    assert not result.is_error
    assert "answered no read of window.innerWidth x innerHeight" in result.content
    assert "0 x 0" not in result.content


def test_a_new_session_opens_at_the_default_size():
    assert browser_tool.DEFAULT_VIEWPORT == {"width": 1280, "height": 720}
    assert browser_tool.viewport_size(1280, 720) == browser_tool.DEFAULT_VIEWPORT


# ── console ───────────────────────────────────────────────────────────────


async def test_console_returns_errors_first_then_warnings_then_the_rest(page):
    page.emit("console", _message("warning", "[Input] rendered without a label"))
    page.emit("console", _message("log", "ready"))
    page.emit("pageerror", SimpleNamespace(message="TypeError: x is undefined"))
    page.emit("console", _message("error", "GET /api/me 500"))
    result = await _call({"action": "console"})
    assert not result.is_error
    lines = result.content.splitlines()
    assert URL in lines[0] and "4 message(s), 4 distinct" in lines[0]
    assert lines[1] == "[pageerror] TypeError: x is undefined"
    assert lines[2].startswith("[error] GET /api/me 500")
    assert lines[3] == "[warning] [Input] rendered without a label (https://app.example.com/assets/index.js:12)"
    assert lines[4].startswith("[log] ready")


async def test_a_repeated_message_is_shown_once_with_its_count(page):
    repeated = '[["me"]]: No queryFn was passed as an option, and no default queryFn was found.'
    page.emit("console", _message("log", "boot"))
    for _ in range(150):
        page.emit("console", _message("error", repeated))
    page.emit("console", _message("warning", "slow render"))
    result = await _call({"action": "console"})
    assert not result.is_error
    assert len(result.content) < 2_000
    assert result.content.count(repeated) == 1
    lines = result.content.splitlines()
    assert "152 message(s), 3 distinct" in lines[0]
    assert lines[1].startswith(f"[error] x150 {repeated}")
    assert lines[2].startswith("[warning] slow render")
    assert lines[3].startswith("[log] boot")


async def test_level_keeps_the_severe_messages_and_counts_the_rest(page):
    page.emit("console", _message("log", "ready"))
    page.emit("console", _message("log", "ready"))
    page.emit("console", _message("warning", "slow render"))
    page.emit("pageerror", SimpleNamespace(message="TypeError: x is undefined"))
    errors = (await _call({"action": "console", "level": "error"})).content
    assert "TypeError" in errors and "slow render" not in errors and "ready" not in errors
    assert "3 below level 'error' not shown (2 distinct)" in errors
    warnings = (await _call({"action": "console", "level": "warning"})).content
    assert "TypeError" in warnings and "slow render" in warnings and "[log]" not in warnings
    assert "2 below level 'warning' not shown (1 distinct)" in warnings


async def test_level_with_nothing_that_severe_says_so(page):
    page.emit("console", _message("log", "ready"))
    content = (await _call({"action": "console", "level": "error"})).content
    assert content.splitlines()[-1] == "No message at level 'error' or above."
    assert "1 below level 'error' not shown" in content


async def test_an_unknown_level_is_refused(page):
    result = await _call({"action": "console", "level": "debug"})
    assert result.is_error
    assert "level must be one of error, warning" in result.content


def test_the_answer_is_cut_with_what_it_does_not_show_counted():
    log = browser_tool.ConsoleLog()
    page = FakePage()
    log.watch(page)
    for i in range(browser_tool.MAX_CONSOLE_MESSAGES):
        page.emit("console", _message("log", f"{i:03d} " + "y" * 200))
    content = log.describe(URL)
    assert len(content) <= browser_tool.MAX_CONSOLE_ANSWER_CHARS + 200
    last = content.splitlines()[-1]
    assert last.startswith("... ") and "more distinct message(s)" in last and "level='error'" in last
    shown = len(content.splitlines()) - 2
    hidden = int(last.split()[1])
    assert shown + hidden == browser_tool.MAX_CONSOLE_MESSAGES


async def test_console_says_when_nothing_was_logged(page):
    result = await _call({"action": "console"})
    assert not result.is_error
    assert result.content.startswith("No console message or page error since the last navigate")


async def test_a_navigate_starts_a_new_log(page):
    page.emit("console", _message("error", "old page"))
    result = await _call({"action": "navigate", "url": "https://app.example.com/flows"})
    assert not result.is_error
    page.emit("console", _message("log", "new page"))
    content = (await _call({"action": "console"})).content
    assert "new page" in content and "old page" not in content


def test_the_log_is_bounded_and_counts_what_it_dropped():
    log = browser_tool.ConsoleLog()
    page = FakePage()
    log.watch(page)
    for i in range(browser_tool.MAX_CONSOLE_MESSAGES + 5):
        page.emit("console", _message("log", f"m{i}"))
    page.emit("console", _message("log", "x" * (browser_tool.MAX_CONSOLE_TEXT_CHARS + 50)))
    assert len(log.entries) == browser_tool.MAX_CONSOLE_MESSAGES
    assert log.dropped == 6
    last = list(log.entries.values())[-1]
    assert last.text.endswith(" [cut]")
    assert len(last.text) == browser_tool.MAX_CONSOLE_TEXT_CHARS + len(" [cut]")
    assert "6 older dropped" in log.describe(URL)


def test_a_dropped_entry_counts_every_message_it_folded():
    log = browser_tool.ConsoleLog()
    page = FakePage()
    log.watch(page)
    for _ in range(4):
        page.emit("console", _message("error", "first"))
    for i in range(browser_tool.MAX_CONSOLE_MESSAGES):
        page.emit("console", _message("log", f"m{i}"))
    assert ("error", "first") not in log.entries
    assert log.dropped == 4


def test_a_message_playwright_shaped_differently_is_read_or_skipped_never_raised():
    log = browser_tool.ConsoleLog()
    page = FakePage()
    log.watch(page)
    # Methods instead of properties (older releases), and no location at all.
    page.emit("console", SimpleNamespace(type=lambda: "info", text=lambda: "as methods", location=lambda: None))

    class Broken:
        @property
        def type(self):
            raise RuntimeError("gone")

    page.emit("console", Broken())
    assert [(e.kind, e.text, e.where, e.count) for e in log.entries.values()] == [("info", "as methods", "", 1)]


# ── hover ─────────────────────────────────────────────────────────────────


async def test_hover_moves_the_pointer_over_the_element(page):
    result = await _call({"action": "hover", "selector": "[data-testid=help]"})
    assert not result.is_error
    page.hover.assert_awaited_once_with("[data-testid=help]")
    assert URL in result.content


async def test_hover_needs_a_selector(page):
    result = await _call({"action": "hover"})
    assert result.is_error
    page.hover.assert_not_awaited()


async def test_hover_aims_at_a_point_the_element_itself_receives(page):
    # A curved SVG edge stroke: its box centre lands on the grid beneath, a point on its geometry does not.
    page.element.evaluate.return_value = {"x": 412.4, "y": 233.6}
    result = await _call({"action": "hover", "selector": "path[data-edge-hit]"})
    assert not result.is_error
    page.locator.assert_called_with("path[data-edge-hit]")
    script = page.element.evaluate.await_args.args[0]
    assert "elementFromPoint" in script and "getPointAtLength" in script
    page.mouse.move.assert_awaited_once_with(412.4, 233.6)
    page.hover.assert_not_awaited()
    assert "at (412, 234)" in result.content


@pytest.mark.parametrize("answer", [None, {"x": "a", "y": 1}, {"x": float("nan"), "y": 2}, [1, 2]])
async def test_hover_without_a_reachable_point_falls_back_to_playwright(page, answer):
    page.element.evaluate.return_value = answer
    result = await _call({"action": "hover", "selector": "#menu"})
    assert not result.is_error
    page.hover.assert_awaited_once_with("#menu")
    page.mouse.move.assert_not_awaited()


async def test_hover_falls_back_when_the_point_probe_fails(page):
    page.element.evaluate.side_effect = RuntimeError("Execution context was destroyed")
    result = await _call({"action": "hover", "selector": "#menu"})
    assert not result.is_error
    page.hover.assert_awaited_once_with("#menu")


# ── drag ──────────────────────────────────────────────────────────────────


async def test_drag_presses_moves_by_the_offset_and_releases_with_real_input(page):
    # A resize handle with setPointerCapture ignores an untrusted PointerEvent: only mouse input moves it.
    page.element.evaluate.return_value = {"x": 300.0, "y": 400.0}
    calls: list[tuple] = []
    page.mouse.move.side_effect = lambda *a, **k: calls.append(("move", a, k))
    page.mouse.down.side_effect = lambda *a, **k: calls.append(("down", a, k))
    page.mouse.up.side_effect = lambda *a, **k: calls.append(("up", a, k))
    result = await _call(
        {"action": "drag", "selector": '[data-testid="fleet.studio.resize"]', "dx": -120, "dy": 0, "steps": 12}
    )
    assert not result.is_error, result.content
    assert calls == [
        ("move", (300.0, 400.0), {}),
        ("down", (), {}),
        ("move", (180.0, 400.0), {"steps": 12}),
        ("up", (), {}),
    ]
    assert "from (300, 400) to (180, 400) in 12 move(s)" in result.content
    assert URL in result.content


async def test_drag_presses_at_the_box_centre_without_a_reachable_point(page):
    page.element.bounding_box.return_value = {"x": 10, "y": 20, "width": 8, "height": 100}
    result = await _call({"action": "drag", "selector": "#divider", "dy": 50})
    assert not result.is_error, result.content
    assert page.mouse.move.await_args_list[0].args == (14.0, 70.0)
    assert page.mouse.move.await_args_list[1].args == (14.0, 120.0)
    assert page.mouse.move.await_args_list[1].kwargs == {"steps": browser_tool.DEFAULT_DRAG_STEPS}
    page.mouse.up.assert_awaited_once()


async def test_drag_releases_the_button_when_the_move_fails(page):
    page.element.evaluate.return_value = {"x": 1.0, "y": 1.0}
    page.mouse.move.side_effect = [None, RuntimeError("Target closed")]
    result = await _call({"action": "drag", "selector": "#divider", "dx": 5})
    assert result.is_error
    page.mouse.up.assert_awaited_once()


async def test_drag_of_nothing_visible_is_refused_without_pressing(page):
    result = await _call({"action": "drag", "selector": "#gone", "dx": 5})
    assert result.is_error
    assert "no visible element" in result.content
    page.mouse.down.assert_not_awaited()


@pytest.mark.parametrize(
    "arguments",
    [
        {},
        {"dx": 0, "dy": 0},
        {"dx": 5000},
        {"dx": True},
        {"dx": "10"},
        {"dx": 10, "steps": 0},
        {"dx": 10, "steps": 101},
        {"dx": 10, "steps": 2.5},
    ],
)
async def test_a_drag_out_of_bounds_is_refused_before_the_page(page, arguments):
    result = await _call({"action": "drag", "selector": "#divider", **arguments})
    assert result.is_error
    assert "refused" in result.content
    page.mouse.down.assert_not_awaited()
    page.mouse.move.assert_not_awaited()


async def test_drag_needs_a_selector(page):
    result = await _call({"action": "drag", "dx": 10})
    assert result.is_error
    page.mouse.down.assert_not_awaited()


# ── screenshot of one element ─────────────────────────────────────────────


async def test_a_screenshot_with_a_selector_captures_that_element(page):
    shot = await browser_tool.browser_screenshot("agent:conv", selector="main h1")
    assert shot == b"element"
    page.locator.assert_called_once_with("main h1")
    page.element.screenshot.assert_awaited_once()
    page.screenshot.assert_not_awaited()


async def test_an_element_that_fits_the_viewport_is_captured_without_resizing(page):
    page.element.bounding_box.return_value = {"x": 0, "y": 0, "width": 1440, "height": 900}
    assert await browser_tool.browser_screenshot("agent:conv", selector="#root") == b"element"
    page.set_viewport_size.assert_not_awaited()


async def test_an_element_taller_than_the_viewport_is_captured_with_the_page_grown_to_it_then_restored(page):
    # A page paints what intersects its viewport: the cards of a 4129 px root were background past 900 px.
    page.element.bounding_box.return_value = {"x": 0, "y": 0, "width": 1440, "height": 4128.6}
    sizes: list[dict] = []
    page.set_viewport_size.side_effect = lambda size: sizes.append(size)
    page.element.screenshot.side_effect = lambda **_kw: sizes.append("capture") or b"element"

    assert await browser_tool.browser_screenshot("agent:conv", selector="#root") == b"element"
    assert sizes == [{"width": 1440, "height": 4129}, "capture", {"width": 1440, "height": 900}]
    page.evaluate.assert_awaited_once()  # two animation frames to paint before the capture


async def test_the_grown_viewport_is_bounded_and_restored_when_the_capture_fails(page):
    page.element.bounding_box.return_value = {"x": 0, "y": 0, "width": 1440, "height": 50_000}
    page.element.screenshot.side_effect = RuntimeError("Target closed")

    with pytest.raises(RuntimeError):
        await browser_tool.browser_screenshot("agent:conv", selector="#root")
    assert [c.args[0] for c in page.set_viewport_size.await_args_list] == [
        {"width": 1440, "height": browser_tool.MAX_ELEMENT_CAPTURE_HEIGHT},
        {"width": 1440, "height": 900},
    ]


async def test_a_blank_capture_is_flagged_in_its_result(page):
    import io

    from PIL import Image

    blank = io.BytesIO()
    Image.new("RGB", (200, 400), (0xF2, 0xF2, 0xF3)).save(blank, format="PNG")
    page.element.screenshot.return_value = blank.getvalue()

    result = await _call({"action": "screenshot", "selector": "#root"})
    text = result if isinstance(result, str) else result.content
    assert "dominant #F2F2F3 100.0%" in text
    assert "This capture may be blank" in text


async def test_a_painted_capture_is_not_flagged(page):
    import io

    from PIL import Image, ImageDraw

    painted = io.BytesIO()
    image = Image.new("RGB", (200, 400), (0xF2, 0xF2, 0xF3))
    ImageDraw.Draw(image).rectangle((10, 10, 120, 80), fill=(0x00, 0x15, 0xA8))
    image.save(painted, format="PNG")
    page.element.screenshot.return_value = painted.getvalue()

    result = await _call({"action": "screenshot", "selector": "#root"})
    text = result if isinstance(result, str) else result.content
    assert "#0015A8" in text
    assert "may be blank" not in text


async def test_a_screenshot_without_a_selector_is_unchanged(page):
    assert await browser_tool.browser_screenshot("agent:conv", full_page=True) == b"page"
    page.screenshot.assert_awaited_once_with(full_page=True)
    page.locator.assert_not_called()


async def test_a_full_page_capture_of_an_app_shell_grows_the_viewport_so_the_shell_is_painted_once(page):
    # An app that scrolls inside its container: a stitched capture painted its fixed sidebar mid-page (3433 px).
    page.full_page_height = 3432.4
    sizes: list = []
    page.set_viewport_size.side_effect = lambda size: sizes.append(size) or page._resize(size)
    page.screenshot.side_effect = lambda **kw: sizes.append(("capture", kw)) or b"page"

    assert await browser_tool.browser_screenshot("agent:conv", full_page=True) == b"page"
    assert sizes == [
        {"width": 1440, "height": 3433},
        ("capture", {"full_page": True}),
        {"width": 1440, "height": 900},
    ]
    assert page.viewport_size == {"width": 1440, "height": 900}


@pytest.mark.parametrize("answer", [None, 900, 400, True, "3000"])
async def test_a_full_page_that_fits_or_answers_no_measure_is_captured_as_before(page, answer):
    page.full_page_height = answer
    assert await browser_tool.browser_screenshot("agent:conv", full_page=True) == b"page"
    page.screenshot.assert_awaited_once_with(full_page=True)
    page.set_viewport_size.assert_not_awaited()


async def test_a_full_page_capture_is_bounded_and_restores_the_viewport_when_it_fails(page):
    page.full_page_height = 50_000
    page.screenshot.side_effect = RuntimeError("Target closed")

    with pytest.raises(RuntimeError):
        await browser_tool.browser_screenshot("agent:conv", full_page=True)
    assert [c.args[0] for c in page.set_viewport_size.await_args_list] == [
        {"width": 1440, "height": browser_tool.MAX_ELEMENT_CAPTURE_HEIGHT},
        {"width": 1440, "height": 900},
    ]


async def test_a_viewport_screenshot_measures_nothing(page):
    assert await browser_tool.browser_screenshot("agent:conv") == b"page"
    page.screenshot.assert_awaited_once_with(full_page=False)
    assert all(c.args[0] != browser_tool._FULL_PAGE_HEIGHT_JS for c in page.evaluate.await_args_list)


async def test_a_selector_and_full_page_together_are_refused(page):
    result = await _call({"action": "screenshot", "selector": "main", "full_page": True})
    assert result.is_error
    assert "not both" in result.content
    page.screenshot.assert_not_awaited()


# ── read-only profile sessions ────────────────────────────────────────────


def test_a_read_only_profile_session_may_resize_and_read_the_console_but_not_hover():
    from app.services.builtin_tools.web import BROWSER_INTERACTIVE_ACTIONS

    # A hover dispatches pointer events a page may act on; resizing and reading the console only read it.
    assert {"hover", "drag"} <= BROWSER_INTERACTIVE_ACTIONS
    assert {"viewport", "console", "screenshot"}.isdisjoint(BROWSER_INTERACTIVE_ACTIONS)
