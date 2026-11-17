"""A large input document never reaches the prompt inline, whatever the ``$input`` opt-in (#12263)."""

import json
from typing import Any

import pytest

from app.api.v1 import platform as platform_api
from app.llm.base import BaseLLMProvider, ChatMessage, ChatResponse, ContextOverflowError
from app.tools import agent_loop as al
from app.tools.base import CompositeToolExecutor, FileContext
from app.tools.session_input import (
    INPUT_INLINE_MAX_CHARS,
    INPUT_MOUNT_NAME,
    detach_input_document,
    input_reference_block,
    locate_input_document,
    oversize_input_chars,
)

_INSTRUCTIONS = "Enrich the report.\n\n--- STIX BUNDLE ---\n"


def _bundle(chars: int) -> dict[str, Any]:
    filler = "x" * 1000
    notes = [
        {"type": "note", "id": f"note--{i:08d}-0000-4000-8000-000000000000", "content": filler}
        for i in range(max(1, chars // 1100))
    ]
    return {"type": "bundle", "id": "bundle--1", "objects": [{"type": "report", "id": "report--1"}, *notes]}


class _RecordingLLM(BaseLLMProvider):
    provider_name = "stub"
    context_window = 1_000_000

    def __init__(self) -> None:
        self.requests: list[list[Any]] = []

    async def chat(self, messages, model=None, tools=None, **kwargs):  # type: ignore[no-untyped-def,override]
        self.requests.append(list(messages))
        return ChatResponse(content="done", model="stub")

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


# ── Change 1: the size protection is not the opt-in ──────────────────────────


@pytest.mark.asyncio
async def test_an_opted_out_run_still_detaches_a_bundle_the_window_cannot_hold(_quiet_langfuse: None) -> None:
    bundle = _bundle(3_100_000)
    content = _INSTRUCTIONS + json.dumps(bundle, indent=2)
    assert len(content) > 3_000_000
    llm = _RecordingLLM()
    ctx = FileContext()
    await al.run_agent_loop(
        llm=llm,
        messages=[ChatMessage(role="user", content=content)],
        tools=[],
        executor=CompositeToolExecutor(),
        max_iterations=2,
        progressive_tools=False,
        input_reference=False,
        file_context=ctx,
    )
    first = "".join(getattr(m, "content", "") or "" for m in llm.requests[0])
    assert len(first) < 100_000
    assert f"attachments/{INPUT_MOUNT_NAME}" in first
    # Not exposed: the block never offers the placeholder.
    assert "Reference it as $input" not in first
    assert json.loads(ctx.sandbox_inputs[INPUT_MOUNT_NAME]) == bundle


@pytest.mark.asyncio
async def test_an_opted_out_run_keeps_a_document_the_window_holds_inline(_quiet_langfuse: None) -> None:
    content = _INSTRUCTIONS + json.dumps(_bundle(300_000))
    llm = _RecordingLLM()
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


def test_the_oversize_limit_is_half_the_window_and_never_under_the_inline_limit() -> None:
    assert oversize_input_chars(1_000_000, 3) == 1_500_000
    assert oversize_input_chars(8_000, 3) == INPUT_INLINE_MAX_CHARS


def test_the_unexposed_block_names_the_sandbox_file_only() -> None:
    block = input_reference_block({"type": "bundle", "objects": []}, 42, exposed=False)
    assert f"attachments/{INPUT_MOUNT_NAME}" in block
    assert "Reference it as $input" not in block
    messages = [ChatMessage(role="user", content=_INSTRUCTIONS + json.dumps(_bundle(200_000)))]
    _document, detached = detach_input_document(messages, exposed=False)
    assert detached is True
    assert "Reference it as $input" not in messages[0].content


# ── Change 2: the document after the delimiter comes first ───────────────────


def test_instructions_opening_with_json_never_hide_the_bundle() -> None:
    bundle = _bundle(3_100_000)
    content = '{"example": 1}\nAdd a label.\n\n--- STIX BUNDLE ---\n' + json.dumps(bundle)
    found = locate_input_document(content)
    assert found is not None and found[0] == bundle
    messages = [ChatMessage(role="user", content=content)]
    document, detached = detach_input_document(messages)
    assert detached is True and document == bundle
    assert messages[0].content.startswith('{"example": 1}\nAdd a label.')


def test_a_fenced_json_example_never_hides_the_bundle() -> None:
    bundle = _bundle(50_000)
    content = 'Output like:\n```json\n{"op": "add"}\n```\n--- STIX BUNDLE ---\n' + json.dumps(bundle)
    found = locate_input_document(content)
    assert found is not None and found[0] == bundle


def test_a_json_example_without_delimiter_gives_way_to_a_bundle_after_it() -> None:
    bundle = _bundle(5_000)
    content = '{"example": 1}\nThe bundle:\n' + json.dumps(bundle)
    found = locate_input_document(content)
    assert found is not None and found[0] == bundle


def test_a_whole_json_message_is_still_the_document() -> None:
    document = {"alpha": [1, 2]}
    found = locate_input_document(json.dumps(document))
    assert found is not None and found[0] == document


# ── Change 3: the overflow message states the threshold ──────────────────────


def test_the_overflow_message_names_the_abort_threshold() -> None:
    message = str(ContextOverflowError(estimated_tokens=951_038, context_window=1_000_000, model="m"))
    assert "exceeds 1,000,000" not in message
    assert "95% abort threshold (950,000 tokens) of the 1,000,000 token window" in message


def test_an_estimate_over_the_window_still_says_by_how_much() -> None:
    message = str(ContextOverflowError(estimated_tokens=1_200_000, context_window=1_000_000))
    assert "exceeds the 1,000,000 token window by ~200,000 tokens" in message


# ── Change 4: a statement timeout is retryable ──────────────────────────────


class QueryCanceledError(Exception):
    sqlstate = "57014"


class _DBAPIError(Exception):
    def __init__(self, orig: BaseException) -> None:
        super().__init__(f"(sqlalchemy.dialects.postgresql.asyncpg.Error) {orig}")
        self.orig = orig


def test_a_statement_timeout_answers_503_retryable() -> None:
    error = _DBAPIError(QueryCanceledError("canceling statement due to statement timeout"))
    failure = platform_api.classify_platform_turn_failure(error)
    assert failure.status_code == 503
    assert failure.retryable is True
    body = failure.body("c")
    assert body["error_type"] == platform_api.PLATFORM_CHAT_DATABASE_TIMEOUT
    assert body["retryable"] is True


def test_a_statement_timeout_raised_from_another_error_is_found() -> None:
    try:
        try:
            raise QueryCanceledError("canceling statement due to statement timeout")
        except QueryCanceledError as inner:
            raise RuntimeError("turn setup failed") from inner
    except RuntimeError as outer:
        failure = platform_api.classify_platform_turn_failure(outer)
    assert failure.status_code == 503 and failure.retryable is True


def test_any_other_internal_error_stays_500_not_retryable() -> None:
    failure = platform_api.classify_platform_turn_failure(ValueError("boom"))
    assert failure.status_code == 500 and failure.retryable is False
