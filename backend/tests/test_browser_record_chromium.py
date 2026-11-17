"""The ``record`` action records an animating page in a real Chromium without slowing it down.

A page animates a box with ``requestAnimationFrame`` and counts its frames. Recording it for two seconds answers an
MP4 (ffprobe reads its video stream at :data:`browser_tool.RECORD_FPS`) and the page's own frame rate while it is
recorded stays within 10 % of its rate without recording: the screencast is painted by the browser process, never
on the page's main thread. The viewport is the production retest's (1728x1000): at 640x360 a recorder that read
back every frame at full size passed here and cost a real page 25 to 29 % of its rate (#11131).

Skipped where Playwright's Chromium or ffmpeg is not installed; the ``Browser tool (Chromium)`` CI job installs
Chromium and sets ``REQUIRE_CHROMIUM=1``, which turns the Chromium skip into a failure.
"""

from __future__ import annotations

import os
import tempfile
from collections.abc import AsyncIterator
from unittest.mock import AsyncMock

import pytest

from app.services import browser_tool
from app.services.video_studio import encoder

pytestmark = pytest.mark.chromium

VIEWPORT = {"width": 1728, "height": 1000}

PAGE = """<!doctype html><html><body style="margin: 0; background: #F2F2F3">
<div id="box" style="position: absolute; width: 80px; height: 80px; background: #0015A8"></div>
<script>
window.__frames = 0;
const box = document.getElementById('box');
function tick(t) {
  window.__frames += 1;
  box.style.transform = `translateX(${(t / 4) % 1600}px)`;
  requestAnimationFrame(tick);
}
requestAnimationFrame(tick);
</script></body></html>"""

FPS_PROBE = """async (ms) => {
  const start = window.__frames;
  const t0 = performance.now();
  await new Promise(r => setTimeout(r, ms));
  return (window.__frames - start) * 1000 / (performance.now() - t0);
}"""


@pytest.fixture
async def session(monkeypatch) -> AsyncIterator[browser_tool.BrowserSession]:
    if not encoder.encoder_available():
        pytest.skip("ffmpeg is not installed")
    try:
        from playwright.async_api import async_playwright
    except ImportError:  # pragma: no cover - Playwright is a dev dependency
        pytest.skip("Playwright is not installed")
    async with async_playwright() as playwright:
        try:
            browser = await playwright.chromium.launch()
        except Exception as exc:
            if "Executable doesn't exist" not in str(exc) and "playwright install" not in str(exc).lower():
                raise
            if os.environ.get("REQUIRE_CHROMIUM") == "1":
                pytest.fail(f"Playwright's Chromium is required here and is not installed: {exc}")
            pytest.skip("Playwright's Chromium is not installed (python -m playwright install chromium)")
        try:
            context = await browser.new_context(viewport=VIEWPORT)
            page = await context.new_page()
            await page.set_content(PAGE)
            held = browser_tool.BrowserSession(pw=None, browser=browser, context=context, page=page)
            monkeypatch.setattr(browser_tool, "_get_or_create_session", AsyncMock(return_value=held))
            yield held
        finally:
            await browser.close()


async def test_a_recording_is_an_mp4_and_leaves_the_page_rate_alone(session):
    import asyncio

    page = session.page
    alone = await page.evaluate(FPS_PROBE, 2000)
    recording = asyncio.ensure_future(browser_tool.browser_record("test:record", 2))
    await asyncio.sleep(0.2)
    while_recorded = await page.evaluate(FPS_PROBE, 1500)
    video = await recording

    assert video.mp4[4:8] == b"ftyp"
    assert video.frames > 10
    assert (video.width, video.height) == browser_tool.record_frame_size(VIEWPORT)
    with tempfile.NamedTemporaryFile(suffix=".mp4") as handle:
        handle.write(video.mp4)
        handle.flush()
        info = await encoder.probe(handle.name)
    assert info.has_video
    assert info.fps == pytest.approx(browser_tool.RECORD_FPS, rel=0.05)
    assert info.duration == pytest.approx(2.0, abs=0.3)
    assert while_recorded >= 0.9 * alone, f"{while_recorded:.1f} fps recorded for {alone:.1f} fps alone"
