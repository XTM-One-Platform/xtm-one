"""A step on an option of a graph's screen-reader-only list is refused, naming the node to act on, in a real Chromium.
The fixture mirrors the OpenCTI graph: a canvas painting each node's label under its circle, whose context
menu opens on a hovered node, and a visually hidden ``role=listbox`` (``sr-only``: 1 px, clipped, overflow hidden)
with one ``role=option`` per node, as ``GraphAccessibleList`` draws it - no hook: the node step the refusal
names finds its node by its label. A hidden element a click at its point still reaches - a toggle's input under its
label, an icon button's hidden name - is clicked as before. Skipped where Playwright's Chromium is not installed; the
``Browser tool (Chromium)`` CI job sets ``REQUIRE_CHROMIUM=1``.
"""

from __future__ import annotations

import os
import shutil
import tempfile
import threading
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse

import pytest

from app.services import web_fetch
from app.services.browser_profiles import browser
from app.services.browser_profiles.browser import TargetPolicy
from app.services.video_studio import capture, spec

pytestmark = pytest.mark.chromium

HOST = "app.localhost"
PAGE = """<!doctype html><html><head><style>
body { margin: 0; background: #0b0f1a; font: 16px sans-serif; color: #f4f6fa; }
#graph { position: absolute; left: 100px; top: 80px; width: 1200px; height: 700px; display: block; }
#menu { position: absolute; display: none; background: #1a2238; padding: 6px 0; min-width: 240px; }
#menu div { padding: 6px 14px; }
.sr-only { position: absolute; width: 1px; height: 1px; padding: 0; margin: -1px; overflow: hidden;
  clip: rect(0, 0, 0, 0); white-space: nowrap; border: 0; }
#fit { position: absolute; left: 1360px; top: 100px; width: 180px; height: 44px; }
#dark { position: absolute; left: 1360px; top: 200px; width: 180px; height: 44px; display: block; background: #1a2238; }
#dark input { left: 20px; top: 24px; margin: 0; }
#close { position: absolute; left: 1360px; top: 300px; width: 64px; height: 44px; }
#close .sr-only { left: 10px; top: 10px; margin: 0; }
#said { position: absolute; left: 1360px; top: 400px; }
</style></head><body>
<canvas id="graph" width="1200" height="700"></canvas>
<button id="fit">Fit the graph</button>
<label id="dark"><input type="checkbox" class="sr-only" id="dark-input"><span>Dark mode</span></label>
<button id="close"><span class="sr-only">Close the panel</span>X</button>
<div id="said"></div>
<div class="sr-only" role="listbox" tabindex="0" aria-label="Elements of the graph">
  <div role="option" aria-selected="false">Intrusion-Set Storm-1567, 6 relationships</div>
  <div role="option" aria-selected="false">Malware Akira, 4 relationships</div>
</div>
<div id="menu" role="menu"><div role="menuitem">Select entity and neighbours</div><div role="menuitem">Hide</div></div>
<script>
const canvas = document.getElementById('graph'), ctx = canvas.getContext('2d'), menu = document.getElementById('menu');
const NODES = [['Storm-1567', 500, 300], ['Akira', 800, 200]].map(([label, x, y]) => ({ label, x, y }));
let hovered = null;
function draw() {
  ctx.setTransform(1, 0, 0, 1, 0, 0); ctx.fillStyle = '#0b0f1a'; ctx.fillRect(0, 0, 1200, 700);
  for (const n of NODES) {
    ctx.beginPath(); ctx.arc(n.x, n.y, 24, 0, Math.PI * 2);
    ctx.fillStyle = n === hovered ? '#00f0ff' : '#5b6cff'; ctx.fill();
    ctx.fillStyle = '#f4f6fa'; ctx.font = '22px sans-serif'; ctx.textAlign = 'center'; ctx.textBaseline = 'top';
    ctx.fillText(n.label, n.x, n.y + 22);
  }
}
const at = (cx, cy) => { const r = canvas.getBoundingClientRect();
  return NODES.find((n) => Math.hypot(r.left + n.x - cx, r.top + n.y - cy) <= 24) || null; };
canvas.addEventListener('pointermove', (e) => { hovered = at(e.clientX, e.clientY); draw(); });
canvas.addEventListener('contextmenu', (e) => {
  e.preventDefault();
  if (hovered) { menu.style.left = e.clientX + 'px'; menu.style.top = e.clientY + 'px'; menu.style.display = 'block'; }
});
document.addEventListener('contextmenu', (e) => e.preventDefault());
const said = document.getElementById('said');
document.getElementById('dark-input').addEventListener('change', (e) => { said.textContent = e.target.checked ? 'Dark mode on' : ''; });
document.getElementById('close').addEventListener('click', () => { said.textContent = 'Panel closed'; });
draw();
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
    try:
        yield f"http://{HOST}:{server.server_address[1]}/"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(5)


async def _record(start_url: str, workdir: str, steps: list[dict]) -> capture.CaptureResult:
    script = spec.parse_take_script(
        spec.validate_take_script(
            {
                "viewport": {"width": 1600, "height": 900},
                "pixel_ratio": 1,
                "settle_ms": 0,
                "end_hold_ms": 300,
                "steps": steps,
            }
        )
    )
    try:
        return await capture.record_take(
            script, workdir=workdir, profile=None, start_url=start_url, policy=TargetPolicy(), max_seconds=60
        )
    except Exception as exc:
        if "Executable doesn't exist" in str(exc) or "playwright install" in str(exc).lower():
            if os.environ.get("REQUIRE_CHROMIUM") == "1":
                pytest.fail(f"Playwright's Chromium is required here and is not installed: {exc}")
            pytest.skip("Playwright's Chromium is not installed (python -m playwright install chromium)")
        raise


async def test_a_right_click_on_a_screen_reader_only_option_fails_its_step_naming_the_node_to_act_on(site):
    with tempfile.TemporaryDirectory() as workdir, pytest.raises(capture.StepFailedError) as failed:
        await _record(
            site,
            workdir,
            [
                {"action": "wait", "seconds": 0.2},
                {"action": "right_click", "role": "option", "name": "Intrusion-Set Storm-1567, 6 relationships"},
                {"action": "click", "role": "menuitem", "name": "Select entity and neighbours"},
            ],
        )
    message = str(failed.value)
    assert message.startswith("Step 1 (right_click"), message
    assert "the option of the list 'Elements of the graph' is drawn for a screen reader only" in message
    assert "a click at its point lands " in message
    assert "act on node {label: 'Intrusion-Set Storm-1567'} or a point (at {x, y, in: 'canvas'})" in message
    assert failed.value.step == 1


def _needs_ocr() -> None:
    """A node read by its painted label needs Tesseract: the CI job installs it, so its absence there is a failure."""
    if shutil.which("tesseract") is None:
        if os.environ.get("REQUIRE_CHROMIUM") == "1":
            pytest.fail("Tesseract is required here and is not installed (apt-get install tesseract-ocr)")
        pytest.skip("Tesseract is not installed (apt-get install tesseract-ocr)")


async def test_the_node_step_the_refusal_names_opens_the_menu_the_next_step_clicks(site):
    """The label the refusal names is the entry's text before its comma, type included: the canvas paints only the
    title after it, which OCR reads as a whole label."""
    _needs_ocr()
    with tempfile.TemporaryDirectory() as workdir:
        result = await _record(
            site,
            workdir,
            [
                {"action": "wait", "seconds": 0.2},
                {"action": "right_click", "node": {"label": "Intrusion-Set Storm-1567"}},
                {"action": "click", "role": "menuitem", "name": "Select entity and neighbours"},
            ],
        )
    right = next(e for e in result.events if e["action"] == "right_click")
    assert right["located"]["by"] == "ocr" and right["located"]["ocr"]["text"] == "Storm-1567"
    assert right["located"]["identity"]["match"] == "exact"
    assert right["menu"]["items"][:1] == ["Select entity and neighbours"]
    assert next(e for e in result.events if e["action"] == "click")["ok"]


@pytest.mark.parametrize(
    ("step", "shown"),
    [
        # A toggle's input hidden under its label: a click at the input's point lands on the label, which checks it.
        ({"action": "click", "role": "checkbox", "name": "Dark mode"}, "Dark mode on"),
        # An icon button's hidden name: a click at its point lands inside the button it names.
        ({"action": "click", "selector": "#close .sr-only"}, "Panel closed"),
    ],
)
async def test_a_hidden_element_a_click_at_its_point_still_reaches_is_clicked_as_before(site, step, shown):
    """The refusal reads where the click lands, not the clipping alone: a hidden element whose point lies on its
    label or inside the control it names is clicked as it always was."""
    with tempfile.TemporaryDirectory() as workdir:
        result = await _record(
            site,
            workdir,
            [{"action": "wait", "seconds": 0.2}, step, {"action": "wait_for", "text": shown, "timeout": 5}],
        )
    assert next(e for e in result.events if e["action"] == "click")["ok"]
    assert next(e for e in result.events if e["action"] == "wait_for")["ok"]


async def test_the_same_right_click_on_the_node_opens_the_menu_the_next_step_clicks(site):
    _needs_ocr()
    with tempfile.TemporaryDirectory() as workdir:
        result = await _record(
            site,
            workdir,
            [
                {"action": "wait", "seconds": 0.2},
                {"action": "right_click", "node": {"label": "Storm-1567"}},
                {"action": "click", "role": "menuitem", "name": "Select entity and neighbours"},
            ],
        )
    right = next(e for e in result.events if e["action"] == "right_click")
    assert right["ok"] and right["target"] == {"node": "Storm-1567"} and right["located"]["by"] == "ocr"
    assert right["menu"]["items"][:1] == ["Select entity and neighbours"]
    assert next(e for e in result.events if e["action"] == "click")["ok"]


async def test_an_element_a_person_sees_is_clicked_as_before(site):
    """The refusal reads the clipping, not the role: a visible control is clicked as it always was."""
    with tempfile.TemporaryDirectory() as workdir:
        result = await _record(
            site,
            workdir,
            [
                {"action": "wait", "seconds": 0.2},
                {"action": "click", "role": "button", "name": "Fit the graph"},
            ],
        )
    assert next(e for e in result.events if e["action"] == "click")["ok"]
