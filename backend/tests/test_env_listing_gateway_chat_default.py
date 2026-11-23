"""#1421: a listed AI Gateway's default is the chat model it marks, never another provider's."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.llm.env_listing import apply_listing, declared_default, list_models_for_seed, replaced_alias

LISTED = ["claude-sonnet-5", "default", "gpt-6"]


def test_the_gateway_declares_the_chat_model_it_marks():
    assert declared_default("filigran_gateway", "gpt-6", LISTED) == "gpt-6"
    assert replaced_alias("filigran_gateway", "gpt-6") == "default"


def test_the_gateway_keeps_its_alias_without_a_listed_mark():
    assert declared_default("filigran_gateway", None, LISTED) == "default"
    assert declared_default("filigran_gateway", "gpt-retired", LISTED) == "default"
    assert replaced_alias("filigran_gateway", "default") is None


def test_the_gateway_alias_follows_the_mark():
    cfg = SimpleNamespace(provider="filigran_gateway", default_model="default", available_models=None, env_models=None)
    apply_listing(cfg, LISTED, chat_default="gpt-6")
    assert cfg.default_model == "gpt-6"


# Pin today's behaviour: these pass before and after #1421.
@pytest.mark.parametrize("provider", ["custom_openai", "litellm"])
def test_another_provider_ignores_a_marked_chat_default(provider):
    alias = declared_default(provider, None, LISTED)
    assert declared_default(provider, "gpt-6", LISTED) == alias
    assert replaced_alias(provider, "gpt-6") is None
    cfg = SimpleNamespace(provider=provider, default_model="default", available_models=None, env_models=None)
    apply_listing(cfg, LISTED, chat_default="gpt-6")
    assert cfg.default_model == "default"


class _Listing:
    def __init__(self, provider_name):
        self.provider_name = provider_name

    async def list_models(self):
        return ["claude-sonnet-5", "gpt-6"]

    async def marked_default_model(self, kind):
        return "gpt-6" if kind == "chat" else None


@pytest.mark.asyncio
@pytest.mark.parametrize(("provider", "expected"), [("filigran_gateway", "gpt-6"), ("custom_openai", None)])
async def test_only_the_gateway_listing_reads_a_chat_default(monkeypatch, provider, expected):
    from app.llm import router

    monkeypatch.setattr(router, "provider_instance", lambda _cls, _key: _Listing(provider))
    listing = await list_models_for_seed(provider, "key")
    assert listing is not None
    assert listing.models == ["claude-sonnet-5", "gpt-6"]
    assert listing.chat_default == expected
