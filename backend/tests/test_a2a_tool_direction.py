"""The agent tools give an A2A integration its direction, as the REST API does.

An A2A integration (``oauth_a2a``, ``entra_a2a``, ``google_a2a``) is valid only
with its ``a2a_integration_settings`` row: the row holds its direction, and an
integration without one can neither mint a partner credential nor delegate
(#11778, created by the ``create_integration`` tool with no row). Driven
through the tool executor with an in-memory session double.
"""

from __future__ import annotations

import uuid
from types import SimpleNamespace

import pytest

import app.a2a.providers as providers_module
import app.core.database as database
import app.services.builtin_tools as builtin_tools_module
from app.a2a.providers import ALL_A2A_PROVIDERS, requested_direction
from app.models.a2a import A2AClientTarget, A2AIntegrationSettings
from app.models.integration import Integration
from app.services.builtin_tools import build_agent_builder_tool
from app.tools.base import ToolCall
from tests.a2a_chat_gate import stub_chat_gate


class _Result:
    def __init__(self, value=None):
        self._value = value

    def scalar_one_or_none(self):
        return self._value

    def scalars(self):
        return self

    def all(self):
        return [] if self._value is None else list(self._value)


class _Session:
    """Records what a tool adds, flushes and commits; every read answers nothing."""

    def __init__(self, sessions):
        sessions.append(self)
        self.added: list[object] = []
        self.commits = 0
        self.rollbacks = 0

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_a):
        return False

    async def execute(self, _stmt):
        return _Result()

    def add(self, instance):
        self.added.append(instance)

    async def delete(self, _instance):
        return None

    async def flush(self):
        return None

    async def run_sync(self, _fn, *_a):
        # The exposure converge of an inbound row (#11786): no agent binds a
        # registration created a moment ago, so it exposes nothing.
        return 0

    async def commit(self):
        self.commits += 1

    async def rollback(self):
        self.rollbacks += 1


@pytest.fixture
def sessions(monkeypatch):
    opened: list[_Session] = []
    monkeypatch.setattr(database, "async_session", lambda: _Session(opened))
    caller = SimpleNamespace(id=uuid.uuid4(), role="user", is_admin=False)

    async def _caller(_db, _uid):
        return caller

    monkeypatch.setattr(builtin_tools_module, "_load_caller_user_for_builder", _caller)
    stub_chat_gate(monkeypatch)
    return opened


async def _call(tool, **arguments):
    _, executor = build_agent_builder_tool(SimpleNamespace(), str(uuid.uuid4()))
    return await executor.execute_tool(ToolCall(id=str(uuid.uuid4()), name=tool, arguments=arguments))


def _added(sessions, kind):
    return [obj for session in sessions for obj in session.added if isinstance(obj, kind)]


@pytest.mark.asyncio
@pytest.mark.parametrize("provider", sorted(ALL_A2A_PROVIDERS))
async def test_a_tool_created_a2a_integration_gets_an_inbound_settings_row(sessions, provider):
    result = await _call("create_integration", provider=provider, name="Partner")

    assert "created successfully" in result.content, result.content
    [integration] = _added(sessions, Integration)
    [row] = _added(sessions, A2AIntegrationSettings)
    assert row.integration_id == integration.id
    assert row.direction == "inbound"
    assert "A2A direction: inbound" in result.content
    assert sessions[-1].commits == 1


@pytest.mark.asyncio
async def test_a_tool_created_outbound_integration_syncs_its_card_urls(sessions):
    result = await _call(
        "create_integration",
        provider="oauth_a2a",
        name="Partner out",
        a2a_direction="outbound",
        extra_config='{"card_urls": "https://a.example/card.json, https://b.example/card.json"}',
    )

    assert "created successfully" in result.content, result.content
    [row] = _added(sessions, A2AIntegrationSettings)
    assert row.direction == "outbound"
    targets = _added(sessions, A2AClientTarget)
    assert sorted(t.card_url for t in targets) == ["https://a.example/card.json", "https://b.example/card.json"]


@pytest.mark.asyncio
async def test_a_direction_on_a_provider_that_is_not_a2a_is_refused(sessions):
    result = await _call("create_integration", provider="github", name="GH", a2a_direction="inbound")

    assert result.content == "Integration not created - a2a_direction applies only to A2A integrations."
    assert _added(sessions, Integration) == []


@pytest.mark.asyncio
async def test_an_unknown_direction_is_refused_before_anything_is_written(sessions):
    result = await _call("create_integration", provider="oauth_a2a", name="P", a2a_direction="sideways")

    assert "must be 'inbound' or 'outbound'" in result.content
    assert sessions == []


@pytest.mark.asyncio
async def test_an_inbound_federation_config_the_auth_path_would_refuse_is_refused(sessions):
    result = await _call("create_integration", provider="entra_a2a", name="Foundry", extra_config='{"tenant_id": "x"}')

    assert result.content.startswith("Integration not created - "), result.content
    assert _added(sessions, Integration) == []


@pytest.mark.asyncio
async def test_a_non_a2a_provider_gets_no_settings_row(sessions):
    await _call("create_integration", provider="github", name="GH", api_key="ghp_x")

    assert _added(sessions, A2AIntegrationSettings) == []


@pytest.mark.asyncio
async def test_a_tool_create_is_refused_while_chat_is_unavailable(sessions, monkeypatch):
    stub_chat_gate(monkeypatch, enabled=False)

    import app.api.deps as deps

    async def _refuse(_db):
        from fastapi import HTTPException

        raise HTTPException(status_code=403, detail="Chat is not available")

    monkeypatch.setattr(deps, "refuse_chat_unavailable", _refuse)
    result = await _call("create_integration", provider="oauth_a2a", name="P")

    assert result.content.startswith("Integration not created - Chat is not available")
    assert _added(sessions, Integration) == []


def _a2a_row(provider="oauth_a2a"):
    return SimpleNamespace(
        id=uuid.uuid4(),
        name="Partner",
        provider=provider,
        auth_type="none",
        is_builtin=False,
        user_id=uuid.uuid4(),
        extra_config_enc=None,
        authorize_url=None,
        token_url=None,
        tool_slug=None,
        credential_mode=None,
        oauth_share_acknowledged=None,
        relay_group_id=None,
    )


@pytest.mark.asyncio
async def test_update_changes_the_direction_through_change_direction(sessions, monkeypatch):
    integ = _a2a_row()

    async def _gate(_db, _model, _uid, _id, *, action="modify", entity_label="entity", lock=False):
        return integ, None

    calls: list[str] = []

    async def _get_settings(_db, _id):
        return SimpleNamespace(direction="inbound")

    async def _change(_db, integration, direction):
        calls.append(direction)

    async def _nothing(*_a, **_k):
        return None

    monkeypatch.setattr(builtin_tools_module, "_load_manageable_entity_for_builder", _gate)
    monkeypatch.setattr(providers_module, "get_settings", _get_settings)
    monkeypatch.setattr(providers_module, "change_direction", _change)
    monkeypatch.setattr("app.services.agent_accounts.listening.sync_integration_accounts", _nothing)

    result = await _call("update_integration", integration_id=str(integ.id), a2a_direction="outbound")

    assert calls == ["outbound"]
    assert "updated: a2a_direction" in result.content, result.content
    assert "A2A direction: outbound" in result.content


@pytest.mark.asyncio
async def test_update_keeps_the_refusal_while_bound(sessions, monkeypatch):
    integ = _a2a_row()

    async def _gate(_db, _model, _uid, _id, *, action="modify", entity_label="entity", lock=False):
        return integ, None

    async def _get_settings(_db, _id):
        return SimpleNamespace(direction="inbound")

    async def _change(_db, integration, direction):
        raise providers_module.DirectionChangeRefusedError(["Analyst"])

    monkeypatch.setattr(builtin_tools_module, "_load_manageable_entity_for_builder", _gate)
    monkeypatch.setattr(providers_module, "get_settings", _get_settings)
    monkeypatch.setattr(providers_module, "change_direction", _change)

    result = await _call("update_integration", integration_id=str(integ.id), a2a_direction="outbound")

    assert result.content.startswith("Integration not updated - Unbind this integration")
    assert "Analyst" in result.content
    assert all(s.commits == 0 for s in sessions)


@pytest.mark.asyncio
async def test_update_refuses_a_direction_on_a_provider_that_is_not_a2a(sessions, monkeypatch):
    integ = _a2a_row(provider="github")

    async def _gate(_db, _model, _uid, _id, *, action="modify", entity_label="entity", lock=False):
        return integ, None

    monkeypatch.setattr(builtin_tools_module, "_load_manageable_entity_for_builder", _gate)
    result = await _call("update_integration", integration_id=str(integ.id), a2a_direction="inbound")

    assert result.content == "Integration not updated - a2a_direction applies only to A2A integrations."


def test_both_tools_declare_the_direction_argument():
    definitions, _ = build_agent_builder_tool(SimpleNamespace(), str(uuid.uuid4()))
    by_name = {d.name: d for d in definitions}
    for name in ("create_integration", "update_integration"):
        schema = by_name[name].input_schema["properties"]["a2a_direction"]
        assert schema["enum"] == ["inbound", "outbound"]


@pytest.mark.parametrize(
    ("provider", "asked", "expected"),
    [
        ("oauth_a2a", None, "inbound"),
        ("oauth_a2a", "", "inbound"),
        ("entra_a2a", "outbound", "outbound"),
        ("google_a2a", " inbound ", "inbound"),
        ("github", None, None),
    ],
)
def test_requested_direction_defaults_inbound_for_every_a2a_provider(provider, asked, expected):
    assert requested_direction(provider, asked) == expected
