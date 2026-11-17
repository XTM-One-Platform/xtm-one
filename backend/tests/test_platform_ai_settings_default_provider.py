"""The platform default chat model and provider through ``update_platform_ai_settings``.

An administrator's agent watching the LLM providers moves the platform default
to another provider serving the model, through the handler of Settings > AI and
models > Models (``update_general_assistant_config``): the same validation,
the same audit entry with the values before and after, the same commit. The
read lists the configured providers serving the default model, the valid
targets. Driven through the real handler with a fake session.
"""

from __future__ import annotations

import asyncio
import json
import uuid
from types import SimpleNamespace

import pytest

ADMIN_ID = "00000000-0000-4000-8000-000000000001"


class _Db:
    """Just enough session for the tool and ``update_general_assistant_config``."""

    def __init__(self, ga, providers):
        self._ga = ga
        self._providers = providers
        self.committed = 0

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_exc):
        return False

    async def execute(self, _statement):
        return SimpleNamespace(
            scalar_one_or_none=lambda: self._ga,
            scalars=lambda: SimpleNamespace(all=lambda: self._providers),
            all=lambda: self._providers,
        )

    def add(self, _obj):
        pass

    async def refresh(self, _obj):
        pass

    async def flush(self):
        pass

    async def commit(self):
        self.committed += 1


def _ga():
    return SimpleNamespace(
        llm_provider=None,
        llm_provider_id=None,
        llm_model=None,
        linked_agent_id=None,
        max_iterations=25,
        system_prompt="p",
        knowledge_base_ids=[],
        mcp_server_ids=[],
        custom_tool_ids=[],
        builtin_tool_ids=[],
        integration_bindings={},
        handover_agent_ids=[],
        handover_types={},
        available_channel_types=["web"],
        channel_config={},
    )


def _ps(provider):
    return SimpleNamespace(
        agent_default_provider=provider.provider,
        agent_default_provider_id=provider.id,
        agent_default_model="claude-sonnet-4-6",
        default_reasoning_effort=None,
        maximum_wait_time=None,
        maximum_total_wait_time=None,
        wait_unbounded_with_until=None,
        tool_approvals_enabled=True,
        approve_tool_calls_default=False,
        embedding_provider=None,
        embedding_provider_id=None,
        embedding_model=None,
        embedding_dimensions=None,
        image_provider=None,
        image_provider_id=None,
        image_model=None,
        routing_model=None,
        routing_provider_id=None,
        fallback_model=None,
        fallback_provider_id=None,
        decision_model=None,
        decision_provider_id=None,
        mcp_exposed_agent_ids=[],
        platform_mcp_enabled=False,
        xtm_mcp_enabled=False,
    )


def _provider(provider, *models, display_name=None):
    return SimpleNamespace(
        id=uuid.uuid4(), provider=provider, display_name=display_name, available_models=list(models), created_at=None
    )


@pytest.fixture
def lab(monkeypatch):
    """Two providers serving one model, a third serving another, an admin by default."""
    from app.api.v1 import settings as settings_api
    from app.services.builtin_tools import platform_admin

    anthropic = _provider("anthropic", "claude-sonnet-4-6", display_name="Anthropic direct")
    bedrock = _provider("bedrock", "claude-sonnet-4-6", "claude-opus-4-8", display_name="Bedrock")
    openai = _provider("openai", "gpt-5", display_name="OpenAI")
    state = SimpleNamespace(
        ps=_ps(anthropic),
        db=_Db(_ga(), [anthropic, bedrock, openai]),
        admin=True,
        audits=[],
        anthropic=anthropic,
        bedrock=bedrock,
        openai=openai,
    )

    async def _admin(_db, _user_id):
        return SimpleNamespace(id=uuid.UUID(ADMIN_ID), email="admin@example.test", is_admin=True) if state.admin else None

    async def _load(_db):
        return state.ps

    async def _audit(_db, **kwargs):
        state.audits.append(kwargs)

    async def _no_problem(*_a, **_k):
        return None

    async def _catalog_check(_db, model, **_k):
        listed = {m for p in state.db._providers for m in p.available_models}
        return None if model in listed else f"Model '{model}' is not in any configured provider's available models."

    async def _no_announcement(*_a, **_k):
        return None

    monkeypatch.setattr("app.core.database.async_session", lambda: state.db)
    monkeypatch.setattr(platform_admin, "_admin", _admin)
    monkeypatch.setattr("app.api.v1.platform_settings_helpers.get_platform_settings", _load)
    monkeypatch.setattr(settings_api, "log_audit", _audit)
    monkeypatch.setattr(settings_api, "provider_reference_problem", _no_problem)
    monkeypatch.setattr("app.llm.router.validate_selectable_chat_model", _catalog_check)
    monkeypatch.setattr(
        "app.services.model_catalog_announcements.record_default_model_change", _no_announcement
    )
    monkeypatch.setattr("app.core.config.settings.default_model", "", raising=False)
    monkeypatch.setattr("app.core.config.settings.byollm_activated", False, raising=False)
    monkeypatch.setattr("app.core.config.settings.default_reasoning_effort", "", raising=False)
    _defs, handlers = platform_admin.build_platform_admin_tools(ADMIN_ID)
    state.update = lambda **kw: asyncio.run(handlers["update_platform_ai_settings"](**kw))
    state.read = lambda: asyncio.run(handlers["get_platform_ai_settings"]())
    return state


def test_the_read_lists_the_providers_serving_the_default_model(lab):
    default = json.loads(lab.read())["default_model"]
    assert default["agent_default_model"] == "claude-sonnet-4-6"
    assert default["agent_default_provider_id"] == str(lab.anthropic.id)
    assert [(p["provider"], p["current"]) for p in default["serving_providers"]] == [
        ("anthropic", True),
        ("bedrock", False),
    ]
    assert default["env_locked"] is False
    assert "Read-only here" not in default["note"]


def test_an_admin_switches_between_two_providers_serving_the_model(lab):
    answer = json.loads(lab.update(agent_default_provider=str(lab.bedrock.id)))
    assert lab.ps.agent_default_provider == "bedrock"
    assert lab.ps.agent_default_provider_id == lab.bedrock.id
    assert lab.ps.agent_default_model == "claude-sonnet-4-6"
    assert lab.db.committed == 1
    # The answer is the value read back after the write.
    assert answer["settings"]["default_model"]["agent_default_provider_id"] == str(lab.bedrock.id)
    assert answer["changed"]["agent_default_provider"] == {"before": "anthropic", "after": "bedrock"}
    assert "agent_default_model" not in answer["changed"]
    # And back, by provider key this time.
    json.loads(lab.update(agent_default_provider="anthropic"))
    assert lab.ps.agent_default_provider_id == lab.anthropic.id
    assert lab.db.committed == 2


def test_the_switch_is_audited_like_the_settings_page_with_before_and_after(lab):
    lab.update(agent_default_provider=str(lab.bedrock.id))
    (entry,) = lab.audits
    assert entry["entity_type"] == "general_assistant"
    assert entry["action"] == "configure"
    changed = entry["details"]["model_defaults_changed"]
    assert changed["agent_default_provider_id"] == {"before": str(lab.anthropic.id), "after": str(lab.bedrock.id)}
    assert changed["agent_default_provider"] == {"before": "anthropic", "after": "bedrock"}
    assert "agent_default_provider_id" in entry["summary"]


def test_a_model_and_its_provider_change_together(lab):
    answer = json.loads(lab.update(agent_default_provider="bedrock", agent_default_model="claude-opus-4-8"))
    assert (lab.ps.agent_default_provider, lab.ps.agent_default_model) == ("bedrock", "claude-opus-4-8")
    assert answer["changed"]["agent_default_model"] == {"before": "claude-sonnet-4-6", "after": "claude-opus-4-8"}


def test_a_provider_not_configured_is_refused_and_nothing_written(lab):
    answer = lab.update(agent_default_provider="mistral")
    assert answer.startswith("Error:") and "no configured LLM provider is named 'mistral'" in answer
    answer = lab.update(agent_default_provider=str(uuid.uuid4()))
    assert answer.startswith("Error:")
    assert lab.ps.agent_default_provider_id == lab.anthropic.id
    assert lab.db.committed == 0 and lab.audits == []


def test_a_provider_that_does_not_serve_the_model_is_refused(lab):
    answer = lab.update(agent_default_provider=str(lab.openai.id))
    assert answer.startswith("Error:") and "does not serve the model 'claude-sonnet-4-6'" in answer
    assert lab.ps.agent_default_provider == "anthropic"
    assert lab.db.committed == 0 and lab.audits == []


def test_a_default_locked_by_default_model_is_refused_naming_the_variable(lab, monkeypatch):
    monkeypatch.setattr("app.core.config.settings.default_model", "claude-sonnet-4-6", raising=False)
    answer = lab.update(agent_default_provider=str(lab.bedrock.id))
    assert answer.startswith("Error 403") and "DEFAULT_MODEL" in answer
    assert lab.ps.agent_default_provider == "anthropic"
    assert lab.db.committed == 0 and lab.audits == []
    assert json.loads(lab.read())["default_model"]["env_locked"] is True


def test_a_non_admin_is_refused_with_the_403_sentence(lab):
    lab.admin = False
    assert lab.update(agent_default_provider=str(lab.bedrock.id)).startswith("Error 403")
    assert lab.ps.agent_default_provider == "anthropic"
    assert lab.db.committed == 0 and lab.audits == []


def test_mixed_with_another_setting_it_is_refused(lab):
    answer = lab.update(agent_default_provider="bedrock", maximum_wait_time=60)
    assert answer.startswith("Error:") and "a call of their own" in answer
    assert lab.ps.agent_default_provider == "anthropic" and lab.ps.maximum_wait_time is None


def test_a_caller_passing_none_of_the_new_fields_sees_no_change(lab):
    answer = json.loads(lab.update(approve_tool_calls_default=True, agent_default_provider=None))
    assert answer["changed"] == {"approve_tool_calls_default": True}
    assert lab.ps.agent_default_provider == "anthropic"
    assert all(entry["entity_type"] == "platform_settings" for entry in lab.audits)


def test_the_update_schema_names_both_fields():
    from app.services.builtin_tools import platform_admin

    defs, _handlers = platform_admin.build_platform_admin_tools(ADMIN_ID)
    schema = {d.name: d for d in defs}["update_platform_ai_settings"].input_schema["properties"]
    assert schema["agent_default_provider"]["type"] == ["string", "null"]
    assert schema["agent_default_model"]["type"] == ["string", "null"]
