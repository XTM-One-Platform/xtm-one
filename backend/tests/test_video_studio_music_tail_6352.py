"""A fade-ending track's tail, a video that outgrew its AI track, and a take extended in place.

* the ``music_level`` check: a quiet stretch that starts in the video's last
  seconds - the outro card and the music's fade out, 8 s at most - under a bed
  whose ending is ``fade`` is the track's decay under the closing card, a fact
  of the answer, never a finding; one that starts before is still a finding;
* the verdict: a warning alone never reads ``blocked``;
* the save: a video longer than its looping AI track by more than 2 s is a
  warning of its quality check, said at the edit;
* ``generate_music(refit: true, extend: true)``: the take kept sample for
  sample up to its cut, its tail composed again as the continuation of a clip
  of it on a model that takes one, and the take playing again when the
  extension is not made.
"""

from __future__ import annotations

import asyncio
import json
import uuid
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

from app.llm import elevenlabs_provider
from app.llm.elevenlabs_provider import ElevenLabsProvider
from app.llm.music import MusicReference, MusicRequest, MusicResult
from app.models.video_studio import JOB_FAILED, JOB_PENDING, JOB_READY, VideoMusic
from app.services.video_studio import encoder, jobs, music_level, quality, render, render_answer, render_checks, runners
from app.services.video_studio import music as music_tracks
from app.services.video_studio.spec import Music, parse_video_spec, validate_video_spec

needs_ffmpeg = pytest.mark.skipif(not encoder.encoder_available(), reason="ffmpeg is not installed here")

USER_ID = uuid.uuid4()
TAKE_FILE = uuid.uuid4()


def _blocks(level, seconds: float) -> list[tuple[float, float]]:
    """The momentary loudness of a bed of *seconds*: ``level(t)`` LUFS for the block centred on *t*, every 100 ms."""
    ends = [round(0.4 + k * 0.1, 3) for k in range(round((seconds - 0.4) / 0.1) + 1)]
    return [(end, float(level(end - 0.2))) for end in ends]


def _closing_document(duration: float, outro: float) -> dict:
    return {
        "duration": duration,
        "scenes": [
            {"id": "open", "type": "title", "start": 0.0, "end": 10.0},
            {"id": "body", "type": "statement", "start": 10.0, "end": duration - outro},
            {"id": "close", "type": "outro", "start": duration - outro, "end": duration},
        ],
    }


# ── 1. The closing window ─────────────────────────────────────────────────


def test_the_closing_window_is_the_outro_and_the_fade_out_capped_at_eight_seconds():
    assert music_level.closing_seconds(_closing_document(147.0, 4.0), ending="fade", fade_out=2.5) == pytest.approx(6.5)
    assert music_level.closing_seconds(_closing_document(147.0, 6.0), ending="fade", fade_out=2.5) == 8.0
    # No outro card: the music's own fade out alone; a natural ending lands on the end, nothing is exempt.
    no_outro = {"duration": 30.0, "scenes": [{"id": "a", "type": "statement", "start": 0.0, "end": 30.0}]}
    assert music_level.closing_seconds(no_outro, ending="fade", fade_out=2.5) == 2.5
    assert music_level.closing_seconds(_closing_document(147.0, 6.0), ending="natural", fade_out=2.5) == 0.0


def test_a_quiet_stretch_starting_inside_the_closing_window_is_the_decay_and_one_before_it_a_finding():
    tail = music_level.measure(
        _blocks(lambda t: -60.0 if t >= 140.1 else -18.0, 146.5), duration=146.5, volume=0.5, duck_db=10.0, closing=8.0
    )
    assert tail.problems() == [] and tail.quiet is None
    assert tail.decay == (pytest.approx(139.9), pytest.approx(146.5)) and tail.decay_seconds == pytest.approx(6.6)
    section = tail.summary()
    assert section["status"] == render_checks.PASSED and section["closing_seconds"] == 8.0
    line = music_level.summary_line(section)
    assert "The track decays under the closing card, by design: under -35 LUFS for 6.6 s (2:19.90-2:26.50)" in line
    # The same stretch with no closing window - a natural ending - is the quiet passage it was.
    [problem] = music_level.measure(
        _blocks(lambda t: -60.0 if t >= 140.1 else -18.0, 146.5), duration=146.5, volume=0.5, duck_db=10.0
    ).problems()
    assert problem.code == "quiet_stretch" and "under -35 LUFS for 6.6 s (2:19.90-2:26.50)" in problem.message
    # A stretch that starts before the window is a quiet passage of the track, whatever it runs into.
    early = music_level.measure(
        _blocks(lambda t: -60.0 if t >= 130.0 else -18.0, 146.5), duration=146.5, volume=0.5, duck_db=10.0, closing=8.0
    )
    assert [p.code for p in early.problems()] == ["quiet_stretch"] and early.decay is None
    # A quiet passage mid-track beside the decay: the passage is the finding, the decay a fact.
    both = music_level.measure(
        _blocks(lambda t: -60.0 if 40.0 <= t < 50.0 or t >= 140.0 else -18.0, 146.5),
        duration=146.5,
        volume=0.5,
        duck_db=10.0,
        closing=8.0,
    )
    [problem] = both.problems()
    assert problem.at == pytest.approx(39.8) and both.decay is not None and both.decay[0] == pytest.approx(139.8)
    # A tail too short to be a finding is no decay to say: counted as before, the answer's line unchanged.
    short = music_level.measure(
        _blocks(lambda t: -60.0 if t >= 143.0 else -18.0, 146.5), duration=146.5, volume=0.5, duck_db=10.0, closing=8.0
    )
    assert short.decay is None and short.quiet_seconds == pytest.approx(3.7) and short.problems() == []
    assert "decays" not in music_level.summary_line(short.summary())


async def _checked(monkeypatch, tmp_path, bed_path: str, document: dict, *, closing: float) -> render_checks.Checks:
    from app.services.video_studio import audio_qc, frame_freeze

    async def heard(**_kwargs):
        return SimpleNamespace(as_summary=lambda: {"status": "passed"}, pronunciation={}, findings=[], notes=[])

    async def watched(*_args, **_kwargs):
        return SimpleNamespace(as_summary=lambda: {"status": "passed"}, findings=[])

    monkeypatch.setattr(audio_qc, "check_audio", heard)
    monkeypatch.setattr(frame_freeze, "check_frames", watched)
    duration = float(document["duration"])
    plan = SimpleNamespace(document=document, narration_spans={}, voice_windows=lambda **_: [], duration=duration)
    return await render.check_render(
        final_path="final.mp4",
        stem=None,
        plan=plan,
        media=render._Media(),
        size=(1280, 720),
        fps=30,
        workdir=tmp_path,
        music=encoder.MusicTrack(bed_path, 0.5, 10.0, ()),
        music_closing=closing,
    )


@needs_ffmpeg
async def test_a_video_three_seconds_longer_than_its_fade_ending_track_passes_with_a_fact_and_no_finding(
    monkeypatch, tmp_path
):
    """The issue's first test: a 144 s AI track - a soft intro, its own 8 s decay - under a 147 s video with a 6 s
    outro, ending fade. The bed loops into the intro and fades out over the close: its quiet tail is the decay."""
    track = str(tmp_path / "track.wav")
    expr = "0.25*sin(2*PI*220*t)*pow(min(1,t/8),4)*if(gt(t,136),pow((144-t)/8,3),1)"
    await encoder.ffmpeg(["-f", "lavfi", "-i", f"aevalsrc='{expr}':s=24000:d=144", "-c:a", "pcm_s16le", track])
    bed = music_tracks.Bed.of(Music(music_id=str(uuid.uuid4())), 147.0)
    assert (bed.loop, bed.ending, bed.fade_out) == (True, "fade", 2.5)
    bed_path = str(tmp_path / "bed.wav")
    notes = await music_tracks.make_bed(bed, bed_path, source_path=track, workdir=tmp_path)
    assert notes == ["Music: the AI track lasts 144 s under the 147 s video: it loops at 144 s."]
    document = _closing_document(147.0, 6.0)
    closing = music_level.closing_seconds(document, ending=bed.ending, fade_out=bed.fade_out)
    assert closing == 8.0
    checks = await _checked(monkeypatch, tmp_path, bed_path, document, closing=closing)
    section = checks.audio["music"]
    assert checks.findings == [] and section["status"] == render_checks.PASSED
    assert 139.0 <= section["decay_from"] < 141.0 and section["decay_to"] == pytest.approx(147.0, abs=0.05)
    assert section["decay_seconds"] >= music_level.QUIET_SECONDS
    answer = render_answer.of_checks(checks.as_dict())
    assert answer.status == render_answer.GREEN and answer.findings == []
    assert any("The track decays under the closing card, by design" in fact["what"] for fact in answer.facts)
    assert answer.verdict().startswith("Verdict: all gates green - no finding.")
    # Without its closing window the same bed warns - and a warning alone never blocks the verdict.
    warned = await _checked(monkeypatch, tmp_path, bed_path, document, closing=0.0)
    [finding] = warned.findings
    assert (finding.kind, finding.severity, finding.data) == (
        render_checks.MUSIC_LEVEL,
        render_checks.WARN,
        {"code": "quiet_stretch"},
    )
    answer = render_answer.of_checks(warned.as_dict())
    assert answer.status == render_answer.WARNINGS and answer.verdict().startswith("Verdict: not blocked - 1 warning")


# ── 2. The verdict ────────────────────────────────────────────────────────


def test_a_warning_alone_never_sets_the_verdict_to_blocked():
    warning = render_checks.Finding(render_checks.FROZEN, 3.0, 5.0, "s1", "the picture holds", render_checks.WARN)
    failing = render_checks.Finding(render_checks.STUTTER, 8.0, None, "s2", "a stutter")
    warned = render_answer.of_checks(render_checks.Checks(findings=[warning]).as_dict())
    assert warned.status == render_answer.WARNINGS and warned.blocking == 0
    assert warned.verdict() == (
        "Verdict: not blocked - 1 warning, nothing blocking. These are the issues to report or fix, and the only ones."
    )
    blocked = render_answer.of_checks(render_checks.Checks(findings=[warning, failing]).as_dict())
    assert blocked.status == render_answer.BLOCKED
    assert blocked.verdict().startswith("Verdict: blocked - 2 findings (1 blocking, 1 warning).")
    # A render that ended with only a warning to say still blocks, on why it ended.
    ended = render_answer.of_render(SimpleNamespace(status=JOB_FAILED, error="killed"), render_checks.Checks(findings=[warning]).as_dict())
    assert ended.status == render_answer.BLOCKED and ended.findings[0]["kind"] == render_answer.RENDER_FAILED


# ── 3. Warned at save ─────────────────────────────────────────────────────


class _Rows:
    def __init__(self, rows: dict) -> None:
        self.rows = rows

    async def get(self, model, key, **_options):
        return self.rows.get((model, str(key)))


def _outgrown_spec(track_id: str) -> object:
    raw = {
        "title": "Brand film",
        "music": {"music_id": track_id},
        "scenes": [
            {"id": "open", "type": "title", "title": "Filigran", "duration": 10},
            {"id": "body", "type": "statement", "lines": ["Every signal"], "duration": 30},
            {"id": "close", "type": "outro", "title": "filigran.io", "duration": 6},
        ],
    }
    return parse_video_spec(validate_video_spec(raw))


async def test_a_save_of_a_video_outgrown_by_two_and_a_half_seconds_carries_the_warning():
    """The issue's second test: a 144 s AI track under a 146.5 s video - planned at its spoken narration - warns at the
    save, on the scene where the track loops; 1.5 s over is none."""
    track_id = str(uuid.uuid4())
    spec = _outgrown_spec(track_id)
    row = SimpleNamespace(status=JOB_READY, duration_ms=144_000, params={})
    facts = await quality.content_facts(_Rows({(VideoMusic, track_id): row}), spec, SimpleNamespace(id=USER_ID))
    assert (facts.music_seconds, facts.music_composing) == (144.0, False)
    found = [f for f in quality.content_findings(spec, _closing_document(146.5, 6.0), facts, None) if f.kind == "music_outgrown"]
    [warning] = found
    # On the scene where the track loops: 144 s falls in the outro.
    assert (warning.severity, warning.scene_id, warning.group) == ("advisory", "close", "content")
    assert warning.message.startswith("track 144 s under a 146.5 s video: refit or accept the loop")
    assert "refit: true, extend: true" in warning.message and warning.severity not in render_checks.BLOCKING_SEVERITIES
    said = quality.format_quality_report(quality.QualityReport(scene_count=3, findings=found))
    assert "(advisory): track 144 s under a 146.5 s video: refit or accept the loop" in said
    # Within 2 s it loops unsaid by the check; the save's note says it.
    assert not [f for f in quality.content_findings(spec, _closing_document(145.5, 6.0), facts, None) if f.kind == "music_outgrown"]
    note = await music_tracks.length_note(_Rows({(VideoMusic, track_id): row}), spec.music, 145.5)
    assert note.startswith("Music loops at 144 s of 146 s")
    # The note leaves the outgrown track to the check: said once.
    assert await music_tracks.length_note(_Rows({(VideoMusic, track_id): row}), spec.music, 146.5) is None
    # A track still composed at that length says so.
    composing = SimpleNamespace(status=JOB_PENDING, duration_ms=None, params={"duration_seconds": 144})
    facts = await quality.content_facts(_Rows({(VideoMusic, track_id): composing}), spec, SimpleNamespace(id=USER_ID))
    [warning] = [f for f in quality.content_findings(spec, _closing_document(146.5, 6.0), facts, None) if f.kind == "music_outgrown"]
    assert warning.message.startswith("track 144 s (being composed at that length) under a 146.5 s video")


def test_only_a_looping_ai_track_is_outgrown():
    track = str(uuid.uuid4())
    assert music_tracks.outgrows(Music(music_id=track), 144.0, 146.5)
    assert not music_tracks.outgrows(Music(music_id=track), 144.0, 146.0)
    # Asked to play once, the end is music_short's; an upload or a built-in score is none.
    assert not music_tracks.outgrows(Music(music_id=track, loop=False), 144.0, 150.0)
    assert not music_tracks.outgrows(Music(file_id=track), 144.0, 150.0)
    assert not music_tracks.outgrows(Music(track="deep-focus"), None, 150.0)


# ── 4. A take extended in place ───────────────────────────────────────────


class _Locked:
    async def refresh(self, _row, with_for_update=False):
        return None

    async def flush(self):
        return None


class _Composer:
    """A music provider: ``music_v2*`` continue a clip, the rest do not; its account does not say its credits."""

    def continues_music(self, model: str) -> bool:
        return model.startswith("music_v2")

    async def music_credits(self, _model: str) -> int | None:
        return None


def _take(**fields) -> SimpleNamespace:
    return SimpleNamespace(
        **{
            "status": JOB_READY,
            "params": {"duration_seconds": 152, "provider_id": "p-lyria"},
            "prompt": "A warm synth bed that builds",
            "model": "lyria-3.5",
            "error": None,
            "file_id": TAKE_FILE,
            "duration_ms": 144_000,
            **fields,
        }
    )


def _music_providers(monkeypatch, *, listed: list[dict]) -> list[tuple]:
    from app.llm import router

    resolved: list[tuple] = []

    async def resolve(_db, _user_id, *, model=None, provider_id=None, **_kwargs):
        resolved.append((model, provider_id))
        return _Composer(), model or "lyria-3.5", SimpleNamespace(id=provider_id or "p-lyria", provider="x", display_name="X")

    async def listed_models(_db, *, user_id):
        return listed

    async def enqueue(_db, _kind, _row, **_options):
        return None

    monkeypatch.setattr(music_tracks, "resolve", resolve)
    monkeypatch.setattr(router, "list_music_models_for", listed_models)
    monkeypatch.setattr(jobs, "enqueue", enqueue)
    return resolved


def test_an_extended_refit_keeps_the_take_and_composes_on_a_model_that_continues_it(monkeypatch):
    resolved = _music_providers(monkeypatch, listed=[{"model": "music_v2_5", "provider_id": "p-eleven", "continues": True}])
    row = _take()
    user = SimpleNamespace(id=USER_ID)
    asyncio.run(music_tracks.restart_music(_Locked(), user, row, refit=True, extend=True, duration_seconds=155))
    # The take is kept, never queued for deletion; the track's own model does not continue it, one that does is used.
    assert row.params[music_tracks.EXTENDED_FROM] == {
        "file_id": str(TAKE_FILE),
        "seconds": 144.0,
        "duration_ms": 144_000,
        "cut": 120.0,
    }
    assert music_tracks.REPLACED_MEDIA not in row.params
    assert resolved == [("lyria-3.5", "p-lyria"), ("music_v2_5", "p-eleven")]
    assert (row.model, row.status, row.file_id, row.params["duration_seconds"]) == ("music_v2_5", JOB_PENDING, None, 155)
    # Asked for a model that does not continue a clip: refused, never swapped.
    with pytest.raises(music_tracks.MusicError, match="does not continue a clip"):
        asyncio.run(
            music_tracks.restart_music(_Locked(), user, _take(), refit=True, extend=True, duration_seconds=155, model="lyria-3.5")
        )
    # No model continues one: refused, the take as it was.
    _music_providers(monkeypatch, listed=[{"model": "lyria-3.5", "provider_id": "p-lyria", "continues": False}])
    kept = _take()
    with pytest.raises(music_tracks.MusicError, match="No music model you may use continues"):
        asyncio.run(music_tracks.restart_music(_Locked(), user, kept, refit=True, extend=True, duration_seconds=155))
    assert (kept.status, kept.file_id) == (JOB_READY, TAKE_FILE)


def test_an_extension_needs_a_refit_and_a_take_to_keep(monkeypatch):
    _music_providers(monkeypatch, listed=[{"model": "music_v2_5", "provider_id": "p-eleven", "continues": True}])
    user = SimpleNamespace(id=USER_ID)
    with pytest.raises(music_tracks.MusicError, match="goes with refit"):
        asyncio.run(music_tracks.restart_music(_Locked(), user, _take(), extend=True, duration_seconds=155))
    with pytest.raises(music_tracks.MusicError, match="nothing to extend"):
        asyncio.run(music_tracks.restart_music(_Locked(), user, _take(), refit=True, extend=True, duration_seconds=140))
    with pytest.raises(music_tracks.MusicError, match="too short to keep"):
        asyncio.run(
            music_tracks.restart_music(_Locked(), user, _take(duration_ms=30_000), refit=True, extend=True, duration_seconds=60)
        )
    with pytest.raises(music_tracks.MusicError, match="Only a ready take"):
        asyncio.run(
            music_tracks.restart_music(
                _Locked(), user, _take(status=JOB_FAILED, file_id=None, duration_ms=None), refit=True, extend=True, duration_seconds=155
            )
        )


class _Continuer:
    """A provider continuing a clip: its answer replays 30 s, then a new 35 s tone at another rate and layout."""

    def __init__(self, answer: bytes) -> None:
        self.answer = answer
        self.asked: list[MusicRequest] = []

    async def generate_music(self, request: MusicRequest) -> MusicResult:
        self.asked.append(request)
        return MusicResult(self.answer, "audio/wav", continues_at=30.0)


async def _pcm(path: str, tmp_path: Path, rate: int, channels: int) -> bytes:
    raw = tmp_path / f"{Path(path).stem}-{uuid.uuid4().hex[:6]}.f32"
    await encoder.decode_pcm(path, str(raw), rate=rate, channels=channels)
    return raw.read_bytes()


@needs_ffmpeg
async def test_a_refit_with_extend_keeps_the_first_120_s_of_the_take_bit_identical(tmp_path):
    """The issue's third test: a 144 s take extended to 155 s keeps its first 120 s sample for sample."""
    take = str(tmp_path / "take.wav")
    await encoder.ffmpeg(
        ["-f", "lavfi", "-i", "aevalsrc='0.3*sin(2*PI*261.6*t)*(0.6+0.4*sin(2*PI*0.1*t))':s=16000:d=144", "-c:a", "pcm_s16le", take]
    )
    answer = tmp_path / "answer.wav"
    await encoder.ffmpeg(
        ["-f", "lavfi", "-i", "aevalsrc='if(lt(t,30),0,0.2*sin(2*PI*329.6*t))':s=22050:d=65", "-ac", "2", "-c:a", "pcm_s16le", str(answer)]
    )
    provider = _Continuer(answer.read_bytes())
    request = music_tracks.build_request(model="music_v2_5", prompt="A warm synth bed", duration_seconds=155 - 120)
    workdir = tmp_path / "work"
    workdir.mkdir()
    path, made = await music_tracks.extend_take(provider, request, take, cut=120.0, workdir=workdir)
    # The model was given the 30 s before the cut, to continue for 35 s.
    [asked] = provider.asked
    assert asked.continue_from.mime == "audio/wav" and asked.continue_from.seconds == pytest.approx(30.0)
    assert asked.duration_seconds == 35.0 and made.continues_at == 30.0
    clip = tmp_path / "clip.wav"
    clip.write_bytes(asked.continue_from.audio)
    rate = 16000
    original = await _pcm(take, tmp_path, rate, 1)
    assert await _pcm(str(clip), tmp_path, rate, 1) == original[90 * rate * 4 : 120 * rate * 4]
    # The extended take: the take's own rate and layout, 120 s of it then the continuation.
    info = await encoder.probe(path)
    assert (info.sample_rate, info.channels) == (rate, 1) and info.duration == pytest.approx(155.0, abs=0.05)
    extended = await _pcm(path, tmp_path, rate, 1)
    head = 120 * rate * 4
    assert extended[:head] == original[:head]
    # Past the crossfade it is the continuation as the model answered it, from where its new music starts.
    continuation = (await _pcm(str(answer), tmp_path, rate, 1))[30 * rate * 4 :]
    fade = round(music_tracks.EXTEND_CROSSFADE_SECONDS * rate) * 4
    assert extended[head + fade :] == continuation[fade:]
    assert extended[head : head + fade] != original[head : head + fade]


def _run_music_row(params: dict) -> VideoMusic:
    return VideoMusic(
        id=uuid.uuid4(),
        user_id=USER_ID,
        provider="elevenlabs",
        model="music_v2_5",
        prompt="A warm synth bed",
        params=params,
        status=JOB_PENDING,
    )


def _runner(monkeypatch, row: VideoMusic, extended) -> tuple[list[dict], list[list]]:
    from contextlib import asynccontextmanager

    from app.models.user import User
    from app.services.video_studio import assets

    marks: list[dict] = []
    deleted: list[list] = []

    class _Db:
        async def get(self, model, _key):
            return User(id=USER_ID, email="maker@example.com") if model is User else row

        async def commit(self):
            return None

    @asynccontextmanager
    async def session():
        yield _Db()

    async def resolve_continuing(_db, _user_id, *, model=None, provider_id=None, asked=False):
        return _Composer(), model, SimpleNamespace(id=provider_id, provider="elevenlabs")

    async def mark(_kind, _job_id, **changes):
        marks.append(changes)

    async def delete_files(ids):
        deleted.append(list(ids))

    async def probe(_path):
        return encoder.MediaInfo(155.0, 0, 0, 0.0, 0, True, False, 16000, 1)

    async def stored(_path, **_kwargs):
        return SimpleNamespace(id=uuid.UUID(int=7))

    monkeypatch.setattr(runners, "async_session", session)
    monkeypatch.setattr(music_tracks, "resolve_continuing", resolve_continuing)
    monkeypatch.setattr(runners, "_extended", extended)
    monkeypatch.setattr(jobs, "mark", mark)
    monkeypatch.setattr(assets, "delete_files", delete_files)
    monkeypatch.setattr(assets, "store_path", stored)
    monkeypatch.setattr(encoder, "probe", probe)
    return marks, deleted


def _extending(**more) -> dict:
    take = {"file_id": str(TAKE_FILE), "seconds": 144.0, "duration_ms": 144_000, "cut": 120.0}
    return {"duration_seconds": 155, "provider_id": "p-eleven", music_tracks.EXTENDED_FROM: take, **more}


def test_the_extended_take_replaces_the_kept_one_once_it_is_stored(monkeypatch, tmp_path):
    asked: list[float] = []

    async def extended(_provider, request, take, _workdir):
        asked.append(request.duration_seconds)
        out = tmp_path / "extended.wav"
        out.write_bytes(b"RIFF")
        return str(out), MusicResult(b"", "audio/mpeg", continues_at=30.0)

    row = _run_music_row(_extending())
    marks, deleted = _runner(monkeypatch, row, extended)
    assert asyncio.run(runners.run_music(str(row.id))).kind == "done"
    # The continuation runs from the cut to the length asked.
    assert asked == [35.0]
    final = marks[-1]
    assert (final["status"], final["file_id"], final["duration_ms"]) == (JOB_READY, uuid.UUID(int=7), 155_000)
    assert music_tracks.EXTENDED_FROM not in final["params"] and deleted == [[str(TAKE_FILE)]]


def test_an_extension_that_is_not_made_leaves_the_take_playing(monkeypatch):
    async def refused(*_args):
        raise music_tracks.MusicError("ElevenLabs refused the music request (HTTP 403): inpainting is not enabled")

    row = _run_music_row(_extending())
    marks, deleted = _runner(monkeypatch, row, refused)
    assert asyncio.run(runners.run_music(str(row.id))).kind == "done"
    final = marks[-1]
    assert (final["status"], final["file_id"], final["duration_ms"], final["error"]) == (
        JOB_READY,
        TAKE_FILE,
        144_000,
        None,
    )
    assert final["params"][music_tracks.EXTEND_FAILED].startswith("ElevenLabs refused the music request (HTTP 403)")
    assert music_tracks.EXTENDED_FROM not in final["params"] and deleted == [[]]


def test_deleting_a_track_mid_extension_deletes_the_take_it_keeps():
    replaced = str(uuid.uuid4())
    row = _run_music_row(_extending(**{music_tracks.REPLACED_MEDIA: [replaced]}))
    assert music_tracks.held_files(row) == [str(TAKE_FILE), replaced]
    row.file_id, row.params = TAKE_FILE, {}
    assert music_tracks.held_files(row) == [str(TAKE_FILE)]


def test_an_extension_the_engine_gave_up_on_leaves_the_take_playing(monkeypatch):
    row = _run_music_row(_extending())
    marks: list[dict] = []

    async def load(_kind, _job_id):
        return row

    async def mark(_kind, _job_id, **changes):
        marks.append(changes)

    monkeypatch.setattr(jobs, "load", load)
    monkeypatch.setattr(jobs, "mark", mark)
    asyncio.run(runners.dead_letter("music", row.id, "The music failed: the worker restarted"))
    [final] = marks
    assert (final["status"], final["file_id"]) == (JOB_READY, TAKE_FILE)
    assert final["params"][music_tracks.EXTEND_FAILED].startswith("The music failed")


def test_generate_music_extends_the_take_in_a_refit_and_says_what_it_keeps(monkeypatch):
    from contextlib import asynccontextmanager

    from app.services.builtin_tools import video_studio as tool
    from app.services.video_studio import projects
    from app.tools.base import ToolCall

    track_id = str(uuid.uuid4())
    project = SimpleNamespace(id=uuid.uuid4(), title="Brand film", revision=7, spec={"music": {"music_id": track_id}})
    row = VideoMusic(
        id=uuid.UUID(track_id),
        user_id=USER_ID,
        provider="elevenlabs",
        model="music_v2_5",
        prompt="A warm synth bed",
        status=JOB_READY,
        file_id=TAKE_FILE,
        duration_ms=144_000,
        params={},
    )
    asked: dict = {}

    class _Session(_Rows):
        async def commit(self):
            return None

    @asynccontextmanager
    async def session():
        yield _Session({(VideoMusic, track_id): row})

    async def returns(value):
        return value

    async def restart(_db, _user, track, **kwargs):
        asked.update(kwargs)
        track.params = {music_tracks.EXTENDED_FROM: music_tracks.extension(track, kwargs["duration_seconds"])}
        return "ElevenLabs"

    async def nothing(*_args, **_kwargs):
        return None

    async def waited(*_args, **_kwargs):
        return [SimpleNamespace(id=row.id, status=JOB_PENDING, params={"duration_seconds": 155})]

    monkeypatch.setattr(tool, "async_session", session)
    monkeypatch.setattr(tool, "_user", lambda _db, _uid: returns(SimpleNamespace(id=USER_ID, role="user")))
    monkeypatch.setattr(tool, "emit_tool_event", nothing)
    monkeypatch.setattr(projects, "load_visible_project", lambda *_a, **_k: returns(project))
    monkeypatch.setattr(projects, "can_manage_project", lambda *_a, **_k: returns(True))
    monkeypatch.setattr(projects, "planned_seconds", lambda *_a, **_k: returns(146.5))
    monkeypatch.setattr(projects, "rendered_seconds", lambda *_a, **_k: returns(None))
    monkeypatch.setattr(projects, "render_music_line", nothing)
    monkeypatch.setattr(music_tracks, "restart_music", restart)
    monkeypatch.setattr("app.services.audit.log_audit", nothing)
    monkeypatch.setattr(jobs, "wake", nothing)
    monkeypatch.setattr(jobs, "wait_for", waited)
    _definitions, executor = tool.build_video_studio_tools(None, str(USER_ID), None)
    alone = asyncio.run(
        executor.execute_tool(ToolCall(id="m0", name="generate_music", arguments={"video_id": str(project.id), "extend": True}))
    )
    assert alone.is_error and "goes with refit: true" in alone.content and not asked
    arguments = {"video_id": str(project.id), "refit": True, "extend": True}
    result = asyncio.run(executor.execute_tool(ToolCall(id="m1", name="generate_music", arguments=arguments)))
    assert not result.is_error, result.content
    assert (asked["refit"], asked["extend"], asked["duration_seconds"]) == (True, True, 155)
    assert (
        "Extending the take: its first 120 s are kept as they are, its last 24 s composed again as the continuation "
        "of the 30 s before them, to 155 s, with music_v2_5" in result.content
    )


# ── The model that continues a clip ───────────────────────────────────────


def test_eleven_music_v2_continues_a_clip_through_a_composition_plan():
    provider = ElevenLabsProvider(api_key="k")
    assert provider.continues_music("music_v2_5") and provider.continues_music("music_v2")
    assert not provider.continues_music("music_v1")
    clip = MusicReference(b"RIFF", "audio/wav", 30.0)
    request = MusicRequest(
        model="music_v2_5",
        prompt="Warm synth bed, slow build; soft pads",
        duration_seconds=35,
        negative_prompt="drums",
        continue_from=clip,
    )
    plan, replayed = elevenlabs_provider._continuation_plan(request, "song-1")
    replay, outro = plan["chunks"]
    assert replayed == 30_000 and replay == {"song_id": "song-1", "range": {"start_ms": 0, "end_ms": 30_000}}
    assert (outro["text"], outro["duration_ms"], outro["context_adherence"]) == ("[Outro]", 35_000, "high")
    assert outro["conditioning_ref"] == {"song_id": "song-1", "range": {"start_ms": 0, "end_ms": 30_000}}
    assert outro["positive_styles"] == ["Warm synth bed", "slow build", "soft pads", "instrumental"]
    assert outro["negative_styles"] == ["drums", "vocals"]
    # A continuation longer than a chunk is cut in chunks of 2 minutes at most, the first conditioned on the clip.
    long, _ = elevenlabs_provider._continuation_plan(
        MusicRequest(model="music_v2_5", prompt="Bed", duration_seconds=150, continue_from=clip), "song-1"
    )
    lengths = [chunk.get("duration_ms") for chunk in long["chunks"][1:]]
    assert lengths == [75_000, 75_000] and "conditioning_ref" not in long["chunks"][2]


def test_eleven_music_uploads_the_clip_and_says_where_its_continuation_starts(monkeypatch):
    seen: list[tuple[str, bytes]] = []
    real = httpx.AsyncClient

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append((request.url.path, request.read()))
        if request.url.path == "/v1/music/upload":
            return httpx.Response(200, json={"song_id": "song-1"})
        return httpx.Response(200, content=b"ID3-music", headers={"content-type": "audio/mpeg"})

    def client(*args, **kwargs):
        kwargs["transport"] = httpx.MockTransport(handler)
        return real(*args, **kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", client)
    request = MusicRequest(
        model="music_v2_5", prompt="Bed", duration_seconds=35, continue_from=MusicReference(b"RIFF-clip", "audio/wav", 30.0)
    )
    made = asyncio.run(ElevenLabsProvider(api_key="k").generate_music(request))
    assert (made.audio, made.mime, made.continues_at) == (b"ID3-music", "audio/mpeg", 30.0)
    (upload, sent), (compose, body) = seen
    assert upload == "/v1/music/upload" and b"RIFF-clip" in sent and b'filename="continue-from.wav"' in sent
    composed = json.loads(body)
    assert compose == "/v1/music" and composed["model_id"] == "music_v2_5"
    assert composed["composition_plan"]["chunks"][0] == {"song_id": "song-1", "range": {"start_ms": 0, "end_ms": 30_000}}
    # A model whose plans take no clip is refused before anything is uploaded.
    with pytest.raises(Exception, match="does not continue a clip"):
        asyncio.run(
            ElevenLabsProvider(api_key="k").generate_music(
                MusicRequest(model="music_v1", prompt="Bed", continue_from=MusicReference(b"x", "audio/wav", 30.0))
            )
        )
    assert len(seen) == 2
