"""``$workspace:PATH`` tool arguments: a large argument read from the run's sandbox workspace (#11981)."""

import json

import pytest

from app.llm.base import BaseLLMProvider, ChatMessage, ChatResponse
from app.services.builtin_tools import build_stix_bundle_validator_tool
from app.services.sandbox_workspace.paths import WorkspacePathError, normalize
from app.tools import agent_loop as al
from app.tools.base import (
    WORKSPACE_REFERENCE_MAX_BYTES,
    WORKSPACE_REFERENCE_MAX_PATH_CHARS,
    CompositeToolExecutor,
    ToolCall,
    ToolDefinition,
    resolve_workspace_references,
    workspace_reference_keys,
)

_REPORT_ID = "report--8e2e2d2b-17d4-4cbf-938f-98ee46b3cd3f"
_IDENTITY_ID = "identity--f431f809-377b-45e0-aa1c-6a4751cae5ff"


def _identity(index: int) -> dict:
    return {
        "type": "identity",
        "spec_version": "2.1",
        "id": f"identity--{index:08d}-0000-4000-8000-000000000000",
        "created": "2026-01-01T00:00:00.000Z",
        "modified": "2026-01-01T00:00:00.000Z",
        "name": f"Added sector {index} " + "x" * 400,
        "identity_class": "class",
    }


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
                "description": "Original description.",
                "published": "2026-01-01T00:00:00.000Z",
                "report_types": ["threat-report"],
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


def _large_patch() -> list:
    """An RFC 6902 patch over 120,000 characters: 250 objects added, each referenced by the report."""
    patch: list = []
    for index in range(250):
        identity = _identity(index)
        patch.append({"op": "add", "path": "/objects/-", "value": identity})
        patch.append({"op": "add", "path": "/objects/0/object_refs/-", "value": identity["id"]})
    assert len(json.dumps(patch)) > 120_000
    return patch


class _Workspace:
    """A workspace sink double: the stored files, read under the workspace's own path rules."""

    def __init__(self, files: dict[str, bytes]) -> None:
        self.files = files
        self.reads: list[str] = []

    async def read_file(self, path: object, *, tool: str) -> tuple[str, str, bytes]:
        try:
            rel = normalize(path)
        except WorkspacePathError as exc:
            raise ValueError(f"{exc}.") from None
        if rel not in self.files:
            raise ValueError(f"workspace/{rel} does not exist.")
        self.reads.append(rel)
        return rel, rel.rsplit("/", 1)[-1], self.files[rel]


def _validator(workspace: _Workspace | None) -> CompositeToolExecutor:
    tools, executor = build_stix_bundle_validator_tool()
    composite = CompositeToolExecutor()
    composite.add_tools(tools, executor)
    composite.workspace_sink = workspace
    return composite


class _ScriptedLLM(BaseLLMProvider):
    provider_name = "stub"
    context_window = 400_000

    def __init__(self, script):
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


def _validator_call(**arguments) -> ChatResponse:
    return ChatResponse(
        content="",
        model="stub",
        stop_reason="tool_calls",
        tool_calls=[ToolCall(id="c1", name="stix_bundle_validator", arguments=arguments)],
    )


_FINALIZE = {"tool_name": "stix_bundle_validator", "valid_key": "valid", "output_key": "bundle"}


# ── The agent loop: $input plus a patch read from the workspace, finalised ────


@pytest.mark.asyncio
async def test_a_large_patch_from_the_workspace_is_applied_and_finalises_the_run(_quiet_langfuse) -> None:
    workspace = _Workspace({"patch.json": json.dumps(_large_patch()).encode()})
    composite = _validator(workspace)
    arguments = {"input_bundle": "$input", "patch": "$workspace:patch.json"}
    assert len(json.dumps(arguments)) < 200
    llm = _ScriptedLLM([_validator_call(**arguments), ChatResponse(content="unused", model="stub")])
    response = await al.run_agent_loop(
        llm=llm,
        messages=[ChatMessage(role="user", content="Add the sectors.\n--- STIX BUNDLE ---\n" + json.dumps(_bundle()))],
        tools=composite.all_tools,
        executor=composite,
        max_iterations=4,
        progressive_tools=False,
        input_reference=True,
        finalize_on_tool=_FINALIZE,
    )
    assert workspace.reads == ["patch.json"]
    assert llm.call_count == 1, "the validated patch finalises the run without another model turn"
    bundle = json.loads(response.content)
    assert len(bundle["objects"]) == 252
    assert bundle["objects"][0]["name"] == "Original"
    assert len(bundle["objects"][0]["object_refs"]) == 251


@pytest.mark.asyncio
async def test_a_bundle_from_the_workspace_is_validated(_quiet_langfuse) -> None:
    workspace = _Workspace({"work/bundle.json": json.dumps(_bundle()).encode()})
    composite = _validator(workspace)
    llm = _ScriptedLLM(
        [_validator_call(bundle="$workspace:workspace/work/bundle.json"), ChatResponse(content="x", model="stub")]
    )
    response = await al.run_agent_loop(
        llm=llm,
        messages=[ChatMessage(role="user", content="Validate.")],
        tools=composite.all_tools,
        executor=composite,
        max_iterations=4,
        progressive_tools=False,
        finalize_on_tool=_FINALIZE,
    )
    assert workspace.reads == ["work/bundle.json"]
    assert json.loads(response.content)["objects"][0]["id"] == _REPORT_ID


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("files", "reference", "expected"),
    [
        ({}, "$workspace:../etc/passwd", "'..'"),
        ({}, "$workspace:/etc/passwd", "absolute"),
        ({}, "$workspace:patch.json", "does not exist"),
        ({"patch.json": b"not json at all"}, "$workspace:patch.json", "not valid JSON"),
        ({"patch.json": b'{"op": "add"}'}, "$workspace:patch.json", "takes a JSON array"),
        ({"patch.json": b"\xff\xfe"}, "$workspace:patch.json", "not UTF-8"),
        ({}, "$workspace:", "names no file"),
    ],
)
async def test_a_reference_that_cannot_be_read_refuses_the_call_and_runs_nothing(
    _quiet_langfuse, files, reference, expected
) -> None:
    tools, _ = build_stix_bundle_validator_tool()
    calls: list = []

    async def _spy(**kwargs) -> str:
        calls.append(kwargs)
        return json.dumps({"valid": True, "bundle": _bundle()})

    from app.tools.base import CallableToolExecutor

    callable_exec = CallableToolExecutor()
    callable_exec.register("stix_bundle_validator", _spy)
    composite = CompositeToolExecutor()
    composite.add_tools(tools, callable_exec)
    composite.workspace_sink = _Workspace(files)
    llm = _ScriptedLLM(
        [_validator_call(input_bundle="$input", patch=reference), ChatResponse(content="done", model="stub")]
    )
    messages = [ChatMessage(role="user", content=json.dumps(_bundle()))]
    await al.run_agent_loop(
        llm=llm,
        messages=messages,
        tools=composite.all_tools,
        executor=composite,
        max_iterations=4,
        progressive_tools=False,
        input_reference=True,
    )
    assert calls == []
    tool_messages = [m for m in messages if m.role == "tool"]
    assert tool_messages
    content = tool_messages[0].content
    assert expected in content
    assert "NOT executed" in content and "$workspace:PATH" in content
    assert "is not of type" not in content


# ── The resolver ──────────────────────────────────────────────────────────────


def _definition(properties: dict) -> ToolDefinition:
    return ToolDefinition(
        name="t", description="", input_schema={"type": "object", "properties": properties}, source="builtin:test"
    )


@pytest.mark.asyncio
async def test_a_string_parameter_receives_the_file_as_text_and_the_written_call_is_kept() -> None:
    call = ToolCall(id="c", name="t", arguments={"text": "$workspace:notes.md", "other": 3})
    resolved, refusal = await resolve_workspace_references(
        call, _definition({"text": {"type": "string"}}), _Workspace({"notes.md": b'{"a": 1}'})
    )
    assert refusal is None
    assert resolved.arguments == {"text": '{"a": 1}', "other": 3}
    assert call.arguments["text"] == "$workspace:notes.md", "the transcript keeps the short reference"


@pytest.mark.asyncio
async def test_an_object_parameter_receives_parsed_json() -> None:
    call = ToolCall(id="c", name="t", arguments={"doc": "$workspace:d.json"})
    resolved, refusal = await resolve_workspace_references(
        call, _definition({"doc": {"type": ["object", "null"]}}), _Workspace({"d.json": b'{"a": [1, 2]}'})
    )
    assert refusal is None and resolved.arguments == {"doc": {"a": [1, 2]}}


@pytest.mark.asyncio
async def test_no_workspace_and_an_oversized_file_are_refused() -> None:
    call = ToolCall(id="c", name="t", arguments={"doc": "$workspace:d.json"})
    _, refusal = await resolve_workspace_references(call, _definition({"doc": {"type": "object"}}), None)
    assert refusal and "no sandbox workspace" in refusal
    big = _Workspace({"d.json": b" " * (WORKSPACE_REFERENCE_MAX_BYTES + 1)})
    _, refusal = await resolve_workspace_references(call, _definition({"doc": {"type": "object"}}), big)
    assert refusal and "ceiling" in refusal


def test_only_top_level_strings_with_the_prefix_are_references() -> None:
    arguments = {"a": "$workspace:x", "b": "$input", "c": ["$workspace:y"], "d": "workspace:z"}
    assert workspace_reference_keys(arguments) == ["a"]
    assert workspace_reference_keys(None) == []


# ── Prose that starts with the prefix is text, not a reference (#12034) ────────


@pytest.mark.parametrize(
    ("tool", "key", "prose"),
    [
        ("store_memory", "content", "$workspace:PATH arguments are read on the server; prose stays prose."),
        ("update_work_item", "note", "$workspace:work/patch.json written,\nnext step: the PR."),
        ("send_message", "body", "$workspace:\tthe file is in the sandbox"),
        ("store_memory", "content", "$workspace:" + "a" * (WORKSPACE_REFERENCE_MAX_PATH_CHARS + 1)),
    ],
)
@pytest.mark.asyncio
async def test_prose_starting_with_the_prefix_reaches_the_tool_unchanged(tool, key, prose) -> None:
    call = ToolCall(id="c", name=tool, arguments={key: prose})
    assert workspace_reference_keys(call.arguments) == []
    workspace = _Workspace({})
    definition = ToolDefinition(
        name=tool,
        description="",
        input_schema={"type": "object", "properties": {key: {"type": "string"}}},
        source="builtin:test",
    )
    resolved, refusal = await resolve_workspace_references(call, definition, workspace)
    assert refusal is None
    assert resolved.arguments == {key: prose}
    assert workspace.reads == []


@pytest.mark.asyncio
async def test_a_single_token_reference_still_resolves_and_a_missing_one_still_errors() -> None:
    definition = _definition({"text": {"type": "string"}})
    call = ToolCall(id="c", name="t", arguments={"text": "  $workspace:work/notes.md\n"})
    assert workspace_reference_keys(call.arguments) == ["text"]
    resolved, refusal = await resolve_workspace_references(call, definition, _Workspace({"work/notes.md": b"hello"}))
    assert refusal is None and resolved.arguments == {"text": "hello"}
    missing = ToolCall(id="c", name="t", arguments={"text": "$workspace:work/absent.md"})
    _, refusal = await resolve_workspace_references(missing, definition, _Workspace({}))
    assert refusal and "does not exist" in refusal and "NOT executed" in refusal
