"""A node step records on a canvas graph that exposes no hook, in a real Chromium.

The fixture is a graph drawn on one ``<canvas>`` at the page's pixel ratio:
labelled circles - each label painted under its circle -, two of them close
enough that their labels overlap, and no element for any node. Beside it, as
a graph's accessible list does, a visually hidden ``role=listbox`` mirrors
every node ("City Paris, 2 relationships"), one more ("Oslo") that the canvas
does not draw; focused, its active entry (``aria-activedescendant``) moves
with ArrowDown / ArrowUp and the canvas paints that node in another colour.
The canvas pans when dragged on its ground, opens a context menu on a hovered
node, and posts every pointer event back to the test server with the node it
hit. ``?list=0`` drops the list.

Skipped where Playwright's Chromium is not installed; the ``Browser tool (Chromium)`` CI job installs it, and
Tesseract, and sets ``REQUIRE_CHROMIUM=1``, which turns the skip into a failure.
"""

from __future__ import annotations

import json
import os
import shutil
import tempfile
import threading
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
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
</style></head><body>
<canvas id="graph"></canvas>
<div id="menu" role="menu"><div role="menuitem">Select its neighbours</div><div role="menuitem">Hide it</div></div>
<script>
const NODES = [['Paris', 250, 150, 2], ['Berlin', 550, 320, 3], ['Madrid', 850, 200, 1], ['Vienna', 350, 520, 2],
  ['Lisbon', 980, 470, 1], ['Geneva', 640, 600, 1], ['Zurich', 660, 604, 1]]
  .map(([label, x, y, links]) => ({ label, x, y, links }));
const LISTED = [...NODES, { label: 'Oslo', links: 0 }];
const R = 16, FONT = 15;
const canvas = document.getElementById('graph'), menu = document.getElementById('menu');
const dpr = window.devicePixelRatio || 1;
canvas.width = 1200 * dpr; canvas.height = 700 * dpr;
const ctx = canvas.getContext('2d');
const view = { px: 0, py: 0 };
let hovered = null, active = -1, focused = false, panning = null;
const log = [];
const place = (n) => [n.x + view.px, n.y + view.py];
function draw() {
  ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  ctx.fillStyle = '#0b0f1a'; ctx.fillRect(0, 0, 1200, 700);
  for (const n of NODES) {
    const [cx, cy] = place(n);
    ctx.beginPath(); ctx.arc(cx, cy, R, 0, Math.PI * 2);
    ctx.fillStyle = focused && LISTED[active] === n ? '#ffd166' : n === hovered ? '#00f0ff' : '#5b6cff';
    ctx.fill();
  }
  ctx.fillStyle = '#f4f6fa'; ctx.font = FONT + 'px sans-serif'; ctx.textAlign = 'center'; ctx.textBaseline = 'top';
  for (const n of NODES) { const [cx, cy] = place(n); ctx.fillText(n.label, cx, cy + R); }
}
const at = (x, y) => {
  const r = canvas.getBoundingClientRect();
  return NODES.find((n) => { const [cx, cy] = place(n); return Math.hypot(r.left + cx - x, r.top + cy - y) <= R; }) || null;
};
if (!location.search.includes('list=0')) {
  const list = document.createElement('div');
  list.className = 'sr-only'; list.setAttribute('role', 'listbox'); list.tabIndex = 0;
  list.setAttribute('aria-label', 'Elements of the graph');
  LISTED.forEach((n, i) => {
    const option = document.createElement('div');
    option.setAttribute('role', 'option'); option.id = 'node-' + i; option.setAttribute('aria-selected', 'false');
    option.textContent = 'City ' + n.label + ', ' + n.links + ' relationships';
    list.appendChild(option);
  });
  list.addEventListener('focus', () => { focused = true; draw(); });
  list.addEventListener('blur', () => { focused = false; draw(); });
  list.addEventListener('keydown', (e) => {
    if (e.key !== 'ArrowDown' && e.key !== 'ArrowUp') return;
    e.preventDefault();
    active = e.key === 'ArrowDown' ? Math.min(active + 1, LISTED.length - 1) : Math.max(active - 1, 0);
    list.setAttribute('aria-activedescendant', 'node-' + active);
    log.push({ type: 'active', node: LISTED[active].label });
    draw();
  });
  document.body.appendChild(list);
}
for (const type of ['pointermove', 'pointerdown', 'pointerup', 'click', 'contextmenu']) {
  canvas.addEventListener(type, (e) => {
    const node = at(e.clientX, e.clientY);
    if (type !== 'pointermove' || e.buttons) log.push({ type, x: e.clientX, y: e.clientY, node: node && node.label, buttons: e.buttons });
    if (type === 'pointerdown' && !node) panning = [e.clientX, e.clientY, view.px, view.py];
    if (type === 'pointerup') panning = null;
    if (type === 'pointermove') {
      if (panning && e.buttons) { view.px = panning[2] + e.clientX - panning[0]; view.py = panning[3] + e.clientY - panning[1]; }
      hovered = node; draw();
    }
    if (type === 'contextmenu') {
      e.preventDefault();
      if (hovered) { menu.style.left = e.clientX + 'px'; menu.style.top = e.clientY + 'px'; menu.style.display = 'block'; }
    }
  });
}
document.addEventListener('keydown', (e) => { if (e.key === 'Escape') menu.style.display = 'none'; });
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


@pytest.fixture(autouse=True)
def _ocr_engine() -> None:
    """The OCR stage needs Tesseract: the CI job installs it, so its absence there is a failure."""
    if shutil.which("tesseract") is None:
        if os.environ.get("REQUIRE_CHROMIUM") == "1":
            pytest.fail("Tesseract is required here and is not installed (apt-get install tesseract-ocr)")
        pytest.skip("Tesseract is not installed (apt-get install tesseract-ocr)")


async def _record(start_url: str, workdir: str, steps: list[dict], *, pixel_ratio: float = 1) -> capture.CaptureResult:
    script = spec.parse_take_script(
        spec.validate_take_script(
            {
                "viewport": {"width": 1600, "height": 900},
                "pixel_ratio": pixel_ratio,
                "settle_ms": 0,
                "end_hold_ms": 300,
                "steps": steps,
            }
        )
    )
    try:
        return await capture.record_take(
            script, workdir=workdir, profile=None, start_url=start_url, policy=TargetPolicy(), max_seconds=90
        )
    except Exception as exc:
        if "Executable doesn't exist" in str(exc) or "playwright install" in str(exc).lower():
            if os.environ.get("REQUIRE_CHROMIUM") == "1":
                pytest.fail(f"Playwright's Chromium is required here and is not installed: {exc}")
            pytest.skip("Playwright's Chromium is not installed (python -m playwright install chromium)")
        raise


def _hits(kind: str) -> list[dict]:
    return [e for e in _Handler.logged if e["type"] == kind]


async def _settled_log() -> None:
    import asyncio

    await asyncio.sleep(0.3)


@pytest.mark.parametrize("pixel_ratio", [1, 2])
async def test_a_node_read_by_ocr_is_clicked_with_no_hook_on_the_page(site, pixel_ratio):
    with tempfile.TemporaryDirectory() as workdir:
        result = await _record(
            site,
            workdir,
            [{"action": "wait", "seconds": 0.2}, {"action": "click", "node": {"label": "Paris"}}],
            pixel_ratio=pixel_ratio,
        )
    await _settled_log()
    click = result.events[1]
    assert click["ok"] and click["target"] == {"node": "Paris"}
    located = click["located"]
    assert located["by"] == "ocr", located
    assert located["identity"] == {"entry": "City Paris, 2 relationships", "match": "exact", "list": "Elements of the graph"}
    assert located["ocr"]["text"] == "Paris" and located["ocr"]["match"] == "exact"
    assert located["said"].startswith("label read in the list 'Elements of the graph' (exact), its box read on the canvas by OCR")
    # The label is about 11 px tall: the node 1.2 label heights above its centre, inside the circle of radius 16.
    assert 8 <= located["ocr"]["label_px"] <= 16
    [clicked] = _hits("click")
    assert clicked["node"] == "Paris", clicked
    assert abs(click["point"]["x"] - 350) <= 3 and abs(click["point"]["y"] - 230) <= 14
    # The camera frames the node and its label.
    box = click["box"]
    assert box["x"] <= 350 <= box["x"] + box["w"] and box["y"] <= 230 <= box["y"] + box["h"]


async def test_two_labels_overlapping_fall_back_to_the_highlight_the_node_takes_on_focus(site):
    with tempfile.TemporaryDirectory() as workdir:
        result = await _record(
            site, workdir, [{"action": "wait", "seconds": 0.2}, {"action": "click", "node": {"label": "Geneva"}}]
        )
    await _settled_log()
    located = result.events[1]["located"]
    assert located["by"] == "highlight", located
    assert located["ocr"] == {"missed": "no box by OCR"}
    # Lisbon active (five keys), then Geneva, then Zurich beside it: three shots.
    assert located["highlight"]["keys"] == 7 and located["highlight"]["shots"] == 3
    assert 0 < located["highlight"]["share"] < 0.01
    assert located["said"].endswith("no box by OCR, its highlight on focus")
    # The list's entry was made active as a keyboard user does, and the click hit Geneva.
    assert [e["node"] for e in _hits("active")][-3:] == ["Lisbon", "Geneva", "Zurich"]
    [clicked] = _hits("click")
    assert clicked["node"] == "Geneva", clicked


async def test_a_label_in_no_list_and_on_no_canvas_fails_naming_the_closest_and_keeps_the_canvas(site):
    with tempfile.TemporaryDirectory() as workdir:
        with pytest.raises(capture.StepFailedError) as failed:
            await _record(site, workdir, [{"action": "click", "node": {"label": "Pariss"}, "timeout": 1}])
        message = str(failed.value)
        assert "no node labelled 'Pariss' on the graph: the list 'Elements of the graph' mirroring it for screen " in message
        assert 'Closest on the page then: "City Paris" (node)' in message
        [(name, said)] = failed.value.attached
        assert (Path(workdir) / name).stat().st_size > 1000 and said.startswith("step 0: the canvas read for")
        assert capture.failure_sheet_frames(failed.value)[-1][1] == name


async def test_a_label_on_no_canvas_and_with_no_list_says_both_are_missing(site):
    with tempfile.TemporaryDirectory() as workdir, pytest.raises(capture.StepFailedError) as failed:
        await _record(site + "?list=0", workdir, [{"action": "click", "node": {"label": "Atlantis"}, "timeout": 1}])
    message = str(failed.value)
    assert (
        "no node labelled 'Atlantis' on the graph: no list on the page mirrors its nodes for screen readers, and OCR "
        "read no 'Atlantis' painted on the canvas" in message
    )
    assert failed.value.attached


async def test_a_page_with_no_list_still_finds_a_painted_label_by_ocr(site):
    with tempfile.TemporaryDirectory() as workdir:
        result = await _record(
            site + "?list=0", workdir, [{"action": "wait", "seconds": 0.2}, {"action": "click", "node": {"label": "Berlin"}}]
        )
    await _settled_log()
    located = result.events[1]["located"]
    assert located["by"] == "ocr" and located["identity"] is None
    assert located["said"].startswith("no list mirrors the graph, its box read on the canvas by OCR")
    assert [e["node"] for e in _hits("click")] == ["Berlin"]


async def test_a_node_in_the_list_the_canvas_does_not_draw_fails_at_the_highlight(site):
    with tempfile.TemporaryDirectory() as workdir, pytest.raises(capture.StepFailedError) as failed:
        await _record(site, workdir, [{"action": "wait", "seconds": 0.2}, {"action": "click", "node": {"label": "Oslo"}}])
    message = str(failed.value)
    assert (
        "node not located on the canvas (label read in the list 'Elements of the graph', no box by OCR, no highlight "
        "on focus: nothing changed on the canvas)" in message
    ), message
    assert failed.value.attached


async def test_a_right_click_on_a_node_opens_its_menu_read_as_menuitems(site):
    with tempfile.TemporaryDirectory() as workdir:
        result = await _record(
            site,
            workdir,
            [
                {"action": "wait", "seconds": 0.2},
                {"action": "right_click", "node": {"label": "Berlin"}},
                {"action": "click", "role": "menuitem", "name": "Select its neighbours"},
            ],
        )
    await _settled_log()
    right = result.events[1]
    assert right["located"]["by"] == "ocr" and right["menu"]["items"] == ["Select its neighbours", "Hide it"]
    assert [e["node"] for e in _hits("contextmenu")] == ["Berlin"]
    assert result.events[2]["ok"]


async def test_a_drag_from_one_node_releases_on_the_other(site):
    with tempfile.TemporaryDirectory() as workdir:
        result = await _record(
            site,
            workdir,
            [
                {"action": "wait", "seconds": 0.2},
                {"action": "drag", "node": {"label": "Madrid"}, "to": {"node": {"label": "Lisbon"}}},
            ],
        )
    await _settled_log()
    drag = result.events[1]
    assert drag["located"]["by"] == "ocr" and drag["to_located"]["by"] == "ocr"
    assert drag["to"] == {"node": "Lisbon"} and drag["moves"] >= 24
    assert [e["node"] for e in _hits("pointerdown")] == ["Madrid"]
    assert [e["node"] for e in _hits("pointerup")] == ["Lisbon"]


async def test_a_pan_between_two_steps_reads_the_node_again_where_it_moved(site):
    with tempfile.TemporaryDirectory() as workdir:
        result = await _record(
            site,
            workdir,
            [
                {"action": "wait", "seconds": 0.2},
                {"action": "click", "node": {"label": "Vienna"}},
                {"action": "drag", "at": {"x": 0.05, "y": 0.95, "in": "#graph"}, "to": {"x": 0.15, "y": 0.85, "in": "#graph"}},
                {"action": "click", "node": {"label": "Vienna"}},
            ],
        )
    await _settled_log()
    first, second = result.events[1]["point"], result.events[3]["point"]
    # The ground dragged 120 px right and 70 px up: the second click follows the node there.
    assert abs(second["x"] - first["x"] - 120) <= 3 and abs(second["y"] - first["y"] + 70) <= 3, (first, second)
    # The pan's own release clicks the ground, on no node.
    assert [e["node"] for e in _hits("click") if e["node"]] == ["Vienna", "Vienna"]
