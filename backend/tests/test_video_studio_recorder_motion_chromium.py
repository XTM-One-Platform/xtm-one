"""A take of an inner scroll panel, a hover tooltip and a slow-painting page plays at its frame rate, in a real Chromium.

* An application shell whose document never scrolls - its ``main#panel`` does (``overflow: auto``) - and a button whose
  tooltip fades in over 0.4 s, recorded with the screencast handing over 2.1 frames a second: the hover step and the
  scroll step both play at 24 fps or more once paced, and the log names the panel it scrolled.
* A page whose paint takes 300 ms each time it is scrolled, scrolled 600 px over 2.5 s at 1600x900: its scroll plays at
  30 frames a second, no two alike while it moves, and the quality check says no scroll will step.
* A list page keeping a search term: a step looking for a row the term hides fails naming the term beside the
  closest names.

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
from app.services.video_studio import capture, quality, scroll_pacing, spec, timeline
from tests import paced_scroll
from tests.test_video_studio_scroll_pacing_chromium import _painting_at

pytestmark = pytest.mark.chromium

HOST = "app.localhost"
ROWS = "".join(
    f'<div class="row" style="background: hsl({(i * 47) % 360} 45% {30 + (i * 7) % 30}%)">Row {i} - '
    f'{"alpha beta gamma delta " * (1 + i % 4)}</div>'
    for i in range(90)
)
STYLE = """body { margin: 0; font: 22px sans-serif; color: #f4f6fa; background: #0b0f1a; }
.row { height: 39px; border-bottom: 1px solid #000; padding-left: 24px; line-height: 39px; }"""
PANEL = f"""<!doctype html><html><head><style>{STYLE}
html, body {{ height: 100%; overflow: hidden; }}
header {{ position: fixed; top: 0; left: 0; right: 0; height: 60px; background: #1a2238; }}
nav {{ position: fixed; top: 60px; bottom: 0; left: 0; width: 220px; background: #141a2c; }}
main {{ position: absolute; top: 60px; bottom: 0; left: 220px; right: 0; overflow: auto; }}
#save {{ margin: 24px; font: 22px sans-serif; padding: 10px 28px; }}
#tip {{ position: absolute; left: 24px; top: 84px; padding: 12px 20px; background: #f4f6fa; color: #0b0f1a; opacity: 0;
  transition: opacity 0.4s linear; pointer-events: none; }}
#save:hover + #tip {{ opacity: 1; }}
</style></head><body><header>Shell</header><nav>Menu</nav>
<main id="panel" class="content"><button id="save">Save</button><div id="tip" class="tooltip">Saved to the workspace</div>
{ROWS}</main></body></html>"""
#: Each paint after a scroll costs the page 300 ms (a heavy dashboard), and the wheel waits on the page.
SLOW = f"""<!doctype html><html><head><style>{STYLE}</style></head><body>{ROWS}<script>
addEventListener('wheel', () => {{}}, {{ passive: false }});
let seen = scrollY;
const paint = () => {{
  if (scrollY !== seen) {{ seen = scrollY; const until = performance.now() + 300; while (performance.now() < until); }}
  requestAnimationFrame(paint);
}};
requestAnimationFrame(paint);
</script></body></html>"""
LIST = f"""<!doctype html><html><head><style>{STYLE}</style></head><body>
<input aria-label="Search" value="alpha" style="margin: 20px; font: 22px sans-serif">
{"".join(f'<div class="row"><a href="#">Row alpha {i}</a></div>' for i in range(8))}</body></html>"""
PAGES = {"/panel": PANEL, "/slow": SLOW, "/list": LIST}


class _Handler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        body = PAGES.get(urlparse(self.path).path, PANEL).encode("utf-8")
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


async def _record(start_url: str, workdir: str, steps: list[dict], *, pixel_ratio: int = 1) -> capture.CaptureResult:
    script = spec.parse_take_script(
        spec.validate_take_script(
            {"viewport": {"width": 1600, "height": 900}, "pixel_ratio": pixel_ratio, "settle_ms": 0, "end_hold_ms": 400, "steps": steps}
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


#: CSS pixels a scroll moves between two frames for the second to be a new picture: one pixel of the quarter-size
#: comparison of a 1600-pixel-wide window.
_VISIBLE_MOVE = 4.0


def _distinct(
    workdir: str, frames: list[tuple[float, str]], region: tuple[slice, slice], *, scroll: dict | None = None
) -> list[float]:
    """The instants of *frames* that repeat the one before inside *region* (a quarter-size grey picture's) - for a
    *scroll*, only where the page moved between them as the take draws it (``paced_scroll.moved``)."""
    previous, repeated = None, []
    for t, name in frames:
        with Image.open(Path(workdir) / name) as opened:
            shown = np.asarray(opened.convert("L").resize((400, 225)), dtype=np.int16)[region]
        same = previous is not None and np.abs(shown - previous[1]).mean() <= 0.3
        if same and (scroll is None or paced_scroll.moved(scroll, previous[0], t) >= _VISIBLE_MOVE):
            repeated.append(round(t, 3))
        previous = (t, shown)
    return repeated


async def test_an_inner_scroll_panel_and_a_hover_tooltip_play_at_the_take_s_rate(site, monkeypatch):
    monkeypatch.setattr(capture._Screencast, "_on_frame", _painting_at(2.1))
    steps = [
        {"action": "wait", "seconds": 0.4},
        {"action": "hover", "role": "button", "name": "Save"},
        {"action": "wait", "seconds": 0.6},
        {"action": "scroll", "by": 1500, "seconds": 2.0},
    ]
    with tempfile.TemporaryDirectory() as workdir:
        result = await _record(f"{site}/panel", workdir, steps)
        hover = next(event for event in result.events if event["action"] == "hover")
        assert any("div#tip.tooltip" in str(window[2]) for window in hover["motion"]), hover.get("motion")
        paced = scroll_pacing.pace(result.frames, result.events, workdir=workdir, fps=30, pixel_ratio=result.pixel_ratio, painted=result.painted)
        scroll = next(event for event in result.events if event["action"] == "scroll")
        # The log names the panel it scrolled, and both steps play at the take's rate.
        assert scroll["scrolled_element"].startswith("main#panel.content"), scroll["scrolls"]
        assert scroll["fps"] >= 24 and scroll["painted_fps"] < 5, scroll["scrolls"]
        assert hover["fps"] >= 24 and hover["painted_fps"] < 5, hover["motion"]
        # The tooltip fades in over its window: no frame there repeats the one before.
        tip = next(window for window in hover["motion"] if window.get("element", "").startswith("div#tip") and window.get("paced"))
        start, end = tip["t"]
        during = [(t, name) for t, name in paced.frames if start < t <= end]
        assert len(during) >= 0.9 * 30 * (end - start) - 1
        assert _distinct(workdir, during, (slice(20, 60), slice(50, 200))) == []
        rate = scroll_pacing.take_rate(result.events, captured_frames=len(result.frames), duration=result.duration)
        assert not rate.steps and rate.fps >= 24, rate


async def test_a_page_painting_300_ms_an_offset_scrolled_600_px_over_2_5_s_plays_at_30_fps(site):
    with tempfile.TemporaryDirectory() as workdir:
        result = await _record(f"{site}/slow", workdir, [{"action": "wait", "seconds": 0.3}, {"action": "scroll", "by": 600, "seconds": 2.5}])
        paced = scroll_pacing.pace(result.frames, result.events, workdir=workdir, fps=30, pixel_ratio=result.pixel_ratio, painted=result.painted)
        event = next(event for event in result.events if event["action"] == "scroll")
        [scroll] = [s for s in event["scrolls"] if s.get("why") != scroll_pacing.DID_NOT_MOVE]
        moving = [t for t, top in scroll["log"] if 0 < top < scroll["log"][-1][1]]
        start, end = min(moving), max(moving)
        during = [(t, name) for t, name in paced.frames if start <= t <= end]
        assert event["fps"] == 30.0 and len(during) >= 0.9 * 30 * (end - start) - 1, scroll
        assert _distinct(workdir, during, (slice(0, 225), slice(0, 400)), scroll=scroll) == [], scroll
        authored = spec.validate_video_spec(
            {"title": "Slow", "freestyle": True, "scenes": [{"id": "demo", "type": "recording", "take_id": "00000000-0000-4000-8000-000000006432"}]}
        )
        parsed = spec.parse_video_spec(authored)
        take = timeline.TakeMedia(
            "00000000-0000-4000-8000-000000006432", result.duration, 1600, 900, 1.0, 60, round(result.duration * 60),
            events=result.events, captured_frames=len(result.frames),
        )  # fmt: skip
        document = timeline.estimate_plan(parsed, {}, {}).document
        found = quality.content_findings(parsed, document, quality.ContentFacts(takes={take.take_id: take}), authored)
        assert not [f.message for f in found if "scrolls will step" in f.message]


async def test_a_step_missing_a_row_the_page_s_search_term_hides_names_the_term(site):
    with tempfile.TemporaryDirectory() as workdir, pytest.raises(capture.TakeError) as raised:
        await _record(f"{site}/list?searchTerm=alpha", workdir, [{"action": "click", "text": "Row beta 3", "timeout": 1}])
    said = str(raised.value)
    assert 'The page then held its field "Search" "alpha"; its address searchTerm=alpha' in said
    # The page state it missed its row in: the address with its query, and what matched the text.
    assert said.endswith(f"{capture.FILTERED}. The page was at {site}/list?searchTerm=alpha, where nothing matches its text.")
