"""Context windows: typed as ``128K`` / ``1.05M``, scanned from the provider and the catalog.

* :func:`parse_token_count` reads what an administrator types, and the provider
  requests store it as a whole token count (422 otherwise);
* each provider's listing reports its windows (:meth:`list_model_context_windows`),
  bounded like a typed value since the listing is external input;
* :func:`scan_context_windows` answers each model from the provider, then the
  catalog, then the family table, and says when the provider was not read;
* ``POST /settings/llm-providers/{id}/context-windows/scan`` serves it to admins.
"""

from __future__ import annotations

import asyncio
import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from openai.types import Model
from pydantic import ValidationError

from app.api import deps
from app.api.v1 import settings as settings_api
from app.api.v1.schemas.settings import LLMProviderRequest, LLMProviderUpdateRequest
from app.core.database import get_db
from app.llm import context_window_scan
from app.llm.anthropic_provider import AnthropicProvider
from app.llm.base import BaseLLMProvider
from app.llm.context_window_scan import ScannedContextWindow, scan_context_windows
from app.llm.custom_openai_provider import CustomOpenAIProvider
from app.llm.gemini_provider import GeminiProvider
from app.llm.model_limits import (
    MAX_CONTEXT_WINDOW_TOKENS,
    parse_token_count,
    reported_context_window,
    resolve_snapshot_context_window,
)
from app.llm.openai_provider import OpenAIProvider
from app.llm.openrouter_provider import OpenRouterProvider

# ---------------------------------------------------------------------------
# What an administrator types
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "typed,tokens",
    [
        ("128K", 128_000),
        ("128k", 128_000),
        (" 128 K ", 128_000),
        ("1.05M", 1_050_000),
        ("1.050000M", 1_050_000),
        ("100.0M", MAX_CONTEXT_WINDOW_TOKENS),
        ("1,05M", 1_050_000),
        ("1M", 1_000_000),
        ("128.5K", 128_500),
        ("0.5K", 500),
        ("200000", 200_000),
        ("200,000", 200_000),
        ("200 000", 200_000),
        ("1_000_000", 1_000_000),
        ("1\u202f000\u202f000", 1_000_000),
        ("100M", MAX_CONTEXT_WINDOW_TOKENS),
        (128_000, 128_000),
        (128_000.0, 128_000),
    ],
)
def test_a_token_count_is_read_as_typed(typed, tokens):
    assert parse_token_count(typed) == tokens


@pytest.mark.parametrize(
    "typed",
    [
        "",
        "   ",
        "big",
        "128KB",
        "128 tokens",
        "1.5",  # a fraction of a token, no suffix
        "1.0000005M",  # 1_000_000.5 tokens
        "0",
        "0K",
        "-5",
        "-1M",
        "1,00,000",  # not grouped by three
        "200,000 000",  # two separators
        "1e6",
        "128\u00a0K",
        "128\tK",
        "0x10",
        "\u0661\u0662\u0668",  # Arabic-Indic digits
        "\u0661\u0662\u0668K",
        "\u0661,\u0662\u0662\u0668",
        "101M",
        # A fraction finer than a token is refused, however many digits it
        # takes: a Decimal rounded at its context precision accepted these.
        "1.0000000000000000000000000000001M",
        "100.000000000000000000000000000001M",
        "1.0001K",
        "1" * 5000,  # past int()'s digit limit: refused, never an exception
        "1." + "0" * 5000 + "M",
        MAX_CONTEXT_WINDOW_TOKENS + 1,
        0,
        -1,
        128_000.5,
        True,
        None,
        [128_000],
    ],
)
def test_anything_else_is_not_a_token_count(typed):
    assert parse_token_count(typed) is None


@pytest.mark.parametrize(
    "listed,tokens",
    [(128_000, 128_000), (128_000.0, 128_000), ("128000", None), ("128K", None), (True, None),
     (0, None), (MAX_CONTEXT_WINDOW_TOKENS + 1, None), (None, None), ({"tokens": 1}, None)],
)
def test_a_listed_window_is_only_a_whole_number_in_bounds(listed, tokens):
    assert reported_context_window(listed) == tokens


@pytest.mark.parametrize("request_model", [LLMProviderRequest, LLMProviderUpdateRequest])
def test_a_provider_save_stores_the_shorthand_as_tokens(request_model):
    extra = {"provider": "openai", "api_key": "sk-test"} if request_model is LLMProviderRequest else {}
    body = request_model(context_windows={"a": "128K", "b": "1.05M", "c": 200_000}, **extra)
    assert body.context_windows == {"a": 128_000, "b": 1_050_000, "c": 200_000}
    assert request_model(context_windows=None, **extra).context_windows is None


@pytest.mark.parametrize("bad", ["big", "0", 0, -1, "1.5", True, "101M"])
@pytest.mark.parametrize("request_model", [LLMProviderRequest, LLMProviderUpdateRequest])
def test_a_provider_save_refuses_a_window_that_is_not_a_count(request_model, bad):
    extra = {"provider": "openai", "api_key": "sk-test"} if request_model is LLMProviderRequest else {}
    with pytest.raises(ValidationError):
        request_model(context_windows={"a": bad}, **extra)


@pytest.mark.parametrize("key", ["", "   ", "m" * 256])
def test_a_window_must_be_keyed_by_a_model_id(key):
    with pytest.raises(ValidationError):
        LLMProviderUpdateRequest(context_windows={key: 128_000})


# ---------------------------------------------------------------------------
# What each provider's listing reports
# ---------------------------------------------------------------------------


def _openai_models(*entries: dict) -> SimpleNamespace:
    base = {"created": 0, "object": "model", "owned_by": "x"}
    return SimpleNamespace(data=[Model.model_validate({**base, **entry}) for entry in entries])


async def test_an_openai_compatible_listing_reports_every_known_field(monkeypatch):
    provider = OpenRouterProvider(api_key="sk-or-test")
    listing = _openai_models(
        {"id": "openrouter/a", "context_length": 200_000},
        {"id": "mistral/b", "max_context_length": 131_072},
        {"id": "vllm/c", "max_model_len": 32_768},
        {"id": "groq/d", "context_window": 8_192},
        {"id": "litellm/e", "max_input_tokens": 1_050_000},
        {"id": "first/wins", "context_length": 100, "max_model_len": 999},
        {"id": "none/listed"},
        {"id": "hostile/huge", "context_length": MAX_CONTEXT_WINDOW_TOKENS + 1},
        {"id": "hostile/text", "context_length": "128K"},
        {"id": "hostile/negative", "context_length": -1},
    )
    monkeypatch.setattr(provider.client.models, "list", AsyncMock(return_value=listing))

    assert await provider.list_model_context_windows() == {
        "openrouter/a": 200_000,
        "mistral/b": 131_072,
        "vllm/c": 32_768,
        "groq/d": 8_192,
        "litellm/e": 1_050_000,
        "first/wins": 100,
    }


async def test_a_custom_endpoint_reports_the_windows_of_its_raw_listing(monkeypatch):
    provider = CustomOpenAIProvider(api_key="key||https://llm.example.com/v1")
    entries = [{"id": "local-a", "max_model_len": 65_536}, {"id": "local-b"}, {"no": "id"}]
    monkeypatch.setattr(provider, "_list_model_entries", AsyncMock(return_value=entries))

    assert await provider.list_model_context_windows() == {"local-a": 65_536}
    assert await provider.list_models() == ["local-a", "local-b"]


async def test_anthropic_reports_max_input_tokens(monkeypatch):
    provider = AnthropicProvider(api_key="sk-ant-test")
    page = SimpleNamespace(data=[
        SimpleNamespace(id="claude-a", max_input_tokens=1_000_000),
        SimpleNamespace(id="claude-b", max_input_tokens=None),
        SimpleNamespace(id="claude-c"),
    ])
    listing = AsyncMock(return_value=page)
    monkeypatch.setattr(provider.client.models, "list", listing)

    assert await provider.list_model_context_windows() == {"claude-a": 1_000_000}
    assert listing.await_args.kwargs == {"limit": 100}


async def test_gemini_reports_input_token_limit(monkeypatch):
    provider = GeminiProvider(api_key="AIza-test")

    async def _pager():
        for model in (
            SimpleNamespace(name="models/gemini-a", input_token_limit=1_048_576),
            SimpleNamespace(name="models/gemini-b", input_token_limit=None),
            SimpleNamespace(name=None, input_token_limit=10),
        ):
            yield model

    listing = AsyncMock(return_value=_pager())
    monkeypatch.setattr(provider.client.aio.models, "list", listing)

    assert await provider.list_model_context_windows() == {"gemini-a": 1_048_576}
    assert listing.await_args.kwargs == {"config": {"query_base": True}}


def test_a_provider_whose_listing_carries_no_window_says_so():
    assert OpenAIProvider.list_model_context_windows is BaseLLMProvider.list_model_context_windows


# ---------------------------------------------------------------------------
# The scan: provider, then catalog, then family
# ---------------------------------------------------------------------------

_FAMILY_ONLY = "claude-opus-5-xtm-scan-test"


class _ListingProvider:
    provider_name = "fake"

    def __init__(self, windows=None, error: Exception | None = None, delay: float = 0.0):
        self._windows = windows
        self._error = error
        self._delay = delay

    async def list_model_context_windows(self):
        if self._delay:
            await asyncio.sleep(self._delay)
        if self._error:
            raise self._error
        return self._windows


async def test_the_provider_answers_first_then_the_catalog_then_the_family():
    assert resolve_snapshot_context_window("gpt-4o") == 128_000
    assert resolve_snapshot_context_window(_FAMILY_ONLY) is None
    provider = _ListingProvider({"gpt-4o": 64_000, "self-hosted": 32_768})

    scan = await scan_context_windows(
        provider, ["gpt-4o", "self-hosted", "gpt-4.1-mini-xtm", _FAMILY_ONLY, "unknown-model"]
    )

    assert scan.live_lookup == "ok"
    assert scan.results == [
        ScannedContextWindow("gpt-4o", 64_000, "provider"),
        ScannedContextWindow("self-hosted", 32_768, "provider"),
        ScannedContextWindow("gpt-4.1-mini-xtm", 1_000_000, "family"),
        ScannedContextWindow(_FAMILY_ONLY, 1_000_000, "family"),
        ScannedContextWindow("unknown-model", None, None),
    ]


async def test_a_provider_with_no_listed_windows_leaves_the_catalog_to_answer():
    scan = await scan_context_windows(_ListingProvider(None), ["gpt-4o", "unknown-model"])

    assert scan.live_lookup == "unsupported"
    assert scan.results == [
        ScannedContextWindow("gpt-4o", 128_000, "catalog"),
        ScannedContextWindow("unknown-model", None, None),
    ]


async def test_a_listing_that_fails_is_reported_and_the_catalog_still_answers(caplog):
    provider = _ListingProvider(error=RuntimeError("401 key sk-secret rejected\nforged line"))

    scan = await scan_context_windows(provider, ["gpt-4o"])

    assert scan.live_lookup == "failed"
    assert scan.results == [ScannedContextWindow("gpt-4o", 128_000, "catalog")]
    warnings = [r for r in caplog.records if r.name == context_window_scan.logger.name]
    assert len(warnings) == 1
    assert "\nforged line" not in warnings[0].getMessage()


async def test_a_listing_that_hangs_is_given_up(monkeypatch):
    monkeypatch.setattr(context_window_scan, "PROVIDER_LISTING_TIMEOUT_S", 0.01)

    scan = await scan_context_windows(_ListingProvider({"gpt-4o": 1}, delay=5), ["gpt-4o"])

    assert scan.live_lookup == "failed"
    assert scan.results == [ScannedContextWindow("gpt-4o", 128_000, "catalog")]


# ---------------------------------------------------------------------------
# The route
# ---------------------------------------------------------------------------

_SCAN = "/api/v1/settings/llm-providers/{id}/context-windows/scan"


class _Rows:
    def __init__(self, row):
        self._row = row

    def scalar_one_or_none(self):
        return self._row


class _FakeDB:
    def __init__(self, row):
        self._row = row

    async def execute(self, *_args, **_kwargs):
        return _Rows(self._row)


def _user(*, admin: bool):
    return SimpleNamespace(id=uuid.uuid4(), email="someone@example.com", is_admin=admin, is_active=True)


def _client(user, config) -> TestClient:
    app = FastAPI()
    app.include_router(settings_api.router, prefix="/api/v1/settings")

    async def _get_db():
        yield _FakeDB(config)

    app.dependency_overrides[get_db] = _get_db
    app.dependency_overrides[deps.get_current_user] = lambda: user
    app.dependency_overrides[deps.require_ai_enabled] = lambda: None
    return TestClient(app, raise_server_exceptions=False)


class _RouteProvider(_ListingProvider):
    built_with: list[str] = []
    windows: dict | None = {"anthropic/claude-x": 200_000}
    error: Exception | None = None

    def __init__(self, api_key: str):
        type(self).built_with.append(api_key)
        super().__init__(type(self).windows, type(self).error)


@pytest.fixture
def stored_provider(monkeypatch):
    config = SimpleNamespace(id=uuid.uuid4(), provider="openrouter")
    _RouteProvider.built_with = []
    _RouteProvider.windows = {"anthropic/claude-x": 200_000}
    _RouteProvider.error = None
    monkeypatch.setitem(settings_api.PROVIDER_MAP, "openrouter", _RouteProvider)
    reveal = AsyncMock(return_value="sk-or-stored")
    monkeypatch.setattr(settings_api, "reveal_field", reveal)
    return config, reveal


def test_an_admin_scans_with_the_stored_key(stored_provider):
    config, reveal = stored_provider

    resp = _client(_user(admin=True), config).post(
        _SCAN.format(id=config.id),
        json={"models": ["anthropic/claude-x", " gpt-4o ", "gpt-4o", "unknown-model"]},
    )

    assert resp.status_code == 200
    assert resp.json() == {
        "provider": "openrouter",
        "live_lookup": "ok",
        "results": [
            {"model": "anthropic/claude-x", "context_window": 200_000, "source": "provider"},
            {"model": "gpt-4o", "context_window": 128_000, "source": "catalog"},
            {"model": "unknown-model", "context_window": None, "source": None},
        ],
    }
    assert _RouteProvider.built_with == ["sk-or-stored"]
    assert reveal.await_args.args == (config, "api_key_enc")


def test_a_failed_listing_is_said_without_the_provider_s_answer(stored_provider):
    config, _reveal = stored_provider
    _RouteProvider.error = RuntimeError("upstream said: key sk-or-stored is revoked")

    resp = _client(_user(admin=True), config).post(
        _SCAN.format(id=config.id), json={"models": ["gpt-4o"]}
    )

    assert resp.status_code == 200
    assert resp.json()["live_lookup"] == "failed"
    assert "sk-or-stored" not in resp.text
    assert resp.json()["results"] == [{"model": "gpt-4o", "context_window": 128_000, "source": "catalog"}]


def test_a_non_admin_cannot_scan(stored_provider):
    config, reveal = stored_provider

    resp = _client(_user(admin=False), config).post(
        _SCAN.format(id=config.id), json={"models": ["gpt-4o"]}
    )

    assert resp.status_code == 403
    assert reveal.await_count == 0
    assert _RouteProvider.built_with == []


@pytest.mark.parametrize("provider_id", ["00000000-0000-0000-0000-000000000000", "not-a-uuid"])
def test_an_unknown_provider_is_not_found(stored_provider, provider_id):
    resp = _client(_user(admin=True), None).post(
        _SCAN.format(id=provider_id), json={"models": ["gpt-4o"]}
    )

    assert resp.status_code == 404
    assert resp.json() == {"detail": "Provider config not found"}


@pytest.mark.parametrize(
    "body",
    [{}, {"models": []}, {"models": [""]}, {"models": ["   "]}, {"models": ["m" * 256]},
     {"models": ["m"] * 1001}, {"models": "gpt-4o"}, {"models": [None]}],
)
def test_a_malformed_scan_is_refused_with_a_422(stored_provider, body):
    config, reveal = stored_provider

    resp = _client(_user(admin=True), config).post(_SCAN.format(id=config.id), json=body)

    assert resp.status_code == 422
    assert reveal.await_count == 0


def test_the_scan_route_is_behind_the_ai_switch():
    route = next(
        r for r in settings_api.router.routes
        if getattr(r, "path", "") == "/llm-providers/{provider_id}/context-windows/scan"
    )
    calls = {d.call for d in route.dependant.dependencies}
    assert calls >= {deps.require_admin, deps.require_ai_enabled}


@pytest.mark.parametrize("windows", [{"m": "big"}, {"m": 0}, {"m": "101M"}])
def test_a_save_with_a_window_that_is_not_a_count_is_a_422(windows):
    resp = _client(_user(admin=True), None).put(
        f"/api/v1/settings/llm-providers/{uuid.uuid4()}", json={"context_windows": windows}
    )

    assert resp.status_code == 422
