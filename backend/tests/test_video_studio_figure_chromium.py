"""A figure scene's build-in, drawn for real: the figure page in a real Chromium, the real encoder.

An architecture of 6 nodes, 8 edges and 1 zone, 16 s, built
in sequence - is planned by the render's planner and drawn by the render's
figure stage (``render._draw_figures``), the page being the built frontend's
``/videos/figures/static`` served on loopback. The video it makes is read back
frame by frame:

* ``even``: the nodes drawn at 25 / 50 / 75 % of the scene are about N/4, N/2
  and 3N/4, and the last frame of a figure that holds still (``motion: none``)
  is the whole figure - the editor's still (``figures.draw_still``) is that frame;
* ``follow``: the camera moves to each step and is back on the whole figure
  2 s before the scene ends, then drifts in;
* ``narration``: each step comes in as its words are said, never all at the
  end, and the figure keeps clear of the subtitles' band;
* ``drift`` (the default), the issue's scene - 6 nodes over 17 s, drawn
  as ``render_video_scene`` draws it (the draft preset): about N/4, N/2 and
  3N/4 nodes in at 25 / 50 / 75 %, and the frames at 80 % and 100 % of the
  scene differ (SSIM under 0.98), where a figure that holds still has the
  same frame from its build's end to the scene's.

Skipped where Playwright's Chromium, ffmpeg or the built frontend
(``cd frontend && yarn build``) is missing; the ``Browser tool (Chromium)`` CI
job has all three and sets ``REQUIRE_CHROMIUM=1``, which turns the skip into a
failure.
"""

from __future__ import annotations

import asyncio
import functools
import io
import itertools
import json
import os
import shutil
import threading
import time
import uuid
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import numpy as np
import pytest
from PIL import Image

from app.core.config import settings
from app.services.video_studio import encoder, figures, frame_qc, render, speech_timing, timeline
from app.services.video_studio import lease as frame_lease
from app.services.video_studio.spec import QUALITY_PRESETS, parse_video_spec, validate_video_spec

pytestmark = pytest.mark.chromium

DIST = Path(__file__).resolve().parents[2] / "frontend" / "dist"
LENGTH = 16.0
FPS = 10
VIEWPORT = (1920, 1080)

ARCH = {
    "type": "architecture",
    "nodes": [
        {"id": "hub", "label": "XTM Hub", "sublabel": "Entry point", "icon": "globe", "row": 1, "col": 1, "col_span": 3},
        {"id": "cti", "label": "OpenCTI", "icon": "brain", "tone": "info", "row": 2, "col": 1},
        {"id": "aev", "label": "OpenAEV", "icon": "crosshair", "tone": "warning", "row": 2, "col": 3},
        {"id": "core", "label": "XTM One", "sublabel": "Agentic core", "icon": "sparkles", "shape": "circle", "emphasis": "core", "row": 3, "col": 2},
        {"id": "crq", "label": "OpenCRQ", "icon": "chart-line", "tone": "success", "row": 4, "col": 1},
        {"id": "grc", "label": "OpenGRC", "icon": "shield", "row": 4, "col": 3},
    ],
    "edges": [
        {"source": "hub", "target": "cti", "label": "Onboarding"},
        {"source": "hub", "target": "aev"},
        {"source": "cti", "target": "aev", "label": "Threat data"},
        {"source": "core", "target": "cti", "arrows": "none", "tone": "muted"},
        {"source": "core", "target": "aev", "arrows": "none", "tone": "muted"},
        {"source": "core", "target": "crq", "arrows": "none", "tone": "muted"},
        {"source": "core", "target": "grc", "arrows": "none", "tone": "muted"},
        {"source": "crq", "target": "grc", "label": "Risk"},
    ],
    "zones": [{"label": "XTM Platform", "icon": "layers", "row": 2, "col": 1, "row_span": 3, "col_span": 3}],
    "legend": [{"label": "Data flow", "marker": "arrow"}, {"label": "Runs on the core", "marker": "line", "tone": "muted"}],
}
NARRATION = (
    "Everything starts at the XTM Hub. OpenCTI gathers the threat data, OpenAEV tests the defences, "
    "XTM One runs the agents, OpenCRQ prices the risk and OpenGRC proves compliance."
)

#: The figure page waits for the platform's configuration, like every route of the app: answered here as the platform would.
_PLATFORM_CONFIG = {
    "instance_id": "test",
    "platform_mode": "xtm_one",
    "platform_name": "XTM One",
    "platform_logo": None,
    "platform_logo_hash": None,
    "platform_description": "",
    "platform_login_message": "",
    "platform_language": "en",
    "platform_theme_color": "#0a0f1c",
    "platform_accent_color": None,
    "platform_background_color": None,
    "platform_default_accent_color": "#00bcd4",
    "platform_default_background_color": "#0a0f1c",
    "platform_favicon": None,
    "platform_favicon_hash": None,
    "platform_og_image": "",
    "platform_og_image_hash": None,
    "platform_version": "test",
    "license_required": False,
    "license_valid": True,
    "license_customer": None,
    "license_type": None,
    "license_expiration": None,
    "features": {},
    "integration_status": {"opencti": {"status": "not_registered"}, "openaev": {"status": "not_registered"}},
    "xtm_license": None,
    "agentic_enabled": True,
    "agentic_quotas": {},
    "agentic_source": "none",
    "edition": "enterprise",
    "chat_enabled": True,
    "disable_code_interpreter": False,
    "disable_custom_tools": False,
    "disable_custom_variables": False,
    "ai_catalog_enabled": False,
    "ai_enabled": True,
}


@functools.cache
def _shell() -> bytes:
    from app.services.branding import render_branded_index_html

    return render_branded_index_html((DIST / "index.html").read_text(encoding="utf-8"), base_path="").encode("utf-8")


def _missing(what: str) -> None:
    if os.environ.get("REQUIRE_CHROMIUM") == "1":
        pytest.fail(f"{what} is required here and is missing")
    pytest.skip(f"{what} is missing")


class _Frontend(SimpleHTTPRequestHandler):
    """The built frontend as the platform serves it: its files, the app shell for its routes, the platform's configuration.

    The shell is the platform's own (``branding.render_branded_index_html``):
    the build names its bundles relative to the page, the platform anchors
    them at the root.
    """

    extensions_map = {
        **SimpleHTTPRequestHandler.extensions_map,
        ".js": "text/javascript",
        ".mjs": "text/javascript",
        ".css": "text/css",
        ".svg": "image/svg+xml",
        ".woff2": "font/woff2",
        ".json": "application/json",
        ".wasm": "application/wasm",
    }

    def do_GET(self) -> None:
        path = self.path.split("?", 1)[0]
        if path == "/api/v1/platform/config":
            self._send(json.dumps(_PLATFORM_CONFIG).encode("utf-8"), "application/json")
            return
        if path.startswith("/api/"):
            self.send_error(404)
            return
        if path in ("/", "/index.html") or not Path(self.translate_path(path)).is_file():
            self._send(_shell(), "text/html; charset=utf-8")
            return
        super().do_GET()

    def _send(self, body: bytes, content_type: str) -> None:
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *_args) -> None:
        return None


class _Server(ThreadingHTTPServer):
    # The app warms every lazy chunk at once: a few hundred requests the default backlog of 5 refuses.
    request_queue_size = 256
    daemon_threads = True


@pytest.fixture(scope="module")
def figure_page():
    if not (DIST / "index.html").is_file():
        _missing("The built frontend (cd frontend && yarn build)")
    if not shutil.which("ffmpeg"):
        _missing("ffmpeg")
    server = _Server(("127.0.0.1", 0), partial(_Frontend, directory=str(DIST)))
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}"
    finally:
        server.shutdown()
        server.server_close()


@pytest.fixture
def served(figure_page, monkeypatch):
    monkeypatch.setattr(settings, "presentation_render_base_url", figure_page)
    monkeypatch.setattr(settings, "base_path", "")


def _browser_missing(exc: Exception) -> None:
    text = str(exc)
    if "Executable doesn't exist" in text or "playwright install" in text.lower():
        _missing(f"Playwright's Chromium (python -m playwright install chromium): {text[:200]}")
    raise exc


def _spec(**scene):
    raw = {
        "title": "Figure",
        "format": {"aspect_ratio": "16:9"},
        "scenes": [{"id": "s05", "type": "figure", "block_spec": ARCH, "duration": LENGTH, "build": "sequence", **scene}],
    }
    return parse_video_spec(validate_video_spec(raw))


def _transform(text: str) -> tuple[float, float, float]:
    """``(x, y, scale)`` of the slide canvas's ``translate(Xpx, Ypx) scale(S)``."""
    import re

    x, y, scale = (float(v) for v in re.findall(r"-?\d+(?:\.\d+)?(?:e-?\d+)?", text))
    return x, y, scale


def _source(spec):
    scene = spec.scenes[0]
    source = figures.source_from_spec(scene)
    return source, figures.figure_document(source, scheme=spec.theme.mode, palette={})


async def _drawn(spec, workdir: Path, fps: int = FPS) -> tuple[Path, float, timeline.TimelinePlan]:
    """The scene planned and drawn by the render's figure stage: the video it made, the scene's length, the plan."""
    media = render._Media()
    media.figures["s05"] = _source(spec)
    plan = timeline.plan(spec, media.plan, fps=fps, theme={})

    async def progress(*_args, **_kwargs) -> None:
        return None

    try:
        await render._draw_figures(spec, plan, media, user=None, fps=fps, scale=1, workdir=workdir, progress=progress)
    except RuntimeError as exc:
        _browser_missing(exc)
    placed = next(scene for scene in plan.document["scenes"] if scene["id"] == "s05")
    return Path(media.assets.sources["clip/s05"].path), float(placed["end"]) - float(placed["start"]), plan


async def _on_page(spec, probe):
    """*probe* run on the figure page of *spec*'s scene, loaded as the render loads it."""
    from app.services.presentation_export import open_render_page

    source, document = _source(spec)
    try:
        async with open_render_page(
            figures.figure_page_url(),
            width=VIEWPORT[0],
            height=VIEWPORT[1],
            signal="figure",
            error=figures.FigureError,
            answer=figures._answerer(source, document, None),
        ) as page:
            return await probe(page)
    except figures.FigureError as exc:
        _browser_missing(exc)


async def _camera(page, t: float) -> str:
    await page.evaluate("(t) => window.__xtmFigure.seek(t)", t)
    return await page.evaluate("() => document.querySelector('.deck-static-slide').style.transform")


def _picture(video: Path, t: float) -> Image.Image:
    """The frame of *video* at *t* seconds."""
    out = video.with_name(f"at-{t:06.2f}.png")
    asyncio.run(encoder.ffmpeg(["-ss", f"{t:.3f}", "-i", str(video), "-frames:v", "1", str(out)]))
    return Image.open(out)


def _frame(video: Path, t: float) -> np.ndarray:
    """The frame of *video* at *t* seconds, in grey levels."""
    return np.asarray(_picture(video, t).convert("L"), dtype=np.float32)


def _nodes_shown(frame: np.ndarray, absent: np.ndarray, present: np.ndarray, boxes: list[dict]) -> int:
    """How many nodes *frame* draws: those whose middle looks like the finished figure's more than the empty frame's."""
    shown = 0
    for box in boxes:
        x0, x1 = int(box["x"] + box["width"] * 0.2), int(box["x"] + box["width"] * 0.8)
        y0, y1 = int(box["y"] + box["height"] * 0.2), int(box["y"] + box["height"] * 0.8)
        crop = frame[y0:y1, x0:x1]
        if np.abs(crop - present[y0:y1, x0:x1]).mean() < np.abs(crop - absent[y0:y1, x0:x1]).mean():
            shown += 1
    return shown


def test_an_even_build_shows_a_quarter_a_half_then_three_quarters_of_its_nodes(served, tmp_path):
    spec = _spec(reveal="even", camera="still", motion="none")
    video, length, _plan = asyncio.run(_drawn(spec, tmp_path))
    assert length == pytest.approx(LENGTH)

    async def probe(page):
        described = await page.evaluate("() => window.__xtmFigure.describe()")
        plan = figures._plan_of(described, scene=spec.scenes[0], length=length, words=None, safe=dict(figures.NO_SAFE_AREA))
        await page.evaluate("(plan) => window.__xtmFigure.plan(plan)", plan)
        await page.evaluate("(t) => window.__xtmFigure.seek(t)", length)
        boxes = await page.evaluate(
            "() => [...document.querySelectorAll('[data-figure-root] .deck-arch-node')].map((n) => n.getBoundingClientRect().toJSON())"
        )
        return described, boxes

    described, boxes = asyncio.run(_on_page(spec, probe))
    nodes = len(boxes)
    absent, present = _frame(video, 0.0), _frame(video, length - 2 / FPS)
    shown = {share: _nodes_shown(_frame(video, share * length), absent, present, boxes) for share in (0.25, 0.5, 0.75)}
    for share, count in shown.items():
        assert abs(count - nodes * share) <= 1, f"{count} of {nodes} nodes drawn at {share:.0%} of the scene: {shown}"
    assert shown[0.25] < shown[0.5] < shown[0.75] < _nodes_shown(present, absent, present, boxes) == nodes
    # The zone first, then every node on its own, named by its own lines of text.
    assert len(described["steps"]) == 1 + len(ARCH["nodes"]) == 7
    assert described["steps"][0]["labels"] == ["XTM Platform"] and described["steps"][1]["labels"] == ["XTM Hub", "Entry point"]

    still = asyncio.run(figures.draw_still(*_source(spec), user=None, viewport=VIEWPORT))
    still_frame = np.asarray(Image.open(io.BytesIO(still)).convert("L"), dtype=np.float32)
    assert np.abs(still_frame - present).mean() < 4, "the editor's still is the scene's last frame"


def test_the_camera_moves_to_each_step_and_ends_on_the_whole_figure(served):
    spec = _spec(reveal="even", camera="follow")
    scene = spec.scenes[0]

    async def probe(page):
        described = await page.evaluate("() => window.__xtmFigure.describe()")
        plan = figures._plan_of(described, scene=scene, length=LENGTH, words=None, safe=dict(figures.NO_SAFE_AREA))
        await page.evaluate("(plan) => window.__xtmFigure.plan(plan)", plan)
        framed = [await _camera(page, t + 0.5) for t in plan["times"]]
        leaving = [await _camera(page, t - 0.25) for t in plan["times"][1:]]
        ends = [await _camera(page, t) for t in (LENGTH - figures.WHOLE_HOLD, LENGTH - 0.01)]
        await page.evaluate("(plan) => window.__xtmFigure.plan(plan)", {**plan, "camera": "still"})
        return framed, leaving, ends, await _camera(page, 1.0)

    framed, leaving, ends, whole = asyncio.run(_on_page(spec, probe))
    assert len(set(framed)) == len(framed), "each step is framed as it comes in"
    # A move takes 0.6 s: just before a step comes in, the camera is still on the one before.
    assert leaving == framed[:-1]
    assert ends[0] == whole, "the camera is back on the whole figure 2 s before the scene ends"
    assert all(frame != whole for frame in framed[1:])
    # Then it drifts in on the whole figure: 1.06 times as close by the end, about the same middle.
    (x0, y0, s0), (x1, y1, s1) = _transform(whole), _transform(ends[1])
    assert s1 / s0 == pytest.approx(1.06, abs=0.002)
    middle = (VIEWPORT[0] / 2, VIEWPORT[1] / 2)
    assert (middle[0] - x1) / s1 == pytest.approx((middle[0] - x0) / s0, abs=0.5)
    assert (middle[1] - y1) / s1 == pytest.approx((middle[1] - y0) / s0, abs=0.5)


#: 17 s, drawn as a scene preview draws it (``render_video_scene``: the draft preset).
DRIFT_LENGTH = 17.0
DRAFT_FPS = QUALITY_PRESETS["draft"].fps
_VISIBLE_NODES = (
    "() => [...document.querySelectorAll('[data-figure-root] .deck-arch-node')]"
    ".filter((node) => Number(getComputedStyle(node).opacity) > 0.5).length"
)


def test_a_built_figure_keeps_moving_to_the_end_of_its_scene(served, tmp_path):
    """The figure is never a still - it builds in a node at a time, then drifts, pulses and breathes to the end."""
    spec = _spec(duration=DRIFT_LENGTH)
    scene = spec.scenes[0]
    assert (scene.reveal, scene.camera, scene.motion) == ("even", "follow", "drift")
    video, length, _plan = asyncio.run(_drawn(spec, tmp_path / "drift", fps=DRAFT_FPS))
    assert length == pytest.approx(DRIFT_LENGTH)

    async def probe(page):
        described = await page.evaluate("() => window.__xtmFigure.describe()")
        plan = figures._plan_of(described, scene=scene, length=length, words=None, safe=dict(figures.NO_SAFE_AREA))
        planned = await page.evaluate("(plan) => window.__xtmFigure.plan(plan)", plan)
        shown = {}
        for share in (0.25, 0.5, 0.75, 1.0):
            await page.evaluate("(t) => window.__xtmFigure.seek(t)", share * length - 0.01)
            shown[share] = await page.evaluate(_VISIBLE_NODES)
        return planned, shown

    planned, shown = asyncio.run(_on_page(spec, probe))
    nodes = len(ARCH["nodes"])
    for share in (0.25, 0.5, 0.75):
        assert abs(shown[share] - nodes * share) <= 1, f"{shown[share]} of {nodes} nodes in at {share:.0%} of the scene: {shown}"
    assert shown[0.25] < shown[0.5] < shown[0.75] < shown[1.0] == nodes
    # Something moves to the very end: the render screenshots every frame from the first pulse on.
    assert planned["active"][-1][1] == pytest.approx(length)
    assert planned["whole"] <= length - figures.WHOLE_HOLD + 1e-6

    # The frames at 80 % and 100 % of the scene are not the same picture.
    late, last = _picture(video, 0.8 * length), _picture(video, length - 1 / DRAFT_FPS)
    assert frame_qc.ssim(late, last) < 0.98
    # After its build, a figure that holds still shows one frame to the end; a drifting one never does.
    still_spec = _spec(duration=DRIFT_LENGTH, motion="none")
    held, _length, _plan = asyncio.run(_drawn(still_spec, tmp_path / "held", fps=DRAFT_FPS))
    built = planned["whole"] + 0.2
    assert frame_qc.ssim(_picture(held, built), _picture(held, length - 1 / DRAFT_FPS)) > 0.99
    assert frame_qc.ssim(_picture(video, built), last) < 0.98


#: The scene of render 14d6bcbd, 25 s drifting to its end, under a lease the drawing outlasts many times.
LEASE_LENGTH = 25.0
LEASE_SECONDS = 3.0


def test_a_figure_drifting_for_25_s_keeps_its_lease_from_its_frames(served, tmp_path, monkeypatch):
    """Drawn for real with no worker heartbeat: its frames keep it moving, and its keeper's thread renews the lease,
    never more than a lease apart."""
    # The encode reports it is still working well inside the stall window, as it does under a real one.
    monkeypatch.setattr(encoder, "WAIT_REPORT_SECONDS", LEASE_SECONDS / 3)
    renewed: list[float] = []
    stamps: list[float] = []

    async def extend() -> None:
        renewed.append(time.monotonic())

    spec = _spec(duration=LEASE_LENGTH)
    assert spec.scenes[0].motion == "drift"
    keeper = frame_lease.FrameLease(
        uuid.uuid4(),
        "worker-1",
        interval=frame_lease.renewal_interval(LEASE_SECONDS),
        lease_seconds=int(LEASE_SECONDS),
        window=LEASE_SECONDS,
    )
    # The renewal recorded, never sent to a database: the thread calls it on its own loop.
    keeper._extend = extend
    keeper.start()

    def on_frame() -> None:
        stamps.append(time.monotonic())
        keeper.frame_drawn()

    async def drawn() -> Path:
        media = render._Media()
        media.figures["s05"] = _source(spec)
        plan = timeline.plan(spec, media.plan, fps=FPS, theme={})

        async def progress(*_args, **_kwargs) -> None:
            return None

        try:
            await render._draw_figures(
                spec, plan, media, user=None, fps=FPS, scale=1, workdir=tmp_path, progress=progress, on_frame=on_frame
            )
        except RuntimeError as exc:
            _browser_missing(exc)
        await keeper.close()
        return Path(media.assets.sources["clip/s05"].path)

    video = asyncio.run(drawn())
    # Screenshot where something moves - drifting, nearly everywhere - and held between: the whole scene is drawn.
    frames = round(LEASE_LENGTH * FPS)
    assert len(stamps) > frames // 2 and abs(asyncio.run(encoder.probe(str(video))).frame_count - frames) <= 1
    if stamps[-1] - stamps[0] < 2 * LEASE_SECONDS:
        pytest.skip(f"the figure was drawn in {stamps[-1] - stamps[0]:.1f} s: too fast to outlast its lease")
    # From its first frame on: before it, Chromium is still starting and nothing is drawn.
    held = sorted([stamps[0], *(at for at in renewed if at >= stamps[0]), stamps[-1]])
    assert keeper.lost is None and keeper.failures == 0 and len(renewed) >= 2
    assert max(b - a for a, b in itertools.pairwise(held)) < LEASE_SECONDS


def test_a_narrated_build_follows_the_words_and_keeps_clear_of_the_subtitles(served, tmp_path):
    spec = _spec(reveal="narration", camera="still", narration=NARRATION)
    video, length, plan = asyncio.run(_drawn(spec, tmp_path))
    said_from, said_to = plan.narration_spans["s05"]
    placed = next(scene for scene in plan.document["scenes"] if scene["id"] == "s05")
    words = asyncio.run(
        speech_timing.word_times(NARRATION, start=said_from - float(placed["start"]), seconds=said_to - said_from)
    )
    safe = figures.caption_safe_area(spec, spec.scenes[0], VIEWPORT[1])
    # The caption pill's top at the bottom, the safe margin at the top.
    assert safe["bottom"] > 150 and safe["top"] == round(VIEWPORT[1] * figures.SAFE_MARGIN, 1)

    async def probe(page):
        described = await page.evaluate("() => window.__xtmFigure.describe()")
        return figures._plan_of(described, scene=spec.scenes[0], length=length, words=words, safe=safe)

    times = asyncio.run(_on_page(spec, probe))["times"]
    said = {word.text: word.start for word in words}
    for step, label in ((2, "opencti"), (3, "openaev"), (5, "opencrq"), (6, "opengrc")):
        assert times[step] == pytest.approx(said[label] - figures.NARRATION_LEAD, abs=0.01)
    assert times == sorted(times) and times[-1] <= length - figures.WHOLE_HOLD
    assert times[-1] - times[1] > length / 3, "the steps are spread, never all at the end"

    # Nothing of the figure is drawn in the subtitles' band: it looks the same in the first frame and the last.
    absent, present = _frame(video, 0.0), _frame(video, length - 2 / FPS)
    band = int(VIEWPORT[1] - safe["bottom"])
    assert np.abs(present[band:] - absent[band:]).mean() < 1.0
    assert np.abs(present[:band] - absent[:band]).mean() > 5.0
