"""Each model is offered where it serves: a provider's chat catalogue holds chat
models, its image, speech, video and embedding models are picked in their own
Settings section, and a provider that does not chat offers no chat model."""

import asyncio
import re
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from app.api.v1 import settings as settings_api
from app.llm import router
from app.llm.gemini_provider import GeminiProvider, _aspect_ratio_of

_GEMINI_IDS = [
    "gemini-2.5-pro",
    "gemini-3-pro-image-preview",
    "gemini-2.5-flash-image",
    "gemini-2.5-flash-preview-tts",
    "gemini-embedding-001",
    "imagen-4.0-generate-001",
    "veo-3.1-generate-preview",
]


def _gemini() -> GeminiProvider:
    provider = GeminiProvider(api_key="test-key")

    async def ids():
        return list(_GEMINI_IDS)

    provider._list_all_model_ids = ids
    return provider


def test_gemini_chats_with_its_chat_models_only():
    assert asyncio.run(_gemini().list_models()) == ["gemini-2.5-pro"]


def test_gemini_offers_its_drawing_models_with_imagen():
    """Nano Banana (Pro) is an image model: offered under Image Generation, never as a chat model."""
    assert set(asyncio.run(_gemini().list_image_models())) == {
        "gemini-3-pro-image-preview",
        "gemini-2.5-flash-image",
        "imagen-4.0-generate-001",
    }


def _drawn(parts):
    return SimpleNamespace(candidates=[SimpleNamespace(content=SimpleNamespace(parts=parts), finish_reason="STOP")])


def test_a_gemini_image_model_draws_through_generate_content_at_the_asked_ratio():
    provider = GeminiProvider(api_key="test-key")
    seen: dict = {}

    async def generate_content(**kwargs):
        seen.update(kwargs)
        image = SimpleNamespace(inline_data=SimpleNamespace(data=b"PNG", mime_type="image/png"))
        return _drawn([SimpleNamespace(inline_data=None, text="Here it is"), image])

    with patch.object(provider.client.aio.models, "generate_content", new=generate_content):
        data = asyncio.run(provider.generate_image("a cat", model="gemini-3-pro-image-preview", size="1536x1024"))
    assert data == b"PNG"
    assert seen["model"] == "gemini-3-pro-image-preview"
    assert seen["config"].response_modalities == ["IMAGE"]
    assert seen["config"].image_config.aspect_ratio == "3:2"


def test_a_gemini_image_model_that_draws_nothing_says_so():
    provider = GeminiProvider(api_key="test-key")

    async def generate_content(**_kwargs):
        return _drawn([SimpleNamespace(inline_data=None, text="I cannot draw that")])

    with (
        patch.object(provider.client.aio.models, "generate_content", new=generate_content),
        pytest.raises(RuntimeError, match=re.escape("gemini-2.5-flash-image returned no image")),
    ):
        asyncio.run(provider.generate_image("x", model="gemini-2.5-flash-image"))


def test_imagen_keeps_its_own_api():
    provider = GeminiProvider(api_key="test-key")

    async def generate_images(**kwargs):
        assert kwargs["model"] == "imagen-4.0-generate-001"
        return SimpleNamespace(generated_images=[SimpleNamespace(image=SimpleNamespace(image_bytes=b"JPG"))])

    with patch.object(provider.client.aio.models, "generate_images", new=generate_images):
        assert asyncio.run(provider.generate_image("x", model="imagen-4.0-generate-001")) == b"JPG"


@pytest.mark.parametrize(
    ("size", "ratio"),
    [("1024x1024", "1:1"), ("1536x1024", "3:2"), ("1024x1536", "2:3"), ("1920x1080", "16:9"), ("auto", "1:1")],
)
def test_a_size_is_drawn_at_the_nearest_ratio(size, ratio):
    assert _aspect_ratio_of(size) == ratio


def test_image_models_are_recognised_by_the_catalog_then_their_name():
    assert router.is_image_model("gemini-3-pro-image-preview")
    assert router.is_image_model("gemini/gemini-2.5-flash-image")
    assert router.is_image_model("gpt-image-2")
    assert not router.is_image_model("gemini-2.5-pro")
    assert not router.is_image_model(None)


def test_a_provider_that_does_not_chat_says_so():
    assert router.provider_capabilities("elevenlabs") == ["speech"]
    assert router.provider_capabilities("tei") == ["embedding"]
    assert router.provider_capabilities("openai") == ["chat", "decision"]
    assert router.provider_capabilities("anthropic") == ["chat"]
    assert router.provider_capabilities("not-a-provider") == []


class _Listing:
    def __init__(self, models, groups=None):
        self._models, self._groups = models, groups

    async def list_models_grouped(self):
        return self._groups

    async def list_models(self):
        return list(self._models)


def test_a_new_providers_discovered_models_are_its_chat_models():
    models, groups = asyncio.run(
        settings_api._list_chat_models(_Listing(["gpt-5", "gpt-image-2", "text-embedding-3-large", "tts-1"]))
    )
    assert (models, groups) == (["gpt-5"], None)
    models, groups = asyncio.run(
        settings_api._list_chat_models(
            _Listing([], {"OpenAI": ["gpt-5", "gpt-image-2"], "Images": ["dall-e-3"], "Google": ["gemini-2.5-pro"]})
        )
    )
    assert models == ["gemini-2.5-pro", "gpt-5"]
    assert groups == {"OpenAI": ["gpt-5"], "Google": ["gemini-2.5-pro"]}
