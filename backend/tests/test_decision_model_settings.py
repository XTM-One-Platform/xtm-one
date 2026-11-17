"""The decision model setting and its picker."""

import uuid
from types import SimpleNamespace

import httpx
import openai
import pytest
from fastapi import HTTPException
from pydantic import ValidationError

from app.api.deps import require_admin
from app.api.v1.schemas.settings import GeneralAssistantConfigRequest
from app.api.v1.settings import (
    _list_models_by_capability,
    _write_decision_model,
    router,
)
from app.llm.openai_provider import OpenAIProvider


def test_a_decision_model_longer_than_its_column_is_a_422_not_a_500():
    with pytest.raises(ValidationError):
        GeneralAssistantConfigRequest(decision_model="m" * 101)
    with pytest.raises(ValidationError):
        GeneralAssistantConfigRequest(decision_provider_id="x" * 37)
    assert GeneralAssistantConfigRequest(decision_model="m" * 100).decision_model == "m" * 100


def test_only_an_admin_sets_the_decision_model():
    put_route = next(
        route
        for route in router.routes
        if getattr(route, "path", "") == "/general-assistant" and "PUT" in getattr(route, "methods", set())
    )
    assert require_admin in {dependency.call for dependency in put_route.dependant.dependencies}


class _Scalars:
    def __init__(self, rows):
        self._rows = rows

    def all(self):
        return self._rows


class _Result:
    def __init__(self, rows):
        self._rows = rows

    def scalars(self):
        return _Scalars(self._rows)


class _Db:
    def __init__(self, rows):
        self._rows = rows

    async def execute(self, _stmt):
        return _Result(self._rows)


@pytest.mark.asyncio
async def test_the_decision_picker_asks_only_the_providers_the_caller_may_use(monkeypatch):
    # A restricted provider's models and id are not listed to whoever opens
    # Settings, and the provider is never called on their behalf.
    asked: list[str] = []

    class _Provider:
        def __init__(self, api_key: str):
            self.api_key = api_key

        async def decision_listing(self):
            asked.append(self.api_key)
            return [f"clm-{self.api_key}"], True

    open_cfg = SimpleNamespace(id=uuid.uuid4(), provider="ollama", api_key_enc="open")
    restricted_cfg = SimpleNamespace(id=uuid.uuid4(), provider="ollama", api_key_enc="restricted")

    async def _routing(_db, _user_id, configs):
        return SimpleNamespace(usable=[c for c in configs if c is open_cfg])

    async def _provider_for_config(cfg):
        return _Provider(cfg.api_key_enc)

    monkeypatch.setattr("app.api.v1.settings.PROVIDER_MAP", {"ollama": _Provider})
    monkeypatch.setattr("app.api.v1.settings.provider_for_config", _provider_for_config)
    monkeypatch.setattr("app.llm.provider_access.provider_routing", _routing)

    user = SimpleNamespace(id=uuid.uuid4(), email="user@example.com")
    models = await _list_models_by_capability(_Db([open_cfg, restricted_cfg]), "decision", user, usable_only=True)

    assert [(m.model, m.provider_id) for m in models] == [("clm-open", str(open_cfg.id))]
    assert asked == ["open"]


class _OpenAIClient:
    """OpenAI as measured on 2026-10-16: unknown models refused as such, decision models for the duplicate."""

    api_key = "sk-test"
    base_url = "https://api.openai.com/v1/"

    def __init__(self, listed, decision_models):
        self.listed = listed
        self.decision_models = decision_models
        self.models = self

    def with_options(self, **_options):
        return self

    async def list(self):
        return SimpleNamespace(data=[SimpleNamespace(id=m) for m in self.listed])

    async def post(self, _path, *, cast_to, body):
        request = httpx.Request("POST", "https://api.openai.com/v1/decisions")
        if body["model"] in self.decision_models:
            error = {"message": "Choice values must be unique.", "param": "questions[0].choices[1].value", "code": None}
            raise openai.BadRequestError(error["message"], response=httpx.Response(400, request=request), body=error)
        error = {"message": "The model does not exist.", "param": None, "code": "model_not_found"}
        raise openai.NotFoundError(error["message"], response=httpx.Response(404, request=request), body=error)


@pytest.mark.asyncio
async def test_the_decision_picker_lists_openai_decision_models_whatever_the_card_enables(monkeypatch):
    cfg = SimpleNamespace(id=uuid.uuid4(), provider="openai", api_key_enc="k", available_models=["gpt-chat"])

    async def _routing(_db, _user_id, configs):
        return SimpleNamespace(usable=list(configs))

    async def _provider_for_config(config):
        provider = OpenAIProvider(api_key="sk-test")
        provider.client = _OpenAIClient(["gpt-chat", "gpt-dec"], {"gpt-dec"})
        provider.provider_config_id = str(config.id)
        return provider

    monkeypatch.setattr("app.api.v1.settings.provider_for_config", _provider_for_config)
    monkeypatch.setattr("app.llm.provider_access.provider_routing", _routing)

    user = SimpleNamespace(id=uuid.uuid4(), email="admin@example.com")
    models = await _list_models_by_capability(_Db([cfg]), "decision", user, usable_only=True)

    assert [(m.model, m.provider, m.provider_id) for m in models] == [("gpt-dec", "openai", str(cfg.id))]


@pytest.mark.asyncio
async def test_a_decision_model_on_a_provider_serving_none_is_refused():
    class _GetDb:
        async def get(self, _model, _id):
            return SimpleNamespace(provider="azure_openai")

    body = GeneralAssistantConfigRequest(decision_model="gpt-dec", decision_provider_id=str(uuid.uuid4()))
    ps = SimpleNamespace(decision_model=None, decision_provider_id=None)

    with pytest.raises(HTTPException) as refused:
        await _write_decision_model(_GetDb(), ps, body)

    assert refused.value.status_code == 422
    assert "serves no decision models" in refused.value.detail
    assert ps.decision_model is None


@pytest.mark.asyncio
async def test_a_restricted_openai_provider_is_neither_built_nor_probed(monkeypatch):
    built: list[str] = []
    usable = SimpleNamespace(id=uuid.uuid4(), provider="openai", api_key_enc="open")
    restricted = SimpleNamespace(id=uuid.uuid4(), provider="openai", api_key_enc="restricted")

    async def _routing(_db, _user_id, configs):
        return SimpleNamespace(usable=[c for c in configs if c is usable])

    async def _provider_for_config(config):
        built.append(config.api_key_enc)
        provider = OpenAIProvider(api_key="sk-test")
        provider.client = _OpenAIClient(["gpt-dec"], {"gpt-dec"})
        return provider

    monkeypatch.setattr("app.api.v1.settings.provider_for_config", _provider_for_config)
    monkeypatch.setattr("app.llm.provider_access.provider_routing", _routing)

    user = SimpleNamespace(id=uuid.uuid4(), email="user@example.com")
    models = await _list_models_by_capability(_Db([usable, restricted]), "decision", user, usable_only=True)

    assert [m.provider_id for m in models] == [str(usable.id)]
    assert built == ["open"]


@pytest.mark.asyncio
async def test_ollama_and_openai_decision_models_list_together(monkeypatch):
    class _Ollama:
        async def decision_listing(self):
            return ["clef-flash:latest"], True

    ollama = SimpleNamespace(id=uuid.uuid4(), provider="ollama", api_key_enc="o")
    openai_cfg = SimpleNamespace(id=uuid.uuid4(), provider="openai", api_key_enc="k")

    async def _routing(_db, _user_id, configs):
        return SimpleNamespace(usable=list(configs))

    async def _provider_for_config(config):
        if config is ollama:
            return _Ollama()
        provider = OpenAIProvider(api_key="sk-test")
        provider.client = _OpenAIClient(["gpt-chat", "gpt-dec"], {"gpt-dec"})
        return provider

    monkeypatch.setattr("app.api.v1.settings.provider_for_config", _provider_for_config)
    monkeypatch.setattr("app.llm.provider_access.provider_routing", _routing)

    user = SimpleNamespace(id=uuid.uuid4(), email="admin@example.com")
    models = await _list_models_by_capability(_Db([ollama, openai_cfg]), "decision", user, usable_only=True)

    assert [(m.model, m.provider_id) for m in models] == [
        ("clef-flash:latest", str(ollama.id)),
        ("gpt-dec", str(openai_cfg.id)),
    ]


@pytest.mark.parametrize("is_admin", [True, False])
@pytest.mark.asyncio
async def test_the_decision_picker_says_why_a_listing_provider_offered_none(monkeypatch, is_admin):
    # "none declared" and "listing failed" both used to read as [].
    answers = {
        "listed": (["clm"], True),
        "empty": ([], True),
        "incomplete": ([], False),
        "failed": RuntimeError("connection refused"),
        "unlisted": ([], True),
    }

    class _Listing:
        lists_decision_models = True

        def __init__(self, name):
            self.name = name

        async def decision_listing(self):
            answer = answers[self.name]
            if isinstance(answer, Exception):
                raise answer
            return answer

    class _Unlisted(_Listing):
        lists_decision_models = False

    cfgs = {name: SimpleNamespace(id=uuid.uuid4(), provider="ollama", api_key_enc=name) for name in answers}
    cfgs["unlisted"].provider = "openrouter"
    restricted = SimpleNamespace(id=uuid.uuid4(), provider="ollama", api_key_enc="restricted")

    async def _routing(_db, _user_id, configs):
        return SimpleNamespace(usable=[c for c in configs if c is not restricted])

    async def _provider_for_config(cfg):
        return (_Unlisted if cfg.provider == "openrouter" else _Listing)(cfg.api_key_enc)

    monkeypatch.setattr("app.api.v1.settings.PROVIDER_MAP", {"ollama": _Listing, "openrouter": _Unlisted})
    monkeypatch.setattr("app.api.v1.settings.provider_for_config", _provider_for_config)
    monkeypatch.setattr("app.llm.provider_access.provider_routing", _routing)

    listings = {}
    user = SimpleNamespace(id=uuid.uuid4(), email="admin@example.com", is_admin=is_admin)
    await _list_models_by_capability(
        _Db([*cfgs.values(), restricted]), "decision", user, usable_only=True, listings=listings
    )

    expected = {str(cfgs[name].id): name for name in ("listed", "empty", "incomplete", "failed")}
    if is_admin:
        expected[str(restricted.id)] = "unavailable"
    assert listings == expected
