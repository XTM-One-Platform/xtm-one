"""get_agent reads only the sections asked for (#277).

An audit of one setting across several agents read every persona whole: the
``sections`` argument keeps the identity block and the named blocks only.
"""

import uuid
from typing import Any, cast

import pytest

import app.core.database as database
from app.services.builtin_tools.agent_builder import (
    GET_AGENT_SECTIONS,
    parse_get_agent_sections,
    project_get_agent_sections,
)
from app.tools.base import ToolCall
from tests.test_agent_builder_tool import (
    _DedicatedToolSession,
    _SharedSession,
    build_agent_builder_tool,
)

_PARTS = [
    "**Desk**",
    "  • ID: `a`",
    "",
    "Behavior:",
    "  • Output format: markdown",
    "",
    "Tools & resources:",
    '  • Built-in allowed tools: {"platform": ["get_agent"]}',
    "",
    "Human-in-the-loop:",
    "  • Enabled: No",
    "",
    "  • Created: 2026-11-19",
    "",
    "Human-in-the-loop prompt (full):",
    "Ask first.",
    "",
    "Persona (full):",
    "Line one\n\nTools & resources:\nstill the persona",
]


def _db() -> Any:
    return cast(Any, _SharedSession())


def test_no_sections_keeps_the_whole_answer() -> None:
    assert parse_get_agent_sections(None) == (None, None)
    assert parse_get_agent_sections([]) == (None, None)
    assert project_get_agent_sections(_PARTS, None) == _PARTS


def test_a_projection_keeps_the_identity_and_the_sections_named() -> None:
    wanted, err = parse_get_agent_sections(["tools"])
    assert err is None
    text = "\n".join(project_get_agent_sections(_PARTS, wanted))
    assert text.startswith("**Desk**\n  • ID: `a`")
    assert "Built-in allowed tools" in text
    assert "  • Created: 2026-11-19" in text
    assert "Behavior:" not in text
    assert "Persona (full):" not in text
    assert "still the persona" not in text


def test_human_in_the_loop_carries_its_prompt_and_the_persona_stays_whole() -> None:
    wanted, _ = parse_get_agent_sections("human_in_the_loop, persona")
    kept = project_get_agent_sections(_PARTS, wanted)
    assert "Ask first." in kept
    assert "Line one\n\nTools & resources:\nstill the persona" in kept
    assert "Tools & resources:" not in kept


def test_an_unknown_section_is_refused_with_the_valid_names() -> None:
    wanted, err = parse_get_agent_sections(["tools", "secrets"])
    assert wanted is None
    assert err is not None
    assert "secrets" in err
    for name in GET_AGENT_SECTIONS:
        assert name in err
    _, err = parse_get_agent_sections({"tools": True})
    assert err == "sections must be a list of section names."


@pytest.mark.asyncio
async def test_get_agent_refuses_an_unknown_section_before_any_query(monkeypatch: pytest.MonkeyPatch) -> None:
    created_sessions: list[Any] = []
    monkeypatch.setattr(database, "async_session", lambda: _DedicatedToolSession(created_sessions, "unused"))
    _, executor = build_agent_builder_tool(_db(), str(uuid.uuid4()))
    result = await executor.execute_tool(
        ToolCall(
            id=str(uuid.uuid4()),
            name="get_agent",
            arguments={"agent_id": str(uuid.uuid4()), "sections": ["personas"]},
        )
    )
    assert "Unknown get_agent section(s): personas" in result.content
    assert created_sessions == []


def test_the_schema_names_the_sections() -> None:
    tools, _ = build_agent_builder_tool(_db(), str(uuid.uuid4()))
    tool = next(t for t in tools if t.name == "get_agent")
    prop = tool.input_schema["properties"]["sections"]
    for name in GET_AGENT_SECTIONS:
        assert name in prop["description"]
    assert tool.input_schema["required"] == ["agent_id"]
