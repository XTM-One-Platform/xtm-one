"""Transformer agents keep their input: patch output mode, ``$input`` and changed-value warnings (#11825)."""

import json
from types import SimpleNamespace

import pytest

from app.agents.builtin_agents import BUILTIN_AGENTS
from app.llm.base import BaseLLMProvider, ChatMessage, ChatResponse
from app.services.builtin_tools import build_stix_bundle_validator_tool
from app.services.builtin_tools.stix import stix_validator_strict_refs
from app.tools import agent_loop as al
from app.tools.base import CallableToolExecutor, CompositeToolExecutor, ToolCall, ToolDefinition
from app.tools.json_patch import JsonPatchError, apply_json_patch
from app.tools.session_input import (
    carries_stix_bundle,
    input_document_from_messages,
    input_reference_enabled,
    input_reference_for_run,
    normalize_input_placeholder,
    parse_input_document,
    unexposed_input_reference_error,
)

_REPORT_ID = "report--8e2e2d2b-17d4-4cbf-938f-98ee46b3cd3f"
_IDENTITY_ID = "identity--f431f809-377b-45e0-aa1c-6a4751cae5ff"
_DESCRIPTION = "<html><body>" + "<p>Paragraph of the original report.</p>" * 2700 + "</body></html>"


def _bundle() -> dict:
    return {
        "type": "bundle",
        "id": "bundle--5d0092c5-5f74-4287-9642-33f4c354e56d",
        "objects": [
            {
                "type": "report",
                "spec_version": "2.1",
                "id": _REPORT_ID,
                "created": "2026-01-01T00:00:00.000Z",
                "modified": "2026-01-01T00:00:00.000Z",
                "name": "Original",
                "description": _DESCRIPTION,
                "published": "2026-01-01T00:00:00.000Z",
                "report_types": ["threat-report"],
                "labels": ["apt"],
                "object_refs": [_IDENTITY_ID],
            },
            {
                "type": "identity",
                "spec_version": "2.1",
                "id": _IDENTITY_ID,
                "created": "2026-01-01T00:00:00.000Z",
                "modified": "2026-01-01T00:00:00.000Z",
                "name": "Example sector",
                "identity_class": "class",
            },
        ],
    }


async def _validate(executor, **arguments) -> dict:
    """The validator's whole result (``structured``: the bundle included, whatever its size)."""
    result = await executor.execute_tool(ToolCall(id="v", name="stix_bundle_validator", arguments=arguments))
    return result.structured if isinstance(result.structured, dict) else json.loads(result.content)


# ── JSON Patch ────────────────────────────────────────────────────────────────


def test_json_patch_applies_rfc6902_operations_on_a_copy() -> None:
    doc = {"a": {"b": [1, 2]}, "c": "x"}
    patched = apply_json_patch(
        doc,
        [
            {"op": "add", "path": "/a/b/-", "value": 3},
            {"op": "replace", "path": "/c", "value": "y"},
            {"op": "copy", "from": "/c", "path": "/d"},
            {"op": "move", "from": "/d", "path": "/e"},
            {"op": "remove", "path": "/a/b/0"},
            {"op": "test", "path": "/e", "value": "y"},
            {"op": "add", "path": "/k~1v", "value": True},
        ],
    )
    assert patched == {"a": {"b": [2, 3]}, "c": "y", "e": "y", "k/v": True}
    assert doc == {"a": {"b": [1, 2]}, "c": "x"}


@pytest.mark.parametrize(
    "patch",
    [
        [{"op": "replace", "path": "/missing", "value": 1}],
        [{"op": "remove", "path": "/a/b/9"}],
        [{"op": "test", "path": "/c", "value": "other"}],
        [{"op": "add", "path": "no-slash", "value": 1}],
        [{"op": "nope", "path": "/c"}],
        [{"op": "add", "path": "/c"}],
        {"op": "add"},
        [{"op": "add", "path": "/c", "value": 1, "_skip": True}],
    ],
)
def test_json_patch_refuses_an_operation_that_cannot_apply(patch) -> None:
    with pytest.raises(JsonPatchError):
        apply_json_patch({"a": {"b": [1, 2]}, "c": "x"}, patch)


def test_json_patch_names_the_unknown_key_of_an_operation() -> None:
    with pytest.raises(JsonPatchError, match=r"^op 1: unknown key '_skip'"):
        apply_json_patch(
            {"c": "x"},
            [{"op": "add", "path": "/d", "value": 1}, {"op": "remove", "path": "/c", "_skip": True}],
        )


# ── $input ────────────────────────────────────────────────────────────────────


def test_input_document_is_read_after_the_delimiter_line() -> None:
    text = "Add the label phishing.\n--- STIX BUNDLE ---\n" + json.dumps(_bundle())
    assert parse_input_document(text) == _bundle()
    assert parse_input_document(json.dumps(_bundle())) == _bundle()
    assert parse_input_document("no document here") is None


def test_input_reference_is_opt_in() -> None:
    assert input_reference_enabled({"output_mode": "patch"})
    assert input_reference_enabled({"input_reference": True})
    assert not input_reference_enabled({})
    assert not input_reference_enabled(None)


def test_finalising_on_the_stix_validator_exposes_input_unless_opted_out() -> None:
    finalize = {"finalize_on_builtin_tool": {"tool_name": "stix_bundle_validator"}}
    assert input_reference_enabled(finalize)
    assert not input_reference_enabled({**finalize, "input_reference": False})
    assert not input_reference_enabled({"input_reference": False, "output_mode": "patch"})
    assert not input_reference_enabled({"finalize_on_builtin_tool": {"tool_name": "other_tool"}})


@pytest.mark.parametrize(
    ("value", "schema", "refused"),
    [
        ("$input", {"type": "object"}, True),
        ("$input", {"type": "string"}, False),
        ('"$input"', {"type": "object"}, True),
        ("$input.objects[0]", {"type": "object"}, True),
        ("$input[0]", None, True),
        ("$input.objects[0].name", {"type": "string"}, False),
        ("$inputs are fine", {"type": "object"}, False),
        ("plain", {"type": "object"}, False),
    ],
)
def test_unexposed_input_reference_error_refuses_only_placeholders(value, schema, refused) -> None:
    message = unexposed_input_reference_error("input_bundle", value, schema)
    assert (message is not None) is refused
    if refused:
        assert "$input is not exposed to this agent" in message
        assert 'output_mode: "patch"' in message and "input_reference: true" in message


# ── The OpenCTI playbook path ─────────────────────────────────────────────────


def _playbook_content(instruction: str = "Add the label phishing to the report.") -> str:
    """The ``content`` OpenCTI's ``buildAgentMessageContent`` sends: instructions, delimiter, pretty JSON."""
    return f"{instruction}\n\n--- STIX BUNDLE ---\n{json.dumps(_bundle(), indent=2)}"


def test_a_playbook_message_carries_its_bundle_even_with_a_brief_after_it() -> None:
    content = _playbook_content() + "\n\n[Agents answering after you]\n--- lineup ---\n- Beta"
    assert parse_input_document(content) == _bundle()
    fenced = "Do it.\n```json\n" + json.dumps(_bundle()) + "\n```\nThanks."
    assert parse_input_document(fenced) == _bundle()
    inline = "Bundle: " + json.dumps(_bundle()) + " (end)"
    assert parse_input_document(inline) == _bundle()
    assert carries_stix_bundle(_playbook_content())
    assert not carries_stix_bundle('Count these: {"a": 1}')


def test_a_message_carrying_a_stix_bundle_exposes_input_unless_opted_out() -> None:
    assert input_reference_for_run({}, _playbook_content())
    assert input_reference_for_run(None, _playbook_content())
    assert not input_reference_for_run({"input_reference": False}, _playbook_content())
    assert not input_reference_for_run({}, "no bundle in this message")
    assert input_reference_for_run({"input_reference": True}, "no bundle in this message")


def test_the_newest_user_message_is_the_input() -> None:
    older = {"type": "bundle", "objects": [{"type": "identity", "name": "older"}]}
    messages = [
        ChatMessage(role="user", content=json.dumps(older)),
        ChatMessage(role="assistant", content="ok"),
        ChatMessage(role="user", content=_playbook_content()),
    ]
    assert input_document_from_messages(messages) == _bundle()


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ('"$input"', "$input"),
        ("'$input.objects[0]'", "$input.objects[0]"),
        (" $input ", "$input"),
        ('"$inputs"', '"$inputs"'),
        ("$input mentions in a search", "$input mentions in a search"),
    ],
)
def test_a_quoted_placeholder_reads_as_the_placeholder(value, expected) -> None:
    assert normalize_input_placeholder(value) == expected


@pytest.mark.parametrize("span_match", ["$input", "$input.objects[0]", "$input is not exposed"])
def test_a_search_text_reading_input_is_never_refused(span_match) -> None:
    assert unexposed_input_reference_error("span_match", span_match, {"type": "string"}) is None


class _ScriptedLLM(BaseLLMProvider):
    provider_name = "stub"
    context_window = 400_000

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


def _echo_executor(seen: list) -> CompositeToolExecutor:
    callable_exec = CallableToolExecutor()

    async def _echo(name: str = "") -> str:
        seen.append(name)
        return json.dumps({"ok": True})

    callable_exec.register("echo", _echo)
    composite = CompositeToolExecutor()
    composite.add_tools(
        [
            ToolDefinition(
                name="echo",
                description="Echo a name",
                input_schema={"type": "object", "properties": {"name": {"type": "string"}}},
                source="builtin:test",
            )
        ],
        callable_exec,
    )
    return composite


def _call(name_arg: str) -> ChatResponse:
    return ChatResponse(
        content="",
        model="stub",
        stop_reason="tool_calls",
        tool_calls=[ToolCall(id="c1", name="echo", arguments={"name": name_arg})],
    )


@pytest.mark.asyncio
async def test_input_reference_resolves_in_a_tool_argument(_quiet_langfuse) -> None:
    seen: list = []
    composite = _echo_executor(seen)
    llm = _ScriptedLLM([_call("$input.objects[0].name"), ChatResponse(content="done", model="stub")])
    text = "Rename nothing.\n--- STIX BUNDLE ---\n" + json.dumps(_bundle())
    await al.run_agent_loop(
        llm=llm,
        messages=[ChatMessage(role="user", content=text)],
        tools=composite.all_tools,
        executor=composite,
        max_iterations=4,
        progressive_tools=False,
        input_reference=True,
    )
    assert seen == ["Original"]


@pytest.mark.asyncio
async def test_input_reference_resolves_a_quoted_bracket_key(_quiet_langfuse) -> None:
    seen: list = []
    composite = _echo_executor(seen)
    ext = "extension-definition--ea279b3e-5c71-4632-ac08-831c66a786ba"
    bundle = {"type": "bundle", "objects": [{"type": "report", "extensions": {ext: {"content": "<p>x</p>"}}}]}
    reference = f'$input.objects[0].extensions["{ext}"].content'
    llm = _ScriptedLLM([_call(reference), ChatResponse(content="done", model="stub")])
    await al.run_agent_loop(
        llm=llm,
        messages=[ChatMessage(role="user", content=json.dumps(bundle))],
        tools=composite.all_tools,
        executor=composite,
        max_iterations=4,
        progressive_tools=False,
        input_reference=True,
    )
    assert seen == ["<p>x</p>"]


@pytest.mark.asyncio
@pytest.mark.parametrize("reference", ["$input.objects[99].name", "$input.objects[0].name"])
async def test_unresolvable_input_reference_errors_the_call(_quiet_langfuse, reference) -> None:
    seen: list = []
    composite = _echo_executor(seen)
    llm = _ScriptedLLM([_call(reference), ChatResponse(content="done", model="stub")])
    one_object = {"type": "bundle", "objects": [{"type": "identity", "name": "only"}]}
    # [99] is out of range on a one-object bundle; [0] on an input with no document at all.
    text = json.dumps(one_object) if "99" in reference else "plain text, no document"
    messages = [ChatMessage(role="user", content=text)]
    await al.run_agent_loop(
        llm=llm,
        messages=messages,
        tools=composite.all_tools,
        executor=composite,
        max_iterations=4,
        progressive_tools=False,
        input_reference=True,
    )
    assert seen == []
    tool_messages = [m for m in messages if m.role == "tool"]
    assert tool_messages and "Could not resolve session reference" in tool_messages[0].content
    assert "$collected" not in tool_messages[0].content


@pytest.mark.asyncio
@pytest.mark.parametrize("reference", ["$input.objects[0].name", '"$input.objects[0].name"'])
async def test_a_playbook_message_resolves_input_in_a_custom_tool(_quiet_langfuse, reference) -> None:
    """(b) of the playbook path: a string argument reading $input resolves on a playbook-shaped message."""
    seen: list = []
    composite = _echo_executor(seen)
    llm = _ScriptedLLM([_call(reference), ChatResponse(content="done", model="stub")])
    content = _playbook_content()
    await al.run_agent_loop(
        llm=llm,
        messages=[ChatMessage(role="user", content=content)],
        tools=composite.all_tools,
        executor=composite,
        max_iterations=4,
        progressive_tools=False,
        input_reference=input_reference_for_run({}, content),
    )
    assert seen == ["Original"]


@pytest.mark.asyncio
@pytest.mark.parametrize("placeholder", ["$input", '"$input"'])
async def test_a_playbook_message_resolves_input_in_the_validator(_quiet_langfuse, placeholder) -> None:
    """(a) of the playbook path: input_bundle=\"$input\" plus a patch reaches the validator as the bundle."""
    tools, _ = build_stix_bundle_validator_tool()
    calls: list = []

    async def _spy(**kwargs) -> str:
        calls.append(kwargs)
        return json.dumps({"valid": True})

    callable_exec = CallableToolExecutor()
    callable_exec.register("stix_bundle_validator", _spy)
    composite = CompositeToolExecutor()
    composite.add_tools(tools, callable_exec)
    patch = [{"op": "add", "path": "/objects/0/labels/-", "value": "phishing"}]
    call = ChatResponse(
        content="",
        model="stub",
        stop_reason="tool_calls",
        tool_calls=[
            ToolCall(id="c1", name="stix_bundle_validator", arguments={"input_bundle": placeholder, "patch": patch})
        ],
    )
    llm = _ScriptedLLM([call, ChatResponse(content="done", model="stub")])
    content = _playbook_content()
    config = {"finalize_on_builtin_tool": {"tool_name": "stix_bundle_validator"}}
    await al.run_agent_loop(
        llm=llm,
        messages=[ChatMessage(role="user", content=content)],
        tools=composite.all_tools,
        executor=composite,
        max_iterations=4,
        progressive_tools=False,
        input_reference=input_reference_for_run(config, content),
    )
    assert len(calls) == 1
    assert json.loads(calls[0]["input_bundle"]) == _bundle()


@pytest.mark.asyncio
async def test_input_reference_is_left_literal_without_the_opt_in(_quiet_langfuse) -> None:
    seen: list = []
    composite = _echo_executor(seen)
    llm = _ScriptedLLM([_call("$input.objects[0].name"), ChatResponse(content="done", model="stub")])
    await al.run_agent_loop(
        llm=llm,
        messages=[ChatMessage(role="user", content=json.dumps(_bundle()))],
        tools=composite.all_tools,
        executor=composite,
        max_iterations=4,
        progressive_tools=False,
    )
    assert seen == ["$input.objects[0].name"]


@pytest.mark.asyncio
async def test_input_placeholder_without_the_opt_in_is_refused_with_the_opt_in_named(_quiet_langfuse) -> None:
    tools, _ = build_stix_bundle_validator_tool()
    calls: list = []

    async def _spy(**kwargs) -> str:
        calls.append(kwargs)
        return json.dumps({"valid": True})

    callable_exec = CallableToolExecutor()
    callable_exec.register("stix_bundle_validator", _spy)
    composite = CompositeToolExecutor()
    composite.add_tools(tools, callable_exec)
    patch = [{"op": "add", "path": "/objects/-", "value": {"type": "identity", "name": "new"}}]
    call = ChatResponse(
        content="",
        model="stub",
        stop_reason="tool_calls",
        tool_calls=[
            ToolCall(id="c1", name="stix_bundle_validator", arguments={"input_bundle": "$input", "patch": patch})
        ],
    )
    llm = _ScriptedLLM([call, ChatResponse(content="done", model="stub")])
    messages = [ChatMessage(role="user", content=json.dumps(_bundle()))]
    await al.run_agent_loop(
        llm=llm,
        messages=messages,
        tools=composite.all_tools,
        executor=composite,
        max_iterations=4,
        progressive_tools=False,
    )
    assert calls == []
    tool_messages = [m for m in messages if m.role == "tool"]
    assert tool_messages
    content = tool_messages[0].content
    assert "$input is not exposed to this agent" in content
    assert 'output_mode: "patch"' in content and "input_reference: true" in content
    assert "is not of type" not in content


# ── Validator: patch mode and changed values ──────────────────────────────────


@pytest.mark.asyncio
async def test_patch_mode_keeps_every_untouched_value_byte_identical() -> None:
    _, executor = build_stix_bundle_validator_tool()
    patch = [{"op": "add", "path": "/objects/0/labels/-", "value": "phishing"}]
    # The call carries the input as the JSON string ``$input`` injection makes of it.
    result = await _validate(executor, input_bundle=json.dumps(_bundle()), patch=patch)
    assert result["valid"] is True, result
    report = result["bundle"]["objects"][0]
    assert report["name"] == "Original"
    assert report["description"] == _DESCRIPTION
    assert report["labels"] == ["apt", "phishing"]
    assert "warnings" not in result or not any("changed" in w and "'name'" in w for w in result["warnings"])
    # What the model writes is the patch: its size does not follow the description's.
    assert len(json.dumps(patch)) < 200 < len(_DESCRIPTION)


@pytest.mark.asyncio
async def test_patch_needs_the_input_bundle_and_reports_an_inapplicable_op() -> None:
    _, executor = build_stix_bundle_validator_tool()
    missing = await _validate(executor, patch=[{"op": "remove", "path": "/objects/0"}])
    assert missing["valid"] is False and "input_bundle" in missing["errors"][0]
    bad = await _validate(executor, input_bundle=_bundle(), patch=[{"op": "remove", "path": "/objects/7"}])
    assert bad["valid"] is False and bad["errors"][0].startswith("Patch not applied: op 0")


@pytest.mark.asyncio
@pytest.mark.parametrize("value", [None, "objects", {"a": 1}])
async def test_patch_setting_objects_to_a_non_array_is_an_error_naming_every_lost_object(value) -> None:
    _, executor = build_stix_bundle_validator_tool()
    result = await _validate(
        executor, input_bundle=json.dumps(_bundle()), patch=[{"op": "add", "path": "/objects", "value": value}]
    )
    assert result["valid"] is False, result
    assert any("'objects' is" in e and "the input had 2 objects" in e for e in result["errors"])
    assert any("never /objects itself" in e for e in result["errors"])
    lost = [w for w in result.get("warnings", []) if "is not in the bundle" in w]
    assert len(lost) == 2


@pytest.mark.asyncio
async def test_patch_removing_objects_is_an_error() -> None:
    _, executor = build_stix_bundle_validator_tool()
    result = await _validate(executor, input_bundle=_bundle(), patch=[{"op": "remove", "path": "/objects"}])
    assert result["valid"] is False
    assert any(e.startswith("'objects' is missing") for e in result["errors"])


@pytest.mark.asyncio
async def test_patch_emptying_objects_reports_the_count_lost() -> None:
    three = _bundle()
    three["objects"].append(
        {
            "type": "identity",
            "spec_version": "2.1",
            "id": "identity--0f2a6d0c-3c0b-4a2e-9b1a-1d5f2b7c9e10",
            "created": "2026-01-01T00:00:00.000Z",
            "modified": "2026-01-01T00:00:00.000Z",
            "name": "Other",
            "identity_class": "class",
        }
    )
    _, executor = build_stix_bundle_validator_tool()
    result = await _validate(executor, input_bundle=three, patch=[{"op": "replace", "path": "/objects", "value": []}])
    assert result["valid"] is False
    assert any(e.startswith("objects lost: the input had 3 objects, the result has 0") for e in result["errors"])


@pytest.mark.asyncio
async def test_patch_with_an_unknown_operation_key_is_not_applied() -> None:
    _, executor = build_stix_bundle_validator_tool()
    patch = [{"op": "add", "path": "/objects/0/labels/-", "value": "x", "_skip": True}]
    result = await _validate(executor, input_bundle=_bundle(), patch=patch)
    assert result["valid"] is False
    assert result["errors"][0].startswith("Patch not applied: op 0: unknown key '_skip'")


@pytest.mark.asyncio
@pytest.mark.parametrize("objects", [None, []])
async def test_an_input_already_without_objects_and_a_no_op_patch_is_not_an_agent_error(objects) -> None:
    empty = {"type": "bundle", "id": "bundle--5d0092c5-5f74-4287-9642-33f4c354e56d", "objects": objects}
    _, executor = build_stix_bundle_validator_tool()
    result = await _validate(executor, input_bundle=empty, patch=[])
    assert result["valid"] is True, result


@pytest.mark.asyncio
@pytest.mark.parametrize("field", ["bundle", "bundle_json"])
async def test_a_bundle_whose_objects_is_null_is_invalid_in_every_mode(field) -> None:
    nulled = {"type": "bundle", "id": "bundle--5d0092c5-5f74-4287-9642-33f4c354e56d", "objects": None}
    _, executor = build_stix_bundle_validator_tool()
    value = nulled if field == "bundle" else json.dumps(nulled)
    result = await _validate(executor, **{field: value})
    assert result["valid"] is False
    assert result["errors"][0].startswith("'objects' is null, not an array")
    with_input = await _validate(executor, **{field: value}, input_bundle=_bundle())
    assert with_input["valid"] is False
    assert any("the input had 2 objects" in e for e in with_input["errors"])


@pytest.mark.asyncio
async def test_patch_candidate_emptying_objects_is_an_error() -> None:
    _, executor = build_stix_bundle_validator_tool()
    first = await _validate(executor, input_bundle=_bundle(), patch=[])
    assert first["valid"] is True
    result = await _validate(
        executor,
        input_bundle=_bundle(),
        patch=[{"op": "replace", "path": "/objects", "value": []}],
        patch_candidate=True,
    )
    assert result["valid"] is False
    assert any(e.startswith("objects lost: the input had 2 objects") for e in result["errors"])


@pytest.mark.asyncio
async def test_changed_value_against_the_input_is_a_warning_then_an_error_when_strict() -> None:
    renamed = _bundle()
    renamed["objects"][0]["name"] = "Renamed"
    _, executor = build_stix_bundle_validator_tool()
    result = await _validate(executor, bundle=renamed, input_bundle=_bundle())
    assert result["valid"] is True
    assert any(
        w.startswith("objects[0] (report): property 'name'") and '"Original" -> "Renamed"' in w
        for w in result["warnings"]
    )
    strict = await _validate(executor, bundle=renamed, input_bundle=_bundle(), strict_input=True)
    assert strict["valid"] is False
    assert any("property 'name'" in e for e in strict["errors"])


@pytest.mark.asyncio
async def test_a_long_changed_value_is_named_by_length_and_hash() -> None:
    changed = _bundle()
    changed["objects"][0]["description"] = _DESCRIPTION + "x"
    _, executor = build_stix_bundle_validator_tool()
    result = await _validate(executor, bundle=changed, input_bundle=_bundle())
    warning = next(w for w in result["warnings"] if "'description'" in w)
    assert "chars, sha256" in warning and len(warning) < 400


# ── The built-in transformer opts in ──────────────────────────────────────────


def test_cti_stix_transformer_runs_in_patch_mode() -> None:
    transformer = next(a for a in BUILTIN_AGENTS if a["slug"] == "cti-stix-transformer")
    config = transformer["config"]
    assert config["output_mode"] == "patch"
    assert config["finalize_on_builtin_tool"]["tool_name"] == "stix_bundle_validator"

    agent = SimpleNamespace(config=config, finalize_tool_ref=None)
    # Refs to objects OpenCTI already holds stay warnings for the transformer.
    assert stix_validator_strict_refs(agent) is False
    agent.config = {"finalize_on_builtin_tool": {"tool_name": "stix_bundle_validator"}}
    assert stix_validator_strict_refs(agent) is True
