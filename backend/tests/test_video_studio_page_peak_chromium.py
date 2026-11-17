"""One page drawing 2160p frames: its peak measured, and the budget a render's pages are laid out by held to it.

A 4K render of 21 Oct 2026 drew on 3 of its worker's 8 pages for 2.5 times as long: the pre-flight held each page to
a per-pixel model of 3.7 GiB, never measured. Here one page draws the 60 s mixed video of
``video_studio_render_fixture`` - a title, a recording with a video insert, a green screen with a keyed insert, a
statement over a clip, a picture, a clip with a video and a picture insert, a closing card - at 3840x2160 through
``compositor.render_frames``, every insert matted as a render mattes it, while the render's process tree is sampled
four times a second, each process by its proportional share as the render supervisor counts it
(``resources.tree_memory``):

* the page's peak - its Chromium renderer, its encoder and the decoders of what it shows, and what its frame server
  and the Playwright driver hold for it (``video_studio_memory.TreeSampler``) - with a screen insert on screen and
  with none;
* the measured peak the layout reads (``render_preflight.PAGE_PEAK_FILE``) must hold it within its margin
  (``render_preflight.PAGE_MARGIN``, 20 %), both ways: a page past its budget would be stopped at the ceiling, a
  budget far above what a page holds would shut pages the worker could open. A failure logs the record to commit.

The page draws 10 frames a second of video (600 frames) with ultra's encoder - x264 ``slow``, crf 15, PNG frames,
a keyframe every 120 frames, which caps x264's lookahead at its 50 frames - so it holds what a page of an ultra
render holds, in the minutes a runner has: what one page holds depends on what a frame shows and on the encoder's
settings, not on how many frames a second of video it draws. Its encoder runs on 8 threads, a page alone on an
8-CPU worker (``render_preflight.encoder_threads``).

Tens of minutes on a 2 vCPU runner: it runs only with ``VIDEO_STUDIO_ULTRA_RENDER=1`` (the ``Video Studio ultra
render`` workflow), and needs ffmpeg and Playwright's Chromium - a failure, not a skip, under ``REQUIRE_CHROMIUM=1``.
"""

from __future__ import annotations

import json
import logging
import os
import time

import pytest

from app.services.builtin_tools.html_render import font_face_css
from app.services.video_studio import compositor, encoder, render_preflight, render_process, resources
from app.services.video_studio.spec import QUALITY_PRESETS
from tests import video_studio_render_fixture as fixture
from tests.video_studio_memory import TreeSampler

pytestmark = [pytest.mark.chromium, pytest.mark.video_ultra]

#: Frames a second of the fixture the page draws: 600 frames of its 60 s.
DRAWN_FPS = 10
#: The keyframe interval of an ultra render (``encoder.x264_args``: two seconds at 60 fps).
ULTRA_KEYFRAMES = 120
#: The CPUs of the worker the page is measured on: alone there, its encoder on every one of them.
WORKER_CPUS = 8
SIZE = (3840, 2160)


@pytest.fixture(autouse=True)
def _asked():
    if os.environ.get("VIDEO_STUDIO_ULTRA_RENDER") != "1":
        pytest.skip("a page drawing 2160p takes minutes: VIDEO_STUDIO_ULTRA_RENDER=1 runs it")


def _missing(what: str) -> None:
    if os.environ.get("REQUIRE_CHROMIUM") == "1":
        pytest.fail(f"{what} is required here and is missing")
    pytest.skip(f"{what} is missing")


def _ultra_keyframes(args: list[str]) -> list[str]:
    """*args* of ``encoder.x264_args`` with ultra's keyframe interval, whatever frame rate the page draws at."""
    keyed = list(args)
    keyed[keyed.index("-g") + 1] = str(ULTRA_KEYFRAMES)
    return keyed


async def test_one_page_drawing_2160p_peaks_within_the_budget_measured_for_it(tmp_path, monkeypatch):
    if not fixture.ffmpeg():
        _missing("ffmpeg")
    if not os.path.isdir("/proc"):
        pytest.skip("a process tree's memory is read from /proc")
    # Measured in the environment a render process runs in (``render_process.process_env``): its glibc heaps.
    for name, value in render_process.RENDER_ENVIRONMENT.items():
        assert os.environ.get(name) == value, f"run it with {name}={value}, as a render process runs"
    monkeypatch.setattr(resources, "cpu_budget", lambda: WORKER_CPUS)
    x264 = encoder.x264_args
    monkeypatch.setattr(encoder, "x264_args", lambda **kwargs: _ultra_keyframes(x264(**kwargs)))
    # A segment of 10 s, as an ultra render of a minute closes them: never one the fixture's length would shorten.
    monkeypatch.setattr(compositor, "CHECKPOINT_SHARE", 1.0)
    made = fixture.build(tmp_path, seconds=60, fps=DRAWN_FPS)
    await fixture.matte(made, SIZE)
    document = made.plan.document
    assert 58.0 <= float(document["duration"]) <= 62.0, document["duration"]
    load = render_preflight.InsertLoad.of(document)
    kinds = render_preflight.FrameKinds.of(document)
    tracker = compositor.DrawProgress()

    def phase() -> str | None:
        try:
            drawing = list(tracker.positions.values())
        except RuntimeError:  # the page moved on while it was read
            return None
        if not drawing:
            return None
        return "insert" if "insert" in kinds.kind(drawing[0]) else "plain"

    sampler = TreeSampler(phase=phase)
    sampler.start()
    started = time.monotonic()
    try:
        try:
            await compositor.render_frames(
                document,
                made.media.assets,
                preset=QUALITY_PRESETS["ultra"],
                output_size=SIZE,
                workdir=str(tmp_path),
                font_css=font_face_css(),
                pages=1,
                tracker=tracker,
            )
        except Exception as exc:
            if "Executable doesn't exist" in str(exc) or "playwright install" in str(exc).lower():
                _missing(f"Playwright's Chromium: {str(exc)[:200]}")
            raise
    finally:
        sampler.stop()
    took = time.monotonic() - started

    peak, plain = sampler.page_peak, sampler.peaks.get("plain")
    record = {
        "size": list(SIZE),
        "x264_preset": "slow",
        "frame_format": "png",
        "threads": tracker.encoder_threads,
        "inserts": {"videos": load.videos, "pictures": load.pictures},
        "peak_bytes": peak,
        "plain_bytes": plain,
    }
    measured = render_preflight.page_peak(SIZE, "slow")
    report = (
        f"one page at 3840x2160: {int(document['frames'])} frames in {took:.0f} s on {tracker.encoder_threads} encoder "
        f"threads, {sampler.said()}; per kind {json.dumps(tracker.kinds())}; the record a re-measure commits to "
        f"{render_preflight.PAGE_PEAK_FILE.name}: {json.dumps(record)}"
    )
    logging.getLogger(__name__).warning("%s", report)
    assert tracker.encoder_threads == WORKER_CPUS and set(tracker.kind_frames) >= {"plain", "transition", "insert"}, report
    assert peak and plain and sampler.samples > 100, report
    assert measured is not None and measured.threads == WORKER_CPUS, f"no 2160p page peak in {render_preflight.PAGE_PEAK_FILE}"
    assert (measured.inserts.videos, measured.inserts.pictures) == (load.videos, load.pictures), report
    margin = 1 + render_preflight.PAGE_MARGIN
    # The budget the layout holds a page to holds what this page held, with or without an insert on screen ...
    assert peak <= render_preflight.page_budget(SIZE, "slow", load, threads=WORKER_CPUS), report
    assert plain <= render_preflight.page_budget(SIZE, "slow", None, threads=WORKER_CPUS), report
    # ... and the peak it is set from is this measurement's, within the same margin: never one so high it shuts
    # pages a worker could open, the 37 GiB a 4K render was told to need.
    assert measured.peak <= peak * margin and measured.plain <= plain * margin, report
