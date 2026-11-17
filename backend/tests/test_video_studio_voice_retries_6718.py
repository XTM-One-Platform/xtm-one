"""Video b56997a2 rev 149, render 45b976b8: two narrator voices, a summary at 100 %, three identical retries.

On the synthetic speech models of ``test_video_studio_pronunciation_voice`` (each says what it is sent, a
scripted speech-to-text hears it word by word) and the whole render of ``test_video_studio_render_checks``:

* F64: a three-scene video whose second narration only the closest voice says right - the whole render is
  refused before its frames, the ``voice_fallback`` finding blocks with the fix it writes, and the first and
  third scenes are untouched: no finding, their passes kept and never spoken again;
* F65: a term one take fails on and the retry passes - the render's pronunciation summary names the failed
  take beside the take kept;
* F66: a brand term the voice drops a syllable from as written - the take after two failures sends its
  lexicon respelling alone in its clause, and passes in the video's own voice;
* how a term is set apart (``lexicon.REFRAMED``), and which terms two takes heard wrong.
"""

from __future__ import annotations

import asyncio
import dataclasses
from types import SimpleNamespace

from app.llm.router import SpeechTarget
from app.llm.speech import PronunciationSupport, SpeechRequest
from app.models.video_studio import JOB_FAILED
from app.services.video_studio import audio_qc, frame_freeze, render, render_checks, voice, voice_check
from app.services.video_studio import lexicon as lex
from app.services.video_studio.render_checks import FAIL, PASSED
from app.services.video_studio.spec import Voiceover, parse_video_spec, validate_video_spec
from tests.test_video_studio_pronunciation_voice import (  # noqa: F401 - pipeline is a fixture
    LEXICON,
    PLAIN,
    _gate_failures_of,
    _Transcriber,
    _Voice,
    pipeline,
)
from tests.test_video_studio_render_checks import whole  # noqa: F401 - a fixture
from tests.video_voice_doubles import speech_config

DEMO = "Run the drills with OpenAEV every week."


class _Drops(_Voice):
    """A synthetic model whose *voices* say some written strings their own way: ``OpenAEV`` as ``OpenA``.

    *takes*: only its first that many requests do (a voice that slips once); ``None``: every one.
    """

    def __init__(self, says: dict[str, str], voices: frozenset[str], *, takes: int | None = None) -> None:
        super().__init__(PLAIN)
        self.says, self.voices, self.takes = says, voices, takes

    def said(self, request: SpeechRequest) -> list[str]:
        text = request.text
        slips = self.takes is None or len(self.requests) <= self.takes
        if (request.voice or "narrator") in self.voices and slips:
            for written, spoken in self.says.items():
                text = text.replace(written, spoken)
        return super().said(dataclasses.replace(request, text=text))


def _video(provider: _Voice) -> SpeechTarget:
    return SpeechTarget(provider=provider, model="tts-1-hd", config=speech_config(), voice="narrator")


def _speaker(target: SpeechTarget) -> voice_check.Speaker:
    return voice_check.Speaker(target, Voiceover(voice="brand"), _Transcriber(), None, lexicon=LEXICON)


def _spoken(pipeline, speaker, line: voice.Line) -> voice_check.SpokenSegment:  # noqa: F811
    return asyncio.run(voice_check.speak_checked(speaker, line, workdir=pipeline.workdir))


# ── F66: the retry after two changes the term's context ───────────────────


def test_f66_a_term_the_voice_drops_a_syllable_from_passes_respelled_in_the_video_s_own_voice(pipeline):  # noqa: F811
    # tts-1-hd reads no SSML: OpenAEV's text rewrite is OpenAEV itself (letters glued), which Brand says "OpenA".
    target = _video(_Drops({"OpenAEV": "OpenA"}, frozenset({"brand"})))
    speaker = _speaker(target)
    spoken = _spoken(pipeline, speaker, voice.Line("st-demo", DEMO, None, None))
    assert spoken.status == voice_check.PASSED and not spoken.fallback and spoken.voice_change is None
    assert [a.kind for a in spoken.attempts] == ["same", "again", "reframed"]
    assert [request.voice for request in target.provider.requests] == ["brand"] * 3
    # Sent the same way twice, heard wrong twice; then its respelling run by run, alone in its clause.
    assert spoken.attempts[0].sent == spoken.attempts[1].sent == DEMO
    assert [[(t.term, t.passed, t.heard) for t in a.check.terms] for a in spoken.attempts[:2]] == [
        [("OpenAEV", False, "OpenA")]
    ] * 2
    reframed = spoken.attempts[2]
    assert reframed.sent == "Run the drills with, Open AEV, every week."
    assert (reframed.mechanism, reframed.reframed, reframed.speed) == ("text_reframe", ("OpenAEV",), None)
    assert [(t.term, t.passed, t.heard) for t in reframed.check.terms] == [("OpenAEV", True, "Open AEV")]
    assert spoken.path == reframed.path
    assert any("OpenAEV respelled alone in its clause" in note for note in spoken.notes)
    # A pass of the video's own voice: nothing for the gate, and the next render places that take, never spoken again.
    assert _gate_failures_of(target, DEMO, LEXICON, Voiceover(voice="brand")) == []
    again = _spoken(pipeline, speaker, voice.Line("st-demo", DEMO, None, None))
    assert again.status == voice_check.PASSED and len(target.provider.requests) == 3
    assert again.attempts[0].object_name == reframed.object_name and again.attempts[0].reframed == ("OpenAEV",)
    assert any("OpenAEV respelled alone in its clause" in note for note in again.notes)


def test_only_a_term_two_takes_heard_wrong_is_set_apart():
    def take(kind: str, *wrong: str) -> voice_check.Attempt:
        terms = tuple(SimpleNamespace(term=term, passed=False) for term in wrong)
        return voice_check.Attempt(
            "brand", kind, f"/{kind}.wav", check=voice_check.TranscriptCheck(0.0, "", terms=terms)
        )

    assert voice_check.heard_wrong_twice([take("same", "CTEM"), take("again", "OpenAEV")]) == frozenset()
    assert voice_check.heard_wrong_twice([take("same", "CTEM", "CTEM"), take("again")]) == frozenset()
    assert voice_check.heard_wrong_twice([take("same", "CTEM", "OpenAEV"), take("again", "OpenAEV")]) == {"OpenAEV"}
    # Another voice's take is no take of the video's.
    assert voice_check.heard_wrong_twice([take("same", "CTEM"), take("closest", "CTEM")]) == frozenset()


def test_a_term_set_apart_is_its_respelling_run_by_run_with_a_comma_where_nothing_parts_it():
    entries = {entry.text: entry for entry in LEXICON.entries}
    assert lex.reframe_form(entries["OpenAEV"]) == "Open AEV" and lex.rewrite_form(entries["OpenAEV"]) == "OpenAEV"
    assert lex.reframe_form(entries["XTM One"]) == "XTM One" and lex.reframe_form(entries["Filigran"]) == "Fil-i-gran"

    # A model reading SSML phonemes is still sent text at this level: the phoneme was the first take's.
    phonemes = PronunciationSupport(ssml="inline", phoneme=True)

    def sent(text: str, *terms: str) -> str:
        return lex.render(text, LEXICON, phonemes, level=lex.REFRAMED, reframe=terms).text

    assert sent("OpenAEV runs the drills.", "OpenAEV") == "Open AEV, runs the drills."
    assert sent("Meet OpenAEV.", "OpenAEV") == "Meet, Open AEV."
    assert sent("Its twin (OpenAEV) runs them.", "OpenAEV") == "Its twin (Open AEV) runs them."
    # Only the term named is set apart; the others are rewritten as the retry before rewrote them.
    rendition = lex.render("OpenCTI and OpenAEV run CTEM.", LEXICON, phonemes, level=lex.REFRAMED, reframe={"OpenAEV"})
    assert rendition.text == "OpenCTI and, Open AEV, run see-tem." and not rendition.ssml
    assert rendition.mechanism == "text_rewrite+text_reframe"
    assert rendition.terms == (("OpenCTI", "text_rewrite"), ("OpenAEV", "text_reframe"), ("CTEM", "text_rewrite"))
    fallback = lex.render("OpenCTI and OpenAEV run CTEM.", LEXICON, phonemes, level=lex.FALLBACK)
    assert fallback.key() != rendition.key() and fallback.text == "OpenCTI and OpenAEV run see-tem."
    # Two terms set apart side by side: one comma between them, never two.
    assert sent("Run OpenCTI OpenAEV now.", "OpenCTI", "OpenAEV") == "Run, Open CTI, Open AEV, now."


# ── F65: the summary counts every take ────────────────────────────────────


def test_f65_a_term_one_take_failed_on_is_named_beside_the_take_kept(pipeline, monkeypatch, tmp_path):  # noqa: F811
    # Brand slips on its first take only: the retry passes, and is the take the render plays.
    target = _video(_Drops({"OpenAEV": "OpenA"}, frozenset({"brand"}), takes=1))
    line = voice.Line("st-demo", DEMO, None, None)
    spoken = _spoken(pipeline, _speaker(target), line)
    assert spoken.status == voice_check.PASSED and [a.kind for a in spoken.attempts] == ["same", "again"]
    media = render._Media()
    media.lines, media.lexicon = [line], LEXICON
    render._place_narration(media, spoken, forced=False)
    assert media.voice_paths == {"st-demo": spoken.attempts[1].path}

    async def heard(**_kwargs):
        # The final voice plays the take kept: heard as its own check heard it.
        hearing = voice_check.hearing_of(DEMO, spoken.transcript, lexicon=LEXICON)
        section, findings = audio_qc.pronunciation_findings(hearing, [audio_qc.Narration("st-demo", DEMO, 0.5, 3.0)])
        return audio_qc.AudioCheck(PASSED, findings, pronunciation=section)

    async def watched(*_args, **_kwargs):
        return SimpleNamespace(as_summary=lambda: {"status": PASSED}, findings=[])

    monkeypatch.setattr(audio_qc, "check_audio", heard)
    monkeypatch.setattr(frame_freeze, "check_frames", watched)
    plan = SimpleNamespace(narration_starts={"st-demo": 0.5}, document={"scenes": [{"id": "st-demo", "start": 0.0}]})
    checks = asyncio.run(
        render.check_render(
            final_path=str(tmp_path / "render.mp4"),
            stem=None,
            plan=plan,
            media=media,
            size=(1280, 720),
            fps=30,
            workdir=tmp_path,
        )
    )
    assert checks.pronunciation == {
        "status": PASSED,
        "terms": [
            {
                "term": "OpenAEV",
                "occurrences": 1,
                "passed": 1,
                "heard": ["OpenAEV"],
                "attempts_failed": [{"scene_id": "st-demo", "times": 1, "heard": ["OpenA"]}],
            }
        ],
    }
    assert render_checks.pronunciation_lines(checks.pronunciation) == [
        "Pronunciation: passed: OpenAEV 1 of 1 in the takes kept (heard 'OpenAEV'), 1 attempt failed on scene st-demo "
        "(heard 'OpenA')."
    ]
    # The narration's own check passed it: no finding, the render plays its own voice.
    assert checks.findings == [] and checks.voice[0]["status"] == PASSED


def test_a_term_no_take_kept_says_counts_every_take_it_failed_in():
    section = {"status": PASSED, "terms": [{"term": "CTEM", "occurrences": 2, "passed": 2, "heard": ["see tem"]}]}
    takes = [
        ("s2", [{"term": "OpenAEV", "passed": False, "heard": "OpenA"}]),
        (
            "s2",
            [
                {"term": "OpenAEV", "passed": False, "heard": "Open"},
                {"term": "OpenAEV", "passed": False, "heard": "Open"},
            ],
        ),
        ("s3", [{"term": "CTEM", "passed": False, "heard": "stem"}]),
    ]
    merged = audio_qc.with_attempts(section, takes)
    assert section["terms"][0].get("attempts_failed") is None
    assert render_checks.pronunciation_lines(merged) == [
        "Pronunciation: passed: CTEM 2 of 2 in the takes kept (heard 'see tem'), 1 attempt failed on scene s3 "
        "(heard 'stem'), OpenAEV in no take kept, 2 attempts failed on scene s2 (heard 'OpenA', 'Open')."
    ]
    # Nothing failed: the summary reads as it always did.
    assert audio_qc.with_attempts(section, []) is section
    assert render_checks.pronunciation_lines(section) == ["Pronunciation: passed: CTEM 2 of 2 (heard 'see tem')."]


# ── F64: another voice is refused like a failed narration ─────────────────

SCENES = {
    "s1": "OpenCTI maps every threat to the team.",
    "s2": DEMO,
    "s3": "XTM One ties the platforms together.",
}


def _three_scenes() -> dict:
    return validate_video_spec(
        {
            "title": "Three narrators",
            "freestyle": True,
            "voiceover": {"voice": "brand"},
            "scenes": [
                {"id": sid, "type": "title", "title": sid.upper(), "duration": 3, "narration": text}
                for sid, text in SCENES.items()
            ],
        }
    )


def test_f64_a_narration_only_another_voice_said_refuses_the_render_and_leaves_the_others_untouched(
    pipeline,  # noqa: F811
    whole,  # noqa: F811
):
    # Brand drops OpenAEV's last letters whatever it is sent - glued, respelled, slower; Anchor, its closest voice, does not.
    target = _video(_Drops({"OpenAEV": "OpenA", "Open AEV": "OpenA"}, frozenset({"brand"})))
    speaker = _speaker(target)
    effective = _three_scenes()
    spec = parse_video_spec(effective)
    lines = voice.lines_of(spec)
    spoken = [_spoken(pipeline, speaker, line) for line in lines]
    assert [(s.scene_id, s.status, s.fallback) for s in spoken] == [
        ("s1", voice_check.PASSED, False),
        ("s2", voice_check.PASSED, True),
        ("s3", voice_check.PASSED, False),
    ]
    # Two takes of OpenAEV heard wrong, then set apart, then slower - all Brand's - before Anchor.
    assert [a.kind for a in spoken[1].attempts] == ["same", "again", "reframed", "slower", "closest"]
    assert spoken[1].voice_change.voice == "Anchor"
    whole.effective, whole.project.spec = effective, effective
    whole.media.lines = lines
    for segment in spoken:
        render._place_narration(whole.media, segment, forced=False)
    asyncio.run(render.run_render(str(whole.row.id), workdir=whole.workdir))
    # Refused like a narration that failed: never drawn, never what the video plays.
    assert whole.drawn is False and whole.project.latest_render_id == whole.current
    failed = next(mark for mark in whole.marks if mark.get("status") == JOB_FAILED)
    assert "stopped before drawing its frames" in failed["error"] and "scene s2" in failed["error"]
    (finding,) = failed["checks"]["findings"]
    assert (finding["kind"], finding["severity"], finding["scene_id"]) == (render_checks.VOICE_FALLBACK, FAIL, "s2")
    assert "its narration is spoken by Anchor, not the video's voice Brand" in finding["message"]
    assert "reword the narration or add a pronunciation entry" in finding["message"]
    assert "make Anchor the video's voice (voiceover.voice)" in finding["message"]
    # Scenes 1 and 3 untouched: passed in the video's voice, one take each, nothing held against them.
    voices = {summary["scene_id"]: summary for summary in failed["checks"]["voice"]}
    assert [(voices[sid]["status"], voices[sid]["fallback"], voices[sid]["attempts"]) for sid in ("s1", "s3")] == [
        (voice_check.PASSED, False, 1)
    ] * 2
    held = asyncio.run(voice_check.failed_narrations(target, spec, LEXICON))
    assert [sid for sid, _record in held] == ["s2"]
    # The next render places their takes from the cache; only scene 2 is spoken again, in Brand first.
    asked = len(target.provider.requests)
    replay = [_spoken(pipeline, speaker, line) for line in lines]
    assert [len(s.attempts) for s in (replay[0], replay[2])] == [1, 1]
    assert target.provider.requests[asked].voice == "brand" and target.provider.requests[asked].text == DEMO
    assert all(
        request.text != SCENES["s1"] and request.text != SCENES["s3"] for request in target.provider.requests[asked:]
    )
