import json

import pytest

from app.llm.base import BaseLLMProvider, ChatMessage, ChatResponse
from app.tools import agent_loop as al
from app.tools.base import (
    CallableToolExecutor,
    CompositeToolExecutor,
    ToolCall,
    ToolDefinition,
)


class _ScriptedLLM(BaseLLMProvider):
    provider_name = "stub"
    context_window = 100_000

    def __init__(self, script: list[ChatResponse]) -> None:
        self._script = script
        self.call_count = 0

    async def chat(self, messages, model=None, tools=None, **kwargs):
        response = self._script[self.call_count]
        self.call_count += 1
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


def _tool_call_response() -> ChatResponse:
    return ChatResponse(
        content="",
        model="stub",
        stop_reason="tool_calls",
        tool_calls=[
            ToolCall(
                id="validator-call",
                name="stix_bundle_validator",
                arguments={"bundle": {"type": "bundle", "objects": []}},
            )
        ],
    )


def _executor_with_validator_payload(payload: dict) -> CompositeToolExecutor:
    callable_exec = CallableToolExecutor()

    async def _validator(bundle: dict) -> str:
        return json.dumps(payload, ensure_ascii=False)

    callable_exec.register("stix_bundle_validator", _validator)
    composite = CompositeToolExecutor()
    composite.add_tools(
        [
            ToolDefinition(
                name="stix_bundle_validator",
                description="Validate a STIX bundle",
                input_schema={
                    "type": "object",
                    "properties": {"bundle": {"type": "object"}},
                    "required": ["bundle"],
                },
                source="builtin:stix_bundle_validator",
            )
        ],
        callable_exec,
    )
    return composite


@pytest.mark.asyncio
async def test_valid_terminal_tool_result_skips_final_llm_call(_quiet_langfuse):
    bundle = {"type": "bundle", "objects": [{"type": "campaign", "name": "x"}]}
    composite = _executor_with_validator_payload(
        {"valid": True, "error_count": 0, "errors": [], "bundle": bundle}
    )
    llm = _ScriptedLLM([_tool_call_response()])
    messages: list[ChatMessage] = [ChatMessage(role="user", content="extract")]
    events: list[dict] = []

    async def _on_event(event: dict) -> None:
        events.append(event)

    response = await al.run_agent_loop(
        llm=llm,
        messages=messages,
        tools=composite.all_tools,
        executor=composite,
        max_iterations=5,
        progressive_tools=False,
        on_event=_on_event,
        finalize_output_schema={
            "type": "object",
            "properties": {"type": {"const": "bundle"}},
            "required": ["type"],
        },
        finalize_on_tool={
            "tool_name": "stix_bundle_validator",
            "valid_key": "valid",
            "output_key": "bundle",
        },
    )

    assert json.loads(response.content) == bundle
    assert llm.call_count == 1
    # The fast path must report the same model the provider returned (the one
    # from the tool-calling response), not the requested override / "unknown".
    assert response.model == "stub"
    # The loop exited via the terminal-tool mechanism, so the returned response
    # must carry stop_reason="finalize_tool" (matching the iteration-outcome
    # metadata), not the default "end_turn".
    assert response.stop_reason == "finalize_tool"
    assert [m for m in messages if m.role == "tool"] == []
    assert any(
        event.get("status") == "terminal_tool_decision"
        and event.get("reason") == "final_llm_call_skipped"
        for event in events
    )


@pytest.mark.asyncio
async def test_invalid_terminal_tool_result_continues_to_next_llm_call(
    _quiet_langfuse,
):
    composite = _executor_with_validator_payload(
        {
            "valid": False,
            "error_count": 1,
            "errors": ["missing is_family"],
            "bundle": {"type": "bundle", "objects": []},
        }
    )
    llm = _ScriptedLLM(
        [
            _tool_call_response(),
            ChatResponse(content="repaired final answer", model="stub"),
        ]
    )
    messages: list[ChatMessage] = [ChatMessage(role="user", content="extract")]
    events: list[dict] = []

    async def _on_event(event: dict) -> None:
        events.append(event)

    response = await al.run_agent_loop(
        llm=llm,
        messages=messages,
        tools=composite.all_tools,
        executor=composite,
        max_iterations=5,
        progressive_tools=False,
        on_event=_on_event,
        finalize_on_tool={
            "tool_name": "stix_bundle_validator",
            "valid_key": "valid",
            "output_key": "bundle",
            "validate_output_schema": True,
        },
    )

    assert response.content == "repaired final answer"
    assert llm.call_count == 2
    tool_messages = [m for m in messages if m.role == "tool"]
    assert len(tool_messages) == 1
    assert json.loads(tool_messages[0].content)["valid"] is False
    assert any(
        event.get("status") == "terminal_tool_decision"
        and event.get("reason") == "validation_not_successful"
        for event in events
    )


@pytest.mark.asyncio
async def test_terminal_tool_output_must_match_structured_output_schema(
    _quiet_langfuse,
):
    composite = _executor_with_validator_payload(
        {
            "valid": True,
            "error_count": 0,
            "errors": [],
            "bundle": {"type": "bundle", "objects": []},
        }
    )
    llm = _ScriptedLLM(
        [
            _tool_call_response(),
            ChatResponse(content="schema fallback", model="stub"),
        ]
    )
    messages: list[ChatMessage] = [ChatMessage(role="user", content="extract")]
    events: list[dict] = []

    async def _on_event(event: dict) -> None:
        events.append(event)

    response = await al.run_agent_loop(
        llm=llm,
        messages=messages,
        tools=composite.all_tools,
        executor=composite,
        max_iterations=5,
        progressive_tools=False,
        on_event=_on_event,
        finalize_output_schema={
            "type": "object",
            "properties": {"required_marker": {"type": "string"}},
            "required": ["required_marker"],
        },
        finalize_on_tool={
            "tool_name": "stix_bundle_validator",
            "valid_key": "valid",
            "output_key": "bundle",
            "validate_output_schema": True,
        },
    )

    assert response.content == "schema fallback"
    assert llm.call_count == 2
    assert len([m for m in messages if m.role == "tool"]) == 1
    assert any(
        event.get("status") == "terminal_tool_decision"
        and event.get("reason") == "structured_output_validation_failed"
        for event in events
    )


@pytest.mark.asyncio
async def test_terminal_tool_skips_schema_validation_unless_explicitly_enabled(
    _quiet_langfuse,
):
    bundle = {"type": "bundle", "objects": []}
    composite = _executor_with_validator_payload(
        {"valid": True, "error_count": 0, "errors": [], "bundle": bundle}
    )
    llm = _ScriptedLLM([_tool_call_response()])
    messages: list[ChatMessage] = [ChatMessage(role="user", content="extract")]

    response = await al.run_agent_loop(
        llm=llm,
        messages=messages,
        tools=composite.all_tools,
        executor=composite,
        max_iterations=5,
        progressive_tools=False,
        finalize_output_schema={
            "type": "object",
            "properties": {"required_marker": {"type": "string"}},
            "required": ["required_marker"],
        },
        finalize_on_tool={
            "tool_name": "stix_bundle_validator",
            "valid_key": "valid",
            "output_key": "bundle",
        },
    )

    assert json.loads(response.content) == bundle
    assert llm.call_count == 1
    assert response.model == "stub"
    assert response.stop_reason == "finalize_tool"
    assert [m for m in messages if m.role == "tool"] == []


@pytest.mark.asyncio
async def test_terminal_tool_tolerates_mistyped_key_config(_quiet_langfuse):
    # A mis-typed persisted config (non-string valid_key/output_key) must not
    # raise ``TypeError: unhashable type`` and abort the loop — it should fall
    # back to the documented defaults so the fast path still finalizes.
    bundle = {"type": "bundle", "objects": []}
    composite = _executor_with_validator_payload(
        {"valid": True, "error_count": 0, "errors": [], "bundle": bundle}
    )
    llm = _ScriptedLLM([_tool_call_response()])
    messages: list[ChatMessage] = [ChatMessage(role="user", content="extract")]

    response = await al.run_agent_loop(
        llm=llm,
        messages=messages,
        tools=composite.all_tools,
        executor=composite,
        max_iterations=5,
        progressive_tools=False,
        finalize_on_tool={
            "tool_name": "stix_bundle_validator",
            "valid_key": ["unhashable"],
            "output_key": {"also": "unhashable"},
        },
    )

    assert json.loads(response.content) == bundle
    assert llm.call_count == 1
    assert response.stop_reason == "finalize_tool"
    assert [m for m in messages if m.role == "tool"] == []


@pytest.mark.asyncio
async def test_terminal_tool_tolerates_union_type_output_schema(_quiet_langfuse):
    # A JSON Schema union type declares ``type`` as a list (e.g.
    # ``["object", "null"]``). A list is unhashable, so raw-schema detection
    # must not raise ``TypeError: unhashable type: 'list'`` and abort the loop.
    # The schema is still recognized as a raw JSON Schema and used to validate
    # the terminal output, so the fast path finalizes without a second LLM call.
    bundle = {"type": "bundle", "objects": []}
    composite = _executor_with_validator_payload(
        {"valid": True, "error_count": 0, "errors": [], "bundle": bundle}
    )
    llm = _ScriptedLLM([_tool_call_response()])
    messages: list[ChatMessage] = [ChatMessage(role="user", content="extract")]

    response = await al.run_agent_loop(
        llm=llm,
        messages=messages,
        tools=composite.all_tools,
        executor=composite,
        max_iterations=5,
        progressive_tools=False,
        finalize_output_schema={"type": ["object", "null"]},
        finalize_on_tool={
            "tool_name": "stix_bundle_validator",
            "valid_key": "valid",
            "output_key": "bundle",
            "validate_output_schema": True,
        },
    )

    assert json.loads(response.content) == bundle
    assert llm.call_count == 1
    assert response.stop_reason == "finalize_tool"
    assert [m for m in messages if m.role == "tool"] == []
