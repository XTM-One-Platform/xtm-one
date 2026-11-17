"""A recording of a scrolling page plays its scroll at its frame rate, in a real Chromium.

A 4000 px page under a fixed header, scrolled 2000 px by a ``scroll`` step in a 1600x900 window at pixel ratio 2 -
3200x1800 frames, which the screencast hands over a few a second. The recorder logs where the page stands while the
step scrolls, and the take is paced: its scroll plays at 30 frames a second, each frame the page further down than the
one before, none repeating the one before while it moves but those the pacing says it held.

Skipped where Playwright's Chromium is not installed; the ``Browser tool (Chromium)`` CI job installs it and sets
``REQUIRE_CHROMIUM=1``, which turns the skip into a failure.
"""

from __future__ import annotations

import os
import tempfile
import threading
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

import numpy as np
import pytest
from PIL import Image

from app.services import web_fetch
from app.services.browser_profiles import browser
from app.services.browser_profiles.browser import TargetPolicy
from app.services.video_studio import capture, scroll_pacing, spec
from tests import paced_scroll

pytestmark = pytest.mark.chromium

HOST = "app.localhost"
ROWS = "".join(
    f'<div class="row" style="background: hsl({(i * 47) % 360} 45% {30 + (i * 7) % 30}%)">Row {i} - '
    f'{"alpha beta gamma delta " * (1 + i % 4)}</div>'
    for i in range(100)
)
PAGE = f"""<!doctype html><html><head><style>
body {{ margin: 0; font: 22px sans-serif; color: #f4f6fa; background: #0b0f1a; }}
header {{ position: fixed; top: 0; left: 0; right: 0; height: 60px; background: #1a2238; z-index: 2; }}
main {{ padding-top: 60px; }}
.row {{ height: 39px; border-bottom: 1px solid #000; padding-left: 24px; line-height: 39px; }}
</style></head><body><header>Fixed header</header><main>{ROWS}</main></body></html>"""


#: A page whose paint takes 300 ms an offset: a scroll listener taking the main thread that long on each one
#: under a wheel listener that is not passive, or with a background fixed to the viewport too - every offset repainted
#: on the main thread, the screencast handing over a frame saying the next offset over the paint before.
_BUSY = "addEventListener('scroll', () => { const end = performance.now() + 300; while (performance.now() < end); });"
SLOW = {
    "listener": f"<script>addEventListener('wheel', () => {{}}, {{passive: false}}); {_BUSY}</script>",
    "repaint": f"<style>body {{ background: linear-gradient(#123, #456) fixed; }}</style><script>{_BUSY}</script>",
}


class _Handler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        slow = SLOW.get(urlparse(self.path).path.removeprefix("/slow/"))
        body = (PAGE.replace("</body>", f"{slow}</body>") if slow else PAGE).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *_args) -> None:
        return


@pytest.fixture
def site(monkeypatch) -> Iterator[str]:
    server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()

    async def validate(url: str, allow_private: bool = False) -> str:
        if urlparse(url).hostname == HOST:
            return url
        return await web_fetch.validate_outbound_url_async(url, allow_private=allow_private)

    monkeypatch.setattr(browser, "validate_outbound_url_async", validate)
    monkeypatch.setattr(browser, "private_targets_enabled", lambda: False)
    try:
        yield f"http://{HOST}:{server.server_address[1]}/"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(5)


async def _record(
    start_url: str, workdir: str, *, scroll_seconds: float | None = None, by: int = 2000, pixel_ratio: float = 2
) -> capture.CaptureResult:
    scroll = {"action": "scroll", "by": by, **({"seconds": scroll_seconds} if scroll_seconds is not None else {})}
    script = spec.parse_take_script(
        spec.validate_take_script(
            {
                "viewport": {"width": 1600, "height": 900},
                "pixel_ratio": pixel_ratio,
                "settle_ms": 0,
                "end_hold_ms": 400,
                "steps": [{"action": "wait", "seconds": 0.3}, scroll],
            }
        )
    )
    try:
        return await capture.record_take(script, workdir=workdir, profile=None, start_url=start_url, policy=TargetPolicy(), max_seconds=60)
    except Exception as exc:
        if "Executable doesn't exist" in str(exc) or "playwright install" in str(exc).lower():
            if os.environ.get("REQUIRE_CHROMIUM") == "1":
                pytest.fail(f"Playwright's Chromium is required here and is not installed: {exc}")
            pytest.skip("Playwright's Chromium is not installed (python -m playwright install chromium)")
        raise


def _grey(workdir: str, name: str) -> np.ndarray:
    with Image.open(Path(workdir) / name) as opened:
        return np.asarray(opened.convert("L").resize((800, 450)), dtype=np.int16)


def _same(a: np.ndarray, b: np.ndarray) -> bool:
    """Two frames showing the page at the same place, the fixed header left out."""
    return float(np.abs(a[40:] - b[40:]).mean()) <= 0.5


#: CSS pixels the page moves between two frames for the second to be a new picture: one pixel of the comparison, at
#: 800x450, of a 1600-pixel-wide window.
_VISIBLE_MOVE = 2.0


def _repeated_while_moving(workdir: str, frames: list[tuple[float, str]], scroll: dict) -> list[float]:
    """The instants of a take's *frames* at which a frame repeats the one before though the page moved between them
    as the take draws it (``paced_scroll.moved``): never where it rests, however far the log's clock runs from the
    paint's. Two frames both showing the page where it rests - the take's first picture, before the scroll, or its
    last, after it - are the page at rest: the take draws the keyframe painted at rest from the instant it
    exists, which on a loaded runner is a frame before the wheel's clock says the page arrives."""
    rests = [_grey(workdir, frames[0][1]), _grey(workdir, frames[-1][1])] if frames else []
    previous = None
    repeated: list[float] = []
    for t, name in frames:
        shown = _grey(workdir, name)
        at_rest = previous is not None and any(_same(shown, rest) and _same(previous[1], rest) for rest in rests)
        if (
            previous is not None
            and not at_rest
            and _same(shown, previous[1])
            and paced_scroll.moved(scroll, previous[0], t) >= _VISIBLE_MOVE
        ):
            repeated.append(round(t, 3))
        previous = (t, shown)
    return repeated


async def test_a_recorded_scroll_plays_at_30_frames_a_second(site):
    with tempfile.TemporaryDirectory() as workdir:
        result = await _record(site, workdir)
        assert result.frame_size == (3200, 1800)
        scroll_event = next(event for event in result.events if event["action"] == "scroll")
        [logged] = scroll_event["scrolls"]
        samples = logged["samples"]
        # Logged from before it moves to after it settles: 2000 px down, the window its scroller. Read at up to 60 Hz - a
        # loaded machine reads it less often, and the pacing interpolates between the samples whatever their rate.
        assert samples[0][1] == 0 and samples[-1][1] >= 1990
        duration = samples[-1][0] - samples[0][0]
        assert len(samples) >= 15 * duration
        paced = scroll_pacing.pace(result.frames, result.events, workdir=workdir, fps=30, pixel_ratio=result.pixel_ratio)
        [scroll] = scroll_event["scrolls"]
        assert scroll["scroller"] == "window" and scroll["still"]["top"] >= 100, "the fixed header stands still"
        moving = [t for t, top in scroll["log"] if 0 < top < scroll["log"][-1][1]]
        start, end = min(moving), max(moving)
        during = [(t, name) for t, name in paced.frames if start <= t <= end]
        if scroll["paced"]:
            assert scroll["fps"] == 30.0 and scroll["painted"] < 27
        # Whether the browser painted it fast enough or the take was paced, its scroll plays at 30 frames a second ...
        assert len(during) >= 0.9 * 30 * (end - start) - 1
        # ... and no frame repeats the one before while it moves, but one the pacing says it held: scrolled past what
        # any keyframe painted, which a screencast handing over a few frames a second leaves on a loaded machine.
        repeated = _repeated_while_moving(workdir, paced.frames, scroll)
        assert len(repeated) <= int(scroll.get("held") or 0), f"repeated at {repeated}: {scroll}"
        rate = scroll_pacing.take_rate(result.events, captured_frames=len(result.frames), duration=result.duration)
        assert rate.scrolls and rate.fps >= 27 and not rate.steps


def _painting_at(rate: float):
    """A screencast handing over one frame every 1/*rate* s, every frame acknowledged (the worker's 2.1 fps)."""
    original = capture._Screencast._on_frame

    def on_frame(self, event: dict) -> None:
        stamp = float((event.get("metadata") or {}).get("timestamp") or 0.0)
        kept = getattr(self, "_kept_at", None)
        if kept is not None and stamp - kept < 1.0 / rate:
            self._loop.create_task(self._ack(event.get("sessionId")))
            return
        self._kept_at = stamp
        original(self, event)

    return on_frame


async def test_a_scroll_given_its_seconds_lasts_them_and_is_paced_on_what_its_keyframes_painted(site, monkeypatch):
    """The wheel turns on a clock over the step's seconds, and a window's scroll painted at 2 fps is paced on the
    offsets the screencast says its keyframes painted."""
    monkeypatch.setattr(capture._Screencast, "_on_frame", _painting_at(2.1))
    with tempfile.TemporaryDirectory() as workdir:
        result = await _record(site, workdir, scroll_seconds=2.0)
        assert result.painted and set(result.painted) <= {name for _t, name in result.frames}
        scroll_event = next(event for event in result.events if event["action"] == "scroll")
        [logged] = scroll_event["scrolls"]
        moving = [sample[0] for sample in logged["samples"] if 0 < sample[1] < 2000]
        paced = scroll_pacing.pace(
            result.frames, result.events, workdir=workdir, fps=30, pixel_ratio=result.pixel_ratio, painted=result.painted
        )
        [scroll] = scroll_event["scrolls"]
        assert scroll["paced"] and scroll["offsets"] == "painted" and scroll["painted"] < 4, scroll
        # The wheel turned on its clock over the step's 2 seconds: read on its own log, never the page's, which the
        # browser's smooth scroll of the last turn and a busy main thread carry past it.
        (began, nothing), *_turned, (ended, all_of_it) = scroll["wheel"]
        assert (nothing, all_of_it) == (0.0, 1.0) and ended - began == pytest.approx(2.0, abs=0.2), "the wheel turned over the step's 2 seconds"
        start, end = min(moving), max(moving)
        during = [(t, name) for t, name in paced.frames if start <= t <= end]
        assert len(during) >= 0.9 * 30 * (end - start) - 1
        repeated = _repeated_while_moving(workdir, paced.frames, scroll)
        assert len(repeated) <= int(scroll.get("held") or 0), f"repeated at {repeated}: {scroll}"


@pytest.mark.parametrize("kind", sorted(SLOW))
async def test_a_page_painting_300_ms_an_offset_scrolls_at_30_fps_and_the_save_says_nothing_steps(site, kind):
    """600 px over 2.5 s at 1600x900 on a page whose paint takes 300 ms an offset - 3 frames a second."""
    from app.services.video_studio import quality, timeline
    from tests.test_video_studio_scroll_pacing_6408 import TAKE, _spec

    with tempfile.TemporaryDirectory() as workdir:
        result = await _record(f"{site}slow/{kind}", workdir, scroll_seconds=2.5, by=600, pixel_ratio=1)
        assert result.frame_size == (1600, 900)
        scroll_event = next(event for event in result.events if event["action"] == "scroll")
        paced = scroll_pacing.pace(
            result.frames, result.events, workdir=workdir, fps=30, pixel_ratio=result.pixel_ratio, painted=result.painted
        )
        [scroll] = scroll_event["scrolls"]
        assert scroll["paced"] and scroll["fps"] == 30.0 and scroll["painted"] < 12, scroll
        assert "held" not in scroll and "outran" not in scroll, scroll
        (began, nothing), *_turned, (ended, all_of_it) = scroll["wheel"]
        assert (nothing, all_of_it) == (0.0, 1.0) and ended - began == pytest.approx(2.5, abs=0.2), "the wheel turned 600 px over 2.5 s"
        start, end = scroll["t"]
        shown = []
        for t, name in paced.frames:
            if start <= t <= end:
                with Image.open(Path(workdir) / name) as opened:
                    shown.append((round(t, 3), np.asarray(opened.convert("L").resize((800, 450)), dtype=np.int16)))
        # The scroll's frames, from the first that leaves where the page rested to the last before where it came to rest.
        moved = [k for k in range(1, len(shown)) if np.abs(shown[k][1][40:] - shown[0][1][40:]).mean() > 0.5]
        rested = [k for k in range(len(shown) - 1) if np.abs(shown[k][1][40:] - shown[-1][1][40:]).mean() > 0.5]
        first, last = moved[0], rested[-1] + 1
        assert shown[last][0] - shown[first][0] == pytest.approx(2.5, abs=0.4), "the wheel's 2.5 s, at its pace"
        repeated = [shown[k][0] for k in range(first + 1, last + 1) if np.abs(shown[k][1][40:] - shown[k - 1][1][40:]).mean() <= 0.5]
        assert repeated == [], f"repeated at {repeated}: {scroll}"
        rate = scroll_pacing.take_rate(result.events, captured_frames=len(result.frames), duration=result.duration)
        assert rate.fps == 30.0 and not rate.steps and rate.seconds is None
        assert rate.words() == f"scrolls at 30 fps ({rate.synthesised:.0f} frames a second synthesised between keyframes)"
        spec_, document, authored = _spec(True)
        take = timeline.TakeMedia(TAKE, result.duration, 1600, 900, 1.0, 60, round(result.duration * 60), events=result.events, captured_frames=len(result.frames))
        found = quality.content_findings(spec_, document, quality.ContentFacts(takes={TAKE: take}), authored)
        assert not [f for f in found if f.kind == "take_frame_rate"]
        assert not [f for f in found if "scrolls will step" in f.message]
