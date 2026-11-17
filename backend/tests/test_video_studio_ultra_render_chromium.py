"""A 30 s mixed video rendered at ultra - 3840x2160 at 60 frames a second - for real, under a 60 s lease.

ultra (PNG frames, x264 ``slow``, crf 15) had never been attempted when the
brand film of 20 Oct 2026 was produced. This draws what a render draws, with
the render's own stages and nothing faked but the database:

* a figure scene - the brand film's architecture - drawn by the render's
  figure stage (``render._draw_figures``) on the built frontend's figure page,
  at the device scale of the output (2);
* a recording with a screen insert mapped onto it, and a statement over a
  clip, decoded and composed by ``compositor.render_frames`` on two pages,
  each page piping its frames into its encoder;
* the frozen-picture check (``frame_freeze.check_frames``) on the MP4.

The render's lease is renewed from its own thread while every frame drawn,
followed or watched and every report says the render moves
(``lease.FrameLease``), against a lease of 60 s that no worker
heartbeat renews, and the supervisor's stall window
(``VIDEO_STUDIO_RENDER_STALL_SECONDS``) is held to every report the stages
make. The pre-flight ``render_video(quality=ultra)`` says
(``render_preflight``) is checked against what the render measured: its memory
peak within 1.5 times what it said either way, and the time it took
within :func:`time_band`: never more than 1.5 times what it said - a
pre-flight that under-promises fails -, and never under a third of it. The
low side is wider on purpose: until this platform measured a render the
pre-flight says the slowest CI run on record (``DEFAULT_PAGE_RATE``), and a
faster runner (591 to 614 s against 1135 s said on 31 Oct 2026) is not a
defect; a third is the runner's 1.5 times its variation over the 0.5 the
platform's own correction goes down to (``CORRECTION_BOUNDS``), so a model
more pessimistic than its correction can absorb still fails.

Minutes on a workstation, tens of minutes on a 2 vCPU runner: it runs only
with ``VIDEO_STUDIO_ULTRA_RENDER=1`` (the ``Video Studio ultra render``
workflow, nightly and on demand), and needs ffmpeg, Playwright's Chromium and
the built frontend (``cd frontend && yarn build``) - a failure, not a skip,
under ``REQUIRE_CHROMIUM=1``.
"""

from __future__ import annotations

import itertools
import logging
import os
import shutil
import subprocess
import threading
import time
import uuid
from functools import partial
from pathlib import Path

import pytest

from app.core.config import settings
from app.services.builtin_tools.html_render import font_face_css
from app.services.video_studio import (
    compositor,
    encoder,
    figures,
    frame_freeze,
    render,
    render_preflight,
    resources,
    themes,
    timeline,
)
from app.services.video_studio import lease as frame_lease
from app.services.video_studio.screen_insert import InsertSource
from app.services.video_studio.spec import QUALITY_PRESETS, output_size, parse_video_spec, validate_video_spec

pytestmark = [pytest.mark.chromium, pytest.mark.video_ultra]

DIST = Path(__file__).resolve().parents[2] / "frontend" / "dist"
LEASE_SECONDS = 60
TAKE_ID = "00000000-0000-0000-0000-0000000061d1"
INSERT_FILE = "00000000-0000-0000-0000-0000000061d2"
BACKDROP_FILE = "00000000-0000-0000-0000-0000000061d3"
#: A monitor on the recording's desk, seen a little from the left.
QUAD = [[0.30, 0.20], [0.80, 0.25], [0.78, 0.75], [0.32, 0.70]]
_MIB = 1 << 20
#: How far the render's time may stray from what the pre-flight said (:func:`time_band`): a runner's variation
#: on the slow side, that variation over the correction's floor on the fast side.
RUNNER_VARIATION = 1.5
FASTEST_SHARE = render_preflight.CORRECTION_BOUNDS[0] / RUNNER_VARIATION


def time_band(said_seconds: float) -> tuple[float, float]:
    """The seconds a render said to take *said_seconds* may take: at most :data:`RUNNER_VARIATION` times it,
    at least :data:`FASTEST_SHARE` of it - a faster runner passes, an estimate the correction cannot bring back fails."""
    return said_seconds * FASTEST_SHARE, said_seconds * RUNNER_VARIATION


@pytest.fixture(autouse=True)
def _asked():
    if os.environ.get("VIDEO_STUDIO_ULTRA_RENDER") != "1":
        pytest.skip("a 2160p60 render takes minutes: VIDEO_STUDIO_ULTRA_RENDER=1 runs it")


def _missing(what: str) -> None:
    if os.environ.get("REQUIRE_CHROMIUM") == "1":
        pytest.fail(f"{what} is required here and is missing")
    pytest.skip(f"{what} is missing")


@pytest.fixture(scope="module")
def figure_page():
    from tests.test_video_studio_figure_chromium import _Frontend, _Server

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


def _clip(path: Path, *, size: str, rate: int, seconds: float, source: str = "testsrc2") -> None:
    subprocess.run(  # noqa: S603
        [
            shutil.which("ffmpeg") or "ffmpeg",
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-f",
            "lavfi",
            "-i",
            f"{source}=size={size}:rate={rate}",
            "-t",
            str(seconds),
            "-c:v",
            "libx264",
            "-preset",
            "ultrafast",
            "-pix_fmt",
            "yuv420p",
            str(path),
        ],
        check=True,
        timeout=300,
    )


def _spec():
    from tests.test_video_studio_figure_chromium import ARCH

    raw = {
        "title": "Ultra",
        "freestyle": True,
        "format": {"aspect_ratio": "16:9"},
        "captions": {"enabled": False},
        "default_transition": {"type": "crossfade", "duration": 0.6},
        "scenes": [
            {"id": "s01-platform", "type": "figure", "block_spec": ARCH, "duration": 10, "build": "sequence"},
            {
                "id": "s02-console",
                "type": "recording",
                "take_id": TAKE_ID,
                "overlays": [
                    {"type": "screen_insert", "source": {"file_id": INSERT_FILE}, "quad": QUAD, "start": 1.0, "duration": 9}
                ],
            },
            {
                "id": "s03-claim",
                "type": "statement",
                "lines": ["Every alert", "*triaged* first"],
                "duration": 9,
                "backdrop": {"file_id": BACKDROP_FILE},
            },
        ],
    }
    return parse_video_spec(validate_video_spec(raw))


class _Heard:
    """When the render was last heard from: each report a stage makes, as the supervisor's stall window reads them."""

    def __init__(self) -> None:
        self.at = [time.monotonic()]
        self.what = ["start"]

    def said(self, what: str) -> None:
        self.at.append(time.monotonic())
        self.what.append(what)

    def longest_silence(self) -> tuple[float, str]:
        gaps = [(b - a, f"before {what}") for (a, b), what in zip(itertools.pairwise(self.at), self.what[1:], strict=True)]
        return max(gaps)


class _Lease(frame_lease.FrameLease):
    """The render's lease, renewed from its thread while the render moves: each renewal timed, none sent anywhere."""

    def __init__(self) -> None:
        super().__init__(
            uuid.uuid4(), "worker-1", interval=frame_lease.renewal_interval(LEASE_SECONDS), lease_seconds=LEASE_SECONDS
        )
        self.renewed_at = [time.monotonic()]

    async def _extend(self) -> None:
        self.renewed_at.append(time.monotonic())


class _Memory(threading.Thread):
    """The peak memory of this process and everything it started, every half second (``None`` without ``/proc``).

    At the peak, what each kind of process held (``/proc/<pid>/comm``) and the stage the render was in: which part
    of ``render_preflight.memory_needed`` a miss is in.
    """

    def __init__(self, heard: _Heard) -> None:
        super().__init__(daemon=True)
        self.heard = heard
        self.peak: int | None = None
        self.at_peak = ""
        self.done = threading.Event()

    def run(self) -> None:
        while not self.done.wait(0.5):
            held: dict[str, int] = {}
            for pid in resources.process_tree(os.getpid()):
                memory = resources.tree_memory(pid, pids=[pid])
                if memory is not None:
                    held[_command(pid)] = held.get(_command(pid), 0) + memory
            if held and sum(held.values()) > (self.peak or 0):
                self.peak = sum(held.values())
                parts = ", ".join(f"{name} {resources.format_bytes(n)}" for name, n in sorted(held.items(), key=lambda i: -i[1]))
                self.at_peak = f"{parts}; after {self.heard.what[-1]}"


def _command(pid: int) -> str:
    try:
        return Path("/proc", str(pid), "comm").read_text().strip() or "?"
    except OSError:
        return "?"


async def test_a_30_second_mixed_video_renders_at_ultra_under_a_60_second_lease(figure_page, monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "presentation_render_base_url", figure_page)
    monkeypatch.setattr(settings, "base_path", "")
    take, insert, backdrop = tmp_path / "take.mp4", tmp_path / "insert.mp4", tmp_path / "backdrop.mp4"
    _clip(take, size="1600x900", rate=30, seconds=12)
    _clip(insert, size="1280x720", rate=24, seconds=10, source="mandelbrot")
    _clip(backdrop, size="1920x1080", rate=24, seconds=10)
    spec = _spec()
    preset = QUALITY_PRESETS["ultra"]
    fps, size = preset.fps, output_size(spec.format.aspect_ratio, preset.short_side)
    assert (fps, size) == (60, (3840, 2160))

    media = render._Media()
    source = figures.source_from_spec(spec.scenes[0])
    media.figures["s01-platform"] = (source, figures.figure_document(source, scheme=spec.theme.mode, palette={}))
    media.plan.takes[TAKE_ID] = timeline.TakeMedia(
        take_id=TAKE_ID, duration=12.0, css_width=1600, css_height=900, pixel_ratio=1.0, fps=30, frame_count=360
    )
    media.assets.sources["take/s02-console"] = compositor.FrameSource(str(take), 30.0)
    key = timeline.insert_key("s02-console", 0)
    media.plan.inserts_gathered = True
    media.plan.inserts[key] = timeline.ClipMedia(10.0, 1280, 720, 24.0, 240)
    media.assets.inserts[key] = InsertSource(str(insert), "video", tuple((x, y) for x, y in QUAD), fps=24.0)
    media.plan.backdrops["s03-claim"] = timeline.ClipMedia(10.0, 1920, 1080, 24.0, 240)
    media.assets.sources["backdrop/s03-claim"] = compositor.FrameSource(str(backdrop), 24.0)
    theme = themes.build_theme(mode=spec.theme.mode, background_style=spec.theme.background)
    plan = timeline.plan(spec, media.plan, fps=fps, theme=theme)
    assert 28.0 <= plan.duration <= 32.0, plan.duration
    frames = int(plan.document["frames"])
    # What the pre-flight says before the render starts, from the plan alone.
    said = render_preflight.estimate(frames=frames, size=size, figure_seconds=10.0, pages=2, cpus=resources.cpu_budget())

    lease, heard = _Lease(), _Heard()
    memory = _Memory(heard)
    lease.start()

    # Every report says the render moves, as the render process's ``report`` does.
    async def progress(_value, stage, *, drawing=None):
        lease.moved()
        heard.said(stage)

    async def frame_progress(percent):
        lease.moved()
        heard.said(f"frames {percent}%")

    def reported(event):
        lease.moved()
        heard.said(event["type"])

    # What a render's stages call, as run_render wires it: the render moves, a heartbeat for the stall window.
    frame_done = render.frame_reporter(lease.frame_drawn, reported)

    memory.start()
    started = time.monotonic()
    try:
        try:
            await render._draw_figures(
                spec, plan, media, user=None, fps=fps, scale=2, workdir=tmp_path, progress=progress, on_frame=frame_done
            )
        except RuntimeError as exc:
            if "Executable doesn't exist" in str(exc) or "playwright install" in str(exc).lower():
                _missing(f"Playwright's Chromium: {str(exc)[:200]}")
            raise
        heard.said("figures finished")
        figured = time.monotonic() - started
        plan = timeline.plan(spec, media.plan, fps=fps, theme=theme)
        video = await compositor.render_frames(
            plan.document,
            media.assets,
            preset=preset,
            output_size=size,
            workdir=str(tmp_path),
            font_css=font_face_css(),
            pages=2,
            progress=frame_progress,
            on_frame=frame_done,
        )
        heard.said("frames finished")
        drawn = time.monotonic() - started - figured
        checked = await frame_freeze.check_frames(
            video, document=plan.document, width=size[0], height=size[1], fps=fps, on_frame=frame_done
        )
        heard.said("checks finished")
        await lease.close()
    finally:
        memory.done.set()
        memory.join()
    took = time.monotonic() - started
    lease.renewed_at.append(time.monotonic())

    info = await encoder.probe(video)
    gaps = [(b - a, a - started) for a, b in itertools.pairwise(lease.renewed_at)]
    longest, since = max(gaps)
    silence, where = heard.longest_silence()
    report = (
        f"ultra: {frames} frames in {took:.0f} s (figure {figured:.0f} s, frames {drawn:.0f} s), "
        f"{frames / max(drawn, 1e-6):.2f} frames/s, longest lease gap {longest:.0f} s ({since:.0f} s in), longest "
        f"silence {silence:.0f} s ({where}), memory peak {resources.format_bytes(memory.peak)} ({memory.at_peak}); "
        f"the pre-flight said {said.minutes} min ({said.seconds:.0f} s) and {resources.format_bytes(said.memory_bytes)}"
    )
    logging.getLogger(__name__).warning("%s", report)
    assert (info.width, info.height) == size and info.fps == pytest.approx(60, abs=0.5)
    assert info.duration == pytest.approx(plan.duration, abs=0.2) and checked.summary["frames"] > 0
    # The lease lived on the render's own work alone: never 60 s without a renewal, never lost.
    assert lease.lost is None and longest < LEASE_SECONDS, report
    # The stall window never saw the render stand still.
    assert silence < settings.video_studio_render_stall_seconds, report
    # What the pre-flight quotes the user and plans the render's ranges on: on the 2 vCPU runner the renders took 997
    # to 1058 s against 19 min said (20 Oct 2026), 591 to 614 s on 31 Oct 2026. Slower than 1.5 times fails; faster
    # passes down to a third (:func:`time_band`).
    fastest, slowest = time_band(said.seconds)
    assert fastest <= took <= slowest, report
    if memory.peak is not None:
        # What the pre-flight compares with the worker's ceiling: too low, a render it lets start is stopped at the
        # ceiling; too high, it refuses one that fits.
        assert said.memory_bytes / 1.5 <= memory.peak <= said.memory_bytes * 1.5, report
