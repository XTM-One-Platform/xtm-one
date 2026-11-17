"""A render's screen-insert scenes shared out across its pages at ultra, for real: no page idles while frames remain.

The 22 Oct 2026 ultra render of a 136 s video drew its 7,100 plain frames on 8 pages in 15 min, then the 780 frames of
its two screen-insert scenes for over 40 min on the 2 pages whose runs held them, 6 pages idle. Here
``video_studio_render_fixture`` - a title, a recording with a video insert on its monitor, a green screen with a keyed
insert, a statement over a clip, a picture, a closing card - is drawn at the ultra preset (60 frames a second, its
motion blur's sub-frames, its encoder) by ``compositor.render_frames`` on as many pages as the runner's memory and CPUs
hold at their measured peak, two to four, its inserts matted as a render mattes them. At 1920x1080: how the pages share
the frames out does not depend on the size, and two 2160p pages starve a hosted runner (it lost its runner twice):

* no page goes more than 60 s without a frame to draw while another still draws;
* every frame is in the video once, the pieces the pages drew tiling the range.

What the pages measured, and what one run a page by frame count would have left them idle at those costs, is logged.

Minutes on a hosted runner: it runs with ``VIDEO_STUDIO_ULTRA_RENDER=1`` (the ``Video Studio ultra render`` workflow),
with ffmpeg and Playwright's Chromium - a failure, not a skip, under ``REQUIRE_CHROMIUM=1``.
"""

from __future__ import annotations

import itertools
import logging
import os
import time

import pytest

from app.services.builtin_tools.html_render import font_face_css
from app.services.video_studio import compositor, encoder, page_schedule, render_preflight, resources
from app.services.video_studio.spec import QUALITY_PRESETS
from tests import video_studio_render_fixture as fixture

pytestmark = [pytest.mark.chromium, pytest.mark.video_ultra]

#: The most pages drawn, and the fewest: one page shares nothing out.
PAGES = 4
MIN_PAGES = 2
#: What the runner keeps beside the pages: the test process, the matted inserts' files, the system.
KEPT_BYTES = 4 * 1024**3
SIZE = (1920, 1080)
#: The fixture's scenes but its last clip: its two insert scenes - the recording's video insert, the green screen's
#: keyed one - between cards, a statement and a picture.
SCENES = ("f01-open", "f02-console", "f03-green", "f04-claim", "f05-picture", "f07-close")
#: The longest a page may go without a frame to draw while another still draws.
IDLE_SECONDS = 60


@pytest.fixture(autouse=True)
def _asked():
    if os.environ.get("VIDEO_STUDIO_ULTRA_RENDER") != "1":
        pytest.skip("a 2160p60 render takes minutes: VIDEO_STUDIO_ULTRA_RENDER=1 runs it")


def _missing(what: str) -> None:
    if os.environ.get("REQUIRE_CHROMIUM") == "1":
        pytest.fail(f"{what} is required here and is missing")
    pytest.skip(f"{what} is missing")


def _pages(document: dict, x264_preset: str) -> int:
    """The pages this runner's memory holds at their measured peak with the video's inserts
    (``render_preflight.memory_budget``, as a render lays its pages out) and its CPUs, :data:`MIN_PAGES` to
    :data:`PAGES`."""
    room = (resources.memory_limit() or resources.host_memory() or 0) - KEPT_BYTES
    most = max(MIN_PAGES, min(PAGES, resources.cpu_budget()))
    inserts = render_preflight.InsertLoad.of(document)
    held = [
        count
        for count in range(MIN_PAGES, most + 1)
        if render_preflight.memory_budget(SIZE, pages=count, x264_preset=x264_preset, inserts=inserts, cpus=count) <= room
    ]
    return max(held, default=MIN_PAGES)


async def test_no_page_idles_more_than_60_seconds_while_the_insert_scenes_are_drawn(tmp_path):
    if not fixture.ffmpeg():
        _missing("ffmpeg")
    preset = QUALITY_PRESETS["ultra"]
    made = fixture.build(tmp_path, seconds=24, fps=preset.fps, scenes=SCENES)
    document = made.plan.document
    kinds = render_preflight.FrameKinds.of(document)
    assert len(kinds.inserts) >= 2, kinds
    await fixture.matte(made, SIZE)
    pages = _pages(document, preset.x264_preset)
    tracker = compositor.DrawProgress()
    began = time.monotonic()
    try:
        video = await compositor.render_frames(
            document,
            made.media.assets,
            preset=preset,
            output_size=SIZE,
            workdir=str(tmp_path / "render"),
            font_css=font_face_css(),
            pages=pages,
            tracker=tracker,
        )
    except Exception as exc:
        if "Executable doesn't exist" in str(exc) or "playwright install" in str(exc).lower():
            _missing(f"Playwright's Chromium: {str(exc)[:200]}")
        raise
    took = time.monotonic() - began
    work = tracker.work
    assert work is not None
    idle = tracker.idle()
    # What one run a page by frame count would have left each page waiting for the slowest, at the measured costs.
    costs = tracker.costs()
    prices = page_schedule.FramePrices(kinds, 0, int(document["frames"]))
    segments = compositor.plan_segments(0, int(document["frames"]), compositor.segment_frames(int(document["frames"]), preset.fps), [])
    before = [sum(prices.cost(a, b, costs) + costs.segment for a, b in run) for run in compositor.page_runs(segments, pages)]
    report = (
        f"ultra, {int(document['frames'])} frames in {took:.0f} s on {work.pages} pages: idle while frames remained {idle} s; "
        f"{work.moved} pieces taken whole from another page's run, {work.cuts} cut on a frame, {tracker.warm_frames} "
        f"frames drawn uncaptured into a matted insert; each kind at {tracker.kinds()}; one run a page by frame count "
        f"would have taken {[round(seconds) for seconds in before]} s, the pages idle up to "
        f"{round(max(before) - min(before))} s"
    )
    logging.getLogger(__name__).warning("%s", report)
    assert idle and max(idle.values()) <= IDLE_SECONDS, report
    plan = work.plan()
    assert plan[0][0] == 0 and plan[-1][1] == int(document["frames"]), plan
    assert all(a[1] == b[0] for a, b in itertools.pairwise(plan)), plan
    info = await encoder.probe(video)
    assert (info.width, info.height) == SIZE and info.duration == pytest.approx(float(document["duration"]), abs=0.2), report
