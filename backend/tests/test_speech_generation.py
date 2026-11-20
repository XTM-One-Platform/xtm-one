"""Speech generation as a provider capability.

The classification of speech models, the OpenAI speech API on every
OpenAI-shaped provider, Gemini's speech routes, the ElevenLabs voice
generator, the platform's choice of provider / model / voice and its two
consumers: the chat's read-aloud and the Video Studio voice-over.
"""

import asyncio
import base64
import json
import uuid
from pathlib import Path
from types import SimpleNamespace

import httpx
import openai
import pytest

from app.llm import router, speech
from app.llm.speech import SpeechGenerationError, SpeechRequest, Voice

# ── Which models speak, which providers chat ─────────────────────────────


def test_speech_models_are_recognised_from_the_catalog_then_their_marker():
    for model in ("tts-1-hd", "gpt-4o-mini-tts", "elevenlabs/eleven_v3", "aws_polly/neural", "vertex_ai/chirp"):
        assert router.is_speech_model(model), model
    for model in ("gpt-4o", "eleven-chat", "claude-sonnet-5", "", None):
        assert not router.is_speech_model(model), model
    # A newer snapshot the catalog does not know yet keeps its marker.
    assert router.is_speech_model("gpt-5-mini-tts-2026-09-01")
    assert not router.is_chat_capable_model("vertex_ai/chirp")


def test_a_provider_serving_one_capability_never_chats():
    assert {"tei", "elevenlabs"} <= router.CHATLESS_PROVIDERS
    assert frozenset({"tei"}) == router.EMBEDDING_ONLY_PROVIDERS
    assert router.PROVIDER_MAP["elevenlabs"].only_capability == "speech"
    assert "openai" not in router.CHATLESS_PROVIDERS


def test_the_built_in_speech_models_come_from_the_provider_classes():
    assert {
        name: cls.default_speech_model
        for name, cls in router.PROVIDER_MAP.items()
        if getattr(cls, "default_speech_model", None)
    } == router.SPEECH_DEFAULTS
    assert {"openai", "gemini", "elevenlabs"} <= set(router.SPEECH_DEFAULTS)
    # A provider configured for speech alone is tried first.
    assert router._SPEECH_PROVIDERS[0] == "elevenlabs"


# ── The OpenAI speech API ─────────────────────────────────────────────────


class _SpeechAPI:
    def __init__(self, refuse_instructions: bool = False):
        self.calls: list[dict] = []
        self.refuse_instructions = refuse_instructions

    async def create(self, **kwargs):
        self.calls.append(kwargs)
        if self.refuse_instructions and "instructions" in kwargs:
            request = httpx.Request("POST", "https://x/audio/speech")
            raise openai.BadRequestError("no", response=httpx.Response(400, request=request), body=None)
        return SimpleNamespace(read=lambda: b"AUDIO")


def _openai_shaped(cls, api: _SpeechAPI):
    provider = cls.__new__(cls)
    provider.client = SimpleNamespace(audio=SimpleNamespace(speech=api))
    return provider


def test_openai_speaks_with_its_voices_and_takes_the_style():
    from app.llm.openai_provider import OpenAIProvider

    api = _SpeechAPI()
    provider = _openai_shaped(OpenAIProvider, api)
    voices = [v.id for v in asyncio.run(provider.list_voices("gpt-4o-mini-tts"))]
    assert voices[0] == "marin" and "cedar" in voices and len(voices) == 13
    result = asyncio.run(
        provider.synthesize_speech(
            SpeechRequest(
                model="gpt-4o-mini-tts", text="Hi", voice="Coral", style="warm", speed=2.0, audio_format="wav"
            )
        )
    )
    call = api.calls[0]
    assert call["voice"] == "coral" and call["instructions"] == "warm" and call["response_format"] == "wav"
    assert call["speed"] == speech.MAX_SPEED
    assert result.mime == "audio/wav" and result.audio == b"AUDIO" and result.voice == "coral"


def _answering(content_type: str | None) -> _SpeechAPI:
    """A speech API answering as the SDK does, with the ``Content-Type`` the server sent."""
    from openai._legacy_response import HttpxBinaryResponseContent

    api = _SpeechAPI()

    async def create(**kwargs):
        api.calls.append(kwargs)
        headers = {"content-type": content_type} if content_type else {}
        return HttpxBinaryResponseContent(httpx.Response(200, content=b"AUDIO", headers=headers))

    api.create = create
    return api


@pytest.mark.parametrize("content_type", ["audio/wav", "audio/x-wav", "audio/wave", "audio/WAV; codecs=1"])
def test_speech_is_labelled_by_the_format_returned_not_the_one_asked(content_type):
    # A Gemini TTS model through the AI Gateway (an OpenAI-compatible provider) answers WAV to an MP3 request.
    from app.llm.openai_compat import OpenAICompatibleProvider

    provider = _openai_shaped(OpenAICompatibleProvider, _answering(content_type))
    result = asyncio.run(provider.synthesize_speech(SpeechRequest(model="gemini-3-flash-tts", text="Hi", voice="Kore")))
    assert result.mime == "audio/wav" and result.audio == b"AUDIO"


@pytest.mark.parametrize(
    ("content_type", "audio_format", "mime"),
    [
        ("audio/mp3", "mp3", "audio/mpeg"),
        (None, "mp3", "audio/mpeg"),
        ("application/octet-stream", "mp3", "audio/mpeg"),
        ("application/octet-stream", "wav", "audio/wav"),
    ],
)
def test_speech_without_an_audio_content_type_keeps_the_format_asked(content_type, audio_format, mime):
    from app.llm.openai_provider import OpenAIProvider

    provider = _openai_shaped(OpenAIProvider, _answering(content_type))
    result = asyncio.run(
        provider.synthesize_speech(SpeechRequest(model="tts-1", text="Hi", voice="alloy", audio_format=audio_format))
    )
    assert result.mime == mime


def test_the_first_openai_models_take_no_style_and_fewer_voices():
    from app.llm.openai_provider import OpenAIProvider

    api = _SpeechAPI()
    provider = _openai_shaped(OpenAIProvider, api)
    assert "marin" not in [v.id for v in asyncio.run(provider.list_voices("tts-1-hd"))]
    result = asyncio.run(
        provider.synthesize_speech(SpeechRequest(model="tts-1-hd", text="Hi", voice="marin", style="warm"))
    )
    assert "instructions" not in api.calls[0]
    assert api.calls[0]["voice"] == "nova"
    assert any("not offered" in n for n in result.notes) and any("no delivery instruction" in n for n in result.notes)


def test_a_refused_instruction_is_retried_without_it():
    from app.llm.openai_provider import OpenAIProvider

    api = _SpeechAPI(refuse_instructions=True)
    result = asyncio.run(
        _openai_shaped(OpenAIProvider, api).synthesize_speech(
            SpeechRequest(model="gpt-4o-mini-tts", text="Hi", style="x")
        )
    )
    assert len(api.calls) == 2 and "instructions" not in api.calls[1]
    assert any("refused the delivery instruction" in n for n in result.notes)


def test_openai_takes_a_terms_ipa_in_its_instruction_and_tts_1_does_not():
    from app.llm.openai_provider import OpenAIProvider

    api = _SpeechAPI()
    provider = _openai_shaped(OpenAIProvider, api)
    assert provider.speech_pronunciation("gpt-4o-mini-tts") == speech.PronunciationSupport(instruction=True)
    assert provider.speech_pronunciation("tts-1-hd") == speech.PronunciationSupport()
    asyncio.run(
        provider.synthesize_speech(
            SpeechRequest(
                model="gpt-4o-mini-tts",
                text="Filigran ships.",
                style="Warm.",
                pronunciations=(("Filigran", "ˈfɪlɪɡræn"),),
            )
        )
    )
    assert api.calls[0]["input"] == "Filigran ships."
    assert api.calls[0]["instructions"] == (
        'Warm.\n\nPronounce these words exactly as their IPA reads, every time the text says them: "Filigran" as '
        "/ˈfɪlɪɡræn/."
    )
    refused = _SpeechAPI(refuse_instructions=True)
    result = asyncio.run(
        _openai_shaped(OpenAIProvider, refused).synthesize_speech(
            SpeechRequest(model="gpt-4o-mini-tts", text="Filigran.", pronunciations=(("Filigran", "ˈfɪlɪɡræn"),))
        )
    )
    assert "spoken without the IPA of its terms" in " ".join(result.notes)


def test_a_gateway_passes_another_vendors_voice_through():
    from app.llm.litellm_provider import LiteLLMProvider

    api = _SpeechAPI()
    provider = _openai_shaped(LiteLLMProvider, api)
    assert asyncio.run(provider.list_voices("elevenlabs/eleven_v3")) == []
    assert asyncio.run(provider.list_voices("tts-1"))  # an OpenAI model behind it speaks OpenAI's voices
    asyncio.run(
        provider.synthesize_speech(SpeechRequest(model="elevenlabs/eleven_v3", text="Hi", voice="JBFqnCBsd6RMkjVDRZzb"))
    )
    assert api.calls[0]["voice"] == "JBFqnCBsd6RMkjVDRZzb"
    with pytest.raises(SpeechGenerationError, match="Name the voice"):
        asyncio.run(provider.synthesize_speech(SpeechRequest(model="vertex_ai/chirp", text="Hi")))


def test_azure_speaks_on_its_v1_endpoint():
    from app.llm.azure_openai_provider import AzureOpenAIProvider

    provider = AzureOpenAIProvider.__new__(AzureOpenAIProvider)
    provider._endpoint, provider._key, provider._v1_client = "https://res.openai.azure.com", "k", None
    assert str(provider._speech_client().base_url) == "https://res.openai.azure.com/openai/v1/"
    assert provider.speech_voices_are_fixed


_REAL_CLIENT = httpx.AsyncClient


def _mock_client(handler):
    def _factory(*args, **kwargs):
        kwargs["transport"] = httpx.MockTransport(handler)
        return _REAL_CLIENT(*args, **kwargs)

    return _factory


def test_litellm_lists_the_aliases_its_proxy_declares_speech():
    from app.llm.litellm_provider import LiteLLMProvider

    def _proxy(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v1/model/info":
            return httpx.Response(
                200,
                json={
                    "data": [
                        {"model_name": "narrator", "model_info": {"mode": "audio_speech"}},
                        {"model_name": "veo", "model_info": {"mode": "video_generation"}},
                    ]
                },
            )
        return httpx.Response(200, json={"object": "list", "data": [{"id": "tts-1", "object": "model"}]})

    provider = LiteLLMProvider(api_key="sk||http://proxy:4000")
    provider.client = openai.AsyncOpenAI(
        api_key="sk", base_url=provider.base_url, http_client=httpx.AsyncClient(transport=httpx.MockTransport(_proxy))
    )
    assert asyncio.run(provider.list_speech_models()) == ["narrator", "tts-1"]
    assert asyncio.run(provider.list_video_models()) == ["veo"]


def test_the_gateway_types_its_speech_models():
    from app.llm import gateway_provider

    assert gateway_provider._classify_model("gpt-4o-mini-tts") == "speech"
    assert gateway_provider._classify_model("gpt-4o") == "chat"


# ── ElevenLabs ────────────────────────────────────────────────────────────


def _eleven(monkeypatch, handler, key: str = "sk_test"):
    from app.llm import elevenlabs_provider

    monkeypatch.setattr(elevenlabs_provider.httpx, "AsyncClient", _mock_client(handler))
    return elevenlabs_provider.ElevenLabsProvider(api_key=key)


def test_elevenlabs_lists_the_models_that_speak(monkeypatch):
    def _api(request: httpx.Request) -> httpx.Response:
        assert request.headers["xi-api-key"] == "sk_test"
        assert request.url.path == "/v1/models"
        return httpx.Response(
            200,
            json=[
                {"model_id": "eleven_multilingual_v2", "can_do_text_to_speech": True},
                {"model_id": "eleven_english_sts_v2", "can_do_text_to_speech": False},
            ],
        )

    assert asyncio.run(_eleven(monkeypatch, _api).list_speech_models()) == ["eleven_multilingual_v2"]


def test_elevenlabs_reads_the_whole_voice_library_its_own_voices_first(monkeypatch):
    pages = []

    def _api(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/v2/voices"
        pages.append(dict(request.url.params))
        if "next_page_token" not in request.url.params:
            return httpx.Response(
                200,
                json={
                    "voices": [
                        {"voice_id": "p1", "name": "George", "category": "premade", "labels": {"accent": "british"}},
                    ],
                    "has_more": True,
                    "next_page_token": "t2",
                },
            )
        return httpx.Response(
            200,
            json={"voices": [{"voice_id": "c1", "name": "Brand voice", "category": "cloned"}], "has_more": False},
        )

    provider = _eleven(monkeypatch, _api)
    voices = asyncio.run(provider.list_voices())
    assert [v.id for v in voices] == ["c1", "p1"]
    assert voices[1].description == "british" and voices[0].category == "cloned"
    assert pages[0]["page_size"] == "100" and pages[1]["next_page_token"] == "t2"
    asyncio.run(provider.list_voices())
    assert len(pages) == 2  # reused for a few minutes


def test_elevenlabs_speaks_with_the_neighbouring_narration(monkeypatch):
    seen = {}

    def _api(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v2/voices":
            return httpx.Response(200, json={"voices": [{"voice_id": "c1", "name": "Brand voice"}], "has_more": False})
        seen["path"], seen["params"], seen["body"] = (
            request.url.path,
            dict(request.url.params),
            json.loads(request.content),
        )
        return httpx.Response(200, content=b"MP3")

    provider = _eleven(monkeypatch, _api)
    result = asyncio.run(
        provider.synthesize_speech(
            SpeechRequest(
                model="eleven_multilingual_v2",
                text="Now",
                voice="brand voice",
                style="warm",
                speed=0.8,
                previous_text="x" * 1500,
                next_text="Next",
            )
        )
    )
    assert seen["path"] == "/v1/text-to-speech/c1" and seen["params"]["output_format"] == "mp3_44100_128"
    assert seen["body"]["voice_settings"] == {"speed": 0.8} and len(seen["body"]["previous_text"]) == 1000
    assert seen["body"]["next_text"] == "Next" and seen["body"]["model_id"] == "eleven_multilingual_v2"
    assert result.audio == b"MP3" and result.mime == "audio/mpeg" and result.voice == "c1"
    assert any("no written delivery instruction" in n for n in result.notes)


def test_elevenlabs_refusals_say_why_without_the_key(monkeypatch):
    def _refused(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"detail": {"status": "invalid_api_key", "message": "bad key"}})

    with pytest.raises(SpeechGenerationError, match="refused the API key") as exc:
        asyncio.run(_eleven(monkeypatch, _refused, key="sk_secret").list_speech_models())
    assert "sk_secret" not in str(exc.value)

    def _quota(request: httpx.Request) -> httpx.Response:
        return httpx.Response(429, json={"detail": {"status": "quota_exceeded", "message": "Quota exceeded."}})

    with pytest.raises(SpeechGenerationError, match=r"HTTP 429\): Quota exceeded"):
        asyncio.run(_eleven(monkeypatch, _quota).list_speech_models())


def test_elevenlabs_takes_a_regional_endpoint_and_never_chats():
    from app.llm.elevenlabs_provider import ElevenLabsProvider

    assert ElevenLabsProvider("sk||https://api.eu.residency.elevenlabs.io/v1/").base_url == (
        "https://api.eu.residency.elevenlabs.io"
    )
    assert ElevenLabsProvider("sk").base_url == "https://api.elevenlabs.io"
    provider = ElevenLabsProvider("sk")
    assert asyncio.run(provider.list_models()) == []
    with pytest.raises(NotImplementedError):
        asyncio.run(provider.chat([]))


# ── Gemini ────────────────────────────────────────────────────────────────


def _gemini(monkeypatch, handler):
    from app.llm.gemini_provider import GeminiProvider

    monkeypatch.setattr(httpx, "AsyncClient", _mock_client(handler))
    provider = GeminiProvider.__new__(GeminiProvider)
    provider._api_key = "g-key"
    return provider


def test_gemini_offers_its_studio_voices_first_then_the_project_voices(monkeypatch):
    def _api(request: httpx.Request) -> httpx.Response:
        assert request.headers["x-goog-api-key"] == "g-key" and request.url.path == "/v1beta/voices"
        return httpx.Response(
            200,
            json={
                "voices": [
                    {"id": "voice_abc", "display_name": "Our narrator", "type": "prompted"},
                    {"id": "Kore", "display_name": "Kore", "type": "prebuilt"},
                    {"id": "lib-1", "display_name": "Mira", "type": "prebuilt", "accent": "Irish"},
                ]
            },
        )

    provider = _gemini(monkeypatch, _api)
    voices = asyncio.run(provider.list_voices("gemini-3.8-flash-tts"))
    ids = [v.id for v in voices]
    assert ids[0] == "Kore" and len([i for i in ids if i == "Kore"]) == 1
    assert ids.index("voice_abc") == 30 and ids[-1] == "lib-1"
    assert voices[-1].description == "Irish"


def test_gemini_reads_its_voice_library_once_for_a_whole_narration(monkeypatch):
    reads = []

    def _api(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v1beta/voices":
            reads.append(1)
            return httpx.Response(200, json={"voices": [{"id": "lib-1", "display_name": "Mira", "type": "prebuilt"}]})
        audio = base64.b64encode(b"RIFF").decode()
        return httpx.Response(
            200, json={"steps": [{"type": "model_output", "content": [{"type": "audio", "data": audio}]}]}
        )

    provider = _gemini(monkeypatch, _api)
    for voice in ("Kore", None, "Mira", "Mira"):
        asyncio.run(provider.synthesize_speech(SpeechRequest(model="gemini-3.8-flash-tts", text="Hi", voice=voice)))
    assert len(reads) == 1  # a studio voice needs no library, the library is reused


def test_gemini_speaks_through_the_interactions_api(monkeypatch):
    seen = {}

    def _api(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v1beta/voices":
            return httpx.Response(200, json={"voices": []})
        seen["body"] = json.loads(request.content)
        audio = base64.b64encode(b"RIFF....WAVE").decode()
        return httpx.Response(
            200, json={"steps": [{"type": "model_output", "content": [{"type": "audio", "data": audio}]}]}
        )

    result = asyncio.run(
        _gemini(monkeypatch, _api).synthesize_speech(
            SpeechRequest(model="gemini-3.8-flash-tts", text="Hello", voice="puck", style="cheerful", speed=0.8)
        )
    )
    body = seen["body"]
    assert body["generation_config"]["speech_config"] == [{"voice": "Puck"}]
    text = body["input"][0]["content"][0]
    assert text["text"] == "Hello"
    assert text["annotations"] == [{"type": "speech_metadata", "style": "cheerful, speaking slowly"}]
    assert result.audio == b"RIFF....WAVE" and result.mime == "audio/wav"


def test_a_designed_gemini_voice_is_used_as_given(monkeypatch):
    seen = {}

    def _api(request: httpx.Request) -> httpx.Response:
        seen["body"] = json.loads(request.content)
        audio = base64.b64encode(b"\x00\x00").decode()
        return httpx.Response(
            200,
            json={
                "steps": [
                    {"type": "model_output", "content": [{"type": "audio", "data": audio, "mime_type": "audio/l16"}]}
                ]
            },
        )

    result = asyncio.run(
        _gemini(monkeypatch, _api).synthesize_speech(
            SpeechRequest(model="gemini-3.8-flash-tts", text="Hi", voice="voice_xyz")
        )
    )
    assert seen["body"]["generation_config"]["speech_config"] == [{"voice": "voice_xyz"}]
    assert result.audio.startswith(b"RIFF")  # raw PCM wrapped


def test_a_refused_gemini_call_names_its_reason(monkeypatch):
    def _api(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v1beta/voices":
            return httpx.Response(500)
        return httpx.Response(400, json={"error": {"message": "Voice not found"}})

    with pytest.raises(SpeechGenerationError, match=r"HTTP 400\): Voice not found"):
        asyncio.run(
            _gemini(monkeypatch, _api).synthesize_speech(SpeechRequest(model="gemini-3.8-flash-tts", text="Hi"))
        )


# ── Which provider, model and voice speak ─────────────────────────────────


def _config(provider: str) -> SimpleNamespace:
    return SimpleNamespace(id=uuid.uuid4(), provider=provider, display_name=None)


class _Db:
    def __init__(self, configs):
        self.configs = configs

    async def execute(self, _statement):
        return SimpleNamespace(scalars=lambda: SimpleNamespace(all=lambda: list(self.configs)))


@pytest.fixture
def _speech(monkeypatch):
    state = {"ps": SimpleNamespace(speech_provider_id=None, speech_model=None, speech_voice=None), "withheld": set()}

    async def _ps(_db):
        return state["ps"]

    async def _cached(config):
        return f"provider:{config.provider}"

    async def _routing(_db, _user_id, configs):
        usable = [c for c in configs if c.id not in state["withheld"]]
        return SimpleNamespace(team_first=usable, excluded_ids=frozenset())

    monkeypatch.setattr(router, "_get_platform_settings", _ps)
    monkeypatch.setattr(router, "_cached_provider", _cached)
    monkeypatch.setattr(router, "provider_routing", _routing)
    return state


def test_the_admins_speech_model_and_voice_are_used(_speech):
    openai_config, eleven = _config("openai"), _config("elevenlabs")
    _speech["ps"] = SimpleNamespace(speech_provider_id=eleven.id, speech_model="eleven_v3", speech_voice="c1")
    target = asyncio.run(router.get_speech_provider(_Db([openai_config, eleven])))
    assert (target.provider, target.model, target.config, target.voice) == (
        "provider:elevenlabs",
        "eleven_v3",
        eleven,
        "c1",
    )
    # Another model of that provider does not carry the admin's voice.
    other = asyncio.run(router.get_speech_provider(_Db([openai_config, eleven]), model="eleven_flash_v2_5"))
    assert other.model == "eleven_flash_v2_5" and other.voice is None


def test_without_a_choice_a_voice_generator_comes_before_a_chat_provider(_speech):
    openai_config, eleven = _config("openai"), _config("elevenlabs")
    target = asyncio.run(router.get_speech_provider(_Db([openai_config, eleven])))
    assert target.config is eleven and target.model == router.SPEECH_DEFAULTS["elevenlabs"]
    target = asyncio.run(router.get_speech_provider(_Db([_config("anthropic"), openai_config])))
    assert target.config is openai_config and target.model == "gpt-4o-mini-tts"


def test_an_admin_choice_the_person_may_not_use_falls_back(_speech):
    reserved, openai_config = _config("elevenlabs"), _config("openai")
    _speech["ps"] = SimpleNamespace(speech_provider_id=reserved.id, speech_model="eleven_v3", speech_voice="c1")
    _speech["withheld"] = {reserved.id}
    target = asyncio.run(router.get_speech_provider(_Db([reserved, openai_config])))
    assert target.config is openai_config and target.voice is None
    with pytest.raises(router.ProviderNotUsableError, match="reserved for other groups"):
        asyncio.run(router.get_speech_provider(_Db([reserved, openai_config]), provider_id=reserved.id))


def test_the_callers_choice_and_the_missing_ones(_speech):
    gateway = _config("litellm")
    target = asyncio.run(router.get_speech_provider(_Db([gateway]), provider_id=gateway.id, model="narrator"))
    assert target.model == "narrator" and target.config is gateway
    with pytest.raises(ValueError, match="Name the speech model"):
        asyncio.run(router.get_speech_provider(_Db([gateway]), provider_id=gateway.id))
    with pytest.raises(ValueError, match="No speech provider with this id"):
        asyncio.run(router.get_speech_provider(_Db([gateway]), provider_id=uuid.uuid4()))
    with pytest.raises(ValueError, match="Speech generation"):
        asyncio.run(router.get_speech_provider(_Db([_config("anthropic")])))


class _Gateway:
    """The AI Gateway marking *default* as its speech model (XTM-One-Platform/ai-gateway#376)."""

    def __init__(self, default: str | None, voices: tuple[str, ...] = ("marin", "cedar")):
        self.default, self.voices = default, voices

    async def marked_default_model(self, kind: str) -> str | None:
        return self.default if kind == "speech" else None

    async def list_voices(self, model: str | None = None) -> list[Voice]:
        return [Voice(v, v.title()) for v in self.voices]


@pytest.fixture
def _gateway_speech(_speech, monkeypatch):
    _speech["gateway"] = _Gateway("gpt-4o-mini-tts")

    async def _cached(config):
        return _speech["gateway"] if config.provider == "filigran_gateway" else f"provider:{config.provider}"

    monkeypatch.setattr(router, "_cached_provider", _cached)
    return _speech


def test_the_gateway_default_speaks_when_nothing_else_does(_gateway_speech):
    gateway = _config("filigran_gateway")
    target = asyncio.run(router.get_speech_provider(_Db([_config("anthropic"), gateway])))
    assert (target.provider, target.model, target.config, target.voice) == (
        _gateway_speech["gateway"], "gpt-4o-mini-tts", gateway, None,
    )
    # A caller naming the gateway without a model gets its default too.
    named = asyncio.run(router.get_speech_provider(_Db([gateway]), provider_id=gateway.id))
    assert named.model == "gpt-4o-mini-tts"


def test_a_built_in_speech_default_and_the_admins_choice_come_first(_gateway_speech):
    gateway, openai_config = _config("filigran_gateway"), _config("openai")
    target = asyncio.run(router.get_speech_provider(_Db([gateway, openai_config])))
    assert target.config is openai_config
    _gateway_speech["ps"] = SimpleNamespace(speech_provider_id=gateway.id, speech_model="tts-1", speech_voice=None)
    target = asyncio.run(router.get_speech_provider(_Db([gateway])))
    assert target.model == "tts-1"


def test_the_platform_voice_on_the_gateway_default_or_its_first_voice(_gateway_speech):
    gateway = _config("filigran_gateway")
    _gateway_speech["ps"] = SimpleNamespace(speech_provider_id=None, speech_model=None, speech_voice="Cedar")
    assert asyncio.run(router.get_speech_provider(_Db([gateway]))).voice == "cedar"
    # A voice the default model lacks is its first voice, never an error.
    _gateway_speech["ps"].speech_voice = "rachel"
    assert asyncio.run(router.get_speech_provider(_Db([gateway]))).voice == "marin"
    # The same when the caller names the gateway.
    named = asyncio.run(router.get_speech_provider(_Db([gateway]), provider_id=gateway.id))
    assert (named.model, named.voice) == ("gpt-4o-mini-tts", "marin")


def test_the_gateway_the_admin_chose_without_a_model_speaks_first(_gateway_speech, monkeypatch):
    first, chosen = _config("filigran_gateway"), _config("filigran_gateway")
    gateways = {first.id: _Gateway("first-tts"), chosen.id: _Gateway("chosen-tts")}

    async def _cached(config):
        return gateways[config.id]

    monkeypatch.setattr(router, "_cached_provider", _cached)
    _gateway_speech["ps"] = SimpleNamespace(speech_provider_id=chosen.id, speech_model=None, speech_voice=None)
    target = asyncio.run(router.get_speech_provider(_Db([first, chosen])))
    assert (target.config, target.model) == (chosen, "chosen-tts")


def test_a_gateway_marking_no_speech_default_is_todays_refusal(_gateway_speech):
    _gateway_speech["gateway"] = _Gateway(None)
    gateway = _config("filigran_gateway")
    with pytest.raises(ValueError, match="Speech generation"):
        asyncio.run(router.get_speech_provider(_Db([gateway])))
    with pytest.raises(ValueError, match="Name the speech model"):
        asyncio.run(router.get_speech_provider(_Db([gateway]), provider_id=gateway.id))


# ── The setting ───────────────────────────────────────────────────────────


def test_the_setting_is_a_provider_reference_like_the_video_one():
    from app.api.v1 import settings as settings_api
    from app.api.v1.schemas.settings import GeneralAssistantConfigRequest, GeneralAssistantConfigResponse
    from app.models.platform_settings import PlatformSettings

    for column in ("speech_provider", "speech_provider_id", "speech_model", "speech_voice"):
        assert hasattr(PlatformSettings, column)
        assert column in GeneralAssistantConfigResponse.model_fields
        assert column in GeneralAssistantConfigRequest.model_fields
        assert column in settings_api._GA_PROVIDER_REFERENCE_FIELDS
    assert ("speech_provider", "speech_provider_id") in settings_api._GA_ALIGNED_PROVIDER_PAIRS
    assert any(field == "speech_provider_id" for field, _scope, _label in settings_api._GA_PROVIDER_ID_FIELDS)
    assert settings_api.PROVIDER_MAP["elevenlabs"] is router.PROVIDER_MAP["elevenlabs"]


def _validate(monkeypatch, provider_cls, capability="reasoning"):
    from app.api.v1 import settings as settings_api
    from app.api.v1.schemas.settings import ValidateProviderRequest

    monkeypatch.setitem(settings_api.PROVIDER_MAP, "elevenlabs", provider_cls)
    monkeypatch.setattr(settings_api, "_ensure_new_provider_allowed", lambda _p: None)

    async def _resolve(value):
        return value

    monkeypatch.setattr(settings_api, "resolve_secret_text", _resolve)
    user = SimpleNamespace(id=uuid.uuid4(), email="admin@example.com")
    body = ValidateProviderRequest(provider="elevenlabs", api_key="sk", capability=capability)
    return asyncio.run(settings_api.validate_provider_with_key(body, user=user, _ai_gate=None))


def test_a_voice_generator_is_validated_for_speech_whatever_flow_adds_it(monkeypatch):
    class _Speaks:
        only_capability = "speech"

        def __init__(self, api_key):
            pass

        async def list_speech_models(self):
            return ["eleven_v3"]

    response = _validate(monkeypatch, _Speaks)
    assert response.capability == "speech" and response.connection_ok and response.models == ["eleven_v3"]


def test_a_refused_voice_generator_key_is_said_as_such(monkeypatch):
    class _Refuses:
        only_capability = "speech"

        def __init__(self, api_key):
            pass

        async def list_speech_models(self):
            raise SpeechGenerationError("ElevenLabs refused the API key.")

    response = _validate(monkeypatch, _Refuses)
    assert not response.connection_ok and response.message == "ElevenLabs refused the API key."


# ── The consumers ─────────────────────────────────────────────────────────


class _Speaker:
    def __init__(self):
        self.requests: list[SpeechRequest] = []

    async def synthesize_speech(self, request):
        self.requests.append(request)
        return speech.SpeechResult(b"MP3", "audio/mpeg", request.voice or "first", ["a note"])

    async def list_voices(self, model=None):
        return [Voice("first", "First")]


def _target(voice: str | None = None):
    return router.SpeechTarget(_Speaker(), "eleven_v3", _config("elevenlabs"), voice)


def test_read_aloud_speaks_with_the_platforms_voice(monkeypatch):
    from app.api.v1 import chat

    target = _target(voice="c1")

    async def _get(_db, **_kwargs):
        return target

    monkeypatch.setattr(router, "get_speech_provider", _get)
    user = SimpleNamespace(id=uuid.uuid4(), email="u@example.com")
    response = asyncio.run(chat.text_to_speech(chat.TTSRequest(text="Hello"), user=user, db=None, _ai_gate=None))
    assert response.body == b"MP3" and response.media_type == "audio/mpeg"
    assert target.provider.requests[0].voice == "c1" and target.provider.requests[0].audio_format == "mp3"


def test_read_aloud_without_a_speech_model_says_where_to_set_one(monkeypatch):
    from fastapi import HTTPException

    from app.api.v1 import chat

    async def _none(_db, **_kwargs):
        raise ValueError("No speech model is available to you.")

    monkeypatch.setattr(router, "get_speech_provider", _none)
    user = SimpleNamespace(id=uuid.uuid4(), email="u@example.com")
    with pytest.raises(HTTPException) as exc:
        asyncio.run(chat.text_to_speech(chat.TTSRequest(text="Hello"), user=user, db=None, _ai_gate=None))
    assert exc.value.status_code == 400 and "No speech model" in exc.value.detail


def test_a_voiceover_names_its_provider_model_and_voice():
    from app.services.video_studio.spec import Voiceover

    provider_id = str(uuid.uuid4())
    vo = Voiceover(provider_id=provider_id, model="eleven_v3", voice="JBFqnCBsd6RMkjVDRZzb")
    assert vo.provider_id == provider_id and vo.provider == "auto"
    assert Voiceover(provider="gemini").provider == "gemini"  # an older spec stays valid
    with pytest.raises(ValueError):
        Voiceover(provider_id="not an id")


def test_the_voiceover_cache_keys_on_everything_that_changes_the_sound(monkeypatch, tmp_path):
    from app.services import file_storage as storage
    from app.services.video_studio import encoder, voice

    class _Storage:
        def __init__(self):
            self.objects: dict[str, bytes] = {}

        async def download_to_path(self, name, path, *, missing_ok=False):
            if name not in self.objects:
                raise storage.FileNotFoundInStorageError(name)
            Path(path).write_bytes(self.objects[name])

        async def upload_path(self, path, name, _type):
            self.objects[name] = Path(path).read_bytes()

    store = _Storage()
    monkeypatch.setattr(storage, "file_storage", store)

    async def _condition(src, dst):
        Path(dst).write_bytes(b"W" * 100 + Path(src).read_bytes())

    monkeypatch.setattr(encoder, "condition_voice", _condition)
    target = _target()
    out = str(tmp_path / "v.wav")

    async def _speak(**kwargs):
        return await voice.synthesize(target, text="Hi", voice=None, style=None, speed=1.0, out_path=out, **kwargs)

    used, notes = asyncio.run(_speak(previous_text="Before"))
    assert used == "first" and notes == ["a note"]
    assert target.provider.requests[0].previous_text == "Before" and target.provider.requests[0].audio_format == "wav"
    asyncio.run(_speak(previous_text="Before"))
    assert len(target.provider.requests) == 1  # the same narration is a cache hit
    asyncio.run(_speak(previous_text="Other"))
    assert len(target.provider.requests) == 2  # its neighbours change how it sounds


def test_a_voiceover_naming_a_provider_type_speaks_on_its_first_usable_config(monkeypatch):
    from app.services.video_studio import voice
    from app.services.video_studio.spec import Voiceover

    gemini = _config("gemini")
    seen = {}

    async def _routing(_db, _user_id, configs):
        return SimpleNamespace(team_first=list(configs))

    async def _get(_db, **kwargs):
        seen.update(kwargs)
        return "target"

    from app.llm import provider_access

    monkeypatch.setattr(provider_access, "provider_routing", _routing)
    monkeypatch.setattr(router, "get_speech_provider", _get)
    assert (
        asyncio.run(voice.resolve(_Db([_config("openai"), gemini]), uuid.uuid4(), Voiceover(provider="gemini")))
        == "target"
    )
    assert seen["provider_id"] == gemini.id

    async def _refuse(_db, **_kwargs):
        raise ValueError("No speech model is available to you.")

    monkeypatch.setattr(router, "get_speech_provider", _refuse)
    with pytest.raises(voice.VoiceUnavailableError, match="shown as captions"):
        asyncio.run(voice.resolve(_Db([]), uuid.uuid4(), Voiceover()))
