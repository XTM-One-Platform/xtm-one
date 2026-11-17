"""A recording's wait holds until its page settled, and a hold on a page still loading is said, in a real Chromium.

* A tab whose panel shows a progress bar (MUI's markup: ``role=progressbar``) for 5 s after it is clicked, then a
  ``wait`` of 2 s: the wait holds until the bar cleared and the page stood still, logs when it settled, and its frames
  are no loading hold.
* A tab drawing a graph on a canvas that moves for 3 s and then repaints the same picture on every frame: the wait
  after it is judged settled once its pixels stop changing - at the screencast's own rate and at 2.1 frames a second.
* A pause on a panel still loading - a progress bar, then a canvas painting nothing - is flagged in the take's answer.
* ``snapshot_browser_profile_page`` says how long a page showing a progress bar for 3 s took to settle once it opened.

Skipped where Playwright's Chromium is not installed; the ``Browser tool (Chromium)`` CI job installs it and sets
``REQUIRE_CHROMIUM=1``, which turns the skip into a failure.
"""

from __future__ import annotations

import os
import tempfile
import threading
import uuid
from collections.abc import Iterator
from contextlib import asynccontextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import SimpleNamespace
from urllib.parse import urlparse

import pytest

from app.models.video_studio import JOB_READY, VideoTake
from app.services import web_fetch
from app.services.browser_profiles import browser
from app.services.browser_profiles.browser import TargetPolicy
from app.services.browser_profiles.profiles import ProfileSnapshot
from app.services.builtin_tools import video_studio as tool
from app.services.video_studio import capture, page_settle, spec
from app.tools.base import ToolCall
from tests.test_video_studio_scroll_pacing_chromium import _painting_at

pytestmark = pytest.mark.chromium

HOST = "app.localhost"
#: How long the Knowledge tab's panel fetches, how long the graph moves, how long a refresh and the map take.
LOAD_MS, MOVE_MS, REFRESH_MS, MAP_MS = 5000, 3000, 2500, 2000
PROGRESS = (
    '<span class="MuiLinearProgress-root MuiLinearProgress-indeterminate" role="progressbar">'
    '<span class="MuiLinearProgress-bar"></span></span>'
)
STYLE = """body { margin: 0; font: 22px sans-serif; color: #f4f6fa; background: #0b0f1a; }
[role=tablist], .tools { display: flex; gap: 12px; padding: 20px; }
button { font: 22px sans-serif; padding: 10px 24px; }
.MuiLinearProgress-root { position: relative; display: block; height: 6px; overflow: hidden; background: #1d3b53; }
.MuiLinearProgress-bar { position: absolute; top: 0; bottom: 0; width: 40%; background: #00bcd4;
  animation: slide 1.2s linear infinite; }
@keyframes slide { from { left: -40%; } to { left: 100%; } }
#panel { padding: 20px; }
canvas { display: block; background: #101626; }"""
ENTITY = f"""<!doctype html><html><head><style>{STYLE}</style></head><body>
<div role="tablist"><button role="tab" aria-selected="true">Overview</button>
<button role="tab" id="knowledge">Knowledge</button><button role="tab" id="graph">Graph</button></div>
<div class="tools"><button id="refresh">Refresh</button><button id="map">Map</button></div>
<div id="panel"><h2>Overview</h2><p>An intrusion set</p></div>
<script>
const panel = document.getElementById('panel');
document.getElementById('knowledge').addEventListener('click', () => {{
  panel.innerHTML = '{PROGRESS}';
  setTimeout(() => {{ panel.innerHTML = '<h2>Knowledge</h2><p>12 relationships</p>'; }}, {LOAD_MS});
}});
document.getElementById('refresh').addEventListener('click', () => {{
  panel.innerHTML = '{PROGRESS}';
  setTimeout(() => {{ panel.innerHTML = '<h2>Refreshed</h2>'; }}, {REFRESH_MS});
}});
document.getElementById('map').addEventListener('click', () => {{
  panel.innerHTML = '<canvas id="world" width="900" height="500"></canvas>';
  setTimeout(() => {{
    const ctx = document.getElementById('world').getContext('2d');
    ctx.fillStyle = '#4caf50';
    for (let i = 0; i < 9; i++) ctx.fillRect(60 + i * 90, 120 + (i % 3) * 90, 60, 60);
  }}, {MAP_MS});
}});
// The graph moves for MOVE_MS, then is drawn again, the same, on every frame (a graph that never pauses its redraw).
document.getElementById('graph').addEventListener('click', () => {{
  panel.innerHTML = '<canvas id="network" width="900" height="500"></canvas>';
  const ctx = document.getElementById('network').getContext('2d');
  const start = performance.now();
  const draw = (now) => {{
    const k = Math.min(1, (now - start) / {MOVE_MS});
    ctx.fillStyle = '#101626';
    ctx.fillRect(0, 0, 900, 500);
    for (let i = 0; i < 12; i++) {{
      const a = (i / 12) * Math.PI * 2 + (1 - k) * 6, r = 60 + 160 * k;
      ctx.fillStyle = `hsl(${{i * 30}} 70% 60%)`;
      ctx.beginPath();
      ctx.arc(450 + r * Math.cos(a), 250 + r * Math.sin(a), 18, 0, Math.PI * 2);
      ctx.fill();
    }}
    requestAnimationFrame(draw);
  }};
  requestAnimationFrame(draw);
}});
</script></body></html>"""
#: A page showing a progress bar for 3 s once it opened.
OPENING = f"""<!doctype html><html><head><style>{STYLE}</style></head><body><div id="panel">{PROGRESS}</div><script>
setTimeout(() => {{ document.getElementById('panel').innerHTML = '<h2>Loaded</h2>'; }}, 3000);
</script></body></html>"""
#: How long the spinner page's panel spins once loaded: a CSS spinner - no role, no aria, no DOM change.
SPIN_MS = 3000
SPINNER = f"""<!doctype html><html><head><style>{STYLE}
.spinner {{ width: 64px; height: 64px; border-radius: 50%; border: 8px solid #1d3b53; border-top-color: #00bcd4;
  animation: spin 0.8s linear infinite; }}
@keyframes spin {{ to {{ transform: rotate(360deg); }} }}</style></head><body>
<div class="tools"><button id="load">Load</button></div><div id="panel"><h2>Ready</h2></div><script>
document.getElementById('load').addEventListener('click', () => {{
  const panel = document.getElementById('panel');
  panel.innerHTML = '<div class="spinner"></div>';
  setTimeout(() => {{ panel.innerHTML = '<h2>Loaded</h2><p>42 indicators</p>'; }}, {SPIN_MS});
}});
</script></body></html>"""
PAGES = {"/entity": ENTITY, "/opening": OPENING, "/spinner": SPINNER}


class _Handler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        body = PAGES.get(urlparse(self.path).path, ENTITY).encode("utf-8")
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
        yield f"http://{HOST}:{server.server_address[1]}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(5)


def _skip_without_chromium(exc: Exception) -> None:
    if "Executable doesn't exist" in str(exc) or "playwright install" in str(exc).lower():
        if os.environ.get("REQUIRE_CHROMIUM") == "1":
            pytest.fail(f"Playwright's Chromium is required here and is not installed: {exc}")
        pytest.skip("Playwright's Chromium is not installed (python -m playwright install chromium)")


async def _record(start_url: str, workdir: str, steps: list[dict]) -> capture.CaptureResult:
    script = spec.parse_take_script(
        spec.validate_take_script(
            {"viewport": {"width": 1600, "height": 900}, "pixel_ratio": 1, "settle_ms": 0, "end_hold_ms": 300, "steps": steps}
        )
    )
    try:
        return await capture.record_take(script, workdir=workdir, profile=None, start_url=start_url, policy=TargetPolicy(), max_seconds=60)
    except Exception as exc:
        _skip_without_chromium(exc)
        raise


def _step(result: capture.CaptureResult, index: int) -> dict:
    return next(event for event in result.events if event["step"] == index)


async def test_a_wait_after_a_tab_click_holds_until_its_progress_bar_cleared(site):
    with tempfile.TemporaryDirectory() as workdir:
        result = await _record(
            f"{site}/entity", workdir, [{"action": "click", "role": "tab", "name": "Knowledge"}, {"action": "wait", "seconds": 2}]
        )
    click, wait = _step(result, 0), _step(result, 1)
    assert click.get("tab") is True and click["url"] == click["url_before"]
    # Held by default after a tab click: past its 2 s, until the bar cleared and the page stood still 0.5 s.
    assert wait["until_settled"] is True, wait
    settled = wait["settled"]
    cleared = click["ta"] + LOAD_MS / 1000
    assert cleared - 0.2 <= settled["at"] <= cleared + 0.6, (settled, click["ta"])
    assert settled["after"] > settled["seconds"] == 2
    # Its frames showed the bar while it loaded, named as the page names it, and it ended on the loaded panel. The
    # span ends on the first read that saw the bar gone: on a starved runner that read can come seconds after the
    # page's own clear time, so the wait is measured against that observed read, never against the wall clock.
    [span] = wait["loading"]
    assert any("MuiLinearProgress-root" in name and "[progressbar]" in name for name in span[2]), span
    observed = span[1]
    assert settled["at"] - 0.3 <= observed <= wait["t1"], (span, settled, wait["t1"])
    # Held at least until the page stood still, and released within a few reads of the first instant it could be judged
    # settled: the read that saw the bar gone, or the still time after the clear, whichever came later.
    ready = max(observed, settled["at"] + page_settle.STILL_SECONDS)
    assert settled["at"] + page_settle.STILL_SECONDS - 0.05 <= wait["t1"] <= ready + 4 * page_settle.SAMPLE_SECONDS, (wait, span)
    assert page_settle.loading_hold(wait) is None
    assert "until settled: settled at" in page_settle.settled_words(settled) and "past its 2 s" in page_settle.settled_words(settled)


async def test_a_wait_with_until_settled_off_ends_on_its_seconds_and_is_flagged(site):
    with tempfile.TemporaryDirectory() as workdir:
        result = await _record(
            f"{site}/entity",
            workdir,
            [{"action": "click", "role": "tab", "name": "Knowledge"}, {"action": "wait", "seconds": 2, "until_settled": False}],
        )
    wait = _step(result, 1)
    assert "until_settled" not in wait and 1.9 <= wait["t1"] - wait["t0"] <= 2.4, wait
    hold = page_settle.loading_hold(wait)
    assert hold is not None and hold.seconds >= 1.5 and not hold.gave_up, wait


@pytest.mark.parametrize("paint_fps", [None, 2.1])
async def test_a_canvas_repainting_is_settled_once_its_pixels_stop_changing(site, monkeypatch, paint_fps):
    if paint_fps is not None:
        monkeypatch.setattr(capture._Screencast, "_on_frame", _painting_at(paint_fps))
    with tempfile.TemporaryDirectory() as workdir:
        result = await _record(f"{site}/entity", workdir, [{"action": "click", "role": "tab", "name": "Graph"}, {"action": "wait", "seconds": 1}])
    click, wait = _step(result, 0), _step(result, 1)
    stopped = click["ta"] + MOVE_MS / 1000
    settled = wait["settled"]
    # Never settled while it moved; settled within a paint of it stopping, though it is still drawn on every frame. The
    # click lands its delay after ``ta``, and a frame is stamped once composited.
    late = 0.6 if paint_fps is None else 1.0 / paint_fps + 0.6
    assert settled["at"] is not None and stopped - 0.25 <= settled["at"] <= stopped + late, (settled, click["ta"], paint_fps)
    assert wait["t1"] >= settled["at"] + page_settle.STILL_SECONDS - 0.05
    assert wait["t1"] - wait["t0"] < 1 + page_settle.TIMEOUT_SECONDS
    assert page_settle.loading_hold(wait) is None, wait


async def test_a_pause_on_a_panel_still_loading_is_flagged_in_the_take_s_answer(site, monkeypatch):
    steps = [
        {"action": "click", "role": "button", "name": "Refresh"},
        {"action": "pause", "seconds": 3, "label": "The refreshed panel"},
        {"action": "click", "role": "button", "name": "Map"},
        {"action": "pause", "seconds": 3},
    ]
    with tempfile.TemporaryDirectory() as workdir:
        result = await _record(f"{site}/entity", workdir, steps)
    bar = page_settle.loading_hold(_step(result, 1))
    empty = page_settle.loading_hold(_step(result, 3))
    assert bar is not None and 1.5 <= bar.seconds <= REFRESH_MS / 1000 + 0.6, bar
    assert any("[progressbar]" in name for name in bar.shows), bar
    assert empty is not None and 1.0 <= empty.seconds <= MAP_MS / 1000 + 0.6, empty
    assert empty.shows == ("canvas#world (empty)",), empty
    # The answer says both, before the take is placed in a video.
    user_id = uuid.uuid4()
    take = VideoTake(
        id=uuid.uuid4(),
        user_id=user_id,
        name="Refresh",
        status=JOB_READY,
        progress=100,
        duration_ms=round(result.duration * 1000),
        viewport_width=1600,
        viewport_height=900,
        pixel_ratio=1.0,
        fps=60,
        captured_frames=len(result.frames),
        events=result.events,
        script={"steps": steps, "pixel_ratio": 1},
    )

    class _Session:
        async def get(self, model, key):
            if model is VideoTake:
                return take if str(key) == str(take.id) else None
            return SimpleNamespace(id=user_id, role="member")

    @asynccontextmanager
    async def session():
        yield _Session()

    monkeypatch.setattr(tool, "async_session", session)
    _definitions, executor = tool.build_video_studio_tools(None, str(user_id))
    answer = await executor.execute_tool(
        ToolCall(id="c1", name="get_video_recording", arguments={"recording_id": str(take.id), "include_contact_sheet": False})
    )
    assert f"Holds on a loading page: step 1 (pause) holds on a loading page for {bar.seconds:.1f} s" in answer.content
    assert f"step 3 (pause) holds on a loading page for {empty.seconds:.1f} s" in answer.content
    assert "canvas#world (empty)" in answer.content and page_settle.REMEDY in answer.content


async def test_a_wait_holds_until_a_css_spinner_stops_moving(site):
    # The spinner changes no DOM and has no role: the wait once read the page settled half a second in.
    with tempfile.TemporaryDirectory() as workdir:
        result = await _record(
            f"{site}/spinner",
            workdir,
            [{"action": "click", "role": "button", "name": "Load"}, {"action": "wait", "seconds": 1, "until_settled": True}],
        )
    click, wait = _step(result, 0), _step(result, 1)
    settled = wait["settled"]
    gone = click["ta"] + SPIN_MS / 1000
    assert settled["at"] is not None and gone - 0.3 <= settled["at"] <= gone + 0.8, (settled, click["ta"])
    [span] = wait["loading"]
    assert any(name.startswith("div.spinner") and name.endswith(page_settle.LOOPING_MARK) for name in span[2]), span
    assert page_settle.loading_hold(wait) is None


async def test_a_page_snapshot_says_how_long_its_page_took_to_settle_once_it_opened(site):
    profile = ProfileSnapshot(id="profile", name="Demo", start_url=f"{site}/opening", login_url=None)
    try:
        snap = await capture.snapshot_page(profile, f"{site}/opening")
    except Exception as exc:
        _skip_without_chromium(exc)
        raise
    assert snap.settled_after is not None and 2.9 <= snap.settled_after <= 4.5, snap.settled_after
    assert snap.settle_words().startswith(f"It settled {snap.settled_after:.1f} s after it opened")
