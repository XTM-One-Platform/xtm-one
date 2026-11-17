"""The render layout's memory holds the browsers its pages draw in. No browser started: a fake Playwright.

Every page whose frames are begun draws in a Chromium browser of its own, where pages screenshot share one:

* ``compositor._on_browsers`` launches one browser per page with ``BEGIN_FRAME_ARGS`` in begin-frame mode, and one
  browser for all the pages in screenshot mode;
* the memory model counts a browser more for each begun page past the first (``render_preflight.BROWSER_BASE``,
  ``browsers_of``), so a ceiling that held N screenshot pages may hold fewer begun ones - never more than it holds.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import playwright.async_api
import pytest

from app.core.config import settings
from app.services.headless_render import RENDER_CHROMIUM_ARGS
from app.services.video_studio import compositor, render_preflight

UHD = (3840, 2160)


class _Playwright:
    """``async_playwright()`` launching fake browsers, each launch's arguments recorded."""

    def __init__(self) -> None:
        self.launched: list[list[str]] = []
        self.closed = 0
        playwright = self

        class _Browser:
            async def close(self):
                playwright.closed += 1

        async def launch(*, headless: bool, args: list[str]):
            assert headless
            await asyncio.sleep(0)
            playwright.launched.append(list(args))
            return _Browser()

        self.chromium = SimpleNamespace(launch=launch)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_exc):
        return False


@pytest.mark.parametrize(("capture", "browsers"), [("begin_frame", 3), ("screenshot", 1)])
def test_pages_begun_draw_in_a_browser_each_and_pages_screenshot_share_one(monkeypatch, capture, browsers):
    fake = _Playwright()
    monkeypatch.setattr(playwright.async_api, "async_playwright", lambda: fake)
    drawn: list[tuple[int, int]] = []

    async def chunk(browser, index):
        await asyncio.sleep(0)
        drawn.append((id(browser), index))

    asyncio.run(compositor._on_browsers(capture, 3, chunk))
    assert sorted(index for _browser, index in drawn) == [0, 1, 2]
    assert len(fake.launched) == browsers == len({browser for browser, _index in drawn}) == fake.closed
    for args in fake.launched:
        assert args[: len(RENDER_CHROMIUM_ARGS)] == list(RENDER_CHROMIUM_ARGS)
        assert (set(compositor.BEGIN_FRAME_ARGS) <= set(args)) is (capture == "begin_frame")


@pytest.fixture
def capture(monkeypatch):
    def told(mode: str) -> None:
        monkeypatch.setenv(compositor.FRAME_CAPTURE_ENV, mode)

    return told


def test_each_page_begun_past_the_first_holds_a_browser_more(capture):
    capture("screenshot")
    shared = render_preflight.memory_budget(UHD, pages=4, x264_preset="slow", cpus=8)
    assert render_preflight.browsers_of(4) == 1
    capture("begin_frame")
    begun = render_preflight.memory_budget(UHD, pages=4, x264_preset="slow", cpus=8)
    assert render_preflight.browsers_of(4) == 4
    assert begun - shared == 3 * render_preflight.BROWSER_BASE
    # One page draws in one browser either way.
    alone = render_preflight.memory_budget(UHD, pages=1, x264_preset="slow", cpus=8)
    capture("screenshot")
    assert render_preflight.memory_budget(UHD, pages=1, x264_preset="slow", cpus=8) == alone
    # The modelled sizes count it too (a draft at 720p, no page peak measured there).
    draft_shared = render_preflight.memory_needed((1280, 720), pages=3, x264_preset="veryfast")
    capture("begin_frame")
    assert render_preflight.memory_needed((1280, 720), pages=3, x264_preset="veryfast") - draft_shared == 2 * render_preflight.BROWSER_BASE


def test_a_ceiling_lays_out_no_more_pages_begun_than_their_browsers_leave_room_for(capture, monkeypatch):
    monkeypatch.setattr(settings, "video_studio_render_pages", 0)
    capture("screenshot")
    four = render_preflight.memory_budget(UHD, pages=4, x264_preset="slow", cpus=8)
    # A ceiling that just holds 4 screenshot pages holds 3 begun ones: their 3 browsers more do not fit beside them.
    shared = render_preflight.layout(UHD, x264_preset="slow", ceiling=four, cpus=8, pages=4)
    capture("begin_frame")
    begun = render_preflight.layout(UHD, x264_preset="slow", ceiling=four, cpus=8, pages=4)
    assert (shared.pages, begun.pages) == (4, 3)
    assert render_preflight.memory_budget(UHD, pages=begun.pages, x264_preset="slow", cpus=8) <= four


def test_a_web_process_counts_the_browsers_asked_for(monkeypatch):
    monkeypatch.delenv(compositor.FRAME_CAPTURE_ENV, raising=False)
    monkeypatch.setattr(compositor, "_CAPTURE", [])
    monkeypatch.setattr(settings, "video_studio_frame_capture", "begin_frame")
    assert render_preflight.browsers_of(5) == 5
    monkeypatch.setattr(settings, "video_studio_frame_capture", "screenshot")
    assert render_preflight.browsers_of(5) == 1
    # A worker whose probe settled on screenshots counts one, whatever is asked.
    monkeypatch.setattr(settings, "video_studio_frame_capture", "begin_frame")
    monkeypatch.setattr(compositor, "_CAPTURE", [compositor.FrameCapture("begin_frame", "screenshot", "no begun frame within 2 s")])
    assert render_preflight.browsers_of(5) == 1
