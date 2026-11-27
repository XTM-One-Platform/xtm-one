"""#2686: a write call that retypes a ``$collected`` field is refused.

A run that holds converted rules in ``$collected`` must hand them to a write
tool by reference (``$collected.rules[i].pattern``). A literal that is a near
copy of a collected field (the model's own rewrite of the rule) is refused with
the path to pass; an exact copy, a short value or free text passes.
"""

import json

from app.services.builtin_tools.handover import _collected_summary
from app.tools.agent_loop import _retyped_collected_error
from app.tools.base import CallableToolExecutor, CompositeToolExecutor, ToolDefinition

_PATTERN = (
    "title: Suspicious PowerShell download cradle\n"
    "logsource:\n  product: windows\n  category: process_creation\n"
    "detection:\n  selection:\n    CommandLine|contains:\n      - 'IEX (New-Object Net.WebClient)'\n"
    "  condition: selection\n"
)
_DESCRIPTION = "Detects a PowerShell download cradle used by the intrusion set to stage its loader."
_SESSION = {
    "$collected": {
        "rules": {
            "_item_0": {"name": "cradle", "description": _DESCRIPTION, "pattern": _PATTERN},
            "_item_1": {"name": "other", "description": "x" * 60, "pattern": "y" * 200},
        }
    }
}


def test_retyped_pattern_is_refused_with_the_path() -> None:
    retyped = _PATTERN.replace("Suspicious", "Malicious").replace("condition: selection", "condition: all of them")
    error = _retyped_collected_error({"pattern": retyped, "pattern_type": "sigma"}, _SESSION)
    assert error is not None
    assert "$collected.rules[0].pattern" in error
    assert "NOT executed" in error


def test_exact_copy_passes() -> None:
    assert _retyped_collected_error({"pattern": _PATTERN, "description": _DESCRIPTION}, _SESSION) is None


def test_short_values_and_unrelated_text_pass() -> None:
    args = {
        "name": "cradle v2",
        "pattern_type": "sigma",
        "description": "A hand-written summary of the whole hunt package.",
    }
    assert _retyped_collected_error(args, _SESSION) is None


def test_update_field_value_is_compared_with_the_named_field() -> None:
    retyped = _PATTERN.replace("Net.WebClient", "System.Net.WebClient")
    error = _retyped_collected_error({"id": "indicator--1", "key": "pattern", "value": retyped}, _SESSION)
    assert error is not None
    assert "$collected.rules[0].pattern" in error
    assert "'value'" in error


def test_no_collected_no_check() -> None:
    assert _retyped_collected_error({"pattern": _PATTERN + " "}, {}) is None
    assert _retyped_collected_error({"pattern": _PATTERN + " "}, None) is None


def test_list_shaped_collected_and_quoted_key() -> None:
    session = {"$collected": {"my-rules": [{"pattern": _PATTERN}]}}
    error = _retyped_collected_error({"pattern": _PATTERN.replace("windows", "linux")}, session)
    assert error is not None
    assert '$collected["my-rules"][0].pattern' in error


class _FieldGraphQL:
    """Stub of ``knowledge._graphql``: one indicator whose STIX form holds a pattern."""

    def __init__(self, stored: str) -> None:
        self.stored = stored
        self.patches: list[dict[str, object]] = []

    async def __call__(self, client, base_url, headers, query, variables=None):  # type: ignore[no-untyped-def]
        if "toStix" in query:
            return {"stixCoreObject": {"toStix": json.dumps({"pattern": self.stored})}}, None
        if "fieldPatch" in query:
            self.patches.append(variables or {})
            return {"stixDomainObjectEdit": {"fieldPatch": {"id": "indicator--1", "entity_type": "Indicator"}}}, None
        return {}, None


def _field_executor(monkeypatch, stored: str):  # type: ignore[no-untyped-def]
    import app.integrations.providers.opencti.knowledge as knowledge_mod
    from app.integrations.providers.opencti import build_tools

    stub = _FieldGraphQL(stored)
    monkeypatch.setattr(knowledge_mod, "_graphql", stub)
    executor = CallableToolExecutor()
    build_tools(
        "https://demo.opencti.io||test-token",
        executor,
        scope_headers={"opencti-draft-id": "draft--abc"},
        direct_mutations=True,
    )
    return executor, stub


async def test_update_field_to_the_stored_value_answers_unchanged(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    from app.tools.base import ToolCall

    executor, stub = _field_executor(monkeypatch, _PATTERN)
    result = await executor.execute_tool(
        ToolCall(
            id="c1",
            name="update_opencti_field",
            arguments={"entity_id": "indicator--1", "key": "pattern", "value": _PATTERN},
        )
    )
    assert result.content.startswith("Unchanged: 'pattern'"), result.content
    assert "draft--abc" in result.content
    assert "Updated" not in result.content
    assert stub.patches == []


async def test_update_field_to_a_new_value_patches_and_names_the_draft(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    from app.tools.base import ToolCall

    executor, stub = _field_executor(monkeypatch, "old pattern")
    result = await executor.execute_tool(
        ToolCall(
            id="c2",
            name="update_opencti_field",
            arguments={"entity_id": "indicator--1", "key": "pattern", "value": _PATTERN},
        )
    )
    assert result.content.startswith("Updated 'pattern' on [Indicator] indicator--1"), result.content
    assert "recorded in draft draft--abc" in result.content
    assert len(stub.patches) == 1


def _loop_with_create_indicator() -> tuple[CompositeToolExecutor, list[dict[str, str]]]:
    executed: list[dict[str, str]] = []
    callable_exec = CallableToolExecutor()

    async def _create(name: str, pattern: str) -> str:
        executed.append({"name": name, "pattern": pattern})
        return "created indicator--1"

    callable_exec.register("create_opencti_indicator", _create)
    composite = CompositeToolExecutor()
    composite.add_tools(
        [
            ToolDefinition(
                name="create_opencti_indicator",
                description="Create an indicator",
                input_schema={
                    "type": "object",
                    "properties": {"name": {"type": "string"}, "pattern": {"type": "string"}},
                    "required": ["name", "pattern"],
                },
                source="integration:opencti",
            )
        ],
        callable_exec,
    )
    return composite, executed


async def _run_loop(monkeypatch, pattern_arg: str):  # type: ignore[no-untyped-def]
    from app.llm.base import BaseLLMProvider, ChatMessage, ChatResponse
    from app.tools import agent_loop as al
    from app.tools.base import ToolCall

    for name in ("_lf_start_llm_gen", "_lf_end_generation", "_lf_end_span", "_lf_end_tool_span"):
        monkeypatch.setattr(al, name, lambda *a, **kw: None)
    monkeypatch.setattr(al, "_lf_start_iter_span", lambda **kw: None)
    monkeypatch.setattr(al, "_lf_start_tool_span", lambda **kw: None)

    class _Scripted(BaseLLMProvider):
        provider_name = "stub"
        context_window = 100_000

        def __init__(self) -> None:
            self._script = [
                ChatResponse(
                    content="",
                    model="stub",
                    stop_reason="tool_calls",
                    tool_calls=[
                        ToolCall(
                            id="c1",
                            name="create_opencti_indicator",
                            arguments={"name": "$collected.rules[0].name", "pattern": pattern_arg},
                        )
                    ],
                ),
                ChatResponse(content="done", model="stub"),
            ]

        async def chat(self, messages, model=None, tools=None, **kwargs):  # type: ignore[no-untyped-def,override]
            return self._script.pop(0)

        async def chat_stream(self, messages, **kwargs):  # type: ignore[no-untyped-def,override]
            yield ""

        async def embed(self, text, model=None):  # type: ignore[no-untyped-def]
            return [0.0]

        async def list_models(self):  # type: ignore[no-untyped-def]
            return []

    composite, executed = _loop_with_create_indicator()
    messages = [ChatMessage(role="user", content="go")]
    await al.run_agent_loop(
        llm=_Scripted(),
        messages=messages,
        tools=composite.all_tools,
        executor=composite,
        max_iterations=5,
        progressive_tools=False,
        session_vars={"$collected": {"rules": {k: dict(v) for k, v in _SESSION["$collected"]["rules"].items()}}},
    )
    results = [m.content for m in messages if m.role == "tool" and m.tool_call_id == "c1"]
    return executed, results


async def test_loop_refuses_a_retyped_pattern_and_does_not_execute(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    executed, results = await _run_loop(monkeypatch, _PATTERN.replace("process_creation", "process_create"))
    assert executed == []
    assert results and "$collected.rules[0].pattern" in results[0]


async def test_loop_passes_the_reference_verbatim(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    executed, results = await _run_loop(monkeypatch, "$collected.rules[0].pattern")
    assert executed == [{"name": "cradle", "pattern": _PATTERN}]
    assert results == ["created indicator--1"]


def test_summary_lists_every_item_by_index() -> None:
    items = {
        f"_item_{i}": {
            "name": f"rule-{i}",
            "pattern_type": "sigma",
            "pattern": "p" * 500,
            "source_indicator_ids": [f"indicator--{i}"],
        }
        for i in range(13)
    }
    out = _collected_summary(items, 2, "rules")
    assert "Items by index:" in out
    for i in range(13):
        assert f"[{i}] name=rule-{i}" in out
        assert f'source_indicator_ids=["indicator--{i}"]' in out
    assert "pattern: 500 chars" in out
    assert "p" * 500 not in out
