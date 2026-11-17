"""The checks a render keeps and the gate a render passes. Synthetic videos and rows only.

* a scene's digest changes with the scene, the video's look, the captions a
  lexicon entry changes and the state of its media (a shot generated again in
  place), not with another scene or the voice-over (every render hears
  the narrations again); a preview recorded before keeps its digest; a
  preview and a render record what they drew on the video;
* a preview's neighbour whose narration failed is a warning on the neighbour;
* the gate refuses for a narration that failed its check, for a failing
  finding the latest render of the same revision left open, for a frozen
  picture only at the same quality (a draft never stops the final render),
  for a scene changed since it was last drawn - and says which;
* ``start_render`` raises the refusal, or, forced past a refusal, records the
  override on the render, logs it and audits it (forced with nothing to
  refuse, nothing is recorded); ``render_video`` words the refusal and passes
  ``force``; the route answers 409 with the reasons;
* ``run_render``: a whole render whose audio check failed is failed with its
  findings and its MP4 kept, never made current; forced, it is ready; a
  narration that failed every attempt stops it before its frames, and so does
  one only another voice said;
* the editor labels every finding kind a render stores.
"""

from __future__ import annotations

import asyncio
import logging
import re
import struct
import uuid
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest

from app.models.user import User
from app.models.video_studio import JOB_FAILED, JOB_PENDING, JOB_READY, VideoProject, VideoRender
from app.services.video_studio import (
    assets,
    compositor,
    encoder,
    jobs,
    lexicon_store,
    projects,
    quality,
    render,
    render_checks,
    render_retention,
    stitch,
    voice,
    voice_check,
)
from app.services.video_studio.render_checks import FAIL, FROZEN, STUTTER, WARN, Checks, Finding
from app.services.video_studio.spec import validate_video_spec

CLIP = "00000000-0000-0000-0000-00000000c1c1"


def _effective(
    narration: str = "Every alert is triaged first.", voice_name: str = "marin", opening: str = "Hello", **top
) -> dict:
    return validate_video_spec(
        {
            "title": "Gate",
            "freestyle": True,
            "voiceover": {"voice": voice_name},
            "scenes": [
                {"id": "open", "type": "title", "title": opening, "duration": 3, "narration": narration},
                {"id": "claim", "type": "statement", "lines": ["One platform"], "duration": 4},
                {"id": "shot", "type": "clip", "clip_id": CLIP},
            ],
            **top,
        }
    )


def _shot_row(file_id: str) -> SimpleNamespace:
    """A ready AI shot as the digest and the planner read it (its length unknown: the asked one is planned)."""
    return SimpleNamespace(
        status=JOB_READY,
        file_id=file_id,
        finished_at=None,
        name=None,
        expired_at=None,
        params={},
        duration_ms=None,
        width=None,
        height=None,
        has_audio=False,
    )


class _Media:
    """Rows a digest reads: the shot as it is now."""

    def __init__(self, clip=None):
        self.clip = clip

    async def get(self, model, _key, **_options):
        return self.clip if model.__name__ == "VideoClip" else None


def _digests(effective: dict, clip=None) -> dict[str, str]:
    return asyncio.run(render_checks.scene_digests(_Media(clip), effective))


def test_a_scenes_digest_follows_what_it_shows_and_says():
    base = _digests(_effective())
    assert base == _digests(_effective()) and set(base) == {"open", "claim", "shot"}
    assert base["open"] != _digests(_effective(narration="Something else."))["open"]
    # The voice-over is heard again by every render's narration check, never by drawing the scene again.
    assert base == _digests(_effective(voice_name="cedar"))
    assert base == _digests(_effective(voiceover={"voice": "marin", "pronunciations": [{"text": "alert", "say": "a-lert"}]}))
    # The video's look decides every scene.
    assert base["claim"] != _digests(_effective(captions={"enabled": False}))["claim"]
    # The shot generated again in place: same id, another take - another digest, for it and its neighbour.
    made = _shot_row("f1")
    again = _shot_row("f2")
    assert _digests(_effective(), made)["shot"] != _digests(_effective(), again)["shot"]
    assert _digests(_effective(), made)["claim"] != _digests(_effective(), again)["claim"]
    assert _digests(_effective(), made)["open"] == _digests(_effective(), again)["open"]


def test_a_lexicon_entry_shown_in_the_captions_changes_the_scenes_whose_captions_it_changes():
    from app.services.video_studio import lexicon as lex

    def captioned(say: str, text: str = "alert") -> dict:
        return {"voice": "marin", "pronunciations": [{"text": text, "say": say, "apply_to": "captions"}]}

    base = _digests(_effective())
    shown = _digests(_effective(voiceover=captioned("ALERT")))
    # Its captions are its narration's: drawn by its own preview, never by a neighbour's.
    assert shown["open"] != base["open"] and shown["claim"] == base["claim"] and shown["shot"] == base["shot"]
    assert _digests(_effective(voiceover=captioned("WARNING")))["open"] != shown["open"]
    # A term the narration does not write changes nothing; with the captions off, nothing shows it.
    assert _digests(_effective(voiceover=captioned("FLEET", "fleet"))) == base
    assert _digests(_effective(voiceover=captioned("ALERT"), captions={"enabled": False}))["open"] == _digests(
        _effective(captions={"enabled": False})
    )["open"]
    # The platform's entries count as the video's: the lexicon the render shows is the one given.
    platform = lex.merge([{"text": "alert", "say": "ALERT", "apply_to": "captions"}], ())
    assert asyncio.run(render_checks.scene_digests(_Media(), _effective(), platform))["open"] == shown["open"]


def test_a_preview_recorded_under_an_earlier_digest_is_as_if_never_drawn(speaking):
    """A scene is hashed with its neighbours: what an earlier rule recorded holds no render and clears nothing."""
    earlier = {"open": {"digest": "0" * 40, "render_id": "r0", "at": "2026-10-20T08:00:00+00:00"}}
    assert render_checks.changed_scenes(earlier, _digests(_effective())) == []
    assert render_checks.passed_scenes(earlier, _digests(_effective())) == set()
    assert _gate(_Rows(), project=_project(earlier)) == []


def test_a_preview_reports_a_neighbours_failed_narration_as_a_warning_on_the_neighbour():
    def spoken(scene: str, duration: float, status: str) -> voice_check.SpokenSegment:
        attempts = [voice_check.Attempt("marin", "same", f"/{scene}.wav")]
        reason = f"brand term mispronounced: CTEM heard as 'stem' in {scene}" if status == voice_check.FAILED else None
        return voice_check.SpokenSegment(scene, f"/{scene}.wav", duration, status, attempts, reason=reason)

    media = render._Media()
    media.spoken = [spoken("open", 2.5, voice_check.FAILED), spoken("claim", 2.0, voice_check.PASSED)]
    plan = SimpleNamespace(narration_starts={"open": 0.5, "claim": 3.0})
    # The preview of `claim` plays the end of `open` in its transition in: open's, a warning, never claim's.
    (neighbour,) = render._voice_findings(media, plan, offset=2.5, span=(2.5, 4.0), scene_id="claim")
    assert (neighbour.scene_id, neighbour.severity, neighbour.at) == ("open", WARN, 0.0)
    assert "heard in the transition of scene claim, which it does not fail" in neighbour.message
    assert "render_video_scene(scene='open')" in neighbour.message
    checks = Checks(voice=[s.summary() for s in media.spoken], findings=[neighbour])
    assert not checks.failed and checks.status == "findings"
    assert "- [warn] 0:00.00-0:00.50 scene `open` (voice_segment)" in render_checks.format_checks(checks.as_dict())
    # Its own narration failing fails it; a whole render fails on every narration that failed.
    media.spoken[1] = spoken("claim", 2.0, voice_check.FAILED)
    previewed = render._voice_findings(media, plan, offset=2.5, span=(2.5, 4.0), scene_id="claim")
    assert [(f.scene_id, f.severity) for f in previewed] == [("open", WARN), ("claim", FAIL)]
    assert [(f.scene_id, f.severity) for f in render._voice_findings(media, plan)] == [("open", FAIL), ("claim", FAIL)]


def _fallback(scene: str = "open") -> voice_check.SpokenSegment:
    """A narration the video's voice failed on every take, then the closest voice of its model said right."""
    heard = voice_check.TranscriptCheck(0.18, "moscow knee center the cars walk in like they own a place")
    attempts = [
        voice_check.Attempt("fable", "same", f"/{scene}-0.wav", 2.4, heard),
        voice_check.Attempt("fable", "again", f"/{scene}-1.wav", 2.4, heard),
        voice_check.Attempt("fable", "slower", f"/{scene}-2.wav", 2.8, heard, speed=0.85),
        voice_check.Attempt("Ash", "closest", f"/{scene}-3.wav", 2.5, voice_check.TranscriptCheck(0.0, "ok")),
    ]
    change = voice_check.VoiceChange(
        "Fable", "Ash", "18% of its words differ from the narration", "the voice of the same speech model sharing the most with Fable (british, male)"
    )
    return voice_check.SpokenSegment(
        scene, f"/{scene}-3.wav", 2.5, voice_check.PASSED, attempts, True, reason="18% off", voice_change=change
    )


def test_a_narration_another_voice_said_is_a_failure_naming_the_voice_and_why():
    """Another voice mid-film is heard by the audience - a failure like a narration that failed,
    naming the voice, why and what to change."""
    media = render._Media()
    passed = voice_check.SpokenSegment("claim", "/claim.wav", 2.0, voice_check.PASSED, [voice_check.Attempt("fable", "same", "/claim.wav")])
    media.spoken = [_fallback(), passed]
    plan = SimpleNamespace(narration_starts={"open": 0.5, "claim": 3.0})
    (finding,) = render._voice_findings(media, plan)
    assert (finding.kind, finding.severity, finding.scene_id, finding.at) == (render_checks.VOICE_FALLBACK, FAIL, "open", 0.5)
    assert finding.message.startswith(
        "its narration is spoken by Ash, not the video's voice Fable: Fable failed its transcript check (18% of its words "
        "differ from the narration); Ash is the voice of the same speech model sharing the most with Fable (british, male)"
    )
    assert "render_video_scene(scene='open')" in finding.message
    assert finding.data["voice_change"]["voice"] == "Ash"
    assert [a["kind"] for a in finding.data["attempts"]] == ["same", "again", "slower", "closest"]
    checks = Checks(voice=[s.summary() for s in media.spoken], findings=[finding])
    assert checks.failed and checks.status == render_checks.FAILED
    text = render_checks.format_checks(checks.as_dict())
    assert (
        "Narrations heard back: 1 of 2 passed in the video's voice, 1 spoken by another voice after the video's failed "
        "(each listed below)."
    ) in text
    assert "- [FAIL] 0:00.50-0:03.00 scene `open` (voice_fallback): its narration is spoken by Ash" in text
    # The fix it names: reword, a pronunciation entry, or that voice made the video's.
    assert "reword the narration or add a pronunciation entry" in finding.message and "make Ash the video's voice" in finding.message
    # In the preview of its neighbour, heard in the transition: the neighbour's warning, never the preview's failure.
    (neighbour,) = render._voice_findings(media, plan, offset=2.5, span=(2.5, 4.0), scene_id="claim")
    assert (neighbour.kind, neighbour.severity) == (render_checks.VOICE_FALLBACK, WARN)


def test_what_a_render_drew_is_recorded_on_the_video():
    seen = render_checks.record_seen(None, {"open": "d1", "claim": ""}, "r1")
    assert set(seen) == {"open"} and seen["open"]["digest"] == "d1" and seen["open"]["render_id"] == "r1"
    later = render_checks.record_seen(seen, {"claim": "d2"}, "r2")
    assert later["open"]["render_id"] == "r1" and later["claim"]["render_id"] == "r2"


def test_the_checks_say_every_finding_with_its_time():
    checks = Checks(
        voice=[{"scene_id": "open", "status": "passed", "fallback": True}],
        audio={"status": "failed", "wer": 0.18, "model": "whisper-1", "provider": "OpenAI (Production)"},
        frames={"status": "findings"},
        findings=[
            Finding(FROZEN, 64.5, 66.25, "shot", "the picture is frozen", WARN),
            Finding(STUTTER, 3.25, 3.61, "open", "the same 120 ms of sound is heard 3 times in a row", FAIL),
        ],
    )
    stored = checks.as_dict()
    assert stored["status"] == "failed" and [f["kind"] for f in stored["findings"]] == ["stutter", "frozen"]
    text = render_checks.format_checks(stored)
    assert "Checks: failed." in text and "18% of the narration's words heard wrong" in text
    # The narrations' audio went to that provider, whatever spoke them.
    assert "(speech-to-text: whisper-1 on OpenAI (Production))" in text
    assert "- [FAIL] 0:03.25-0:03.61 scene `open` (stutter)" in text
    assert "- [warn] 1:04.50-1:06.25 scene `shot` (frozen)" in text
    # An older render kept `fallback` for the platform's default voice: still said as another voice.
    assert "1 spoken by another voice after the video's failed" in text
    assert render_checks.format_checks(None) == ""
    assert Checks(findings=[Finding(FROZEN, 1, 2, "s", "m", WARN)]).status == "findings"


# ── The gate ──────────────────────────────────────────────────────────────


#: The shot the gate's videos play: ready, so only what a test sets up refuses.
READY_SHOT = _shot_row("f0")


class _Rows(_Media):
    """The renders of the revision, newest first, and the shot as it is now (ready unless given)."""

    def __init__(self, *renders, clip=None):
        super().__init__(clip if clip is not None else READY_SHOT)
        self.renders = list(renders)

    async def execute(self, statement, _params=None):
        assert "video_renders.checks IS NOT NULL" in str(statement)
        return SimpleNamespace(scalars=lambda: SimpleNamespace(all=lambda: self.renders))


@pytest.fixture
def speaking(monkeypatch):
    failed: list = []

    async def resolve(_db, _user_id, _voiceover):
        return SimpleNamespace(name="video")

    async def failures(_target, _spec, _lexicon=None):
        return failed

    async def clean(*_args, **_kwargs):
        return quality.QualityReport(scene_count=3)

    async def own_lexicon(_db, voiceover):
        return voice.lexicon_of(voiceover)

    monkeypatch.setattr(lexicon_store, "for_voiceover", own_lexicon)
    monkeypatch.setattr(voice, "resolve", resolve)
    monkeypatch.setattr(voice_check, "failed_narrations", failures)
    monkeypatch.setattr(quality, "check_video_quality", clean)
    return failed


def _project(scene_previews=None) -> SimpleNamespace:
    return SimpleNamespace(id=uuid.uuid4(), revision=4, title="Gate", scene_previews=scene_previews)


PROJECT = _project()
USER = SimpleNamespace(id=uuid.uuid4())
FROZEN_WARNING = {"kind": "frozen", "at": 12.5, "severity": "warn"}
STUTTER_FAILURE = {"kind": "stutter", "at": 3.0, "severity": "fail"}


def _gate(db, effective=None, *, project=PROJECT, quality="high"):
    return asyncio.run(render_checks.gate(db, project, USER, effective or _effective(), quality=quality))


def test_an_open_gate_has_no_reason(speaking):
    assert _gate(_Rows()) == []


def test_a_narration_that_failed_its_check_refuses_the_render(speaking):
    speaking.append(("open", {"status": "failed", "reason": '"the alert" is said 2 times in a row'}))
    reasons = _gate(_Rows())
    assert [(r.code, r.scene_id) for r in reasons] == [("narration_failed", "open")]
    assert "said 2 times in a row" in reasons[0].message


def test_a_failing_finding_of_the_revision_refuses_any_render_of_it(speaking):
    failed = VideoRender(id=uuid.uuid4(), quality="draft", checks={"findings": [STUTTER_FAILURE, FROZEN_WARNING]})
    reasons = _gate(_Rows(failed), quality="high")
    assert [r.code for r in reasons] == ["open_findings"]
    assert "(draft) of this same revision left 1 finding(s) open (stutter at 0:03.00)" in reasons[0].message
    # The editor builds the reason in the reader's language from its params: no render id, kinds as keys.
    params = reasons[0].as_dict()["params"]
    assert params == {"count": 1, "quality": "draft", "created_at": None, "findings": [{"kind": "stutter", "at": 3.0}]}
    assert str(failed.id) not in repr(params)
    clean = VideoRender(id=uuid.uuid4(), quality="high", checks={"findings": []})
    assert _gate(_Rows(clean)) == []


def test_a_draft_with_a_frozen_picture_never_stops_the_final_render(speaking):
    draft = VideoRender(id=uuid.uuid4(), quality="draft", checks={"findings": [FROZEN_WARNING]})
    assert _gate(_Rows(draft), quality="high") == []
    # The same video again at the same quality would freeze the same way: that one is refused.
    reasons = _gate(_Rows(draft), quality="draft")
    assert [r.code for r in reasons] == ["open_findings"] and "frozen at 0:12.50" in reasons[0].message
    high = VideoRender(id=uuid.uuid4(), quality="high", checks={"findings": [FROZEN_WARNING]})
    assert [r.code for r in _gate(_Rows(draft, high), quality="high")] == ["open_findings"]


def test_a_scene_changed_since_it_was_last_drawn_refuses_it(speaking):
    then = _digests(_effective(narration="An older narration."), READY_SHOT)
    now = _digests(_effective(), READY_SHOT)
    seen = render_checks.record_seen(None, {"open": then["open"], "shot": now["shot"]}, "r1")
    reasons = _gate(_Rows(), project=_project(seen))
    assert [(r.code, r.scene_id) for r in reasons] == [("scene_changed", "open")]
    assert "render_video_scene(scene='open')" in reasons[0].message
    # Its neighbour's preview draws its picture and the transition between them, never its narration: the
    # claim, previewed before the narration changed, is still current.
    beside = render_checks.record_seen(None, {"claim": then["claim"]}, "r1")
    assert _gate(_Rows(), project=_project(beside)) == []
    # Its picture changed: the claim's preview drew it at its edge, and changed with it.
    retitled = _digests(_effective(narration="An older narration.", opening="Hi"), READY_SHOT)
    assert retitled["claim"] != then["claim"]
    # A shot generated again in place since it was drawn is a scene changed too; a scene never drawn is not.
    made = _shot_row("f1")
    shot = render_checks.record_seen(None, {"shot": _digests(_effective(), made)["shot"]}, "r2")
    again = _shot_row("f2")
    assert [r.scene_id for r in _gate(_Rows(clip=again), project=_project(shot))] == ["shot"]
    assert _gate(_Rows(clip=made), project=_project(shot)) == []


def test_a_voice_over_edit_never_asks_to_preview_the_scenes_again(speaking):
    # Previewed, then the voice and a lexicon reading changed: the render hears the narrations before it
    # draws a frame, and a narration that failed its check still refuses it.
    drawn = _digests(_effective(), READY_SHOT)
    seen = render_checks.record_seen(None, drawn, "r1")
    edited = _effective(voiceover={"voice": "cedar", "pronunciations": [{"text": "alert", "say": "a-lert"}]})
    assert _gate(_Rows(), edited, project=_project(seen)) == []
    speaking.append(("open", {"status": "failed", "reason": "brand term mispronounced: alert heard as 'a lot'"}))
    assert [(r.code, r.scene_id) for r in _gate(_Rows(), edited, project=_project(seen))] == [("narration_failed", "open")]


# ── Starting a render ─────────────────────────────────────────────────────


class _StartDb:
    def __init__(self):
        self.added = []

    async def execute(self, _statement, _params=None):
        return SimpleNamespace(scalars=lambda: SimpleNamespace(all=lambda: []))

    def add(self, row):
        self.added.append(row)

    async def flush(self):
        return None


@pytest.fixture
def starting(monkeypatch):
    world = SimpleNamespace(reasons=[], audits=[])

    async def gate(*_args, **_kwargs):
        return list(world.reasons)

    async def effective(*_args, **_kwargs):
        return SimpleNamespace(spec=_effective())

    async def none(*_args, **_kwargs):
        return []

    async def nothing(*_args, **_kwargs):
        return None

    async def audit(_db, **kwargs):
        world.audits.append(kwargs)

    monkeypatch.setattr(render_checks, "gate", gate)
    monkeypatch.setattr(projects, "effective_spec", effective)
    monkeypatch.setattr(jobs, "renders_in_flight", none)
    monkeypatch.setattr(jobs, "enqueue", nothing)
    monkeypatch.setattr("app.services.audit.log_audit", audit)
    return world


def test_a_refused_render_never_starts(starting):
    starting.reasons = [render_checks.GateReason("scene_changed", "scene open changed since its last preview", "open")]
    db = _StartDb()
    with pytest.raises(render_checks.RenderGateError) as refused:
        asyncio.run(projects.start_render(db, PROJECT, USER, quality="high"))
    assert refused.value.reasons[0].scene_id == "open" and db.added == []


def test_a_forced_render_records_logs_and_audits_its_override(starting, caplog):
    starting.reasons = [render_checks.GateReason("open_findings", "render x left 1 finding(s) open")]
    db = _StartDb()
    with caplog.at_level(logging.WARNING):
        started = asyncio.run(projects.start_render(db, PROJECT, USER, quality="high", force=True, via_agent=True))
    override = started.render.gate_override
    assert override["by"] == str(USER.id) and override["via"] == "agent"
    assert override["reasons"] == [{"code": "open_findings", "message": "render x left 1 finding(s) open", "scene_id": None}]
    assert "forced past its quality gate" in caplog.text
    details = starting.audits[0]["details"]
    assert details["forced"] is True and details["gate_reasons"] == override["reasons"]
    assert starting.audits[0]["summary"].endswith("past its quality gate")


def test_force_with_nothing_to_refuse_records_no_override(starting, caplog):
    with caplog.at_level(logging.WARNING):
        started = asyncio.run(projects.start_render(_StartDb(), PROJECT, USER, quality="high", force=True))
    assert started.render.gate_override is None and "forced past its quality gate" not in caplog.text
    assert "forced" not in starting.audits[0]["details"]
    assert not starting.audits[0]["summary"].endswith("past its quality gate")


def test_the_tool_words_the_refusal_and_passes_force(monkeypatch):
    from app.services.builtin_tools.video_studio import build_video_studio_tools
    from app.tools.base import ToolCall

    project = SimpleNamespace(id=uuid.uuid4(), title="Gate", revision=2)
    seen = {}

    async def visible(*_args, **_kwargs):
        return project

    async def manages(*_args, **_kwargs):
        return True

    async def start(_db, _project, _user, **kwargs):
        seen.update(kwargs)
        raise render_checks.RenderGateError([render_checks.GateReason("narration_failed", "scene open's narration failed")])

    class _Session:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_exc):
            return False

        async def get(self, *_args, **_kwargs):
            return SimpleNamespace(id=uuid.UUID(int=1), email="maker@example.com", role="user")

    monkeypatch.setattr(projects, "load_visible_project", visible)
    monkeypatch.setattr(projects, "can_manage_project", manages)
    monkeypatch.setattr(projects, "start_render", start)
    monkeypatch.setattr("app.services.builtin_tools.video_studio.async_session", lambda: _Session())
    _defs, executor = build_video_studio_tools(user_id=str(uuid.UUID(int=1)))
    call = ToolCall(id="r1", name="render_video", arguments={"video_id": str(project.id), "force": True})
    result = asyncio.run(executor.execute_tool(call))
    assert seen["force"] is True
    assert result.is_error and "quality gate refused it" in result.content and "- scene open's narration failed" in result.content
    assert "render_video(force: true)" in result.content


def test_the_route_answers_409_with_the_reasons(monkeypatch):
    from fastapi import HTTPException

    from app.api.v1 import video_studio as api
    from app.api.v1.schemas.video_studio import RenderRequest

    async def manageable(*_args, **_kwargs):
        return PROJECT

    async def start(*_args, **kwargs):
        assert kwargs["force"] is False
        raise render_checks.RenderGateError([render_checks.GateReason("scene_changed", "scene open changed", "open")])

    monkeypatch.setattr(api, "_manageable_project", manageable)
    monkeypatch.setattr(projects, "start_render", start)
    with pytest.raises(HTTPException) as refused:
        asyncio.run(api.create_render(str(PROJECT.id), RenderRequest(quality="high"), user=USER, db=None, _ai_gate=None))
    assert refused.value.status_code == 409
    detail = refused.value.detail
    assert detail["code"] == "render_gate" and detail["reasons"] == [{"code": "scene_changed", "message": "scene open changed", "scene_id": "open"}]


# ── A whole render and its checks ─────────────────────────────────────────


@pytest.fixture
def whole(monkeypatch, tmp_path):
    """``run_render`` of a whole render of a two-scene synthetic video, its heavy steps recorded."""
    effective = _effective()
    maker = User(id=uuid.uuid4(), email="maker@example.com", role="user")
    current = uuid.uuid4()
    project = VideoProject(id=uuid.uuid4(), title="Gate", spec=effective, revision=3, latest_render_id=current)
    row = VideoRender(id=uuid.uuid4(), project_id=project.id, user_id=maker.id, status=JOB_PENDING, quality="high")
    world = SimpleNamespace(
        row=row, project=project, current=current, drawn=False, checks=Checks(), media=render._Media(), marks=[], effective=effective
    )

    class _Db:
        async def get(self, model, _key, **_options):
            return {VideoRender: row, VideoProject: project, User: maker}.get(model)

        async def execute(self, _statement, _params=None):
            # The renders it replaced at its quality: none.
            return SimpleNamespace(all=lambda: [])

        async def commit(self):
            return None

        async def flush(self):
            return None

        def expunge(self, _row):
            return None

    @asynccontextmanager
    async def session():
        yield _Db()

    async def effective_spec(_db, _project, _user):
        return SimpleNamespace(spec=world.effective, template=None, notes=[])

    async def gather(_spec, **_kwargs):
        return world.media

    async def render_frames(_document, _assets, *, workdir, **_kwargs):
        world.drawn = True
        out = Path(workdir) / "video.mp4"
        out.write_bytes(b"MP4")
        return str(out)

    async def write(*_args, out_path=None, **_kwargs):
        Path(out_path).write_bytes(b"X")

    async def probe(_path):
        return SimpleNamespace(duration=7.0, width=1920, height=1080, has_audio=True)

    async def frame(_path, *, at, out_path, width=None):
        Path(out_path).write_bytes(b"JPG")

    async def store_path(path, **kwargs):
        return SimpleNamespace(id=uuid.uuid4(), size=10, path=path, **kwargs)

    async def checks(**_kwargs):
        return world.checks

    async def mark(_kind, _job_id, **changes):
        world.marks.append(changes)

    async def nothing(*_args, **_kwargs):
        return None

    async def none_replaced(*_args, **_kwargs):
        return render_retention.Deletion()

    monkeypatch.setattr(render, "async_session", session)
    monkeypatch.setattr(render, "bind_llm_caller", nothing)
    monkeypatch.setattr(projects, "effective_spec", effective_spec)
    monkeypatch.setattr(render, "_gather_media", gather)
    monkeypatch.setattr(compositor, "render_frames", render_frames)
    monkeypatch.setattr(encoder, "mix_audio", write)
    monkeypatch.setattr(encoder, "mux", write)
    monkeypatch.setattr(encoder, "probe", probe)
    monkeypatch.setattr(encoder, "extract_frame", frame)
    monkeypatch.setattr(assets, "store_path", store_path)
    monkeypatch.setattr(render, "check_render", checks)
    monkeypatch.setattr(jobs, "mark", mark)
    monkeypatch.setattr(render.retention, "touch", nothing)
    monkeypatch.setattr(render_retention, "replaced_previews", none_replaced)
    monkeypatch.setattr(render, "font_face_css", lambda: "", raising=False)
    world.workdir = tmp_path
    return world


def test_a_render_that_fails_its_audio_check_is_failed_with_its_mp4_kept(whole):
    whole.checks = Checks(audio={"status": "failed"}, findings=[Finding(STUTTER, 3.25, 3.6, "open", "the same 120 ms ...", FAIL)])
    asyncio.run(render.run_render(str(whole.row.id), workdir=whole.workdir))
    row = whole.row
    assert row.status == JOB_FAILED and row.file_id is not None
    assert row.error.startswith("The render failed its audio check: 0:03.25 scene open") and "MP4 is kept" in row.error
    assert row.checks["status"] == "failed" and whole.project.latest_render_id == whole.current
    # A failed render is a draft to look at: it records no scene as drawn.
    assert whole.project.scene_previews is None


def test_a_frozen_picture_alone_leaves_the_render_ready_with_its_finding(whole):
    whole.checks = Checks(frames={"status": "findings"}, findings=[Finding(FROZEN, 5.0, 6.0, "claim", "frozen", WARN)])
    asyncio.run(render.run_render(str(whole.row.id), workdir=whole.workdir))
    assert whole.row.status == JOB_READY and whole.project.latest_render_id == whole.row.id
    assert whole.row.checks["findings"][0]["kind"] == "frozen"
    # A ready render records every scene it drew, as they were when it started.
    seen = whole.project.scene_previews
    assert set(seen) == {"open", "claim", "shot"} and {entry["render_id"] for entry in seen.values()} == {str(whole.row.id)}


def test_a_render_records_the_frames_it_kept_beside_those_it_drew(whole, monkeypatch):
    # An attempt resumed from the segments of an earlier one draws only the rest; both are recorded.
    async def resumed(_document, _assets, *, workdir, tracker, **_kwargs):
        tracker.total, tracker.done, tracker.kept = 420, 420, 300
        out = Path(workdir) / "video.mp4"
        out.write_bytes(b"MP4")
        return str(out)

    monkeypatch.setattr(compositor, "render_frames", resumed)
    asyncio.run(render.run_render(str(whole.row.id), workdir=whole.workdir))
    measured = whole.row.measured
    assert whole.row.status == JOB_READY
    assert (measured["frames"], measured["frames_reused"]) == (120, 300)
    assert (measured["figure_frames"], measured["figure_frames_reused"]) == (0, 0) and "figures" not in measured


def test_a_forced_render_keeps_its_findings_and_is_ready(whole):
    whole.row.gate_override = {"by": "someone", "reasons": []}
    whole.checks = Checks(findings=[Finding(STUTTER, 1.0, 1.3, "open", "stutter", FAIL)])
    asyncio.run(render.run_render(str(whole.row.id), workdir=whole.workdir))
    assert whole.row.status == JOB_READY and whole.row.checks["status"] == "failed"
    assert any("Forced past its quality gate" in note for note in whole.row.notes)


def test_a_narration_failing_every_attempt_stops_the_render_before_its_frames(whole):
    whole.media.spoken = [
        voice_check.SpokenSegment(
            "open", None, None, voice_check.FAILED, [voice_check.Attempt("marin", "same", None, error="refused")], reason="it could not be spoken"
        )
    ]
    asyncio.run(render.run_render(str(whole.row.id), workdir=whole.workdir))
    assert whole.drawn is False
    failed = next(m for m in whole.marks if m.get("status") == JOB_FAILED)
    assert "stopped before drawing its frames" in failed["error"]
    assert failed["checks"]["findings"][0]["kind"] == "voice_segment" and failed["checks"]["findings"][0]["scene_id"] == "open"


def test_a_narration_another_voice_said_stops_the_render_before_its_frames_unless_forced(whole):
    """Two narrator voices in one film - refused like a narration that failed; forced, that voice plays it."""
    whole.media.spoken = [_fallback()]
    asyncio.run(render.run_render(str(whole.row.id), workdir=whole.workdir))
    assert whole.drawn is False
    failed = next(m for m in whole.marks if m.get("status") == JOB_FAILED)
    assert "stopped before drawing its frames" in failed["error"] and "spoken by Ash" in failed["error"]
    (finding,) = failed["checks"]["findings"]
    assert (finding["kind"], finding["severity"], finding["scene_id"]) == ("voice_fallback", "fail", "open")
    whole.marks.clear()
    whole.row.status, whole.row.gate_override = JOB_PENDING, {"by": "someone", "reasons": []}
    asyncio.run(render.run_render(str(whole.row.id), workdir=whole.workdir))
    assert whole.drawn is True and whole.row.status == JOB_READY
    assert not any(m.get("status") == JOB_FAILED for m in whole.marks)


def test_the_stem_is_built_from_the_plans_narration_starts(tmp_path):
    plan = SimpleNamespace(narration_starts={"open": 0.35}, duration=3.0)
    assert asyncio.run(render.build_stem(plan, {}, tmp_path)) is None
    path = tmp_path / "v.wav"
    pcm = (np.full(4800, 0.1) * 32767).astype("<i2").tobytes()
    header = b"RIFF" + struct.pack("<I", 36 + len(pcm)) + b"WAVE"
    path.write_bytes(header + b"fmt " + struct.pack("<IHHIIHH", 16, 1, 1, 48000, 96000, 2, 16) + b"data" + struct.pack("<I", len(pcm)) + pcm)
    stem = asyncio.run(render.build_stem(plan, {"open": str(path), "orphan": str(path)}, tmp_path))
    assert isinstance(stem, stitch.Stem) and [p.scene_id for p in stem.placements] == ["open"]
    assert stem.placements[0].first == round(0.35 * 48000)


def test_two_overlapping_narrations_fail_the_render_never_mix(tmp_path):
    plan = SimpleNamespace(narration_starts={"a": 0.0, "b": 0.05}, duration=3.0)
    path = tmp_path / "v.wav"
    pcm = (np.full(4800, 0.1) * 32767).astype("<i2").tobytes()
    header = b"RIFF" + struct.pack("<I", 36 + len(pcm)) + b"WAVE"
    path.write_bytes(header + b"fmt " + struct.pack("<IHHIIHH", 16, 1, 1, 48000, 96000, 2, 16) + b"data" + struct.pack("<I", len(pcm)) + pcm)
    with pytest.raises(RuntimeError, match="never overlap"):
        asyncio.run(render.build_stem(plan, {"a": str(path), "b": str(path)}, tmp_path))


def test_the_editor_labels_every_finding_kind_a_render_stores():
    """The renders panel words a finding by its kind; a kind it cannot label used to take the app down."""
    source = (
        Path(__file__).resolve().parents[2] / "frontend" / "src" / "components" / "video-studio" / "video-format.ts"
    ).read_text(encoding="utf-8")
    block = re.search(r"const FINDING_KIND_LABELS = \{(.*?)\} as const;", source, re.S)
    assert block, "FINDING_KIND_LABELS is gone from video-format.ts"
    labelled = set(re.findall(r"^\s*(\w+):", block.group(1), re.M))
    stored = (
        render_checks.AUDIO_KINDS
        | render_checks.PICTURE_KINDS
        | render_checks.HELD_WARNINGS
        | {render_checks.MUSIC_LEVEL, render_checks.AUDIO_MIX}
    )
    assert labelled == stored


def test_the_editor_words_every_finding_code_a_render_stores():
    """A finding with a code is worded in the reader's language from FINDING_MESSAGES; every code a check
    writes has its message there, with the placeholders its params fill."""
    services = Path(render_checks.__file__).resolve().parent
    written: dict[str, set[str]] = {}
    for path in services.glob("*.py"):
        source = path.read_text(encoding="utf-8")
        for code, params in re.findall(r'code="(\w+)",\s*params=\{(.*?)\},?\n', source):
            written[code] = set(re.findall(r'"(\w+)": ', params))
    assert set(written) == {"frozen", "brightness_dip", "double_image", "near_empty", "static_flicker"}
    source = (
        Path(__file__).resolve().parents[2] / "frontend" / "src" / "components" / "video-studio" / "video-format.ts"
    ).read_text(encoding="utf-8")
    block = re.search(r"const FINDING_MESSAGES = \{(.*?)\} as const;", source, re.S)
    assert block, "FINDING_MESSAGES is gone from video-format.ts"
    messages = dict(re.findall(r'^\s*(\w+): "(.*)",$', block.group(1), re.M))
    assert set(messages) == set(written)
    for code, params in written.items():
        assert set(re.findall(r"\{(\w+)[,}]", messages[code])) == params, code


def test_a_finding_code_survives_the_store_and_a_reworded_message_drops_it():
    """The code and params are stored and read back; a finding reworded for a neighbour scene keeps its
    English words alone, so the editor never shows the bare finding's translation for them."""
    finding = render_checks.Finding(
        "frozen", 1.0, 2.5, "s02", "the picture is frozen for 1.5 s", render_checks.FAIL,
        code="frozen", params={"seconds": 1.5, "start": "0:01.00", "end": "0:02.50"},
    )
    stored = finding.as_dict()
    # A number, so the editor formats the decimal in the reader's locale ("1,5 s" in fr and es).
    assert stored["code"] == "frozen" and stored["params"]["seconds"] == 1.5
    back = render_checks.Checks.from_dict({"findings": [stored]}).findings[0]
    assert back.code == "frozen" and back.params == stored["params"]
    plain = render_checks.Finding("frozen", 1.0, None, None, "still")
    assert "code" not in plain.as_dict() and "params" not in plain.as_dict()
    checks = render_checks.Checks(findings=[finding])
    render_checks.settle_neighbours(checks, "s01", {})
    assert checks.findings[0].code is None and checks.findings[0].message.startswith("on scene s02")
