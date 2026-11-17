"""AI shots are made on the configured default video model, and on the configured video fallbacks alone (#10838).

Synthetic model names only. What is proven:

* a new shot naming no model is asked of the platform's choice (the configured
  default); naming the default is the same; a configured fallback is kept;
  any other model is refused before anything is queued or paid, naming the
  configuration;
* a regeneration of a shot first made on another model is asked of the
  default, never the shot's own model or provider;
* no fallback named: the configured video fallbacks are the chain, one the
  shot cannot take skipped with a note; ``fallback_models`` outside them is
  refused; with none configured there is no chain;
* with no default video model configured, the models named are kept;
* ``router.configured_video_models`` reads the settings, the default never
  its own fallback.
"""

from __future__ import annotations

import asyncio
import uuid
from types import SimpleNamespace

import pytest

from app.llm import router
from app.services.video_studio import clips

USER = SimpleNamespace(id=uuid.UUID("00000000-0000-0000-0000-0000000a1838"))
CONFIG = {"model-b": "00000000-0000-0000-0000-0000000b1838", "model-c": "00000000-0000-0000-0000-0000000c1838"}


def test_no_model_and_the_default_are_the_platforms_choice():
    assert clips.configured_model(None, default="model-a", fallbacks=[]) is None
    assert clips.configured_model("  ", default="model-a", fallbacks=["model-b"]) is None
    assert clips.configured_model("model-a", default="model-a", fallbacks=[]) is None


def test_a_configured_fallback_is_kept():
    assert clips.configured_model("model-b", default="model-a", fallbacks=["model-b"]) == "model-b"


def test_a_model_outside_the_configuration_is_refused_naming_it():
    with pytest.raises(clips.ClipError) as refused:
        clips.configured_model("model-x", default="model-a", fallbacks=[])
    message = str(refused.value)
    assert "not allowed by configuration" in message
    assert "model-a" in message and "no video fallback is configured" in message
    assert clips.VIDEO_MODELS_SETTING in message
    assert refused.value.about_model
    with pytest.raises(clips.ClipError, match=r"model-a, model-b"):
        clips.configured_model("model-x", default="model-a", fallbacks=["model-b"])


def test_without_a_configured_default_the_named_model_is_kept():
    assert clips.configured_model("model-x", default=None, fallbacks=[]) == "model-x"
    assert clips.configured_model(None, default=None, fallbacks=[]) is None


def test_a_regeneration_moves_to_the_default():
    params = {"provider_id": CONFIG["model-b"]}
    assert clips._configured_restart(params, {}, default="model-a", fallbacks=[]) == (None, None, True)
    # The shot's own provider is not carried over: the default's configured provider makes it.
    assert clips._configured_restart(params, {"model": "model-a"}, default="model-a", fallbacks=[]) == (
        None,
        None,
        True,
    )


def test_a_regeneration_may_name_a_configured_fallback_only():
    named = {"model": "model-b", "provider_id": CONFIG["model-b"]}
    assert clips._configured_restart({}, named, default="model-a", fallbacks=["model-b"]) == (
        "model-b",
        CONFIG["model-b"],
        True,
    )
    with pytest.raises(clips.ClipError, match="not allowed by configuration"):
        clips._configured_restart({}, {"model": "model-x"}, default="model-a", fallbacks=["model-b"])


def test_a_regeneration_without_a_configured_default_keeps_the_shots_model():
    params = {"provider_id": CONFIG["model-b"]}
    assert clips._configured_restart(params, {}, default=None, fallbacks=[]) == (None, CONFIG["model-b"], False)


@pytest.fixture
def resolved(monkeypatch):
    """``clips.resolve`` answering each configured model; *unusable* are the models that cannot take the shot."""
    unusable: set[str] = set()

    async def resolve(_db, _user_id, *, model=None, **_kwargs):
        if model in unusable:
            raise clips.ClipError(f"{model} is not served", about_model=True)
        return object(), model, SimpleNamespace(id=CONFIG[model], provider="provider", display_name=f"Provider {model}")

    monkeypatch.setattr(clips, "resolve", resolve)
    monkeypatch.setattr(clips, "_check_fallback", lambda *_a, **_k: None)
    return unusable


def test_no_fallback_named_the_configured_chain_is_used(resolved):
    chain, notes = asyncio.run(
        clips.resolve_fallbacks(
            None, USER, None, model="model-a", params={}, configured=("model-a", ["model-b", "model-c"])
        )
    )
    assert [item["model"] for item in chain] == ["model-b", "model-c"]
    assert notes == ["Configured video fallback: model-b, then model-c, tried in order if the default model fails."]


def test_a_configured_fallback_the_shot_cannot_use_is_skipped_with_a_note(resolved):
    resolved.add("model-b")
    chain, notes = asyncio.run(
        clips.resolve_fallbacks(
            None, USER, None, model="model-a", params={}, configured=("model-a", ["model-b", "model-c"])
        )
    )
    assert [item["model"] for item in chain] == ["model-c"]
    assert "not used for this shot: model-b" in notes[-1]


def test_no_fallback_configured_means_no_chain(resolved):
    assert asyncio.run(
        clips.resolve_fallbacks(None, USER, None, model="model-a", params={}, configured=("model-a", []))
    ) == ([], [])


def test_a_named_fallback_outside_the_configuration_is_refused(resolved):
    with pytest.raises(clips.ClipError, match="model-x not allowed by configuration") as refused:
        asyncio.run(
            clips.resolve_fallbacks(
                None, USER, ["model-b", "model-x"], model="model-a", params={}, configured=("model-a", ["model-b"])
            )
        )
    assert refused.value.about_model
    with pytest.raises(clips.ClipError, match="none is configured"):
        asyncio.run(
            clips.resolve_fallbacks(None, USER, ["model-b"], model="model-a", params={}, configured=("model-a", []))
        )


def test_a_named_fallback_inside_the_configuration_narrows_the_chain(resolved):
    chain, _notes = asyncio.run(
        clips.resolve_fallbacks(
            None, USER, ["model-c"], model="model-a", params={}, configured=("model-a", ["model-b", "model-c"])
        )
    )
    assert [item["model"] for item in chain] == ["model-c"]


def test_without_a_configured_default_named_fallbacks_are_kept(resolved):
    chain, _notes = asyncio.run(
        clips.resolve_fallbacks(None, USER, ["model-b"], model="model-a", params={}, configured=(None, []))
    )
    assert [item["model"] for item in chain] == ["model-b"]
    assert asyncio.run(
        clips.resolve_fallbacks(None, USER, None, model="model-a", params={}, configured=(None, []))
    ) == (
        [],
        [],
    )


def test_router_reads_the_configured_video_models(monkeypatch):
    settings = SimpleNamespace(video_model=" model-a ", video_fallback_models=["model-b", "", "model-a", "model-b", 3])

    async def platform_settings(_db):
        return settings

    monkeypatch.setattr(router, "_get_platform_settings", platform_settings)
    assert asyncio.run(router.configured_video_models(None)) == ("model-a", ["model-b"])
    settings.video_model, settings.video_fallback_models = None, None
    assert asyncio.run(router.configured_video_models(None)) == (None, [])
