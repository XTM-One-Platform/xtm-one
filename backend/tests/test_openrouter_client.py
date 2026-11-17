"""The OpenRouter API client behind the integration's console.

Every OpenRouter answer is typed on the way in, every failure becomes an
``OpenRouterError`` whose public message is a fixed sentence (OpenRouter's own
wording stays in the log), and no value a caller chose reaches a URL unchecked.
The transport is an ``httpx.MockTransport``: nothing leaves the process.
"""

from __future__ import annotations

import gzip
import hashlib
import json

import httpx
import pytest

from app.integrations.providers import openrouter

_KEY = "sk-or-v1-test-key"
_MANAGEMENT = "sk-or-v1-management"


def _install(monkeypatch, handler) -> list[httpx.Request]:
    """Route every client the module opens through ``handler``; return the requests."""
    seen: list[httpx.Request] = []
    real = httpx.AsyncClient

    def _recording(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return handler(request)

    def _client(**kwargs):
        return real(transport=httpx.MockTransport(_recording), **kwargs)

    # The module builds every client through egress_client (the relay funnel).
    monkeypatch.setattr(openrouter, "egress_client", _client)
    openrouter._catalog_cache.clear()
    openrouter._catalog_locks.clear()
    return seen


class _Stream(httpx.AsyncByteStream):
    """A body served in chunks, as a socket serves it (a ``content=`` body is pre-read)."""

    def __init__(self, body: bytes) -> None:
        self._body = body

    async def __aiter__(self):
        for start in range(0, len(self._body), 512):
            yield self._body[start : start + 512]


def _raw(status: int, body: bytes, headers: dict[str, str] | None = None) -> httpx.Response:
    return httpx.Response(status, headers=headers or {}, stream=_Stream(body))


def _json(status: int, body) -> httpx.Response:
    return _raw(status, json.dumps(body).encode(), {"content-type": "application/json"})


_MODEL = {
    "id": "anthropic/claude-sonnet-5",
    "canonical_slug": "anthropic/claude-sonnet-5-20260101",
    "name": "Anthropic: Claude Sonnet 5",
    "created": 1767225600,
    "description": "x" * 900,
    "context_length": 200000,
    "architecture": {"input_modalities": ["text", "image"], "output_modalities": ["text"]},
    "pricing": {
        "prompt": "0.000003",
        "completion": "0.000015",
        "input_cache_read": "0.0000003",
        "web_search": "0.01",
    },
    "top_provider": {"context_length": 200000, "max_completion_tokens": 64000, "is_moderated": True},
    "supported_parameters": ["tools", "tool_choice", "reasoning", "response_format"],
}


# ---------------------------------------------------------------------------
# Catalog
# ---------------------------------------------------------------------------


async def test_a_model_is_typed_with_prices_per_million_tokens(monkeypatch):
    seen = _install(monkeypatch, lambda request: _json(200, {"data": [_MODEL]}))

    [model] = await openrouter.list_models(_KEY)

    assert model == {
        "id": "anthropic/claude-sonnet-5",
        "name": "Anthropic: Claude Sonnet 5",
        "author": "anthropic",
        "created": "2026-01-01T00:00:00+00:00",
        "description": "x" * 600,
        "context_length": 200000,
        "max_completion_tokens": 64000,
        "is_moderated": True,
        "input_modalities": ["text", "image"],
        "output_modalities": ["text"],
        "pricing": {
            "prompt": 3.0,
            "completion": 15.0,
            "input_cache_read": pytest.approx(0.3),
            "input_cache_write": None,
            "internal_reasoning": None,
            "request": None,
            "image": None,
            "web_search": 0.01,
        },
        "variable_pricing": False,
        "is_free": False,
        "supports_tools": True,
        "supports_reasoning": True,
        "supports_structured_outputs": True,
        "supported_parameters": ["tools", "tool_choice", "reasoning", "response_format"],
        "knowledge_cutoff": None,
        "expiration_date": None,
    }
    [request] = seen
    assert str(request.url) == "https://openrouter.ai/api/v1/models"
    assert request.headers["authorization"] == f"Bearer {_KEY}"
    assert request.headers["accept-encoding"] == "identity"


async def test_a_router_priced_with_the_sentinel_has_no_price(monkeypatch):
    auto = {**_MODEL, "id": "openrouter/auto", "pricing": {"prompt": "-1", "completion": "-1"}}
    _install(monkeypatch, lambda request: _json(200, {"data": [auto]}))

    [model] = await openrouter.list_models(_KEY)

    assert (model["pricing"]["prompt"], model["pricing"]["completion"]) == (None, None)
    assert model["variable_pricing"] is True
    assert model["is_free"] is False


async def test_a_free_model_is_marked_free(monkeypatch):
    free = {**_MODEL, "id": "meta-llama/llama-3.3-70b-instruct:free", "pricing": {"prompt": "0", "completion": "0"}}
    _install(monkeypatch, lambda request: _json(200, {"data": [free]}))

    [model] = await openrouter.list_models(_KEY)

    assert model["is_free"] is True
    assert model["id"] == "meta-llama/llama-3.3-70b-instruct:free"


async def test_an_alias_of_an_author_s_latest_model_is_listed_under_its_author(monkeypatch):
    alias = {**_MODEL, "id": "~google/gemini-pro-latest"}
    _install(monkeypatch, lambda request: _json(200, {"data": [alias]}))

    [model] = await openrouter.list_models(_KEY)

    assert (model["id"], model["author"]) == ("~google/gemini-pro-latest", "google")


@pytest.mark.parametrize(
    "bad",
    [
        {"id": "../keys"},
        {"id": "anthropic/../keys"},
        {"id": "no-slash"},
        {"id": "a/b/c"},
        {"id": "~~google/gemini-pro-latest"},
        {"id": "~/gemini-pro-latest"},
        {"id": "goo~gle/gemini-pro-latest"},
        {"id": "google/~gemini-pro-latest"},
        {"id": 42},
        "not an object",
    ],
    ids=[
        "dotdot", "dotdot-slug", "no-slash", "two-slashes", "two-tildes", "tilde-alone",
        "tilde-inside-author", "tilde-leading-slug", "not-a-string", "not-an-object",
    ],
)
async def test_an_entry_that_is_not_a_model_id_is_dropped(monkeypatch, bad):
    _install(monkeypatch, lambda request: _json(200, {"data": [bad, _MODEL]}))

    models = await openrouter.list_models(_KEY)

    assert [m["id"] for m in models] == ["anthropic/claude-sonnet-5"]


async def test_embedding_models_come_from_their_own_route(monkeypatch):
    seen = _install(monkeypatch, lambda request: _json(200, {"data": []}))

    await openrouter.list_models(_KEY, kind="embeddings")

    assert seen[0].url.path == "/api/v1/embeddings/models"


async def test_the_catalog_is_fetched_once_per_key_and_kind(monkeypatch):
    seen = _install(monkeypatch, lambda request: _json(200, {"data": [_MODEL]}))

    first = await openrouter.cached_models(_KEY)
    second = await openrouter.cached_models(_KEY)
    await openrouter.cached_models(_KEY, kind="embeddings")
    await openrouter.cached_models("sk-or-v1-other")

    assert first is second
    assert [r.url.path for r in seen] == [
        "/api/v1/models",
        "/api/v1/embeddings/models",
        "/api/v1/models",
    ]


async def test_model_endpoints_drop_the_variant_and_type_each_provider(monkeypatch):
    seen = _install(
        monkeypatch,
        lambda request: _json(
            200,
            {
                "data": {
                    "id": "anthropic/claude-sonnet-5",
                    "endpoints": [
                        {
                            "provider_name": "Anthropic",
                            "quantization": "unknown",
                            "context_length": 200000,
                            "status": 0,
                            "uptime_last_30m": 99.5,
                            "latency_last_30m": {"p50": 412.0},
                            "supported_parameters": ["tools"],
                            "pricing": {"prompt": "0.000003", "completion": "0.000015"},
                        },
                        "junk",
                    ],
                }
            },
        ),
    )

    [endpoint] = await openrouter.list_model_endpoints(_KEY, "anthropic/claude-sonnet-5:nitro")

    assert seen[0].url.path == "/api/v1/models/anthropic/claude-sonnet-5/endpoints"
    assert endpoint["provider_name"] == "Anthropic"
    assert endpoint["latency_p50_ms"] == 412.0
    assert endpoint["supports_tools"] is True
    assert endpoint["pricing"] == {"prompt": 3.0, "completion": 15.0, "input_cache_read": None}


async def test_an_alias_s_endpoints_are_asked_for_by_its_id(monkeypatch):
    seen = _install(monkeypatch, lambda request: _json(200, {"data": {"endpoints": []}}))

    assert await openrouter.list_model_endpoints(_KEY, "~google/gemini-pro-latest") == []
    assert seen[0].url.path == "/api/v1/models/~google/gemini-pro-latest/endpoints"


@pytest.mark.parametrize(
    "model_id", ["../keys", "anthropic/../../keys", "anthropic", "a/b/c", "~~a/b", "~/../keys", ""]
)
async def test_a_model_id_that_is_not_one_never_reaches_a_url(monkeypatch, model_id):
    seen = _install(monkeypatch, lambda request: _json(200, {"data": {"endpoints": []}}))

    with pytest.raises(ValueError):
        await openrouter.list_model_endpoints(_KEY, model_id)
    assert seen == []


# ---------------------------------------------------------------------------
# Account
# ---------------------------------------------------------------------------


async def test_the_key_info_is_typed(monkeypatch):
    _install(
        monkeypatch,
        lambda request: _json(
            200,
            {
                "data": {
                    "label": "sk-or-v1-abc...123",
                    "limit": 50,
                    "limit_remaining": 12.5,
                    "limit_reset": "monthly",
                    "usage": 37.5,
                    "usage_daily": 1.25,
                    "usage_weekly": "not a number",
                    "is_free_tier": False,
                    "is_provisioning_key": True,
                    "limit_reset_extra": "ignored",
                }
            },
        ),
    )

    info = await openrouter.get_key_info(_KEY)

    assert info["limit"] == 50.0
    assert info["limit_remaining"] == 12.5
    assert info["limit_reset"] == "monthly"
    assert (info["usage"], info["usage_daily"], info["usage_weekly"]) == (37.5, 1.25, 0.0)
    # The deprecated field still answers for the new one.
    assert info["is_management_key"] is True
    assert info["free_model_daily_requests"] is None


async def test_credits_are_read_with_the_management_key(monkeypatch):
    seen = _install(
        monkeypatch, lambda request: _json(200, {"data": {"total_credits": 100, "total_usage": 37.5}})
    )

    assert await openrouter.get_credits(_MANAGEMENT) == {
        "total_credits": 100.0,
        "total_usage": 37.5,
        "remaining": 62.5,
    }
    assert seen[0].headers["authorization"] == f"Bearer {_MANAGEMENT}"


async def test_activity_rows_are_typed_and_filters_validated(monkeypatch):
    seen = _install(
        monkeypatch,
        lambda request: _json(
            200,
            {
                "data": [
                    {
                        "date": "2026-09-30 00:00:00",
                        "model": "openai/gpt-4o",
                        "provider_name": "OpenAI",
                        "usage": 0.42,
                        "requests": 7,
                        "prompt_tokens": 1000,
                        "completion_tokens": 200,
                    },
                    {"model": "no date"},
                ]
            },
        ),
    )
    hash_ = "a" * 64

    rows = await openrouter.get_activity(_MANAGEMENT, date="2026-09-30", api_key_hash=hash_)

    assert rows == [
        {
            "date": "2026-09-30",
            "model": "openai/gpt-4o",
            "provider_name": "OpenAI",
            "usage": 0.42,
            "byok_usage_inference": 0.0,
            "requests": 7,
            "prompt_tokens": 1000,
            "completion_tokens": 200,
            "reasoning_tokens": 0,
            "cached_tokens": 0,
        }
    ]
    assert dict(seen[0].url.params) == {"date": "2026-09-30", "api_key_hash": hash_}
    with pytest.raises(ValueError):
        await openrouter.get_activity(_MANAGEMENT, date="2026-9-30&x=1")
    with pytest.raises(ValueError):
        await openrouter.get_activity(_MANAGEMENT, api_key_hash="../keys")
    assert len(seen) == 1


# ---------------------------------------------------------------------------
# Failures
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("status", "management", "kind"),
    [
        (401, False, "auth"),
        (403, False, "auth"),
        (403, True, "management_key_required"),
        (404, False, "not_found"),
        (429, False, "rate_limited"),
        (400, False, "invalid_request"),
        (422, False, "invalid_request"),
        (500, False, "unavailable"),
        (503, False, "unavailable"),
    ],
)
async def test_a_refusal_is_a_fixed_sentence(monkeypatch, status, management, kind):
    secret_wording = "upstream says: account acct_42 is suspended for fraud"
    _install(monkeypatch, lambda request: _json(status, {"error": {"code": status, "message": secret_wording}}))

    with pytest.raises(openrouter.OpenRouterError) as raised:
        await openrouter.request("GET", "/credits", key=_MANAGEMENT, action="read credits", management=management)

    assert raised.value.kind == kind
    assert raised.value.public_message == openrouter._PUBLIC_MESSAGES[kind]
    assert "acct_42" not in raised.value.public_message
    assert _MANAGEMENT not in raised.value.detail


async def test_a_transport_failure_is_unavailable(monkeypatch):
    def _boom(request):
        raise httpx.ConnectTimeout("timed out", request=request)

    _install(monkeypatch, _boom)

    with pytest.raises(openrouter.OpenRouterError) as raised:
        await openrouter.get_key_info(_KEY)
    assert raised.value.kind == "unavailable"


@pytest.mark.parametrize(
    "response",
    [
        _raw(200, b"<html>maintenance</html>", {"content-type": "text/html"}),
        _raw(200, b"{not json", {"content-type": "application/json"}),
        _json(200, {"no": "envelope"}),
        _raw(
            200,
            gzip.compress(json.dumps({"data": []}).encode()),
            {"content-type": "application/json", "content-encoding": "gzip"},
        ),
        _raw(302, b"", {"location": "https://elsewhere.example/models"}),
    ],
    ids=["html", "bad-json", "no-envelope", "compressed", "redirect"],
)
async def test_an_answer_that_is_not_the_api_is_invalid(monkeypatch, response):
    _install(monkeypatch, lambda request: response)

    with pytest.raises(openrouter.OpenRouterError) as raised:
        await openrouter.list_models(_KEY)
    assert raised.value.kind == "invalid_response"


async def test_a_body_over_the_cap_is_cut(monkeypatch):
    monkeypatch.setattr(openrouter, "_MAX_BODY_BYTES", 1024)
    _install(monkeypatch, lambda request: _json(200, {"data": ["x" * 4096]}))

    with pytest.raises(openrouter.OpenRouterError) as raised:
        await openrouter.list_models(_KEY)
    assert raised.value.kind == "invalid_response"


# ---------------------------------------------------------------------------
# API keys
# ---------------------------------------------------------------------------

_HASH = "f" * 64
_KEY_OBJECT = {
    "hash": _HASH,
    "name": "Platform",
    "label": "sk-or-v1-abc...123",
    "disabled": False,
    "limit": 25,
    "limit_reset": "weekly",
    "usage": 1.5,
    "created_at": "2026-09-01T10:00:00Z",
}


def test_the_key_hash_is_the_sha256_of_the_key():
    assert openrouter.key_hash(f"  {_KEY}\n") == hashlib.sha256(_KEY.encode()).hexdigest()


async def test_keys_are_listed_page_by_page(monkeypatch):
    page = [{**_KEY_OBJECT, "hash": f"{i:064x}"} for i in range(openrouter._KEYS_PAGE_SIZE)]

    def _pages(request):
        offset = int(request.url.params["offset"])
        return _json(200, {"data": page if offset == 0 else [_KEY_OBJECT, {"hash": "bad"}]})

    seen = _install(monkeypatch, _pages)

    listing = await openrouter.list_api_keys(_MANAGEMENT)

    assert len(listing.keys) == openrouter._KEYS_PAGE_SIZE + 1
    assert listing.truncated is False
    assert [r.url.params["offset"] for r in seen] == ["0", str(openrouter._KEYS_PAGE_SIZE)]
    assert seen[0].url.params["include_disabled"] == "true"


async def test_a_created_key_comes_with_its_secret(monkeypatch):
    seen = _install(monkeypatch, lambda request: _json(201, {"data": _KEY_OBJECT, "key": "sk-or-v1-new"}))

    created, secret = await openrouter.create_api_key(_MANAGEMENT, {"name": "Platform", "limit": 25})

    assert secret == "sk-or-v1-new"
    assert created["hash"] == _HASH
    assert (created["limit"], created["limit_reset"]) == (25.0, "weekly")
    assert json.loads(seen[0].content) == {"name": "Platform", "limit": 25}


async def test_a_create_answer_without_the_secret_is_invalid(monkeypatch):
    _install(monkeypatch, lambda request: _json(201, {"data": _KEY_OBJECT}))

    with pytest.raises(openrouter.OpenRouterError) as raised:
        await openrouter.create_api_key(_MANAGEMENT, {"name": "Platform"})
    assert raised.value.kind == "invalid_response"


async def test_update_and_delete_address_the_key_by_its_hash(monkeypatch):
    def _answer(request):
        if request.method == "PATCH":
            return _json(200, {"data": {**_KEY_OBJECT, "disabled": True}})
        return _json(200, {"deleted": True})

    seen = _install(monkeypatch, _answer)

    updated = await openrouter.update_api_key(_MANAGEMENT, _HASH, {"disabled": True})
    await openrouter.delete_api_key(_MANAGEMENT, _HASH)

    assert updated["disabled"] is True
    assert [(r.method, r.url.path) for r in seen] == [
        ("PATCH", f"/api/v1/keys/{_HASH}"),
        ("DELETE", f"/api/v1/keys/{_HASH}"),
    ]


async def test_an_unconfirmed_delete_is_invalid(monkeypatch):
    _install(monkeypatch, lambda request: _json(200, {"deleted": False}))

    with pytest.raises(openrouter.OpenRouterError):
        await openrouter.delete_api_key(_MANAGEMENT, _HASH)


@pytest.mark.parametrize("hash_", ["../credits", "F" * 64, "a" * 63, ""])
async def test_a_hash_that_is_not_one_never_reaches_a_url(monkeypatch, hash_):
    seen = _install(monkeypatch, lambda request: _json(200, {"deleted": True}))

    with pytest.raises(ValueError):
        await openrouter.delete_api_key(_MANAGEMENT, hash_)
    with pytest.raises(ValueError):
        await openrouter.update_api_key(_MANAGEMENT, hash_, {"disabled": True})
    assert seen == []
