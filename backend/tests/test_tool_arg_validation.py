"""Tests for pre-execution tool-argument validation (CORE-2).

Tool-call arguments are validated against the tool's ``input_schema`` BEFORE
the call is dispatched to the executor.  Invalid calls must NOT execute —
the loop appends a structured tool-result error so the model self-corrects
on the next iteration — and validation failures must count toward the
existing per-tool failure tracking so repeated garbage still trips the
``_MAX_TOOL_FAILURES`` nudge.

Two layers are covered:

* **Unit** — the pure ``validate_tool_arguments`` helper.
* **Integration** — ``run_agent_loop`` with a scripted stub LLM + a real
  ``CompositeToolExecutor``, mirroring the patterns used by the other
  agent-loop test modules (see ``test_progressive_tool_disclosure.py``).
"""

from __future__ import annotations

import pytest

from app.llm.base import BaseLLMProvider, ChatMessage, ChatResponse
from app.tools import agent_loop as al
from app.tools.agent_loop import validate_tool_arguments
from app.tools.base import (
    CallableToolExecutor,
    CompositeToolExecutor,
    ToolCall,
    ToolDefinition,
)

_SCHEMA = {
    "type": "object",
    "properties": {
        "title": {"type": "string"},
        "count": {"type": "integer"},
    },
    "required": ["title"],
}


# ─────────────────────────────────────────────────────────────────────────
# Unit: validate_tool_arguments
# ─────────────────────────────────────────────────────────────────────────


def test_valid_arguments_return_none():
    assert validate_tool_arguments("t", {"title": "hello"}, _SCHEMA) is None
    assert (
        validate_tool_arguments("t", {"title": "hello", "count": 3}, _SCHEMA)
        is None
    )


def test_missing_required_field_is_reported():
    feedback = validate_tool_arguments("create_issue", {}, _SCHEMA)
    assert feedback is not None
    assert "Invalid arguments for tool create_issue" in feedback
    assert "title" in feedback
    assert "required" in feedback
    assert "Expected schema:" in feedback
    assert "NOT executed" in feedback


def test_wrong_type_is_reported():
    feedback = validate_tool_arguments(
        "create_issue", {"title": "x", "count": "three"}, _SCHEMA
    )
    assert feedback is not None
    assert "count" in feedback
    assert "integer" in feedback


def test_raw_fallback_arguments_are_reported_as_malformed_json():
    """The ``{"raw": ...}`` marker emitted by ``parse_tool_arguments`` on
    malformed JSON must produce the structured feedback path."""
    feedback = validate_tool_arguments(
        "create_issue", {"raw": '{"title": "unterminated'}, _SCHEMA
    )
    assert feedback is not None
    assert "not valid JSON" in feedback
    assert "Expected schema:" in feedback


def test_raw_is_allowed_when_schema_declares_it():
    schema = {
        "type": "object",
        "properties": {"raw": {"type": "string"}},
        "required": ["raw"],
    }
    assert validate_tool_arguments("t", {"raw": "payload"}, _SCHEMA) is not None
    assert validate_tool_arguments("t", {"raw": "payload"}, schema) is None


def test_raw_falls_through_for_free_form_schema():
    """A schema with no declared ``properties`` (free-form object) cannot
    distinguish the malformed-JSON marker from a legitimate ``{"raw": ...}``
    argument — validation must defer to plain schema checking, which passes
    here, preserving pre-validation behaviour (execute the tool)."""
    free_form = {"type": "object", "additionalProperties": True}
    assert validate_tool_arguments("t", {"raw": "payload"}, free_form) is None


def test_raw_is_flagged_for_empty_properties_schema():
    """No-arg tools default to ``properties: {}`` (the ``ToolDefinition``
    schema default) — a ``{"raw": ...}`` payload there can only be the
    malformed-JSON marker and must be flagged.  A truthiness check on the
    empty declared-property set previously let the marker fall through to
    plain validation and execute."""
    no_arg = {"type": "object", "properties": {}}
    feedback = validate_tool_arguments("t", {"raw": '{"oops": tru'}, no_arg)
    assert feedback is not None
    assert "not valid JSON" in feedback
    assert "NOT executed" in feedback


def test_multiple_errors_with_mixed_path_types_sort_deterministically():
    """Violation paths mix ints (array indices) and strs (property names);
    the sort key must stringify elements so ``sorted`` can never raise
    ``TypeError`` — which the broad schema-interpretation except would
    otherwise misread as "uninterpretable schema" and silently skip
    validation."""
    schema = {
        "type": "object",
        "properties": {
            "tags": {"type": "array", "items": {"type": "string"}},
            "title": {"type": "string"},
        },
        "required": ["title"],
    }
    feedback = validate_tool_arguments(
        "t", {"tags": ["ok", 7], "title": 5}, schema
    )
    assert feedback is not None
    assert "tags.1" in feedback
    assert "title" in feedback


def test_missing_or_empty_schema_skips_validation():
    assert validate_tool_arguments("t", {"anything": 1}, None) is None
    assert validate_tool_arguments("t", {"anything": 1}, {}) is None


def test_uninterpretable_schema_never_blocks_execution():
    # ``required`` must be an array of strings — an invalid schema must be
    # skipped, not treated as a validation failure.
    bad_schema = {"type": "object", "required": "title"}
    assert validate_tool_arguments("t", {}, bad_schema) is None


def test_none_arguments_validate_as_empty_object():
    feedback = validate_tool_arguments("t", None, _SCHEMA)
    assert feedback is not None
    assert "title" in feedback


def _count_schema_checks(monkeypatch) -> list[dict]:
    import jsonschema

    checked: list[dict] = []
    real_validator_for = jsonschema.validators.validator_for

    def _validator_for(schema, *args, **kwargs):
        cls = real_validator_for(schema, *args, **kwargs)
        original = cls.check_schema
        if not getattr(original, "_counted", False):

            def _check(schema, *a, **k):
                checked.append(schema)
                return original(schema, *a, **k)

            _check._counted = True
            monkeypatch.setattr(cls, "check_schema", staticmethod(_check))
        return cls

    monkeypatch.setattr(jsonschema.validators, "validator_for", _validator_for)
    return checked


def test_a_tool_schema_is_checked_once_across_calls(monkeypatch):
    """The metaschema check costs ~100 ms on a large MCP schema; it ran on
    every call. The verdicts are unchanged."""
    checked = _count_schema_checks(monkeypatch)
    schema = {
        "type": "object",
        "properties": {"title": {"type": "string"}, "count": {"type": "integer"}},
        "required": ["title"],
    }

    verdicts = [
        validate_tool_arguments("t", {"title": "ok"}, schema),
        validate_tool_arguments("t", {"count": "three"}, schema),
        validate_tool_arguments("t", {"title": "ok", "count": 3}, schema),
    ]

    assert len(checked) == 1
    assert verdicts[0] is None and verdicts[2] is None
    assert "title" in verdicts[1] and "count" in verdicts[1]


def test_an_uninterpretable_schema_is_checked_and_skipped_on_every_call(monkeypatch):
    checked = _count_schema_checks(monkeypatch)
    bad_schema = {"type": "object", "required": "title"}

    assert validate_tool_arguments("t", {}, bad_schema) is None
    assert validate_tool_arguments("t", {}, bad_schema) is None
    assert len(checked) == 2


def test_the_collected_injection_path_reuses_its_reduced_schema(monkeypatch):
    checked = _count_schema_checks(monkeypatch)
    schema = {
        "type": "object",
        "properties": {"items": {"type": "array"}, "title": {"type": "string"}},
        "required": ["items", "title"],
    }

    for _ in range(3):
        assert (
            al._validate_tool_arguments_excluding("t", {"items": "[1, 2]", "title": "x"}, schema, {"items"})
            is None
        )
    missing = al._validate_tool_arguments_excluding("t", {"items": "[1, 2]"}, schema, {"items"})

    assert len(checked) == 1
    assert missing is not None and "title" in missing


# ─────────────────────────────────────────────────────────────────────────
# Integration: run_agent_loop
# ─────────────────────────────────────────────────────────────────────────


class _ScriptedLLM(BaseLLMProvider):
    """Stub LLM returning a pre-scripted sequence of responses."""

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
    """Neutralise Langfuse plumbing so the loop runs without real tracing."""
    monkeypatch.setattr(al, "_lf_start_llm_gen", lambda *a, **kw: None)
    monkeypatch.setattr(al, "_lf_end_generation", lambda *a, **kw: None)
    monkeypatch.setattr(al, "_lf_end_span", lambda *a, **kw: None)
    monkeypatch.setattr(al, "_lf_end_tool_span", lambda *a, **kw: None)
    monkeypatch.setattr(al, "_lf_start_iter_span", lambda **kw: None)
    monkeypatch.setattr(al, "_lf_start_tool_span", lambda **kw: None)


def _executor_with_create_issue() -> tuple[CompositeToolExecutor, list[dict]]:
    """Composite executor with one ``create_issue`` tool. Returns the
    executor and a list recording the kwargs of every actual execution."""
    executed: list[dict] = []
    callable_exec = CallableToolExecutor()

    async def _create_issue(title: str, count: int = 1) -> str:
        executed.append({"title": title, "count": count})
        return f"created {title}"

    callable_exec.register("create_issue", _create_issue)
    composite = CompositeToolExecutor()
    composite.add_tools(
        [
            ToolDefinition(
                name="create_issue",
                description="Create an issue",
                input_schema=_SCHEMA,
                source="mcp:GitHub",
            )
        ],
        callable_exec,
    )
    return composite, executed


def _tool_call_response(arguments: dict | None, call_id: str = "c1") -> ChatResponse:
    return ChatResponse(
        content="",
        model="stub",
        stop_reason="tool_calls",
        tool_calls=[ToolCall(id=call_id, name="create_issue", arguments=arguments or {})],
    )


@pytest.mark.asyncio
async def test_invalid_args_produce_feedback_and_skip_executor(_quiet_langfuse):
    composite, executed = _executor_with_create_issue()
    llm = _ScriptedLLM(
        [
            _tool_call_response({"count": 2}),  # missing required "title"
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
    assert executed == [], "tool must NOT execute on invalid arguments"
    feedback = [
        m
        for m in messages
        if m.role == "tool" and m.tool_call_id == "c1"
    ]
    assert len(feedback) == 1
    assert "Invalid arguments for tool create_issue" in feedback[0].content
    assert "title" in feedback[0].content
    assert "Expected schema:" in feedback[0].content


@pytest.mark.asyncio
async def test_valid_args_pass_through_to_executor(_quiet_langfuse):
    composite, executed = _executor_with_create_issue()
    llm = _ScriptedLLM(
        [
            _tool_call_response({"title": "hello", "count": 2}),
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
    assert executed == [{"title": "hello", "count": 2}]
    results = [m for m in messages if m.role == "tool" and m.tool_call_id == "c1"]
    assert len(results) == 1
    assert results[0].content == "created hello"


@pytest.mark.asyncio
async def test_raw_malformed_args_produce_feedback(_quiet_langfuse):
    """The ``{"raw": ...}`` fallback (malformed tool-call JSON) must hit the
    same structured feedback path — no execution, self-correction signal."""
    composite, executed = _executor_with_create_issue()
    llm = _ScriptedLLM(
        [
            _tool_call_response({"raw": '{"title": "unterminated'}),
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
    assert "not valid JSON" in feedback[0].content


@pytest.mark.asyncio
async def test_repeated_invalid_calls_trip_failure_nudge(_quiet_langfuse):
    """Validation failures must count toward ``tool_error_counts`` so that
    ``_MAX_TOOL_FAILURES`` consecutive garbage calls inject the
    repeated-failure nudge (proving the counter incremented)."""
    composite, executed = _executor_with_create_issue()
    invalid = {"count": 1}  # missing required "title"
    llm = _ScriptedLLM(
        [
            _tool_call_response(invalid, call_id="c1"),
            _tool_call_response(invalid, call_id="c2"),
            ChatResponse(content="giving up", model="stub"),
        ]
    )
    messages: list[ChatMessage] = [ChatMessage(role="user", content="go")]

    response = await al.run_agent_loop(
        llm=llm,
        messages=messages,
        tools=composite.all_tools,
        executor=composite,
        max_iterations=8,
        progressive_tools=False,
    )

    assert response.content == "giving up"
    assert executed == []
    feedback = [
        m
        for m in messages
        if m.role == "tool" and "Invalid arguments" in (m.content or "")
    ]
    assert len(feedback) == 2
    nudges = [
        m
        for m in messages
        if m.role == "system"
        and "repeated tool failure" in (m.content or "")
        and "create_issue" in (m.content or "")
    ]
    assert len(nudges) == 1, (
        "two consecutive validation failures must trip the "
        "_MAX_TOOL_FAILURES repeated-failure nudge"
    )


@pytest.mark.asyncio
async def test_parallel_calls_validate_independently(_quiet_langfuse):
    """In a multi-call turn, invalid calls are rejected with feedback while
    valid calls still execute."""
    composite, executed = _executor_with_create_issue()
    llm = _ScriptedLLM(
        [
            ChatResponse(
                content="",
                model="stub",
                stop_reason="tool_calls",
                tool_calls=[
                    ToolCall(id="bad", name="create_issue", arguments={}),
                    ToolCall(
                        id="good",
                        name="create_issue",
                        arguments={"title": "ok"},
                    ),
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
    assert executed == [{"title": "ok", "count": 1}]
    bad_results = [m for m in messages if m.role == "tool" and m.tool_call_id == "bad"]
    good_results = [m for m in messages if m.role == "tool" and m.tool_call_id == "good"]
    assert len(bad_results) == 1
    assert "Invalid arguments" in bad_results[0].content
    assert len(good_results) == 1
    assert good_results[0].content == "created ok"


def _executor_with_store_items() -> tuple[CompositeToolExecutor, list]:
    """Composite executor with one ``store_items`` tool whose ``items``
    parameter is declared as an array. Returns the executor and a list
    recording the kwargs of every actual execution."""
    executed: list = []
    callable_exec = CallableToolExecutor()

    async def _store_items(items) -> str:
        executed.append(items)
        return "stored"

    callable_exec.register("store_items", _store_items)
    composite = CompositeToolExecutor()
    composite.add_tools(
        [
            ToolDefinition(
                name="store_items",
                description="Store a list of items",
                input_schema={
                    "type": "object",
                    "properties": {"items": {"type": "array"}},
                    "required": ["items"],
                },
                source="builtin:sub_agent",
            )
        ],
        callable_exec,
    )
    return composite, executed


@pytest.mark.asyncio
async def test_session_var_injected_call_skips_validation(_quiet_langfuse):
    """The ``$collected`` session-var indirection replaces the variable name
    with a platform-controlled JSON *string*, regardless of the declared
    parameter type.  The injected key is EXCLUDED from validation — otherwise
    an array/object-typed parameter would reject the documented flow with
    feedback the model cannot act on (it is told to pass the variable name
    as a string).  Here ``items`` is the only argument, so the call executes."""
    import json as _json

    composite, executed = _executor_with_store_items()
    llm = _ScriptedLLM(
        [
            ChatResponse(
                content="",
                model="stub",
                stop_reason="tool_calls",
                tool_calls=[
                    ToolCall(
                        id="c1",
                        name="store_items",
                        arguments={"items": "$collected"},
                    )
                ],
            ),
            ChatResponse(content="done", model="stub"),
        ]
    )
    messages: list[ChatMessage] = [ChatMessage(role="user", content="go")]
    collected = {"results": [1, 2, 3]}

    response = await al.run_agent_loop(
        llm=llm,
        messages=messages,
        tools=composite.all_tools,
        executor=composite,
        max_iterations=5,
        progressive_tools=False,
        session_vars={"$collected": collected},
    )

    assert response.content == "done"
    assert executed == [_json.dumps(collected, ensure_ascii=False)], (
        "the injected JSON string must reach the executor unvalidated"
    )
    results = [m for m in messages if m.role == "tool" and m.tool_call_id == "c1"]
    assert len(results) == 1
    assert results[0].content == "stored"


@pytest.mark.asyncio
async def test_non_injected_call_still_validates_array_param(_quiet_langfuse):
    """The session-var skip is narrow: a plain string that is NOT a session
    variable must still fail validation against an array-typed parameter."""
    composite, executed = _executor_with_store_items()
    llm = _ScriptedLLM(
        [
            ChatResponse(
                content="",
                model="stub",
                stop_reason="tool_calls",
                tool_calls=[
                    ToolCall(
                        id="c1",
                        name="store_items",
                        arguments={"items": "not-a-session-var"},
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
        session_vars={"$collected": {"results": []}},
    )

    assert response.content == "done"
    assert executed == []
    feedback = [m for m in messages if m.role == "tool" and m.tool_call_id == "c1"]
    assert len(feedback) == 1
    assert "Invalid arguments for tool store_items" in feedback[0].content


def _executor_with_store_name() -> tuple[CompositeToolExecutor, list]:
    """Composite executor with one ``store_name`` tool whose ``name``
    parameter is declared as a string scalar."""
    executed: list = []
    callable_exec = CallableToolExecutor()

    async def _store_name(name) -> str:
        executed.append(name)
        return "stored"

    callable_exec.register("store_name", _store_name)
    composite = CompositeToolExecutor()
    composite.add_tools(
        [
            ToolDefinition(
                name="store_name",
                description="Store a single name",
                input_schema={
                    "type": "object",
                    "properties": {"name": {"type": "string"}},
                    "required": ["name"],
                },
                source="builtin:sub_agent",
            )
        ],
        callable_exec,
    )
    return composite, executed


@pytest.mark.asyncio
async def test_scalar_session_injection_is_validated_and_native(_quiet_langfuse):
    """A per-item path like ``$collected.rules[i].name`` resolves to a native
    SCALAR. Unlike the whole-object JSON-string form, a scalar can be validated
    against the declared type, so validation must still run (not be bypassed)
    and the executor must receive the real value (not a JSON-quoted string)."""
    composite, executed = _executor_with_store_name()
    llm = _ScriptedLLM(
        [
            ChatResponse(
                content="",
                model="stub",
                stop_reason="tool_calls",
                tool_calls=[
                    ToolCall(
                        id="c1",
                        name="store_name",
                        arguments={"name": "$collected.rules[0].name"},
                    )
                ],
            ),
            ChatResponse(content="done", model="stub"),
        ]
    )
    messages: list[ChatMessage] = [ChatMessage(role="user", content="go")]
    collected = {"rules": {"_item_0": {"name": "rule-0"}}}

    response = await al.run_agent_loop(
        llm=llm,
        messages=messages,
        tools=composite.all_tools,
        executor=composite,
        max_iterations=5,
        progressive_tools=False,
        session_vars={"$collected": collected},
    )

    assert response.content == "done"
    # Native string reached the executor — NOT a JSON-encoded '"rule-0"'.
    assert executed == ["rule-0"]
    results = [m for m in messages if m.role == "tool" and m.tool_call_id == "c1"]
    assert len(results) == 1
    assert results[0].content == "stored"


def _executor_with_named_items() -> tuple[CompositeToolExecutor, list]:
    """Composite executor with a ``store_named_items`` tool that declares BOTH
    an array ``items`` param (the $collected bulk target) and a required string
    ``name`` param, so a bulk injection on ``items`` can be combined with a
    mistake on ``name``."""
    executed: list = []
    callable_exec = CallableToolExecutor()

    async def _store_named_items(items, name) -> str:
        executed.append({"items": items, "name": name})
        return "stored"

    callable_exec.register("store_named_items", _store_named_items)
    composite = CompositeToolExecutor()
    composite.add_tools(
        [
            ToolDefinition(
                name="store_named_items",
                description="Store a named list of items",
                input_schema={
                    "type": "object",
                    "properties": {
                        "items": {"type": "array"},
                        "name": {"type": "string"},
                    },
                    "required": ["items", "name"],
                },
                source="builtin:sub_agent",
            )
        ],
        callable_exec,
    )
    return composite, executed


@pytest.mark.asyncio
async def test_bulk_injection_still_validates_other_missing_required_arg(_quiet_langfuse):
    """A dict/list `$collected` injection no longer bypasses validation for the
    WHOLE call - only the injected key is excluded. A missing required OTHER
    argument on the same call must still be caught."""
    composite, executed = _executor_with_named_items()
    llm = _ScriptedLLM(
        [
            ChatResponse(
                content="",
                model="stub",
                stop_reason="tool_calls",
                tool_calls=[
                    ToolCall(
                        id="c1",
                        name="store_named_items",
                        arguments={"items": "$collected"},  # 'name' missing
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
        session_vars={"$collected": {"results": [1, 2, 3]}},
    )

    assert response.content == "done"
    assert executed == []
    feedback = [m for m in messages if m.role == "tool" and m.tool_call_id == "c1"]
    assert len(feedback) == 1
    assert "Invalid arguments for tool store_named_items" in feedback[0].content
    assert "name" in feedback[0].content


@pytest.mark.asyncio
async def test_bulk_injection_executes_when_other_args_valid(_quiet_langfuse):
    """When the non-injected arguments are valid, the call executes: the
    injected key receives the JSON string and the validated `name` passes
    through natively."""
    import json as _json

    composite, executed = _executor_with_named_items()
    llm = _ScriptedLLM(
        [
            ChatResponse(
                content="",
                model="stub",
                stop_reason="tool_calls",
                tool_calls=[
                    ToolCall(
                        id="c1",
                        name="store_named_items",
                        arguments={"items": "$collected", "name": "ok"},
                    )
                ],
            ),
            ChatResponse(content="done", model="stub"),
        ]
    )
    messages: list[ChatMessage] = [ChatMessage(role="user", content="go")]
    collected = {"results": [1, 2, 3]}

    response = await al.run_agent_loop(
        llm=llm,
        messages=messages,
        tools=composite.all_tools,
        executor=composite,
        max_iterations=5,
        progressive_tools=False,
        session_vars={"$collected": collected},
    )

    assert response.content == "done"
    assert executed == [
        {"items": _json.dumps(collected, ensure_ascii=False), "name": "ok"}
    ]


@pytest.mark.asyncio
async def test_scalar_session_injection_type_mismatch_is_rejected(_quiet_langfuse):
    """The narrow validation bypass is only for the JSON-string bulk form. A
    resolved scalar whose type violates the schema (string into an array param)
    must be REJECTED — previously every injection bypassed validation, so this
    type error would have slipped through and executed."""
    composite, executed = _executor_with_store_items()
    llm = _ScriptedLLM(
        [
            ChatResponse(
                content="",
                model="stub",
                stop_reason="tool_calls",
                tool_calls=[
                    ToolCall(
                        id="c1",
                        name="store_items",
                        arguments={"items": "$collected.rules[0].name"},
                    )
                ],
            ),
            ChatResponse(content="done", model="stub"),
        ]
    )
    messages: list[ChatMessage] = [ChatMessage(role="user", content="go")]
    collected = {"rules": {"_item_0": {"name": "rule-0"}}}

    response = await al.run_agent_loop(
        llm=llm,
        messages=messages,
        tools=composite.all_tools,
        executor=composite,
        max_iterations=5,
        progressive_tools=False,
        session_vars={"$collected": collected},
    )

    assert response.content == "done"
    assert executed == []
    feedback = [m for m in messages if m.role == "tool" and m.tool_call_id == "c1"]
    assert len(feedback) == 1
    assert "Invalid arguments for tool store_items" in feedback[0].content
