"""Each capability's model picker offers what its provider serves now. Synthetic listings and providers.

* a Gemini listing that answers is the truth for video, speech, music, image
  and embedding: with none of a capability's models in it, none is offered;
  the built-in ones stand in only for a listing that cannot be read, and a
  rejected key is raised;
* Gemini's video probe reads the listing: a model it shut down keeps a record
  ``models.get`` answers, but is no longer listed, so it is not served;
* Settings > Video generation lists what the Video Studio lists: a model its
  provider answered HTTP 404 for is left out.
"""

from __future__ import annotations

import asyncio
import uuid
from types import SimpleNamespace

import pytest
from google.genai import errors as genai_errors

from app.api.v1 import settings as settings_api
from app.llm import provider_access, router, video_served
from app.llm.gemini_provider import GeminiProvider
from app.llm.video import VideoAPIError
from app.services import ai_state

#: What a Gemini API key lists once the Veo previews are shut down: no ``veo-*`` id, Gemini Omni for video.
_LISTED = [
    "gemini-3.5-flash",
    "gemini-omni-1.1-flash",
    "gemini-3.8-flash-tts",
    "lyria-3.5",
    "lyria-realtime-exp",
    "gemini-embedding-001",
    "gemini-3.1-flash-image",
]

_LISTINGS = {
    "list_video_models": GeminiProvider._FALLBACK_VIDEO_MODELS,
    "list_speech_models": GeminiProvider._FALLBACK_SPEECH_MODELS,
    "list_music_models": GeminiProvider._FALLBACK_MUSIC_MODELS,
    "list_image_models": ("imagen-3.0-generate-002",),
    "list_embedding_models": ("text-embedding-004",),
}


def _gemini(listing) -> GeminiProvider:
    """A Gemini provider whose base-model listing answers *listing*, or raises it."""
    provider = GeminiProvider(api_key="test-key")

    async def ids():
        if isinstance(listing, BaseException):
            raise listing
        return list(listing)

    provider._list_all_model_ids = ids
    return provider


def _client_error(code: int) -> genai_errors.ClientError:
    return genai_errors.ClientError(code, {"error": {"code": code, "message": f"HTTP {code}", "status": "ERROR"}})


def test_a_gemini_key_offers_the_video_models_it_lists_and_no_other():
    assert asyncio.run(_gemini(_LISTED).list_video_models()) == ["gemini-omni-1.1-flash"]
    assert asyncio.run(_gemini([m for m in _LISTED if "omni" not in m]).list_video_models()) == []
    assert asyncio.run(_gemini([*_LISTED, "veo-3.1-generate-001"]).list_video_models()) == [
        "veo-3.1-generate-001",
        "gemini-omni-1.1-flash",
    ]


def test_each_capability_keeps_its_own_listed_models():
    gemini = _gemini(_LISTED)
    assert asyncio.run(gemini.list_speech_models()) == ["gemini-3.8-flash-tts"]
    assert asyncio.run(gemini.list_music_models()) == ["lyria-3.5"]
    assert asyncio.run(gemini.list_embedding_models()) == ["gemini-embedding-001"]
    assert asyncio.run(gemini.list_image_models()) == ["gemini-3.1-flash-image"]


@pytest.mark.parametrize("method", sorted(_LISTINGS))
def test_a_listing_that_answers_is_never_replaced_by_the_built_in_models(method):
    assert asyncio.run(getattr(_gemini(["gemini-3.5-flash"]), method)()) == []


@pytest.mark.parametrize("method", sorted(_LISTINGS))
def test_the_built_in_models_stand_in_only_for_a_listing_that_cannot_be_read(method):
    built_in = list(_LISTINGS[method])
    assert asyncio.run(getattr(_gemini(RuntimeError("network down")), method)()) == built_in
    assert asyncio.run(getattr(_gemini(_client_error(429)), method)()) == built_in
    for code in (401, 403):
        with pytest.raises(genai_errors.ClientError):
            asyncio.run(getattr(_gemini(_client_error(code)), method)())


class _Redis:
    """The few Redis commands the not-served record uses, in memory."""

    def __init__(self):
        self.data: dict[str, str] = {}

    async def mget(self, keys):
        return [self.data.get(key) for key in keys]

    async def set(self, key, value, ex=None):
        self.data[key] = value
        return True

    async def delete(self, *keys):
        return sum(self.data.pop(key, None) is not None for key in keys)


@pytest.fixture
def redis(monkeypatch):
    store = _Redis()

    async def client():
        return store

    monkeypatch.setattr(video_served, "redis_client", client)
    return store


def test_a_model_gemini_shut_down_is_not_served_though_its_record_answers(redis):
    gemini = _gemini(["veo-3.1-generate-001"])
    read: list[str] = []

    async def get(*, model):
        read.append(model)
        return SimpleNamespace(name=f"models/{model}")

    gemini.client = SimpleNamespace(aio=SimpleNamespace(models=SimpleNamespace(get=get)))
    config = SimpleNamespace(id=uuid.uuid4(), provider="gemini", display_name="Google Gemini")
    reasons = asyncio.run(video_served.problems(gemini, config, ["veo-3.1-generate-preview", "veo-3.1-generate-001"]))
    assert reasons == {
        "veo-3.1-generate-preview": "veo-3.1-generate-preview is not served by Google Gemini (HTTP 404: "
        "veo-3.1-generate-preview is not among the models Gemini lists for this key)"
    }
    assert read == []
    # Recorded for every worker and the API, as a 404 to a generation is.
    assert any(key.endswith(":veo-3.1-generate-preview") for key in redis.data)


def test_with_no_listing_the_probe_reads_the_models_record(redis):
    gemini = _gemini(RuntimeError("network down"))

    async def get(*, model):
        if model.startswith("veo-2"):
            raise _client_error(404)
        return SimpleNamespace(name=f"models/{model}")

    gemini.client = SimpleNamespace(aio=SimpleNamespace(models=SimpleNamespace(get=get)))
    config = SimpleNamespace(id=uuid.uuid4(), provider="gemini", display_name=None)
    reasons = asyncio.run(video_served.problems(gemini, config, ["veo-3.0-generate-001", "veo-2.0-generate-001"]))
    assert list(reasons) == ["veo-2.0-generate-001"]


class _VideoProvider:
    """A video provider listing a catalog of its own; its API answers HTTP 404 for ``model-b``."""

    async def list_video_models(self):
        return ["model-a", "model-b"]

    async def probe_video_model(self, model):
        if model == "model-b":
            raise VideoAPIError(404, "model model-b not found")

    def max_reference_images(self, _model):
        return 0

    def reference_images_with_start_frame(self, _model):
        return True


class _Db:
    def __init__(self, configs):
        self.configs = configs

    async def execute(self, _statement):
        return SimpleNamespace(scalars=lambda: SimpleNamespace(all=lambda: list(self.configs)))


@pytest.fixture
def video_world(monkeypatch, redis):
    config = SimpleNamespace(id=uuid.uuid4(), provider="kling", display_name="Kling AI")
    provider = _VideoProvider()

    async def cached(_config):
        return provider

    async def audience(*_args, **_kwargs):
        return provider_access.NO_ACCOUNT

    async def ai_on(_db):
        return True

    monkeypatch.setattr(router, "_cached_provider", cached)
    monkeypatch.setattr(provider_access, "load_provider_audience", audience)
    monkeypatch.setattr(ai_state, "is_ai_enabled", ai_on)
    return SimpleNamespace(config=config, db=_Db([config]))


def test_settings_offer_the_video_models_the_video_studio_offers(video_world):
    user = SimpleNamespace(id=uuid.uuid4(), email="admin@example.com")
    items = asyncio.run(settings_api.list_available_video_models(user=user, db=video_world.db))
    assert [(item.model, item.provider, item.provider_id) for item in items] == [
        ("model-a", "kling", str(video_world.config.id))
    ]
    studio = asyncio.run(router.list_video_models_for(video_world.db, user_id=user.id))
    assert [entry["model"] for entry in studio] == [item.model for item in items]


def test_settings_ask_no_video_provider_while_ai_is_off(video_world, monkeypatch):
    async def ai_off(_db):
        return False

    monkeypatch.setattr(ai_state, "is_ai_enabled", ai_off)
    user = SimpleNamespace(id=uuid.uuid4(), email="admin@example.com")
    assert asyncio.run(settings_api.list_available_video_models(user=user, db=video_world.db)) == []
