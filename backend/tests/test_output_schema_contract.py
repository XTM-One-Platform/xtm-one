"""An answer that breaks its output schema is re-prompted once, then flagged (#2765).

A delegated run (``spawn_background_task`` / ``consult_agent`` with a per-call
``output_schema``) used to store a well-formed but schema-violating answer as a
clean completion: the validation errors only reached a log line. The delivery
now keeps them (``deliver_json_answer_checked``), re-prompts the model once with
the error list (``deliver_json_answer_with_contract``), and an answer still off
the schema is delivered marked ``partial`` with stop reason
``schema_violation`` and its errors, which the caller reads.
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from app.services.builtin_tools.handover import _schema_violation_line
from app.services.structured_output import (
    SCHEMA_VIOLATION_STOP_REASON,
    deliver_json_answer,
    deliver_json_answer_checked,
    deliver_json_answer_with_contract,
    emit_repaired_json_checked,
    schema_corrective_message,
    schema_violation_note,
)
from app.tools.agent_loop import UNFINISHED_STOP_REASONS

_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "a": {"type": "integer"},
        "b": {"type": "integer"},
        "c": {"type": "integer"},
        "list": {"type": "array", "items": {"type": "integer"}, "minItems": 3},
    },
    "required": ["a", "b", "c", "list"],
}

_BROKEN = '{"a": 1, "list": [1]}'
_VALID = '{"a": 1, "b": 2, "c": 3, "list": [1, 2, 3]}'


class _Reprompt:
    """A corrective call that answers from a script and counts its calls."""

    def __init__(self, *answers: str | None) -> None:
        self.answers = list(answers)
        self.calls: list[tuple[str, str]] = []

    async def __call__(self, corrective: str, answer: str) -> str | None:
        self.calls.append((corrective, answer))
        return self.answers.pop(0) if self.answers else None


def test_the_checked_delivery_keeps_the_schema_errors() -> None:
    delivery = deliver_json_answer_checked(_BROKEN, "json", _SCHEMA, log_context="test")

    assert json.loads(delivery.content) == {"a": 1, "list": [1]}
    joined = " ".join(delivery.schema_errors)
    for missing in ("'b'", "'c'"):
        assert missing in joined
    assert "list" in joined


def test_the_plain_delivery_is_unchanged() -> None:
    """``deliver_json_answer`` still answers the repaired text alone."""
    assert json.loads(deliver_json_answer(_BROKEN, "json", _SCHEMA, log_context="test")) == {
        "a": 1,
        "list": [1],
    }


def test_syntax_damage_the_repair_fixes_is_no_schema_error() -> None:
    raw = '```json\n{"a": 1, "b": 2, "c": 3, "list": [1, 2, 3],}\n```'
    text, errors = emit_repaired_json_checked(raw, _SCHEMA, log_context="test")

    assert errors == []
    assert text is not None
    assert json.loads(text) == json.loads(_VALID)


@pytest.mark.asyncio
async def test_a_violating_answer_is_reprompted_once_and_a_valid_retry_wins() -> None:
    reprompt = _Reprompt(_VALID)

    delivery = await deliver_json_answer_with_contract(_BROKEN, "json", _SCHEMA, log_context="test", reprompt=reprompt)

    assert len(reprompt.calls) == 1
    corrective, answer = reprompt.calls[0]
    assert "'b' is a required property" in corrective
    assert json.loads(answer) == {"a": 1, "list": [1]}
    assert delivery.reprompted
    assert delivery.schema_errors == []
    assert json.loads(delivery.content) == json.loads(_VALID)


@pytest.mark.asyncio
async def test_a_retry_still_off_the_schema_is_delivered_with_its_errors() -> None:
    reprompt = _Reprompt('{"a": 1, "b": 2, "list": [1]}')

    delivery = await deliver_json_answer_with_contract(_BROKEN, "json", _SCHEMA, log_context="test", reprompt=reprompt)

    assert len(reprompt.calls) == 1
    assert delivery.schema_errors
    # The retry has fewer errors (b is there now): it is the best payload.
    assert json.loads(delivery.content) == {"a": 1, "b": 2, "list": [1]}


@pytest.mark.asyncio
async def test_a_retry_that_fails_keeps_the_first_answer_and_its_errors() -> None:
    async def failing(_corrective: str, _answer: str) -> str | None:
        raise TimeoutError

    delivery = await deliver_json_answer_with_contract(_BROKEN, "json", _SCHEMA, log_context="test", reprompt=failing)

    assert delivery.reprompted
    assert delivery.schema_errors
    assert json.loads(delivery.content) == {"a": 1, "list": [1]}


@pytest.mark.asyncio
async def test_a_valid_first_answer_is_never_reprompted() -> None:
    reprompt = _Reprompt(_BROKEN)

    delivery = await deliver_json_answer_with_contract(_VALID, "json", _SCHEMA, log_context="test", reprompt=reprompt)

    assert reprompt.calls == []
    assert not delivery.reprompted
    assert delivery.schema_errors == []


@pytest.mark.asyncio
async def test_a_fenced_schema_valid_answer_is_repaired_with_no_reprompt() -> None:
    reprompt = _Reprompt(_BROKEN)
    fenced = '```json\n{"a": 1, "b": 2, "c": 3, "list": [1, 2, 3]}\n```'

    delivery = await deliver_json_answer_with_contract(fenced, "json", _SCHEMA, log_context="test", reprompt=reprompt)

    assert reprompt.calls == []
    assert delivery.schema_errors == []
    assert json.loads(delivery.content) == json.loads(_VALID)


@pytest.mark.asyncio
async def test_an_answer_without_a_schema_or_a_reprompt_is_never_reprompted() -> None:
    no_schema = await deliver_json_answer_with_contract(
        _BROKEN, "json", None, log_context="test", reprompt=_Reprompt(_VALID)
    )
    assert no_schema.schema_errors == []

    no_reprompt = await deliver_json_answer_with_contract(_BROKEN, "json", _SCHEMA, log_context="test", reprompt=None)
    assert no_reprompt.schema_errors
    assert not no_reprompt.reprompted


def test_the_corrective_message_names_the_errors_and_caps_them() -> None:
    errors = [f"Validation error at (root): 'k{i}' is a required property" for i in range(14)]
    message = schema_corrective_message(errors)

    assert "'k0' is a required property" in message
    assert "'k13'" not in message
    assert "and 4 more" in message


def test_the_schema_violation_is_an_unfinished_stop() -> None:
    assert SCHEMA_VIOLATION_STOP_REASON in UNFINISHED_STOP_REASONS


def test_the_caller_reads_the_violation_in_the_task_result() -> None:
    errors = ["Validation error at (root): 'b' is a required property"]
    line = _schema_violation_line({"status": "completed", "partial": True, "schema_errors": errors})

    assert SCHEMA_VIOLATION_STOP_REASON in line
    assert "'b' is a required property" in line
    assert line == f"  {schema_violation_note(errors)}\n"
    assert _schema_violation_line({"status": "completed", "partial": False, "schema_errors": []}) == ""
    assert _schema_violation_line({"status": "completed"}) == ""
