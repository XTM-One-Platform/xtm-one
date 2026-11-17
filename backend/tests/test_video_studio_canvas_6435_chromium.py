"""A recording acts on a canvas and captures its animation, in a real Chromium.

The fixture is a graph drawn on one ``<canvas>``: five circles, no element for
any of them and no hook. Their labels are not painted - a graph zoomed
out that far hides them -, only mirrored in a visually hidden ``role=listbox``
whose active entry the canvas paints in another colour while it has the focus:
each node is found by that highlight (``node_locate``). Its context menu opens
only on a node the pointer hovered (as the OpenCTI graph's does), the wheel
zooms it, and a button lays 300 nodes out again over 1.5 s. Every pointer
event the canvas receives is posted back to the test server, which the
assertions read.

Skipped where Playwright's Chromium is not installed; the ``Browser tool (Chromium)`` CI job installs it and sets
``REQUIRE_CHROMIUM=1``, which turns the skip into a failure.
"""

from __future__ import annotations

import json
import os
import re
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
from app.services.video_studio import capture, capture_rate, spec, timeline

pytestmark = pytest.mark.chromium

HOST = "app.localhost"
PAGE = """<!doctype html><html><head><style>
body { margin: 0; background: #0b0f1a; font: 16px sans-serif; color: #f4f6fa; }
#graph { position: absolute; left: 100px; top: 80px; width: 1200px; height: 700px; display: block; }
#menu { position: absolute; display: none; background: #1a2238; padding: 6px 0; min-width: 240px; }
#menu div { padding: 6px 14px; }
#relayout { position: absolute; left: 1360px; top: 100px; width: 180px; height: 44px; }
.sr-only { position: absolute; width: 1px; height: 1px; padding: 0; margin: -1px; overflow: hidden;
  clip: rect(0, 0, 0, 0); white-space: nowrap; border: 0; }
</style></head><body>
<canvas id="graph" width="1200" height="700"></canvas>
<button id="relayout">Layered layout</button>
<div id="menu" role="menu"><div role="menuitem">Lay out the graph around it</div><div role="menuitem">Hide</div></div>
<div id="nodes" class="sr-only" role="listbox" tabindex="0" aria-label="Nodes of the graph"></div>
<script>
const canvas = document.getElementById('graph'), ctx = canvas.getContext('2d'), menu = document.getElementById('menu');
const NODES = [['A', 200, 150], ['B', 500, 300], ['C', 800, 200], ['D', 300, 550], ['E', 950, 500]]
  .map(([label, x, y]) => ({ label, x, y }));
const DOTS = Array.from({ length: 300 }, (_, i) => ({ fx: (i * 97) % 1150 + 25, fy: (i * 53) % 650 + 25,
  tx: (i % 20) * 58 + 30, ty: Math.floor(i / 20) * 44 + 30 }));
let scale = 1, hovered = null, morph = 0, active = -1, focused = false;
const log = [];
function draw() {
  ctx.setTransform(1, 0, 0, 1, 0, 0); ctx.fillStyle = '#0b0f1a'; ctx.fillRect(0, 0, 1200, 700);
  ctx.fillStyle = '#3a4a6a';
  for (const d of DOTS) ctx.fillRect(d.fx + (d.tx - d.fx) * morph, d.fy + (d.ty - d.fy) * morph, 8, 8);
  ctx.setTransform(scale, 0, 0, scale, 0, 0);
  for (const n of NODES) {
    ctx.beginPath(); ctx.arc(n.x, n.y, 24, 0, Math.PI * 2);
    ctx.fillStyle = focused && NODES[active] === n ? '#ffd166' : n === hovered ? '#00f0ff' : '#5b6cff'; ctx.fill();
  }
}
const at = (cx, cy) => { const r = canvas.getBoundingClientRect();
  return NODES.find((n) => Math.hypot(r.left + n.x * scale - cx, r.top + n.y * scale - cy) <= 24 * scale) || null; };
const list = document.getElementById('nodes');
NODES.forEach((n, i) => {
  const option = document.createElement('div');
  option.setAttribute('role', 'option'); option.id = 'node-' + i; option.textContent = n.label;
  list.appendChild(option);
});
list.addEventListener('focus', () => { focused = true; draw(); });
list.addEventListener('blur', () => { focused = false; draw(); });
list.addEventListener('keydown', (e) => {
  if (e.key !== 'ArrowDown' && e.key !== 'ArrowUp') return;
  e.preventDefault();
  active = e.key === 'ArrowDown' ? Math.min(active + 1, NODES.length - 1) : Math.max(active - 1, 0);
  list.setAttribute('aria-activedescendant', 'node-' + active);
  draw();
});
for (const type of ['pointermove', 'pointerdown', 'pointerup', 'click', 'contextmenu', 'wheel']) {
  canvas.addEventListener(type, (e) => {
    log.push({ type, x: e.clientX, y: e.clientY, buttons: e.buttons, ctrl: e.ctrlKey, button: e.button });
    if (type === 'pointermove') { hovered = at(e.clientX, e.clientY); draw(); }
    if (type === 'contextmenu') {
      e.preventDefault();
      if (hovered) { menu.style.left = e.clientX + 'px'; menu.style.top = e.clientY + 'px'; menu.style.display = 'block'; }
    }
    if (type === 'wheel') { e.preventDefault(); scale *= Math.exp(-e.deltaY / 600); draw(); }
  }, { passive: type !== 'contextmenu' && type !== 'wheel' });
}
document.addEventListener('keydown', (e) => { if (e.key === 'Escape') menu.style.display = 'none'; });
document.getElementById('relayout').addEventListener('click', () => {
  const t0 = performance.now();
  (function step(now) { morph = Math.min(1, (now - t0) / 1500); draw(); if (morph < 1) requestAnimationFrame(step); })(t0);
});
setInterval(() => { if (log.length) fetch('/log', { method: 'POST', body: JSON.stringify(log.splice(0)) }); }, 100);
draw();
</script></body></html>"""


class _Handler(BaseHTTPRequestHandler):
    logged: list[dict] = []

    def do_GET(self) -> None:
        body = PAGE.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self) -> None:
        length = int(self.headers.get("Content-Length") or 0)
        _Handler.logged.extend(json.loads(self.rfile.read(length) or b"[]"))
        self.send_response(204)
        self.end_headers()

    def log_message(self, *_args) -> None:
        return


@pytest.fixture
def site(monkeypatch) -> Iterator[str]:
    _Handler.logged = []
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


async def _record(start_url: str, workdir: str, steps: list[dict], *, pixel_ratio: float = 1) -> capture.CaptureResult:
    script = spec.parse_take_script(
        spec.validate_take_script(
            {"viewport": {"width": 1600, "height": 900}, "pixel_ratio": pixel_ratio, "settle_ms": 0, "end_hold_ms": 300, "steps": steps}
        )
    )
    return await _take(script, workdir, start_url, max_seconds=60)


async def _take(script: spec.TakeScript, workdir: str, start_url: str, **options) -> capture.CaptureResult:
    """``capture.record_take``, skipped where Playwright's Chromium is missing - a failure under ``REQUIRE_CHROMIUM=1``."""
    try:
        return await capture.record_take(script, workdir=workdir, profile=None, start_url=start_url, policy=TargetPolicy(), **options)
    except Exception as exc:
        if "Executable doesn't exist" in str(exc) or "playwright install" in str(exc).lower():
            if os.environ.get("REQUIRE_CHROMIUM") == "1":
                pytest.fail(f"Playwright's Chromium is required here and is not installed: {exc}")
            pytest.skip("Playwright's Chromium is not installed (python -m playwright install chromium)")
        raise


#: Where the fixture draws each node at scale 1, in CSS px of the window (the canvas stands at 100, 80).
NODE = {"A": (300, 230), "B": (600, 380), "C": (900, 280), "D": (400, 630), "E": (1050, 580)}


async def test_the_cursor_clicks_right_clicks_drags_and_wheels_the_canvas(site):
    with tempfile.TemporaryDirectory() as workdir:
        result = await _record(
            site,
            workdir,
            [
                {"action": "wait", "seconds": 0.2},
                {"action": "click", "node": {"label": "B"}},
                {"action": "right_click", "node": {"label": "C"}},
                {"action": "press", "key": "Escape"},
                {"action": "drag", "at": {"x": 0.2, "y": 0.2, "in": "#graph"}, "to": {"x": 0.6, "y": 0.6, "in": "#graph"}},
                {"action": "click", "node": {"label": "E"}, "modifiers": ["Control"]},
                {"action": "wheel", "node": {"label": "A"}, "by": -360, "seconds": 0.6},
                {"action": "wait", "seconds": 0.5},
            ],
        )
    events = {event["step"]: event for event in result.events}
    logged = _Handler.logged

    # The click lands on B's centre, after the pointer rested on B moving over it.
    bx, by = NODE["B"]
    clicks = [e for e in logged if e["type"] == "click"]
    assert abs(clicks[0]["x"] - bx) <= 2 and abs(clicks[0]["y"] - by) <= 2
    before = logged[: logged.index(clicks[0])]
    assert sum(1 for e in before[-12:] if e["type"] == "pointermove" and abs(e["x"] - bx) <= 2 and abs(e["y"] - by) <= 2) >= 3
    click = events[1]
    assert click["target"] == {"node": "B"} and click["point"] == {"x": bx, "y": by}
    assert click["box"] == {"x": bx - 24, "y": by - 24, "w": 48.0, "h": 48.0}

    # The right-click opens the menu a hovered node offers, and the log lists its entries.
    assert events[2]["menu"]["items"] == ["Lay out the graph around it", "Hide"]
    assert any(e["type"] == "contextmenu" and e["button"] == 2 for e in logged)

    # The drag moves in 24 pointer moves at least, the button held from (340, 220) to (820, 500).
    drag = events[4]
    held = [e for e in logged if e["type"] == "pointermove" and e["buttons"] == 1]
    assert drag["moves"] >= 24 and len(held) >= 20
    assert drag["point"] == {"x": 340.0, "y": 220.0} and drag["to_point"] == {"x": 820.0, "y": 500.0}

    # Control held through the click on E; the wheel zoomed the canvas in.
    assert any(e["type"] == "click" and e["ctrl"] and abs(e["x"] - NODE["E"][0]) <= 2 for e in logged)
    assert events[5]["modifiers"] == ["Control"]
    assert sum(1 for e in logged if e["type"] == "wheel") >= 5 and events[6]["target"] == {"node": "A"}

    # A recording scene on this take zooms toward B at the click.
    media = timeline.TakeMedia(
        "t", result.duration, *result.viewport, result.pixel_ratio, 60, 60, events=result.events, cursor=[tuple(c) for c in result.cursor]
    )
    time_map = timeline.build_time_map(
        duration=result.duration, trim_start=0.0, trim_end=None, speed=1.0, idle_mode="off", idle_threshold=1.2, idle_speed=6.0,
        events=result.events,
    )
    camera = timeline.plan_camera(
        mode="auto", max_zoom=1.8, zoom_on=["click"], keyframes=[], media=media, time_map=time_map, trim_start=0.0,
        out_duration=time_map[-1][0],
    )
    first = next(frame for frame in camera if frame[3] > 1.2)
    assert abs(first[1] - bx / 1600) < 0.1 and abs(first[2] - by / 900) < 0.15 and first[0] <= click["ta"] + 0.5


async def test_a_node_not_on_the_graph_fails_naming_the_closest_labels(site):
    with tempfile.TemporaryDirectory() as workdir, pytest.raises(capture.TakeError) as failed:
        await _record(site, workdir, [{"action": "click", "node": {"label": "Z"}, "timeout": 1}])
    assert "no node labelled 'Z' on the graph" in str(failed.value)
    assert 'Closest on the page then: "A" (node), "B" (node), "C" (node)' in str(failed.value)


def _changed(workdir: str, frames: list[tuple[float, str]]) -> list[bool]:
    previous, out = None, []
    for _t, name in frames:
        with Image.open(Path(workdir) / name) as opened:
            grey = np.asarray(opened.convert("L").resize((400, 225)), dtype=np.int16)
        out.append(previous is not None and float(np.abs(grey - previous).mean()) > 0.05)
        previous = grey
    return out


async def test_a_relayout_is_captured_frame_after_frame_and_its_rate_logged(site):
    """The 1.5 s relayout of 300 nodes captured at 1600x900: every frame of it a new picture, the step's rate logged as
    its frames say, said low exactly when it fell under 20 fps - and on a machine that keeps up, at the take's 30 fps.
    The rate a loaded machine reaches is its own: what is judged is that the take says it."""
    with tempfile.TemporaryDirectory() as workdir:
        result = await _record(
            site, workdir, [{"action": "wait", "seconds": 0.3}, {"action": "click", "selector": "#relayout"}, {"action": "wait", "seconds": 1.8}]
        )
        captured = capture_rate.measure(result.frames, result.events, duration=result.duration, fps=30, workdir=workdir)
        relayout = result.events[1]
        during = [(t, name) for t, name in result.frames if relayout["ta"] + 0.1 <= t <= relayout["ta"] + 1.4]
        changed = _changed(workdir, during)
    rate = relayout["capture_fps"]
    assert during and sum(changed) >= len(during) - 2, "a frame of the relayout repeats the one before"
    assert len(during) >= 0.75 * rate * 1.3 - 2, (len(during), rate)
    # The relayout step (1) is said low under the floor, never well above it (a rate on the floor may average
    # either side of it). The trailing wait (step 2) is judged by its own frames: a runner stall there is logged
    # for that step and says nothing about the relayout's rate.
    relayout_low = [low for low in captured["low"] if low["step"] == 1]
    if rate < capture_rate.LOW_FPS - 2:
        assert relayout_low, (rate, captured["low"])
    elif rate > capture_rate.LOW_FPS + 2:
        assert relayout_low == [], (rate, captured["low"])
    if rate >= 28:
        assert len(during) >= 0.8 * 30 * 1.3 and max(captured["rates"]) >= 24
    assert result.capture.get("backend"), "the GL backend that drew the canvas is logged"


@pytest.mark.parametrize(("rates", "lowered"), [({2.0: 14.0, 1.5: 40.0}, True), ({2.0: 4.7, 1.5: 8.0}, False)])
async def test_a_worker_short_of_the_takes_rate_records_at_1_5_or_refuses_before_step_1(site, monkeypatch, rates, lowered):
    """The warm-up measured at the take's size before step 1 (here a worker said to capture *rates*): a take it cannot
    capture at pixel ratio 2 is recorded at 1.5 when that sustains it, else refused - never recorded as asked - naming
    the window measured sustaining it, where the worker captures the 1.5 rate's frames by their pixels."""

    async def warm_up(_browser, viewport, ratio, **_kw):
        if viewport == (1600, 900) and round(ratio, 2) in rates:
            return rates[round(ratio, 2)]
        return rates[1.5] * 2400 * 1350 / (round(viewport[0] * ratio) * round(viewport[1] * ratio))

    monkeypatch.setattr(capture_rate, "warm_up", warm_up)
    monkeypatch.setattr(capture_rate, "_TABLE", {})
    script = spec.parse_take_script(
        spec.validate_take_script({"viewport": {"width": 1600, "height": 900}, "pixel_ratio": 2, "settle_ms": 0, "steps": [{"action": "wait", "seconds": 0.3}]})
    )
    with tempfile.TemporaryDirectory() as workdir:
        if not lowered:
            with pytest.raises(capture.TakeError) as refused:
                await _take(script, workdir, site, max_seconds=30, warm_up=True)
            assert str(refused.value).startswith("Refused before recording: this worker captures 3200x1800 frames at 5 fps")
            assert re.search(r"sustaining 30 fps on this worker \(.*\) is a \d+x\d+ window at pixel ratio 1 \(", str(refused.value))
            return
        result = await _take(script, workdir, site, max_seconds=30, warm_up=True)
    assert result.frame_size == (2400, 1350) and result.pixel_ratio == 1.5
    assert result.capture["pixel_ratio"] == {"asked": 2.0, "recorded": 1.5} and result.capture["preflight"]["fps"] == 14.0
    assert any(note.startswith("This worker captured 3200x1800 frames at 14 fps") for note in result.notes)


async def test_a_take_captured_too_slowly_says_low_capture_rate_at_its_step(site, monkeypatch):
    """A screencast handing over 8 frames a second while the canvas lays itself out: the take says it at that step."""
    original = capture._Screencast._on_frame

    def on_frame(self, event: dict) -> None:
        stamp = float((event.get("metadata") or {}).get("timestamp") or 0.0)
        kept = getattr(self, "_kept_at", None)
        if kept is not None and stamp - kept < 1.0 / 8:
            self._loop.create_task(self._ack(event.get("sessionId")))
            return
        self._kept_at = stamp
        original(self, event)

    monkeypatch.setattr(capture._Screencast, "_on_frame", on_frame)
    with tempfile.TemporaryDirectory() as workdir:
        result = await _record(
            site, workdir, [{"action": "wait", "seconds": 0.3}, {"action": "click", "selector": "#relayout"}, {"action": "wait", "seconds": 1.8}]
        )
        captured = capture_rate.measure(result.frames, result.events, duration=result.duration, fps=30, workdir=workdir)
    # The stretch is judged on the frames this take recorded, never on the wall clock of the runner: a loaded runner's
    # Chromium can hand nothing over for a second in the middle of the relayout, and ``capture_rate.runs`` then cuts
    # it where it stopped painting (a gap past ``RUN_GAP``) - the rule, not a fault. Every frame of the relayout is
    # slow (the screencast is held to 8 a second), so the stretch said is exactly the painted run it starts in, the
    # first such run reaching ``LOW_SECONDS`` that holds a frame of the relayout, named by the step it starts in.
    relayout = result.events[1]
    times = [t for t, _name in result.frames]
    painted = [(times[a], times[b - 1]) for a, b in capture_rate.runs(times)]
    expected = [
        (a, b)
        for a, b in painted
        if b - a >= capture_rate.LOW_SECONDS and b >= relayout["ta"] and a <= relayout["ta"] + 1.5 + 1.0
    ]
    if not expected:
        pytest.skip(f"this runner painted the relayout in no run of {capture_rate.LOW_SECONDS} s or more: {painted}")
    [low] = captured["low"]
    assert tuple(low["t"]) == (round(expected[0][0], 3), round(expected[0][1], 3)), (relayout["ta"], painted, low)
    starts_in = capture_rate.step_at(result.events, low["t"][0])
    assert starts_in is not None and low["step"] == starts_in["step"] and low["step"] in (1, 2), low
    assert low["fps"] < 10, low
    [said] = capture_rate.low_words(captured)
    assert re.fullmatch(rf"low capture rate at step {low['step']} \(\d fps for \d\.\d s\)", said), said
    assert result.events[low["step"]]["capture_fps"] < 10
