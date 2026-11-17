"""A scroll step over a WebGL force graph plays at the take's 60 fps, in a real Chromium.

A force graph of 1,200 nodes drawn by WebGL across a 1600x900 window at pixel ratio 1.5 - 2400x1350 frames, which the
screencast hands over a few a second - under a toolbar the page draws over it. Its wheel zooms the graph about the
pointer and scrolls nothing, as a graph library's does. A ``scroll`` step recorded at fps 60: the recorder finds the
canvas under the cursor, turns the wheel on the take's clock, each turn painted before the next, and the zoom is drawn
again from its keyframes at 60 frames a second - the take reports 60 fps, its answer says the step's painted rate.

Skipped where Playwright's Chromium is not installed; the ``Browser tool (Chromium)`` CI job installs it and sets
``REQUIRE_CHROMIUM=1``, which turns the skip into a failure.
"""

from __future__ import annotations

import itertools
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

pytestmark = pytest.mark.chromium

HOST = "app.localhost"
BY = -600
#: The page: a force graph across the window under a toolbar - its layout laid out once, then frozen.
_GRAPH = """<!doctype html><html><head><style>
html, body { margin: 0; height: 100%; overflow: hidden; background: #0b0f1a; }
canvas { position: fixed; left: 0; top: 0; width: 100vw; height: 100vh; display: block; }
.bar { position: fixed; top: 16px; left: 16px; padding: 10px 18px; background: #1a2238; color: #e8ecf4;
       font: 18px sans-serif; border-radius: 6px; }
</style></head><body><canvas id="graph"></canvas><div class="bar">Knowledge graph - 1,200 entities</div><script>
const canvas = document.getElementById('graph');
const ratio = window.devicePixelRatio || 1;
canvas.width = Math.round(innerWidth * ratio);
canvas.height = Math.round(innerHeight * ratio);
let seed = 6430;
const random = () => (seed = (seed * 16807) % 2147483647) / 2147483647;
const N = 1200, nodes = [], links = [];
for (let i = 0; i < N; i++) nodes.push({ x: 100 + random() * 1400, y: 80 + random() * 740, vx: 0, vy: 0 });
for (let i = 1; i < N; i++) links.push([i, Math.floor(random() * i)]);
for (let i = 0; i < N / 2; i++) links.push([Math.floor(random() * N), Math.floor(random() * N)]);
for (let step = 0; step < 40; step++) {
  for (const [a, b] of links) {
    const p = nodes[a], q = nodes[b], dx = q.x - p.x, dy = q.y - p.y, d = Math.hypot(dx, dy) || 1, f = (d - 30) * 0.004;
    p.vx += dx / d * f; p.vy += dy / d * f; q.vx -= dx / d * f; q.vy -= dy / d * f;
  }
  for (const n of nodes) { n.x += n.vx; n.y += n.vy; n.vx *= 0.5; n.vy *= 0.5; }
}
const tints = nodes.map(() => [0.35 + 0.6 * random(), 0.35 + 0.6 * random(), 0.35 + 0.6 * random()]);
let k = 1, tx = 0, ty = 0, dirty = true;
"""
#: Drawn by WebGL: no transform a page can read, the zoom in a uniform.
_WEBGL = """const gl = canvas.getContext('webgl2', { antialias: true });
const program = gl.createProgram();
const shader = (type, source) => { const s = gl.createShader(type); gl.shaderSource(s, source); gl.compileShader(s); gl.attachShader(program, s); };
shader(gl.VERTEX_SHADER, `#version 300 es
in vec2 position; in vec3 color; uniform vec3 view; uniform vec2 size; uniform float radius; out vec3 tint;
void main() {
  vec2 p = position * view.x + view.yz;
  gl_Position = vec4(p.x / size.x * 2.0 - 1.0, 1.0 - p.y / size.y * 2.0, 0.0, 1.0);
  gl_PointSize = radius * view.x;
  tint = color;
}`);
shader(gl.FRAGMENT_SHADER, `#version 300 es
precision mediump float; in vec3 tint; uniform bool disc; out vec4 outColor;
void main() {
  if (disc && length(gl_PointCoord - 0.5) > 0.5) discard;
  outColor = vec4(tint, 1.0);
}`);
gl.linkProgram(program);
gl.useProgram(program);
const buffer = (data, name, width) => {
  const b = gl.createBuffer(); gl.bindBuffer(gl.ARRAY_BUFFER, b); gl.bufferData(gl.ARRAY_BUFFER, new Float32Array(data), gl.STATIC_DRAW);
  return () => { const at = gl.getAttribLocation(program, name); gl.bindBuffer(gl.ARRAY_BUFFER, b); gl.enableVertexAttribArray(at); gl.vertexAttribPointer(at, width, gl.FLOAT, false, 0, 0); };
};
const linePositions = [], lineColors = [], nodePositions = [], nodeColors = [];
for (const [a, b] of links) { linePositions.push(nodes[a].x, nodes[a].y, nodes[b].x, nodes[b].y); lineColors.push(0.3, 0.38, 0.5, 0.3, 0.38, 0.5); }
for (let i = 0; i < N; i++) { nodePositions.push(nodes[i].x, nodes[i].y); nodeColors.push(...tints[i]); }
const lines = [buffer(linePositions, 'position', 2), buffer(lineColors, 'color', 3)];
const points = [buffer(nodePositions, 'position', 2), buffer(nodeColors, 'color', 3)];
const zoomed = () => {};
const draw = () => {
  gl.viewport(0, 0, canvas.width, canvas.height);
  gl.clearColor(0.043, 0.059, 0.102, 1); gl.clear(gl.COLOR_BUFFER_BIT);
  gl.uniform3f(gl.getUniformLocation(program, 'view'), k, tx, ty);
  gl.uniform2f(gl.getUniformLocation(program, 'size'), innerWidth, innerHeight);
  gl.uniform1f(gl.getUniformLocation(program, 'radius'), 9 * ratio);
  gl.uniform1i(gl.getUniformLocation(program, 'disc'), 0);
  lines.forEach((bind) => bind()); gl.drawArrays(gl.LINES, 0, links.length * 2);
  gl.uniform1i(gl.getUniformLocation(program, 'disc'), 1);
  points.forEach((bind) => bind()); gl.drawArrays(gl.POINTS, 0, N);
};
"""
#: Drawn on a 2D canvas whose context keeps the zoom's transform between frames, as a 2D force graph's does.
_CANVAS_2D = """const ctx = canvas.getContext('2d');
const zoomed = () => ctx.setTransform(k * ratio, 0, 0, k * ratio, tx * ratio, ty * ratio);
zoomed();
const draw = () => {
  ctx.save(); ctx.setTransform(1, 0, 0, 1, 0, 0); ctx.fillStyle = '#0b0f1a'; ctx.fillRect(0, 0, canvas.width, canvas.height); ctx.restore();
  ctx.strokeStyle = 'rgb(77, 97, 128)'; ctx.lineWidth = 1 / k; ctx.beginPath();
  for (const [a, b] of links) { ctx.moveTo(nodes[a].x, nodes[a].y); ctx.lineTo(nodes[b].x, nodes[b].y); }
  ctx.stroke();
  nodes.forEach((n, i) => {
    const [r, g, b] = tints[i].map((v) => Math.round(v * 255));
    ctx.fillStyle = `rgb(${r}, ${g}, ${b})`; ctx.beginPath(); ctx.arc(n.x, n.y, 4.5, 0, 2 * Math.PI); ctx.fill();
  });
};
"""
#: The wheel zooms the graph about the pointer (a d3 zoom's rate) and scrolls nothing; a frame redraws what changed.
_WHEEL = """canvas.addEventListener('wheel', (e) => {
  e.preventDefault();
  const next = Math.min(8, Math.max(0.2, k * Math.pow(2, -e.deltaY * 0.002)));
  tx = e.clientX - (e.clientX - tx) * next / k; ty = e.clientY - (e.clientY - ty) * next / k; k = next; dirty = true;
  zoomed();
}, { passive: false });
const frame = () => { if (dirty) { draw(); dirty = false; } requestAnimationFrame(frame); };
requestAnimationFrame(frame);
</script></body></html>"""
PAGES = {"/": _GRAPH + _WEBGL + _WHEEL, "/2d": _GRAPH + _CANVAS_2D + _WHEEL}


class _Handler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        body = PAGES.get(urlparse(self.path).path, PAGES["/"]).encode("utf-8")
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


async def _record(start_url: str, workdir: str) -> tuple[capture.CaptureResult, spec.TakeScript]:
    script = spec.parse_take_script(
        spec.validate_take_script(
            {
                "viewport": {"width": 1600, "height": 900},
                "pixel_ratio": 1.5,
                "fps": 60,
                "settle_ms": 0,
                "end_hold_ms": 400,
                "steps": [{"action": "wait", "seconds": 0.4}, {"action": "scroll", "by": BY, "seconds": 1.5}],
            }
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
    return result, script


@pytest.mark.parametrize(("path", "context", "progress"), [("/", "webgl2", "wheel"), ("/2d", "2d", "transform")], ids=["webgl", "2d"])
async def test_a_scroll_step_over_a_force_graph_plays_at_the_take_s_60_fps(site, path, context, progress):
    with tempfile.TemporaryDirectory() as workdir:
        result, script = await _record(site.rstrip("/") + path, workdir)
        assert result.frame_size == (2400, 1350)
        scroll_event = next(event for event in result.events if event["action"] == "scroll")
        [logged] = scroll_event["scrolls"]
        surface = logged["surface"]
        # The canvas under the cursor, drawn by WebGL or in 2D, under the page's toolbar.
        assert surface["context"] == context and surface["box"] == [0.0, 0.0, 1600.0, 900.0], surface
        assert any(16 <= x <= 17 and 16 <= y <= 17 for x, y, _w, _h in surface["over"]), surface["over"]
        # The wheel turned on the take's clock over the step's 1.5 s, each turn logged once painted: as many as the
        # canvas paints in that time (a 2D one rastered on the CPU paints a few a second), the delta caught up on each.
        sent = [(tick[0], tick[1]) for tick in surface["ticks"]]
        start = next(t for t, delta in sent if delta != 0)
        end = next(t for t, delta in sent if delta == BY)
        assert sent[0][1] == 0 and sent[-1][1] == BY and sum(1 for _t, delta in sent if delta not in (0, BY)) >= 5
        assert [delta for _t, delta in sent] == sorted((delta for _t, delta in sent), reverse=True)
        # Its last turn sent once the step's seconds are up and logged once painted: up to one of the canvas's paints
        # past them, a few tenths of a second for a 2D canvas rastered on a loaded machine's CPU.
        turned = [t for t, _delta in sent[: next(i for i, (_t, delta) in enumerate(sent) if delta == BY) + 1]]
        paint = max(b - a for a, b in itertools.pairwise(turned))
        assert 1.5 - 0.3 <= end - start <= 1.5 + 0.3 + paint, f"the wheel turned over the step's seconds: {end - start:.3f} s"
        if context == "webgl2":
            assert all(tick[2] is None for tick in surface["ticks"]), "a WebGL canvas has no 2D transform to read"
        else:
            # The transform the 2D canvas draws under, read between the turns: the graph zoomed 2 ** 1.2 about them.
            first, last = surface["ticks"][0][2], surface["ticks"][-1][2]
            assert first[0] == pytest.approx(1.5) and last[0] / first[0] == pytest.approx(2**1.2, rel=0.01)
        paced = scroll_pacing.pace(
            result.frames, result.events, workdir=workdir, fps=script.fps, pixel_ratio=result.pixel_ratio, painted=result.painted
        )
        [scroll] = scroll_event["scrolls"]
        assert scroll["scroller"] == "canvas" and scroll["progress"] == progress, scroll
        if not scroll["paced"]:
            # A machine whose Chromium painted the zoom at the take's rate keeps its frames.
            assert scroll["why"] == scroll_pacing.PAINTED_AT_RATE and scroll["painted"] >= 54, scroll
        else:
            assert scroll["fps"] == 60.0 and scroll["painted"] < 54, scroll
            # 2 ** (600 * 0.002): how far the graph's wheel zooms 600 px of it.
            assert scroll["zoom"] == pytest.approx(2 ** 1.2, rel=0.08), scroll
        during = [(t, name) for t, name in paced.frames if start <= t <= end]
        assert len(during) >= 0.9 * 60 * (end - start) - 1
        # A frame repeats the one before only where the wheel moved under a pixel between them: its last turn of a
        # pixel or two, caught up a paint later on a slow machine, leaves the graph where it stands. Nor where it shows
        # the graph at rest - before the wheel turned, or as it stands once the zoom is done: the turns are logged once
        # painted, so a canvas painting a few frames a second can show the zoom done while its log still moves.
        log_t, log_offset = zip(*scroll["log"], strict=True)

        def grey(name: str) -> np.ndarray:
            with Image.open(Path(workdir) / name) as opened:
                return np.asarray(opened.convert("L").reduce(4), dtype=np.int16)

        resting = [name for t, name in paced.frames if t < start] or [paced.frames[0][1]]
        rests = (grey(resting[-1]), grey(paced.frames[-1][1]))
        previous = None
        repeated: list[float] = []
        for t, name in during:
            shown = grey(name)
            if previous is not None and np.abs(shown - previous[1]).mean() <= 0.2:
                moved = abs(float(np.interp(t, log_t, log_offset)) - float(np.interp(previous[0], log_t, log_offset)))
                if moved >= 1.0 and not any(np.abs(shown - rest).mean() <= 0.2 for rest in rests):
                    repeated.append(round(t, 3))
            previous = (t, shown)
        assert len(repeated) <= int(scroll.get("held") or 0) + 2, f"repeated at {repeated}: {scroll}"
        rate = scroll_pacing.take_rate(result.events, captured_frames=len(result.frames), duration=result.duration)
        assert rate.scrolls and rate.fps >= 54 and not rate.steps, rate
        words = scroll_pacing.scroll_steps_words(result.events)
        assert words.startswith(f"Scroll steps: step 1 (scroll) zoomed a canvas: painted at {scroll['painted']:.1f} fps, "), words
