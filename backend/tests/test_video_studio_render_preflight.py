"""What a whole render will take, said before it starts, against the worker that draws it. Synthetic rows only.

* the estimate is the plan's frames at the rate one page draws them, times the
  pages, each page counted for the CPUs it has - measured on this platform's
  last renders at that quality, else on the CI run - and says every input:
  the frames, the seconds a frame takes on a page and their source,
  the pages, the CPUs; the pages left at 0 are the CPUs less one;
* its memory, against the ceiling the worker publishes: the pages as many as
  the CPUs and the ceiling hold, the encoders as many as the ceiling holds
  beside them; past it even on one page and one encoder, a frame too slow for
  the stall window, or past the hours its quality may run (8 h for ultra, in
  segments) is a problem that refuses the render like the quality gate -
  unless this platform already rendered that quality whole, which is said;
* ``start_render`` answers the estimate and keeps it on the render, refuses on
  its problems, and ``force`` starts it anyway with the reasons recorded; an
  estimate that cannot be made never stops a render;
* only what the render draws is counted: the figures the reuse index
  serves are left out and said, and what a render measured says the frames it
  kept beside those it drew, so an attempt that resumed is never read as the
  whole render.
"""

from __future__ import annotations

import asyncio
import json
import math
import uuid
from types import SimpleNamespace

import pytest

from app.core.config import settings
from app.services.video_studio import jobs, projects, render_checks, render_preflight, render_process, reuse_index
from app.services.video_studio.spec import validate_video_spec

_GIB = 1 << 30
USER = SimpleNamespace(id=uuid.uuid4(), role="user")
PROJECT = SimpleNamespace(id=uuid.uuid4(), revision=3, title="Brand film", scene_previews=None)


@pytest.fixture(autouse=True)
def _worker(monkeypatch):
    monkeypatch.setattr(settings, "video_studio_render_pages", 2)
    monkeypatch.setattr(settings, "video_studio_render_stall_seconds", 120)
    monkeypatch.setattr(settings, "orchestration_lease_seconds", 60)


def test_a_30_second_ultra_render_is_its_frames_at_the_rate_its_pages_draw_them_in_ci():
    said = render_preflight.estimate(frames=1800, size=(3840, 2160), quality="ultra", cpus=4)
    assert said.measured == 0 and (said.pages, said.cpus) == (2, 4)
    # The slowest CI ultra run: 2.15 frames a second on two pages of a 2 vCPU runner, its other stages, and its
    # check stage watching 1,800 frames of 8.3 million pixels.
    check = render_preflight.DEFAULT_CHECK_PER_MEGAPIXEL_FRAME * 1800 * 3840 * 2160 / 1e6
    assert said.seconds == pytest.approx(1800 / 2.15 + 30 * render_preflight.DEFAULT_OTHER_PER_SECOND + check)
    assert said.minutes == 15 and said.check_seconds == pytest.approx(check)
    # Its memory: two pages of 4K at their measured peak, each encoder on 2 of the 4 CPUs, over the
    # processes' own - what a real ultra render of 30 s peaked at (6.4 GiB, the ultra render workflow).
    assert said.memory_bytes == render_preflight.memory_needed((3840, 2160), pages=2, x264_preset="slow", cpus=4)
    assert 6 * _GIB < said.memory_bytes < 7 * _GIB and said.ceiling_bytes is None and not said.problems
    line = said.line()
    # Every input it is made of: the frames, a frame's seconds on a page and where they come from, the pages.
    assert line.startswith(
        f"Expected to take about 15 min: 1,800 frames at 3840x2160, {said.frame_seconds:.2f} s a frame on each of 2 "
        "pages (4 CPUs) - the CI measurement: no render of this platform measured yet"
    )
    assert said.rate_source == "default" and said.frame_seconds == pytest.approx(2 / 2.15)
    # A figure is drawn first, one frame at a time at the output's size.
    assert render_preflight.estimate(frames=1800, size=(3840, 2160), figure_seconds=10, cpus=4).seconds > said.seconds


def test_pages_on_their_own_cpus_render_faster_and_a_page_without_one_does_not():
    # The 131 s brand film at ultra on Copilot's worker: 8 CPUs, 16 GiB for an ultra render.
    two = render_preflight.estimate(frames=7860, size=(3840, 2160), pages=2, cpus=8, ceiling=16 * _GIB)
    four = render_preflight.estimate(frames=7860, size=(3840, 2160), pages=8, cpus=8, ceiling=16 * _GIB)
    # 16 GiB holds 4 pages at 2160p, each with its CPU: twice as fast as 2 pages, well under an hour.
    assert four.pages == 4 and four.seconds < two.seconds * 0.6 and four.minutes < 45
    # More pages than CPUs draw no faster: the worker draws on as many pages as it has CPUs at most.
    crowded = render_preflight.estimate(frames=7860, size=(1920, 1080), quality="high", pages=16, cpus=8)
    eight = render_preflight.estimate(frames=7860, size=(1920, 1080), quality="high", pages=8, cpus=8)
    assert crowded.pages == eight.pages == 8 and crowded.seconds == pytest.approx(eight.seconds)
    # 0, the default: the CPUs less the one the worker's own jobs keep.
    auto = render_preflight.estimate(frames=7860, size=(1920, 1080), quality="high", pages=0, cpus=8)
    assert auto.pages == 7 and eight.seconds < auto.seconds < eight.seconds * 8 / 7 * 1.01
    assert render_preflight.auto_pages(1) == 1 and render_preflight.auto_pages(2) == 1


def test_the_pages_are_as_many_as_the_memory_ceiling_holds():
    # A page at 2160p holds its measured peak (``render_page_peak.json``): 3.7 GiB with no insert on screen,
    # its encoder on 8 threads - and what a layout holds it to, 20 % more.
    peak = render_preflight.page_peak((3840, 2160), "slow")
    assert render_preflight.page_memory((3840, 2160), "slow") == peak.plain and 3.25 * _GIB < peak.plain < 4 * _GIB
    drawn = render_preflight.layout((3840, 2160), x264_preset="slow", ceiling=16 * _GIB, pages=8, cpus=8)
    assert drawn.wanted == 8 and 1 < drawn.pages < 8
    budget = lambda pages: render_preflight.memory_budget((3840, 2160), pages=pages, x264_preset="slow", cpus=8)  # noqa: E731
    assert budget(drawn.pages) <= 16 * _GIB < budget(drawn.pages + 1)
    # 8 pages at 2160p would need far more than 16 GiB.
    assert render_preflight.memory_needed((3840, 2160), pages=8, x264_preset="slow", cpus=8) > 16 * _GIB
    # At 1080p every page fits; without a ceiling every page is drawn on.
    assert render_preflight.layout((1920, 1080), x264_preset="slow", ceiling=16 * _GIB, pages=8, cpus=8).pages == 8
    assert render_preflight.layout((3840, 2160), x264_preset="slow", ceiling=None, pages=8, cpus=8).pages == 8
    # Fewer pages than the worker has: said, never refused - the pages its fact, the memory that draws on every one
    # the platform's sizing, said apart.
    said = render_preflight.estimate(frames=7860, size=(3840, 2160), pages=8, cpus=8, ceiling=16 * _GIB)
    assert not said.problems and said.notes[0].startswith(f"It draws on {drawn.pages} pages at 3840x2160")
    [sizing] = said.infra
    assert f"holds {drawn.pages} of its 8 pages at 3840x2160" in sizing.text and "set VIDEO_STUDIO_ULTRA_RENDER_MEMORY_MB to" in sizing.text
    assert "VIDEO_STUDIO_ULTRA_RENDER_MEMORY_MB" not in said.line()


def test_an_ultra_render_runs_past_4_hours_in_segments_up_to_8_when_it_is_not_split():
    rates = render_preflight.Rates(render_preflight.DEFAULT_PAGE_RATE / 6)
    common = {"frames": 7860, "size": (3840, 2160), "pages": 2, "cpus": 2, "ceiling": 16 * _GIB, "rate": rates}
    long = render_preflight.estimate(**common, max_ranges=1)
    assert 4 * 3600 < long.seconds < 8 * 3600 and not long.problems and long.ranges == 1
    assert "in segments" in long.notes[0] and "a new attempt resumes from the last one" in long.notes[0]
    assert long.wall_seconds == 8 * 3600
    # Split (the default): in scene ranges, each within the 4 h.
    split = render_preflight.estimate(**common, scenes=19)
    assert split.ranges == 2 and split.range_seconds < 4 * 3600 and not split.problems
    assert split.expected_seconds < long.seconds and "it is drawn in 2 scene ranges" in split.notes[-1]
    # Another quality keeps its 4 h.
    high = render_preflight.estimate(
        frames=7860 * 12, size=(1920, 1080), quality="high", pages=1, cpus=1, ceiling=16 * _GIB, rate=rates, max_ranges=1
    )
    assert high.wall_seconds == 4 * 3600 and any("past the 4 h a render at high may run" in p for p in high.problems)


def test_what_would_stop_the_render_on_the_worker_refuses_it_until_that_quality_was_rendered_here():
    # A 1 GiB worker container: 60% of it for a render.
    small = render_preflight.estimate(frames=1800, size=(3840, 2160), ceiling=int(0.6 * _GIB))
    assert small.refuses and "even on one page (" in small.problems[0] and "past the 0.6 GiB" in small.problems[0]
    # Rendered whole at ultra here before: evidence beats the estimate - said, never refused.
    here = render_preflight.Rates(render_preflight.DEFAULT_PAGE_RATE, rendered_here=1)
    rendered = render_preflight.estimate(frames=1800, size=(3840, 2160), ceiling=int(0.6 * _GIB), rate=here)
    assert rendered.problems and not rendered.refuses
    # A page slower than the stall window, or past the 8 h an ultra render may run (not split).
    slow = render_preflight.estimate(
        frames=1800, size=(3840, 2160), rate=render_preflight.Rates(1e5, measured=3), max_ranges=1
    )
    assert any("waits for a render to move" in problem for problem in slow.problems)
    assert any("past the 8 h a render at ultra may run" in problem for problem in slow.problems)
    # What to give: the CPUs it needs, with their pages.
    assert slow.cpus_needed and any(
        f"the worker needs {slow.cpus_needed} CPUs" in problem and "VIDEO_STUDIO_RENDER_PAGES=0" in problem
        for problem in slow.problems
    )
    # The lease is renewed from a thread while the render moves: no frame, however slow, is past it.
    assert not any("lease" in problem for problem in slow.problems)


def test_a_render_short_of_the_worker_is_given_the_worker_never_a_lower_quality():
    # One page at ultra against a 1 GiB container's 0.6: what to give, in the two ways an administrator gives it -
    # the page's measured peak and its margin, never its peak alone, which the layout would refuse again.
    small = render_preflight.estimate(frames=1800, size=(3840, 2160), ceiling=int(0.6 * _GIB))
    one_page = render_preflight.memory_budget((3840, 2160), pages=1, x264_preset="slow", cpus=small.cpus)
    # An ultra render has a ceiling of its own: its setting, and a container that leaves the worker its own
    # jobs and its scene previews' reserve.
    assert f"set VIDEO_STUDIO_ULTRA_RENDER_MEMORY_MB to {math.ceil(one_page / (1 << 20) / 100) * 100} or more" in small.problems[0]
    assert render_process.memory_to_give(one_page, "ultra") in small.problems[0]
    assert "stay with the worker's own jobs and its scene previews" in small.problems[0]
    high = render_preflight.estimate(frames=1800, size=(1920, 1080), quality="high", ceiling=int(0.6 * _GIB))
    assert "give the worker's container" in high.problems[0] and "(60% of it goes to a render)" in high.problems[0]
    slow = render_preflight.estimate(
        frames=1800, size=(3840, 2160), ceiling=int(0.6 * _GIB), rate=render_preflight.Rates(1e5, measured=3)
    )
    for problem in slow.problems:
        assert "The video and its quality need no change" in problem or "move" in problem
        lowered = problem.lower()
        assert not any(word in lowered for word in ("draft", "standard", " high", "shorter", "lower quality")), problem


def test_a_draft_or_a_high_render_on_a_large_worker_has_nothing_to_say_but_its_duration():
    for quality, size in (("draft", (1280, 720)), ("high", (1920, 1080))):
        said = render_preflight.estimate(frames=3600, size=size, quality=quality, ceiling=8 * _GIB)
        assert said.problems == [] and not said.refuses and said.ceiling_bytes == 8 * _GIB
        assert "of the worker's 8.0 GiB ceiling" in said.line()


def test_every_quality_fits_the_ceiling_of_a_worker_without_a_container_limit():
    # 4096 MiB (``render_process.memory_ceiling`` without a limit): the 1080p presets fit it on every page.
    for quality, size in (("draft", (1280, 720)), ("standard", (1920, 1080)), ("high", (1920, 1080))):
        assert not render_preflight.estimate(frames=3600, size=size, quality=quality, ceiling=4 * _GIB).problems
    # Ultra does not: one 2160p page measured at 3.6 GiB beside the processes' base, past 4 GiB with its
    # margin - refused, saying the memory to give, before a render is stopped at the ceiling.
    ultra = render_preflight.estimate(frames=1800, size=(3840, 2160), ceiling=4 * _GIB)
    assert ultra.pages == 1 and ultra.refuses and ultra.memory_bytes < 4 * _GIB
    assert "even on one page" in ultra.problems[0] and "with its margin" in ultra.problems[0]
    # Its own ceiling, 16 GiB by default, draws it on its pages.
    assert not render_preflight.estimate(frames=1800, size=(3840, 2160), ceiling=16 * _GIB).problems


class _Rows:
    def __init__(self, rows):
        self.rows = rows

    async def execute(self, _statement, _params=None):
        return SimpleNamespace(all=lambda: list(self.rows))


def _row(quality: str, width: int, height: int, seconds: float, measured: dict | None):
    return (quality, width, height, int(seconds * 1000), measured)


def _drawn(frames: int, seconds: float, *, pages: int = 2, cpus: int = 4, other: float = 30.0) -> dict:
    return {"pages": pages, "cpus": cpus, "frames": frames, "seconds": seconds, "other_seconds": other}


def test_the_rate_is_what_one_page_drew_in_the_last_renders_measured_at_that_quality():
    rows = [
        _row("high", 1920, 1080, 60, _drawn(3600, 450)),  # 8 frames/s of 1080p on 2 pages
        _row("high", 1920, 1080, 60, _drawn(3600, 900)),  # a busy worker: never the estimate alone
        _row("draft", 1280, 720, 60, _drawn(1800, 60)),
        _row("high", 1920, 1080, 60, _drawn(3600, 450)),
        _row("high", 1920, 1080, 60, None),  # rendered before renders measured: evidence it renders, no rate
    ]
    rates = asyncio.run(render_preflight.measured_rates(_Rows(rows), "high"))
    assert (rates.measured, rates.measured_quality, rates.rendered_here) == (3, 3, 4)
    # Per page: 4 frames/s of 1080p.
    assert rates.page_rate == pytest.approx(8 * 1920 * 1080 / 2)
    assert rates.other_per_second == pytest.approx(0.5)
    # Nothing measured at standard: the other qualities' rates, each weighted by what its pixels cost.
    other = asyncio.run(render_preflight.measured_rates(_Rows(rows), "standard"))
    assert (other.measured, other.measured_quality, other.rendered_here) == (4, 0, 0)
    # Two pages that shared one CPU drew half as fast: the rate counts each for the CPU it lacked.
    lone = asyncio.run(render_preflight.measured_rates(_Rows([_row("high", 1920, 1080, 60, _drawn(3600, 900, cpus=1))]), "high"))
    assert lone.page_rate == pytest.approx(rates.page_rate)
    assert asyncio.run(render_preflight.measured_rates(_Rows([]), "ultra")) is None
    # Too few frames to measure a page by: no rate.
    assert asyncio.run(render_preflight.measured_rates(_Rows([_row("high", 1920, 1080, 2, _drawn(60, 9))]), "high")).measured == 0


def test_what_a_render_measured_and_what_it_was_expected_to_take_are_said():
    drawn = render_preflight.layout((3840, 2160), x264_preset="slow", ceiling=16 * _GIB, pages=8, cpus=8)
    measured = render_preflight.measurement(drawn, frames=7860, seconds=1800.4, figure_seconds=300, other_seconds=120)
    assert measured == {
        "pages": 4,
        "cpus": 8,
        "frames": 7860,
        "frames_reused": 0,
        "seconds": 1800.4,
        "figure_frames": 0,
        "figure_frames_reused": 0,
        "figure_seconds": 300.0,
        "other_seconds": 120.0,
    }
    said = render_preflight.estimate(frames=7860, size=(3840, 2160), pages=8, cpus=8, ceiling=16 * _GIB)
    render = SimpleNamespace(preflight=said.as_dict(), measured=measured)
    line = render_preflight.said_of(render)
    # Its estimate with every input it was made of, then what it measured.
    assert (
        f"The pre-flight expected about {said.minutes} min: 7,860 frames at 3840x2160, {said.frame_seconds:.2f} s a "
        "frame on each of 4 pages (8 CPUs) - the CI measurement: no render of this platform measured yet." in line
    )
    # Nothing kept is said too: a render never takes another render's frames.
    assert "It drew 7,860 frames on 4 pages (8 CPUs) in 31 min, 4.37 frames/s; no frame was kept from an earlier attempt." in line
    assert "more were kept" not in line and "Its figures" not in line
    assert render_preflight.said_of(SimpleNamespace()) == ""


def test_an_attempt_that_resumed_says_what_it_kept_beside_what_it_drew():
    # XTM Blast, 20 Oct 2026: 11,100 frames at 60 fps, the last attempt drew 3,298 in 16 min - the 7,802 others were
    # the segments an earlier attempt closed, never a render 3x faster than its estimate.
    drawn = render_preflight.layout((3840, 2160), x264_preset="slow", ceiling=16 * _GIB, pages=8, cpus=8)
    measured = render_preflight.measurement(
        drawn,
        frames=3298,
        frames_reused=7802,
        seconds=960,
        figure_frames=240,
        figure_frames_reused=1860,
        figures=[{"scene_id": "s05", "inputs": "a" * 40, "key": "k" * 64, "drawn": 0, "reused": 1860}],
    )
    assert (measured["frames"], measured["frames_reused"]) == (3298, 7802)
    assert (measured["figure_frames"], measured["figure_frames_reused"]) == (240, 1860)
    assert measured["figures"][0]["inputs"] == "a" * 40
    line = render_preflight.said_of(SimpleNamespace(measured=measured))
    assert "It drew 3,298 frames on 4 pages (8 CPUs) in 16 min, 3.44 frames/s; 7,802 more were kept from the " in line
    assert "Its figures: 240 frames drawn, 1,860 served by drawings kept by an earlier render or attempt." in line
    # Every frame kept: what was kept is still said.
    whole = render_preflight.measurement(drawn, frames=0, frames_reused=11100, seconds=0)
    assert "It drew 0 frames on 4 pages (8 CPUs); 11,100 more were kept" in render_preflight.said_of(SimpleNamespace(measured=whole))
    # Drawn in scene ranges: what the other ranges' workers drew is theirs, never kept by this one.
    ranged = {**render_preflight.measurement(drawn, frames=2000, seconds=600), "ranges": 3, "frames_joined": 6000}
    said = render_preflight.said_of(SimpleNamespace(measured=ranged))
    assert "It drew 2,000 frames on 4 pages (8 CPUs) in 10 min, 3.33 frames/s; no frame was kept from an earlier attempt; 6,000 were drawn by the workers of" in said
    assert "more were kept" not in said


def test_the_figures_an_earlier_render_kept_are_left_out_of_the_estimate_and_said():
    full = render_preflight.estimate(frames=11100, size=(3840, 2160), figure_seconds=60, pages=8, cpus=8, ceiling=16 * _GIB)
    served = reuse_index.Served(figures=3, served=2, seconds=40)
    reused = render_preflight.estimate(
        frames=11100, size=(3840, 2160), figure_seconds=60, pages=8, cpus=8, ceiling=16 * _GIB, reuse=served
    )
    # 40 s of figures at 60 fps, drawn on one page at FIGURE_SPEED, are no longer counted; the composed frames are.
    per_page = render_preflight.frames_per_second(
        render_preflight.Rates(render_preflight.DEFAULT_PAGE_RATE), size=(3840, 2160), quality="ultra", pages=1, cpus=8
    )
    assert full.seconds - reused.seconds == pytest.approx(2400 / (per_page * render_preflight.FIGURE_SPEED), rel=1e-3)
    assert reused.frames == full.frames == 11100
    assert (reused.figures, reused.figures_reused, reused.figure_frames_reused) == (3, 2, 2400)
    assert "2 of its 3 figures (2,400 frames) are kept from an earlier render and not drawn again." in reused.line()
    assert "kept from an earlier render" not in full.line()
    kept = json.loads(json.dumps(reused.as_dict()))
    assert (kept["figures"], kept["figures_reused"], kept["figure_frames_reused"]) == (3, 2, 2400)
    said = render_preflight.said_of(SimpleNamespace(preflight=kept))
    assert ", 2 of its 3 figures kept from an earlier render." in said
    # Never more than the figures there are.
    over = reuse_index.Served(figures=1, served=1, seconds=600)
    assert render_preflight.estimate(frames=11100, size=(3840, 2160), figure_seconds=60, reuse=over).figure_frames_reused == 3600


class _Redis:
    def __init__(self):
        self.values: dict = {}

    async def set(self, key, value, ex=None):
        self.values[key] = value

    async def get(self, key):
        return self.values.get(key)


def test_the_worker_publishes_what_it_holds_a_render_to_and_the_web_reads_it(monkeypatch):
    redis = _Redis()

    async def client():
        return redis

    monkeypatch.setattr("app.core.redis.redis_client", client)
    monkeypatch.setattr(settings, "video_studio_render_memory_mb", 3072)
    monkeypatch.setattr(render_preflight, "_published_at", [])
    monkeypatch.setattr("app.services.video_studio.resources.cpu_budget", lambda: 8)
    asyncio.run(render_preflight.publish_worker())
    published = asyncio.run(render_preflight.worker_capacity())
    # The CPUs its quota grants, which its pages share.
    assert published["memory_ceiling"] == 3072 << 20 and published["pages"] == 2 and published["cpus"] == 8
    # At most every few minutes.
    redis.values.clear()
    asyncio.run(render_preflight.publish_worker())
    assert redis.values == {}


def _spec(seconds: float = 30) -> dict:
    return validate_video_spec(
        {
            "title": "Ultra",
            "freestyle": True,
            "scenes": [{"id": "open", "type": "title", "title": "Hello", "duration": seconds}],
        }
    )


class _Session:
    """The start's reads: no render in flight, no history; ``begin_nested`` a savepoint that does nothing."""

    def __init__(self):
        self.added = []

    async def execute(self, _statement, _params=None):
        return SimpleNamespace(scalars=lambda: SimpleNamespace(all=lambda: []), all=lambda: [])

    async def get(self, *_args, **_kwargs):
        return None

    def begin_nested(self):
        session = self

        class _Savepoint:
            async def __aenter__(self):
                return session

            async def __aexit__(self, *_exc):
                return False

        return _Savepoint()

    def add(self, row):
        self.added.append(row)

    async def flush(self):
        return None


@pytest.fixture
def starting(monkeypatch):
    world = SimpleNamespace(audits=[], ceiling=None)

    async def no_reasons(*_args, **_kwargs):
        return []

    async def effective(*_args, **_kwargs):
        return SimpleNamespace(spec=_spec(15))

    async def capacity():
        return {"memory_ceiling": world.ceiling, "pages": 2} if world.ceiling else None

    async def none(*_args, **_kwargs):
        return []

    async def nothing(*_args, **_kwargs):
        return None

    async def audit(_db, **kwargs):
        world.audits.append(kwargs)

    monkeypatch.setattr(render_checks, "gate", no_reasons)
    monkeypatch.setattr(projects, "effective_spec", effective)
    monkeypatch.setattr(render_preflight, "worker_capacity", capacity)
    monkeypatch.setattr(jobs, "renders_in_flight", none)
    monkeypatch.setattr(jobs, "enqueue", nothing)
    monkeypatch.setattr("app.services.audit.log_audit", audit)
    return world


def test_an_ultra_render_a_small_worker_cannot_hold_is_refused_and_force_records_why(starting):
    starting.ceiling = int(0.6 * _GIB)
    db = _Session()
    with pytest.raises(render_checks.RenderGateError) as refused:
        asyncio.run(projects.start_render(db, PROJECT, USER, quality="ultra"))
    [reason] = refused.value.reasons
    assert reason.code == "preflight" and "the worker holds a render to" in reason.message and db.added == []
    started = asyncio.run(projects.start_render(_Session(), PROJECT, USER, quality="ultra", force=True))
    assert [r["code"] for r in started.render.gate_override["reasons"]] == ["preflight"]
    assert started.preflight.frames == 15 * 60 and started.preflight.size == (3840, 2160)


def test_a_render_the_worker_can_hold_starts_with_its_estimate_kept_on_it(starting):
    starting.ceiling = 16 * _GIB
    started = asyncio.run(projects.start_render(_Session(), PROJECT, USER, quality="ultra"))
    assert started.render.gate_override is None and started.preflight.problems == []
    assert json.loads(json.dumps(started.preflight.as_dict()))["memory_ceiling_bytes"] == 16 * _GIB
    # Kept on the render: get_video_jobs says it while it runs.
    assert started.render.preflight == started.preflight.as_dict() and started.render.preflight["pages"] == 2


def _figure_spec() -> dict:
    block = {
        "type": "architecture",
        "nodes": [{"id": "a", "label": "OpenCTI", "row": 1, "col": 1}, {"id": "b", "label": "OpenAEV", "row": 1, "col": 2}],
        "edges": [{"source": "a", "target": "b"}],
    }
    return validate_video_spec(
        {
            "title": "Platform",
            "freestyle": True,
            "scenes": [
                {"id": "open", "type": "title", "title": "Hello", "duration": 5},
                {"id": "arch", "type": "figure", "block_spec": block, "duration": 12},
            ],
        }
    )


def test_a_start_leaves_out_the_figures_the_reuse_index_serves(starting, monkeypatch):
    starting.ceiling = 16 * _GIB
    asked: dict = {}

    async def figure_spec(*_args, **_kwargs):
        return SimpleNamespace(spec=_figure_spec())

    async def served(_db, _effective, **kwargs):
        asked.update(kwargs)
        return reuse_index.Served(figures=1, served=1, seconds=kwargs["lengths"]["arch"])

    monkeypatch.setattr(projects, "effective_spec", figure_spec)
    monkeypatch.setattr(reuse_index, "served", served)
    started = asyncio.run(projects.start_render(_Session(), PROJECT, USER, quality="ultra"))
    # Read for this video, as the person who renders it (the voice that times the figures is theirs).
    assert asked["video_id"] == str(PROJECT.id) and asked["user"] is USER
    # At the size and frame rate the render draws: a drawing at that variant, or sharper, serves it.
    assert asked["size"] == (3840, 2160) and asked["fps"] == 60 and set(asked["lengths"]) == {"arch"}
    assert started.preflight.figures_reused == 1 and started.render.preflight["figures_reused"] == 1
    assert "1 of its 1 figure" in started.preflight.line()

    # An index that cannot be read counts every figure, and never stops the start.
    async def broken(*_args, **_kwargs):
        raise RuntimeError("the index could not be read")

    monkeypatch.setattr(reuse_index, "served", broken)
    counted = asyncio.run(projects.start_render(_Session(), PROJECT, USER, quality="ultra"))
    assert (counted.preflight.figures, counted.preflight.figures_reused) == (1, 0)
    assert counted.preflight.seconds > started.preflight.seconds


def test_each_quality_says_its_limits_before_a_render_is_asked(monkeypatch):
    async def capacity():
        # Copilot's worker: 8 CPUs, VIDEO_STUDIO_RENDER_MEMORY_MB=16384 for every quality.
        ceilings = {quality: 16 * _GIB for quality in ("draft", "standard", "high", "ultra")}
        return {"pages": 8, "cpus": 8, "memory_ceilings": ceilings}

    monkeypatch.setattr(render_preflight, "worker_capacity", capacity)
    limits = asyncio.run(render_preflight.limits(_Session()))
    ultra, high = limits["ultra"], limits["high"]
    assert (ultra["output"], ultra["pages"], ultra["cpus"], ultra["memory_mb"]) == ("2160p 60 fps", 4, 8, 16384)
    assert ultra["max_render_hours"] == 8 and high["max_render_hours"] == 4 and high["pages"] == 8
    # The 131 s brand film fits at ultra; the longest video a render produces bounds the rest.
    assert 131 < ultra["max_duration_s"] <= settings.video_studio_max_video_seconds
    assert ultra["rate_from"] == "the CI measurement: no render of this platform measured yet"
    # In scene ranges each on a worker of its own: longer than one worker draws in its 8 h.
    assert ultra["max_ranges"] == settings.video_studio_render_max_ranges and ultra["frame_seconds"] > 0


def test_an_estimate_that_cannot_be_made_never_stops_a_render(starting, monkeypatch):
    async def broken(*_args, **_kwargs):
        raise RuntimeError("the plan could not be read")

    monkeypatch.setattr(render_preflight, "preflight", broken)
    started = asyncio.run(projects.start_render(_Session(), PROJECT, USER, quality="ultra"))
    assert started.preflight is None and started.render.gate_override is None
