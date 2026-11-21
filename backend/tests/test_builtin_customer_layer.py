"""The customer layer of a built-in agent (#914).

Mechanics stay locked and are refused by name; the customer layer (bindings,
MCP servers, knowledge bases, skills, custom tools, model, organization notes)
is writable by whoever manages the built-in and survives a seed run.
"""

from __future__ import annotations

import uuid
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from app.agents import seed as seed_module
from app.api.v1.schemas.agent import UpdateAgentRequest
from app.services import builtin_customer_layer as layer
from tests.test_builtin_hitl_endpoint_wiring import (
    _admin,
    _builtin_agent,
    _invoke_agent_update,
    _member,
)


@pytest.fixture(autouse=True)
def _xtm_one_mode(monkeypatch):
    from app.core.config import settings

    monkeypatch.setattr(settings, "platform_mode", "xtm_one", raising=False)


# --- Pure rules ---------------------------------------------------------------


def test_mechanics_fields_are_outside_the_customer_layer():
    for field in ("persona", "name", "handover_agent_ids", "handover_types", "builtin_tool_ids",
                  "tool_deny_list", "human_in_the_loop_prompt", "config", "is_company_managed"):
        assert field not in layer.CUSTOMER_LAYER_FIELDS, field


def test_the_customer_layer_names_what_a_customer_brings():
    for field in ("integration_bindings", "mcp_server_ids", "knowledge_base_ids",
                  "knowledge_base_permissions", "skill_ids", "custom_tool_ids",
                  "customer_notes", "llm_model"):
        assert field in layer.CUSTOMER_LAYER_FIELDS, field


def test_a_mechanics_field_resent_unchanged_is_not_a_change():
    agent = SimpleNamespace(persona="core", mcp_server_ids=None)
    sent = {"persona": "core", "mcp_server_ids": [str(uuid.uuid4())]}
    assert layer.locked_changes(agent, sent) == []
    assert layer.customer_layer_changes(agent, sent) == {"mcp_server_ids"}


def test_the_refusal_names_each_locked_field():
    detail = layer.locked_refusal(["persona", "handover_types"])
    assert detail.startswith("Built-in agents cannot be modified: handover_types, persona are locked")
    assert "Fork the fleet" in detail
    assert layer.locked_refusal([]) == "Built-in agents cannot be modified"


def test_notes_follow_the_persona_and_never_replace_it():
    agent = SimpleNamespace(customer_notes="Tickets go to project ITS.")
    text = layer.persona_with_customer_notes(agent, "You are the triage analyst.")
    assert text.startswith("You are the triage analyst.")
    assert text.endswith("Tickets go to project ITS.")
    assert layer.CUSTOMER_NOTES_HEADING in text
    assert layer.persona_with_customer_notes(SimpleNamespace(customer_notes=None), "p") == "p"


def test_notes_are_bounded():
    agent = SimpleNamespace(customer_notes="x" * (layer.CUSTOMER_NOTES_MAX_CHARS + 50))
    text = layer.persona_with_customer_notes(agent, "p")
    assert text.count("x") == layer.CUSTOMER_NOTES_MAX_CHARS


def test_clearing_the_model_hands_it_back_to_the_release():
    agent = SimpleNamespace(customer_fields=None, llm_model="gpt-x", llm_provider=None,
                            llm_provider_id=None, llm_model_source=None)
    layer.record_customer_fields(agent, {"llm_model", "mcp_server_ids"})
    assert layer.seed_keeps_model(agent)
    agent.llm_model = None
    layer.record_customer_fields(agent, {"llm_model"})
    assert not layer.seed_keeps_model(agent)
    assert agent.customer_fields == ["mcp_server_ids"]


# --- The endpoint -------------------------------------------------------------


@pytest.mark.asyncio
async def test_an_admin_adds_an_mcp_server_to_a_builtin(monkeypatch):
    """A whole form re-sends the persona unchanged: not a locked change."""
    agent = _builtin_agent()
    mcp = str(uuid.uuid4())
    await _invoke_agent_update(
        monkeypatch,
        UpdateAgentRequest(mcp_server_ids=[mcp], persona=agent.persona),
        agent,
        _admin(),
    )
    assert agent.mcp_server_ids == [mcp]
    assert agent.customer_fields == ["mcp_server_ids"]


@pytest.mark.asyncio
async def test_an_admin_sets_template_bindings_the_seed_then_keeps(monkeypatch):
    agent = _builtin_agent()
    await _invoke_agent_update(
        monkeypatch,
        UpdateAgentRequest(
            integration_template_bindings=["opencti", "jira"],
            integration_template_read_only=["jira"],
        ),
        agent,
        _admin(),
    )
    assert agent.customer_fields == ["integration_template_bindings", "integration_template_read_only"]


@pytest.mark.asyncio
async def test_an_admin_sets_organization_notes_on_a_builtin(monkeypatch):
    agent = _builtin_agent()
    await _invoke_agent_update(
        monkeypatch, UpdateAgentRequest(customer_notes="  EDR is Defender.  "), agent, _admin()
    )
    assert agent.customer_notes == "EDR is Defender."
    assert agent.persona == "persona", "the seeded persona is never touched"


@pytest.mark.asyncio
async def test_notes_on_an_ordinary_agent_are_refused(monkeypatch):
    agent = _builtin_agent()
    agent.is_builtin = False
    with pytest.raises(HTTPException) as exc:
        await _invoke_agent_update(monkeypatch, UpdateAgentRequest(customer_notes="x"), agent, _admin())
    assert exc.value.status_code == 422


@pytest.mark.asyncio
async def test_a_persona_rewrite_beside_a_customer_field_is_refused_by_name(monkeypatch):
    agent = _builtin_agent()
    with pytest.raises(HTTPException) as exc:
        await _invoke_agent_update(
            monkeypatch,
            UpdateAgentRequest(mcp_server_ids=[str(uuid.uuid4())], persona="rewritten"),
            agent,
            _admin(),
        )
    assert exc.value.status_code == 403
    assert "persona is locked by the product" in exc.value.detail
    assert agent.mcp_server_ids is None, "refused edit must not have applied"


@pytest.mark.asyncio
async def test_a_member_cannot_edit_the_customer_layer_of_a_company_managed_builtin(monkeypatch):
    agent = _builtin_agent()
    with pytest.raises(HTTPException) as exc:
        await _invoke_agent_update(
            monkeypatch, UpdateAgentRequest(skill_ids=[str(uuid.uuid4())]), agent, _member()
        )
    assert exc.value.status_code == 403
    assert exc.value.detail == "Only admins can edit company-managed agents"


# --- The seed -----------------------------------------------------------------


def _seeded_builtin(**customer) -> SimpleNamespace:
    agent = SimpleNamespace(
        name="Triage", description=None, icon="Bot", persona="old", output_format="markdown",
        output_schema=None, disable_chat=False, hide_runs=False, max_iterations=None,
        llm_model=None, llm_model_source=None, llm_provider=None, llm_provider_id=None,
        tool_deny_list=[], builtin_tool_ids=[], integration_template_bindings=[],
        integration_template_read_only=None, available_channel_types=["web"], tags=["builtin"],
        supported_intents={}, temperature=None, reasoning_effort=None,
        organization_context_categories=None, finalize_tool_ref=None, prepare_tool_ref=None,
        skip_llm=False, config=None, memory_short_term_enabled=True,
        memory_medium_term_enabled=True, memory_persistent_enabled=True, memory_user_enabled=True,
        mcp_server_ids=None, knowledge_base_ids=None, skill_ids=None, custom_tool_ids=None,
        integration_bindings=None, customer_notes=None, customer_fields=None,
    )
    for key, value in customer.items():
        setattr(agent, key, value)
    return agent


def _sync(agent, data, monkeypatch):
    monkeypatch.setattr(seed_module, "flag_modified", lambda *_a, **_k: None, raising=False)
    monkeypatch.setattr("sqlalchemy.orm.attributes.flag_modified", lambda *_a, **_k: None)
    seed_module._sync_agent_fields(agent, data)


def test_a_seed_run_keeps_every_customer_field_and_restores_the_mechanics(monkeypatch):
    mcp, kb, skill, tool = (str(uuid.uuid4()) for _ in range(4))
    bindings = {"jira": [str(uuid.uuid4())], "microsoft_defender": [str(uuid.uuid4())]}
    agent = _seeded_builtin(
        persona="hand edited",
        mcp_server_ids=[mcp], knowledge_base_ids=[kb], skill_ids=[skill], custom_tool_ids=[tool],
        integration_bindings=bindings, customer_notes="EDR is Defender.",
        llm_model="customer-model", customer_fields=["llm_model", "mcp_server_ids"],
    )
    _sync(agent, {"slug": "triage", "name": "Triage", "persona": "Seeded core persona.",
                  "llm_model": "seed-model"}, monkeypatch)
    assert agent.persona == "Seeded core persona."
    assert agent.mcp_server_ids == [mcp]
    assert agent.knowledge_base_ids == [kb]
    assert agent.skill_ids == [skill]
    assert agent.custom_tool_ids == [tool]
    assert agent.integration_bindings == bindings
    assert agent.customer_notes == "EDR is Defender."
    assert agent.llm_model == "customer-model"


def test_a_seed_run_keeps_template_bindings_a_person_set(monkeypatch):
    agent = _seeded_builtin(
        integration_template_bindings=["opencti", "jira"],
        integration_template_read_only=["jira"],
        customer_fields=["integration_template_bindings", "integration_template_read_only"],
    )
    _sync(
        agent,
        {
            "slug": "triage",
            "name": "Triage",
            "persona": "Seeded.",
            "integration_template_bindings": ["opencti"],
            "integration_template_read_only": ["opencti"],
        },
        monkeypatch,
    )
    assert agent.integration_template_bindings == ["opencti", "jira"]
    assert agent.integration_template_read_only == ["jira"]


def test_a_seed_run_writes_the_seeded_template_bindings_when_none_were_set(monkeypatch):
    agent = _seeded_builtin(integration_template_bindings=["stale"])
    _sync(
        agent,
        {
            "slug": "triage",
            "name": "Triage",
            "persona": "Seeded.",
            "integration_template_bindings": ["opencti"],
            "integration_template_read_only": ["opencti"],
        },
        monkeypatch,
    )
    assert agent.integration_template_bindings == ["opencti"]
    assert agent.integration_template_read_only == ["opencti"]


def test_a_seed_run_writes_the_release_model_when_none_was_pinned(monkeypatch):
    agent = _seeded_builtin(llm_model="stale")
    _sync(agent, {"slug": "triage", "name": "Triage", "persona": "Seeded.", "llm_model": "seed-model"},
          monkeypatch)
    assert agent.llm_model == "seed-model"
