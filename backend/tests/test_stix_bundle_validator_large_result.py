"""The STIX validator never echoes a large bundle back into the model's context.

A transformer working on a large input validated a patch, failed, and got the
whole bundle back in the tool result; each failed call grew the next model
request by the full bundle until it overflowed the model window. A bundle over
the inline threshold is now answered by reference (``$candidate``), the whole
result riding on ``ToolResult.structured``, from which the finalize fast path
takes the final bundle.
"""

import json
import uuid

import pytest

from app.llm.base import BaseLLMProvider, ChatMessage, ChatResponse
from app.services.builtin_tools import build_stix_bundle_validator_tool
from app.services.builtin_tools.stix import CANDIDATE_REFERENCE
from app.tools import agent_loop as al
from app.tools.base import CompositeToolExecutor, ToolCall

_REPORT_ID = "report--8e2e2d2b-17d4-4cbf-938f-98ee46b3cd3f"
_IDENTITY_ID = "identity--f431f809-377b-45e0-aa1c-6a4751cae5ff"
_ABSENT_ID = "identity--0b7c1d0e-8f4a-4a5e-9a51-3b2a3c4d5e6f"


def _bundle(description_chars: int) -> dict:
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
                "description": "x" * description_chars,
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


_FAILING_PATCH = [{"op": "add", "path": "/objects/0/object_refs/-", "value": _ABSENT_ID}]
_VALID_PATCH = [{"op": "add", "path": "/objects/0/labels/-", "value": "phishing"}]


async def _call(executor, **arguments):
    return await executor.execute_tool(
        ToolCall(id=str(uuid.uuid4()), name="stix_bundle_validator", arguments=arguments)
    )


@pytest.mark.asyncio
async def test_a_large_invalid_bundle_is_answered_by_reference() -> None:
    _, executor = build_stix_bundle_validator_tool(strict_refs=True)
    result = await _call(executor, input_bundle=_bundle(300_000), patch=_FAILING_PATCH)
    shown = json.loads(result.content)
    assert shown["valid"] is False
    assert shown["error_count"] >= 1 and any(_ABSENT_ID in error for error in shown["errors"])
    assert "bundle" not in shown
    assert shown["bundle_ref"] == CANDIDATE_REFERENCE
    assert shown["bundle_chars"] > 300_000
    assert "patch_candidate" in shown["next_step"]
    assert len(result.content) < 4_000
    # The whole result stays available to the platform, never to the model.
    assert result.structured["bundle"]["objects"][0]["object_refs"][-1] == _ABSENT_ID


@pytest.mark.asyncio
async def test_a_large_valid_bundle_is_answered_by_reference_with_the_bundle_on_structured() -> None:
    _, executor = build_stix_bundle_validator_tool(strict_refs=True)
    result = await _call(executor, input_bundle=_bundle(300_000), patch=_VALID_PATCH)
    shown = json.loads(result.content)
    assert shown["valid"] is True and "bundle" not in shown
    assert len(result.content) < 4_000
    assert result.structured["bundle"]["objects"][0]["labels"] == ["apt", "phishing"]


@pytest.mark.asyncio
async def test_a_small_bundle_keeps_the_inline_contract() -> None:
    _, executor = build_stix_bundle_validator_tool(strict_refs=True)
    failed = json.loads((await _call(executor, input_bundle=_bundle(100), patch=_FAILING_PATCH)).content)
    assert failed["valid"] is False and failed["bundle"]["objects"][0]["name"] == "Original"
    assert "bundle_ref" not in failed
    assert failed["next_step"] == "Fix all errors and call stix_bundle_validator again."
    passed = json.loads((await _call(executor, input_bundle=_bundle(100), patch=_VALID_PATCH)).content)
    assert passed["valid"] is True and passed["bundle"]["objects"][0]["labels"] == ["apt", "phishing"]


@pytest.mark.asyncio
async def test_a_flow_build_always_inlines_the_bundle() -> None:
    _, executor = build_stix_bundle_validator_tool(inline_bundle_max_chars=None)
    shown = json.loads((await _call(executor, input_bundle=_bundle(300_000), patch=_VALID_PATCH)).content)
    assert shown["valid"] is True and shown["bundle"]["objects"][0]["labels"] == ["apt", "phishing"]


@pytest.mark.asyncio
async def test_patch_candidate_applies_the_patch_to_the_last_bundle_validated() -> None:
    _, executor = build_stix_bundle_validator_tool(strict_refs=True)
    original = _bundle(300_000)
    await _call(executor, input_bundle=original, patch=_FAILING_PATCH)
    fix = [{"op": "remove", "path": "/objects/0/object_refs/1"}, *_VALID_PATCH]
    result = await _call(executor, input_bundle=original, patch=fix, patch_candidate=True)
    assert json.loads(result.content)["valid"] is True
    assert result.structured["bundle"]["objects"][0]["object_refs"] == [_IDENTITY_ID]
    assert result.structured["bundle"]["objects"][0]["labels"] == ["apt", "phishing"]


@pytest.mark.asyncio
async def test_patch_candidate_without_a_candidate_or_a_patch_is_refused() -> None:
    _, executor = build_stix_bundle_validator_tool()
    first = json.loads(
        (await _call(executor, input_bundle=_bundle(10), patch=_VALID_PATCH, patch_candidate=True)).content
    )
    assert first["valid"] is False and "no bundle was validated yet" in first["errors"][0]
    await _call(executor, input_bundle=_bundle(10), patch=_VALID_PATCH)
    second = json.loads((await _call(executor, input_bundle=_bundle(10), patch_candidate=True)).content)
    assert second["valid"] is False and "needs a 'patch'" in second["errors"][0]


@pytest.mark.asyncio
async def test_the_repeat_guard_names_the_referenced_bundle() -> None:
    _, executor = build_stix_bundle_validator_tool(strict_refs=True)
    shown: dict = {}
    for _ in range(3):
        shown = json.loads((await _call(executor, input_bundle=_bundle(30_000), patch=_FAILING_PATCH)).content)
    assert shown["terminal"] is True
    assert "the referenced bundle ($candidate)" in shown["next_step"]
    assert "bundle above" not in shown["next_step"]


# ── Agent loop: a transformer on a large input ────────────────────────────────


class _RecordingLLM(BaseLLMProvider):
    provider_name = "stub"
    context_window = 5_000_000

    def __init__(self, script: list[ChatResponse]) -> None:
        self._script = script
        self.request_chars: list[int] = []

    async def chat(self, messages, model=None, tools=None, **kwargs):
        self.request_chars.append(sum(len(m.content or "") for m in messages if isinstance(m.content, str)))
        response = self._script[len(self.request_chars) - 1]
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


def _patch_call(call_id: str, patch: list) -> ChatResponse:
    return ChatResponse(
        content="",
        model="stub",
        stop_reason="tool_calls",
        tool_calls=[
            ToolCall(id=call_id, name="stix_bundle_validator", arguments={"input_bundle": "$input", "patch": patch})
        ],
    )


@pytest.mark.asyncio
async def test_a_transformer_on_a_large_input_never_reads_the_bundle_back(_quiet_langfuse) -> None:
    source = _bundle(2_600_000)
    text = "Add the phishing label.\n--- STIX BUNDLE ---\n" + json.dumps(source)
    assert len(text) > 2_500_000
    tools, executor = build_stix_bundle_validator_tool(strict_refs=True)
    composite = CompositeToolExecutor()
    composite.add_tools(tools, executor)
    llm = _RecordingLLM([_patch_call("c1", _FAILING_PATCH), _patch_call("c2", _VALID_PATCH)])
    messages = [ChatMessage(role="user", content=text)]

    response = await al.run_agent_loop(
        llm=llm,
        messages=messages,
        tools=composite.all_tools,
        executor=composite,
        max_iterations=5,
        progressive_tools=False,
        input_reference=True,
        finalize_on_tool={"tool_name": "stix_bundle_validator", "valid_key": "valid", "output_key": "bundle"},
    )

    # The failed validation reached the model as a few kB, never the bundle.
    tool_messages = [m for m in messages if m.role == "tool"]
    assert len(tool_messages) == 1
    assert len(tool_messages[0].content) < 4_000
    assert json.loads(tool_messages[0].content)["valid"] is False
    assert len(llm.request_chars) == 2
    assert llm.request_chars[1] - llm.request_chars[0] < 10_000

    # The valid patch finalised the run with the whole bundle, no model call after it.
    assert response.stop_reason == "finalize_tool"
    final = json.loads(response.content)
    assert final["objects"][0]["labels"] == ["apt", "phishing"]
    assert final["objects"][0]["description"] == source["objects"][0]["description"]
    expected = {**source, "objects": [{**source["objects"][0], "labels": ["apt", "phishing"]}, source["objects"][1]]}
    assert response.content == json.dumps(expected, ensure_ascii=False)
