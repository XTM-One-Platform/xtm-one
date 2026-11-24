"""When a change to an agent's tools reaches a run (openapi-tools-refresh-in-turn: B, then A in #1874)."""

from __future__ import annotations

from app.services.tool_surface_timing import (
    AGENT_TOOL_FIELDS,
    INTEGRATION_RELOAD_LINE,
    NEXT_TURN_LINE,
    agent_change_line,
    integration_change_line,
)


def test_integration_spec_or_credential_change_states_the_reload() -> None:
    assert integration_change_line(["openapi"]) == INTEGRATION_RELOAD_LINE
    assert integration_change_line(["name", "api_key"]) == INTEGRATION_RELOAD_LINE
    assert integration_change_line(["sharing"]) == INTEGRATION_RELOAD_LINE
    assert "after this step" in INTEGRATION_RELOAD_LINE


def test_integration_label_change_says_nothing() -> None:
    assert integration_change_line(["name", "description", "tags", "scopes"]) == ""
    assert integration_change_line([]) == ""


def test_agent_tool_scope_change_states_next_turn() -> None:
    for field in ("integration_bindings", "integration_read_only", "builtin_tool_ids", "integration_allowed_tools"):
        assert field in AGENT_TOOL_FIELDS
        assert agent_change_line(["persona", field]) == NEXT_TURN_LINE


def test_agent_change_outside_tools_says_nothing() -> None:
    assert agent_change_line(["persona", "name", "temperature"]) == ""


def test_line_states_the_fact_without_an_instruction() -> None:
    assert "next chat turn" in NEXT_TURN_LINE
    assert "next assignment run" in NEXT_TURN_LINE
