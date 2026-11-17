"""Per-call ``output_schema`` on ``consult_agent`` and ``spawn_background_task``.

A caller fixes the shape of one answer: the schema is checked when the tool is
called (a malformed one refuses the call before anything runs) and handed to
the callee's run as ``session_output_schema``, which makes that run answer one
JSON object valid against it. Without it nothing changes. The callee must opt
in (``accept_caller_output_format``, off by default): otherwise the call is
refused before it runs.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Awaitable, Callable
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.services.builtin_tools import build_sub_agent_tools
from app.services.output_format import (
    CALL_OUTPUT_SCHEMA_KEY,
    CALL_OUTPUT_SCHEMA_MAX_BYTES,
    CALL_OUTPUT_SCHEMA_PROPERTY,
    caller_output_format_refusal,
    effective_output_contract,
    parse_call_output_schema,
)

_SERVICES = Path(__file__).resolve().parents[1] / "app" / "services"

_TRANSLATION = {
    "type": "object",
    "required": ["name", "description", "content"],
    "properties": {
        "name": {"type": "string"},
        "description": {"type": "string"},
        "content": {"type": "string"},
    },
}


# ── the validator ────────────────────────────────────────────────────────────


def test_no_schema_is_no_contract() -> None:
    values: tuple[object, ...] = (None, "", {})
    for value in values:
        assert parse_call_output_schema(value) == (None, None)


def test_a_valid_schema_passes_whole() -> None:
    assert parse_call_output_schema(_TRANSLATION) == (_TRANSLATION, None)


def test_a_schema_sent_as_json_text_is_decoded() -> None:
    assert parse_call_output_schema(json.dumps(_TRANSLATION)) == (_TRANSLATION, None)


@pytest.mark.parametrize(
    ("value", "problem"),
    [
        ("{not json", "not valid JSON"),
        ([1, 2], "must be a JSON Schema object, not list"),
        (42, "must be a JSON Schema object, not int"),
        ({"type": "nope"}, "not a valid JSON Schema"),
        ({"type": "object", "required": "name"}, "not a valid JSON Schema"),
    ],
)
def test_a_malformed_schema_is_refused_naming_the_problem(value: object, problem: str) -> None:
    schema, error = parse_call_output_schema(value)
    assert schema is None
    assert error is not None
    assert error.startswith("Error: output_schema refused, nothing ran:")
    assert problem in error


def test_an_oversized_schema_is_refused_with_the_limit() -> None:
    big = {"type": "object", "description": "x" * (CALL_OUTPUT_SCHEMA_MAX_BYTES + 1)}
    schema, error = parse_call_output_schema(big)
    assert schema is None
    assert error is not None and f"{CALL_OUTPUT_SCHEMA_MAX_BYTES:,}-byte limit" in error


def test_the_call_schema_makes_the_run_json() -> None:
    """A markdown agent answers JSON under the caller's schema, as configured without."""
    assert effective_output_contract("markdown", None, _TRANSLATION) == ("json", _TRANSLATION)
    assert effective_output_contract("markdown", None, None) == ("markdown", None)


def test_the_argument_is_documented_in_one_short_line() -> None:
    description = CALL_OUTPUT_SCHEMA_PROPERTY["description"]
    assert CALL_OUTPUT_SCHEMA_PROPERTY["type"] == "object"
    assert "\n" not in description
    assert len(description) < 200
    assert "accept_caller_output_format" in description


# ── the callee's opt-in ─────────────────────────────────────────────────────────────


@pytest.mark.parametrize("accepts", [False, None])
def test_a_callee_that_did_not_opt_in_refuses_a_schema(accepts: bool | None) -> None:
    target = SimpleNamespace(name="Translator", accept_caller_output_format=accepts)
    refusal = caller_output_format_refusal(target, _TRANSLATION)
    assert refusal is not None
    assert refusal.startswith("Error: Agent Translator answers only in its configured format")
    assert "accept_caller_output_format is off" in refusal
    assert "without output_schema" in refusal


def test_a_callee_without_the_attribute_is_off() -> None:
    assert caller_output_format_refusal(SimpleNamespace(name="Old"), _TRANSLATION) is not None


def test_a_callee_that_opted_in_takes_the_schema() -> None:
    target = SimpleNamespace(name="Translator", accept_caller_output_format=True)
    assert caller_output_format_refusal(target, _TRANSLATION) is None


@pytest.mark.parametrize("accepts", [True, False])
def test_no_schema_is_never_refused(accepts: bool) -> None:
    target = SimpleNamespace(name="Translator", accept_caller_output_format=accepts)
    assert caller_output_format_refusal(target, None) is None


def test_the_setting_is_off_by_default_on_the_model_and_the_api() -> None:
    from app.api.v1.schemas.agent import AgentResponse, CreateAgentRequest, UpdateAgentRequest
    from app.models.agent import Agent

    column = Agent.__table__.c.accept_caller_output_format
    assert column.nullable is False
    assert column.default.arg is False
    assert "false" in str(column.server_default.arg)
    assert AgentResponse.model_fields["accept_caller_output_format"].default is False
    assert CreateAgentRequest.model_fields["accept_caller_output_format"].default is False
    assert UpdateAgentRequest.model_fields["accept_caller_output_format"].default is None
    assert UpdateAgentRequest(accept_caller_output_format=True).model_dump(exclude_unset=True) == {
        "accept_caller_output_format": True
    }


def test_the_platform_tools_read_and_write_the_setting() -> None:
    source = (_SERVICES / "builtin_tools" / "agent_builder.py").read_text()
    assert '("accept_caller_output_format", accept_caller_output_format)' in source
    assert '"accept_caller_output_format": {' in source
    assert "Accept a format requested by the caller: " in source


# ── consult_agent: every handler takes the argument and passes it on ─────────


def _consult_body(source: str, register: str) -> str:
    start = source.index("async def _consult_agent(")
    return source[start : source.index(register, start)]


@pytest.mark.parametrize(
    ("module", "register"),
    [
        ("agent_chat.py", 'ho_exec.register("consult_agent", _consult_agent)'),
        ("assignment_engine.py", '_ho_exec.register("consult_agent", _consult_agent)'),
        ("general_chat.py", 'ho_exec.register("consult_agent", _consult_agent)'),
    ],
)
def test_every_consult_handler_checks_and_forwards_the_schema(module: str, register: str) -> None:
    source = (_SERVICES / module).read_text()
    body = _consult_body(source, register)
    assert "output_schema: object = None" in body
    # Checked before the target is resolved: a malformed schema runs nothing.
    assert body.index("parse_call_output_schema(output_schema)") < body.index("resolve_handover_agent(")
    assert "session_output_schema=_call_schema" in body
    definition = source[source.index(register) :]
    definition = definition[: definition.index('"required": ["agent_id", "question"]')]
    assert '"output_schema": CALL_OUTPUT_SCHEMA_PROPERTY' in definition
    # The callee's opt-in is read once the target is known, before it runs.
    gate = body.index("caller_output_format_refusal(target, _call_schema)")
    assert body.index("resolve_handover_agent(") < gate < body.index("session_output_schema=_call_schema")


def test_a_shaped_chat_consult_answer_comes_back_whole() -> None:
    """No citation note or advisory cap is appended to a JSON answer."""
    body = _consult_body((_SERVICES / "agent_chat.py").read_text(), 'ho_exec.register("consult_agent", _consult_agent)')
    whole = body.index("if _call_schema is not None:")
    assert whole < body.index("_CITATION_MARKER.search(_return_text)")
    assert whole < body.index("ADVISORY_CONSULT_RETURN_CAP")


# ── spawn_background_task ─────────────────────────────────────────────────────


def _build_spawn(accepts: bool = True) -> tuple[SimpleNamespace, list[Any], Callable[..., Awaitable[Any]]]:
    peer = SimpleNamespace(
        id=uuid.uuid4(),
        name="Translator",
        slug="translator",
        handover_types={},
        accept_caller_output_format=accepts,
    )
    built = build_sub_agent_tools(
        MagicMock(),
        user_id=str(uuid.uuid4()),
        parent_agent_id=str(uuid.uuid4()),
        handover_agents=[peer],
        parent_run_id=str(uuid.uuid4()),
    )
    assert built is not None
    definitions, executor = built
    return peer, definitions, executor._handlers["spawn_background_task"]


def _patched(create_mock: AsyncMock) -> tuple[Any, Any]:
    return (
        patch("app.services.sub_agent.count_active_sub_agent_executions", new=AsyncMock(return_value=0)),
        patch("app.services.sub_agent.create_sub_agent_task", new=create_mock),
    )


async def test_spawn_stores_the_schema_for_the_worker() -> None:
    peer, _defs, spawn = _build_spawn()
    create_mock = AsyncMock(return_value=SimpleNamespace(id=uuid.uuid4(), status="pending"))
    count_patch, create_patch = _patched(create_mock)
    with count_patch, create_patch:
        answer = await spawn(str(peer.id), "Translate to English", output_schema=_TRANSLATION)
    assert "spawned successfully" in answer
    assert create_mock.await_args is not None
    assert create_mock.await_args.kwargs["extra_metadata"] == {CALL_OUTPUT_SCHEMA_KEY: _TRANSLATION}


async def test_spawn_on_a_callee_that_did_not_opt_in_runs_nothing() -> None:
    peer, _defs, spawn = _build_spawn(accepts=False)
    create_mock = AsyncMock(side_effect=AssertionError("must not spawn"))
    count_patch, create_patch = _patched(create_mock)
    with count_patch, create_patch:
        answer = await spawn(str(peer.id), "Translate", output_schema=_TRANSLATION)
    assert answer.startswith("Error: Agent Translator answers only in its configured format")
    create_mock.assert_not_called()


@pytest.mark.parametrize("accepts", [True, False])
async def test_spawn_without_schema_is_unchanged(accepts: bool) -> None:
    peer, _defs, spawn = _build_spawn(accepts=accepts)
    create_mock = AsyncMock(return_value=SimpleNamespace(id=uuid.uuid4(), status="pending"))
    count_patch, create_patch = _patched(create_mock)
    with count_patch, create_patch:
        answer = await spawn(str(peer.id), "Translate to English")
    assert "spawned successfully" in answer
    assert create_mock.await_args is not None
    assert create_mock.await_args.kwargs["extra_metadata"] is None


async def test_spawn_with_a_malformed_schema_runs_nothing() -> None:
    peer, _defs, spawn = _build_spawn()
    create_mock = AsyncMock(side_effect=AssertionError("must not spawn"))
    count_patch, create_patch = _patched(create_mock)
    with count_patch, create_patch:
        answer = await spawn(str(peer.id), "Translate", output_schema={"type": "nope"})
    assert answer.startswith("Error: output_schema refused, nothing ran:")
    create_mock.assert_not_called()


def test_spawn_documents_the_argument() -> None:
    _peer, definitions, _spawn = _build_spawn()
    spawn_def = next(d for d in definitions if d.name == "spawn_background_task")
    assert spawn_def.input_schema["properties"]["output_schema"] == CALL_OUTPUT_SCHEMA_PROPERTY
    assert spawn_def.input_schema["required"] == ["agent_id", "task"]


def test_the_worker_runs_the_task_under_the_stored_schema() -> None:
    source = (_SERVICES / "sub_agent.py").read_text()
    assert "(task.metadata_json or {}).get(CALL_OUTPUT_SCHEMA_KEY)" in source
    assert "session_output_schema=_call_output_schema" in source
    # A wait-budget continuation keeps the caller's shape.
    assert "call_output_schema=_call_output_schema" in source
    assert "{CALL_OUTPUT_SCHEMA_KEY: call_output_schema}" in source
