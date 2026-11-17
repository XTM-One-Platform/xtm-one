"""A fallback take re-checked once the voice pipeline or its lexicon entry changed. Synthetic models only.

A narration the video's voice says wrong at its speed is placed with a slower take (0.85), and the ready render
playing it used to be answered "already covers this revision" for good. On the synthetic speech models of
``test_video_studio_pronunciation_voice`` (a model saying what it is sent, a scripted speech-to-text) and two real
``render.run_render`` passes - their media gathered for real, their frames, mix and files stubbed:

* the fixture's second narration fails at 1.0 and passes at 0.85: the render places the 0.85 take, notes it with the
  fix that clears it and records it (``drawing.fallbacks``, ``takes``, ``encoded_as``);
* its term's lexicon entry changed, ``start_render`` answers that render with a re-check of that scene offered,
  nothing queued; the re-check speaks that narration alone, which now passes at 1.0; the splice places every other
  narration as it played, keeps the ready render's frames - none drawn, ``measured.frames`` 0 - and notes the
  narration at the video's speed;
* a voice pipeline change offers the same, and the voice check hears the take again from its first take;
* a video playing no fallback take is still answered "already covers"; a scene re-checked to no avail is not offered
  again; an older render is offered from its voice summary - which counts its takes, never iterated as
  them - and the voice cache's record of the scene; an offer that cannot be computed answers the render as covering
  the revision, said why, never a crash;
* the save's quality check lists the fallback take as a fact with its fix; ``render_video`` offers, re-checks when
  asked and in an automation.
"""

from __future__ import annotations

import asyncio
import dataclasses
import hashlib
import io
import struct
import uuid
import wave
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.llm.router import SpeechTarget
from app.llm.speech import SpeechRequest, SpeechResult
from app.models.user import User
from app.models.video_studio import JOB_PENDING, JOB_READY, VideoProject, VideoRender
from app.services.video_studio import (
    assets,
    audio_qc,
    compositor,
    encoder,
    frame_freeze,
    jobs,
    lexicon_store,
    projects,
    quality,
    render,
    render_checks,
    render_retention,
    voice,
    voice_check,
    voice_recheck,
)
from app.services.video_studio import lexicon as lex
from app.services.video_studio.render_checks import PASSED
from app.services.video_studio.spec import parse_video_spec, validate_video_spec
from tests.test_video_studio_pronunciation_voice import (  # noqa: F401 - pipeline is a fixture
    _SAID,
    PLAIN,
    SEED,
    _Transcriber,
    _Voice,
    pipeline,
)
from tests.video_voice_doubles import speech_config

DEMO = "Run the drills with OpenAEV every week."
SCENES = {
    "s1": "OpenCTI maps every threat to the team.",
    "s2": DEMO,
    "s3": "XTM One ties the platforms together.",
}
#: The lexicon fix of the term the video's voice says wrong at its speed: a reading it says right.
FIXED = {"text": "OpenAEV", "say": "Open Ave"}


def _take(seed: int, speed: float) -> bytes:
    """A synthetic take, as long as its speed makes it: half a second at 1.0, longer slower."""
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as out:
        out.setnchannels(1)
        out.setsampwidth(2)
        out.setframerate(48000)
        out.writeframes(struct.pack("<h", seed % 30000) * int(24000 / max(speed, 0.1)))
    return buffer.getvalue()


class _Hurried(_Voice):
    """A synthetic model that slips on OpenAEV at the video's speed - glued or respelled - never slower."""

    def __init__(self, *, slips: bool = True) -> None:
        super().__init__(PLAIN)
        self.slips = {"OpenAEV": "OpenA", "Open AEV": "OpenA"} if slips else {}

    def said(self, request: SpeechRequest) -> list[str]:
        text = request.text
        if (request.speed or 1.0) >= 1.0:
            for written, spoken in self.slips.items():
                text = text.replace(written, spoken)
        return super().said(dataclasses.replace(request, text=text))

    async def synthesize_speech(self, request: SpeechRequest) -> SpeechResult:
        self.requests.append(request)
        data = _take(len(_SAID) + 1, request.speed or 1.0)
        _SAID[hashlib.sha256(data).hexdigest()] = self.said(request)
        return SpeechResult(data, "audio/wav", request.voice or "narrator", [])


def _film(*pronunciations: dict) -> dict:
    return validate_video_spec(
        {
            "title": "Drills",
            "freestyle": True,
            "voiceover": {"voice": "brand", "pronunciations": list(pronunciations)},
            "captions": {"enabled": False},
            "scenes": [
                {"id": sid, "type": "title", "title": sid.upper(), "duration": 6, "narration": text}
                for sid, text in SCENES.items()
            ],
        }
    )


def _lexicon(voiceover) -> lex.Lexicon:
    return lex.merge(SEED, getattr(voiceover, "pronunciations", None) or ())


@pytest.fixture
def studio(pipeline, monkeypatch, tmp_path):  # noqa: F811
    """A video, its renders and two real ``run_render`` passes: media gathered for real, frames and files stubbed."""
    maker = User(id=uuid.uuid4(), email="maker@example.com", role="user")
    project = VideoProject(id=uuid.uuid4(), title="Drills", spec=_film(), revision=1, latest_render_id=None)
    world = SimpleNamespace(
        project=project,
        user=maker,
        rows={},
        effective=_film(),
        model=_Hurried(),
        drawn=0,
        watched=0,
        enqueued=[],
        downloaded=[],
        tmp=tmp_path,
    )
    world.target = SpeechTarget(provider=world.model, model="tts-1-hd", config=speech_config(), voice="narrator")

    class _Db:
        async def get(self, model, key, **_options):
            if model is VideoRender:
                return world.rows.get(key if isinstance(key, uuid.UUID) else uuid.UUID(str(key)))
            return {VideoProject: project, User: maker}.get(model)

        async def execute(self, statement, _params=None):
            sql = str(statement)
            rows = []
            if "FROM video_renders" in sql and "ORDER BY video_renders.finished_at DESC" in sql:
                ready = [row for row in world.rows.values() if row.status == JOB_READY and row.scene_id is None]
                rows = sorted(ready, key=lambda row: row.finished_at, reverse=True)[:1]
            return SimpleNamespace(scalars=lambda: SimpleNamespace(all=lambda: rows), all=lambda: [], first=lambda: None)

        def add(self, row) -> None:
            row.id = row.id or uuid.uuid4()
            world.rows[row.id] = row

        async def flush(self):
            return None

        async def commit(self):
            return None

        async def rollback(self):
            return None

        def expunge(self, _row):
            return None

    world.db = _Db()

    @asynccontextmanager
    async def session():
        yield world.db

    async def effective_spec(_db, _project, _user):
        return SimpleNamespace(spec=world.effective, template=None, notes=[])

    async def for_voiceover(_db, voiceover):
        return _lexicon(voiceover)

    async def references(spec, _user, _template_id):
        return {
            "takes": {},
            "clips": {},
            "animations": {},
            "animation_notes": [],
            "decks": {},
            "palette": {},
            "voice_target": world.target,
            "voice_note": None,
            "insert_files": {},
            "insert_ratios": {},
            "missing_inserts": {},
            "steadied": {},
            "logo_checks": {},
            "screens": {},
            "names": {},
            "transcriber": _Transcriber(),
            "unchecked_reason": None,
            "lexicon": _lexicon(spec.voiceover),
        }

    async def render_frames(document, _assets, *, workdir, tracker, **_kwargs):
        world.drawn += 1
        tracker.total = tracker.done = int(document["frames"])
        out = Path(workdir) / "video.mp4"
        out.write_bytes(b"MP4")
        return str(out)

    async def write(*_args, out_path=None, **_kwargs):
        Path(out_path).write_bytes(b"X")

    async def probe(_path):
        return SimpleNamespace(duration=18.0, width=1920, height=1080, has_audio=True, frame_count=None, fps=60.0)

    async def frame(_path, *, at, out_path, width=None):
        Path(out_path).write_bytes(b"JPG")

    async def store_path(path, **_kwargs):
        return SimpleNamespace(id=uuid.uuid4(), size=10)

    async def download_file(file_id, path):
        world.downloaded.append(str(file_id))
        Path(path).write_bytes(b"MP4")
        return SimpleNamespace(filename="source.mp4")

    async def heard(**_kwargs):
        return audio_qc.AudioCheck(PASSED, [], pronunciation={"status": PASSED})

    async def watched(*_args, **_kwargs):
        world.watched += 1
        return SimpleNamespace(as_summary=lambda: {"status": PASSED}, findings=[])

    async def nothing(*_args, **_kwargs):
        return None

    async def none(*_args, **_kwargs):
        return []

    async def none_replaced(*_args, **_kwargs):
        return render_retention.Deletion()

    async def lint(_db, _project, _user, **_kwargs):
        return world.effective, [], []

    async def enqueue(_db, kind, row, **_kwargs):
        world.enqueued.append(row)

    async def no_preflight(*_args, **_kwargs):
        raise RuntimeError("no estimate in this test")

    monkeypatch.setattr(render, "async_session", session)
    monkeypatch.setattr(render, "bind_llm_caller", nothing)
    monkeypatch.setattr(render, "_read_references", references)
    monkeypatch.setattr(projects, "effective_spec", effective_spec)
    monkeypatch.setattr(lexicon_store, "for_voiceover", for_voiceover)
    monkeypatch.setattr(compositor, "render_frames", render_frames)
    monkeypatch.setattr(encoder, "mix_audio", write)
    monkeypatch.setattr(encoder, "mux", write)
    monkeypatch.setattr(encoder, "probe", probe)
    monkeypatch.setattr(encoder, "extract_frame", frame)
    monkeypatch.setattr(assets, "store_path", store_path)
    monkeypatch.setattr(assets, "download_file", download_file)
    monkeypatch.setattr(audio_qc, "check_audio", heard)
    monkeypatch.setattr(frame_freeze, "check_frames", watched)
    monkeypatch.setattr(jobs, "mark", nothing)
    monkeypatch.setattr(render.retention, "touch", nothing)
    monkeypatch.setattr(render_retention, "replaced_previews", none_replaced)
    # A start: no render in flight, nothing to gate or estimate, the queue recorded.
    monkeypatch.setattr(render_checks, "gate", none)
    monkeypatch.setattr(projects, "_lint", lint)
    monkeypatch.setattr(projects, "_lock_render_starts", nothing)
    monkeypatch.setattr(projects, "render_left_unresumed", nothing)
    monkeypatch.setattr(jobs, "renders_in_flight", none)
    monkeypatch.setattr(jobs, "enqueue", enqueue)
    monkeypatch.setattr("app.services.audit.log_audit", nothing)
    monkeypatch.setattr("app.services.video_studio.render_preflight.preflight", no_preflight)
    return world


def _rendered(world, row: VideoRender, name: str) -> VideoRender:
    """*row* run by the worker, as a whole render (``render.run_render``)."""
    workdir = world.tmp / name
    workdir.mkdir()
    asyncio.run(render.run_render(str(row.id), workdir=workdir))
    return row


def _first_render(world, name: str = "first") -> VideoRender:
    row = VideoRender(
        id=uuid.uuid4(), project_id=world.project.id, user_id=world.user.id, status=JOB_PENDING, quality="high", revision=1
    )
    world.rows[row.id] = row
    _rendered(world, row, name)
    row.finished_at = datetime.now(UTC)
    return row


def _start(world, **kwargs) -> projects.StartedRender:
    return asyncio.run(projects.start_render(world.db, world.project, world.user, quality="high", **kwargs))


def _edit(world, effective: dict) -> None:
    world.effective = effective
    world.project.spec = effective
    world.project.revision += 1


def _speaker(world, effective: dict) -> voice_check.Speaker:
    spec = parse_video_spec(effective)
    return voice_check.Speaker(world.target, spec.voiceover, _Transcriber(), None, lexicon=_lexicon(spec.voiceover))


# ── The acceptance: a lexicon fix re-checked, spliced, no frame drawn ─────


def test_a_lexicon_fix_offers_the_recheck_speaks_that_scene_alone_and_splices_it_with_no_frame_drawn(studio):
    ready = _first_render(studio)
    assert ready.status == JOB_READY and studio.drawn == 1
    # The render notes the fallback: the 0.85 take, why, and the fix that clears it.
    notes = " ".join(ready.notes or [])
    assert "Scene s2: its narration is spoken slower (0.85, the video's speed is 1)" in notes
    assert (
        "Fallback take: scene `s2` plays a 0.85 take: the video's voice heard OpenAEV as 'OpenA' at its speed 1 - a "
        "lexicon entry for OpenAEV the voice says right at full speed" in notes
    )
    fallback = ready.drawing["fallbacks"]["s2"]
    assert (fallback["kind"], fallback["speed"], fallback["pipeline"]) == ("slower", 0.85, voice_check.pipeline_version())
    assert set(ready.drawing["takes"]) == {"s1", "s2", "s3"} and ready.drawing["encoded_as"]
    assert set(ready.drawing["fallbacks"]) == {"s2"}

    # The term's lexicon entry changed: every scene's drawn digest moves (the pronunciations time the narrations),
    # yet only that narration's voice did - the render covers the picture, a re-check of scene s2 is offered.
    _edit(studio, _film(FIXED))
    offered = _start(studio)
    assert offered.render is ready and studio.enqueued == []
    (recheck,) = offered.recheck
    assert (recheck.scene_id, recheck.why) == ("s2", voice_recheck.LEXICON_MOVED)
    said = voice_recheck.offer_line(str(ready.id), studio.project.revision, offered.recheck)
    assert f"Render `{ready.id}` covers the picture of revision 2; scene `s2` plays a 0.85 take, its lexicon entry " in said
    assert "re-check it?" in said and "Nothing was queued" in said

    # Accepted: scene s2 alone is spoken again and heard back - it now passes at 1.0.
    asked = len(studio.model.requests)
    hearing = voice_recheck.Hearing(_speaker(studio, studio.effective), studio.effective)
    (outcome,) = asyncio.run(voice_recheck.recheck(hearing, ["s2"]))
    assert outcome.cleared and outcome.share == 1.0
    assert outcome.said() == (
        "scene `s2` spoken again at 1 on tts-1-hd, voice brand, heard 100% of its words as written: it passes - the "
        "fallback take is replaced"
    )
    respoken = studio.model.requests[asked:]
    assert len(respoken) == 1 and respoken[0].speed == 1.0 and "Open Ave" in respoken[0].text

    # The splice: queued as a whole render recording what it splices into.
    spliced = _start(studio, splice=voice_recheck.Splice(str(ready.id), ("s2",)))
    row = spliced.render
    assert studio.enqueued == [row] and row.drawing == {"splice": {"source": str(ready.id), "scenes": ["s2"]}}
    asked = len(studio.model.requests)
    _rendered(studio, row, "splice")
    # Nothing spoken again: s1 and s3 as the ready render played them, s2 as its re-check placed it.
    assert studio.model.requests[asked:] == []
    voices = {summary["scene_id"]: summary for summary in row.checks["voice"]}
    assert "speed" not in voices["s2"] and voices["s1"]["status"] == voices["s3"]["status"] == voice_check.PASSED
    # No frame drawn: the ready render's MP4 under the new sound, its picture checks its own.
    assert row.status == JOB_READY and studio.drawn == 1 and studio.watched == 1
    assert studio.downloaded == [str(ready.file_id)]
    assert row.measured["frames"] == 0 and row.measured["frames_reused"] == 0
    assert row.measured["spliced_from"] == str(ready.id) and row.measured["frames_spliced"] > 0
    assert row.notes[0].startswith(
        f"Spliced into render `{ready.id}`: scene `s2` plays its narration at the video's speed (1). None of its "
    )
    assert row.drawing["fallbacks"] == {} and row.drawing["encoded_as"] == ready.drawing["encoded_as"]
    assert studio.project.latest_render_id == row.id
    # It covers the revision now, nothing offered.
    row.finished_at = datetime.now(UTC)
    again = _start(studio)
    assert again.render is row and again.covered_from == studio.project.revision and again.recheck == []


def test_a_new_take_that_changes_what_is_drawn_is_drawn_again_and_said_so(studio):
    ready = _first_render(studio)
    _edit(studio, _film(FIXED))
    asyncio.run(voice_recheck.recheck(voice_recheck.Hearing(_speaker(studio, studio.effective), studio.effective), ["s2"]))
    row = _start(studio, splice=voice_recheck.Splice(str(ready.id), ("s2",))).render
    # The ready render's frames were drawn from something else (another release's page, say): drawn again.
    ready.drawing = {**ready.drawing, "encoded_as": "another drawing"}
    _rendered(studio, row, "splice")
    assert studio.drawn == 2 and studio.downloaded == [] and "frames_spliced" not in row.measured
    assert row.notes[0].startswith(f"Re-checked into a new render of render `{ready.id}`'s revision: scene `s2` plays")
    assert "Its frames were drawn again" in row.notes[0]


# ── A voice pipeline change ────────────────────────────────────────────────


def test_a_pipeline_change_offers_the_recheck_and_the_voice_check_hears_the_take_again(studio, monkeypatch):
    ready = _first_render(studio)
    # Nothing in the video changed: answered "already covers".
    covered = _start(studio)
    assert covered.covered_from == 1 and covered.recheck == []
    # A release moves the retry ladder: the 0.85 take was placed under another pipeline.
    monkeypatch.setattr(voice_check, "LADDER_VERSION", voice_check.LADDER_VERSION + 1)
    offered = _start(studio)
    assert offered.render is ready and offered.covered_from is not None
    assert [(r.scene_id, r.why) for r in offered.recheck] == [("s2", voice_recheck.PIPELINE_MOVED)]
    assert "scene `s2` plays a 0.85 take, the voice check changed since: re-check it?" in voice_recheck.offer_line(
        str(ready.id), 1, offered.recheck
    )
    # The voice check no longer places that take unheard: the ladder hears the narration again from its first take.
    asked = len(studio.model.requests)
    spec = parse_video_spec(studio.effective)
    line = next(line for line in voice.lines_of(spec) if line.scene_id == "s2")
    spoken = asyncio.run(voice_check.speak_checked(_speaker(studio, studio.effective), line, workdir=str(studio.tmp)))
    assert [a.kind for a in spoken.attempts][:2] == ["same", "again"] and len(studio.model.requests) > asked
    # Still only right slower with this model: its record says the pipeline it was placed under now.
    assert spoken.placed.speed == 0.85
    record = asyncio.run(voice_check.load_record(voice.digest_of(studio.target, line, spec.voiceover, _lexicon(spec.voiceover))))
    assert record["pipeline"] == voice_check.pipeline_version() and not voice_check.stale_fallback(record)


def test_a_video_playing_no_fallback_take_is_still_answered_already_covers(studio, monkeypatch):
    studio.model.slips = {}
    ready = _first_render(studio)
    assert ready.drawing["fallbacks"] == {} and not any("Fallback take" in note for note in ready.notes or [])
    monkeypatch.setattr(voice_check, "LADDER_VERSION", voice_check.LADDER_VERSION + 1)
    started = _start(studio)
    assert started.render is ready and started.covered_from == 1 and started.recheck == [] and studio.enqueued == []


def test_a_scene_rechecked_to_no_avail_is_not_offered_again(studio, monkeypatch):
    ready = _first_render(studio)
    monkeypatch.setattr(voice_check, "LADDER_VERSION", voice_check.LADDER_VERSION + 1)
    (recheck,) = _start(studio).recheck
    (outcome,) = asyncio.run(
        voice_recheck.recheck(voice_recheck.Hearing(_speaker(studio, studio.effective), studio.effective), ["s2"])
    )
    assert not outcome.cleared and outcome.still.kind == voice_check.SLOWER_TAKE
    said = outcome.said()
    assert said.startswith("scene `s2` spoken again at 1 on tts-1-hd, voice brand, heard ")
    assert "of its words as written at best: it fails at that speed - the fallback take is kept (a 0.85 take: " in said
    assert "the video's voice heard OpenAEV as 'OpenA' at its speed 1, measured now - " in said
    asyncio.run(voice_recheck.settle(studio.db, str(ready.id), {recheck.scene_id: recheck.digest}))
    assert ready.drawing["rechecked"] == {"s2": recheck.digest}
    started = _start(studio)
    assert started.covered_from == 1 and started.recheck == []


def test_a_lexicon_change_of_a_scene_playing_its_own_take_renders_again(studio):
    _first_render(studio)
    # OpenCTI is written by scene s1 only, which plays its own take: nothing to re-check, the revision is drawn again.
    _edit(studio, _film({"text": "OpenCTI", "say": "Open C-T-I"}))
    started = _start(studio)
    assert started.covered_from is None and started.recheck == [] and studio.enqueued == [started.render]


def test_a_render_made_before_6759_is_offered_from_its_voice_summary():
    marks = render_checks.SceneDigests({}, {}, {"s2": "d"}, "h", {"s2": "said"}, "b", {"s2": "p"})
    drawing = {"scenes": {"s2": "d"}, "heard": "h"}
    checks = {"voice": [{"scene_id": "s2", "status": voice_check.PASSED, "speed": 0.85, "fallback": False}]}
    (recheck,) = voice_recheck.offered(drawing, checks, marks, {"s2": 1.0})
    assert (recheck.scene_id, recheck.why, recheck.take.take()) == ("s2", voice_recheck.PIPELINE_MOVED, "a 0.85 take")
    # A summary counts its takes (``SpokenSegment.summary``): never iterated as the takes themselves.
    counted = {"voice": [{"scene_id": "s2", "status": voice_check.PASSED, "speed": 0.85, "attempts": 5}]}
    (recheck,) = voice_recheck.offered(drawing, counted, marks, {"s2": 1.0})
    assert recheck.take.take() == "a 0.85 take" and recheck.take.terms == () and recheck.take.words == ""
    assert recheck.take.why() == "the video's voice failed its transcript check at its speed 1"
    assert recheck.take.fix() == "a reworded narration"
    # The voice cache still has the scene's check record: what its takes heard wrong comes from it.
    record = {
        "status": voice_check.PASSED,
        "speed": 0.85,
        "attempts": [{"kind": "same", "terms": [{"term": "OpenAEV", "passed": False, "heard": "OpenA"}]}],
    }
    (recheck,) = voice_recheck.offered(drawing, counted, marks, {"s2": 1.0}, {"s2": record})
    assert recheck.take.terms == (("OpenAEV", "OpenA"),)
    # A record that no longer places a fallback take (checked again at full speed since) leaves the summary's take.
    (recheck,) = voice_recheck.offered(drawing, counted, marks, {"s2": 1.0}, {"s2": {"status": voice_check.PASSED}})
    assert recheck.take.take() == "a 0.85 take" and recheck.take.terms == ()
    # Its own take everywhere: covered as before.
    plain = {"voice": [{"scene_id": "s2", "status": voice_check.PASSED, "fallback": False}]}
    assert voice_recheck.offered(drawing, plain, marks, {"s2": 1.0}) == []
    # Anything else changed - and nothing recorded to tell the voice apart -: not covered, as before.
    assert voice_recheck.offered({**drawing, "heard": "other"}, checks, marks, {"s2": 1.0}) is None


def _marks(studio) -> render_checks.SceneDigests:
    spec = parse_video_spec(studio.effective)
    return asyncio.run(render_checks.digest_scenes(studio.db, studio.effective, _lexicon(spec.voiceover)))


def _ready_without_fallbacks(studio, marks: render_checks.SceneDigests) -> VideoRender:
    """A ready render of the fixture made earlier: no ``fallbacks`` recorded, its voice summary counting its
    takes (``SpokenSegment.summary``: ``attempts`` an int)."""
    row = VideoRender(
        id=uuid.uuid4(),
        project_id=studio.project.id,
        user_id=studio.user.id,
        status=JOB_READY,
        quality="high",
        revision=1,
        finished_at=datetime.now(UTC),
        checks={
            "status": "passed",
            "findings": [],
            # Judged under the running quality report: an older one would be drawn again (``stale_release``).
            "quality_report_version": quality.QUALITY_REPORT_VERSION,
            "voice": [
                {"scene_id": "s1", "status": "passed", "attempts": 1},
                {"scene_id": "s2", "status": "passed", "speed": 0.85, "attempts": 5},
            ],
        },
        drawing={"scenes": marks.drawn, "heard": marks.heard, "release": render_checks.release_drawing()},
    )
    studio.rows[row.id] = row
    return row


def test_a_render_made_before_6759_whose_summary_counts_its_takes_is_offered_not_crashed(studio, monkeypatch):
    ready = _ready_without_fallbacks(studio, _marks(studio))
    assert "fallbacks" not in ready.drawing

    async def target(*_args, **_kwargs):
        return studio.target

    monkeypatch.setattr("app.services.video_studio.reuse_index.voice_target", target)
    started = _start(studio)
    assert started.render is ready and started.covered_from == 1 and studio.enqueued == []
    assert [(r.scene_id, r.why, r.take.take()) for r in started.recheck] == [
        ("s2", voice_recheck.PIPELINE_MOVED, "a 0.85 take")
    ]
    assert started.unrechecked is None
    assert "scene `s2` plays a 0.85 take, the voice check changed since: re-check it?" in voice_recheck.offer_line(
        str(ready.id), 1, started.recheck
    )
    # The voice cache has that scene's record once it was checked: the terms heard wrong come from it.
    spec = parse_video_spec(studio.effective)
    line = next(line for line in voice.lines_of(spec) if line.scene_id == "s2")
    asyncio.run(voice_check.speak_checked(_speaker(studio, studio.effective), line, workdir=str(studio.tmp)))
    started = _start(studio)
    (recheck,) = started.recheck
    assert recheck.take.terms == (("OpenAEV", "OpenA"),)


def test_an_offer_that_cannot_be_computed_answers_the_render_as_covering_and_says_why(studio, monkeypatch):
    ready = _ready_without_fallbacks(studio, _marks(studio))

    def broken(*_args, **_kwargs):
        raise TypeError("'int' object is not iterable")

    monkeypatch.setattr(voice_recheck, "offered", broken)
    started = _start(studio)
    assert started.render is ready and started.covered_from == 1 and started.recheck == [] and studio.enqueued == []
    assert started.unrechecked.startswith("its scenes playing a fallback take could not be re-checked (")
    assert "'int' object is not iterable" in started.unrechecked


# ── The save's quality check ──────────────────────────────────────────────


def test_the_save_lists_a_fallback_take_as_a_fact_with_its_fix(studio):
    _first_render(studio)
    spec = parse_video_spec(studio.effective)
    takes = asyncio.run(voice_check.fallback_takes(studio.target, spec, _lexicon(spec.voiceover)))
    assert [(take.scene_id, take.kind, take.speed, take.terms) for take in takes] == [
        ("s2", voice_check.SLOWER_TAKE, 0.85, (("OpenAEV", "OpenA"),))
    ]
    assert quality.fallback_note(takes) == (
        "Narrations playing a fallback take (facts, nothing refused):\n- scene `s2` plays a 0.85 take: the video's "
        "voice heard OpenAEV as 'OpenA' at its speed 1 - a lexicon entry for OpenAEV the voice says right at full "
        "speed (voiceover.pronunciations: an ipa, or a say form spelled as it sounds), or a reworded clause around it "
        "would let it pass at its speed."
    )
    assert quality.fallback_note([]) == ""


def test_a_fallback_take_without_a_term_heard_wrong_says_the_clause_to_reword():
    record = {
        "status": voice_check.PASSED,
        "speed": 0.85,
        "attempts": [
            {"kind": "same", "terms": [], "wer": 0.2, "repeated": {"words": "the team", "times": 2}},
            {"kind": "slower", "speed": 0.85, "terms": [], "wer": 0.0},
        ],
    }
    take = voice_check.FallbackTake.of_record("s1", record, 1.0)
    assert take.fix() == "a reworded clause ('the team' 2 times in a row)"
    assert take.why() == "the video's voice said 'the team' 2 times in a row at its speed 1"
    assert voice_check.FallbackTake.of("s1", take.as_dict()) == dataclasses.replace(take, pipeline=None)


# ── render_video ──────────────────────────────────────────────────────────


def _tool(monkeypatch, starts: list, *, automation: bool = False, rechecked: list | None = None):
    from app.services.builtin_tools import video_studio as tool

    project = SimpleNamespace(id=uuid.uuid4(), title="Drills", revision=2)
    asked: list = []
    settled: list = []

    async def visible(*_args, **_kwargs):
        return project

    async def manages(*_args, **_kwargs):
        return True

    async def start(*_args, **kwargs):
        asked.append(kwargs)
        return starts.pop(0)

    async def waited(_kind, ids, **_kwargs):
        return [
            VideoRender(
                id=uuid.UUID(ids[0]),
                project_id=project.id,
                revision=2,
                quality="high",
                status=JOB_READY,
                finished_at=datetime.now(UTC),
                checks={"status": "passed", "findings": []},
                drawing={},
            )
        ]

    async def effective(*_args, **_kwargs):
        return SimpleNamespace(spec=_film(FIXED))

    async def hearing(*_args, **_kwargs):
        return voice_recheck.Hearing(None, _film(FIXED))

    async def recheck(_hearing, scenes):
        return [outcome for outcome in rechecked or [] if outcome.scene_id in scenes]

    async def settle(_db, source, digests):
        settled.append((source, dict(digests)))

    async def nothing(*_args, **_kwargs):
        return None

    class _Session:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_exc):
            return False

        async def get(self, *_args, **_kwargs):
            return SimpleNamespace(id=uuid.uuid4(), email="maker@example.com", role="user")

        async def commit(self):
            return None

    monkeypatch.setattr(projects, "load_visible_project", visible)
    monkeypatch.setattr(projects, "can_manage_project", manages)
    monkeypatch.setattr(projects, "start_render", start)
    monkeypatch.setattr(projects, "effective_spec", effective)
    monkeypatch.setattr(voice_recheck, "hearing_for", hearing)
    monkeypatch.setattr(voice_recheck, "recheck", recheck)
    monkeypatch.setattr(voice_recheck, "settle", settle)
    monkeypatch.setattr(jobs, "wake", nothing)
    monkeypatch.setattr(jobs, "wait_for", waited)
    monkeypatch.setattr(tool, "async_session", lambda: _Session())
    monkeypatch.setattr(tool, "_wait_budget", lambda _default: 0.0)
    _defs, executor = tool.build_video_studio_tools(user_id=str(uuid.uuid4()), automation=automation)
    return SimpleNamespace(executor=executor, project=project, asked=asked, settled=settled, tool=tool)


def _offer(render_id: uuid.UUID) -> projects.StartedRender:
    take = voice_check.FallbackTake("s2", voice_check.SLOWER_TAKE, 1.0, 0.85, terms=(("OpenAEV", "OpenA"),))
    row = SimpleNamespace(id=render_id, revision=1)
    return projects.StartedRender(row, [], covered_from=1, recheck=[voice_recheck.Recheck("s2", take, voice_recheck.PIPELINE_MOVED, "d2")])


def _call(world, **arguments) -> str:
    from app.tools.base import ToolCall

    call = ToolCall(id="r", name="render_video", arguments={"video_id": str(world.project.id), **arguments})
    return asyncio.run(world.executor.execute_tool(call)).content


def test_render_video_says_why_a_render_covers_when_its_recheck_could_not_be_computed(monkeypatch):
    ready = uuid.uuid4()
    why = "its scenes playing a fallback take could not be re-checked (storage down)"
    covered = projects.StartedRender(SimpleNamespace(id=ready, revision=2), [], covered_from=2, unrechecked=why)
    world = _tool(monkeypatch, [covered])
    said = _call(world)
    assert f"Render `{ready}` already covers revision 2, nothing to draw" in said
    assert f"The re-check could not be computed: {why}; render_video(force: true)" in said
    assert "re-check it?" not in said and len(world.asked) == 1


def test_render_video_offers_the_recheck_and_queues_nothing(monkeypatch):
    ready = uuid.uuid4()
    world = _tool(monkeypatch, [_offer(ready)])
    said = _call(world)
    assert f"Render `{ready}` covers the picture of revision 2; scene `s2` plays a 0.85 take, the voice check changed since: re-check it?" in said
    assert "render_video(recheck: true)" in said and "already covers" not in said
    assert len(world.asked) == 1 and world.settled == []
    definition = next(d for d in world.tool.build_video_studio_tools(user_id="")[0] if d.name == "render_video")
    assert definition.input_schema["properties"]["recheck"]["type"] == "boolean"
    assert "offers a re-check of those scenes" in definition.description


def test_render_video_rechecks_when_asked_and_splices_what_now_passes(monkeypatch):
    ready, splice = uuid.uuid4(), uuid.uuid4()
    cleared = voice_recheck.Rechecked("s2", True, 1.0)
    world = _tool(
        monkeypatch,
        [_offer(ready), projects.StartedRender(SimpleNamespace(id=splice, revision=2), [])],
        rechecked=[cleared],
    )
    said = _call(world, recheck=True)
    assert "Re-checked (each spoken again past the voice cache with the video's voice, speed and lexicon" in said
    assert (
        f"scene `s2` spoken again at 1: it passes - the fallback take is replaced. Render `{splice}` splices the new "
        f"take into render `{ready}`"
    ) in said
    assert world.asked[1]["splice"] == voice_recheck.Splice(str(ready), ("s2",)) and world.settled == [(str(ready), {})]


def test_render_video_in_an_automation_rechecks_at_once_and_settles_what_it_could_not_clear(monkeypatch):
    ready = uuid.uuid4()
    still = voice_check.FallbackTake("s2", voice_check.SLOWER_TAKE, 1.0, 0.85, terms=(("OpenAEV", "OpenA"),))
    world = _tool(monkeypatch, [_offer(ready)], automation=True, rechecked=[voice_recheck.Rechecked("s2", False, 1.0, still)])
    said = _call(world)
    assert "Re-checked (each spoken again past the voice cache" in said
    assert "scene `s2` spoken again at 1: it fails at that speed - the fallback take is kept (a 0.85 take: " in said
    assert f"Render `{ready}` stays the video's render and nothing was queued" in said and "re-check it?" not in said
    assert len(world.asked) == 1 and world.settled == [(str(ready), {"s2": "d2"})]


# ── recheck: true measures again, never replays a record (dev:render-recheck-respeaks-fallback) ──


def test_a_render_replaying_a_fallback_take_says_its_figure_is_an_earlier_check_s(studio):
    """Nothing moved since the first render: the second places the 0.85 take from its record, unheard - its note
    says the figure is that check's, never one this render measured."""
    first = _first_render(studio)
    assert not any("not heard again by this render" in note for note in first.notes or [])
    again = _first_render(studio, "again")
    (note,) = [note for note in again.notes or [] if note.startswith("Fallback take:")]
    assert "the video's voice heard OpenAEV as 'OpenA' at its speed 1" in note
    assert "(measured by the check of " in note and "not heard again by this render" in note
    assert again.drawing["fallbacks"]["s2"]["replayed_from"]


def test_a_recheck_speaks_a_fallback_narration_again_past_its_record_and_clears_the_note(studio):
    """The voice now says the narration right at the video's speed (what preview_voiceover hears), yet the record
    still places the 0.85 take under the same pipeline and lexicon: a render alone would replay it for good. The
    re-check speaks it again past the cache, hears 100%, and the next render plays it at 1 with no fallback note."""
    _first_render(studio)
    studio.model.slips = {}
    hearing = voice_recheck.Hearing(_speaker(studio, studio.effective), studio.effective)
    assert asyncio.run(voice_recheck.fallback_scenes(hearing)) == ["s2"]
    asked = len(studio.model.requests)
    (outcome,) = asyncio.run(voice_recheck.recheck(hearing, ["s2"]))
    # Spoken again at the video's speed, past the cached 1.0 take the record knows bad.
    respoken = studio.model.requests[asked:]
    assert len(respoken) == 1 and respoken[0].speed == 1.0
    assert outcome.cleared and outcome.share == 1.0
    assert outcome.said() == (
        "scene `s2` spoken again at 1 on tts-1-hd, voice brand, heard 100% of its words as written: it passes - the "
        "fallback take is replaced"
    )
    assert asyncio.run(voice_recheck.fallback_scenes(hearing)) == []
    after = _first_render(studio, "after")
    assert after.drawing["fallbacks"] == {}
    assert not any("Fallback take" in note for note in after.notes or [])
    voices = {summary["scene_id"]: summary for summary in after.checks["voice"]}
    assert "speed" not in voices["s2"]


def test_a_fresh_measurement_places_no_earlier_take_and_hears_every_take_anew(studio):
    """``speak_checked(fresh=True)``: never the record's take, never a take that passed before - each take spoken."""
    _first_render(studio)
    spec = parse_video_spec(studio.effective)
    line = next(line for line in voice.lines_of(spec) if line.scene_id == "s1")
    replay = asyncio.run(voice_check.speak_checked(_speaker(studio, studio.effective), line, workdir=str(studio.tmp)))
    assert replay.replayed_from and [a.kind for a in replay.attempts] == ["same"]
    asked = len(studio.model.requests)
    fresh = asyncio.run(
        voice_check.speak_checked(_speaker(studio, studio.effective), line, workdir=str(studio.tmp), fresh=True)
    )
    assert fresh.replayed_from is None and len(studio.model.requests) == asked + 1
    assert fresh.placed.check is not None and fresh.placed.check.words


def _measuring(world, monkeypatch, scenes: list[str]) -> None:
    """A video whose voice can speak and hear (``hearing_for``), the voice cache placing *scenes* with a fallback."""

    async def hearing(*_args, **_kwargs):
        return voice_recheck.Hearing(SimpleNamespace(target=SimpleNamespace(model="tts-1-hd")), _film(FIXED))

    async def fallbacks(_hearing):
        return list(scenes)

    monkeypatch.setattr(voice_recheck, "hearing_for", hearing)
    monkeypatch.setattr(voice_recheck, "fallback_scenes", fallbacks)


def test_render_video_recheck_measures_the_fallback_takes_first_and_names_each_result(monkeypatch):
    queued = uuid.uuid4()
    passed = voice_recheck.Rechecked("s2", True, 1.0, share=1.0, spoken_as="tts-1-hd, voice brand")
    world = _tool(monkeypatch, [projects.StartedRender(SimpleNamespace(id=queued, revision=2), [])], rechecked=[passed])
    _measuring(world, monkeypatch, ["s2"])
    said = _call(world, recheck=True)
    assert "Re-checked before rendering (each spoken again past the voice cache with the video's voice" in said
    assert (
        "scene `s2` spoken again at 1 on tts-1-hd, voice brand, heard 100% of its words as written: it passes - the "
        f"fallback take is replaced. Render `{queued}` places each narration as heard now"
    ) in said
    assert "No re-check performed" not in said and len(world.asked) == 1


def test_render_video_recheck_with_no_fallback_take_says_no_recheck_was_performed(monkeypatch):
    queued = uuid.uuid4()
    world = _tool(monkeypatch, [projects.StartedRender(SimpleNamespace(id=queued, revision=2), [])])
    _measuring(world, monkeypatch, [])
    said = _call(world, recheck=True)
    assert "No re-check performed: no narration of this video plays a fallback take" in said
    # Nothing spoken, nothing asked to re-check: the plain render is answered as before.
    assert "Re-checked" not in said and len(world.asked) == 1


def test_render_video_recheck_with_no_voice_says_why_no_recheck_was_performed(monkeypatch):
    queued = uuid.uuid4()
    world = _tool(monkeypatch, [projects.StartedRender(SimpleNamespace(id=queued, revision=2), [])])
    said = _call(world, recheck=True)
    assert "No re-check performed: nothing can speak or hear the narrations" in said


def test_render_video_recheck_splices_a_take_that_now_passes_into_the_render_covering_the_revision(monkeypatch):
    """No offer (nothing moved), the ready render covers the revision: a fallback take it plays that now passes at
    the video's speed is spliced in all the same - the measurement, not a digest, decides."""
    ready, splice = uuid.uuid4(), uuid.uuid4()
    covering = projects.StartedRender(
        SimpleNamespace(id=ready, revision=2, drawing={"fallbacks": {"s2": {"kind": voice_check.SLOWER_TAKE}}}),
        [],
        covered_from=2,
    )
    passed = voice_recheck.Rechecked("s2", True, 1.0, share=1.0, spoken_as="tts-1-hd, voice brand")
    world = _tool(
        monkeypatch,
        [covering, projects.StartedRender(SimpleNamespace(id=splice, revision=2), [])],
        rechecked=[passed],
    )
    _measuring(world, monkeypatch, ["s2"])
    said = _call(world, recheck=True)
    assert world.asked[1]["splice"] == voice_recheck.Splice(str(ready), ("s2",))
    assert "heard 100% of its words as written: it passes - the fallback take is replaced" in said
    assert f"Render `{splice}` splices the new take into render `{ready}`" in said
    assert "already covers" not in said
