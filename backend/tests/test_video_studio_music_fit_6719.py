"""Video 9b94e480 rev 137, final 7a3f8312: what the save measures, how a track plays once, what a call says.

1. The save fitted the music to the paced plan (about 165 s) while the render, its narration spoken and its
   template cards' with it, came out at 175 s: a 174 s track that fit the plan looped under the outro. The save,
   the quality check and ``get_video`` now plan the video as the render places it (``projects.measured_plan``):
   each narration at the length its voice take measured, the template cards' included, the cuts on the beats - and
   the music check takes the current render's length when it came out longer.
2. A track asked to play once still looped when shorter than the video (``fit_to_source``): it now fades
   out on its own end and holds silence after (``encoder.bed_filter``), never looped.
3. An extended refit picked another provider's continuing model silently and failed there on its quota after the
   call answered "ready": the provider's credits are read before the refit is accepted, the switch is named, and an
   extension that ended without a new take is the call's own answer.
4. A template card's minimum was found by failing a save: every save states it (``quality.card_minimums``).
5. A render queued behind another video's said nothing of its wait: the queue line gives the running render's time
   left and when this one starts (``render_queue``).
6. ``beat_sync`` on a track with no steady tempo is said at the save, with what to change.
"""

from __future__ import annotations

import asyncio
import uuid
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import httpx
import numpy as np
import pytest

from app.llm.elevenlabs_provider import ElevenLabsProvider
from app.models.video_studio import JOB_PENDING, JOB_READY, JOB_RUNNING, VideoMusic
from app.services.video_studio import beats, encoder, jobs, preview, projects, quality, render_queue, timeline
from app.services.video_studio import music as music_tracks
from app.services.video_studio.beats import BeatGrid
from app.services.video_studio.spec import Music, parse_video_spec, validate_video_spec

needs_ffmpeg = pytest.mark.skipif(not encoder.encoder_available(), reason="ffmpeg is not installed here")
USER = SimpleNamespace(id=uuid.UUID(int=6719), role="user")
TRACK = "00000000-0000-0000-0000-00000000a719"


# -- 1. The save's length is the one the render places -------------------------------------------------------------


def _effective(**top) -> dict:
    """A video whose template cards speak too: an opening card, a statement and a closing card, each narrated."""
    return validate_video_spec(
        {
            "title": "Launch",
            "freestyle": True,
            "scenes": [
                {"id": "intro", "type": "title", "title": "Launch", "duration": 3, "narration": "Meet the launch."},
                {
                    "id": "pitch",
                    "type": "statement",
                    "lines": ["One platform"],
                    "duration": 4,
                    "narration": "One platform for every exposure your teams track, from the first alert to the fix.",
                },
                {"id": "outro", "type": "outro", "title": "Thanks", "duration": 3, "narration": "Start today."},
            ],
            **top,
        }
    )


def _measured(monkeypatch, effective: dict, *, spoken: dict, grid=None, note=None) -> projects.MeasuredPlan:
    async def spoken_lengths(_db, _spec, _user):
        return dict(spoken)

    async def video_grid(_spec, **_kwargs):
        return grid, note

    monkeypatch.setattr(preview, "spoken_lengths", spoken_lengths)
    monkeypatch.setattr(beats, "video_grid", video_grid)
    return asyncio.run(projects.measured_plan(None, effective, USER))


def test_the_save_plans_each_narration_at_its_spoken_length_the_template_cards_included(monkeypatch):
    effective = _effective()
    paced = asyncio.run(projects.estimate(None, effective)).duration
    # The cards' narrations and the statement's spoken slower than the reading pace, as the render will place them.
    placed = _measured(monkeypatch, effective, spoken={"intro": 4.2, "pitch": 8.5, "outro": 3.4})
    render = timeline.plan(
        parse_video_spec(effective),
        timeline.PlanMedia(
            voices={k: timeline.VoiceMedia(v) for k, v in {"intro": 4.2, "pitch": 8.5, "outro": 3.4}.items()}
        ),
        fps=30,
        theme={},
    )
    assert placed.duration > paced + 3
    assert placed.duration == pytest.approx(render.duration, abs=0.05)
    assert placed.basis() == "every narration at its spoken length"
    some = _measured(monkeypatch, effective, spoken={"pitch": 8.5})
    assert some.basis() == "1 of 3 narrations at their spoken length, 2 at the reading pace"
    none = _measured(monkeypatch, effective, spoken={})
    assert (
        none.duration == pytest.approx(paced) and none.basis() == "every narration at the reading pace, not spoken yet"
    )


def test_a_beat_synced_video_is_planned_with_its_cuts_on_the_beats_as_the_render_lands_them(monkeypatch):
    effective = _effective(theme={"motion": {"beat_sync": "cuts"}}, music={"track": "bright-horizon"})
    grid = BeatGrid(60.0 / 97.0, 0.21)
    unsynced = _measured(monkeypatch, effective, spoken={})
    synced = _measured(monkeypatch, effective, spoken={}, grid=grid)
    render = timeline.plan(parse_video_spec(effective), timeline.PlanMedia(beats=grid), fps=30, theme={})
    assert synced.duration > unsynced.duration
    assert synced.duration == pytest.approx(render.duration, abs=0.05)


def test_the_music_is_short_on_the_length_the_render_places_not_the_paced_plan(monkeypatch):
    effective = _effective(music={"music_id": TRACK, "loop": False})
    spec = parse_video_spec(effective)
    paced = asyncio.run(projects.estimate(None, effective))
    placed = _measured(monkeypatch, effective, spoken={"intro": 4.2, "pitch": 8.5, "outro": 3.4})
    track = paced.duration + 1.0
    facts = quality.ContentFacts(music_seconds=track)
    on_paced = quality.content_findings(spec, paced.document, facts, None)
    assert not [f for f in on_paced if f.kind == "music_short"]
    (short,) = [f for f in quality.content_findings(spec, placed.plan.document, facts, None) if f.kind == "music_short"]
    assert f"at {track:.0f} s of the {placed.duration:.0f} s video" in short.message
    assert "fades out" in short.message and "repeat" not in short.message


# -- 2. A track asked to play once never loops ---------------------------------------------------------------------


def _rms(samples: np.ndarray, rate: int, start: float, end: float) -> float:
    part = samples[int(start * rate) : int(end * rate)]
    return float(np.sqrt(np.mean(part.astype(np.float64) ** 2))) if part.size else 0.0


@needs_ffmpeg
async def test_a_play_once_track_shorter_than_the_video_fades_on_its_end_and_holds_silence(tmp_path):
    source = tmp_path / "tone.wav"
    await encoder.ffmpeg(["-f", "lavfi", "-i", "sine=frequency=440:duration=6", "-c:a", "pcm_s16le", str(source)])
    rate = 8000
    once, looped = tmp_path / "once.wav", tmp_path / "looped.wav"
    await encoder.prepare_bed(str(source), str(once), duration=10.0, loop=False, fade_in=0.0, fade_out=1.0)
    await encoder.prepare_bed(str(source), str(looped), duration=10.0, loop=True, fade_in=0.0, fade_out=1.0)
    played = np.frombuffer(await encoder.decode_mono(str(once), rate=rate), dtype="<f4")
    assert played.size == pytest.approx(10 * rate, abs=rate // 50)
    loud = _rms(played, rate, 1.0, 4.5)
    assert loud > 0.01
    # Its fade ends with the track at 6 s, never at the video's end: the last 4 s hold silence, nothing repeated.
    assert _rms(played, rate, 5.2, 5.9) < loud * 0.8
    assert _rms(played, rate, 6.2, 10.0) < 1e-4
    repeated = np.frombuffer(await encoder.decode_mono(str(looped), rate=rate), dtype="<f4")
    assert _rms(repeated, rate, 6.5, 8.5) > 0.01


# -- 3. An extension's provider: its credits read first, its switch named, its failure said ------------------------


class _Locked:
    async def refresh(self, _row, with_for_update=False):
        return None

    async def flush(self):
        return None


class _Composer:
    """A music provider: ``music_v2*`` continue a clip; its account holds *credits* (``None``: it does not say)."""

    def __init__(self, credits: int | None) -> None:
        self.credits = credits

    def continues_music(self, model: str) -> bool:
        return model.startswith("music_v2")

    async def music_credits(self, _model: str) -> int | None:
        return self.credits


def _take() -> SimpleNamespace:
    return SimpleNamespace(
        id=uuid.UUID(TRACK),
        status=JOB_READY,
        params={"duration_seconds": 174, "provider_id": "p-lyria"},
        prompt="A warm synth bed that builds",
        model="lyria-3.5",
        provider="gemini",
        error=None,
        file_id=uuid.UUID(int=1),
        duration_ms=174_000,
    )


def _providers(monkeypatch, *, eleven: int | None, lyria: int | None = None) -> list:
    from app.llm import router

    queued: list = []

    async def resolve(_db, _user_id, *, model=None, provider_id=None, **_kwargs):
        model = model or "lyria-3.5"
        if model.startswith("music_v2"):
            return (
                _Composer(eleven),
                model,
                SimpleNamespace(id="p-eleven", provider="elevenlabs", display_name="ElevenLabs"),
            )
        return _Composer(lyria), model, SimpleNamespace(id="p-lyria", provider="gemini", display_name="Google")

    async def listed(_db, *, user_id):
        return [{"model": "music_v2_5", "provider_id": "p-eleven", "continues": True}]

    async def enqueue(_db, kind, row, **_options):
        queued.append((kind, row))

    monkeypatch.setattr(music_tracks, "resolve", resolve)
    monkeypatch.setattr(router, "list_music_models_for", listed)
    monkeypatch.setattr(jobs, "enqueue", enqueue)
    return queued


def test_an_extension_on_a_provider_with_no_credits_is_refused_at_call_time_naming_it(monkeypatch):
    queued = _providers(monkeypatch, eleven=0)
    take = _take()
    with pytest.raises(music_tracks.MusicError) as refused:
        asyncio.run(music_tracks.restart_music(_Locked(), USER, take, refit=True, extend=True, duration_seconds=183))
    said = str(refused.value)
    assert "The take's own lyria-3.5 does not continue a clip" in said
    assert "music_v2_5 on ElevenLabs: its account has no credits left" in said and "refit without extend" in said
    # Nothing queued, the take as it was.
    assert not queued
    assert (take.status, take.model, take.file_id, take.params) == (
        JOB_READY,
        "lyria-3.5",
        uuid.UUID(int=1),
        {"duration_seconds": 174, "provider_id": "p-lyria"},
    )
    # With credits there (or an account that does not say), the extension is queued on the model that continues it.
    _providers(monkeypatch, eleven=12_000)
    asyncio.run(music_tracks.restart_music(_Locked(), USER, take, refit=True, extend=True, duration_seconds=183))
    assert (take.model, take.status) == ("music_v2_5", JOB_PENDING)


def test_a_track_on_an_account_with_no_credits_is_never_queued(monkeypatch):
    queued = _providers(monkeypatch, eleven=0, lyria=0)
    with pytest.raises(music_tracks.MusicError, match=r"lyria-3\.5 on Google: its account has no credits left"):
        asyncio.run(music_tracks.start_music(_Locked(), USER, prompt="A calm bed", duration_seconds=60))
    failed = _take()
    failed.status = "failed"
    with pytest.raises(music_tracks.MusicError, match=r"Nothing was composed again: lyria-3\.5 on Google"):
        asyncio.run(music_tracks.restart_music(_Locked(), USER, failed))
    assert not queued and failed.status == "failed"


def test_elevenlabs_reads_the_credits_it_composes_music_with():
    def answer(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/v1/user/subscription"
        return httpx.Response(200, json={"character_count": 100_000, "character_limit": 100_000})

    provider = ElevenLabsProvider("key-elevenlabs-test")
    provider._client = lambda timeout: httpx.AsyncClient(
        base_url=provider.base_url, transport=httpx.MockTransport(answer), timeout=timeout
    )
    assert asyncio.run(provider.music_credits("music_v2_5")) == 0
    provider._client = lambda timeout: httpx.AsyncClient(
        base_url=provider.base_url, transport=httpx.MockTransport(lambda _r: httpx.Response(401)), timeout=timeout
    )
    assert asyncio.run(provider.music_credits("music_v2_5")) is None


def _generate_music(monkeypatch, row: VideoMusic, *, restart, waited) -> str:
    from contextlib import asynccontextmanager

    from app.services.builtin_tools import video_studio as tool
    from app.tools.base import ToolCall

    project = SimpleNamespace(id=uuid.uuid4(), title="Launch", revision=137, spec={"music": {"music_id": str(row.id)}})

    class _Session:
        async def get(self, _model, _key):
            return row

        async def commit(self):
            return None

    @asynccontextmanager
    async def session():
        yield _Session()

    async def returns(value):
        return value

    async def nothing(*_args, **_kwargs):
        return None

    monkeypatch.setattr(tool, "async_session", session)
    monkeypatch.setattr(tool, "_user", lambda _db, _uid: returns(SimpleNamespace(id=row.user_id, role="user")))
    monkeypatch.setattr(tool, "emit_tool_event", nothing)
    monkeypatch.setattr(projects, "load_visible_project", lambda *_a, **_k: returns(project))
    monkeypatch.setattr(projects, "can_manage_project", lambda *_a, **_k: returns(True))
    monkeypatch.setattr(projects, "planned_seconds", lambda *_a, **_k: returns(165.0))
    monkeypatch.setattr(projects, "rendered_seconds", lambda *_a, **_k: returns(175.0))
    monkeypatch.setattr(projects, "render_music_line", nothing)
    monkeypatch.setattr(music_tracks, "restart_music", restart)
    monkeypatch.setattr("app.services.audit.log_audit", nothing)
    monkeypatch.setattr(jobs, "wake", nothing)
    monkeypatch.setattr(jobs, "wait_for", waited)
    _definitions, executor = tool.build_video_studio_tools(None, str(row.user_id), None)
    arguments = {"video_id": str(project.id), "refit": True, "extend": True}
    return asyncio.run(executor.execute_tool(ToolCall(id="m1", name="generate_music", arguments=arguments)))


def _lyria_take() -> VideoMusic:
    return VideoMusic(
        id=uuid.UUID(TRACK),
        user_id=USER.id,
        provider="gemini",
        model="lyria-3.5",
        prompt="A warm synth bed",
        status=JOB_READY,
        file_id=uuid.UUID(int=1),
        duration_ms=174_000,
        params={},
    )


async def _switched(_db, _user, track, **kwargs):
    track.params = {music_tracks.EXTENDED_FROM: music_tracks.extension(track, kwargs["duration_seconds"])}
    track.model, track.provider = "music_v2_5", "elevenlabs"
    return "ElevenLabs"


def test_the_call_names_the_provider_an_extension_switches_to(monkeypatch):
    row = _lyria_take()

    async def waited(*_args, **_kwargs):
        return [SimpleNamespace(id=row.id, status=JOB_PENDING, params={"duration_seconds": 183})]

    result = _generate_music(monkeypatch, row, restart=_switched, waited=waited)
    assert not result.is_error, result.content
    assert "with music_v2_5 on ElevenLabs" in result.content
    assert (
        "The take's own lyria-3.5 does not continue a clip: the extension is composed with music_v2_5 on ElevenLabs"
        in result.content
    )
    assert f"cancel_video_job(music_ids=['{TRACK}'])" in result.content


def test_an_extension_that_ended_without_a_take_is_the_calls_answer(monkeypatch):
    row = _lyria_take()

    async def waited(*_args, **_kwargs):
        reason = "ElevenLabs refused the music request (HTTP 401): You have 0 credits remaining"
        return [
            SimpleNamespace(
                id=row.id,
                status=JOB_READY,
                duration_ms=174_000,
                model="music_v2_5",
                params={music_tracks.EXTEND_FAILED: reason},
            )
        ]

    result = _generate_music(monkeypatch, row, restart=_switched, waited=waited)
    assert result.is_error
    assert "its extension was not made (ElevenLabs refused the music request (HTTP 401)" in result.content
    assert "The take approved still plays, as it was: 174.0 s." in result.content
    assert "is ready" not in result.content


# -- 4. A template card's minimum, said before a save refuses it ---------------------------------------------------


def _card_document(outro_end: float) -> tuple[dict, dict, dict]:
    document = {
        "duration": outro_end,
        "scenes": [
            {"id": "pitch", "type": "statement", "lines": ["One platform"], "start": 0.0, "end": 10.0},
            {
                "id": "template-outro",
                "type": "outro",
                "title": "Thanks for watching",
                "cta": "Start your trial",
                "url": "filigran.io",
                "start": 10.0,
                "end": outro_end,
            },
        ],
    }
    authored = {"scenes": [{"id": "pitch", "type": "statement", "lines": ["One platform"]}]}
    audit = {
        "scenes": [
            {"index": 0, "start": 0.0, "end": 10.0, "shown_until": 10.0, "reveal_end": 0.8, "fit": 1, "findings": []},
            {
                "index": 1,
                "start": 10.0,
                "end": outro_end,
                "shown_until": outro_end - 0.9,
                "reveal_end": 1.2,
                "fit": 1,
                "findings": [],
            },
        ]
    }
    return document, authored, audit


def test_a_template_cards_minimum_is_said_on_a_save_it_passes_before_a_shorter_one_is_refused():
    # The issue's outro: 7.0 s passes, 4.0 s is refused with "make the scene 5.5 s long".
    document, authored, audit = _card_document(17.0)
    said = quality.card_minimums(audit, document, authored)
    # 1.2 s to come in, a glance, 8 words at 3.5 a second, the 0.9 s fade, less the check's 0.25 s: 5.14 s.
    assert said == (
        "The template's cards, the least each lasts with its words read: template_scenes.outro.duration at least "
        "5.2 s for its 8 words (it lasts 7 s)."
    )
    assert not [f for f in quality.findings_from_audit(audit, document, authored) if f.kind == "reading"]
    short_document, _authored, short_audit = _card_document(14.0)
    (reading,) = [f for f in quality.findings_from_audit(short_audit, short_document, authored) if f.kind == "reading"]
    assert "make the scene 5.5 s long (template_scenes.outro.duration)" in reading.message
    assert "template_scenes.outro.duration at least 5.2 s" in quality.card_minimums(
        short_audit, short_document, authored
    )
    # The minimum said is the least the check passes: a tenth under it is refused.
    at_least, _authored, at_least_audit = _card_document(15.2)
    under, _authored, under_audit = _card_document(15.1)
    assert not [f for f in quality.findings_from_audit(at_least_audit, at_least, authored) if f.kind == "reading"]
    assert [f for f in quality.findings_from_audit(under_audit, under, authored) if f.kind == "reading"]
    # A card the author wrote is in the spec, its duration its own: no template card to state.
    assert quality.card_minimums(audit, document, {"scenes": document["scenes"]}) == ""


# -- 5. A queued render's wait ------------------------------------------------------------------------------------


def test_a_queued_render_starts_once_the_slots_ahead_of_it_free():
    assert render_queue.starts_in([600.0], []) == 600.0
    # Two slots: the first to free takes the render queued before it, the second this one.
    assert render_queue.starts_in([300.0, 900.0], [1200.0]) == 900.0
    assert render_queue.starts_in([300.0], [1200.0]) == 1500.0
    assert render_queue.starts_in([], []) is None
    assert render_queue.starts_in([None], []) is None


def test_the_queue_line_gives_the_running_renders_time_left_and_this_ones_start():
    now = datetime(2026, 10, 29, 12, 0, tzinfo=UTC)
    measured = SimpleNamespace(
        scene_id=None,
        status=JOB_RUNNING,
        started_at=now - timedelta(minutes=25),
        preflight={"expected_seconds": 1200, "pace": {"at": (now + timedelta(minutes=18)).isoformat()}},
    )
    wait = render_queue.wait_of([measured], [], now=now)
    assert wait.starts_in_s == pytest.approx(18 * 60)
    assert wait.words() == (
        "the render being made ends in about 18 min (by its measured pace) - this one starts in about 18 min"
    )
    estimated = SimpleNamespace(
        scene_id=None, status=JOB_RUNNING, started_at=now - timedelta(minutes=5), preflight={"expected_seconds": 900}
    )
    queued = SimpleNamespace(scene_id=None, status=JOB_PENDING, started_at=None, preflight={"expected_seconds": 1500})
    wait = render_queue.wait_of([estimated], [queued], now=now)
    assert wait.words() == (
        "the render being made ends in about 10 min (by its pre-flight's estimate); 1 queued before it, about 25 min "
        "between them - this one starts in about 35 min"
    )
    overdue = SimpleNamespace(
        scene_id=None, status=JOB_RUNNING, started_at=now - timedelta(hours=1), preflight={"expected_seconds": 900}
    )
    assert render_queue.wait_of([overdue], [], now=now).words() == (
        "the render being made is past the end its estimate gave - this one starts as soon as a slot frees"
    )
    place = jobs.QueuePlace(
        running=1, ahead=0, per_worker=1, lane="render", render_wait=render_queue.wait_of([measured], [], now=now)
    )
    state = jobs.JobState(
        kind="render", id="r1", status=jobs.STATE_QUEUED, percent=0, stage="Queued", last_progress_at=None, queue=place
    )
    line = jobs.describe(state)
    assert line.startswith("waiting for a worker: 1 render being made now, 0 queued ahead of it in the render lane")
    assert line.endswith(
        "; the render being made ends in about 18 min (by its measured pace) - this one starts in about 18 min"
    )
    assert place.as_dict()["starts_in_seconds"] == 18 * 60


# -- 6. beat_sync on a track with no steady tempo, said at the save ------------------------------------------------


def test_beat_sync_on_music_with_no_steady_tempo_says_what_to_change(monkeypatch):
    async def analysis(_file_id, **_kwargs):
        return {"bpm": None, "first": None, "seconds": 174.0}

    monkeypatch.setattr(beats, "file_analysis", analysis)
    spec = parse_video_spec(_effective(theme={"motion": {"beat_sync": "cuts"}}, music={"file_id": TRACK}))
    grid, note = asyncio.run(beats.video_grid(spec))
    assert grid is None
    assert note.startswith("beat_sync: cuts: the music has no clear beat (its onsets repeat at no steady tempo)")
    assert "every built-in track has one" in note and "theme.motion.beat_sync: off" in note
    # The save's plan carries it: the quality check says it as a note of the video.
    placed = _measured(monkeypatch, _effective(), spoken={}, note=note)
    assert placed.beat_note == note


def test_a_music_source_shorter_than_the_render_is_said_on_the_renders_length():
    assert music_tracks.video_seconds(164.9, 175.0) == 175.0
    note = music_tracks.bed_length_note(music_tracks.Bed.of(Music(music_id=TRACK, loop=False), 175.0), 174.0)
    assert note == (
        "Music: the AI track lasts 174 s under the 175 s video: asked to play once, it fades out at 174 s, leaving the "
        "video's last 1 s without music."
    )
