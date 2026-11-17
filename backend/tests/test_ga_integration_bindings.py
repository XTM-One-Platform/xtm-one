"""A user's General Assistant integration bindings follow what they may see.

``PUT /settings/preferences`` stored ``ga_integration_bindings`` as sent. The
write now refuses an id it ADDS that the user cannot see; the platform's own
General Assistant bindings are the admin's, and stay. A turn uses the saved
bindings as saved, as it always did: nothing is withheld at run time
(maintainer decision of 2026-09-27).

Visibility is stubbed here to "own only"; the real three-tier filter is run by
PostgreSQL in ``test_ga_integration_bindings_postgres.py``.
"""

from __future__ import annotations

import uuid
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

import app.api.v1.helpers as helpers
import app.api.v1.settings as settings_api
from app.models.general_assistant_config import GeneralAssistantConfig
from app.models.integration import Integration
from app.services import integration_credentials
from app.services.integration_credentials import flatten_binding_ids
from tests.integration_credential_fakes import (
    COLLEAGUE_ID,
    OWNER_ID,
    FakeSession,
    make_integration,
)


@pytest.fixture(autouse=True)
def _own_only(monkeypatch):
    """A user sees exactly the integrations they own."""

    async def _no_groups(*_a, **_k):
        return []

    monkeypatch.setattr(
        helpers,
        "user_visible_filter",
        lambda model, user_id, *_a, **_k: model.user_id == user_id,
    )
    monkeypatch.setattr(helpers, "get_user_group_ids", _no_groups)


def test_flatten_binding_ids_ignores_junk():
    assert flatten_binding_ids(None) == []
    assert flatten_binding_ids({"a": ["x", "", 3], "b": "y"}) == ["x", "y"]


# ---------------------------------------------------------------------------
# The write: an id a request adds must be one the user can see
# ---------------------------------------------------------------------------


def _user(**overrides) -> SimpleNamespace:
    fields = {"id": COLLEAGUE_ID, "ga_integration_bindings": None, "role": "user"}
    fields.update(overrides)
    return SimpleNamespace(**fields)


def _db(*integrations, platform_bindings=None) -> FakeSession:
    ga = GeneralAssistantConfig(integration_bindings=platform_bindings or {})
    return FakeSession({Integration: list(integrations), GeneralAssistantConfig: [ga]})


async def test_adding_an_integration_the_user_cannot_see_is_refused():
    theirs = make_integration(provider="jira", auth_type="api_key")

    with pytest.raises(HTTPException) as exc:
        await settings_api._checked_ga_integration_bindings(
            _db(theirs), _user(), {"jira": [str(theirs.id)]}
        )

    assert exc.value.status_code == 403
    assert str(theirs.id) in exc.value.detail


@pytest.mark.parametrize("role", ["admin", "user"])
async def test_an_admin_is_held_to_the_same_rule(role):
    """The picker lists an admin's visible integrations only (no admin bypass)."""
    theirs = make_integration(provider="jira", auth_type="api_key")

    with pytest.raises(HTTPException):
        await settings_api._checked_ga_integration_bindings(
            _db(theirs), _user(role=role), {"jira": [str(theirs.id)]}
        )


async def test_a_missing_integration_is_refused_like_an_invisible_one():
    gone = str(uuid.uuid4())

    with pytest.raises(HTTPException) as exc:
        await settings_api._checked_ga_integration_bindings(_db(), _user(), {"jira": [gone]})

    assert exc.value.status_code == 403


@pytest.mark.parametrize("value", ["not-a-uuid", ""])
async def test_a_malformed_id_is_a_client_error(value):
    with pytest.raises(HTTPException) as exc:
        await settings_api._checked_ga_integration_bindings(_db(), _user(), {"jira": [value]})

    assert exc.value.status_code == 422


@pytest.mark.parametrize("stored_junk", ["not-a-uuid", ""])
async def test_a_malformed_value_already_saved_never_blocks_a_toggle(stored_junk):
    """Main stored the map as sent. The Profile page re-sends the whole map on
    every toggle, so a malformed value already saved must not 422 the save of
    the rest: it is dropped, and the toggled integration is saved."""
    mine = make_integration(user_id=COLLEAGUE_ID, provider="github", auth_type="api_key")
    user = _user(ga_integration_bindings={"jira": [stored_junk]})

    result = await settings_api._checked_ga_integration_bindings(
        _db(mine), user, {"jira": [stored_junk], "github": [str(mine.id)]}
    )

    assert result == {"jira": [], "github": [str(mine.id)]}


async def test_own_saved_and_platform_ids_are_accepted_and_canonicalised():
    mine = make_integration(user_id=COLLEAGUE_ID, provider="github", auth_type="api_key")
    saved = make_integration(provider="slack", auth_type="api_key")  # un-shared since saved
    platform = make_integration(user_id=OWNER_ID, provider="jira", auth_type="api_key")
    user = _user(ga_integration_bindings={"slack": [str(saved.id)]})

    result = await settings_api._checked_ga_integration_bindings(
        _db(mine, saved, platform, platform_bindings={"jira": [str(platform.id)]}),
        user,
        {
            "github": [str(mine.id).upper()],
            "slack": [str(saved.id)],
            "jira": [str(platform.id).replace("-", "")],
        },
    )

    assert result == {
        "github": [str(mine.id)],
        "slack": [str(saved.id)],
        "jira": [str(platform.id)],
    }


async def test_resetting_to_the_platform_defaults_is_not_checked():
    assert await settings_api._checked_ga_integration_bindings(_db(), _user(), None) is None


# ---------------------------------------------------------------------------
# Wiring: which bindings a turn filters
# ---------------------------------------------------------------------------


SESSION_ID = str(uuid.uuid4())


class _StopError(BaseException):
    """Raised from the patched router once the bindings were resolved."""


def _ga_row(**overrides) -> SimpleNamespace:
    row = SimpleNamespace(
        llm_provider=None,
        llm_provider_id=None,
        llm_model=None,
        linked_agent_id=None,
        system_prompt="p",
        knowledge_base_ids=[],
        mcp_server_ids=[],
        custom_tool_ids=[],
        builtin_tool_ids=[],
        skill_ids=[],
        integration_bindings={},
        handover_agent_ids=[],
        handover_types={},
        available_channel_types=["web"],
        max_iterations=None,
    )
    for key, value in overrides.items():
        setattr(row, key, value)
    return row


def _user_row(**overrides) -> SimpleNamespace:
    row = SimpleNamespace(
        ga_builtin_tool_ids=None,
        ga_custom_tool_ids=None,
        ga_integration_bindings=None,
        ga_mcp_server_ids=None,
        ga_knowledge_base_ids=None,
    )
    for key, value in overrides.items():
        setattr(row, key, value)
    return row


async def _integration_context_of_a_turn(monkeypatch, ga_row, user_row, **kwargs) -> dict:
    """The context the turn hands to the integration tool build."""
    from app.services import general_chat, integration_tools

    captured: dict = {}

    async def _session(*_a, **_k):
        return {"github": [SESSION_ID]}

    async def _no_quota(*_a, **_k):
        return None

    async def _build(_db, bindings, *, context=None, channel_bot_mode=False):
        captured.update(bindings=bindings, context=context)
        raise _StopError

    rows = [ga_row, user_row]

    class _Db:
        async def execute(self, _statement):
            return SimpleNamespace(scalar_one_or_none=lambda: rows.pop(0))

    monkeypatch.setattr(integration_credentials, "resolve_session_integration_bindings", _session)
    monkeypatch.setattr("app.services.agent_chat.enforce_agentic_quota", _no_quota)
    monkeypatch.setattr("app.services.builtin_tools.build_identity_context", lambda *_a, **_k: [])
    monkeypatch.setattr(integration_tools, "build_integration_tools", _build)

    async def _platform_wait_policy(_db):
        from app.tools.wait_policy import WaitPolicy

        return WaitPolicy()

    monkeypatch.setattr("app.services.wait_settings.load_platform_wait_policy", _platform_wait_policy)

    with pytest.raises(_StopError):
        await general_chat.run_general_chat(
            _Db(), str(uuid.uuid4()), [], "hello", prebuilt_llm=(object(), "m"), **kwargs
        )
    return captured


async def test_worked_on_main_a_turn_uses_the_user_s_saved_bindings_as_saved(monkeypatch):
    """A saved id the user can no longer see still reaches the tool build."""
    platform_id, unshared_id = str(uuid.uuid4()), str(uuid.uuid4())

    captured = await _integration_context_of_a_turn(
        monkeypatch,
        _ga_row(integration_bindings={"jira": [platform_id]}),
        _user_row(ga_integration_bindings={"jira": [platform_id], "notion": [unshared_id]}),
    )

    assert captured["bindings"] == {"jira": [platform_id], "notion": [unshared_id]}
    assert "binding_entity" not in captured["context"]
    assert "admin_managed_binding_ids" not in captured["context"]


async def test_a_turn_on_the_platform_bindings_uses_them_as_configured(monkeypatch):
    platform_id = str(uuid.uuid4())

    captured = await _integration_context_of_a_turn(
        monkeypatch, _ga_row(integration_bindings={"jira": [platform_id]}), _user_row()
    )

    assert captured["bindings"] == {"jira": [platform_id]}


async def test_a_session_override_goes_through_its_own_resolver(monkeypatch):
    captured = await _integration_context_of_a_turn(
        monkeypatch,
        _ga_row(integration_bindings={"jira": [str(uuid.uuid4())]}),
        _user_row(ga_integration_bindings={"github": [str(uuid.uuid4())]}),
        session_integration_ids=[SESSION_ID],
    )

    assert captured["bindings"] == {"github": [SESSION_ID]}
