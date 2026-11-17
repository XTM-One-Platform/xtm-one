"""A node step on a graph drawn as OpenCTI's is, in a real Chromium and Tesseract.

The fixture draws a graph on one ``<canvas>`` the way OpenCTI's production graph does where the fixture of
``test_video_studio_node_locate_6684_chromium`` did not:

* one hovered element, set by the pointer over a node and by the keyboard list's active entry while it has focus
  (cleared when it loses it); the hovered node is drawn in another colour, its links thicker and brighter;
* a context menu for the hovered element, not for the point clicked, headed by a title (no role, as a design
  system's menu label): the node's title over its actions, "Graph" over the canvas's 6 when nothing is hovered;
* a hover card drawn over the canvas with ``pointer-events: none`` (``?card=0`` drops it), the hovered node's title
  in larger type, following the pointer and the list's active entry;
* edge labels holding nodes' titles, drawn as OpenCTI draws them, a halo of the ground cleared around: "Berlin" along
  the horizontal link Vienna - Lisbon, "Madrid" rotated 5 degrees along Paris - Madrid, "Vienna" in italic along
  Berlin - Prague;
* Bergen, its label drawn 48 px under its circle: the point 1.2 label heights above the label is off the node;
* Geneva and Zurich close enough that their labels overlap, and a screen-reader list mirroring every node with
  ``aria-posinset`` / ``aria-setsize``; ``?stuck=1`` stops its active entry at Vienna.

Every pointer event, the list's active entry and the menu entries clicked are posted back with the node they hit.
Skipped where Playwright's Chromium is not installed; the ``Browser tool (Chromium)`` CI job installs it, and
Tesseract, and sets ``REQUIRE_CHROMIUM=1``, which turns the skip into a failure.
"""

from __future__ import annotations

import asyncio
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
#: Where the canvas sits in the window, and the radius of a node.
LEFT, TOP, R = 100, 80, 16
PAGE = """<!doctype html><html><head><style>
body { margin: 0; background: #0b0f1a; font: 16px sans-serif; color: #f4f6fa; }
#graph { position: absolute; left: 100px; top: 80px; width: 1200px; height: 700px; display: block; }
#menu { position: absolute; display: none; background: #1a2238; padding: 6px 0; min-width: 240px; z-index: 5; }
#menu .title { padding: 6px 14px; color: #8a93a8; font-size: 13px; }
#menu [role=menuitem] { padding: 6px 14px; }
#card { position: absolute; display: none; width: 230px; height: 96px; padding: 12px; box-sizing: border-box;
  background: #1a2238; pointer-events: none; z-index: 3; }
#card .name { font: bold 22px sans-serif; } #card .kind { font-size: 14px; color: #a3acc0; margin-top: 6px; }
.sr-only { position: absolute; width: 1px; height: 1px; padding: 0; margin: -1px; overflow: hidden;
  clip: rect(0, 0, 0, 0); white-space: nowrap; border: 0; }
</style></head><body>
<canvas id="graph"></canvas>
<div id="card"><div class="name"></div><div class="kind"></div></div>
<div id="menu" role="menu"></div>
<script>
const NODES = [['Paris', 250, 150], ['Berlin', 550, 320], ['Madrid', 850, 202], ['Vienna', 350, 520],
  ['Lisbon', 980, 520], ['Geneva', 640, 620], ['Zurich', 660, 624], ['Prague', 1100, 320], ['Bergen', 200, 380, 48]]
  .map(([label, x, y, drop]) => ({ label, x, y, drop: drop || 0 }));
const named = (label) => NODES.find((n) => n.label === label);
// No link crosses a node's label, as a laid-out graph keeps them.
const LINKS = [['Vienna', 'Lisbon', 'Berlin', ''], ['Paris', 'Madrid', 'Madrid', ''], ['Berlin', 'Prague', 'Vienna', 'italic '],
  ['Paris', 'Berlin'], ['Geneva', 'Lisbon']]
  .map(([a, b, label, style]) => ({ a: named(a), b: named(b), label, style }));
const LISTED = [...NODES, { label: 'Oslo' }];
const degree = (n) => LINKS.filter((l) => l.a === n || l.b === n).length;
const R = 16, FONT = 15, CARD = !location.search.includes('card=0'), STUCK = location.search.includes('stuck=1');
const canvas = document.getElementById('graph'), menu = document.getElementById('menu'), card = document.getElementById('card');
const dpr = window.devicePixelRatio || 1;
canvas.width = 1200 * dpr; canvas.height = 700 * dpr;
const ctx = canvas.getContext('2d');
let hovered = null, active = -1, focused = false;
const log = [];
function draw() {
  ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  ctx.fillStyle = '#0b0f1a'; ctx.fillRect(0, 0, 1200, 700);
  for (const l of LINKS) {
    const lit = hovered && (l.a === hovered || l.b === hovered);
    ctx.strokeStyle = lit ? '#e8ecf5' : '#46506a'; ctx.lineWidth = lit ? 4 : 2;
    ctx.beginPath(); ctx.moveTo(l.a.x, l.a.y); ctx.lineTo(l.b.x, l.b.y); ctx.stroke();
  }
  for (const n of NODES) {
    ctx.beginPath(); ctx.arc(n.x, n.y, R, 0, Math.PI * 2);
    ctx.fillStyle = n === hovered ? '#ffd166' : '#5b6cff'; ctx.fill();
  }
  ctx.fillStyle = '#f4f6fa'; ctx.font = FONT + 'px sans-serif'; ctx.textAlign = 'center'; ctx.textBaseline = 'top';
  for (const n of NODES) ctx.fillText(n.label, n.x, n.y + R + n.drop);
  for (const l of LINKS) {
    if (!l.label) continue;
    let angle = Math.atan2(l.b.y - l.a.y, l.b.x - l.a.x);
    if (angle > Math.PI / 2) angle -= Math.PI;
    if (angle < -Math.PI / 2) angle += Math.PI;
    ctx.save(); ctx.translate((l.a.x + l.b.x) / 2, (l.a.y + l.b.y) / 2); ctx.rotate(angle);
    ctx.font = l.style + FONT + 'px sans-serif'; ctx.textAlign = 'center'; ctx.textBaseline = 'middle';
    ctx.lineJoin = 'round'; ctx.lineWidth = 7; ctx.strokeStyle = '#0b0f1a'; ctx.strokeText(l.label, 0, 0);
    ctx.fillStyle = '#c3c9d6'; ctx.fillText(l.label, 0, 0); ctx.restore();
  }
  if (CARD && hovered) {
    card.querySelector('.name').textContent = hovered.label;
    card.querySelector('.kind').textContent = 'City, ' + degree(hovered) + ' relationships';
    card.style.left = (100 + hovered.x + 34) + 'px'; card.style.top = (80 + hovered.y - 20) + 'px'; card.style.display = 'block';
  } else card.style.display = 'none';
}
const hover = (node) => { if (node !== hovered) { hovered = node; draw(); } };
const at = (x, y) => {
  const r = canvas.getBoundingClientRect();
  return NODES.find((n) => Math.hypot(r.left + n.x - x, r.top + n.y - y) <= R) || null;
};
const list = document.createElement('div');
list.className = 'sr-only'; list.setAttribute('role', 'listbox'); list.tabIndex = 0;
list.setAttribute('aria-label', 'Elements of the graph');
LISTED.forEach((n, i) => {
  const option = document.createElement('div');
  option.setAttribute('role', 'option'); option.id = 'node-' + i; option.setAttribute('aria-selected', 'false');
  option.setAttribute('aria-posinset', String(i + 1)); option.setAttribute('aria-setsize', String(LISTED.length));
  option.textContent = 'City ' + n.label + ', ' + (NODES.includes(n) ? degree(n) : 0) + ' relationships';
  list.appendChild(option);
});
list.addEventListener('focus', () => { focused = true; hover(active >= 0 && NODES.includes(LISTED[active]) ? LISTED[active] : null); });
list.addEventListener('blur', () => { focused = false; hover(null); });
list.addEventListener('keydown', (e) => {
  if (e.key !== 'ArrowDown' && e.key !== 'ArrowUp') return;
  e.preventDefault();
  active = e.key === 'ArrowDown' ? Math.min(active + 1, STUCK ? 3 : LISTED.length - 1) : Math.max(active - 1, 0);
  list.setAttribute('aria-activedescendant', 'node-' + active);
  log.push({ type: 'active', node: LISTED[active].label });
  hover(NODES.includes(LISTED[active]) ? LISTED[active] : null);
});
document.body.appendChild(list);
for (const type of ['pointermove', 'pointerdown', 'click', 'contextmenu']) {
  canvas.addEventListener(type, (e) => {
    const node = at(e.clientX, e.clientY);
    if (type !== 'pointermove') log.push({ type, x: e.clientX, y: e.clientY, node: node && node.label });
    if (type === 'pointermove') hover(node);
    if (type !== 'contextmenu') return;
    e.preventDefault();
    const [title, items] = hovered
      ? [hovered.label, ['Select entity and neighbours', 'Pin in place', 'Hide']]
      : ['Graph', ['Select all', 'Select by type', 'Clear selection', 'Show the hidden entities', 'Ungroup all', 'Reset the layout']];
    menu.innerHTML = '<div class="title">' + title + '</div>'
      + items.map((item) => '<div role="menuitem" tabindex="-1">' + item + '</div>').join('');
    menu.dataset.node = hovered ? hovered.label : '';
    menu.style.left = e.clientX + 'px'; menu.style.top = e.clientY + 'px'; menu.style.display = 'block';
  });
}
menu.addEventListener('click', (e) => {
  log.push({ type: 'menu', item: e.target.textContent, node: menu.dataset.node || null });
  menu.style.display = 'none';
});
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


async def _record(start_url: str, workdir: str, steps: list[dict]) -> capture.CaptureResult:
    """A take of *steps* at 1600x900 and pixel ratio 2, as the production graph was filmed."""
    script = spec.parse_take_script(
        spec.validate_take_script(
            {
                "viewport": {"width": 1600, "height": 900},
                "pixel_ratio": 2,
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


async def _hits(kind: str) -> list[dict]:
    await asyncio.sleep(0.3)
    return [e for e in _Handler.logged if e["type"] == kind]


def _under(box: dict, node: tuple[float, float]) -> bool:
    """Whether the label box OCR read (CSS px of the window) is the one painted under the node at *node* (canvas px)."""
    x, y = LEFT + node[0], TOP + node[1] + R
    return abs(box["x"] + box["width"] / 2 - x) <= 6 and y - 2 <= box["y"] <= y + 8


@pytest.mark.parametrize(
    ("label", "node", "edge"),
    [("Berlin", (550, 320), "along a line"), ("Madrid", (850, 202), "rotated"), ("Vienna", (350, 520), "italic")],
)
async def test_an_edge_label_holding_a_nodes_title_is_left_out_and_the_node_right_clicked(site, label, node, edge):
    with tempfile.TemporaryDirectory() as workdir:
        result = await _record(
            site + "?card=0", workdir, [{"action": "wait", "seconds": 0.2}, {"action": "right_click", "node": {"label": label}}]
        )
    right = result.events[1]
    located = right["located"]
    assert located["by"] == "ocr" and _under(located["ocr"]["box"], node), located
    # The edge's label as OCR read it (the line it is drawn on may add a stray glyph), and why it is not the node's.
    assert any(said.startswith(f"'{label}") and said.endswith(f"' {edge}") for said in located["ocr"].get("left_out", [])), located
    assert located["point"] == right["point"]
    assert right["menu"]["titles"] == [label] and right["menu_check"] == {"title": label, "node": True}
    assert [e["node"] for e in await _hits("contextmenu")] == [label]


async def test_a_title_drawn_twice_on_the_canvas_and_in_the_hover_card_is_read_on_the_canvas(site):
    with tempfile.TemporaryDirectory() as workdir:
        result = await _record(
            site,
            workdir,
            [
                {"action": "wait", "seconds": 0.2},
                {"action": "hover", "node": {"label": "Berlin"}, "hover_ms": 400},
                {"action": "right_click", "node": {"label": "Berlin"}},
                {"action": "click", "role": "menuitem", "name": "Select entity and neighbours"},
            ],
        )
    right = result.events[2]
    # The card (22 px, bold, pointer-events: none) says "Berlin" beside the node: its words are never the node's, so
    # the title is read in one place only, the canvas's.
    assert right["located"]["by"] == "ocr" and _under(right["located"]["ocr"]["box"], (550, 320)), right["located"]
    assert "among" not in right["located"]["ocr"], right["located"]
    assert right["menu_check"] == {"title": "Berlin", "node": True}
    assert [e["node"] for e in await _hits("contextmenu")] == ["Berlin"]
    assert [(e["item"], e["node"]) for e in await _hits("menu")] == [("Select entity and neighbours", "Berlin")]


async def test_the_highlight_is_the_nodes_not_the_one_hovered_before_nor_the_card_following_the_list(site):
    with tempfile.TemporaryDirectory() as workdir:
        result = await _record(
            site,
            workdir,
            [
                {"action": "wait", "seconds": 0.2},
                {"action": "hover", "node": {"label": "Paris"}, "hover_ms": 400},
                {"action": "right_click", "node": {"label": "Geneva"}},
            ],
        )
    right = result.events[2]
    located = right["located"]
    assert located["by"] == "highlight", located
    assert located["highlight"]["shots"] == 3 and located["highlight"]["masked"] >= 1, located
    # Lisbon, Geneva and Zurich made active in turn, then the cursor hovered Geneva and its menu opened.
    assert [e["node"] for e in await _hits("active")][-3:] == ["Lisbon", "Geneva", "Zurich"]
    assert abs(right["point"]["x"] - (LEFT + 640)) <= 10 and abs(right["point"]["y"] - (TOP + 620)) <= 10, right["point"]
    assert right["menu_check"] == {"title": "Geneva", "node": True}
    assert [e["node"] for e in await _hits("contextmenu")] == ["Geneva"]


async def test_a_list_whose_active_entry_stops_short_of_the_node_fails_naming_where(site):
    with tempfile.TemporaryDirectory() as workdir, pytest.raises(capture.StepFailedError) as failed:
        await _record(site + "?stuck=1", workdir, [{"action": "wait", "seconds": 0.2}, {"action": "click", "node": {"label": "Geneva"}}])
    message = str(failed.value)
    assert "the list's active entry stopped on 'City Vienna', not on the node's ('City Geneva')" in message, message
    assert not await _hits("click")


async def test_a_right_click_whose_menu_is_not_the_nodes_fails_there_naming_the_title_it_read(site):
    with tempfile.TemporaryDirectory() as workdir:
        with pytest.raises(capture.StepFailedError) as failed:
            await _record(
                site,
                workdir,
                [
                    {"action": "wait", "seconds": 0.2},
                    {"action": "right_click", "node": {"label": "Bergen"}},
                    {"action": "click", "role": "menuitem", "name": "Select entity and neighbours"},
                ],
            )
        message = str(failed.value)
        assert failed.value.step == 1, message
        assert "the menu it opened is headed 'Graph', 6 entries, not the node 'Bergen': the point it resolved by ocr, (" in message
        assert message.split("), is not on the node")[0]
        # The canvas the node was read on, the point marked, ends the failed take's contact sheet.
        [(name, said)] = failed.value.attached
        assert (Path(workdir) / name).stat().st_size > 1000 and said.startswith("step 1: the canvas read for")
    # The step is in the failed take's action log, with what it read.
    logged = failed.value.events[-1]
    assert logged["step"] == 1 and logged["ok"] is False and logged["located"]["by"] == "ocr"
    assert logged["menu_check"] == {"title": "Graph", "node": False}
    assert [e["node"] for e in await _hits("contextmenu")] == [None]
