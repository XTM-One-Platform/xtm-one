"""Pre-validation argument coercion + prose-free schema echo.

The failure this pins: a model called ``inspect_my_traces`` with
``{"include_spans": "true", "limit": "1", "period": "24h"}`` - every value a
JSON string - and was told ``'true' is not of type 'boolean'`` followed by
the tool schema, echoed WITH its descriptions and cut at the length cap
before the parameter list was complete.  It retried the identical shape
until its failure budget was spent.  Two deterministic fixes, zero prompt
characters:

* a string that reads unambiguously as the declared primitive is coerced
  BEFORE validation (``"true"`` -> ``True``, ``"1"`` -> ``1``), strictly:
  only when the declared type does not admit ``string``;
* the schema echo strips description-like prose before the cap, so a
  verbose schema is cut in its prose, never in its structure.
"""

from __future__ import annotations

import json

import pytest

from app.llm.base import BaseLLMProvider, ChatMessage, ChatResponse
from app.tools import agent_loop as al
from app.tools.agent_loop import (
    _compact_schema,
    coerce_tool_arguments,
    validate_tool_arguments,
)
from app.tools.base import (
    CallableToolExecutor,
    CompositeToolExecutor,
    ToolCall,
    ToolDefinition,
)

# The real ``inspect_my_traces`` shape (descriptions abridged).
_TRACES_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "include_spans": {
            "type": "boolean",
            "default": True,
            "description": "Include step-level spans. " * 20,
        },
        "limit": {"type": "integer", "minimum": 1, "maximum": 25, "default": 10},
        "model": {"type": "string"},
        "period": {"type": "string", "enum": ["24h", "7d", "30d", "90d"], "default": "7d"},
        "query": {"type": "string"},
        "source": {"type": "string"},
        "status_filter": {"type": "string", "enum": ["", "ok", "error"]},
    },
}


# ── coerce_tool_arguments ─────────────────────────────────────────────────


def test_stringified_scalars_become_the_declared_primitives():
    args = {"include_spans": "true", "limit": "1", "period": "24h", "query": "generate_file"}
    coerced = coerce_tool_arguments(args, _TRACES_SCHEMA)
    assert coerced == {
        "include_spans": True,
        "limit": 1,
        "period": "24h",
        "query": "generate_file",
    }
    # The input is never mutated in place - a fresh dict is returned.
    assert args["include_spans"] == "true"
    # And the coerced call now validates.
    assert validate_tool_arguments("inspect_my_traces", coerced, _TRACES_SCHEMA) is None


def test_identity_is_preserved_when_nothing_changes():
    args = {"include_spans": True, "limit": 3, "period": "7d"}
    assert coerce_tool_arguments(args, _TRACES_SCHEMA) is args
    assert coerce_tool_arguments({}, _TRACES_SCHEMA) == {}
    assert coerce_tool_arguments(None, _TRACES_SCHEMA) is None
    assert coerce_tool_arguments(args, None) is args
    assert coerce_tool_arguments(args, {"type": "object"}) is args


def test_a_string_typed_property_is_never_touched():
    schema = {
        "type": "object",
        "properties": {
            "code": {"type": "string"},
            "either": {"type": ["string", "integer"]},
            "free": {},
            "choice": {"enum": ["1", "2"]},
        },
    }
    args = {"code": "true", "either": "12", "free": "false", "choice": "1"}
    # ``"12"`` for a string-or-integer union stays a string: the model may
    # mean it, and the validator accepts it either way.
    assert coerce_tool_arguments(args, schema) is args


@pytest.mark.parametrize(
    ("declared", "given", "expected"),
    [
        ({"type": "boolean"}, "true", True),
        ({"type": "boolean"}, "False", False),
        ({"type": "boolean"}, " TRUE ", True),
        ({"type": "boolean"}, "yes", "yes"),
        ({"type": "boolean"}, "1", "1"),
        ({"type": "integer"}, "42", 42),
        ({"type": "integer"}, "-7", -7),
        ({"type": "integer"}, "+3", 3),
        ({"type": "integer"}, "4.0", "4.0"),
        ({"type": "integer"}, "four", "four"),
        ({"type": "number"}, "1.5", 1.5),
        ({"type": "number"}, "2", 2),
        ({"type": "number"}, "nan", "nan"),
        ({"type": "number"}, "inf", "inf"),
        ({"type": "number"}, "1e3", 1000.0),
        ({"type": ["integer", "null"]}, "null", None),
        ({"type": ["integer", "null"]}, "5", 5),
        ({"type": "integer"}, "null", "null"),
        ({"type": "array", "items": {"type": "string"}}, '["a", "b"]', ["a", "b"]),
        ({"type": "array"}, "a, b", "a, b"),
        ({"type": "array"}, "[not json", "[not json"),
        ({"type": "object"}, '{"k": 1}', {"k": 1}),
        ({"type": "object"}, '["k"]', '["k"]'),
        ({"type": "array"}, '{"k": 1}', '{"k": 1}'),
        ({"anyOf": [{"type": "integer"}, {"type": "null"}]}, "8", 8),
        ({"oneOf": [{"type": "boolean"}, {"type": "null"}]}, "false", False),
    ],
)
def test_scalar_coercion_matrix(declared, given, expected):
    schema = {"type": "object", "properties": {"value": declared}}
    result = coerce_tool_arguments({"value": given}, schema)
    assert result["value"] == expected
    assert type(result["value"]) is type(expected)


def test_nested_values_are_coerced_through_items_and_properties():
    schema = {
        "type": "object",
        "properties": {
            "ids": {"type": "array", "items": {"type": "integer"}},
            "options": {
                "type": "object",
                "properties": {
                    "dry_run": {"type": "boolean"},
                    "label": {"type": "string"},
                    "nested": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {"n": {"type": "number"}},
                        },
                    },
                },
            },
        },
    }
    args = {
        "ids": ["1", "2", 3],
        "options": {
            "dry_run": "false",
            "label": "true",
            "nested": [{"n": "0.5"}, {"n": 2}],
            "unknown": "true",
        },
    }
    coerced = coerce_tool_arguments(args, schema)
    assert coerced == {
        "ids": [1, 2, 3],
        "options": {
            "dry_run": False,
            "label": "true",
            "nested": [{"n": 0.5}, {"n": 2}],
            "unknown": "true",
        },
    }
    # An undeclared nested key is left alone (the validator judges it), and
    # the original nested containers are not mutated.
    assert args["ids"] == ["1", "2", 3]
    assert args["options"]["dry_run"] == "false"


def test_a_stringified_container_is_coerced_and_then_recursed():
    schema = {
        "type": "object",
        "properties": {"ids": {"type": "array", "items": {"type": "integer"}}},
    }
    assert coerce_tool_arguments({"ids": '["1", "2"]'}, schema) == {"ids": [1, 2]}


def test_undeclared_keys_are_left_for_the_validator():
    schema = {"type": "object", "properties": {"a": {"type": "integer"}}}
    args = {"a": "1", "b": "true"}
    coerced = coerce_tool_arguments(args, schema)
    assert coerced == {"a": 1, "b": "true"}


def test_skip_keys_are_passed_through_verbatim():
    """A ``$collected`` injection is a platform-written JSON string.

    The loop names those keys as ``skip_keys``: whatever the schema declares
    for them, the string reaches the tool as-is - it is excluded from the
    validation for the same reason. Every other key is still coerced.
    """
    schema = {
        "type": "object",
        "properties": {
            "payload": {"type": "object"},
            "count": {"type": "integer"},
        },
    }
    injected = '{"from": "session"}'
    args = {"payload": injected, "count": "3"}
    coerced = coerce_tool_arguments(args, schema, skip_keys={"payload"})
    assert coerced == {"payload": injected, "count": 3}
    assert coerced["payload"] is injected
    # Only the skipped key is stringified: nothing to do, same object back.
    only_skipped = {"payload": injected}
    assert coerce_tool_arguments(only_skipped, schema, skip_keys={"payload"}) is only_skipped
    # An empty / absent skip set changes nothing about the coercion.
    assert coerce_tool_arguments(args, schema, skip_keys=set()) == {
        "payload": {"from": "session"},
        "count": 3,
    }
    assert coerce_tool_arguments(args, schema, skip_keys=None) == {
        "payload": {"from": "session"},
        "count": 3,
    }


def test_uninterpretable_schema_never_raises():
    weird = {"type": "object", "properties": {"a": {"type": 7, "items": "x"}}}
    args = {"a": "1"}
    assert coerce_tool_arguments(args, weird) is args
    weirder = {"type": "object", "properties": "nope"}
    assert coerce_tool_arguments(args, weirder) is args


# ── anyOf / oneOf: a valid value is the model's choice of branch ──────────

_ID_INT_OR_STR = {
    "oneOf": [
        {"type": "object", "properties": {"id": {"type": "integer"}}},
        {"type": "object", "properties": {"id": {"type": "string"}}},
    ]
}


@pytest.mark.parametrize("combinator", ["oneOf", "anyOf"])
def test_a_value_valid_against_the_union_is_left_untouched(combinator):
    """``{"id": "001"}`` IS the string branch: coercing it toward the first
    object alternative would make it ``{"id": 1}`` and lose the zeros with
    nothing ever reporting it."""
    prop = {combinator: _ID_INT_OR_STR["oneOf"]}
    schema = {"type": "object", "properties": {"ref": prop}}
    args = {"ref": {"id": "001"}}
    assert coerce_tool_arguments(args, schema) is args
    assert args["ref"]["id"] == "001"


def test_an_ambiguous_union_is_not_coerced():
    """Two object alternatives and a value valid against neither: the shape
    does not say which branch the model meant, so nothing moves and the
    validator gets to report the mismatch."""
    schema = {
        "type": "object",
        "properties": {
            "ref": {
                "oneOf": [
                    {"type": "object", "properties": {"id": {"type": "integer"}}},
                    {"type": "object", "properties": {"id": {"type": "boolean"}}},
                ]
            }
        },
    }
    args = {"ref": {"id": "true"}}
    assert coerce_tool_arguments(args, schema) is args


def test_a_single_matching_alternative_is_still_coerced():
    """One array alternative next to a string one: an array of stringified
    integers has exactly one branch it can be meant for, so it is coerced
    through that branch's ``items`` - while a plain string stays the string
    branch and a valid integer array stays as sent."""
    prop = {
        "anyOf": [
            {"type": "array", "items": {"type": "integer"}},
            {"type": "string"},
        ]
    }
    schema = {"type": "object", "properties": {"ids": prop}}
    assert coerce_tool_arguments({"ids": ["1", "2"]}, schema) == {"ids": [1, 2]}
    text = {"ids": "1,2"}
    assert coerce_tool_arguments(text, schema) is text
    valid = {"ids": [1, 2]}
    assert coerce_tool_arguments(valid, schema) is valid


def test_a_scalar_union_still_coerces_the_unambiguous_primitive():
    """The guards do not undo the scalar rule: a string under an
    integer-or-null union is not valid as sent and has one primitive it can
    become."""
    schema = {
        "type": "object",
        "properties": {"n": {"anyOf": [{"type": "integer"}, {"type": "null"}]}},
    }
    assert coerce_tool_arguments({"n": "8"}, schema) == {"n": 8}
    assert coerce_tool_arguments({"n": "null"}, schema) == {"n": None}
    already = {"n": 8}
    assert coerce_tool_arguments(already, schema) is already


# ── _compact_schema: structure survives the cap ───────────────────────────


def test_schema_echo_strips_prose_and_keeps_every_property():
    echoed = _compact_schema(_TRACES_SCHEMA)
    assert "Include step-level spans" not in echoed
    assert "[truncated]" not in echoed
    parsed = json.loads(echoed)
    assert set(parsed["properties"]) == set(_TRACES_SCHEMA["properties"])
    assert parsed["properties"]["include_spans"] == {"type": "boolean", "default": True}
    assert parsed["properties"]["period"]["enum"] == ["24h", "7d", "30d", "90d"]
    assert parsed["additionalProperties"] is False


def test_schema_echo_keeps_a_property_named_description():
    # Jira / Notion tools have a PROPERTY called ``description`` - its value is
    # a schema (dict), not prose, and must survive.
    schema = {
        "type": "object",
        "properties": {
            "title": {"type": "string", "description": "Issue title"},
            "description": {"type": "string", "description": "Issue body"},
        },
        "required": ["title", "description"],
    }
    parsed = json.loads(_compact_schema(schema))
    assert parsed["properties"] == {
        "title": {"type": "string"},
        "description": {"type": "string"},
    }
    assert parsed["required"] == ["title", "description"]


def test_schema_echo_still_caps_a_structurally_huge_schema():
    huge = {
        "type": "object",
        "properties": {f"field_{i:04d}": {"type": "string"} for i in range(500)},
    }
    echoed = _compact_schema(huge)
    assert echoed.endswith("... [truncated]")
    assert len(echoed) <= al._VALIDATION_SCHEMA_MAX_CHARS + len("... [truncated]")


def test_validation_feedback_names_every_parameter_of_a_verbose_schema():
    feedback = validate_tool_arguments(
        "inspect_my_traces", {"include_spans": "maybe"}, _TRACES_SCHEMA
    )
    assert feedback is not None
    assert "include_spans" in feedback
    for name in _TRACES_SCHEMA["properties"]:
        assert name in feedback, f"{name} must survive the schema echo"
    assert "Include step-level spans" not in feedback


# ── Integration: the loop coerces before it validates ─────────────────────


class _ScriptedLLM(BaseLLMProvider):
    provider_name = "stub"
    context_window = 100_000

    def __init__(self, script: list[ChatResponse]) -> None:
        self._script = script
        self._idx = 0

    async def chat(self, messages, model=None, tools=None, **kwargs):
        response = self._script[self._idx]
        self._idx += 1
        return response

    async def chat_stream(self, messages, **kwargs):
        yield ""

    async def embed(self, text, model=None):
        return [0.0]

    async def list_models(self):
        return []


@pytest.fixture
def _quiet_langfuse(monkeypatch):
    monkeypatch.setattr(al, "_lf_start_llm_gen", lambda *a, **kw: None)
    monkeypatch.setattr(al, "_lf_end_generation", lambda *a, **kw: None)
    monkeypatch.setattr(al, "_lf_end_span", lambda *a, **kw: None)
    monkeypatch.setattr(al, "_lf_end_tool_span", lambda *a, **kw: None)
    monkeypatch.setattr(al, "_lf_start_iter_span", lambda **kw: None)
    monkeypatch.setattr(al, "_lf_start_tool_span", lambda **kw: None)


def _executor_with_traces_tool() -> tuple[CompositeToolExecutor, list[dict]]:
    executed: list[dict] = []
    callable_exec = CallableToolExecutor()

    async def _inspect(
        include_spans: bool = True,
        limit: int = 10,
        period: str = "7d",
        query: str | None = None,
        **_ignored,
    ) -> str:
        executed.append(
            {"include_spans": include_spans, "limit": limit, "period": period, "query": query}
        )
        return "traces"

    callable_exec.register("inspect_my_traces", _inspect)
    composite = CompositeToolExecutor()
    composite.add_tools(
        [
            ToolDefinition(
                name="inspect_my_traces",
                description="Inspect traces",
                input_schema=_TRACES_SCHEMA,
                source="builtin:inspect_my_traces",
            )
        ],
        callable_exec,
    )
    return composite, executed


@pytest.mark.asyncio
async def test_loop_coerces_stringified_arguments_and_executes(_quiet_langfuse):
    composite, executed = _executor_with_traces_tool()
    llm = _ScriptedLLM(
        [
            ChatResponse(
                content="",
                model="stub",
                stop_reason="tool_calls",
                tool_calls=[
                    ToolCall(
                        id="c1",
                        name="inspect_my_traces",
                        arguments={
                            "include_spans": "true",
                            "limit": "1",
                            "period": "24h",
                            "query": "generate_file",
                        },
                    )
                ],
            ),
            ChatResponse(content="done", model="stub"),
        ]
    )
    messages: list[ChatMessage] = [ChatMessage(role="user", content="go")]

    response = await al.run_agent_loop(
        llm=llm,
        messages=messages,
        tools=composite.all_tools,
        executor=composite,
        max_iterations=5,
        progressive_tools=False,
    )

    assert response.content == "done"
    assert executed == [
        {"include_spans": True, "limit": 1, "period": "24h", "query": "generate_file"}
    ]
    results = [m for m in messages if m.role == "tool" and m.tool_call_id == "c1"]
    assert len(results) == 1
    assert results[0].content == "traces"
    # The assistant turn replayed to the model carries the coerced shape, so
    # the transcript shows a call that validates.
    assistant = [m for m in messages if m.role == "assistant" and m.tool_calls]
    assert assistant[-1].tool_calls[0].arguments["limit"] == 1


@pytest.mark.asyncio
async def test_loop_still_rejects_what_cannot_be_coerced(_quiet_langfuse):
    composite, executed = _executor_with_traces_tool()
    llm = _ScriptedLLM(
        [
            ChatResponse(
                content="",
                model="stub",
                stop_reason="tool_calls",
                tool_calls=[
                    ToolCall(
                        id="c1",
                        name="inspect_my_traces",
                        arguments={"include_spans": "yes please", "limit": "one"},
                    )
                ],
            ),
            ChatResponse(content="done", model="stub"),
        ]
    )
    messages: list[ChatMessage] = [ChatMessage(role="user", content="go")]

    response = await al.run_agent_loop(
        llm=llm,
        messages=messages,
        tools=composite.all_tools,
        executor=composite,
        max_iterations=5,
        progressive_tools=False,
    )

    assert response.content == "done"
    assert executed == []
    feedback = [m for m in messages if m.role == "tool" and m.tool_call_id == "c1"]
    assert len(feedback) == 1
    assert "Invalid arguments for tool inspect_my_traces" in feedback[0].content
    assert "status_filter" in feedback[0].content, "the full parameter list is echoed"
    assert "Include step-level spans" not in feedback[0].content
