"""A render estimated from its video's own last render, its correction read per worker shape, and its ETA switched to
the pace its frames measure. Synthetic rows and documents only.

The 23 Oct 2026 ultra render of a 136 s video - 8,462 frames at 2160p60 on 8 pages - was estimated at about 32 min
(1.63 s a frame, corrected by x1.43 from one earlier render) while the previous render of the same video, at the same
revision, had taken 1 h 6 min for its frames alone:

* every scene type the video's own last render at the quality drew is priced at what it measured, its pages at the
  parallel efficiency they had over the whole run, before any correction - so its second pre-flight lands within 20%
  of the time its first render took;
* the correction is read per worker shape (pages, CPUs, memory) and per kind of history (the video's own render, the
  renders on the same shape, the platform's), over the last 5 such renders, and names them;
* once a tenth of its frames are drawn, the pace they measure replaces the pre-flight's ETA in the render card and in
  ``get_video_jobs``, and a finished render sets each scene type's measure against its estimate.
"""

from __future__ import annotations

import asyncio
import uuid
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.models.video_studio import VideoRender
from app.services.video_studio import (
    compositor,
    jobs,
    page_schedule,
    projects,
    render,
    render_history,
    render_pace,
    render_preflight,
)
from app.services.video_studio.render_history import Shape
from app.services.video_studio.render_preflight import FrameKinds, Rates

UHD = (3840, 2160)
CEILING = 64 << 30
VIDEO = uuid.UUID("00000000-0000-0000-0000-0000000006a1")
ANOTHER = uuid.UUID("00000000-0000-0000-0000-0000000006a2")
#: What a frame of each scene type took a page in the video's first render at ultra, and its pages' efficiency.
COSTS = {"title": 2.2, "statement": 2.6, "statement+clip": 3.0, "clip+insert": 5.41}
EFFICIENCY = 0.9


def _film() -> FrameKinds:
    """8,462 frames of 3840x2160 at 60 fps: a title, a statement, a statement over a clip, two clips with a screen
    insert - 5,400 frames, then 3,062 -, half-second transitions between them."""
    fps = 60.0
    clip = 1531 / fps
    bounds = [0.0, 20.0, 60.0, 90.0, 90.0 + clip, 90.0 + 2 * clip]
    names = ["title", "statement", "statement+clip", "clip+insert", "clip+insert"]
    return FrameKinds(
        fps=fps,
        frames=8462,
        transitions=tuple((at, at + 0.5) for at in bounds[1:5]),
        inserts=((90.0, bounds[5]),),
        scenes=tuple((bounds[index], bounds[index + 1], names[index]) for index in range(5)),
    )


def _measured(film: FrameKinds, *, costs: dict | None = None, efficiency: float = EFFICIENCY) -> dict:
    """What a whole render of *film* records (``render_preflight.measurement``) drawn on 8 pages of 9 CPUs, each frame
    at *costs* on its page and the pages at *efficiency* over the run."""
    costs = costs or COSTS
    kinds: dict[str, list] = {}
    scenes: dict[str, list] = {}
    for (kind, name), count in film.scene_counts().items():
        for found, key in ((kinds, kind), (scenes, name)):
            drawn = found.setdefault(key, [0, 0.0])
            drawn[0] += count
            drawn[1] += count * costs[name]
    spent = sum(seconds for _frames, seconds in scenes.values())
    frames_seconds = spent / (8 * efficiency)
    stages = {"media": 120.0, "figures": 0.0, "inserts": 300.0, "frames": frames_seconds, "audio": 30.0, "mux": 40.0, "checks": 200.0, "store": 60.0}
    return render_preflight.measurement(
        render_preflight.Layout(pages=8, cpus=9, wanted=8),
        frames=film.frames,
        seconds=frames_seconds,
        kinds={name: [count, round(seconds, 2)] for name, (count, seconds) in kinds.items()},
        scenes={name: [count, round(seconds, 2)] for name, (count, seconds) in scenes.items()},
        stages=stages,
        total_seconds=sum(stages.values()),
        insert_seconds=sum(b - a for a, b in film.inserts),
        encoder_threads=1,
        page_runs=[[2, round(spent / 8, 2), round(frames_seconds - 4, 2)] for _page in range(8)],
    )


def _platform() -> Rates:
    """The platform's history the 23 Oct pre-flight read: every scene type at its median across videos and qualities,
    far under what this video's frames cost at ultra, and one earlier ultra estimate 1.43x under its render."""
    cost = render_preflight.PIXEL_COST["ultra"] * UHD[0] * UHD[1]
    priced = {"title": 0.8, "statement": 0.9, "statement+clip": 1.0, "clip+insert": 1.2}
    return Rates(
        render_preflight.DEFAULT_PAGE_RATE,
        measured=3,
        measured_quality=3,
        contended=False,
        scene_rates={name: cost / seconds for name, seconds in priced.items()},
        scene_measured=dict.fromkeys(priced, 12),
        errors=(render_history.Missed(1260, 1800, "e0a1b2c3"),),
    )


def _estimate(film: FrameKinds, rates: Rates | None) -> render_preflight.Preflight:
    return render_preflight.estimate(
        frames=film.frames,
        size=UHD,
        quality="ultra",
        pages=8,
        cpus=9,
        ceiling=CEILING,
        rate=rates,
        kinds=film,
        insert_seconds=sum(b - a for a, b in film.inserts),
        scenes=len(film.scenes),
        max_ranges=1,
        workers=1,
    )


class _History:
    """The reads of a pre-flight (``render_preflight.rates_of``), each answered from *renders* - newest first - as the
    database would select them: the platform's whole renders, its previews (none), a video's renders, the estimates'
    errors at a quality."""

    def __init__(self, renders: list[SimpleNamespace]):
        self.renders = renders

    def begin_nested(self):
        history = self

        class _Savepoint:
            async def __aenter__(self):
                return history

            async def __aexit__(self, *_exc):
                return False

        return _Savepoint()

    async def execute(self, statement, _params=None):
        text = str(statement)
        asked = statement.compile().params
        project = next((value for key, value in asked.items() if key.startswith("project_id")), None)
        quality = next((value for key, value in asked.items() if key.startswith("quality")), None)
        rows = [row for row in self.renders if (project is None or row.project_id == project) and (quality is None or row.quality == quality)]
        if "video_renders.scene_id IS NOT NULL" in text:
            found = []
        elif project is not None and quality is not None:
            found = [(row.id, row.width, row.height, row.duration_ms, row.measured) for row in rows]
        elif project is not None:
            found = [(row.quality, row.width, row.height, row.measured) for row in rows]
        elif "video_renders.preflight IS NOT NULL" in text:
            # The columns ``render_history.estimate_errors`` reads: JSON scalars as text, objects as they are.
            def text_of(value):
                return None if value is None else str(value)

            found = [
                (
                    row.id,
                    *(text_of(row.measured.get(name)) for name in ("pages", "cpus", "frames_reused", "frames_inherited", "ranges", "total_seconds")),
                    row.measured.get("insert_frames"),
                    row.measured.get("pace"),
                    row.measured.get("contention"),
                    text_of(row.preflight.get("model")),
                    row.preflight.get("shape"),
                    text_of(row.preflight.get("costs_from")),
                    text_of(row.preflight.get("raw_seconds")),
                )
                for row in rows
                if row.preflight is not None
            ]
        else:
            found = [(row.quality, row.width, row.height, row.duration_ms, row.measured) for row in rows]
        return SimpleNamespace(all=lambda: list(found))


def _rendered(film: FrameKinds, said: render_preflight.Preflight, *, video=VIDEO, measured: dict | None = None) -> SimpleNamespace:
    return SimpleNamespace(
        id=uuid.uuid4(),
        project_id=video,
        quality="ultra",
        width=UHD[0],
        height=UHD[1],
        duration_ms=round(film.frames / film.fps * 1000),
        measured=measured or _measured(film),
        preflight=said.as_dict(),
    )


def _rates_of(history: _History, film: FrameKinds, video: uuid.UUID) -> Rates:
    return asyncio.run(
        render_preflight.rates_of(
            history,
            "ultra",
            contended_pages=False,
            video_id=video,
            shape=Shape.of(8, 9, CEILING),
            scene_types={name for _start, _end, name in film.scenes},
        )
    )


# -- 1. A video rendered twice at ultra ------------------------------------------------------


def test_the_second_ultra_pre_flight_of_a_video_lands_within_20_percent_of_its_first_render():
    film = _film()
    # Its first render's pre-flight: the platform's medians across videos, corrected by one earlier estimate.
    first = _estimate(film, _platform())
    assert first.pages == 8 and first.costs_from == "platform"
    rendered = _rendered(film, first)
    took = rendered.measured["total_seconds"]
    # The 23 Oct case: more than 2.5x under what the render took.
    assert first.expected_seconds * 2.5 < took
    # Its second: every scene type its first render drew at what it measured, its pages at their efficiency.
    rates = _rates_of(_History([rendered]), film, VIDEO)
    assert rates.costs_from == "video" and rates.efficiency == pytest.approx(EFFICIENCY, rel=1e-3)
    second = _estimate(film, rates)
    assert abs(second.expected_seconds - took) <= 0.2 * took, (second.expected_seconds, took)
    # The first's error was made from the platform's renders: it corrects none of this one.
    assert second.errors == () and second.correction == 1.0
    assert second.slowest == "clip+insert" and second.scene_raw_seconds["clip+insert"] == pytest.approx(5.41, rel=1e-3)
    short = str(rendered.id)[:8]
    line = second.line()
    assert f"at 5.41 s a frame on each of 8 pages (9 CPUs) - measured on the last ultra render of this video, `{short}` on 8 pages" in line
    assert "By scene type, all measured on the last ultra render of this video: 3,062 frames of clip with a screen insert at 5.41 s each on a page, 1,800 of statement over a clip at 3.00 s" in line
    assert (
        f"Every scene type the last ultra render of this video (`{short}`) drew is priced at what it measured on its 8 "
        "pages, and its frames at the parallel efficiency its pages had over the whole run, 0.90 (its frames a second "
        "over the run divided by its pages, against what a page took for a frame), before any correction."
    ) in line
    assert (
        "Not corrected: no earlier ultra estimate of this model on this worker shape (8 pages of 9 CPUs, a 64.0 GiB "
        "memory ceiling), priced from its video's own last render like this one, was followed by a whole render yet."
    ) in line
    kept = second.as_dict()
    assert kept["costs_from"] == "video" and kept["own_render"] == str(rendered.id) and kept["efficiency"] == pytest.approx(0.9, abs=1e-3)
    assert kept["shape"] == {"pages": 8, "cpus": 9, "memory_mib": 65536}

    # Another video on the same worker shape: each scene type at what the renders on this shape measured, never
    # corrected by an error the platform's medians made.
    other = _rates_of(_History([rendered]), film, ANOTHER)
    assert other.own is None and other.costs_from == "shape" and other.errors == ()
    alike = _estimate(film, other)
    assert alike.scene_basis["clip+insert"] == "measured on 1 ultra render on this worker shape"
    assert alike.correction == 1.0 and abs(alike.expected_seconds - took) <= 0.2 * took

    # Its second render finished 6% over: the third pre-flight is corrected by that error alone, naming the render.
    again = _rendered(film, second, measured=_measured(film, costs={name: cost * 1.06 for name, cost in COSTS.items()}))
    third = _estimate(film, _rates_of(_History([again, rendered]), film, VIDEO))
    assert [item.render for item in third.errors] == [str(again.id)]
    assert third.correction == pytest.approx(again.measured["total_seconds"] / second.raw_seconds, rel=1e-3)
    assert f"priced from its video's own last render like this one - render `{str(again.id)[:8]}` - said" in third.line()


def test_a_render_s_parallel_efficiency_is_its_frames_time_over_its_pages_run():
    measured = {"pages": 8, "cpus": 9, "frames": 4000, "seconds": 1000.0, "kinds": {"plain": [3000, 5400.0], "insert": [1000, 1800.0]}}
    assert render_history.parallel_efficiency(measured) == pytest.approx(0.9)
    # Never past 1, never measured on frames read back drawn or on a render that timed none.
    assert render_history.parallel_efficiency({**measured, "seconds": 100.0}) == 1.0
    assert render_history.parallel_efficiency({**measured, "insert_frames": {"cached": 10, "drawn": 90}}) is None
    assert render_history.parallel_efficiency({"pages": 8, "seconds": 1000.0}) is None
    # Its scene types and stages price the next render of its video; too few frames price nothing.
    own = render_history.OwnRender.of("r1", "ultra", *UHD, 60_000, {**measured, "scenes": {"title": [4000, 7200.0]}})
    assert own is not None and own.efficiency == pytest.approx(0.9) and set(own.scene_rates) == {"title"}
    assert render_history.OwnRender.of("r1", "ultra", *UHD, 60_000, {**measured, "frames": 50, "scenes": {"title": [50, 90.0]}}) is None
    # Drawn on contended pages, priced for full ones at the rate full pages draw.
    contended = render_history.OwnRender.of("r2", "ultra", *UHD, 60_000, {**measured, "pages": 3, "scenes": {"title": [4000, 7200.0]}})
    full = render_preflight.with_own_render(Rates(1.0, contended=False), contended)
    assert full.scene_rates["title"] == pytest.approx(contended.scene_rates["title"] / render_preflight.DEFAULT_CONTENTION)


def test_a_scene_type_is_priced_at_what_the_renders_on_its_worker_shape_measured_first():
    preview = {"pages": 2, "cpus": 9, "frames": 300, "seconds": 30.0, "scenes": {"clip+insert": [300, 30.0]}}
    whole = {"pages": 8, "cpus": 9, "frames": 3000, "seconds": 2000.0, "scenes": {"clip+insert": [3000, 16230.0]}}
    rows = [
        SimpleNamespace(id=uuid.uuid4(), project_id=ANOTHER, quality="draft", width=1280, height=720, duration_ms=10_000, measured=preview, preflight=None),
        SimpleNamespace(id=uuid.uuid4(), project_id=ANOTHER, quality="ultra", width=UHD[0], height=UHD[1], duration_ms=50_000, measured=whole, preflight=None),
    ]
    pooled = asyncio.run(render_preflight.measured_rates(_History(rows), "ultra"))
    here = asyncio.run(render_preflight.measured_rates(_History(rows), "ultra", shape=Shape.of(8, 9, CEILING)))
    cost = render_preflight.PIXEL_COST["ultra"] * UHD[0] * UHD[1]
    assert here.scene_here == {"clip+insert": 1}
    assert cost / here.scene_rates["clip+insert"] == pytest.approx(5.41, rel=1e-3)
    # Pooled across qualities, the draft render's measure prices it - the platform's median across videos.
    assert cost / pooled.scene_rates["clip+insert"] < 2.0 and pooled.scene_here == {}
    assert render_preflight.priced_from(here, {"clip+insert"}) == "shape" and render_preflight.priced_from(pooled, {"clip+insert"}) == "platform"


# -- 2. The ETA switched to the pace its frames measure -----------------------------------


class _Drawing:
    """A render of 1,000 frames on 2 pages its pre-flight priced at 0.5 s a frame on a page, drawn at 1 s a frame."""

    def __init__(self):
        kinds = FrameKinds(fps=30.0, frames=1000, scenes=((0.0, 1000 / 30, "title"),))
        prior = page_schedule.FrameCosts({"plain": 0.5}, 0.0, {"plain": "video"})
        pieces = compositor.plan_segments(0, 1000, compositor.segment_frames(1000, 30), [])
        self.tracker = compositor.DrawProgress(total=1000, prior=prior)
        self.tracker.work = page_schedule.PageWork(pieces, 2, page_schedule.FramePrices(kinds, 0, 1000), self.tracker.costs())
        self.pieces = {page: self.tracker.work.take(page) for page in range(2)}
        self.clock = 0.0
        self.tracker.rate.note(self.clock, 0)

    def draw(self, each: int) -> None:
        for _frame in range(each):
            for page, piece in self.pieces.items():
                piece.next += 1
                self.tracker.timed("plain", 1.0, page, scene="title")
                self.tracker.done += 1
            self.clock += 1.0
            self.tracker.rate.note(self.clock, self.tracker.done)
        self.tracker.status(round(100 * self.tracker.done / self.tracker.total))


def _row(preflight: dict, *, status: str = "running", started: datetime | None = None) -> VideoRender:
    return VideoRender(
        id=uuid.uuid4(),
        project_id=VIDEO,
        revision=3,
        quality="ultra",
        status=status,
        progress=20,
        preflight=preflight,
        started_at=started or datetime.now(UTC) - timedelta(minutes=2),
    )


def test_a_render_whose_first_tenth_measures_twice_the_estimated_pace_switches_its_eta_to_the_measured_one(monkeypatch):
    # Its pre-flight: 1,000 frames at 0.5 s on each of 2 pages, 250 s, a minute of checks after them.
    expected = {"expected_seconds": 310, "check_seconds": 60, "model": render_preflight.ESTIMATE_MODEL}
    written = AsyncMock()
    monkeypatch.setattr(jobs, "mark", written)
    drawing = _Drawing()
    pacer = render.PaceNote("r1", expected)
    started = datetime.now(UTC) - timedelta(minutes=2)

    # Under a tenth of its frames drawn: the pre-flight's ETA stands, from its start.
    drawing.draw(20)
    assert drawing.tracker.pace() is None
    asyncio.run(pacer.note(drawing.tracker))
    assert written.await_count == 0
    eta = render_pace.eta_of(_row(expected, started=started))
    assert eta == {"source": "preflight", "at": (started + timedelta(seconds=310)).isoformat(), "expected_seconds": 310}

    # A tenth drawn, each frame twice what the pre-flight priced it at: the pace they measure is kept.
    drawing.draw(30)
    pace = drawing.tracker.pace()
    assert pace is not None and pace.drawn == 100 and pace.frames == 1000 and pace.per_second == pytest.approx(2.0)
    # The 900 frames left on 2 pages at the 1 s a frame measured: twice the 225 s the pre-flight priced them at.
    assert pace.seconds_left == pytest.approx(2 * 900 * 0.5 / 2, rel=0.05)
    asyncio.run(pacer.note(drawing.tracker))
    assert written.await_count == 1
    kept = written.await_args.kwargs["preflight"]
    assert kept["expected_seconds"] == 310 and kept["pace"]["drawn"] == 100 and kept["pace"]["after_seconds"] == 60
    frames_at = datetime.fromisoformat(kept["pace"]["frames_at"])
    assert datetime.fromisoformat(kept["pace"]["at"]) - frames_at == timedelta(seconds=60)
    assert (frames_at - datetime.fromisoformat(kept["pace"]["measured_at"])).total_seconds() == pytest.approx(pace.seconds_left)

    # The render card and get_video_jobs say the measured ETA, not the pre-flight's.
    row = _row(kept, started=started)
    eta = render_pace.eta_of(row)
    assert eta["source"] == "measured" and eta["at"] == kept["pace"]["at"] and eta["drawn"] == 100
    assert projects.serialize_render(row)["eta"] == eta
    said = render_preflight.said_of(row)
    assert said.startswith("Measured on its first 100 of 1,000 frames (2.0 frames/s over the last minute): its frames end about ")
    assert "UTC with its checks" in said and "this replaces the pre-flight's estimate of about 6 min" in said
    assert "The pre-flight expected" not in said

    # Written again every PACE_EVERY_SECONDS at most; a write that fails never stops the drawing.
    drawing.draw(10)
    asyncio.run(pacer.note(drawing.tracker))
    assert written.await_count == 1
    pacer.written = None
    monkeypatch.setattr(jobs, "mark", AsyncMock(side_effect=RuntimeError("database gone")))
    asyncio.run(pacer.note(drawing.tracker))

    # A finished render, a scene preview: no ETA. A new attempt measures its own pace: the pre-flight's stands again.
    assert render_pace.eta_of(_row(kept, status="ready")) is None
    assert render_pace.eta_of(SimpleNamespace(scene_id="s1", status="running", preflight=kept)) is None
    assert render_pace.eta_of(_row(render_pace.unpaced(kept), started=started))["source"] == "preflight"
    # Queued: the pre-flight's length, no time yet.
    assert render_pace.eta_of(_row(expected, status="pending")) == {"source": "preflight", "at": None, "expected_seconds": 310}


def test_a_finished_render_sets_each_scene_type_s_measure_against_its_estimate():
    film = _film()
    said = _estimate(film, _platform())
    rendered = _rendered(film, said)
    told = render_preflight.said_of(SimpleNamespace(preflight=rendered.preflight, measured=rendered.measured, quality="ultra"))
    priced = said.scene_raw_seconds["clip+insert"]
    assert priced == pytest.approx(1.2, rel=1e-3)
    assert f"clip with a screen insert 5.41 s (3,062 frames, against {priced:.2f} s estimated: x{5.41 / priced:.2f})" in told
    assert (
        "The next pre-flights at ultra price each of these scene types at what this render measured: those of this "
        "video at once, those of other videos on the same worker shape."
    ) in told
    assert "corrected by the errors of the last 5" in told
