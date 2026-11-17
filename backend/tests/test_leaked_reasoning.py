"""A model's reasoning stays out of the prompts XTM One builds (#12356).

Claude declines a prompt that carries reasoning back to it
(``reasoning_extraction``). Memory strips it on write and on every read into a
prompt; the trace tools leave it out of what they return unless asked, and the
stored trace keeps it.
"""

import json
import uuid
from types import SimpleNamespace

import pytest

from app.services import agent_memory as agent_memory_mod
from app.services.agent_memory import format_memory_for_prompt
from app.services.builtin_tools import traces as traces_module
from app.services.builtin_tools.memory import _search_row
from app.services.leaked_reasoning import (
    REASONING_OMITTED,
    serialized_without_reasoning,
    strip_leading_reasoning,
    strip_reasoning_blocks,
    without_reasoning,
)
from app.services.persistent_memory import _entry_line, persist_memory
from tests.test_memory_truncation import AGENT_ID, _fake_llm, _run_consolidation
from tests.test_trace_builtin_tools import (
    _call_payload_tool,
    _fake_span,
    _fake_trace,
    _install_payload_fixtures,
)

LEAK = "<reasoning>Need to update memory, concise. Let me rewrite.</reasoning>"
OMITTED = f"{REASONING_OMITTED} "
SUMMARY = "**Monthly briefing**\n- fact one."

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("tag", ["reasoning", "thinking", "think", "REASONING"])
def test_strip_leading_reasoning_removes_a_leading_block(tag):
    text = f"  <{tag}>plan\nmore</{tag}>\n{SUMMARY}"
    assert strip_leading_reasoning(text) == SUMMARY


def test_strip_leading_reasoning_removes_consecutive_leading_blocks():
    assert strip_leading_reasoning(f"{LEAK}<think>x</think>{SUMMARY}") == SUMMARY


def test_strip_leading_reasoning_keeps_a_block_further_down():
    text = f"{SUMMARY}\n- quotes {LEAK} as an example."
    assert strip_leading_reasoning(text) == text


def test_strip_leading_reasoning_keeps_an_unclosed_block():
    text = f"<reasoning>cut by the token limit {SUMMARY}"
    assert strip_leading_reasoning(text) == text


def test_strip_reasoning_blocks_removes_blocks_anywhere():
    text = f"[consolidated] (id=1) {LEAK}{SUMMARY}"
    assert strip_reasoning_blocks(text) == f"[consolidated] (id=1) {SUMMARY}"
    assert strip_reasoning_blocks(text, marker=REASONING_OMITTED) == f"[consolidated] (id=1) {OMITTED}{SUMMARY}"


def test_a_block_that_quotes_its_own_tag_is_removed_whole():
    text = "<think>a<think>b</think>c</think>Answer"
    assert strip_leading_reasoning(text) == "Answer"
    assert strip_reasoning_blocks(f"x {text}") == "x Answer"


def test_an_unclosed_tag_named_earlier_does_not_hide_a_later_block():
    text = "Never write <thinking> in answers.\n<memory><thinking>leak</thinking> fact</memory>"
    assert strip_reasoning_blocks(text, marker=REASONING_OMITTED) == (
        f"Never write <thinking> in answers.\n<memory>{OMITTED}fact</memory>"
    )


def test_reasoning_that_names_its_own_tag_is_removed_whole():
    text = (
        "<thinking>The user asked me never to emit <thinking> tags, so I will answer directly."
        "</thinking>Here is the briefing."
    )
    assert strip_leading_reasoning(text) == "Here is the briefing."
    assert strip_reasoning_blocks(text, marker=REASONING_OMITTED) == f"{OMITTED}Here is the briefing."


def test_unclosed_reasoning_stays_linear():
    import time

    text = ("<think> " + "y" * 200) * 5000
    started = time.perf_counter()
    assert strip_reasoning_blocks(text) == text
    assert strip_leading_reasoning(text) == text
    assert time.perf_counter() - started < 2


def test_strip_reasoning_blocks_keeps_unclosed_tags_and_strips_later_closed_ones():
    text = "<think>never closed " * 3 + "<Reasoning>x</REASONING> end"
    assert strip_reasoning_blocks(text) == "<think>never closed " * 3 + "end"


def test_without_reasoning_marks_only_the_models_own_reasoning_omitted():
    payload = {
        "content": f"{LEAK}answer",
        "reasoning": "the model's reasoning",
        "messages": [{"role": "assistant", "content": "ok", "thinking": "replayed"}],
        "settings": {"thinking": True, "reasoning": {"effort": "low"}},
    }
    assert without_reasoning(payload) == {
        "content": f"{OMITTED}answer",
        "reasoning": REASONING_OMITTED,
        "messages": [{"role": "assistant", "content": "ok", "thinking": REASONING_OMITTED}],
        "settings": {"thinking": True, "reasoning": {"effort": "low"}},
    }


@pytest.mark.parametrize(
    "visible",
    [
        {"sub_scores": {"accuracy": 40}, "reasoning": "the judge's verdict rationale"},
        {"query": "x", "reasoning": "why the agent called this tool"},
        {"thinking": "adaptive", "effort": "low"},
    ],
)
def test_without_reasoning_keeps_a_reasoning_field_that_is_visible_content(visible):
    assert without_reasoning(visible) == visible


def test_serialized_without_reasoning_handles_a_cut_excerpt():
    full = json.dumps({"content": "answer", "reasoning": "long reasoning text", "x": 1})
    marked = '{"content": "answer", "reasoning": "[reasoning omitted]"'
    cut = full[: full.index("long") + 4] + "… [truncated 20 chars]"
    assert serialized_without_reasoning(cut) == marked + "… [truncated 20 chars]"
    assert serialized_without_reasoning(full) == marked + ', "x": 1}'
    assert json.loads(serialized_without_reasoning(full))["x"] == 1


@pytest.mark.parametrize(
    "visible",
    ['{"score": 4, "reasoning": "v"}', '{"query": "q", "thinking": "adaptive"}'],
)
def test_serialized_without_reasoning_keeps_visible_reasoning_fields(visible):
    assert serialized_without_reasoning(visible) == visible


# ---------------------------------------------------------------------------
# Memory: write
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_consolidation_stores_the_summary_without_the_leading_reasoning():
    llm = _fake_llm(f"{LEAK}{SUMMARY}")

    fake_redis, persist_mock = await _run_consolidation(llm)

    assert fake_redis.store[agent_memory_mod._medium_term_key(AGENT_ID)] == SUMMARY
    assert persist_mock.await_args.kwargs["content"] == SUMMARY


@pytest.mark.asyncio
async def test_a_reasoning_only_consolidation_keeps_the_old_summary():
    llm = _fake_llm(LEAK)

    fake_redis, _persist = await _run_consolidation(llm, medium_term=SUMMARY)

    assert fake_redis.store[agent_memory_mod._medium_term_key(AGENT_ID)] == SUMMARY


@pytest.mark.asyncio
async def test_consolidation_is_not_fed_the_stored_reasoning():
    llm = _fake_llm(SUMMARY)

    await _run_consolidation(llm, medium_term=f"{LEAK}{SUMMARY}")

    prompt = llm.chat.await_args_list[0].kwargs["messages"][1].content
    assert "<reasoning>" not in prompt
    assert SUMMARY in prompt


@pytest.mark.asyncio
async def test_persist_memory_stores_no_leading_reasoning(monkeypatch):
    """Every writer goes through persist_memory: the fact merge, extraction and reconcile included."""
    from app.services import persistent_memory

    captured = {}

    def _stop(content):
        captured["content"] = content
        raise _StoredError

    # The tradecraft guard is the first step to read the content: stop there.
    monkeypatch.setattr(persistent_memory, "in_tradecraft_run_context", lambda: True)
    monkeypatch.setattr(persistent_memory, "tradecraft_leak_reason", _stop)
    with pytest.raises(_StoredError):
        await persist_memory(
            None,
            agent_id=AGENT_ID,
            content=f"{LEAK}{SUMMARY}",
            memory_type=persistent_memory.TRADECRAFT_MEMORY_TYPE,
        )
    assert captured == {"content": SUMMARY}


@pytest.mark.asyncio
async def test_persist_memory_refuses_an_entry_that_was_only_reasoning():
    assert await persist_memory(None, agent_id=AGENT_ID, content=LEAK, memory_type="fact") is None


class _StoredError(Exception):
    pass


@pytest.mark.asyncio
async def test_short_term_entries_reach_consolidation_without_leading_reasoning():
    llm = _fake_llm(SUMMARY)

    await _run_consolidation(llm, short_summary=f"{LEAK}answered")

    prompt = llm.chat.await_args_list[0].kwargs["messages"][1].content
    assert "<reasoning>" not in prompt
    assert "Triage: answered" in prompt


@pytest.mark.asyncio
async def test_a_reasoning_only_short_term_summary_is_not_stored(monkeypatch):
    async def no_redis():
        raise AssertionError("nothing to store, Redis must not be reached")

    monkeypatch.setattr(agent_memory_mod, "redis_client", no_redis)
    assert await agent_memory_mod.store_short_term_memory(AGENT_ID, source="Run", summary=LEAK) == 0


@pytest.mark.asyncio
async def test_store_memory_rejects_reasoning_only_content():
    from app.services.builtin_tools.memory import build_memory_store_tool

    _defs, executor = build_memory_store_tool(AGENT_ID, str(uuid.uuid4()))
    from app.llm.base import ToolCall

    result = await executor.execute_tool(ToolCall(id="m1", name="store_memory", arguments={"content": LEAK}))
    assert "Rejected" in result.content


# ---------------------------------------------------------------------------
# Memory: every read into a prompt
# ---------------------------------------------------------------------------


def test_recalled_medium_term_memory_has_no_leading_reasoning():
    block = format_memory_for_prompt({"medium_term": f"{LEAK}{SUMMARY}", "short_term": []})
    assert "<reasoning>" not in block
    assert SUMMARY in block


def test_search_memory_result_has_no_leading_reasoning():
    row = {
        "id": "ab9342f0",
        "memory_type": "consolidated",
        "content": f"{LEAK}{SUMMARY}",
        "score": 0.5,
    }
    line = _search_row(row)
    assert "<reasoning>" not in line
    assert line.endswith(SUMMARY)


def test_persistent_context_line_has_no_leading_reasoning():
    entry = SimpleNamespace(memory_type="consolidated", content=f"{LEAK}{SUMMARY}")
    assert _entry_line(entry) == f"• [consolidated] {SUMMARY}"


# ---------------------------------------------------------------------------
# Traces: read only
# ---------------------------------------------------------------------------

_OUTPUT = {
    "content": "the answer",
    "tool_calls": [],
    "reasoning": "z" * 5000,
}
_INPUT = {
    "messages": [
        {"role": "tool", "content": f"Remembered context follows:\n[consolidated] (id=1) {LEAK}{SUMMARY}"}
    ]
}


@pytest.mark.asyncio
async def test_a_trace_payload_page_leaves_reasoning_out_and_pages_consistently(monkeypatch):
    agent_id = str(uuid.uuid4())
    span = _fake_span(output=_OUTPUT, input=_INPUT)
    trace = _fake_trace(agent_id)
    _install_payload_fixtures(monkeypatch, trace=trace, span=span)
    _defs, executor = traces_module.build_trace_tools(
        db=object(), user_id=str(uuid.uuid4()), agent_id=agent_id
    )

    out = json.loads(
        (await _call_payload_tool(executor, trace_id=str(trace.id), span_id=str(span.id), field="output")).content
    )
    assert "zzz" not in out["content"]
    assert REASONING_OMITTED in out["content"]
    assert out["total_chars"] == len(out["content"])
    assert out["next_offset"] is None

    inp = json.loads(
        (await _call_payload_tool(executor, trace_id=str(trace.id), span_id=str(span.id), field="input")).content
    )
    assert "<reasoning>" not in inp["content"]
    assert REASONING_OMITTED in inp["content"]
    assert SUMMARY.split("\n")[0] in inp["content"]

    # The stored span is untouched.
    assert span.output == _OUTPUT
    assert LEAK in span.input["messages"][0]["content"]


@pytest.mark.asyncio
async def test_get_trace_payload_returns_reasoning_when_asked(monkeypatch):
    agent_id = str(uuid.uuid4())
    span = _fake_span(output=_OUTPUT)
    trace = _fake_trace(agent_id)
    _install_payload_fixtures(monkeypatch, trace=trace, span=span)
    _defs, executor = traces_module.build_trace_tools(
        db=object(), user_id=str(uuid.uuid4()), agent_id=agent_id
    )

    page = json.loads(
        (
            await _call_payload_tool(
                executor,
                trace_id=str(trace.id),
                span_id=str(span.id),
                field="output",
                include_reasoning=True,
                max_chars=40_000,
            )
        ).content
    )
    assert page["content"] == json.dumps(_OUTPUT, ensure_ascii=False)


def test_a_span_preview_leaves_reasoning_out():
    preview, truncated = traces_module._preview(_OUTPUT, traces_module._PreviewBudget())
    assert truncated is False
    assert "zzz" not in preview
    assert REASONING_OMITTED in preview


def test_an_offloaded_excerpt_leaves_reasoning_out():
    serialized = json.dumps(_OUTPUT)
    envelope = {"preview": serialized[:200] + "… [truncated 4900 chars]"}
    assert "zzz" not in traces_module._envelope_excerpt(envelope)
    assert "zzzz" in traces_module._envelope_excerpt(envelope, include_reasoning=True)
