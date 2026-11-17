"""Every seeded term spoken by each configured speech model and heard back - opt-in, real cost (``README.md``).

A fixture narration holding every term of the seeded pronunciation lexicon
is spoken by each speech model whose key is set - through the platform's own
provider, the strongest mechanism the model offers (``lexicon.render``) and
the segment check (``voice_check.speak_checked``) - and heard back by OpenAI's
speech-to-text, the platform's one: the brand check must pass.

A second fixture gives CTEM a deliberately wrong ``say`` (its IPA kept): a
model sent the ``say`` form says it wrong, the video's voice is tried again
with the text rewrite, then slower, then the closest voice of the same model,
and the failure names the term and what was heard; a model
honouring the IPA says it right (an SSML phoneme always; the IPA in a
delivery instruction when the model follows it).

An ElevenLabs run registers a pronunciation dictionary in the account (the
provider lexicon), as a render does.
"""

from __future__ import annotations

import os
import uuid
from types import SimpleNamespace

import pytest

from app.llm.router import SpeechTarget
from app.llm.transcription import Transcriber
from app.services.video_studio import encoder, voice, voice_check
from app.services.video_studio import lexicon as lex
from app.services.video_studio.spec import Voiceover
from tests.live.live_llm_support import (
    KEY_ENV,
    build_live_provider,
    close_provider,
    live_marks,
    model_for,
    register_secret,
)
from tests.video_voice_doubles import in_memory_storage

SEED = lex.load_seed()
LEXICON = lex.merge(SEED)
WRONG = lex.merge(SEED, [{"text": "CTEM", "say": "stem", "ipa": "siːtɛm"}])
FIXTURE = "Filigran ships XTM One and XTM Hub. OpenCTI, OpenAEV and OpenCRQ run CTEM, from THREAD to XTM."


def _model(provider: str, env: str, default: str, label: str):
    # Heard back by OpenAI's speech-to-text: its key is needed whatever speaks.
    marks = live_marks(provider) + live_marks("openai")
    return pytest.param(provider, model_for(env, default), id=label, marks=marks)


MODELS = [
    _model("openai", "LIVE_SPEECH_OPENAI_MODEL", "gpt-4o-mini-tts", "openai"),
    _model("gemini", "LIVE_SPEECH_GEMINI_MODEL", "gemini-3.8-flash-tts", "gemini"),
    _model("elevenlabs", "LIVE_SPEECH_ELEVENLABS_MODEL", "eleven_multilingual_v2", "elevenlabs"),
    _model("elevenlabs", "LIVE_SPEECH_ELEVENLABS_PHONEME_MODEL", "eleven_flash_v2", "elevenlabs-phonemes"),
]


@pytest.fixture(autouse=True)
def _offline_cache(monkeypatch):
    if not encoder.encoder_available():
        pytest.skip("ffmpeg is not installed")
    in_memory_storage(monkeypatch)


def _target(provider_name: str, model: str) -> SpeechTarget:
    config = SimpleNamespace(id=uuid.uuid4(), provider=provider_name, display_name=provider_name)
    return SpeechTarget(provider=build_live_provider(provider_name), model=model, config=config, voice=None)


def _transcriber() -> Transcriber:
    key = os.environ[KEY_ENV["openai"]].strip()
    register_secret(key)
    return Transcriber(key)


@pytest.mark.parametrize(("provider_name", "model"), MODELS)
async def test_every_seeded_term_is_heard_as_the_lexicon_says_it(provider_name, model, tmp_path):
    target = _target(provider_name, model)
    try:
        speaker = voice_check.Speaker(target, Voiceover(), _transcriber(), None, lexicon=LEXICON)
        spoken = await voice_check.speak_checked(speaker, voice.Line("live", FIXTURE, None, None), workdir=str(tmp_path))
    finally:
        await close_provider(target.provider)
    placed = spoken.placed
    report = [(a.kind, a.mechanism, a.check.reason() if a.check else a.error) for a in spoken.attempts]
    assert spoken.status == voice_check.PASSED, report
    assert placed is not None and placed.sent and all(term["passed"] for term in placed.terms()), report


@pytest.mark.parametrize(("provider_name", "model"), MODELS)
async def test_a_deliberately_wrong_say_fails_with_the_term_and_what_was_heard(provider_name, model, tmp_path):
    target = _target(provider_name, model)
    support = voice.pronunciation_support(target)
    try:
        speaker = voice_check.Speaker(target, Voiceover(), _transcriber(), None, lexicon=WRONG)
        spoken = await voice_check.speak_checked(speaker, voice.Line("live", "Run CTEM today.", None, None), workdir=str(tmp_path))
    finally:
        await close_provider(target.provider)
    if support.phoneme:
        # Its phoneme says the IPA, whatever the say form.
        assert spoken.status == voice_check.PASSED and spoken.attempts[0].mechanism == "ssml_phoneme"
        return
    if support.instruction and spoken.status == voice_check.PASSED:
        # The IPA in a delivery instruction may say it right, whatever the say form: then that take is placed,
        # and one of another voice of the same model says so.
        assert spoken.placed is not None and spoken.placed.mechanism == "ipa_instruction"
        assert (spoken.voice_change is not None) == (spoken.placed.kind == "closest")
        return
    assert spoken.status == voice_check.FAILED
    assert [a.kind for a in spoken.attempts][:3] == ["same", "again", "slower"]
    assert spoken.attempts[1].mechanism == "text_rewrite" and "stem" in (spoken.attempts[1].sent or "")
    assert "brand term mispronounced: CTEM heard as" in (spoken.reason or "") and "tried:" in (spoken.reason or "")
