"""The five render pipeline gaps of 21 Oct 2026, each held where it is decided. No browser, no database.

* a page's memory is its measured peak (``render_preflight.PAGE_PEAK_FILE``) and its margin, scaled by its pixels,
  its encoder's threads and the inserts it shows; the layout opens as many pages as that budget fits, each encoder
  on its page's share of the CPUs; a frame goes to its encoder a chunk at a time, a page's caches and the render
  process's freed heap are released when a scene ends; the answer says the peak, the pages opened and the time on
  them against the time on every page; a render longer than the split on the pages it opens is drawn across the
  workers there;
* the estimate draws each frame at what a page measured for its kind, adds its inserts' preparation and its check
  stage, plans its narrations at their spoken length, counts contended pages apart and is corrected by the errors
  of its last estimates;
* a held clip shorter than its narration is a finding at save that refuses the render;
* an insert's crops are cut where its scene is on screen alone, and the answer says so;
* an idle render slot takes the preview lane's next preview, dealt round-robin by video.
"""

from __future__ import annotations

import asyncio
import dataclasses
import math
import uuid
from types import SimpleNamespace

import pytest

from app.core.config import settings
from app.models.video_studio import JOB_READY
from app.services import orchestration_reconciler as reconciler
from app.services.video_studio import (
    compositor,
    encoder,
    insert_checks,
    jobs,
    preview_lane,
    quality,
    render_checks,
    render_history,
    render_preflight,
    render_process,
    resources,
    timeline,
)
from app.services.video_studio.spec import parse_video_spec, validate_video_spec

_GIB = 1 << 30
UHD = (3840, 2160)
CLIP_ID = "00000000-0000-0000-0000-00000000c257"


@pytest.fixture(autouse=True)
def _worker(monkeypatch):
    monkeypatch.setattr(settings, "video_studio_render_pages", 0)
    monkeypatch.setattr(settings, "video_studio_render_stall_seconds", 120)
    monkeypatch.setattr(settings, "video_studio_render_split_minutes", 30)


# ── 1. A page's memory, measured ──────────────────────────────────────────


def test_a_2160p_page_is_held_to_its_measured_peak_and_its_margin():
    peak = render_preflight.page_peak(UHD, "slow")
    assert peak is not None and peak.size == UHD and peak.measured and peak.fixture
    load = peak.inserts
    # At the size, the inserts and the threads it was measured with: the measured peak, and 20 % over it.
    assert render_preflight.page_memory(UHD, "slow", load, threads=peak.threads) == peak.peak
    assert render_preflight.page_budget(UHD, "slow", load, threads=peak.threads) == math.ceil(peak.peak * 1.2)
    # With no insert on screen, the peak measured with none.
    assert render_preflight.page_memory(UHD, "slow", None, threads=peak.threads) == peak.plain
    # An encoder on fewer threads holds less, one on more holds more: 80 MiB a thread at 2160p.
    fewer = render_preflight.page_memory(UHD, "slow", load, threads=1)
    assert peak.peak - fewer == (peak.threads - 1) * UHD[0] * UHD[1] * render_preflight.ENCODER_THREAD_BYTES_PER_PIXEL
    # An insert more than measured is counted as one is modelled; a square video holds its share of the pixels.
    more = render_preflight.InsertLoad(load.videos + 1, load.pictures)
    assert render_preflight.page_memory(UHD, "slow", more, threads=peak.threads) > peak.peak
    assert render_preflight.page_memory((2160, 2160), "slow", load, threads=peak.threads) < peak.peak
    # 1080p and the previews' qualities were not measured: modelled, as before.
    assert render_preflight.page_peak((1920, 1080), "slow") is None
    assert render_preflight.page_peak((1280, 720), "veryfast") is None


def test_the_layout_opens_the_pages_their_budget_fits_each_encoder_on_its_share():
    load = render_preflight.page_peak(UHD, "slow").inserts
    drawn = render_preflight.layout(UHD, x264_preset="slow", ceiling=16 * _GIB, cpus=8, inserts=load)
    assert drawn.wanted == 7
    needed = render_preflight.memory_budget(UHD, pages=drawn.pages, x264_preset="slow", inserts=load, cpus=8)
    beyond = render_preflight.memory_budget(UHD, pages=drawn.pages + 1, x264_preset="slow", inserts=load, cpus=8)
    assert needed <= 16 * _GIB < beyond
    assert needed == render_preflight.PROCESS_BASE + drawn.pages * render_preflight.page_budget(
        UHD, "slow", load, threads=render_preflight.encoder_threads(drawn.pages, 8)
    )
    # Its share of the CPUs, never every CPU of the worker.
    assert render_preflight.encoder_threads(3, 8) == 2 and render_preflight.encoder_threads(8, 8) == 1
    assert render_preflight.encoder_threads(1, 8) == 8 and render_preflight.encoder_threads(16, 8) == 1
    assert encoder.x264_args(crf=15, preset="slow", fps=60, threads=2)[3] == "2"


def test_the_pre_flight_says_the_page_peak_the_pages_opened_and_both_durations():
    peak = render_preflight.page_peak(UHD, "slow")
    said = render_preflight.estimate(
        frames=7317, size=UHD, quality="ultra", cpus=8, ceiling=16 * _GIB, inserts=peak.inserts, workers=1
    )
    assert said.pages < said.wanted_pages == 7
    assert said.full_pages_seconds is not None and said.full_pages_seconds < said.seconds
    assert said.page_peak_bytes > 0 and said.page_budget_bytes == math.ceil(said.page_peak_bytes * 1.2)
    # The render's own pages are its fact; the time on every page and the memory to give, the platform's sizing.
    note = said.notes[0]
    assert note.startswith(f"It draws on {said.pages} pages at 3840x2160, as many as its memory ceiling holds")
    assert "each at its peak" in note and "measured" in note and "with its margin" in note
    [sizing] = said.infra_once()
    assert sizing.startswith(f"The worker's memory ceiling (16.0 GiB) holds {said.pages} of its 7 pages at 3840x2160")
    assert f"on {said.pages} pages, against about" in sizing and "on all 7" in sizing
    assert sizing not in said.line()
    # Every page open: the peak is said all the same.
    roomy = render_preflight.estimate(frames=7317, size=UHD, quality="ultra", cpus=8, ceiling=64 * _GIB, workers=1)
    assert roomy.pages == 7 and "It draws on all 7 of the worker's pages: each page peaks at about" in roomy.line()
    kept = roomy.as_dict()
    assert kept["page_peak_bytes"] == roomy.page_peak_bytes and kept["wanted_pages"] == 7 and kept["model"] == render_preflight.ESTIMATE_MODEL


def test_a_render_longer_than_the_split_on_its_pages_is_drawn_across_the_workers_there():
    slow = render_preflight.Rates(render_preflight.DEFAULT_PAGE_RATE / 4)
    common = {"frames": 7317, "size": UHD, "quality": "ultra", "cpus": 8, "ceiling": 16 * _GIB, "rate": slow, "scenes": 19}
    alone = render_preflight.estimate(**common, workers=1)
    assert alone.seconds > 30 * 60 and alone.ranges == 1
    shared = render_preflight.estimate(**common, workers=3)
    assert shared.ranges > 1 and shared.split_reason == "split" and shared.ranges <= 3
    assert shared.range_seconds < alone.seconds and "VIDEO_STUDIO_RENDER_SPLIT_MINUTES" in shared.notes[-1]
    assert "with 3 workers there" in shared.notes[-1]
    # 0 splits only past the 4 h a render may run.
    settings.video_studio_render_split_minutes = 0
    assert render_preflight.estimate(**common, workers=3).ranges == 1


async def test_a_frame_goes_to_its_encoder_a_chunk_at_a_time():
    written: list[int] = []

    class _Stdin:
        def write(self, data):
            written.append(len(data))

        async def drain(self):
            return None

    pipe = encoder.FramePipeEncoder(out_path="seg.mp4", fps=60, frame_format="png", crf=15, preset="slow", width=3840, height=2160, threads=2)
    pipe._process = SimpleNamespace(stdin=_Stdin())
    frame = bytes(5 * (1 << 20) + 17)
    await pipe.write(frame)
    assert sum(written) == len(frame) and max(written) <= 1 << 20 and pipe.frames_written == 1


async def test_a_page_and_its_frame_server_release_what_an_ended_scene_held(monkeypatch):
    sent: list[str] = []

    class _Cdp:
        async def send(self, method, params=None):
            sent.append(method)
            if method.startswith("Memory."):
                raise RuntimeError("not offered by this renderer")
            return {}

    page = compositor._Page(0, compositor.FrameServer(compositor.CompositorAssets(), ""))
    page.cdp = _Cdp()
    # One the renderer does not offer is skipped: the render goes on.
    await compositor._release_page_caches(page)
    assert sent == ["HeapProfiler.collectGarbage", "Memory.simulatePressureNotification"]
    trimmed: list[bool] = []
    monkeypatch.setattr(resources, "release_freed_memory", lambda: trimmed.append(True) or True)
    await page.server.release(["clip/s1"])
    assert trimmed == [True]
    # A render process - a preview's too - starts with two glibc heaps unless the worker's environment sets its own.
    monkeypatch.delenv("MALLOC_ARENA_MAX", raising=False)
    assert render_process.process_env(preview=False)["MALLOC_ARENA_MAX"] == "2"
    assert render_process.process_env(preview=True)["MALLOC_ARENA_MAX"] == "2"
    monkeypatch.setenv("MALLOC_ARENA_MAX", "4")
    assert render_process.process_env(preview=False)["MALLOC_ARENA_MAX"] == "4"


# ── 2. The estimate, by what the frames show ──────────────────────────────


def _document(*scenes, fps=60, fade=0.0) -> dict:
    """A planned document of *scenes* ``(start, end, transition in, [(t0, t1) inserts])``."""
    planned = [
        {
            "id": f"s{index}",
            "type": "clip",
            "start": start,
            "end": end,
            "transition_in": {"type": "crossfade" if tin else "cut", "duration": tin},
            "inserts": [{"src": f"insert/s{index}.{n}", "t0": t0, "t1": t1, "kind": "video"} for n, (t0, t1) in enumerate(inserts)],
        }
        for index, (start, end, tin, inserts) in enumerate(scenes)
    ]
    duration = max(end for _start, end, _tin, _inserts in scenes)
    return {"fps": fps, "frames": round(duration * fps), "duration": duration, "fade_in": fade, "fade_out": fade, "scenes": planned}


def test_a_frame_is_of_the_kind_of_what_it_shows():
    # An insert from 1 s to 5.8 s, the next scene's transition in from 5.4 s to 6 s.
    document = _document((0.0, 6.0, 0.0, [(1.0, 5.8)]), (5.4, 10.0, 0.6, []), fps=10)
    kinds = render_preflight.FrameKinds.of(document)
    assert [kinds.kind(n) for n in (0, 20, 55, 59, 70)] == ["plain", "insert", "transition_insert", "transition", "plain"]
    assert kinds.counts() == {"plain": 50, "transition": 2, "insert": 44, "transition_insert": 4}


def _drawn_kinds(plain, insert, transition=None):
    kinds = {"plain": [plain[0], plain[1]], "insert": [insert[0], insert[1]]}
    if transition:
        kinds["transition"] = list(transition)
    return kinds


def test_each_kind_of_frame_is_drawn_at_what_a_page_measured_for_it():
    # 600 plain frames at 0.1 s a page, 400 insert frames at 2 s: a page measured for both.
    measured = render_preflight.measurement(
        render_preflight.Layout(pages=2, cpus=4, wanted=3),
        frames=1000,
        seconds=430,
        kinds=_drawn_kinds((600, 60.0), (400, 800.0)),
        stages={"media": 10.0, "inserts": 40.0, "frames": 430.0, "checks": 60.0, "audio": 5.0, "mux": 2.0, "store": 3.0},
        total_seconds=550.0,
        insert_seconds=20.0,
    )
    rows = [("ultra", 3840, 2160, 16_667, measured, None)]
    rates = asyncio.run(render_preflight.measured_rates(_Rows(rows), "ultra"))
    assert set(rates.kind_rates) == {"plain", "insert"}
    assert rates.kind_rates["plain"] == pytest.approx(rates.kind_rates["insert"] * 20)
    # Its stages, each per its own unit: an insert's second on screen, a frame of a million pixels, a second of video.
    assert rates.insert_per_second == pytest.approx(2.0)
    assert rates.check_per_megapixel_frame == pytest.approx(60.0 / (1000 * 3840 * 2160 / 1e6))
    assert rates.other_per_second == pytest.approx(20.0 / 16.667, rel=1e-3)
    few_inserts = {"plain": 900, "insert": 100}
    many_inserts = {"plain": 100, "insert": 900}
    common = {"frames": 1000, "size": UHD, "quality": "ultra", "pages": 2, "cpus": 4, "rate": rates}
    light = render_preflight.estimate(**common, kinds=few_inserts)
    heavy = render_preflight.estimate(**common, kinds=many_inserts, insert_seconds=15)
    # An average over the frames says the same of both; by kind, the insert-heavy one takes far longer.
    average = render_preflight.estimate(**common)
    assert light.seconds < average.seconds < heavy.seconds
    assert heavy.kind_seconds["insert"] == pytest.approx(heavy.kind_seconds["plain"] * 20)
    assert heavy.insert_seconds == pytest.approx(30.0) and heavy.check_seconds == pytest.approx(60.0)
    line = heavy.line()
    assert "Of its 1,000 frames, 100 plain and 900 with a screen insert (" in line
    assert "for its inserts' preparation" in line and "for its checks" in line


def test_a_render_of_before_its_stages_were_measured_is_never_counted_twice():
    lumped = {"pages": 2, "cpus": 4, "frames": 3600, "seconds": 450, "other_seconds": 30.0}
    rates = asyncio.run(render_preflight.measured_rates(_Rows([("high", 1920, 1080, 60_000, lumped, None)]), "high"))
    # Its checks and its inserts' preparation are in its other stages: no term of their own on top.
    assert rates.other_per_second == pytest.approx(0.5)
    assert rates.insert_per_second == 0.0 and rates.check_per_megapixel_frame == 0.0


def test_contended_pages_draw_at_the_rate_contended_pages_measured():
    rates = render_preflight.Rates(render_preflight.DEFAULT_PAGE_RATE)
    assert render_preflight.contended(3, 8) and not render_preflight.contended(4, 8)
    full = render_preflight.frames_per_second(rates, size=UHD, quality="ultra", pages=4, cpus=8) / 4
    few = render_preflight.frames_per_second(rates, size=UHD, quality="ultra", pages=3, cpus=8) / 3
    # Measured on full pages only: 3 pages of 8 CPUs at DEFAULT_CONTENTION of their rate.
    assert few == pytest.approx(full * render_preflight.DEFAULT_CONTENTION)
    # Measured on contended pages: their own rate, the factor going the other way for full ones.
    measured_contended = render_preflight.Rates(render_preflight.DEFAULT_PAGE_RATE, contended=True)
    assert render_preflight.frames_per_second(measured_contended, size=UHD, quality="ultra", pages=3, cpus=8) / 3 == pytest.approx(full)
    # The renders read are those of the regime asked when any was measured in it.
    row = lambda pages, cpus, seconds: ("ultra", 3840, 2160, 30_000, {"pages": pages, "cpus": cpus, "frames": 1800, "seconds": seconds}, None)  # noqa: E731
    rows = [row(3, 8, 900), row(7, 8, 300)]
    asked = asyncio.run(render_preflight.measured_rates(_Rows(rows), "ultra", contended_pages=True))
    assert asked.contended is True and asked.measured == 1
    other = asyncio.run(render_preflight.measured_rates(_Rows(rows), "ultra", contended_pages=False))
    assert other.contended is False and other.page_rate > asked.page_rate


def test_the_estimate_is_corrected_by_the_errors_of_the_last_ones_and_says_them():
    # Read per worker shape: the pages, the CPUs and the memory ceiling it is estimated for.
    shape = render_history.Shape.of(3, 8, 16 << 30)

    def row(said, took, *, on=shape, drew_on=None, costs_from="platform"):
        pages, cpus = drew_on or (on.pages, on.cpus)
        measured = {"pages": pages, "cpus": cpus, "frames": 7714, "seconds": took - 60, "total_seconds": took}
        recorded = {"model": render_preflight.ESTIMATE_MODEL, "raw_seconds": said, "shape": on.as_dict(), "costs_from": costs_from}
        return (uuid.uuid4(), measured, recorded)

    rows = [row(1800, 2820), row(1200, 1500), row(900, 1000)]
    errors = asyncio.run(render_history.estimate_errors(_ErrorRows(rows), "ultra", shape=shape, costs_from="platform"))
    assert [(item.said, item.took) for item in errors] == [(1800, 2820), (1200, 1500), (900, 1000)]
    assert [item.render for item in errors] == [str(found[0]) for found in rows]
    rates = render_preflight.Rates(render_preflight.DEFAULT_PAGE_RATE, errors=errors)
    assert rates.correction == pytest.approx(1500 / 1200)
    raw = render_preflight.estimate(frames=7317, size=UHD, quality="ultra", pages=3, cpus=8, ceiling=16 << 30, rate=dataclasses.replace(rates, errors=()))
    said = render_preflight.estimate(frames=7317, size=UHD, quality="ultra", pages=3, cpus=8, ceiling=16 << 30, rate=rates)
    assert said.seconds == pytest.approx(raw.seconds * 1.25) and said.raw_seconds == pytest.approx(raw.seconds)
    named = ", ".join(f"`{str(found[0])[:8]}`" for found in rows[:2]) + f" and `{str(rows[2][0])[:8]}`"
    assert (
        "Its last 3 ultra estimates on this worker shape (3 pages of 8 CPUs, a 16.0 GiB memory ceiling), priced from the "
        f"platform's renders like this one - renders {named} - said 30 min, 20 min and 15 min for renders that took 47 "
        "min, 25 min and 17 min: this one is corrected by x1.25, their median."
    ) in said.line()
    assert said.as_dict()["errors"][0] == [1800, 2820, str(rows[0][0])]
    assert said.as_dict()["shape"] == {"pages": 3, "cpus": 8, "memory_mib": 16384}
    # Uncorrected on a shape no estimate was followed by a render: said so.
    assert "Not corrected: no earlier ultra estimate of this model on this worker shape (3 pages of 8 CPUs" in raw.line()
    # The last 5 of them, not the last one.
    many = [row(600 + 60 * index, 1200) for index in range(7)]
    assert len(asyncio.run(render_history.estimate_errors(_ErrorRows(many), "ultra", shape=shape, costs_from="platform"))) == 5
    # Another worker shape (its pages, its memory), a render drawn on other pages than estimated, an estimate made from
    # its video's own render, another model, a render that kept frames: none is an error of this one.
    other_model = row(1800, 2820)
    other_model[2]["model"] = render_preflight.ESTIMATE_MODEL - 1
    kept = row(1800, 2820)
    kept[1]["frames_reused"] = 300
    strangers = [
        row(1800, 2820, on=render_history.Shape.of(4, 8, 16 << 30)),
        row(1800, 2820, on=render_history.Shape.of(3, 8, 32 << 30)),
        row(1800, 2820, drew_on=(2, 8)),
        row(1800, 2820, costs_from="video"),
        other_model,
        kept,
    ]
    assert asyncio.run(render_history.estimate_errors(_ErrorRows(strangers), "ultra", shape=shape, costs_from="platform")) == ()
    # Never past its bounds.
    wild = render_preflight.Rates(1.0, errors=((60, 6000),))
    assert wild.correction == render_preflight.CORRECTION_BOUNDS[1]
    # A finished render says what its own estimate missed by.
    _id, measured, preflight = row(1800, 2820)
    told = render_preflight.said_of(SimpleNamespace(preflight={**preflight, "expected_seconds": 1800}, measured=measured))
    assert "It took 47 min against the 30 min said (+57%)" in told
    assert "corrected by the errors of the last 5" in told


def test_a_pre_flight_reads_the_kinds_and_the_inserts_off_the_spec_its_plan_does_not_place():
    spec = parse_video_spec(
        validate_video_spec(
            {
                "title": "Film",
                "freestyle": True,
                "default_transition": {"type": "crossfade", "duration": 0.6},
                "scenes": [
                    {"id": "a", "type": "title", "title": "Hello", "duration": 4},
                    {
                        "id": "b",
                        "type": "clip",
                        "clip_id": CLIP_ID,
                        "overlays": [{"type": "screen_insert", "source": {"clip_id": CLIP_ID}, "quad": [[0.1, 0.1], [0.5, 0.1], [0.5, 0.5], [0.1, 0.5]], "start": 1.0, "duration": 2.0}],
                    },
                ],
            }
        )
    )
    plan = timeline.estimate_plan(spec, {}, {"b": timeline.ClipMedia(6.0, 1920, 1080, 24.0, 144)})
    placed, inserted = render_preflight.document_kinds(spec, plan.document, 60)
    kinds = placed.counts()
    assert inserted == pytest.approx(2.0) and kinds["insert"] == 120
    assert sum(kinds.values()) == math.ceil(plan.duration * 60) and kinds["transition"] > 0
    # Its narration at the length it was spoken, where it was: the frames the render draws.
    narrated = parse_video_spec(validate_video_spec({"title": "Film", "freestyle": True, "scenes": [{"id": "a", "type": "title", "title": "Hi", "duration": 2, "narration": "One two three four five six."}]}))
    paced = timeline.estimate_plan(narrated, {}).duration
    spoken = timeline.estimate_plan(narrated, {}, voices={"a": 9.0}).duration
    assert spoken > paced and spoken >= 9.0


# ── 3. A held clip shorter than its narration, at save ────────────────────


def _held_clip(narration_seconds: float, *, extend_mode: str = "hold", next_scene: bool = True):
    scenes = [
        {"id": "s1", "type": "clip", "clip_id": CLIP_ID, "narration": "Look at it.", "audio": False, "extend_mode": extend_mode},
    ]
    if next_scene:
        scenes.append({"id": "s2", "type": "title", "title": "Next", "duration": 3})
    spec = parse_video_spec(
        validate_video_spec({"title": "Film", "freestyle": True, "default_transition": {"type": "crossfade", "duration": 0.6}, "scenes": scenes})
    )
    media = timeline.PlanMedia(
        clips={"s1": timeline.ClipMedia(4.0, 1280, 720, 24.0, 96)}, voices={"s1": timeline.VoiceMedia(narration_seconds)}
    )
    document = timeline.plan(spec, media, fps=30, theme={}).document
    facts = quality.ContentFacts(shots={CLIP_ID: quality.ShotFacts("Monitor", JOB_READY)}, spoken={"s1": narration_seconds})
    return spec, document, facts


def _frozen(spec, document, facts):
    return [f for f in quality.content_findings(spec, document, facts, None) if f.kind == "frozen_hold"]


def test_a_held_clip_shorter_than_its_narration_fails_the_save_time_check():
    spec, document, facts = _held_clip(5.5)
    [finding] = _frozen(spec, document, facts)
    assert finding.severity == "dense" and finding.scene_id == "s1"
    assert "its last frame freezes for" in finding.message and "its clip plays 4.0 s" in finding.message
    assert "its narration, 5.5 s as spoken" in finding.message
    assert "extend_mode zoom" in finding.message and "or loop" in finding.message
    # What the render's gate refuses on: a finding the audience would see.
    assert finding.severity in render_checks.BLOCKING_SEVERITIES
    # Only what the scene shows alone freezes on screen: its transition out is a blend.
    frozen = quality.frozen_hold_seconds(document, 0)
    held = quality.held_seconds(document["scenes"][0])
    assert 0.5 < frozen < held
    # Zoom (the default) drifts on what holds, loop crossfades: no frozen picture to say.
    for mode in ("zoom", "loop"):
        assert _frozen(*_held_clip(5.5, extend_mode=mode)) == []
    # A hold within the render's own frozen-picture threshold, or a shot not made yet, says nothing.
    assert _frozen(*_held_clip(4.2)) == []
    spec, document, facts = _held_clip(5.5)
    facts.shots[CLIP_ID] = quality.ShotFacts("Monitor", "running")
    assert _frozen(spec, document, facts) == []


def test_a_narration_not_spoken_yet_is_said_at_the_reading_pace():
    spec, document, facts = _held_clip(6.0, next_scene=False)
    facts.spoken = {}
    [finding] = _frozen(spec, document, facts)
    assert "at the reading pace (not spoken yet)" in finding.message


# ── 4. An insert's crops, out of the transitions ──────────────────────────


def test_an_inserts_crops_are_cut_where_its_scene_is_on_screen_alone():
    # A scene from 5.4 s, its transition in 0.6 s, the next one's from 11.4 s: its insert runs the whole scene.
    document = _document(
        (0.0, 6.0, 0.0, []), (5.4, 12.0, 0.6, [(0.0, 6.6)]), (11.4, 16.0, 0.6, []), fps=30
    )
    inserts = {"insert/s1.0": SimpleNamespace(label="Scene s1: its screen insert 0", logo=None, light=None, key=None)}
    placed = insert_checks.placed_inserts(document, inserts)
    where = placed["insert/s1.0"]
    assert where.owned == pytest.approx((6.0, 11.4))
    crops = insert_checks.crop_frames(placed, fps=30)["insert/s1.0"]
    first, last = min(crops), max(crops)
    # Every crop is outside both transition windows: neither the one in (5.4-6.0 s) nor the one out (11.4-12.0 s).
    for frame in crops:
        assert 6.0 <= frame / 30 < 11.4
    assert first == math.ceil(6.0 * 30) + 1 and last == math.ceil(11.4 * 30) - 2
    # A video fade from black is no frame of its own either.
    faded = _document((0.0, 6.0, 0.0, [(0.0, 6.0)]), (5.4, 9.0, 0.6, []), fps=30, fade=1.0)
    early = insert_checks.crop_frames(insert_checks.placed_inserts(faded, {"insert/s0.0": inserts["insert/s1.0"]}), fps=30)["insert/s0.0"]
    assert min(early) / 30 >= 1.0 and max(early) / 30 < 5.4
    # The answer says where they were cut.
    report = {
        "key": "insert/s1.0",
        "label": "Scene s1: its screen insert 0",
        "frames": 3,
        "crops": [{"frame": n, "word": w, "file_id": str(uuid.uuid4())} for n, w in zip(sorted(crops), ("first", "middle", "last"), strict=True)],
        "cropped_within": list(insert_checks.crop_window(where)),
    }
    [line] = render_checks.insert_lines({"inserts": [report]})
    assert "the first and last frames its scene owns alone, after its transition in and before its transition out" in line


def test_a_preview_window_settles_where_its_scene_owns_its_frames():
    document = _document((0.0, 6.0, 0.0, []), (5.4, 12.0, 0.6, []), (11.4, 16.0, 0.6, []), fps=30, fade=0.5)
    from app.services.video_studio.render import window_times

    start, end, settled_from, settled_to = window_times(document, 1)
    assert (start, end) == (5.4, 12.0) and (settled_from, settled_to) == timeline.owned_span(document, 1) == (6.0, 11.4)
    assert timeline.owned_span(document, 0) == (0.5, 5.4) and timeline.owned_span(document, 2) == (12.0, 15.5)


# ── 5. The preview lane ───────────────────────────────────────────────────


class _Rows:
    def __init__(self, rows):
        self.rows = rows

    async def execute(self, _statement, _params=None):
        return SimpleNamespace(all=lambda: list(self.rows))


class _ErrorRows(_Rows):
    """``(render id, measured, preflight)`` rows as ``render_history.estimate_errors`` selects them: each JSON
    scalar read as text (``->>``), the objects as they are - no filter applied, as a database that kept every row."""

    async def execute(self, _statement, _params=None):
        def text(value):
            return None if value is None else str(value)

        def columns(render_id, measured, recorded):
            return (
                render_id,
                *(text(measured.get(name)) for name in ("pages", "cpus", "frames_reused", "frames_inherited", "ranges", "total_seconds")),
                measured.get("insert_frames"),
                measured.get("pace"),
                measured.get("contention"),
                text(recorded.get("model")),
                recorded.get("shape"),
                text(recorded.get("costs_from")),
                text(recorded.get("raw_seconds")),
            )

        return SimpleNamespace(all=lambda: [columns(*row) for row in self.rows])


class _FakeDb:
    async def commit(self):
        return None


class _Session:
    async def __aenter__(self):
        return _FakeDb()

    async def __aexit__(self, *_exc):
        return False


class _Lane:
    def __init__(self, name, limit, lends=None):
        self.name, self.concurrency_limit, self.lends_idle_slots_to = name, limit, lends

    async def periodic(self, _ctx):
        return None


def _two_videos_queue() -> list[tuple[str, str]]:
    """18 previews of three other videos queued first, then the two scenes of the video being checked."""
    queued = [(f"other-{n % 3}-{n}", f"other-{n % 3}") for n in range(18)]
    return [*queued, ("checked-1", "checked"), ("checked-2", "checked")]


def test_two_videos_queuing_previews_are_served_one_turn_in_two():
    queue = [(f"a{n}", "a") for n in range(5)] + [(f"b{n}", "b") for n in range(3)]
    assert preview_lane.dealt(queue, {}).order == ["a0", "b0", "a1", "b1", "a2", "b2", "a3", "a4"]
    # Asked interleaved, or the other way round: served alternating all the same.
    interleaved = [("b0", "b"), ("a0", "a"), ("b1", "b"), ("a1", "a")]
    assert preview_lane.dealt(interleaved, {}).order == ["b0", "a0", "b1", "a1"]
    # The video asked after 18 previews of three others is served in the first round, never after the 18.
    order = preview_lane.dealt(_two_videos_queue(), {"other-0": 2}).order
    assert order.index("checked-1") <= 3 and order.index("checked-2") <= 7


async def test_an_idle_render_slot_takes_the_next_dealt_preview_while_the_preview_lane_is_full(monkeypatch):
    from app.services import orchestration_engine as engine

    render_lane = _Lane(jobs.RENDER_ADAPTER, 1, lends=jobs.PREVIEW_ADAPTER)
    preview_lane = _Lane(jobs.PREVIEW_ADAPTER, 2)
    adapters = [render_lane, preview_lane]
    dealt = [uuid.uuid4(), uuid.uuid4()]
    claims: list[tuple[str, int, str | None]] = []

    async def claim_due(_db, *, adapter, worker_id, limit, lease_seconds=None, slot=None):
        claims.append((adapter, limit, slot))
        return [SimpleNamespace(id=dealt.pop(0))] if adapter == jobs.PREVIEW_ADAPTER and dealt else []

    spawned: list[tuple[uuid.UUID, str, str | None]] = []
    monkeypatch.setattr(engine, "claim_due", claim_due)
    monkeypatch.setattr(reconciler, "async_session", _Session)
    monkeypatch.setattr(reconciler, "_spawn_execution_task", lambda eid, name, _worker, slot=None: spawned.append((eid, name, slot)))
    monkeypatch.setattr(reconciler, "_inflight_by_adapter", {jobs.PREVIEW_ADAPTER: 2})
    first = dealt[0]
    taken = await reconciler._lend_idle_slots(render_lane, adapters, "w1", 1, 10)
    # The preview lane's next preview, in its own dealt order, in the render lane's slot.
    assert taken == [first] and spawned == [(first, jobs.PREVIEW_ADAPTER, jobs.RENDER_ADAPTER)]
    # Claimed into the render lane's slot, which its execution records: it holds a slot of that lane.
    assert claims == [(jobs.PREVIEW_ADAPTER, 1, jobs.RENDER_ADAPTER)]
    # A preview lane with room claims for itself; a render lane running its own render lends nothing.
    reconciler._inflight_by_adapter[jobs.PREVIEW_ADAPTER] = 1
    assert await reconciler._lend_idle_slots(render_lane, adapters, "w1", 1, 10) == []
    reconciler._inflight_by_adapter.update({jobs.PREVIEW_ADAPTER: 2, jobs.RENDER_ADAPTER: 1})
    assert await reconciler._lend_idle_slots(render_lane, adapters, "w1", 1, 10) == []
    # The preview lane lends nothing of its own.
    assert await reconciler._lend_idle_slots(preview_lane, adapters, "w1", 1, 10) == []


def test_the_lanes_say_who_lends_to_whom():
    from app.services.orchestration_adapters.video_studio import VideoPreviewAdapter, VideoRenderAdapter

    assert VideoRenderAdapter.lends_idle_slots_to == jobs.PREVIEW_ADAPTER
    assert VideoPreviewAdapter.lends_idle_slots_to is None
