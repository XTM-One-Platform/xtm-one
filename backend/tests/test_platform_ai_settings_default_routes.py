"""Every default model route through ``update_platform_ai_settings`` ``default_routes``.

An administrator's agent moves any default of Settings > AI and models > Models
(agent default, routing, fallback, decision, image, embedding) to another
provider, through the Settings page handler (``update_general_assistant_config``):
the same validation, its audit entry, its commit, plus an entry naming the
agent run. Refusals write nothing. Driven through the real handler with the
fake session of ``test_platform_ai_settings_default_provider``.
"""

from __future__ import annotations

import json
import uuid

import pytest

from tests.test_platform_ai_settings_default_provider import ADMIN_ID, lab  # noqa: F401 - the fixture


@pytest.fixture
def routes_lab(lab, monkeypatch):  # noqa: F811 - the imported fixture
    """The lab with a routing, an image and an embedding default set."""
    from app.services.builtin_tools import platform_admin

    lab.openai.available_models += ["gpt-image-1", "text-embedding-3-small"]
    lab.bedrock.available_models += ["gpt-image-1", "text-embedding-3-small"]
    lab.ps.routing_model, lab.ps.routing_provider_id = "claude-sonnet-4-6", lab.anthropic.id
    lab.ps.image_provider, lab.ps.image_provider_id, lab.ps.image_model = "openai", lab.openai.id, "gpt-image-1"
    lab.ps.embedding_provider, lab.ps.embedding_provider_id = "openai", lab.openai.id
    lab.ps.embedding_model, lab.ps.embedding_dimensions = "text-embedding-3-small", 1536
    for var in (
        "default_routing_model",
        "default_fallback_model",
        "default_embedding_provider",
        "default_embedding_model",
        "default_image_provider",
        "default_image_model",
        "default_general_assistant_model",
    ):
        monkeypatch.setattr(f"app.core.config.settings.{var}", "", raising=False)
    monkeypatch.setattr("app.core.config.settings.default_embedding_dimensions", None, raising=False)
    monkeypatch.setattr("app.llm.env_capability_defaults.env_locked_fields", lambda: set())

    async def _audit(_db, **kwargs):
        lab.audits.append(kwargs)

    # The tool's own entry (the page's goes through settings_api.log_audit, patched by lab).
    monkeypatch.setattr("app.services.audit.log_audit", _audit)
    _defs, handlers = platform_admin.build_platform_admin_tools(ADMIN_ID, assignment_run_id="run-1")
    import asyncio

    lab.update = lambda **kw: asyncio.run(handlers["update_platform_ai_settings"](**kw))
    lab.read = lambda: asyncio.run(handlers["get_platform_ai_settings"]())
    return lab


def test_the_read_lists_every_route_with_provider_id_key_model_and_env_flag(routes_lab):
    routes = json.loads(routes_lab.read())["default_routes"]
    assert set(routes) == {"agent_default", "routing", "fallback", "decision", "image", "embedding"}
    assert routes["routing"] == {
        "provider_id": str(routes_lab.anthropic.id),
        "provider": "anthropic",
        "model": "claude-sonnet-4-6",
        "env_managed": False,
    }
    assert routes["embedding"]["dimensions"] == 1536
    assert routes["fallback"]["provider_id"] is None
    # No credential field in any route.
    assert all(set(r) <= {"provider_id", "provider", "model", "env_managed", "dimensions"} for r in routes.values())


def test_an_admin_moves_the_agent_default_and_reads_it_back(routes_lab):
    answer = json.loads(routes_lab.update(default_routes={"agent_default": {"provider": str(routes_lab.bedrock.id)}}))
    assert routes_lab.ps.agent_default_provider_id == routes_lab.bedrock.id
    assert routes_lab.ps.agent_default_model == "claude-sonnet-4-6"
    assert answer["changed"]["agent_default"]["after"]["provider"] == "bedrock"
    assert answer["default_routes"]["agent_default"]["provider_id"] == str(routes_lab.bedrock.id)
    read = json.loads(routes_lab.read())["default_routes"]["agent_default"]
    assert read["provider_id"] == str(routes_lab.bedrock.id)


def test_the_move_is_audited_by_the_page_and_names_the_agent_run(routes_lab):
    routes_lab.update(default_routes={"routing": {"provider": "bedrock"}})
    page, tool = routes_lab.audits
    assert page["entity_type"] == "general_assistant"
    assert page["details"]["model_defaults_changed"]["routing_provider_id"]["after"] == str(routes_lab.bedrock.id)
    assert tool["entity_name"] == "Default model routes"
    assert tool["details"]["assignment_run_id"] == "run-1"
    assert tool["details"]["changed"]["routing"]["before"]["provider_id"] == str(routes_lab.anthropic.id)


def test_the_image_route_moves_with_its_provider_key(routes_lab):
    routes_lab.update(default_routes={"image": {"provider": "bedrock"}})
    assert (routes_lab.ps.image_provider, routes_lab.ps.image_provider_id) == ("bedrock", routes_lab.bedrock.id)
    assert routes_lab.ps.image_model == "gpt-image-1"


def test_the_embedding_route_moves_provider_only(routes_lab):
    routes_lab.update(default_routes={"embedding": {"provider": "bedrock"}})
    assert routes_lab.ps.embedding_provider_id == routes_lab.bedrock.id
    assert (routes_lab.ps.embedding_model, routes_lab.ps.embedding_dimensions) == ("text-embedding-3-small", 1536)


@pytest.mark.parametrize(
    "wanted",
    [
        {"provider": "bedrock", "model": "text-embedding-3-large"},
        {"provider": "bedrock", "dimensions": 3072},
    ],
)
def test_an_embedding_model_or_dimension_change_is_refused(routes_lab, wanted):
    answer = routes_lab.update(default_routes={"embedding": wanted})
    assert answer.startswith("Error:") and "provider only" in answer
    assert routes_lab.ps.embedding_provider_id == routes_lab.openai.id
    assert routes_lab.db.committed == 0 and routes_lab.audits == []


def test_an_unknown_provider_is_refused_naming_it(routes_lab):
    for wanted in ("mistral", str(uuid.uuid4())):
        answer = routes_lab.update(default_routes={"routing": {"provider": wanted}})
        assert answer.startswith("Error:") and "no configured LLM provider is named" in answer
    assert routes_lab.ps.routing_provider_id == routes_lab.anthropic.id
    assert routes_lab.db.committed == 0 and routes_lab.audits == []


def test_a_provider_not_serving_the_model_is_refused(routes_lab):
    answer = routes_lab.update(default_routes={"routing": {"provider": "openai"}})
    assert answer.startswith("Error:") and "does not serve the model 'claude-sonnet-4-6'" in answer
    assert routes_lab.db.committed == 0 and routes_lab.audits == []


def test_an_env_locked_route_is_refused_naming_the_lock(routes_lab, monkeypatch):
    monkeypatch.setattr("app.core.config.settings.default_routing_model", "claude-sonnet-4-6", raising=False)
    answer = routes_lab.update(default_routes={"routing": {"provider": "bedrock"}})
    assert answer.startswith("Error 403") and "routing_model" in answer
    assert routes_lab.ps.routing_provider_id == routes_lab.anthropic.id
    assert routes_lab.db.committed == 0 and routes_lab.audits == []
    assert json.loads(routes_lab.read())["default_routes"]["routing"]["env_managed"] is True


def test_a_non_admin_is_refused_with_the_403_sentence(routes_lab):
    routes_lab.admin = False
    assert routes_lab.update(default_routes={"routing": {"provider": "bedrock"}}).startswith("Error 403")
    assert routes_lab.read().startswith("Error 403")
    assert routes_lab.db.committed == 0 and routes_lab.audits == []


def test_an_unknown_route_or_a_mixed_call_is_refused(routes_lab):
    assert "unknown route" in routes_lab.update(default_routes={"vision": {"provider": "bedrock"}})
    answer = routes_lab.update(default_routes={"routing": {"provider": "bedrock"}}, maximum_wait_time=60)
    assert "a call of their own" in answer
    assert routes_lab.db.committed == 0 and routes_lab.audits == []


def test_one_refused_route_writes_none_of_the_others(routes_lab):
    answer = routes_lab.update(
        default_routes={"agent_default": {"provider": "bedrock"}, "routing": {"provider": "openai"}}
    )
    assert answer.startswith("Error:")
    assert routes_lab.ps.agent_default_provider_id == routes_lab.anthropic.id
    assert routes_lab.db.committed == 0 and routes_lab.audits == []


def test_the_schema_names_default_routes():
    from app.services.builtin_tools import platform_admin

    defs, _handlers = platform_admin.build_platform_admin_tools(ADMIN_ID)
    schema = {d.name: d for d in defs}["update_platform_ai_settings"].input_schema["properties"]
    assert schema["default_routes"]["type"] == ["object", "null"]
