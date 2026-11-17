"""A WebGL canvas transition recorded in a real Chromium, measured on its own pixels.

The fixture is one WebGL ``<canvas>`` drawing 4000 points; a button lays them out again over exactly 60 animation
frames, as a graph's layout move does. What is proven:

* each step logs the canvases it ended on, and the transition is kept at its step with the rate it was captured at;
* where Chromium draws WebGL on a GPU (``capture_rate.gl_backend``), that rate is above 24 fps - skipped on a worker
  without one, where the take says it was drawn on software GL;
* on the software-GL path, a transition captured under 20 fps (the screencast held to 8 frames a second) is flagged:
  a stretch under the floor naming the GL, and the take's words naming a GPU worker.

Skipped where Playwright's Chromium is not installed; ``REQUIRE_CHROMIUM=1`` turns that skip into a failure.
"""

from __future__ import annotations

import os
import tempfile
import threading
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse

import pytest

from app.services import web_fetch
from app.services.browser_profiles import browser
from app.services.browser_profiles.browser import TargetPolicy
from app.services.video_studio import capture, capture_rate, spec

pytestmark = pytest.mark.chromium

HOST = "app.localhost"
#: The canvas stands at (100, 80), 1200x700 CSS px, in a 1600x900 window.
PAGE = """<!doctype html><html><head><style>
body { margin: 0; background: #0b0f1a; }
#graph { position: absolute; left: 100px; top: 80px; width: 1200px; height: 700px; display: block; }
#relayout { position: absolute; left: 1360px; top: 100px; width: 180px; height: 44px; }
</style></head><body>
<canvas id="graph" width="1200" height="700"></canvas>
<button id="relayout">Layered layout</button>
<script>
const canvas = document.getElementById('graph');
const gl = canvas.getContext('webgl');
const N = 4000;
const from = new Float32Array(N * 2), to = new Float32Array(N * 2), now = new Float32Array(N * 2);
for (let i = 0; i < N; i++) {
  from[2 * i] = Math.cos(i * 2.399) * (0.1 + 0.85 * (i / N)); from[2 * i + 1] = Math.sin(i * 2.399) * (0.1 + 0.85 * (i / N));
  to[2 * i] = ((i % 80) / 40 - 1) * 0.95; to[2 * i + 1] = (Math.floor(i / 80) / 25 - 1) * 0.95;
}
const shader = (type, source) => { const s = gl.createShader(type); gl.shaderSource(s, source); gl.compileShader(s); return s; };
const program = gl.createProgram();
gl.attachShader(program, shader(gl.VERTEX_SHADER, 'attribute vec2 p; void main() { gl_Position = vec4(p, 0.0, 1.0); gl_PointSize = 9.0; }'));
gl.attachShader(program, shader(gl.FRAGMENT_SHADER, 'precision mediump float; void main() { gl_FragColor = vec4(0.36, 0.42, 1.0, 1.0); }'));
gl.linkProgram(program); gl.useProgram(program);
const buffer = gl.createBuffer(); gl.bindBuffer(gl.ARRAY_BUFFER, buffer);
const at = gl.getAttribLocation(program, 'p'); gl.enableVertexAttribArray(at); gl.vertexAttribPointer(at, 2, gl.FLOAT, false, 0, 0);
function draw(k) {
  for (let i = 0; i < N * 2; i++) now[i] = from[i] + (to[i] - from[i]) * k;
  gl.clearColor(0.04, 0.06, 0.1, 1); gl.clear(gl.COLOR_BUFFER_BIT);
  gl.bufferData(gl.ARRAY_BUFFER, now, gl.DYNAMIC_DRAW); gl.drawArrays(gl.POINTS, 0, N);
}
document.getElementById('relayout').addEventListener('click', () => {
  let frame = 0;
  (function step() { frame += 1; draw(frame / 60); if (frame < 60) requestAnimationFrame(step); })();
});
draw(0);
</script></body></html>"""


class _Handler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        body = PAGE.encode("utf-8")
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
    monkeypatch.setattr(capture_rate, "_BACKEND", [])
    try:
        yield f"http://{HOST}:{server.server_address[1]}/"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(5)


STEPS = [{"action": "wait", "seconds": 0.3}, {"action": "click", "selector": "#relayout"}, {"action": "wait", "seconds": 1.6}]


async def _recorded(start_url: str, workdir: str) -> tuple[capture.CaptureResult, dict]:
    """The take of :data:`STEPS` and its capture as the runner measures it, canvases included."""
    script = spec.parse_take_script(
        spec.validate_take_script(
            {"viewport": {"width": 1600, "height": 900}, "pixel_ratio": 1, "settle_ms": 0, "end_hold_ms": 300, "steps": STEPS}
        )
    )
    try:
        result = await capture.record_take(script, workdir=workdir, profile=None, start_url=start_url, policy=TargetPolicy(), max_seconds=60)
    except Exception as exc:
        if "Executable doesn't exist" in str(exc) or "playwright install" in str(exc).lower():
            if os.environ.get("REQUIRE_CHROMIUM") == "1":
                pytest.fail(f"Playwright's Chromium is required here and is not installed: {exc}")
            pytest.skip("Playwright's Chromium is not installed (python -m playwright install chromium)")
        raise
    captured = capture_rate.measure(
        result.frames,
        result.events,
        duration=result.duration,
        fps=30,
        workdir=workdir,
        recorded=result.capture,
        viewport_width=result.viewport[0],
        cursor=result.cursor,
        painted=result.frames,
    )
    return result, captured


def _runs(events: list[dict]) -> list[dict]:
    return [run for event in events for run in event.get("canvas_motion") or []]


async def test_a_webgl_transition_on_a_gpu_is_captured_above_24_fps(site):
    with tempfile.TemporaryDirectory() as workdir:
        result, _captured = await _recorded(site, workdir)
    gl = result.capture["gl"]
    assert result.events[1]["canvases"][0][0] == "canvas#graph"
    if not gl["gpu"]:
        assert gl.get("why"), "a take drawn on software GL says why"
        pytest.skip(f"no GPU for Chromium to draw WebGL on here ({gl.get('why')})")
    [run] = [run for run in _runs(result.events) if run["canvas"] == "canvas#graph"]
    assert run["fps"] > 24, run
    assert run["frames"] >= 24, run


async def test_a_webgl_transition_under_20_fps_on_software_gl_is_flagged(site, monkeypatch):
    """SwiftShader, the screencast held to 8 frames a second: the transition is said low, naming the GL."""
    monkeypatch.setattr(capture_rate.settings, "video_studio_recording_gl", "software")
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
        result, captured = await _recorded(site, workdir)
    assert result.capture["gl"]["gpu"] is False and result.capture["gl"]["webgl"] != capture_rate.ON_GPU_STATUS
    [run] = [run for run in _runs(result.events) if run["canvas"] == "canvas#graph"]
    assert run["fps"] < capture_rate.LOW_FPS, run
    assert captured["low"] and all(low["gl"] == "software" for low in captured["low"])
    assert any("software GL" in said for said in capture_rate.low_words(captured))
    assert "a worker whose Chromium draws on a GPU" in capture_rate.software_words(captured)
