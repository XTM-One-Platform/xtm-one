"""What the environment pins for the video, speech, music and decision models,
without a database: which stored choices count as pinned, and the provider
types the configuration docs list. The boot seed itself runs against
PostgreSQL in ``test_env_capability_defaults_postgres.py``.
"""

from __future__ import annotations

import re
import uuid
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.core.config import settings
from app.llm.env_capability_defaults import ROLES, pinned_choices
from app.llm.router import PROVIDER_MAP, VIDEO_DEFAULTS

CONFIGURATION_DOCS = Path(__file__).resolve().parents[2] / "docs" / "docs" / "deployment" / "configuration.md"
GEMINI_ID = uuid.uuid4()


@pytest.fixture(autouse=True)
def _no_capability_env(monkeypatch):
    monkeypatch.setattr(settings, "byollm_activated", False)
    for name in ("default_image_provider", "default_image_model", "default_image_provider_base_url"):
        monkeypatch.setattr(settings, name, "")
    for role in ROLES:
        for env in (role.provider_env, role.model_env, role.voice_env):
            if env:
                monkeypatch.setattr(settings, env.lower(), "")


def _stored(**fields: object) -> SimpleNamespace:
    empty = {attr: None for attr in ("image_model", "image_provider", "image_provider_id")}
    empty.update({attr: None for role in ROLES for attr in role.fields})
    return SimpleNamespace(**{**empty, **fields})


def test_a_role_counts_as_pinned_while_the_platform_holds_what_the_environment_pins(monkeypatch):
    monkeypatch.setattr(settings, "default_video_provider", "gemini")
    applied = _stored(video_provider="gemini", video_provider_id=GEMINI_ID, video_model=VIDEO_DEFAULTS["gemini"])
    (choice,) = pinned_choices(applied)
    assert (choice.key, choice.model, choice.provider_id, choice.provider) == (
        "video",
        VIDEO_DEFAULTS["gemini"],
        GEMINI_ID,
        "gemini",
    )


@pytest.mark.parametrize(
    "stored",
    [
        {"video_provider": "litellm", "video_model": "veo-2.0-generate-001"},
        {"video_provider": "gemini", "video_model": "veo-2.0-generate-001"},
        {"video_provider": "litellm", "video_model": VIDEO_DEFAULTS["gemini"]},
    ],
    ids=["another-provider-and-model", "another-model", "another-provider"],
)
def test_a_choice_the_seed_could_not_replace_is_not_the_environments(monkeypatch, stored):
    """``DEFAULT_VIDEO_PROVIDER=gemini`` with no Gemini configured: the seed kept
    the stored choice, which the environment did not make."""
    monkeypatch.setattr(settings, "default_video_provider", "gemini")
    assert pinned_choices(_stored(**stored)) == []


def test_a_provider_type_that_cannot_serve_the_role_pins_nothing(monkeypatch):
    monkeypatch.setattr(settings, "default_decision_provider", "gemini")
    monkeypatch.setattr(settings, "default_decision_model", "clef-flash")
    assert pinned_choices(_stored(decision_model="clef-flash")) == []

    monkeypatch.setattr(settings, "default_decision_provider", "openai")
    assert [choice.key for choice in pinned_choices(_stored(decision_model="clef-flash"))] == ["decision"]


def test_nothing_is_pinned_under_byollm(monkeypatch):
    monkeypatch.setattr(settings, "byollm_activated", True)
    monkeypatch.setattr(settings, "default_video_provider", "gemini")
    monkeypatch.setattr(settings, "default_image_model", "gpt-image-2")
    stored = _stored(image_model="gpt-image-2", video_provider="gemini", video_model=VIDEO_DEFAULTS["gemini"])
    assert pinned_choices(stored) == []


def _documented_provider_types(variable: str) -> set[str]:
    row = next(
        line for line in CONFIGURATION_DOCS.read_text(encoding="utf-8").splitlines() if line.startswith(f"| `{variable}` |")
    )
    return {token for token in re.findall(r"`([a-z_]+)`", row) if token in PROVIDER_MAP}


@pytest.mark.parametrize(("variable", "key"), [("DEFAULT_DECISION_PROVIDER", "decision"), ("DEFAULT_MUSIC_PROVIDER", "music")])
def test_the_docs_list_every_provider_type_that_serves_a_closed_role(variable, key):
    """The decision and music rows list every provider type serving the role,
    no more: they are what an operator picks from."""
    role = next(role for role in ROLES if role.key == key)
    assert _documented_provider_types(variable) == {name for name in PROVIDER_MAP if role.serves(name)}
