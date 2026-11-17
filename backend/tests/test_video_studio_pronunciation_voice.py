"""Every seeded term spoken and heard back through the voice pipeline, on synthetic speech models.

One speech model per family of mechanism - one reading inline SSML phonemes
and keeping a provider lexicon (the shape of ElevenLabs' phoneme models), one
keeping a provider lexicon only, one reading plain text (OpenAI, Gemini, a
gateway) - each saying exactly what it is sent: a phoneme as the reading its
IPA denotes, an alias rule as its alias, plain text as written. A scripted
speech-to-text hears it word by word. Between them, the real pipeline: the
lexicon's rendition per model, the voice cache, the provider lexicon
registered once, the segment check and its retry ladder.

* a fixture narration holding every seeded term passes with each model, through
  that model's strongest mechanism, the exact string sent recorded;
* a deliberately wrong ``say`` fails the segment: the same voice is tried
  again with the text rewrite (letters glued), then with the term heard wrong
  twice alone in its clause, then slower, then the closest voice of
  the same model; the failure names the term, what was heard and each
  mechanism tried;
* a narration the video's voice says wrong on every take and the closest
  voice of its model says right fails the render with a finding of
  the render and of the quality check naming the voice and why - never a pass
  in the platform's default voice;
* a voice forced to say ``Phil Igram`` fails the gate, and a model taking the
  IPA in its delivery instruction is heard saying ``Filigran``;
* the ElevenLabs adapter's own requests: the phoneme models, the dictionary
  registered from alias rules, the locator sent with the speech.
"""

from __future__ import annotations

import asyncio
import dataclasses
import hashlib
import html
import io
import re
import struct
import wave
from types import SimpleNamespace

import httpx
import pytest

from app.llm.router import SpeechTarget
from app.llm.speech import LexiconRule, PronunciationSupport, SpeechRequest, SpeechResult, Voice
from app.llm.transcription import TranscribedWord, Transcript
from app.services.video_studio import encoder, quality, render, render_checks, voice, voice_check
from app.services.video_studio import lexicon as lex
from app.services.video_studio.spec import Voiceover
from tests.video_voice_doubles import in_memory_storage, speech_config

SEED = lex.load_seed()
LEXICON = lex.merge(SEED)
#: How the brand owner says each IPA: what a model honouring a phoneme says.
READINGS = {entry.ipa: entry.say for entry in SEED if entry.ipa}
FIXTURE = "Filigran ships XTM One and XTM Hub. OpenCTI, OpenAEV and OpenCRQ run CTEM, from THREAD to XTM."
TERMS = ["Filigran", "XTM One", "XTM Hub", "OpenCTI", "OpenAEV", "OpenCRQ", "CTEM", "THREAD", "XTM"]
_PHONEME = re.compile(r'<phoneme alphabet="ipa" ph="([^"]+)">([^<]*)</phoneme>')
#: What each synthetic take said, by the digest of its bytes.
_SAID: dict[str, list[str]] = {}


def _wav(seed: int) -> bytes:
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as out:
        out.setnchannels(1)
        out.setsampwidth(2)
        out.setframerate(48000)
        out.writeframes(struct.pack("<I", seed) * 6000)
    return buffer.getvalue()


#: The voices every synthetic model lists, as a provider describes them (``speech.Voice``).
VOICES = (
    Voice("narrator", "Narrator", "", "british, male, calm"),
    Voice("brand", "Brand", "", "american, female, warm"),
    Voice("anchor", "Anchor", "", "american, female, warm, low"),
)


class _Voice:
    """A synthetic speech model: it says what it is sent, through the mechanisms it declares.

    *phonemes* ``skip``: every voice reads a phoneme tag's word itself; a set of voice ids: those voices only.
    """

    def __init__(self, support: PronunciationSupport, *, phonemes: str | frozenset[str] = "honour") -> None:
        self.support, self.phonemes = support, phonemes
        self.requests: list[SpeechRequest] = []
        self.registered: list[tuple[LexiconRule, ...]] = []

    def speech_pronunciation(self, model: str) -> PronunciationSupport:
        return self.support

    async def list_voices(self, model: str | None = None) -> list[Voice]:
        return list(VOICES)

    async def register_pronunciation_lexicon(self, name: str, rules) -> str:
        self.registered.append(tuple(rules))
        return f"dictionary-{len(self.registered)}:version-1"

    def _phoneme(self, match: re.Match, voice_id: str) -> str:
        ipa, written = html.unescape(match.group(1)), match.group(2)
        if self.phonemes == "skip" or (isinstance(self.phonemes, frozenset) and voice_id in self.phonemes):
            # A model that skips a phoneme tag reads the word itself: an acronym, letter by letter.
            return " ".join(written) if written.isupper() else written
        return READINGS[ipa]

    def said(self, request: SpeechRequest) -> list[str]:
        text = request.text
        if request.ssml:
            body = text.removeprefix("<speak>").removesuffix("</speak>")
            text = html.unescape(_PHONEME.sub(lambda match: self._phoneme(match, request.voice or "narrator"), body))
        for locator in request.lexicons:
            rules = self.registered[int(locator.split(":")[0].rsplit("-", 1)[1]) - 1]
            for rule in rules:
                flags = 0 if rule.case_sensitive else re.IGNORECASE
                text = re.sub(rf"(?<!\w){re.escape(rule.text)}(?!\w)", rule.alias, text, flags=flags)
        return re.findall(r"[^\W_]+", text)

    async def synthesize_speech(self, request: SpeechRequest) -> SpeechResult:
        self.requests.append(request)
        data = _wav(len(_SAID) + 1)
        _SAID[hashlib.sha256(data).hexdigest()] = self.said(request)
        return SpeechResult(data, "audio/wav", request.voice or "narrator", [])


def _target(support: PronunciationSupport, **kwargs) -> SpeechTarget:
    return SpeechTarget(provider=_Voice(support, **kwargs), model="synthetic-tts", config=speech_config(), voice="narrator")


INLINE = PronunciationSupport(ssml="inline", phoneme=True, lexicon=True)
LEXICON_ONLY = PronunciationSupport(lexicon=True)
PLAIN = PronunciationSupport()


class _Transcriber:
    model = "scripted-stt"

    def __init__(self) -> None:
        self.calls = 0


@pytest.fixture
def pipeline(monkeypatch, tmp_path):
    """The voice cache, the check records and the provider lexicon ids in memory; a take heard as it was said."""

    async def condition(source, out):
        with open(source, "rb") as raw, open(out, "wb") as placed:
            placed.write(raw.read())

    async def heard(transcriber, path, *, words=False):
        transcriber.calls += 1
        with open(path, "rb") as take:
            said = _SAID[hashlib.sha256(take.read()).hexdigest()]
        timed = tuple(TranscribedWord(word, 0.4 * i, 0.4 * i + 0.3) for i, word in enumerate(said))
        return Transcript(" ".join(said), timed)

    objects = in_memory_storage(monkeypatch)
    monkeypatch.setattr(encoder, "condition_voice", condition)
    monkeypatch.setattr(voice_check, "transcribe_wav", heard)
    return SimpleNamespace(objects=objects, workdir=str(tmp_path))


def _speak(pipeline, speaker, text: str = FIXTURE, scene: str = "s1") -> voice_check.SpokenSegment:
    return asyncio.run(voice_check.speak_checked(speaker, voice.Line(scene, text, None, None), workdir=pipeline.workdir))


@pytest.mark.parametrize(
    ("support", "mechanism"),
    [
        pytest.param(INLINE, "ssml_phoneme", id="ssml-phonemes"),
        pytest.param(LEXICON_ONLY, "provider_lexicon", id="provider-lexicon"),
        pytest.param(PLAIN, "text_rewrite", id="plain-text"),
    ],
)
def test_every_seeded_term_passes_through_each_model_s_strongest_mechanism(pipeline, support, mechanism):
    target = _target(support)
    transcriber = _Transcriber()
    speaker = voice_check.Speaker(target, Voiceover(), transcriber, None, lexicon=LEXICON)
    spoken = _speak(pipeline, speaker)
    assert spoken.status == voice_check.PASSED and len(spoken.attempts) == 1
    attempt = spoken.attempts[0]
    assert attempt.mechanism == mechanism
    assert [(t.term, t.passed) for t in attempt.check.terms] == [(term, True) for term in TERMS]
    (request,) = target.provider.requests
    assert attempt.sent == request.text
    if support is INLINE:
        assert request.ssml and '<phoneme alphabet="ipa" ph="siːtɛm">CTEM</phoneme>' in request.text
        # Every letters entry is one phonetic token: nothing left to the provider's lexicon.
        assert '<phoneme alphabet="ipa" ph="ˌɛksˌtiːˈɛm ˈwʌn">XTM One</phoneme>' in request.text and not request.lexicons
    elif support is LEXICON_ONLY:
        assert request.text == FIXTURE and request.lexicons and not request.ssml
    else:
        # Letters glued into one token, never hyphens or spaces between them.
        assert "see-tem" in request.text and "XTM One" in request.text and not request.lexicons
        assert not set(request.text) & {"\u2011", "\u202f"}
    summary = spoken.summary()
    assert summary["mechanism"] == mechanism and summary["sent"] == request.text
    assert all(term["passed"] for term in summary["terms"])
    # Heard once: the next render places it from the cache, never spoken or transcribed again.
    again = _speak(pipeline, speaker)
    assert again.status == voice_check.PASSED and len(target.provider.requests) == 1 and transcriber.calls == 1
    assert again.summary()["mechanism"] == mechanism and again.summary()["sent"] == request.text


def test_the_provider_lexicon_is_registered_once_and_its_id_kept(pipeline):
    target = _target(LEXICON_ONLY)
    speaker = voice_check.Speaker(target, Voiceover(), _Transcriber(), None, lexicon=LEXICON)
    _speak(pipeline, speaker, "OpenCTI everywhere.", "s1")
    _speak(pipeline, speaker, "CTEM in XTM Hub.", "s2")
    assert len(target.provider.registered) == 1
    rules = {rule.text: rule for rule in target.provider.registered[0]}
    assert rules["CTEM"].alias == "see-tem" and rules["THREAD"].case_sensitive is False
    assert rules["XTM One"].alias == "XTM One"
    # Another process reads the id kept in the voice cache: nothing registered again.
    voice._LEXICON_IDS.clear()
    _speak(pipeline, speaker, "OpenAEV today.", "s3")
    assert len(target.provider.registered) == 1


WRONG = lex.merge(SEED, [{"text": "CTEM", "say": "stem", "ipa": "siːtɛm"}])


def test_a_deliberately_wrong_say_fails_the_segment_after_each_mechanism_and_names_them(pipeline):
    # Every voice of the video's model skips phoneme tags; its say for CTEM is wrong.
    video = _target(INLINE, phonemes="skip")
    speaker = voice_check.Speaker(video, Voiceover(), _Transcriber(), None, lexicon=WRONG)
    spoken = _speak(pipeline, speaker, "Run CTEM with OpenCTI.")
    assert spoken.status == voice_check.FAILED and spoken.voice_change is None
    assert [a.kind for a in spoken.attempts] == ["same", "again", "reframed", "slower", "closest"]
    assert [a.mechanism for a in spoken.attempts] == [
        "ssml_phoneme",
        "text_rewrite",
        "text_rewrite+text_reframe",
        "text_rewrite+text_reframe",
        "ssml_phoneme",
    ]
    # The retry rewrites the text, letters glued: the wrong say as given, OpenCTI as one word. CTEM heard wrong twice is
    # then said alone in its clause, then slower: a wrong say stays wrong whatever its context.
    assert spoken.attempts[1].sent == "Run stem with OpenCTI."
    assert spoken.attempts[2].sent == spoken.attempts[3].sent == "Run, stem, with OpenCTI."
    assert spoken.attempts[2].reframed == spoken.attempts[3].reframed == ("CTEM",)
    assert [request.speed for request in video.provider.requests] == [1.0, 1.0, 1.0, 0.85, 1.0]
    assert [[(t.term, t.passed, t.heard) for t in a.check.terms] for a in spoken.attempts] == [
        [("CTEM", False, "C T E M"), ("OpenCTI", True, "OpenCTI")],
        [("CTEM", False, "stem"), ("OpenCTI", True, "OpenCTI")],
        [("CTEM", False, "stem"), ("OpenCTI", True, "OpenCTI")],
        [("CTEM", False, "stem"), ("OpenCTI", True, "OpenCTI")],
        # The closest voice of the same model (Brand: no voice shares a trait with Narrator) reads no phoneme either.
        [("CTEM", False, "C T E M"), ("OpenCTI", True, "OpenCTI")],
    ]
    assert spoken.reason.startswith("brand term mispronounced: CTEM heard as 'C T E M' at 0:00.4")
    assert spoken.reason.endswith(
        "tried: ssml_phoneme (the video's voice), text_rewrite (the video's voice again), "
        "text_rewrite+text_reframe (the video's voice, CTEM respelled alone in its clause), "
        "text_rewrite+text_reframe (the video's voice at 0.85, CTEM respelled alone in its clause), "
        "ssml_phoneme (the closest voice, Brand)"
    )
    # Never placed silently: the render's finding names the segment, the term, what was heard, what was tried.
    media = render._Media()
    media.spoken = [spoken]
    plan = SimpleNamespace(narration_starts={"s1": 2.0})
    (finding,) = render._voice_findings(media, plan)
    assert finding.kind == render_checks.VOICE_SEGMENT and finding.scene_id == "s1" and finding.at == 2.0
    assert "brand term mispronounced: CTEM heard as 'C T E M'" in finding.message and "tried:" in finding.message
    assert [a["mechanism"] for a in finding.data["attempts"]] == [a.mechanism for a in spoken.attempts]
    text = render_checks.format_checks(render_checks.Checks(voice=[spoken.summary()], findings=[finding]).as_dict())
    assert "scene `s1` (voice_segment)" in text and "CTEM heard as 'C T E M'" in text


class _Forced(_Voice):
    """A synthetic model that says some words its own way whatever it is sent: ``Filigran`` as ``Phil Igram``."""

    def __init__(self, support: PronunciationSupport, says: dict[str, str]) -> None:
        super().__init__(support)
        self.says = says

    def said(self, request: SpeechRequest) -> list[str]:
        text = request.text
        for written, spoken in self.says.items():
            text = text.replace(written, spoken)
        return super().said(dataclasses.replace(request, text=text))


BRAND_LINE = "Filigran brings open source threat intelligence and exposure management to every security team today."


def _gate_failures_of(target: SpeechTarget, narration: str, lexicon, voiceover: Voiceover) -> list[tuple[str, dict]]:
    """What the render gate reads (``render_checks.gate``): the narrations whose last check failed in their own voice."""
    spec = SimpleNamespace(scenes=[SimpleNamespace(id="s1", narration=narration)], voiceover=voiceover)
    return asyncio.run(voice_check.failed_narrations(target, spec, lexicon))


def _gate_failures(target: SpeechTarget) -> list[tuple[str, dict]]:
    return _gate_failures_of(target, BRAND_LINE, LEXICON, Voiceover())


def test_a_narration_forced_to_phil_igram_fails_the_gate(pipeline):
    # Video b487288e: whatever it is sent - the written form, its respelling - the voice says "Phil Igram".
    forced = SpeechTarget(
        provider=_Forced(PLAIN, {"Fil-i-gran": "Phil Igram", "Filigran": "Phil Igram"}),
        model="tts-1-hd",
        config=speech_config(),
        voice="narrator",
    )
    speaker = voice_check.Speaker(forced, Voiceover(), _Transcriber(), None, lexicon=LEXICON)
    spoken = _speak(pipeline, speaker, BRAND_LINE)
    assert spoken.status == voice_check.FAILED
    assert [a.kind for a in spoken.attempts] == ["same", "again", "reframed", "slower", "closest"]
    assert [[(t.term, t.passed, t.heard) for t in a.check.terms] for a in spoken.attempts] == [[("Filigran", False, "Phil Igram")]] * 5
    assert spoken.reason.startswith("brand term mispronounced: Filigran heard as 'Phil Igram' at 0:00.0")
    # tts-1-hd takes no IPA: the narration said so, and said Filigran by its respelling - heard wrong twice, alone in its
    # clause (it starts the narration, so a comma after it only).
    respelled = BRAND_LINE.replace("Filigran", "Fil-i-gran")
    assert [a.sent for a in spoken.attempts] == [respelled, respelled, *[respelled.replace("gran ", "gran, ")] * 2, respelled]
    assert any('"Filigran" has an IPA' in note and "tts-1-hd cannot take" in note for note in spoken.notes)
    (failed,) = _gate_failures(forced)
    assert failed[0] == "s1" and "Filigran heard as 'Phil Igram'" in failed[1]["reason"]


def test_a_narration_said_with_the_ipa_entry_passes_heard_as_written(pipeline):
    # A model taking the IPA in its delivery instruction (gpt-4o-mini-tts) is sent the written form and the IPA.
    target = SpeechTarget(
        provider=_Voice(PronunciationSupport(instruction=True)), model="gpt-4o-mini-tts", config=speech_config(), voice="marin"
    )
    speaker = voice_check.Speaker(target, Voiceover(), _Transcriber(), None, lexicon=LEXICON)
    spoken = _speak(pipeline, speaker, BRAND_LINE)
    assert spoken.status == voice_check.PASSED and len(spoken.attempts) == 1
    (request,) = target.provider.requests
    assert request.text == BRAND_LINE and request.pronunciations == (("Filigran", "ˈfɪlɪɡræn"),) and not request.ssml
    (term,) = spoken.attempts[0].check.terms
    assert (term.term, term.passed, term.heard, term.reading) == ("Filigran", True, "Filigran", "written")
    assert not term.heard_otherwise and spoken.attempts[0].mechanism == "ipa_instruction" and spoken.notes == []
    assert _gate_failures(target) == []


def test_a_narration_the_video_s_voice_fails_ends_with_a_voice_change_finding_never_a_default_voice_pass(
    pipeline, monkeypatch
):
    """The speech-to-text hears the video's voice say CTEM wrong on every take; another voice of its model is right."""
    from app.llm import router
    from app.services.video_studio.spec import parse_video_spec

    async def no_default_voice(*_args, **_kwargs):
        raise AssertionError("the platform's default voice is never asked for a narration")

    monkeypatch.setattr(router, "get_speech_provider", no_default_voice)
    # The video's voice, Brand, reads no phoneme tag; the other voices of its model do.
    video = _target(INLINE, phonemes=frozenset({"brand"}))
    speaker = voice_check.Speaker(video, Voiceover(voice="brand"), _Transcriber(), None, lexicon=WRONG)
    spoken = _speak(pipeline, speaker, "Run CTEM with OpenCTI.")
    assert [a.kind for a in spoken.attempts] == ["same", "again", "reframed", "slower", "closest"]
    assert [request.voice for request in video.provider.requests] == ["brand", "brand", "brand", "brand", "anchor"]
    assert spoken.status == voice_check.PASSED and spoken.attempts[-1].check.terms[0].heard == "see tem"
    change = spoken.voice_change
    assert (change.video_voice, change.voice) == ("Brand", "Anchor")
    assert change.reason.startswith("brand term mispronounced: CTEM heard as 'C T E M'")
    assert change.closeness == "the voice of the same speech model sharing the most with Brand (american, female, warm)"
    # The render's answer: a failure naming the scene, the voice used and why - never "passed".
    media = render._Media()
    media.spoken = [spoken]
    (finding,) = render._voice_findings(media, SimpleNamespace(narration_starts={"s1": 2.0}))
    assert (finding.kind, finding.severity, finding.scene_id, finding.at) == (
        render_checks.VOICE_FALLBACK,
        render_checks.FAIL,
        "s1",
        2.0,
    )
    assert finding.data["voice_change"] == change.as_dict()
    checks = render_checks.Checks(voice=[spoken.summary()], findings=[finding], audio={"status": "passed"})
    assert checks.status == render_checks.FAILED
    text = render_checks.format_checks(checks.as_dict())
    assert (
        "Narrations heard back: 0 of 1 passed in the video's voice, 1 spoken by another voice after the video's failed "
        "(each listed below)."
    ) in text
    assert (
        "- [FAIL] 0:02.00-0:02.25 scene `s1` (voice_fallback): its narration is spoken by Anchor, not the video's voice "
        "Brand: Brand failed its transcript check (brand term mispronounced: CTEM heard as 'C T E M'"
    ) in text
    # Failed in the video's voice: the gate holds the video, the next render tries Brand again.
    (failed,) = _gate_failures_of(video, "Run CTEM with OpenCTI.", WRONG, Voiceover(voice="brand"))
    assert failed[0] == "s1" and failed[1]["fallback_voice"] == "Anchor"
    assert failed[1]["reason"].endswith("the closest voice of its speech model (Anchor) passed in its place")
    # The quality check reads it from the narration's record: a finding the audience notices, which a render waits for.
    spec = parse_video_spec(
        {
            "title": "Voice change",
            "freestyle": True,
            "voiceover": {"voice": "brand"},
            "scenes": [{"id": "s1", "type": "title", "title": "Run it", "narration": "Run CTEM with OpenCTI."}],
        }
    )
    facts = quality.ContentFacts(voice_changes=dict(asyncio.run(voice_check.voice_changes(video, spec, WRONG))))
    document = {"duration": 4.0, "scenes": [{"id": "s1", "type": "title", "start": 0.0, "end": 4.0}]}
    (found,) = [f for f in quality.content_findings(spec, document, facts, None) if f.kind == "voice_fallback"]
    assert found.severity == "dense" and found.severity in render_checks.BLOCKING_SEVERITIES and found.group == "content"
    assert found.message == change.sentence("s1")


# ── The editor's play button ──────────────────────────────────────────────


def test_the_editor_plays_a_term_as_the_video_s_voice_says_it_spoken_once(pipeline, monkeypatch):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from app.api import deps
    from app.api.v1 import video_studio as api
    from app.core.database import get_db
    from app.services.video_studio import lexicon_store

    target = _target(PLAIN)
    unavailable: list[str] = []

    async def resolve(_db, _user_id, voiceover):
        if unavailable:
            raise voice.VoiceUnavailableError(unavailable[0])
        assert voiceover.voice == "brand"
        return target

    async def lexicon_of(_db, voiceover):
        return lex.merge(SEED, voiceover.pronunciations)

    class _Db:
        async def rollback(self):
            return None

    monkeypatch.setattr(voice, "resolve", resolve)
    monkeypatch.setattr(lexicon_store, "for_voiceover", lexicon_of)
    app = FastAPI()
    app.include_router(api.router, prefix="/api/v1/video-studio")
    app.dependency_overrides[deps.get_current_user] = lambda: SimpleNamespace(id=uuid_of("reader"), is_admin=False)
    app.dependency_overrides[get_db] = lambda: _Db()
    app.dependency_overrides[deps.require_ai_enabled] = lambda: None
    client = TestClient(app)
    body = {"text": "CTEM", "voiceover": {"voice": "brand"}}
    first = client.post("/api/v1/video-studio/pronunciations/speak", json=body)
    assert first.status_code == 200, first.text
    played = first.json()
    assert played["mechanism"] == "text_rewrite" and played["sent"] == "see-tem" and played["seconds"] == 0.25
    assert "/api/v1/video-studio/voice/" in played["audio_url"]
    assert client.post("/api/v1/video-studio/pronunciations/speak", json=body).json()["audio_url"] == played["audio_url"]
    assert len(target.provider.requests) == 1
    # The draft's own entry overrides the platform's.
    own = {"text": "CTEM", "voiceover": {"voice": "brand", "pronunciations": [{"text": "CTEM", "say": "see-tem-program"}]}}
    assert client.post("/api/v1/video-studio/pronunciations/speak", json=own).json()["sent"] == "see-tem-program"
    unavailable.append("No speech model is configured.")
    refused = client.post("/api/v1/video-studio/pronunciations/speak", json=body)
    assert refused.status_code == 409 and "No speech model" in refused.json()["detail"]


def uuid_of(name: str):
    import uuid

    return uuid.uuid5(uuid.NAMESPACE_URL, name)


# ── The ElevenLabs adapter ────────────────────────────────────────────────

_REAL_CLIENT = httpx.AsyncClient


def _eleven(monkeypatch, handler):
    from app.llm import elevenlabs_provider

    def factory(*args, **kwargs):
        kwargs["transport"] = httpx.MockTransport(handler)
        return _REAL_CLIENT(*args, **kwargs)

    monkeypatch.setattr(elevenlabs_provider.httpx, "AsyncClient", factory)
    return elevenlabs_provider.ElevenLabsProvider(api_key="sk_test")


def test_elevenlabs_reads_phonemes_on_its_phoneme_models_and_a_dictionary_on_all(monkeypatch):
    sent: list[tuple[str, dict]] = []

    def api(request: httpx.Request) -> httpx.Response:
        import json

        body = json.loads(request.content or b"{}")
        sent.append((request.url.path, body))
        if request.url.path == "/v1/pronunciation-dictionaries/add-from-rules":
            return httpx.Response(200, json={"id": "pd1", "version_id": "v7"})
        if request.url.path == "/v2/voices":
            return httpx.Response(200, json={"voices": [{"voice_id": "voice1", "name": "Narrator"}], "has_more": False})
        return httpx.Response(200, content=b"ID3audio")

    provider = _eleven(monkeypatch, api)
    assert provider.speech_pronunciation("eleven_flash_v2") == INLINE
    assert provider.speech_pronunciation("eleven_multilingual_v2") == LEXICON_ONLY
    rules = lex.provider_rules(LEXICON)
    assert asyncio.run(provider.register_pronunciation_lexicon("xtm-one-abc", rules)) == "pd1:v7"
    path, body = sent[0]
    assert path == "/v1/pronunciation-dictionaries/add-from-rules" and body["name"] == "xtm-one-abc"
    assert {
        "type": "alias",
        "string_to_replace": "CTEM",
        "alias": "see-tem",
        "case_sensitive": True,
        "word_boundaries": True,
    } in body["rules"]
    asyncio.run(
        provider.synthesize_speech(
            SpeechRequest(model="eleven_flash_v2", text="Run CTEM.", voice="voice1", ssml=True, lexicons=("pd1:v7",))
        )
    )
    path, body = sent[-1]
    assert path == "/v1/text-to-speech/voice1"
    assert body["pronunciation_dictionary_locators"] == [{"pronunciation_dictionary_id": "pd1", "version_id": "v7"}]


def test_the_other_speech_providers_get_the_text_rewrite():
    from app.llm.base import BaseLLMProvider

    assert BaseLLMProvider.speech_pronunciation(SimpleNamespace(), "gpt-4o-mini-tts") == PLAIN
    assert voice.pronunciation_support(SimpleNamespace(provider=object(), model="m")) == PLAIN
