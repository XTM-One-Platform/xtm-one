"""``$input`` keeps a large input document out of the prompt and stages it in the sandbox (#12032)."""

import json
from typing import Any

import pytest

from app.llm.base import BaseLLMProvider, ChatMessage, ChatResponse
from app.services.builtin_tools import build_stix_bundle_validator_tool
from app.services.builtin_tools.media import collect_sandbox_inputs
from app.tools import agent_loop as al
from app.tools.base import CallableToolExecutor, CompositeToolExecutor, FileContext, ToolCall
from app.tools.session_input import (
    INPUT_INLINE_MAX_CHARS,
    INPUT_MOUNT_NAME,
    detach_input_document,
    input_document_bytes,
    input_reference_block,
    normalize_input_placeholder,
)

_INSTRUCTIONS = "Add the label phishing to the report.\n--- STIX BUNDLE ---\n"
_TRAILER = "\n--- END ---\nAnswer with the patch only."


def _large_bundle(chars: int) -> dict[str, Any]:
    """A synthetic bundle of about *chars* characters: one report and many notes."""
    filler = "x" * 1000
    count = max(1, chars // 1100)
    notes = [
        {"type": "note", "id": f"note--{i:08d}-0000-4000-8000-000000000000", "content": filler} for i in range(count)
    ]
    return {
        "type": "bundle",
        "id": "bundle--5d0092c5-5f74-4287-9642-33f4c354e56d",
        "objects": [{"type": "report", "id": "report--1", "name": "Original", "labels": []}, *notes],
    }


def _playbook(bundle: dict[str, Any]) -> str:
    return _INSTRUCTIONS + json.dumps(bundle) + _TRAILER


class _RecordingLLM(BaseLLMProvider):
    provider_name = "stub"
    context_window = 1_000_000

    def __init__(self, script: list[ChatResponse]) -> None:
        self._script = script
        self.requests: list[list[Any]] = []

    async def chat(self, messages, model=None, tools=None, **kwargs):  # type: ignore[no-untyped-def,override]
        self.requests.append(list(messages))
        return self._script[len(self.requests) - 1]

    async def chat_stream(self, messages, **kwargs):  # type: ignore[no-untyped-def,override]
        yield ""

    async def embed(self, text, model=None):  # type: ignore[no-untyped-def]
        return [0.0]

    async def list_models(self) -> list[Any]:
        return []


@pytest.fixture
def _quiet_langfuse(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(al, "_lf_start_llm_gen", lambda *a, **kw: None)
    monkeypatch.setattr(al, "_lf_end_generation", lambda *a, **kw: None)
    monkeypatch.setattr(al, "_lf_end_span", lambda *a, **kw: None)
    monkeypatch.setattr(al, "_lf_end_tool_span", lambda *a, **kw: None)
    monkeypatch.setattr(al, "_lf_start_iter_span", lambda **kw: None)
    monkeypatch.setattr(al, "_lf_start_tool_span", lambda **kw: None)


def _request_chars(request: list[Any]) -> int:
    return sum(len(getattr(m, "content", "") or "") for m in request)


# ── detach_input_document ─────────────────────────────────────────────────────


def test_a_large_document_is_replaced_by_a_reference_block() -> None:
    bundle = _large_bundle(300_000)
    original = ChatMessage(role="user", content=_playbook(bundle))
    messages = [original]
    document, detached = detach_input_document(messages)
    assert detached is True
    assert document == bundle
    content = messages[0].content
    assert content.startswith(_INSTRUCTIONS)
    assert content.endswith(_TRAILER)
    assert len(content) < 2_000
    assert "objects: " + str(len(bundle["objects"])) in content
    assert "note " + str(len(bundle["objects"]) - 1) in content
    assert f"attachments/{INPUT_MOUNT_NAME}" in content
    assert "$input" in content
    # The caller's message object is never mutated: a new one stands in its place.
    assert original.content == _playbook(bundle)
    assert messages[0] is not original


def test_a_small_document_stays_inline() -> None:
    bundle = {"type": "bundle", "id": "bundle--1", "objects": [{"type": "report", "name": "r"}]}
    content = _playbook(bundle)
    messages = [ChatMessage(role="user", content=content)]
    document, detached = detach_input_document(messages)
    assert detached is False
    assert document == bundle
    assert messages[0].content == content


def test_no_document_detaches_nothing() -> None:
    messages = [ChatMessage(role="user", content="plain text, no document")]
    assert detach_input_document(messages) == (None, False)
    assert messages[0].content == "plain text, no document"


def test_the_threshold_is_the_document_span() -> None:
    bundle = _large_bundle(INPUT_INLINE_MAX_CHARS)
    size = len(json.dumps(bundle))
    messages = [ChatMessage(role="user", content=_playbook(bundle))]
    _document, detached = detach_input_document(messages, max_inline_chars=size)
    assert detached is False
    _document, detached = detach_input_document(messages, max_inline_chars=size - 1)
    assert detached is True


def test_the_block_of_a_document_that_is_not_a_bundle() -> None:
    block = input_reference_block({"alpha": 1, "beta": [1, 2]}, 123_456)
    assert "123,456 characters" in block
    assert "alpha" in block and "beta" in block
    assert input_reference_block([1, 2, 3], 10).count("3 item(s)") == 1


def test_the_staged_bytes_load_to_the_exact_document() -> None:
    bundle = _large_bundle(50_000)
    assert json.loads(input_document_bytes(bundle).decode("utf-8")) == bundle


@pytest.mark.parametrize("value", ["$input", '"$input"', '\\"$input\\"', " '$input' "])
def test_every_quoted_form_reads_as_the_placeholder(value: str) -> None:
    assert normalize_input_placeholder(value) == "$input"


# ── The sandbox mount ─────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_the_run_input_is_mounted_and_indexed() -> None:
    bundle = _large_bundle(20_000)
    ctx = FileContext()
    ctx.add_sandbox_input(INPUT_MOUNT_NAME, input_document_bytes(bundle))
    inputs, planned, skipped = await collect_sandbox_inputs(ctx)
    files = dict(inputs)
    assert json.loads(files[INPUT_MOUNT_NAME]) == bundle
    index = json.loads(files["index.json"])
    assert [entry["path"] for entry in index] == [f"attachments/{INPUT_MOUNT_NAME}"]
    assert INPUT_MOUNT_NAME in planned and skipped == []
    # Never a conversation file.
    assert ctx.list_files() == [] and ctx.is_empty


# ── The loop ──────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
@pytest.mark.parametrize("placeholder", ["$input", '"$input"', '\\"$input\\"'])
async def test_a_3m_bundle_stays_out_of_the_first_request(_quiet_langfuse: None, placeholder: str) -> None:
    bundle = _large_bundle(3_100_000)
    content = _playbook(bundle)
    assert len(content) > 3_000_000
    tools, _ = build_stix_bundle_validator_tool()
    calls: list[dict[str, Any]] = []

    async def _spy(**kwargs: Any) -> str:
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
    llm = _RecordingLLM([call, ChatResponse(content="done", model="stub")])
    ctx = FileContext()
    await al.run_agent_loop(
        llm=llm,
        messages=[ChatMessage(role="user", content=content)],
        tools=composite.all_tools,
        executor=composite,
        max_iterations=4,
        progressive_tools=False,
        input_reference=True,
        file_context=ctx,
    )
    assert _request_chars(llm.requests[0]) < 100_000
    assert len(calls) == 1
    assert json.loads(calls[0]["input_bundle"]) == bundle
    assert calls[0]["patch"] == patch
    assert json.loads(ctx.sandbox_inputs[INPUT_MOUNT_NAME]) == bundle


@pytest.mark.asyncio
async def test_without_input_reference_the_prompt_is_unchanged(_quiet_langfuse: None) -> None:
    bundle = _large_bundle(300_000)
    content = _playbook(bundle)
    llm = _RecordingLLM([ChatResponse(content="done", model="stub")])
    ctx = FileContext()
    await al.run_agent_loop(
        llm=llm,
        messages=[ChatMessage(role="user", content=content)],
        tools=[],
        executor=CompositeToolExecutor(),
        max_iterations=2,
        progressive_tools=False,
        file_context=ctx,
    )
    assert any(getattr(m, "content", None) == content for m in llm.requests[0])
    assert ctx.sandbox_inputs == {}
