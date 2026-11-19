"""get_assignment reads only the sections asked for (#283).

A read of one assignment's trigger and gates answered both prompts whole
(53,887 characters for one assignment): the ``sections`` argument keeps the
settings block and the named blocks only.
"""

import uuid
from types import SimpleNamespace
from typing import Any, cast

import pytest

import app.api.v1.helpers as api_helpers
import app.core.database as database
from app.services.builtin_tools.agent_builder import (
    GET_ASSIGNMENT_SECTIONS,
    parse_get_assignment_sections,
)
from app.tools.base import ToolCall
from tests.test_agent_builder_tool import (
    _DedicatedAssignmentSession,
    _DedicatedToolSession,
    _SharedSession,
    build_agent_builder_tool,
)

_GATE = "Is this a pull request event? Answer YES or NO only."
_ACTION = "ACTION:" + ("x" * 600)


def _executor(monkeypatch: pytest.MonkeyPatch) -> tuple[Any, uuid.UUID]:
    assignment_id = uuid.uuid4()
    agent_id = uuid.uuid4()
    assignments = {
        assignment_id: SimpleNamespace(
            id=assignment_id,
            name="Gated assignment",
            description="desc",
            prompt=_ACTION,
            activation_prompt=_GATE,
            activation_check={"tool": "list_work_items", "arguments": {}, "condition": "non_empty"},
            trigger_type="webhook",
            trigger_config={"path": "hook"},
            delay=None,
            enabled=True,
            human_in_the_loop=False,
            ai_label=False,
            ai_label_prompt=None,
            human_in_the_loop_prompt="Ask before acting.",
            agent_id=agent_id,
        )
    }
    agents = {agent_id: SimpleNamespace(id=agent_id, name="Builder agent")}
    created: list[Any] = []

    async def _no_groups(_db: Any, _uid: Any) -> list[Any]:
        return []

    monkeypatch.setattr(database, "async_session", lambda: _DedicatedAssignmentSession(created, assignments, agents))
    monkeypatch.setattr(api_helpers, "get_user_group_ids", _no_groups)
    _, executor = build_agent_builder_tool(cast(Any, _SharedSession()), str(uuid.uuid4()))
    return executor, assignment_id


async def _read(executor: Any, assignment_id: uuid.UUID, **extra: Any) -> str:
    result = await executor.execute_tool(
        ToolCall(id=str(uuid.uuid4()), name="get_assignment", arguments={"assignment_id": str(assignment_id), **extra})
    )
    assert not result.is_error
    return str(result.content)


@pytest.mark.asyncio
async def test_no_sections_keeps_the_whole_answer(monkeypatch: pytest.MonkeyPatch) -> None:
    executor, assignment_id = _executor(monkeypatch)
    whole = await _read(executor, assignment_id)
    assert _GATE in whole and _ACTION in whole and "Ask before acting." in whole
    assert await _read(executor, assignment_id, sections=[]) == whole


@pytest.mark.asyncio
async def test_gates_keep_the_settings_and_drop_every_prompt(monkeypatch: pytest.MonkeyPatch) -> None:
    executor, assignment_id = _executor(monkeypatch)
    text = await _read(executor, assignment_id, sections=["gates"])
    assert text.startswith("**Gated assignment**")
    assert '  • Trigger config: {"path": "hook"}' in text
    assert GET_ASSIGNMENT_SECTIONS["gates"][0] in text
    assert "1. Tool check (no model call): " in text
    assert _GATE not in text
    assert _ACTION not in text
    assert "Ask before acting." not in text


@pytest.mark.asyncio
async def test_a_prompt_section_carries_its_prompt_alone(monkeypatch: pytest.MonkeyPatch) -> None:
    executor, assignment_id = _executor(monkeypatch)
    text = await _read(executor, assignment_id, sections="prompt, human_in_the_loop_prompt")
    assert _ACTION in text
    assert "Ask before acting." in text
    assert _GATE not in text
    assert GET_ASSIGNMENT_SECTIONS["gates"][0] not in text


def test_an_unknown_section_is_refused_with_the_valid_names() -> None:
    wanted, err = parse_get_assignment_sections(["gates", "persona"])
    assert wanted is None
    assert err is not None
    assert "Unknown get_assignment section(s): persona" in err
    for name in GET_ASSIGNMENT_SECTIONS:
        assert name in err


@pytest.mark.asyncio
async def test_get_assignment_refuses_an_unknown_section_before_any_query(monkeypatch: pytest.MonkeyPatch) -> None:
    created: list[Any] = []
    monkeypatch.setattr(database, "async_session", lambda: _DedicatedToolSession(created, "unused"))
    _, executor = build_agent_builder_tool(cast(Any, _SharedSession()), str(uuid.uuid4()))
    result = await executor.execute_tool(
        ToolCall(
            id=str(uuid.uuid4()),
            name="get_assignment",
            arguments={"assignment_id": str(uuid.uuid4()), "sections": ["prompts"]},
        )
    )
    assert "Unknown get_assignment section(s): prompts" in str(result.content)
    assert created == []
