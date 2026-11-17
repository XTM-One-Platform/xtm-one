"""A 24-scene 1080p60 final render (30 Oct 2026): its CPU share, a take a few seconds short, the template's
cards, a loading frame after a settled wait, a beat_sync that syncs nothing.

1. The render's 8 pages got 4.0 of 8 CPUs (92% of the CPU used) beside 6 assignment jobs and a scene preview: a whole
   render now draws in a render lane (``cpu_lanes``: a cgroup v2 leaf weighed above the worker's, else a raised
   priority), the shared pool's jobs go to a worker drawing no render first (``orchestration_reconciler``), the
   pre-flight prices and names them at the CPU their worker measured (``cpu_share``), and the render's record says
   what the rest of its container used, the CPUs left unused and how long its pages waited for one.
2. An approved Lyria take could not be extended in the tool: the continuation is looked for on the take's own provider
   first, and a model-free pad holds its last chord and fades it (``music.padded_samples``), proposed by the save for a
   play-once track under 3 s short.
3. A video a second too long for its play-once track was lengthened by the template's cards: the save sets each card
   left unset to its least, the outro first, as many as the track needs (``card_fit``).
4. A spinner drawn by CSS changes no DOM: a looping animation moving on the frame now reads as loading
   (``page_settle``); ``beat_sync`` with no steady tempo is a save finding naming its two fixes.
"""

from __future__ import annotations

import asyncio
import math
import os
import uuid
from datetime import UTC, datetime
from types import SimpleNamespace

import numpy as np
import pytest

from app.models.video_studio import JOB_PENDING, JOB_READY
from app.services import orchestration_reconciler, worker_presence
from app.services.video_studio import (
    beats,
    card_fit,
    cpu_lanes,
    cpu_share,
    jobs,
    page_settle,
    quality,
    render_pace,
    resources,
)
from app.services.video_studio import music as music_tracks
from app.services.video_studio.spec import parse_video_spec, validate_video_spec

USER = SimpleNamespace(id=uuid.UUID(int=6764), role="user")
TRACK = "00000000-0000-0000-0000-000000006764"


# -- 1. The render lane ----------------------------------------------------------------------------------------------


@pytest.fixture
def cgroup(tmp_path, monkeypatch):
    """A delegated cgroup v2 root: the cpu controller offered, three processes in it."""
    (tmp_path / "cgroup.controllers").write_text("cpuset cpu io memory pids\n")
    (tmp_path / "cgroup.subtree_control").write_text("")
    (tmp_path / "cgroup.procs").write_text("1\n41\n42\n")
    monkeypatch.setattr(cpu_lanes, "ROOT", str(tmp_path))
    monkeypatch.setattr(cpu_lanes, "_lane", None)
    monkeypatch.setattr(cpu_lanes, "_expected_lane", None)
    monkeypatch.setattr(cpu_lanes.settings, "video_studio_render_cpu_weight", 1000)
    return tmp_path


def test_a_delegated_cgroup_gives_the_render_a_leaf_weighed_above_the_worker_s(cgroup):
    assert cpu_lanes.expected().kind == cpu_lanes.CGROUP
    lane = cpu_lanes.lane()
    assert (lane.kind, lane.weight) == (cpu_lanes.CGROUP, 1000)
    # The container's processes left its cgroup for the worker leaf before the cpu controller went to the leaves.
    assert (cgroup / cpu_lanes.WORKER_LEAF / "cgroup.procs").read_text() == "42"
    assert (cgroup / "cgroup.subtree_control").read_text() == "+cpu"
    assert (cgroup / cpu_lanes.RENDER_LEAF / "cpu.weight").read_text() == "1000"
    # A whole render's process goes to the render leaf; what it starts after inherits it.
    assert cpu_lanes.place(4242)
    assert (cgroup / cpu_lanes.RENDER_LEAF / "cgroup.procs").read_text() == "4242"
    assert lane.words() == (
        "its pages draw in a render lane of their own (cgroup cpu.weight 1000 against the 100 of the worker's other jobs)"
    )
    assert cpu_lanes.Lane.of(lane.as_dict()) == lane


def test_without_a_delegated_cgroup_the_render_takes_a_raised_priority_or_says_why_it_has_none(cgroup, monkeypatch):
    (cgroup / "cgroup.controllers").unlink()
    monkeypatch.setattr(cpu_lanes, "_may_raise", lambda nice: True)
    raised: list = []
    monkeypatch.setattr(os, "PRIO_PROCESS", 0, raising=False)
    monkeypatch.setattr(os, "setpriority", lambda which, pid, nice: raised.append((pid, nice)), raising=False)
    lane = cpu_lanes.lane()
    assert (lane.kind, lane.nice) == (cpu_lanes.PRIORITY, -10) and cpu_lanes.raised_nice(1000) == -10
    assert cpu_lanes.place(4242) and raised == [(4242, -10)]
    # A worker that may raise no priority: the render keeps the worker's own, the pre-flight says why and the remedy.
    monkeypatch.setattr(cpu_lanes, "_lane", None)
    monkeypatch.setattr(cpu_lanes, "_expected_lane", None)
    monkeypatch.setattr(cpu_lanes, "_may_raise", lambda nice: False)
    none = cpu_lanes.lane()
    assert none.kind == cpu_lanes.NONE and not cpu_lanes.place(4242)
    assert "the container has no cgroup v2" in none.words() and "CAP_SYS_NICE" in none.words()
    # Weight 0: no lane at all, by choice.
    monkeypatch.setattr(cpu_lanes.settings, "video_studio_render_cpu_weight", 0)
    assert cpu_lanes._expected(cpu_lanes.configured_weight()).reason == "VIDEO_STUDIO_RENDER_CPU_WEIGHT is 0"


def test_a_render_lane_keeps_the_pages_share_against_six_assignment_jobs():
    beside = [(1.0, 0)] * 6
    flat = cpu_share.shares([(8.0, 0), *beside], 8)
    weighed = cpu_share.grouped_shares(8.0, 1000, beside, 8)
    raised = cpu_share.shares([(8.0, cpu_lanes.raised_nice(1000)), *beside], 8)
    # Thread for thread the render's 8 pages got 8 of the 14 runnable threads' CPU; its lane gives it 10 of 11 shares.
    assert flat[0] == pytest.approx(8 * 8 / 14, rel=0.01)
    assert weighed[0] == pytest.approx(8 * 1000 / 1100, rel=0.01) and sum(weighed) == pytest.approx(8)
    assert raised[0] > 7.0
    # A lane under its share leaves the rest to the worker's jobs; jobs under theirs leave the rest to the render.
    assert cpu_share.grouped_shares(2.0, 1000, beside, 8) == [2.0, *[1.0] * 6]
    assert cpu_share.grouped_shares(8.0, 1000, [(0.2, 0)], 8)[0] == pytest.approx(7.8)


class _LoadDb:
    """One running preview on w1, and the jobs of w1's own process."""

    def __init__(self, own: list[tuple[str, int]]) -> None:
        self.own = own
        self.preview = uuid.uuid4()

    def begin_nested(self):
        import contextlib

        return contextlib.nullcontext()

    async def execute(self, statement):
        text = str(statement)
        if "orchestration_executions" in text and "count(" in text:
            rows = self.own
        elif "orchestration_executions" in text:
            rows = [("video_preview", "running", "w1", {"render_id": str(self.preview)})]
        else:
            rows = []
        return SimpleNamespace(all=lambda: rows)


def _load(monkeypatch, own, record: dict) -> list[cpu_share.Job]:
    async def render_workers():
        return {"w1": {"worker_id": "w1", **record}}

    monkeypatch.setattr(cpu_share, "_render_workers", render_workers)
    return asyncio.run(cpu_share.committed_load(_LoadDb(own), preview_cpus=2.0, now=datetime.now(UTC)))


def test_the_pre_flight_prices_the_assignment_jobs_at_what_their_worker_measured_and_names_them(monkeypatch):
    # The container used 1.95 CPUs over the last minute, its preview's process 1.6: 0.35 between 6 assignment jobs.
    found = _load(monkeypatch, [("assignment", 6)], {"cpu": {"cpus": 1.95, "video_cpus": 1.6, "seconds": 60}})
    assigned = [job for job in found if job.kind == "assignment job"]
    assert len(assigned) == 6 and all(job.lasting and job.measured and job.running for job in assigned)
    assert assigned[0].cpus == pytest.approx(0.35 / 6)
    lane = cpu_lanes.Lane(cpu_lanes.CGROUP, weight=1000)
    shared = cpu_share.priced(12 * 60, cpus=8, pages=7, jobs=found, recording_slots=2, lane=lane)
    assert shared.headline().startswith("about 13 min on a worker shared with 1 scene preview and 6 assignment jobs")
    words = shared.words()
    assert "6 assignment jobs (6 running, about 0.06 CPUs each: what its worker's own process used beside its renders" in words
    assert "go to a worker drawing no render first" in words and "cgroup cpu.weight 1000" in words
    # Kept on the render's pre-flight and read back the same.
    again = cpu_share.Shared.of(shared.as_dict())
    assert again is not None and again.lane == lane.as_dict()
    assert [(job.kind, job.lasting) for job in again.jobs] == [(job.kind, job.lasting) for job in shared.jobs]
    # A worker that measured nothing counts them so, and says it.
    counted = [job for job in _load(monkeypatch, [("assignment", 2)], {}) if job.kind == "assignment job"]
    assert [job.cpus for job in counted] == [cpu_share.IN_PROCESS_CPUS] * 2 and not counted[0].measured
    assert cpu_share.job_kind("sub_agent") == "sub agent job"


def test_a_worker_measures_what_its_own_process_used_beside_its_renders_and_previews():
    clock = {"now": 0.0, "used": 0.0, "trees": {}}
    measure = cpu_share.WorkerCpu(clock=lambda: clock["now"], container=lambda: clock["used"], trees=lambda: dict(clock["trees"]))
    assert measure.sample() is None
    # A render started within the span: all its CPU counted in it.
    clock.update(now=5.0, used=20.0, trees={101: 15.0})
    assert measure.sample() == {"cpus": 4.0, "video_cpus": 3.0, "seconds": 5.0}
    for step in range(2, 20):
        clock.update(now=5.0 * step, used=20.0 * step, trees={101: 15.0 * step})
        sampled = measure.sample()
    assert sampled == {"cpus": 4.0, "video_cpus": 3.0, "seconds": 60.0}
    # Nothing to read (no cgroup statistics): nothing published.
    assert cpu_share.WorkerCpu(container=lambda: None, trees=dict).sample() is None


class _Adapter:
    def __init__(self, name: str, limit: int | None = None, reserved: int = 0, presence: dict | None = None) -> None:
        self.name, self.concurrency_limit, self.cpus_reserved, self._presence = name, limit, reserved, presence or {}

    def presence(self) -> dict:
        return self._presence


def test_a_worker_drawing_a_render_leaves_the_shared_pool_to_a_worker_drawing_none(monkeypatch):
    monkeypatch.setattr(orchestration_reconciler.settings, "reconciler_interval_seconds", 2.0)
    assignment, render = _Adapter("assignment"), _Adapter("video_render", limit=1, reserved=7)
    idle = {"worker_id": "w2", "state": worker_presence.STATE_RUNNING, "claims": ["assignment"], "reserved_cpus": 0}
    busy = {**idle, "reserved_cpus": 7}
    leave = orchestration_reconciler._leave_seconds
    assert orchestration_reconciler._reserved_cpus([assignment, render]) == 7
    assert leave(assignment, 7, [idle], "w1") == orchestration_reconciler.LEAVE_TO_OTHERS_CYCLES * 2.0
    # Every other worker draws a render too, none claims the adapter, or this one reserves nothing: claimed here at once.
    assert leave(assignment, 7, [busy], "w1") == 0.0
    assert leave(assignment, 7, [{**idle, "claims": ["video_preview"]}], "w1") == 0.0
    assert leave(assignment, 0, [idle], "w1") == 0.0
    # An adapter with a lane of its own keeps its own slots.
    assert leave(render, 7, [idle], "w1") == 0.0
    # Its presence says what it reserves and runs, with what its adapters add.
    monkeypatch.setattr(orchestration_reconciler, "_inflight_by_adapter", {"assignment": 6, "video_render": 1, "flow": 0})
    extra = orchestration_reconciler._presence_extra([assignment, _Adapter("video_render", 1, 7, {"cpu": {"cpus": 4.4}})])
    assert extra == {"reserved_cpus": 7, "running": {"assignment": 6, "video_render": 1}, "cpu": {"cpus": 4.4}}
    assert worker_presence.reserved_cpus(extra) == 7 and worker_presence.reserved_cpus({}) == 0


def test_the_render_s_record_says_its_pages_left_cpus_unused_without_waiting_for_one():
    clock = {"now": 0.0, "render": 0.0, "worker": 0.0, "waited": 0.0}

    async def neighbours():
        return [render_pace.Neighbour("assignment job", "")] * 6 + [render_pace.Neighbour("scene preview", "p1")]

    watch = render_pace.PaceWatch(
        None,
        pages=8,
        cpus=8,
        clock=lambda: clock["now"],
        cpu=lambda: clock["render"],
        stat=lambda: resources.CpuStat(clock["worker"]),
        neighbours=neighbours,
        pressure=lambda: (clock["waited"], True),
    )
    tracker = SimpleNamespace(scene_frames={}, done=0, samples=1)
    for _ in range(29):
        asyncio.run(watch.observe(tracker))
        # The observed render: 4.0 of 8 CPUs, 92% of what its container used; its pages waited 2% of the time.
        clock["now"] += 60.0
        clock["render"] += 4.0 * 60
        clock["worker"] += 4.0 / 0.92 * 60
        clock["waited"] += 0.02 * 60
    contention = watch.contention()
    assert contention["cpus_used"] == 4.0 and contention["cpu_share"] == pytest.approx(0.92, abs=0.001)
    assert contention["others_cpus"] == pytest.approx(0.35, abs=0.01)
    assert contention["unused_cpus"] == pytest.approx(3.65, abs=0.01)
    assert (contention["waited"], contention["waited_own"]) == (0.02, True)
    said = cpu_share.contention_words(contention, 28 * 60, {"idle_seconds": 12 * 60})
    unused = f"{contention['unused_cpus']:.1f}"
    assert f"beside 6 assignment jobs and 1 scene preview; the rest of its container used 0.35 CPUs and {unused} went unused" in said
    assert "its pages waited for a CPU 2% of the time" in said
    assert said.endswith("Its pages left CPUs unused without waiting for one: the jobs beside them did not hold them back, what they drew did.")
    # Pages that waited are short of CPU: that sentence is not said.
    starved = {**contention, "waited": 0.4}
    assert "without waiting" not in cpu_share.contention_words(starved, 28 * 60, None)


def test_the_kernel_s_cpu_pressure_is_read_in_seconds(tmp_path):
    pressure = tmp_path / "cpu.pressure"
    pressure.write_text("some avg10=1.50 avg60=0.80 avg300=0.20 total=2500000\nfull avg10=0.00 avg60=0.00 avg300=0.00 total=0\n")
    assert resources.cpu_pressure_seconds(pressure) == 2.5
    assert resources.cpu_pressure_seconds(tmp_path / "missing") is None


# -- 2. A take a few seconds short ---------------------------------------------------------------------------------


def _chord(rate: int, seconds: float, decay: float) -> np.ndarray:
    """A three-note chord at level for *seconds*, then decaying to silence over *decay*, in stereo."""
    t = np.arange(round((seconds + decay) * rate)) / rate
    tone = sum(np.sin(2 * np.pi * f * t) for f in (220.0, 277.18, 329.63)) / 6
    envelope = np.clip(1 - np.maximum(0.0, t - seconds) / decay, 0.0, 1.0)
    mono = (tone * envelope).astype(np.float32)
    return np.stack([mono, mono * 0.9], axis=1)


def _peaks(samples: np.ndarray, rate: int, count: int = 3) -> list[float]:
    spectrum = np.abs(np.fft.rfft(samples[:, 0] * np.hanning(len(samples))))
    frequencies = np.fft.rfftfreq(len(samples), 1 / rate)
    found: list[float] = []
    for index in np.argsort(spectrum)[::-1]:
        if all(abs(frequencies[index] - f) > 15 for f in found):
            found.append(float(frequencies[index]))
        if len(found) == count:
            break
    return sorted(found)


def test_a_pad_holds_the_take_s_last_chord_and_fades_it_to_the_length_asked():
    rate = 8000
    take = _chord(rate, 12.0, 1.5)
    padded, held_at = music_tracks.padded_samples(take, rate, 18.0)
    assert len(padded) == 18 * rate and padded.dtype == np.float32
    # Its ending (12 dB under its level 1.1 s into its decay) is shorter than the crossfade: the hold starts the
    # crossfade's second before the take's end, the take kept sample for sample up to there.
    assert held_at == pytest.approx(12.5, abs=0.06)
    cut = round(held_at * rate)
    assert np.array_equal(padded[:cut], take[:cut])
    # Past the crossfade, the chord itself, held at the take's level: its three notes, no attack.
    held = padded[cut + rate : cut + 2 * rate].astype(np.float64)
    assert _peaks(held, rate) == pytest.approx([220.0, 277.18, 329.63], abs=8.0)
    source = take[cut - rate : cut].astype(np.float64)
    level = 20 * math.log10(np.sqrt(np.mean(held**2)) / np.sqrt(np.mean(source**2)))
    assert abs(level) < 3.0
    # Faded out to silence on the length asked, never clipping.
    assert np.sqrt(np.mean(padded[-rate // 20 :].astype(np.float64) ** 2)) < 0.01
    assert float(np.max(np.abs(padded))) <= 0.99
    # A pad made twice is the same; a take that ends on a long decay keeps it up to 12 dB under its level.
    assert np.array_equal(music_tracks.padded_samples(take, rate, 18.0)[0], padded)
    _long, decaying = music_tracks.padded_samples(_chord(rate, 12.0, 4.0), rate, 18.0)
    assert decaying == pytest.approx(15.0, abs=0.06)
    with pytest.raises(music_tracks.MusicError, match="silent"):
        music_tracks.padded_samples(np.zeros((rate * 5, 2), dtype=np.float32), rate, 7.0)


def _take(attempt: int = 0) -> SimpleNamespace:
    return SimpleNamespace(
        id=uuid.UUID(TRACK),
        status=JOB_READY,
        params={"duration_seconds": 170, "provider_id": "p-lyria", "attempt": attempt},
        prompt="A warm synth bed that builds",
        model="lyria-3.5",
        provider="gemini",
        error=None,
        file_id=uuid.UUID(int=1),
        duration_ms=170_200,
    )


class _Locked:
    async def refresh(self, _row, with_for_update=False):
        return None

    async def flush(self):
        return None


def test_a_pad_is_queued_with_no_provider_no_credits_and_no_attempt_spent(monkeypatch):
    queued: list = []

    async def enqueue(_db, kind, row, **options):
        queued.append((kind, options))

    async def never(*_args, **_kwargs):
        raise AssertionError("a pad asks no provider")

    monkeypatch.setattr(jobs, "enqueue", enqueue)
    monkeypatch.setattr(music_tracks, "resolve", never)
    monkeypatch.setattr(music_tracks, "resolve_continuing", never)
    take = _take(attempt=music_tracks.MAX_RETRIES)
    seconds = music_tracks.pad_seconds(171.4)
    assert seconds == 172
    # Five paid attempts spent: a pad still goes, and spends none of them.
    said = asyncio.run(music_tracks.restart_music(_Locked(), USER, take, refit=True, pad=True, duration_seconds=seconds))
    assert said == music_tracks.PADDED_WITH
    kept = take.params[music_tracks.EXTENDED_FROM]
    assert kept == {"file_id": str(uuid.UUID(int=1)), "seconds": 170.2, "duration_ms": 170_200, "cut": 170.2, "pad": True}
    assert (take.status, take.model, take.file_id, take.params["pads"], take.params["attempt"]) == (JOB_PENDING, "lyria-3.5", None, 1, 6)
    assert queued == [("music", {"generation": 6, "owed_to": None})]
    # The take the pad keeps plays again if it is not made, the reason said as the pad's.
    params = dict(take.params)
    assert music_tracks.kept_take(params, "the file is gone")["file_id"] == uuid.UUID(int=1)
    assert params[music_tracks.EXTEND_FAILED] == "the pad: the file is gone"
    # Too far: a chord held 10 s is a drone; nothing to pad; pad and extend together.
    with pytest.raises(music_tracks.MusicError, match="drone"):
        music_tracks.padding(_take(), 181.0)
    with pytest.raises(music_tracks.MusicError, match="nothing to pad"):
        music_tracks.padding(_take(), 170.0)
    with pytest.raises(music_tracks.MusicError, match="pick one"):
        asyncio.run(music_tracks.restart_music(_Locked(), USER, _take(), refit=True, pad=True, extend=True))


def test_an_extension_with_no_continuation_offers_the_pad_and_looks_on_the_take_s_provider_first(monkeypatch):
    from app.llm import router

    class _Composer:
        def __init__(self, credits, continues=()):
            self.credits, self.continues = credits, continues

        def continues_music(self, model):
            return model in self.continues

        async def music_credits(self, _model):
            return self.credits

    async def resolve(_db, _user_id, *, model=None, provider_id=None, **_kwargs):
        if provider_id == "p-eleven" or (model or "").startswith("music_v2"):
            return _Composer(0, ("music_v2_5",)), model, SimpleNamespace(id="p-eleven", provider="elevenlabs", display_name="ElevenLabs")
        return _Composer(None, ("lyria-continue",)), model or "lyria-3.5", SimpleNamespace(id="p-lyria", provider="gemini", display_name="Google")

    listed = [
        {"model": "music_v2_5", "provider_id": "p-eleven", "continues": True},
        {"model": "lyria-continue", "provider_id": "p-lyria", "continues": True},
    ]

    async def models(_db, *, user_id):
        return listed

    monkeypatch.setattr(music_tracks, "resolve", resolve)
    monkeypatch.setattr(router, "list_music_models_for", models)
    # The take's own provider continues a clip with another of its models: that one, before another provider's.
    _provider, model, config = asyncio.run(music_tracks.resolve_continuing(None, USER.id, model="lyria-3.5", provider_id="p-lyria"))
    assert (model, config.id) == ("lyria-continue", "p-lyria")
    # Only another provider continues one, and its account has no credits: refused, the pad offered.
    listed.pop()
    with pytest.raises(music_tracks.MusicError) as refused:
        asyncio.run(music_tracks.restart_music(_Locked(), USER, _take(), refit=True, extend=True, duration_seconds=172))
    said = str(refused.value)
    assert "music_v2_5 on ElevenLabs: its account has no credits left" in said
    assert "or generate_music(video_id, refit: true, pad: true) holds its last chord 1.8 s and fades, with no model call" in said


def _effective(**top) -> dict:
    return validate_video_spec(
        {
            "title": "Launch",
            "freestyle": True,
            "scenes": [
                {"id": "pitch", "type": "statement", "lines": ["One platform"], "duration": 6},
                {"id": "outro", "type": "outro", "title": "Thanks", "duration": 4},
            ],
            **top,
        }
    )


def test_the_save_proposes_the_pad_for_a_play_once_track_under_3_s_short():
    effective = _effective(music={"music_id": TRACK, "loop": False})
    spec = parse_video_spec(effective)
    document = {"duration": 10.0, "scenes": [{"id": "pitch", "type": "statement", "start": 0.0, "end": 6.0}, {"id": "outro", "type": "outro", "start": 6.0, "end": 10.0}]}

    def short(music_seconds: float) -> str:
        facts = quality.ContentFacts(music_seconds=music_seconds)
        (finding,) = [f for f in quality.content_findings(spec, document, facts, None) if f.kind == "music_short"]
        return finding.message

    assert "generate_music(video_id, refit: true, pad: true) holds its last chord 1.2 s and fades, with no model call, or generate_music(video_id, refit: true)" in short(8.8)
    assert "pad: true" not in short(6.5)
    assert music_tracks.pad_offer(8.8, 10.0).endswith("with no model call") and music_tracks.pad_offer(1.0, 10.0) == ""
    looping = music_tracks.outgrown_words(7.4, 10.0)
    assert "pad: true) holds its last chord 2.6 s" in looping


# -- 3. The template's cards fit a play-once track -----------------------------------------------------------------


def _shortening(monkeypatch, *, playable: float, before: float, written: dict | None = None, loop: bool = False):
    effective = _effective(music={"music_id": TRACK, "loop": loop})
    effective["scenes"].insert(0, {"id": "intro", "type": "title", "title": "Launch", "duration": 3.7})
    applied = SimpleNamespace(added=["intro", "outro"], spec=effective)
    leasts = [
        quality.CardLeast("intro", "intro", 3.1, 3.7, 1),
        quality.CardLeast("outro", "outro", 4.9, 5.5, 9),
    ]

    async def playable_seconds(_db, _music):
        return playable, False

    async def measure(_document):
        return {"scenes": []}

    monkeypatch.setattr(music_tracks, "playable_seconds", playable_seconds)
    monkeypatch.setattr(quality, "measure_document", measure)
    monkeypatch.setattr(quality, "card_leasts", lambda _audit, _document, _authored: leasts)
    authored = {"title": "Launch", "scenes": [], **({"template_scenes": written} if written is not None else {})}
    placed = SimpleNamespace(duration=before, plan=SimpleNamespace(document={}))
    return asyncio.run(card_fit.shortened(None, authored, applied, placed))


def test_the_outro_goes_to_its_least_first_and_the_intro_only_when_the_track_needs_it(monkeypatch):
    # A second too long for the track: the outro's 0.6 s and the 0.5 s tolerance cover it.
    one = _shortening(monkeypatch, playable=170.2, before=171.2)
    assert one is not None and [card.card for card in one.cards] == ["outro"]
    assert one.authored["template_scenes"] == {"outro": {"duration": 4.9}}
    assert one.words(170.6) == (
        "The music plays once (loop: false) and ends at 170.2 s of the 171.2 s video: the template's outro shortened "
        "from 5.5 s to its least, 4.9 s, for its 9 words (template_scenes.outro.duration: 4.9) - the video now lasts "
        "170.6 s. Set another duration there to keep a card longer."
    )
    # Two seconds too long: both cards, and the gap left said.
    both = _shortening(monkeypatch, playable=169.2, before=171.2, written={"intro": {"kicker": None}})
    assert [card.card for card in both.cards] == ["outro", "intro"]
    assert both.authored["template_scenes"] == {"intro": {"kicker": None, "duration": 3.1}, "outro": {"duration": 4.9}}
    assert "still 0.8 s past the track" in both.words(170.0)
    # A track that loops or covers the video, a card the author set: nothing shortened.
    assert _shortening(monkeypatch, playable=170.2, before=171.2, loop=True) is None
    assert _shortening(monkeypatch, playable=171.0, before=171.2) is None
    set_outro = _shortening(monkeypatch, playable=170.2, before=171.2, written={"outro": {"duration": 5.5}})
    assert [card.card for card in set_outro.cards] == ["intro"]


def test_a_card_s_least_is_one_second_at_least_and_said_as_before():
    document = {
        "duration": 17.0,
        "scenes": [
            {"id": "pitch", "type": "statement", "lines": ["One platform"], "start": 0.0, "end": 10.0},
            {"id": "template-outro", "type": "outro", "title": "Thanks for watching", "cta": "Start your trial", "url": "filigran.io", "start": 10.0, "end": 17.0},
        ],
    }
    authored = {"scenes": [{"id": "pitch", "type": "statement", "lines": ["One platform"]}]}
    audit = {"scenes": [{"index": 1, "start": 10.0, "end": 17.0, "shown_until": 16.1, "reveal_end": 1.2, "fit": 1, "findings": []}]}
    (card,) = quality.card_leasts(audit, document, authored)
    assert (card.card, card.scene_id, card.least, card.lasts, card.words) == ("outro", "template-outro", 5.2, 7.0, 8)
    assert "template_scenes.outro.duration at least 5.2 s for its 8 words (it lasts 7 s)" in quality.card_minimums(audit, document, authored)


# -- 4. A loading frame after a settled wait, a beat_sync that syncs nothing ---------------------------------------


def _spinner(angle: int) -> np.ndarray:
    crop = np.full((40, 40), 30, dtype=np.int16)
    y, x = np.ogrid[:40, :40]
    ring = (np.hypot(x - 20, y - 20) - 14) ** 2 < 9
    arc = np.abs(((np.degrees(np.arctan2(y - 20, x - 20)) - angle + 180) % 360) - 180) < 60
    crop[ring & arc] = 220
    return crop


def test_an_element_looping_an_animation_on_the_frame_reads_as_loading_until_it_stops():
    raw = {"t": 1000.0, "changed": 0.0, "busy": [], "canvases": [], "looping": [["div.spinner", [100, 100, 40, 40]]]}
    read = page_settle.page_read(raw, origin=0.0, at=1.0)
    assert read.looping == (("div.spinner", (100.0, 100.0, 40.0, 40.0)),)
    key = page_settle.canvas_key("loop:div.spinner", (100.0, 100.0, 40.0, 40.0))
    assert dict(read.boxes())["loop:div.spinner"] == (100.0, 100.0, 40.0, 40.0)
    watch = page_settle.PageWatch(began=0.0)
    for index in range(10):
        t = 1.0 + index * page_settle.SAMPLE_SECONDS
        # A spinner turning: its box grows and shrinks with its angle, read a pixel wider every other time.
        box = (100.0, 100.0, 40.0 + index % 2, 40.0 + index % 2)
        looping = (("div.spinner", box),)
        crop = np.pad(_spinner(index * 70), ((0, index % 2), (0, index % 2)), constant_values=30)
        turned = {page_settle.canvas_key("loop:div.spinner", box): crop}
        watch.observe(page_settle.PageRead(t=t, changed=0.0, looping=looping), turned, frame_t=t)
        if index:
            # The DOM stood still all along: the spinner's pixels alone keep the page from settling.
            assert watch.settled_at(t) is None and watch.moving(t) == ["div.spinner (animating)"]
    # It stops (the content arrived): settled once the page stood still.
    for index in range(10, 16):
        t = 1.0 + index * page_settle.SAMPLE_SECONDS
        watch.observe(page_settle.PageRead(t=t, changed=0.0, looping=read.looping), {key: _spinner(0)}, frame_t=t)
    # Its last move is the read that found it back at rest.
    assert watch.settled_at(t) == pytest.approx(1.0 + 10 * page_settle.SAMPLE_SECONDS)
    [span] = watch.loading
    assert span[2] == ["div.spinner (animating)"]
    # A hold ending on it is flagged, with what to do about a looping element of the page's own.
    hold = page_settle.loading_hold({"action": "pause", "step": 4, "t1": 3.0, "loading": [[1.2, 2.8, ["div.spinner (animating)"]]]})
    assert (
        hold is not None
        and "with a loading signature (a spinner, a skeleton) reads as loading" in page_settle.hold_words(hold)
    )


def test_the_settle_watch_reports_the_elements_looping_an_animation():
    from app.services.video_studio import capture

    assert "document.getAnimations" in capture._SETTLE_WATCH_JS and "iterations !== Infinity" in capture._SETTLE_WATCH_JS
    assert "__LOOPING__" not in capture._SETTLE_WATCH_JS


def test_beat_sync_on_music_with_no_steady_tempo_is_a_save_finding_naming_its_two_fixes(monkeypatch):
    async def analysis(_file_id, **_kwargs):
        return {"bpm": None, "first": None, "seconds": 174.0}

    monkeypatch.setattr(beats, "file_analysis", analysis)
    effective = _effective(theme={"motion": {"beat_sync": "cuts"}}, music={"file_id": TRACK})
    spec = parse_video_spec(effective)
    _grid, note = asyncio.run(beats.video_grid(spec))
    assert isinstance(note, beats.SyncsNothing)
    document = {"duration": 10.0, "scenes": [{"id": "pitch", "type": "statement", "start": 0.0, "end": 6.0}, {"id": "outro", "type": "outro", "start": 6.0, "end": 10.0}]}
    facts = quality.ContentFacts(beat_unsynced=str(note))
    (finding,) = [f for f in quality.content_findings(spec, document, facts, None) if f.kind == "beat_sync_unsynced"]
    assert finding.severity == "advisory" and finding.scene_id == "pitch"
    assert "play a track with a steady beat (every built-in track has one), or set theme.motion.beat_sync: off" in finding.message
    # No music at all: the same finding, its two fixes named.
    silent = parse_video_spec(_effective(theme={"motion": {"beat_sync": "cuts"}}, music={"enabled": False}))
    _grid, none = asyncio.run(beats.video_grid(silent))
    assert isinstance(none, beats.SyncsNothing) and "give it music with a steady beat" in none
