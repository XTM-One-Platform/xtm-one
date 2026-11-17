"""A channel turn's saved session selection picks up the agent's new defaults.

``session_tool_kwargs`` maps the selection a web turn saved on the
conversation; the selection replaces the agent's bindings, so a default
added to the agent since then (an integration bound after the save) must
join it, as the web does when it reopens the conversation (#9892).
"""

from __future__ import annotations

from types import SimpleNamespace

from app.services.chat_saved_tool_selection import session_tool_kwargs

OLD = "11111111-1111-4111-8111-111111111111"
NEW = "22222222-2222-4222-8222-222222222222"
OFF = "33333333-3333-4333-8333-333333333333"


def _agent(**kwargs) -> SimpleNamespace:
    base = {
        "integration_bindings": {"microsoft": OLD},
        "builtin_tool_ids": ["files"],
        "custom_tool_ids": [],
        "mcp_server_ids": [],
        "knowledge_base_ids": [],
    }
    base.update(kwargs)
    return SimpleNamespace(**base)


def _saved(**kwargs) -> dict:
    payload = {
        "builtin_tool_ids": ["files"],
        "custom_tool_ids": [],
        "integration_ids": [OLD],
        "mcp_server_ids": [],
        "knowledge_base_ids": [],
        "default_tool_ids": ["files", OLD],
    }
    payload.update(kwargs)
    return payload


def test_an_integration_bound_after_the_save_is_enabled():
    agent = _agent(integration_bindings={"microsoft": OLD, "whatsapp": [NEW]})
    kwargs = session_tool_kwargs(_saved(), agent)
    assert kwargs["session_integration_ids"] == [OLD, NEW]


def test_a_default_turned_off_for_the_conversation_stays_off():
    agent = _agent(integration_bindings={"microsoft": OLD, "whatsapp": OFF})
    saved = _saved(default_tool_ids=["files", OLD, OFF])
    kwargs = session_tool_kwargs(saved, agent)
    assert kwargs["session_integration_ids"] == [OLD]


def test_a_legacy_save_without_a_snapshot_is_its_own_baseline():
    agent = _agent(integration_bindings={"microsoft": OLD, "whatsapp": NEW}, builtin_tool_ids=["files", "web"])
    saved = _saved()
    saved.pop("default_tool_ids")
    kwargs = session_tool_kwargs(saved, agent)
    assert kwargs["session_integration_ids"] == [OLD, NEW]
    assert kwargs["session_builtin_tool_ids"] == ["files", "web"]


def test_no_saved_selection_leaves_the_agent_defaults_alone():
    assert session_tool_kwargs(None, _agent(integration_bindings={"whatsapp": NEW})) == {}


def test_without_the_agent_the_saved_selection_is_returned_as_is():
    agent_less = session_tool_kwargs(_saved())
    assert agent_less["session_integration_ids"] == [OLD]
