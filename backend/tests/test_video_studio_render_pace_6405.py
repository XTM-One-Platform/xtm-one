"""A render's pace under its pre-flight's, explained; a scene type its video never drew, priced from the platform.
Synthetic trackers, clocks and rows only.

The 23 Oct 2026 ultra render of a video drew at 1.3 to 3.0 frames a second where its previous render - the same scenes
and inserts on the same 8 pages - drew at 7.23, for a pre-flight of 28 min, and nothing said why:

* a pace under half the pre-flight's for 2 minutes names its cause in the status line and in ``get_video_jobs``: the
  scene type over its cost, the jobs sharing the worker, the CPUs its pages got, its insert frames read from the cache;
* under a third for 5 minutes, the worker's throttling and the render lanes are checked once, and said in the stage;
* its measure keeps the breakdown: its time corrects no estimate, and on a shared worker its video's next pre-flights
  learn only the scene types it found over their cost;
* a pre-flight prices a scene type its video's last render did not draw at the platform's measure, and says so.
"""

from __future__ import annotations

import asyncio
import uuid
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.models.video_studio import VideoRender
from app.services.video_studio import compositor, jobs, render, render_history, render_pace, render_preflight, resources
from app.services.video_studio.render_pace import Neighbour, PaceWatch
from app.services.video_studio.render_preflight import FrameKinds, Rates

UHD = (3840, 2160)
CEILING = 64 << 30
#: A pre-flight of 3,000 frames on 2 pages, each scene type at 0.5 s a frame on a page.
EXPECTED = {
    "quality": "ultra",
    "expected_seconds": 900,
    "frames": 3000,
    "pages": 2,
    "cpus": 8,
    "frame_seconds": 0.5,
    "correction": 1.0,
    "scenes": {"title": 1500, "figure": 1500},
    "scene_seconds": {"title": 0.5, "figure": 0.5},
    "scene_raw_seconds": {"title": 0.5, "figure": 0.5},
    "model": render_preflight.ESTIMATE_MODEL,
}


class _Clock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now


async def _none() -> list:
    return []


class _Render:
    """A whole render's frames on 2 pages, each frame of a scene type at what a page takes for it, watched as the
    render's progress reports them."""

    def __init__(self, watch_kwargs: dict | None = None, expected: dict | None = None) -> None:
        self.clock = _Clock()
        self.tracker = compositor.DrawProgress(total=3000)
        kwargs = {"neighbours": _none, "cpu": lambda: None, "stat": lambda: None, **(watch_kwargs or {})}
        self.watch = PaceWatch(expected or EXPECTED, pages=2, cpus=8, clock=self.clock, **kwargs)

    def draw(self, scene: str, frames: int, seconds: float) -> None:
        """*frames* frames of *scene*, each *seconds* on its page: the 2 pages draw side by side."""
        for frame in range(frames):
            self.tracker.timed("plain", seconds, frame % 2, scene=scene)
            self.tracker.done += 1
            self.clock.now += seconds / 2
            asyncio.run(self.watch.observe(self.tracker))

    def status(self) -> str:
        return self.watch.line(self.tracker.status(round(100 * self.tracker.done / self.tracker.total)))


# -- 1. A scene type 5x over its expected cost ---------------------------------------------------


def test_a_scene_type_five_times_over_its_expected_cost_is_named_in_the_status_line(monkeypatch):
    drawing = _Render()
    # Titles at their price: the pace is the pre-flight's, the status line its own.
    drawing.draw("title", 400, 0.5)
    assert drawing.watch.ratio == pytest.approx(1.0) and not drawing.watch.slow
    assert drawing.status() == "Drawing frames (13%)"
    # A figure at 2.5 s a frame, 5x its price: a fifth of the pre-flight's pace, named once slow for 2 minutes.
    drawing.draw("figure", 60, 2.5)
    assert drawing.watch.ratio < render_pace.SLOW_SHARE and not drawing.watch.slow
    drawing.draw("figure", 140, 2.5)
    assert drawing.watch.slow and drawing.watch.ratio == pytest.approx(0.2, rel=0.05)
    line = drawing.status()
    assert line.startswith("Drawing frames (20%) - 0.20x the pre-flight's pace: figure 2.50 s a frame (expected 0.50 s)")
    assert len(line) <= render_pace.STAGE_ROOM

    found = drawing.watch.breakdown()
    assert found["over"] == ["figure"] and found["scenes"]["figure"] == [200, 2.5, 0.5]
    assert found["alike"] == pytest.approx(1.0) and not found["shared"]

    # Kept on its preflight beside its pace: get_video_jobs says the whole cause while it draws.
    written = AsyncMock()
    monkeypatch.setattr(jobs, "mark", written)
    asyncio.run(render.PaceNote("r1", EXPECTED).note(drawing.tracker, drawing.watch))
    kept = written.await_args.kwargs["preflight"]
    assert kept["slow"]["over"] == ["figure"] and "pace" not in kept
    row = VideoRender(
        id=uuid.uuid4(), project_id=uuid.uuid4(), quality="ultra", status="running", preflight=kept, started_at=datetime.now(UTC) - timedelta(minutes=5)
    )
    said = render_preflight.said_of(row)
    span = render_pace._duration(kept["slow"]["slow_seconds"])
    assert kept["slow"]["slow_seconds"] >= render_pace.SLOW_SECONDS
    assert f"Its frames have drawn under half the pre-flight's pace for {span} (0.20x over the last minute" in said
    assert "figure 2.50 s a frame on a page against 0.50 s expected (x5.00, 200 frames)" in said
    assert "every other scene type at x1.00 its expected cost" in said and "no other job ran on its worker" in said
    # A new attempt explains its own pace.
    assert "slow" not in render_pace.unpaced(kept)

    # Its measure keeps the breakdown, said in its answer: the next pre-flights learn the figure, not the render.
    record = drawing.watch.record()
    measured = render_preflight.measurement(render_preflight.Layout(pages=2, cpus=8, wanted=2), frames=600, seconds=350.0, pace=record)
    assert measured["pace"]["over"] == ["figure"]
    told = render_preflight.said_of(SimpleNamespace(preflight=EXPECTED, measured=measured, quality="ultra", status="ready"))
    assert f"Its frames drew under half the pre-flight's pace for {span} (0.20x at worst)" in told
    assert "The next pre-flights price figure at what this render measured, and hold none of its time against their estimates" in told


# -- 2. A worker shared with two scene previews -------------------------------------------------


def test_a_render_on_a_worker_shared_with_two_previews_counts_them_and_checks_the_worker_once():
    previews = [Neighbour("scene preview", "a1" * 16, scene="s03"), Neighbour("scene preview", "b2" * 16, scene="s07")]
    clock_cpu = {"render": 0.0, "worker": 0.0, "periods": 0, "throttled": 0}

    async def neighbours():
        return previews

    def cpu():
        return clock_cpu["render"]

    def stat():
        return resources.CpuStat(clock_cpu["worker"], clock_cpu["periods"], clock_cpu["throttled"], clock_cpu["throttled"] * 0.1)

    lanes = AsyncMock(return_value={"render": {"here": 1, "slots": 1, "running": 1, "waiting": 0}, "preview": {"here": 2, "slots": 2, "running": 2, "waiting": 3}})
    expected = {**EXPECTED, "scenes": {"title": 1500, "statement": 1500}, "scene_seconds": {"title": 0.5, "statement": 0.5}, "scene_raw_seconds": {"title": 0.5, "statement": 0.5}}
    drawing = _Render({"neighbours": neighbours, "cpu": cpu, "stat": stat, "lanes": lanes}, expected)

    def draw(scene: str, frames: int, seconds: float) -> None:
        for _frame in range(frames):
            # Its pages get 2 CPUs of the 8 the worker spends: the previews have the rest, and the quota holds it back.
            clock_cpu["render"] += 2 * seconds / 2
            clock_cpu["worker"] += 8 * seconds / 2
            clock_cpu["periods"] += 10
            clock_cpu["throttled"] += 4
            drawing.draw(scene, 1, seconds)

    # Every scene type 4x its price alike: the worker, not a scene type.
    draw("title", 80, 2.0)
    draw("statement", 80, 2.0)
    assert drawing.watch.slow
    found = drawing.watch.breakdown()
    assert found["over"] == [] and found["alike"] == pytest.approx(4.0) and found["shared"]
    assert found["cpus_used"] == pytest.approx(2.0) and found["cpu_share"] == pytest.approx(0.25)
    line = drawing.status()
    assert "2 scene previews on its worker" in line and "its pages got 2.0 of 8 CPUs" in line
    assert lanes.await_count == 0

    # Under a third for 5 minutes: the throttling and the lanes are checked once, and said in the stage text.
    draw("statement", 200, 2.0)
    assert lanes.await_count == 1
    line = drawing.status()
    assert "checked: throttled 40%, render lane 1/1, preview lane 2/2 +3 waiting" in line and len(line) <= render_pace.STAGE_ROOM
    draw("statement", 40, 2.0)
    assert lanes.await_count == 1
    said = render_pace.slow_words(drawing.watch.breakdown())
    assert "it shares its worker with 2 scene previews (scene preview `a1a1a1a1` of scene s03 and scene preview `b2b2b2b2` of scene s07)" in said
    assert "its pages got 2.0 of the worker's 8 CPUs, 25% of the CPU the worker used" in said
    assert "Checked once after 5 min under a third of the pre-flight's pace: the worker's CPU quota held it back in 40% of its periods" in said
    assert "its preview lane runs 2 of its 2 slots on this worker (2 on all workers, 3 waiting)" in said


def test_the_jobs_beside_a_render_are_read_from_the_executions_its_worker_holds():
    own, preview, other, music = (uuid.uuid4() for _ in range(4))
    preview_render, other_render = uuid.uuid4(), uuid.uuid4()

    class _Db:
        async def execute(self, statement):
            text = str(statement)
            if "orchestration_executions" in text:
                rows = [
                    (own, jobs.RENDER_ADAPTER, {"render_id": str(uuid.uuid4())}),
                    (preview, jobs.PREVIEW_ADAPTER, {"render_id": str(preview_render)}),
                    (other, jobs.RENDER_ADAPTER, {"render_id": str(other_render)}),
                    (music, jobs.MUSIC_ADAPTER, {"music_id": "m1"}),
                ]
            else:
                rows = [(preview_render, "s03", "Drawing frames (40%)"), (other_render, None, "Hearing and watching the render")]
            return SimpleNamespace(all=lambda: rows)

    found = asyncio.run(render_pace.neighbours_of(_Db(), "worker-1", own))
    assert [item.kind for item in found] == ["scene preview", "render", "AI track"]
    assert render_pace.neighbours_count(found) == "1 scene preview, 1 render and 1 AI track"
    assert found[1].words() == f"render `{str(other_render)[:8]}` (Hearing and watching the render)"


# -- 3. What the next pre-flights learn ---------------------------------------------------------


def test_a_render_whose_pace_was_explained_corrects_no_estimate_and_prices_only_what_it_found_over_cost():
    scenes = {"title": [3000, 6600.0], "figure": [600, 1740.0]}
    measured = {"pages": 8, "cpus": 8, "frames": 3600, "seconds": 1100.0, "total_seconds": 1500.0, "scenes": scenes}
    preflight = {"model": render_preflight.ESTIMATE_MODEL, "raw_seconds": 600}
    assert render_history.estimate_error(preflight, measured) == (600.0, 1500.0)
    explained = {**measured, "pace": {"slow_seconds": 900, "over": ["figure"], "shared": False}}
    assert render_history.estimate_error(preflight, explained) is None
    # On its own worker: every scene type it drew prices its video's next pre-flight.
    alone = render_history.OwnRender.of("r1", "ultra", *UHD, 60_000, explained)
    assert set(alone.scene_rates) == {"title", "figure"} and alone.shared == ()
    # On a shared worker: the figure it found over its cost only, the title's measure the worker's share.
    shared = {**measured, "pace": {"slow_seconds": 900, "over": ["figure"], "shared": True}}
    found = render_history.OwnRender.of("r1", "ultra", *UHD, 60_000, shared)
    assert set(found.scene_rates) == {"figure"} and found.shared == ("title",) and found.stages == {}
    assert render_history.OwnRender.of("r1", "ultra", *UHD, 60_000, {**shared, "pace": {**shared["pace"], "over": []}}) is None


def _film() -> FrameKinds:
    """3,600 frames at 60 fps: a title, a statement, then a figure built from a deck - added since the last render."""
    return FrameKinds(fps=60.0, frames=3600, scenes=((0.0, 20.0, "title"), (20.0, 40.0, "statement"), (40.0, 60.0, "figure")))


def test_a_pre_flight_prices_a_figure_its_video_s_last_render_did_not_draw_from_the_platform_class_and_says_so():
    cost = render_preflight.PIXEL_COST["ultra"] * UHD[0] * UHD[1]
    platform = Rates(
        render_preflight.DEFAULT_PAGE_RATE,
        measured=3,
        measured_quality=3,
        contended=False,
        scene_rates={"title": cost / 0.8, "statement": cost / 0.9, "figure": cost / 0.32},
        scene_measured={"title": 12, "statement": 12, "figure": 4},
    )
    # Its last ultra render drew the title and the statement only.
    last = render_preflight.measurement(
        render_preflight.Layout(pages=8, cpus=9, wanted=8),
        frames=2400,
        seconds=700.0,
        scenes={"title": [1200, 2640.0], "statement": [1200, 3120.0]},
    )
    own = render_history.OwnRender.of("1a2b3c4d-0000", "ultra", *UHD, 40_000, last)
    rates = render_preflight.with_own_render(platform, own)
    film = _film()
    said = render_preflight.estimate(
        frames=film.frames, size=UHD, quality="ultra", pages=8, cpus=9, ceiling=CEILING, rate=rates, kinds=film, scenes=3, max_ranges=1, workers=1
    )
    # The title and the statement at what the video's render measured; the figure at the platform's class.
    assert said.scene_raw_seconds["title"] == pytest.approx(2.2, rel=1e-3)
    assert said.scene_raw_seconds["statement"] == pytest.approx(2.6, rel=1e-3)
    assert said.scene_raw_seconds["figure"] == pytest.approx(0.32, rel=1e-3)
    assert said.scene_basis["figure"] == "measured on 4 renders or previews of this platform"
    assert said.unlearned == {"figure": {"frames": 1200, "why": "not drawn", "basis": "measured on 4 renders or previews of this platform"}}
    sentence = (
        "Not learned from this video's own history, priced from the platform's instead: figure (1,200 frames, which its "
        "last ultra render `1a2b3c4d` did not draw) - measured on 4 renders or previews of this platform."
    )
    assert sentence in said.line()
    # Said again by get_video_jobs while it waits for a worker.
    row = VideoRender(id=uuid.uuid4(), project_id=uuid.uuid4(), quality="ultra", status="pending", preflight=said.as_dict())
    assert sentence in render_preflight.said_of(row)
    # A video whose last render drew every scene type it draws learns them all from it: nothing said.
    again = render_preflight.estimate(
        frames=2400,
        size=UHD,
        quality="ultra",
        pages=8,
        cpus=9,
        ceiling=CEILING,
        rate=rates,
        kinds=FrameKinds(fps=60.0, frames=2400, scenes=((0.0, 20.0, "title"), (20.0, 40.0, "statement"))),
        scenes=2,
        max_ranges=1,
        workers=1,
    )
    assert again.unlearned == {} and "Not learned from this video's own history" not in again.line()
