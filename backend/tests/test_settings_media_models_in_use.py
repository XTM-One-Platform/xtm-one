"""The image and speech models generation runs on, for the Settings cards (#1816).

With nothing saved, generation falls back to a built-in default or the AI
Gateway's marked one (#750); the cards showed nothing. The endpoint answers
from the resolvers generation uses, so the card names what really runs.
"""

from __future__ import annotations

import asyncio
import uuid
from types import SimpleNamespace

import pytest

from app.api.v1 import settings as settings_api
from app.llm import router
from app.llm.provider_access import ProviderNotUsableError

USER = SimpleNamespace(id=uuid.uuid4(), email="admin@example.com")


@pytest.fixture
def _resolvers(monkeypatch):
    state = {"ai": True, "image": None, "speech": None, "asked": []}

    async def _ai(_db):
        return state["ai"]

    async def _image(_db, *, user_id=None):
        state["asked"].append(("image", user_id))
        if isinstance(state["image"], Exception):
            raise state["image"]
        return state["image"]

    async def _speech(_db, *, user_id=None, provider_id=None, model=None):
        state["asked"].append(("speech", user_id))
        if isinstance(state["speech"], Exception):
            raise state["speech"]
        return state["speech"]

    monkeypatch.setattr("app.services.ai_state.is_ai_enabled", _ai)
    monkeypatch.setattr(router, "get_image_provider", _image)
    monkeypatch.setattr(router, "get_speech_provider", _speech)
    return state


def _ask() -> dict:
    return asyncio.run(settings_api.media_models_in_use(user=USER, db=object())).model_dump()


def test_the_models_the_resolvers_pick_for_the_caller(_resolvers):
    image_id, speech_id = uuid.uuid4(), uuid.uuid4()
    gateway = SimpleNamespace(provider_name="filigran_gateway", provider_config_id=image_id)
    _resolvers["image"] = (gateway, "gemini-3-pro-image-preview")
    _resolvers["speech"] = SimpleNamespace(
        provider=object(),
        model="tts-1-hd",
        config=SimpleNamespace(id=speech_id, provider="filigran_gateway"),
        voice=None,
    )
    answer = _ask()
    assert answer["image"] == {
        "model": "gemini-3-pro-image-preview",
        "provider": "filigran_gateway",
        "provider_id": str(image_id),
        "access_restricted": False,
    }
    assert answer["speech"]["model"] == "tts-1-hd"
    assert answer["speech"]["provider_id"] == str(speech_id)
    assert _resolvers["asked"] == [("image", USER.id), ("speech", USER.id)]


def test_a_kind_nothing_can_make_is_none_and_the_other_still_answers(_resolvers):
    _resolvers["image"] = ValueError("An image-capable provider is required")
    _resolvers["speech"] = SimpleNamespace(
        provider=object(),
        model="gpt-4o-mini-tts",
        config=SimpleNamespace(id=uuid.uuid4(), provider="openai"),
        voice=None,
    )
    answer = _ask()
    assert answer["image"] is None and answer["speech"]["model"] == "gpt-4o-mini-tts"
    # A provider withheld from the caller answers none as well.
    _resolvers["speech"] = ProviderNotUsableError("reserved for other groups")
    assert _ask()["speech"] is None


def test_a_provider_that_cannot_be_built_never_fails_the_read(_resolvers):
    # An unreadable vault key raises a RuntimeError; a 500 here takes the whole Settings page down.
    _resolvers["image"] = RuntimeError("The secret could not be read from its vault")
    _resolvers["speech"] = RuntimeError("The secret could not be read from its vault")
    assert _ask() == {"image": None, "speech": None}


def test_while_ai_is_off_no_provider_is_asked(_resolvers):
    _resolvers["ai"] = False
    assert _ask() == {"image": None, "speech": None}
    assert _resolvers["asked"] == []


def test_the_route_is_registered():
    paths = {route.path for route in settings_api.router.routes}
    assert "/media-models-in-use" in paths
