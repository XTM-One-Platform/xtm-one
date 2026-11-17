"""Google Vertex AI makes AI clips with Veo, from a service account key. Synthetic keys and API answers.

* the credential is a service account key (the JSON Google Cloud downloads):
  its project, an optional region (``us-central1`` otherwise), its token
  endpoint pinned to Google's; a key that is not one, or a region that is not
  one, is refused saying what is wrong and never echoing a value of the key;
* the provider serves video alone: a google-genai client built for Vertex AI,
  Veo as its built-in model, no chat, speech, music, image or embedding model;
* its Veo models are read from Vertex AI (``publishers/google/models/`` ids),
  never a list of its own; a refusal says what the project or the service
  account misses (the Vertex AI User role, the API enabled);
* a clip is the Gemini provider's Veo operation, asked silent when the shot
  is (``generate_audio``), its video read from the answer's bytes.
"""

from __future__ import annotations

import asyncio
import json
import re
from types import SimpleNamespace

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from google.genai import errors as genai_errors

from app.llm import router
from app.llm.gemini_provider import GeminiProvider
from app.llm.vertex_ai_provider import (
    DEFAULT_LOCATION,
    GOOGLE_TOKEN_URI,
    VertexAIProvider,
    parse_vertex_credential,
)
from app.llm.video import VideoGenerationError, VideoRequest

_PRIVATE_KEY = (
    rsa.generate_private_key(public_exponent=65537, key_size=2048)
    .private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption())
    .decode()
)
SA = "veo@acme-video.iam.gserviceaccount.com"


def _key(**changes) -> dict:
    key = {
        "type": "service_account",
        "project_id": "acme-video",
        "private_key_id": "k1",
        "private_key": _PRIVATE_KEY,
        "client_email": SA,
        "token_uri": GOOGLE_TOKEN_URI,
        "universe_domain": "googleapis.com",
    }
    key.update(changes)
    return {name: value for name, value in key.items() if value is not None}


def _provider(**changes) -> VertexAIProvider:
    return VertexAIProvider(api_key=json.dumps(_key(**changes)))


# ── The credential ───────────────────────────────────────────────────────


def test_a_service_account_key_names_its_project_and_runs_in_the_veo_region():
    credential = parse_vertex_credential(json.dumps(_key()))
    assert (credential.project, credential.client_email, credential.location) == ("acme-video", SA, DEFAULT_LOCATION)
    assert DEFAULT_LOCATION == "us-central1"
    assert credential.info["token_uri"] == GOOGLE_TOKEN_URI


def test_a_key_from_an_env_file_or_a_secret_store_is_read_too():
    import base64

    expanded = json.dumps(_key()).replace("\\n", "\n")
    assert parse_vertex_credential(expanded).info["private_key"] == _PRIVATE_KEY
    encoded = base64.b64encode(json.dumps(_key(location="europe-west4")).encode()).decode()
    assert parse_vertex_credential(encoded).location == "europe-west4"


def test_a_region_beside_the_key_is_used_and_the_token_endpoint_is_always_googles():
    credential = parse_vertex_credential(json.dumps(_key(location=" Europe-West4 ", token_uri="https://evil.example/token")))
    assert credential.location == "europe-west4"
    assert "location" not in credential.info
    assert credential.info["token_uri"] == GOOGLE_TOKEN_URI


@pytest.mark.parametrize(
    ("text", "said"),
    [
        pytest.param("not json", "not a service account key: paste the JSON key", id="not-json"),
        pytest.param(json.dumps(_key(type="authorized_user")), 'its "type" is not "service_account"', id="not-a-service-account"),
        pytest.param(json.dumps(_key(project_id=None, client_email="")), "has no project_id, client_email", id="missing-fields"),
        pytest.param(json.dumps(_key(universe_domain="example.com")), "not for Google Cloud", id="other-universe"),
        pytest.param(json.dumps(_key(location="evil.example/x")), "is not a Vertex AI region", id="host-in-location"),
        pytest.param(json.dumps(_key(location="us-central1.evil")), "is not a Vertex AI region", id="suffix-in-location"),
    ],
)
def test_what_is_not_a_service_account_key_says_why_and_never_shows_the_key(text, said):
    with pytest.raises(VideoGenerationError) as refused:
        parse_vertex_credential(text)
    assert said in str(refused.value)
    assert "PRIVATE KEY" not in str(refused.value)


def test_a_private_key_that_cannot_be_read_is_said_so():
    with pytest.raises(VideoGenerationError, match="private key could not be read"):
        _provider(private_key="not a private key")


# ── What the provider is ─────────────────────────────────────────────────


def test_the_provider_is_a_vertex_ai_client_of_the_keys_project_and_region():
    provider = _provider(location="us-central1")
    assert provider.client.vertexai is True
    assert (provider.client._api_client.project, provider.client._api_client.location) == ("acme-video", "us-central1")
    assert (provider.project, provider.service_account) == ("acme-video", SA)


def test_it_serves_video_alone_with_veo_built_in():
    assert router.PROVIDER_MAP["vertex_ai"] is VertexAIProvider
    assert router.provider_capabilities("vertex_ai") == ["video"]
    assert "vertex_ai" in router.CHATLESS_PROVIDERS
    assert router.VIDEO_DEFAULTS["vertex_ai"] == "veo-3.1-generate-001"
    assert "vertex_ai" not in router.SPEECH_DEFAULTS and "vertex_ai" not in router.MUSIC_DEFAULTS
    provider = _provider()
    for listing in ("list_models", "list_embedding_models", "list_image_models", "list_speech_models", "list_music_models"):
        assert asyncio.run(getattr(provider, listing)()) == [], listing
    assert VertexAIProvider.default_model is None
    assert provider._interactions_video("gemini-omni-1.1-flash") is False
    from app.llm import env_capability_defaults

    served = {role.key for role in env_capability_defaults.ROLES if role.serves("vertex_ai")}
    assert served == {"video"}


# ── Its models ───────────────────────────────────────────────────────────


class _Pager:
    def __init__(self, names):
        self.names = list(names)

    def __aiter__(self):
        async def walk():
            for name in self.names:
                yield SimpleNamespace(name=name)

        return walk()


def _listing(provider, answer):
    async def listed(*, config):
        assert config == {"query_base": True}
        if isinstance(answer, BaseException):
            raise answer
        return _Pager(answer)

    provider.client = SimpleNamespace(aio=SimpleNamespace(models=SimpleNamespace(list=listed)))
    return provider


def test_its_veo_models_are_read_from_vertex_ai():
    provider = _listing(
        _provider(),
        [
            "publishers/google/models/gemini-3.5-flash",
            "publishers/google/models/veo-3.1-generate-001",
            "publishers/google/models/veo-3.1-fast-generate-001",
            "publishers/google/models/gemini-omni-1.1-flash",
            "publishers/google/models/imagen-4.0-generate-001",
        ],
    )
    assert asyncio.run(provider.list_video_models()) == ["veo-3.1-generate-001", "veo-3.1-fast-generate-001"]


def _refused(code: int, reason: str | None = None, message: str = "refused") -> genai_errors.ClientError:
    details = [{"@type": "type.googleapis.com/google.rpc.ErrorInfo", "reason": reason}] if reason else []
    error = {"code": code, "message": message, "status": "PERMISSION_DENIED", "details": details}
    return genai_errors.ClientError(code, {"error": error})


@pytest.mark.parametrize(
    ("answer", "said"),
    [
        pytest.param(
            _refused(403, "IAM_PERMISSION_DENIED"),
            f"{SA} may not list the Veo models on Vertex AI: grant it the Vertex AI User role "
            "(roles/aiplatform.user) on project acme-video.",
            id="role",
        ),
        pytest.param(
            _refused(403, "SERVICE_DISABLED"),
            "The Vertex AI API is not enabled on project acme-video: enable aiplatform.googleapis.com",
            id="api",
        ),
        pytest.param(_refused(401), f"Google refused the service account key of {SA}", id="key"),
        pytest.param(genai_errors.ServerError(503, {"error": {"code": 503, "message": "down"}}), "unavailable (HTTP 503)", id="down"),
        pytest.param(TimeoutError("read timed out"), "could not be reached", id="unreached"),
    ],
)
def test_a_listing_that_fails_says_why_and_offers_no_list_of_its_own(answer, said):
    with pytest.raises(VideoGenerationError) as refused:
        asyncio.run(_listing(_provider(), answer).list_video_models())
    assert said in str(refused.value)


def test_a_key_google_refuses_to_exchange_is_said_so():
    from google.auth.exceptions import RefreshError

    with pytest.raises(VideoGenerationError, match=f"Google refused the service account key of {SA}"):
        asyncio.run(_listing(_provider(), RefreshError("invalid_grant: Invalid JWT Signature.")).list_video_models())


def test_the_settings_dialog_says_what_is_wrong_with_the_key(monkeypatch):
    from tests.test_settings_provider_validation import _validate_route

    response = _validate_route(monkeypatch, VertexAIProvider, "vertex_ai").post(
        "/api/v1/settings/llm-providers/validate",
        json={"provider": "vertex_ai", "api_key": "AIza-not-a-service-account-key", "capability": "video"},
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["connection_ok"] is False and body["models"] == []
    assert body["message"].startswith("The Vertex AI credential is not a service account key")


# ── A clip ───────────────────────────────────────────────────────────────


class _Veo:
    """Veo's operation, scripted: it records the config asked, and answers with the bytes of a video."""

    def __init__(self, video_bytes: bytes | None = b"MP4", refuse: BaseException | None = None):
        self.configs = []
        self.video_bytes, self.refuse = video_bytes, refuse

    async def generate_videos(self, *, model, prompt, image, config):
        if self.refuse is not None:
            raise self.refuse
        self.configs.append(config)
        return SimpleNamespace(name="projects/acme-video/locations/us-central1/operations/op-1", done=False)

    async def get(self, operation):
        video = SimpleNamespace(video_bytes=self.video_bytes, uri=None)
        response = SimpleNamespace(generated_videos=[SimpleNamespace(video=video)])
        return SimpleNamespace(name=operation.name, done=True, error=None, response=response)


def _making(provider: GeminiProvider, veo: _Veo, monkeypatch) -> GeminiProvider:
    from app.llm import video

    monkeypatch.setattr(video, "POLL_SECONDS", 0)
    provider.client = SimpleNamespace(
        aio=SimpleNamespace(models=SimpleNamespace(generate_videos=veo.generate_videos), operations=SimpleNamespace(get=veo.get))
    )
    return provider


def _clip(**asked) -> VideoRequest:
    return VideoRequest(model="veo-3.1-generate-001", prompt="A paper boat on a pond", duration_seconds=4, resolution="720p", **asked)


@pytest.mark.parametrize(("sound", "audio"), [(False, False), (True, True), (None, True)])
def test_a_clip_is_asked_with_its_sound_and_read_from_the_answers_bytes(monkeypatch, sound, audio):
    veo = _Veo()
    result = asyncio.run(_making(_provider(), veo, monkeypatch).generate_video(_clip(sound=sound)))
    assert result.data == b"MP4" and result.job_id.endswith("operations/op-1")
    assert veo.configs[0].generate_audio is audio
    assert (veo.configs[0].duration_seconds, veo.configs[0].resolution) == (4, "720p")


def test_the_gemini_api_is_never_sent_the_audio_field(monkeypatch):
    veo = _Veo()
    asyncio.run(_making(GeminiProvider(api_key="g-key"), veo, monkeypatch).generate_video(_clip(sound=False)))
    assert veo.configs[0].generate_audio is None


def test_a_clip_the_service_account_may_not_make_says_which_role_it_needs(monkeypatch):
    veo = _Veo(refuse=_refused(403, "IAM_PERMISSION_DENIED"))
    with pytest.raises(VideoGenerationError) as refused:
        asyncio.run(_making(_provider(), veo, monkeypatch).generate_video(_clip()))
    assert str(refused.value) == (
        f"{SA} may not generate with veo-3.1-generate-001 on Vertex AI: grant it the Vertex AI User role "
        "(roles/aiplatform.user) on project acme-video."
    )


def test_an_answer_without_the_videos_bytes_says_so(monkeypatch):
    with pytest.raises(VideoGenerationError, match=re.escape("answered veo-3.1-generate-001's video with no bytes")):
        asyncio.run(_making(_provider(), _Veo(video_bytes=None), monkeypatch).generate_video(_clip()))
