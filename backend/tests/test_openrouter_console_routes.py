"""The OpenRouter console routes without a database: their gates, their answers to OpenRouter's refusals.

The permission matrix and the platform provider run on a real PostgreSQL in
``test_openrouter_console_postgres.py``; the client in ``test_openrouter_client.py``.
"""

from __future__ import annotations

import uuid
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from fastapi.routing import APIRoute
from pydantic import ValidationError

from app.api.deps import require_admin, require_ai_enabled
from app.api.v1 import integration_testing
from app.api.v1 import openrouter as console
from app.api.v1.integrations import _is_sensitive_setting_name
from app.api.v1.schemas.openrouter import LinkProviderRequest, ProviderModelsRequest
from app.integrations import lifecycle
from app.integrations.providers import openrouter as client
from app.services import openrouter_llm_provider as llm_link

_ADMIN = SimpleNamespace(id=uuid.uuid4(), role="admin", is_admin=True)


def _dependency_calls(route: APIRoute) -> set:
    calls, stack = set(), list(route.dependant.dependencies)
    while stack:
        dependency = stack.pop()
        calls.add(dependency.call)
        stack.extend(dependency.dependencies)
    return calls


def _routes() -> list[APIRoute]:
    return [route for route in console.router.routes if isinstance(route, APIRoute)]


def test_every_console_route_calls_openrouter_only_with_ai_on():
    routes = _routes()
    assert len(routes) == 11
    for route in routes:
        assert require_ai_enabled in _dependency_calls(route), route.path


def test_feeding_the_platform_provider_is_for_admins():
    admin_only = {
        (method, route.path)
        for route in _routes()
        for method in route.methods
        if require_admin in _dependency_calls(route)
    }
    assert admin_only == {
        ("POST", "/{integration_id}/openrouter/llm-provider"),
        ("PUT", "/{integration_id}/openrouter/llm-provider/{config_id}/models"),
        ("DELETE", "/{integration_id}/openrouter/llm-provider/{config_id}"),
    }


def _integration(*, management_key: bool = True) -> SimpleNamespace:
    from app.core.security import encrypt_value

    extra = '{"management_key": "sk-or-v1-mgmt"}' if management_key else "{}"
    return SimpleNamespace(
        id=uuid.uuid4(),
        provider="openrouter",
        name="OpenRouter",
        user_id=None,
        is_company_managed=True,
        api_key_enc=encrypt_value("sk-or-v1-key"),
        extra_config_enc=encrypt_value(extra),
    )


@pytest.fixture
def loaded(monkeypatch):
    integration = _integration()

    async def _load(db, user, integration_id):
        return integration

    monkeypatch.setattr(console, "_load", _load)
    return integration


@pytest.mark.parametrize(
    ("kind", "status"),
    [
        ("auth", 424),
        ("management_key_required", 424),
        ("invalid_request", 424),
        ("unavailable", 424),
        ("invalid_response", 424),
        ("rate_limited", 429),
        ("not_found", 404),
    ],
)
async def test_an_openrouter_refusal_is_answered_with_its_fixed_sentence(monkeypatch, loaded, kind, status):
    async def _refused(*args, **kwargs):
        raise client.OpenRouterError(kind, client._PUBLIC_MESSAGES[kind], detail="upstream: acct_42 suspended")

    monkeypatch.setattr(client, "cached_models", _refused)

    with pytest.raises(HTTPException) as raised:
        await console.get_models(str(loaded.id), kind="chat", user=_ADMIN, db=None, _ai_gate=None)

    # Never 401 (the web client would sign the user out) nor 502 (it would
    # show the backend as down).
    assert raised.value.status_code == status
    assert raised.value.detail == {"code": f"openrouter_{kind}", "message": client._PUBLIC_MESSAGES[kind]}


async def test_a_management_route_without_a_management_key_asks_for_one(monkeypatch):
    integration = _integration(management_key=False)

    async def _load(db, user, integration_id):
        return integration

    async def _never(*args, **kwargs):
        raise AssertionError("OpenRouter must not be called")

    monkeypatch.setattr(console, "_load", _load)
    monkeypatch.setattr(client, "get_activity", _never)

    with pytest.raises(HTTPException) as raised:
        await console.get_activity(str(integration.id), date=None, key_hash=None, user=_ADMIN, db=None, _ai_gate=None)

    assert raised.value.status_code == 409
    assert raised.value.detail["code"] == "openrouter_management_key_missing"


def test_the_management_key_is_redacted_like_every_secret():
    assert _is_sensitive_setting_name(client.MANAGEMENT_KEY_FIELD) is True


def test_importing_the_integrations_api_registers_the_key_follower():
    # The import in app/api/v1/integrations.py binds no name: an auto-fix that
    # removes it would silently stop copying a replaced key.
    import app.api.v1.integrations  # noqa: F401

    assert lifecycle._ON_UPDATE["openrouter"] is llm_link._on_integration_update


# ---------------------------------------------------------------------------
# Test Connection
# ---------------------------------------------------------------------------


async def test_test_connection_verifies_both_keys(monkeypatch):
    seen: list[tuple[str, str]] = []

    async def _key(api_key):
        seen.append(("key", api_key))
        return {}

    async def _credits(management_key):
        seen.append(("credits", management_key))
        return {}

    monkeypatch.setattr(client, "get_key_info", _key)
    monkeypatch.setattr(client, "get_credits", _credits)

    result = await integration_testing.test_provider_connectivity(
        "openrouter", " sk-or-v1-key ", {"management_key": "sk-or-v1-mgmt"}, "OpenRouter"
    )

    assert result == {"status": "ok", "message": "OpenRouter is connected (API key + management key verified)"}
    assert seen == [("key", "sk-or-v1-key"), ("credits", "sk-or-v1-mgmt")]


async def test_test_connection_without_a_management_key_checks_the_api_key(monkeypatch):
    async def _key(api_key):
        return {}

    async def _never(*args):
        raise AssertionError("no management key to check")

    monkeypatch.setattr(client, "get_key_info", _key)
    monkeypatch.setattr(client, "get_credits", _never)

    result = await integration_testing.test_provider_connectivity("openrouter", "sk-or-v1-key", {}, "OpenRouter")

    assert result == {"status": "ok", "message": "OpenRouter is connected (API key verified)"}


@pytest.mark.parametrize(
    ("failing", "kind", "message"),
    [
        ("key", "auth", "The API key was refused - it may be invalid, revoked or expired"),
        ("credits", "management_key_required", "The management key was refused - it may be invalid, revoked or expired"),
        ("key", "unavailable", f"OpenRouter: {client._PUBLIC_MESSAGES['unavailable']}"),
    ],
)
async def test_test_connection_names_the_refused_key(monkeypatch, failing, kind, message):
    async def _call(*args):
        if failing == _call.name:
            raise client.OpenRouterError(kind, client._PUBLIC_MESSAGES[kind], detail="secret upstream text")
        return {}

    async def _key(api_key):
        _call.name = "key"
        return await _call()

    async def _credits(management_key):
        _call.name = "credits"
        return await _call()

    monkeypatch.setattr(client, "get_key_info", _key)
    monkeypatch.setattr(client, "get_credits", _credits)

    result = await integration_testing.test_provider_connectivity(
        "openrouter", "sk-or-v1-key", {"management_key": "sk-or-v1-mgmt"}, "OpenRouter"
    )

    assert result == {"status": "error", "message": message}


async def test_test_connection_without_an_api_key_says_so():
    assert await integration_testing.test_provider_connectivity("openrouter", "  ", {}, "OpenRouter") == {
        "status": "error",
        "message": "Missing API key",
    }


# ---------------------------------------------------------------------------
# The models a save sends
# ---------------------------------------------------------------------------


def test_a_save_takes_back_every_model_settings_accepted():
    # A provider adopted from Settings keeps its models, and a save sends them
    # back: an OpenRouter alias, a bare id typed in Settings.
    body = ProviderModelsRequest(
        available_models=["openai/gpt-4o", "~google/gemini-pro-latest", "gpt-4o"],
        default_model="~google/gemini-pro-latest",
    )

    assert body.available_models == ["openai/gpt-4o", "~google/gemini-pro-latest", "gpt-4o"]
    assert body.default_model == "~google/gemini-pro-latest"


@pytest.mark.parametrize(
    "body",
    [
        {"available_models": [""]},
        {"available_models": ["   "]},
        {"available_models": ["m" * 256]},
        {"available_models": [None]},
        {"available_models": ["openai/gpt-4o", "openai/gpt-4o"]},
        {"available_models": ["openai/gpt-4o"], "default_model": "  "},
        {"available_models": ["openai/gpt-4o"], "default_model": "~google/gemini-pro-latest"},
    ],
    ids=["empty", "blank", "too-long", "null", "twice", "blank-default", "default-not-offered"],
)
def test_a_save_that_is_not_a_model_list_is_refused_by_the_body(body):
    with pytest.raises(ValidationError):
        ProviderModelsRequest.model_validate(body)


def test_a_new_provider_still_takes_openrouter_ids_only():
    assert LinkProviderRequest(available_models=["~google/gemini-pro-latest"]).available_models == [
        "~google/gemini-pro-latest"
    ]
    with pytest.raises(ValidationError):
        LinkProviderRequest(available_models=["gpt-4o"])


# ---------------------------------------------------------------------------
# Context windows of the models a save enables
# ---------------------------------------------------------------------------

_CATALOG = {
    "a/one": {"context_length": 100},
    "a/two": {"context_length": 200},
    "a/three": {"context_length": None},
}


def test_a_newly_enabled_model_gets_openrouter_s_window():
    assert llm_link.merged_context_windows(None, [], ["a/one", "a/two"], _CATALOG) == {"a/one": 100, "a/two": 200}


def test_a_window_already_set_is_kept_and_an_old_model_is_not_refilled():
    assert llm_link.merged_context_windows(
        {"a/one": 64}, ["a/one", "a/two"], ["a/one", "a/two", "a/three", "x/unknown"], _CATALOG
    ) is None
    assert llm_link.merged_context_windows({"a/one": 64}, ["a/one"], ["a/one", "a/two"], _CATALOG) == {
        "a/one": 64,
        "a/two": 200,
    }


def test_a_stored_window_that_is_not_a_size_is_dropped_only_when_the_map_is_rewritten():
    assert llm_link.merged_context_windows(
        {"a/one": 64, "a/bad": "big", "a/zero": 0, "a/flag": True}, [], ["a/two"], _CATALOG
    ) == {"a/one": 64, "a/two": 200}


def test_a_stored_window_keyed_by_no_model_id_is_dropped_so_the_update_validates():
    from app.api.v1.schemas.settings import LLMProviderUpdateRequest

    stored = {"": 64_000, "   ": 1, "m" * 256: 2, "a/one": 64}
    windows = llm_link.merged_context_windows(stored, ["a/one"], ["a/one", "a/two"], _CATALOG)

    assert windows == {"a/one": 64, "a/two": 200}
    assert LLMProviderUpdateRequest(context_windows=windows).context_windows == windows


def test_a_listed_window_past_the_platform_bound_is_not_stored():
    catalog = {"a/huge": {"context_length": 100_000_001}, "a/text": {"context_length": "128K"}}
    assert llm_link.merged_context_windows(None, [], ["a/huge", "a/text"], catalog) is None
    assert llm_link.merged_context_windows(
        {"a/huge": 100_000_001}, [], ["a/two"], _CATALOG
    ) == {"a/two": 200}


# ---------------------------------------------------------------------------
# Relay routing: the console's calls leave the way the integration's do
# ---------------------------------------------------------------------------


async def test_every_console_call_runs_on_the_integration_s_relay_route(monkeypatch, loaded):
    from app.core.egress import EgressRoute, current_route

    route = EgressRoute(group_id=uuid.uuid4(), source_type="integration", source_id=loaded.id)
    asked: list = []

    async def _integration_route(db, integration):
        asked.append(integration)
        return route

    monkeypatch.setattr("app.services.relays.integration_egress_route", _integration_route)
    seen: dict[str, object] = {}

    def _recorder(name, answer):
        async def _call(*args, **kwargs):
            seen[name] = current_route()
            return answer
        return _call

    monkeypatch.setattr(client, "get_key_info", _recorder("key", {"label": "k"}))
    monkeypatch.setattr(client, "get_credits", _recorder("credits", {"total_credits": 1.0, "total_usage": 0.0, "remaining": 1.0}))
    monkeypatch.setattr(client, "cached_models", _recorder("models", []))
    monkeypatch.setattr(client, "list_model_endpoints", _recorder("endpoints", []))
    monkeypatch.setattr(client, "get_activity", _recorder("activity", []))
    monkeypatch.setattr(client, "list_api_keys", _recorder("keys", client.KeyListing(keys=[], truncated=False)))

    async def _no_uses(db, integration):
        return {}

    async def _no_providers(db, integration_id):
        return []

    monkeypatch.setattr(console, "_keys_in_use", _no_uses)
    monkeypatch.setattr(llm_link, "linked_configs", _no_providers)
    monkeypatch.setattr(llm_link, "adoptable_configs", lambda db: _no_providers(db, None))

    iid = str(loaded.id)
    await console.get_overview(iid, user=_ADMIN, db=None, _ai_gate=None)
    await console.get_models(iid, kind="chat", user=_ADMIN, db=None, _ai_gate=None)
    await console.get_model_endpoints(iid, model_id="openai/gpt-4o", user=_ADMIN, db=None, _ai_gate=None)
    await console.get_activity(iid, date=None, key_hash=None, user=_ADMIN, db=None, _ai_gate=None)
    await console.list_keys(iid, user=_ADMIN, db=None, _ai_gate=None)

    assert seen == {name: route for name in ("key", "credits", "models", "endpoints", "activity", "keys")}
    assert asked == [loaded] * 5
    # Outside the console's calls the route is gone again.
    assert current_route() is None
