"""A render's segments are planned over its whole range and shared out between its pages.

The pages are as many as the worker's CPUs and memory ceiling hold
(``render_preflight.layout``), which a worker given more CPUs, or an attempt
resumed on another worker, may change:

* the segments are planned over the whole range, the finished ones kept, so an
  attempt on another number of pages resumes from every segment closed whole;
* the pages share out the segments in contiguous runs of about the same frames;
* a page that fails stops the others at once, its exception raised as is;
* in Chromium, a render stopped on one page and resumed on three draws only
  the segment it lacked, and plays every frame of the first.
"""

from __future__ import annotations

import asyncio
import os
import shutil
import subprocess

import pytest

from app.services.video_studio import compositor, encoder
from app.services.video_studio.spec import QUALITY_PRESETS


def test_the_segments_are_planned_over_the_whole_range_and_the_finished_ones_kept():
    assert compositor.plan_segments(0, 700, 300, []) == [(0, 300), (300, 600), (600, 700)]
    # Finished by an attempt that drew on two pages, each page's chunk cut on its own: every one kept.
    kept = [(0, 300), (300, 350), (350, 650)]
    assert compositor.plan_segments(0, 700, 300, kept) == [(0, 300), (300, 350), (350, 650), (650, 700)]
    # A gap between two kept segments is cut from its start; overlapping ones are skipped, the first kept.
    assert compositor.plan_segments(0, 700, 300, [(100, 200), (150, 400)]) == [(0, 100), (100, 200), (200, 500), (500, 700)]
    # Outside the range (another window): ignored.
    assert compositor.plan_segments(100, 400, 300, [(0, 300)]) == [(100, 400)]


def test_the_pages_share_the_segments_in_contiguous_runs_of_about_the_same_frames():
    todo = compositor.segment_ranges(0, 7860, 600)
    runs = compositor.page_runs(todo, 4)
    assert len(runs) == 4 and [piece for run in runs for piece in run] == todo
    frames = [sum(b - a for a, b in run) for run in runs]
    assert max(frames) - min(frames) <= 1200
    # Never more pages than segments.
    assert compositor.page_runs([(0, 60)], 8) == [[(0, 60)]]
    assert compositor.page_runs([], 8) == []
    # One long segment, then short ones: every segment once, in order.
    uneven = [(0, 1000), (1000, 1010), (1010, 1020)]
    assert [piece for run in compositor.page_runs(uneven, 3) for piece in run] == uneven


async def test_the_first_failure_stops_the_other_pages_and_is_raised_as_is():
    stopped: list[str] = []

    async def drawing():
        try:
            await asyncio.sleep(30)
        except asyncio.CancelledError:
            stopped.append("drawing")
            raise

    async def failing():
        await asyncio.sleep(0)
        raise compositor.RenderCancelledError("The render was cancelled.")

    with pytest.raises(compositor.RenderCancelledError):
        await compositor._together(drawing(), failing())
    assert stopped == ["drawing"]
    assert await compositor._together(asyncio.sleep(0, "a"), asyncio.sleep(0, "b")) == ["a", "b"]


def _frames(video: str) -> list[str]:
    """The md5 of each decoded frame of *video*."""
    out = subprocess.run(  # noqa: S603
        [shutil.which("ffmpeg") or "ffmpeg", "-hide_banner", "-loglevel", "error", "-i", video, "-map", "0:v", "-f", "framemd5", "-"],
        check=True,
        capture_output=True,
        text=True,
        timeout=120,
    ).stdout
    return [line.rsplit(",", 1)[-1].strip() for line in out.splitlines() if line and not line.startswith("#")]


@pytest.mark.chromium
async def test_a_render_stopped_on_one_page_resumes_on_three_from_its_segments(tmp_path, monkeypatch):
    if not encoder.encoder_available():
        pytest.skip("ffmpeg is not installed")
    from app.services.builtin_tools.html_render import font_face_css
    from tests.test_video_studio_render_stall import _document

    monkeypatch.setattr(compositor, "SEGMENT_SECONDS", 1)
    document = _document()
    total = int(document["frames"])

    async def draw(pages: int, drawn: list[int], tracker: compositor.DrawProgress | None = None) -> str:
        return await compositor.render_frames(
            document,
            compositor.CompositorAssets(),
            preset=QUALITY_PRESETS["draft"],
            output_size=(640, 360),
            workdir=str(tmp_path),
            font_css=font_face_css(),
            pages=pages,
            on_frame=lambda: drawn.append(1),
            tracker=tracker,
        )

    first: list[int] = []
    try:
        video = await draw(1, first)
    except Exception as exc:
        text = str(exc)
        if "Executable doesn't exist" not in text and "playwright install" not in text.lower():
            raise
        if os.environ.get("REQUIRE_CHROMIUM") == "1":
            pytest.fail(f"Playwright's Chromium is required here and is not installed: {exc}")
        pytest.skip("Playwright's Chromium is not installed (python -m playwright install chromium)")
    expected = _frames(video)
    assert len(first) == total and len(expected) == total
    # Stopped while it wrote its second segment; the next attempt has three pages.
    segments = sorted((tmp_path / compositor.SEGMENTS_DIR).glob("seg-*.mp4"))
    segments[1].unlink()
    again: list[int] = []
    tracker = compositor.DrawProgress()
    video = await draw(3, again, tracker)
    assert len(again) == 30 and tracker.kept == total - 30 and tracker.done == total
    # The frames kept are the first attempt's; the one segment drawn again starts its own page, as a chunk does.
    resumed = _frames(video)
    assert len(resumed) == total and resumed[:30] == expected[:30] and resumed[60:] == expected[60:]
