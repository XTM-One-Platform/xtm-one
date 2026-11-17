"""A render's estimate held to the time the render took: within 20 %, from what an earlier render measured.

The 4K render of 21 Oct 2026 was estimated at 30 min and took 47: the model was a per-frame average over past
renders, blind to what a frame shows. Here two renders of ``video_studio_render_fixture`` are drawn for real at
draft (1280x720, 30 fps) on two pages - their inserts matted, their frames composed and encoded by
``compositor.render_frames``, the MP4 watched by the check stage (``frame_freeze.check_frames``):

* the first, the whole mixed video (30 s), is the platform's history: what it measured
  (``render_preflight.measurement``: its frames by kind, each stage's seconds) read back by
  ``render_preflight.measured_rates`` as the next pre-flight reads a render's row;
* the second, another mix of the same scenes (fewer inserts, 39 s), is estimated from it by
  ``render_preflight.estimate`` - its frames by kind where they are (``FrameKinds``), the compositor's schedule of
  them replayed (``page_schedule``: runs of the same cost, the frames left taken by the pages that are idle),
  the slowest page's time, its inserts' time on screen, its frames and pixels for the check - before it is drawn,
  then drawn: the estimate is within 20 % of what it took;
* the first estimated again, as its next render would be: every scene type at what its own render measured,
  its pages at the parallel efficiency they had - within 20 % of the time that render took.

An average over the first render's frames would say the second takes about as long a frame as the first, whose
frames are mostly inserts: the kinds, priced at what each cost a page and shared out as the pages share them, are
what bring it within 20 %.

Minutes on a 2 vCPU runner: it runs with ``VIDEO_STUDIO_ULTRA_RENDER=1`` (the ``Video Studio ultra render``
workflow), with ffmpeg and Playwright's Chromium - a failure, not a skip, under ``REQUIRE_CHROMIUM=1``.
"""

from __future__ import annotations

import logging
import os
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.services.builtin_tools.html_render import font_face_css
from app.services.video_studio import compositor, frame_freeze, render_history, render_preflight, resources
from app.services.video_studio.spec import QUALITY_PRESETS
from tests import video_studio_render_fixture as fixture

pytestmark = [pytest.mark.chromium, pytest.mark.video_ultra]

QUALITY = "draft"
PAGES = 2
#: The second render's scenes: the cards, the recording with its insert, the statement and the picture.
OTHER_MIX = ("f01-open", "f02-console", "f04-claim", "f05-picture", "f07-close")


@pytest.fixture(autouse=True)
def _asked():
    if os.environ.get("VIDEO_STUDIO_ULTRA_RENDER") != "1":
        pytest.skip("two renders take minutes: VIDEO_STUDIO_ULTRA_RENDER=1 runs them")


def _missing(what: str) -> None:
    if os.environ.get("REQUIRE_CHROMIUM") == "1":
        pytest.fail(f"{what} is required here and is missing")
    pytest.skip(f"{what} is missing")


class _Rows:
    """The render rows ``measured_rates`` reads: ``(quality, width, height, duration_ms, measured, preflight)``."""

    def __init__(self, rows):
        self.rows = rows

    async def execute(self, _statement, _params=None):
        return SimpleNamespace(all=lambda: list(self.rows))


async def _render(made: fixture.Fixture, workdir: Path) -> dict:
    """*made* drawn as a render draws it at :data:`QUALITY`: its inserts matted, its frames, its check, each timed."""
    preset = QUALITY_PRESETS[QUALITY]
    size = (1280, 720)
    began = time.monotonic()
    await fixture.matte(made, size)
    inserted = time.monotonic()
    tracker = compositor.DrawProgress()
    try:
        video = await compositor.render_frames(
            made.plan.document,
            made.media.assets,
            preset=preset,
            output_size=size,
            workdir=str(workdir),
            font_css=font_face_css(),
            pages=PAGES,
            tracker=tracker,
        )
    except Exception as exc:
        if "Executable doesn't exist" in str(exc) or "playwright install" in str(exc).lower():
            _missing(f"Playwright's Chromium: {str(exc)[:200]}")
        raise
    drawn = time.monotonic()
    await frame_freeze.check_frames(video, document=made.plan.document, width=size[0], height=size[1], fps=preset.fps)
    checked = time.monotonic()
    document = made.plan.document
    layout = render_preflight.Layout(pages=PAGES, cpus=resources.cpu_budget(), wanted=PAGES)
    return {
        "measured": render_preflight.measurement(
            layout,
            frames=tracker.done,
            seconds=drawn - inserted,
            kinds=tracker.kinds(),
            scenes=tracker.scenes(),
            stages={"media": 0.0, "inserts": inserted - began, "frames": drawn - inserted, "checks": checked - drawn, "audio": 0.0, "mux": 0.0, "store": 0.0},
            total_seconds=checked - began,
            insert_seconds=sum(b - a for a, b in render_preflight.FrameKinds.of(document).inserts),
            encoder_threads=tracker.encoder_threads,
            page_runs=tracker.runs(),
        ),
        "duration_ms": int(float(document["duration"]) * 1000),
        "took": checked - began,
    }


async def test_a_render_takes_within_20_percent_of_its_estimate_made_from_an_earlier_one(tmp_path):
    if not fixture.ffmpeg():
        _missing("ffmpeg")
    preset = QUALITY_PRESETS[QUALITY]
    first_dir, second_dir = tmp_path / "first", tmp_path / "second"
    first_dir.mkdir()
    second_dir.mkdir()
    first = fixture.build(first_dir, seconds=30, fps=preset.fps)
    history = await _render(first, first_dir)
    rates = await render_preflight.measured_rates(
        _Rows([(QUALITY, 1280, 720, history["duration_ms"], history["measured"], None)]),
        QUALITY,
        contended_pages=render_preflight.contended(PAGES, resources.cpu_budget()),
    )
    assert rates is not None and rates.measured_quality == 1 and set(rates.kind_rates) >= {"plain", "insert"}, history
    # The same video estimated again from its own render: every scene type it drew at what it measured, its
    # pages at the parallel efficiency they had over the run - within 20 % of the time that render took.
    own = render_history.OwnRender.of("first", QUALITY, 1280, 720, history["duration_ms"], history["measured"])
    assert own is not None and own.efficiency, history
    first_kinds = render_preflight.FrameKinds.of(first.plan.document)
    again = render_preflight.estimate(
        frames=int(first.plan.document["frames"]),
        size=(1280, 720),
        quality=QUALITY,
        pages=PAGES,
        cpus=resources.cpu_budget(),
        rate=render_preflight.with_own_render(rates, own),
        kinds=first_kinds,
        insert_seconds=sum(b - a for a, b in first_kinds.inserts),
        scenes=len(first.plan.document["scenes"]),
        max_ranges=1,
    )
    assert again.costs_from == "video", again.line()
    assert abs(again.expected_seconds - history["took"]) <= 0.2 * history["took"], (again.expected_seconds, history)

    second = fixture.build(second_dir, seconds=61.4, fps=preset.fps, scenes=OTHER_MIX)
    document = second.plan.document
    kinds = render_preflight.FrameKinds.of(document)
    said = render_preflight.estimate(
        frames=int(document["frames"]),
        size=(1280, 720),
        quality=QUALITY,
        pages=PAGES,
        cpus=resources.cpu_budget(),
        rate=rates,
        kinds=kinds,
        insert_seconds=sum(b - a for a, b in kinds.inserts),
        scenes=len(document["scenes"]),
        max_ranges=1,
    )
    # What a per-frame average of the first render would have said of the second, for the report.
    average = render_preflight.estimate(
        frames=int(document["frames"]), size=(1280, 720), quality=QUALITY, pages=PAGES, cpus=resources.cpu_budget(), rate=rates, max_ranges=1
    )
    runs = render_preflight.page_run_seconds(kinds, rates, size=(1280, 720), quality=QUALITY, pages=PAGES, cpus=resources.cpu_budget())
    drawn = await _render(second, second_dir)
    took = drawn["took"]
    report = (
        f"the second render took {took:.0f} s against {said.expected_seconds:.0f} s estimated by kind "
        f"({said.kinds}, {said.kind_seconds}, its pages' runs {[round(run) for run in runs]} s, measured "
        f"{drawn['measured'].get('page_runs')}) and {average.expected_seconds:.0f} s by a per-frame average; the first "
        f"measured {history['measured']}"
    )
    logging.getLogger(__name__).warning("%s", report)
    # Another mix: a smaller share of its frames show an insert than the first render's did.
    first_kinds = render_preflight.FrameKinds.of(first.plan.document).counts()
    assert said.kinds["insert"] / said.frames < first_kinds["insert"] / sum(first_kinds.values()), report
    assert abs(said.expected_seconds - took) <= 0.2 * took, report
