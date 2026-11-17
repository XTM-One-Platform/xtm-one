"""The viewport capture of ``render_html`` retries only a frameless Chromium.

On a CI runner ``Page.captureScreenshot`` can answer "Unable to capture
screenshot" before the compositor produced a frame. ``_capture_viewport``
retries that exact error after two animation frames, a bounded number of
times; any other error is raised at the first attempt. The page is faked
here (the backend suite runs without Chromium).
"""

from __future__ import annotations

import pytest

from app.services.builtin_tools import html_render

NO_FRAME = "Page.screenshot: Protocol error (Page.captureScreenshot): Unable to capture screenshot"


class _FakePage:
    def __init__(self, outcomes: list[object]) -> None:
        self._outcomes = list(outcomes)
        self.screenshot_calls = 0
        self.evaluated: list[str] = []

    async def screenshot(self, **_kwargs: object) -> bytes:
        self.screenshot_calls += 1
        outcome = self._outcomes.pop(0)
        if isinstance(outcome, BaseException):
            raise outcome
        assert isinstance(outcome, bytes)
        return outcome

    async def evaluate(self, script: str) -> None:
        self.evaluated.append(script)


@pytest.mark.asyncio
async def test_a_frameless_capture_is_retried_after_two_animation_frames() -> None:
    page = _FakePage([RuntimeError(NO_FRAME), b"png"])

    assert await html_render._capture_viewport(page) == b"png"  # type: ignore[arg-type]
    assert page.screenshot_calls == 2
    assert page.evaluated == [html_render._TWO_FRAMES_JS]


@pytest.mark.asyncio
async def test_another_capture_error_is_raised_at_once() -> None:
    page = _FakePage([RuntimeError("Target page, context or browser has been closed"), b"png"])

    with pytest.raises(RuntimeError, match="has been closed"):
        await html_render._capture_viewport(page)  # type: ignore[arg-type]
    assert page.screenshot_calls == 1
    assert page.evaluated == []


@pytest.mark.asyncio
async def test_the_retries_are_bounded() -> None:
    page = _FakePage([RuntimeError(NO_FRAME)] * (html_render.SCREENSHOT_ATTEMPTS + 1))

    with pytest.raises(RuntimeError, match="Unable to capture screenshot"):
        await html_render._capture_viewport(page)  # type: ignore[arg-type]
    assert page.screenshot_calls == html_render.SCREENSHOT_ATTEMPTS
