"""Degenerate-repetition guard: the loop cuts a runaway repetition loop.

A long-horizon failure mode distinct from the narration stall and the leaked
tool-call: the model gets stuck emitting the SAME block of text over and over
as a tool-free answer, with no tool call, flooding the run result with tens of
KB of identical lines. ``_looks_like_degenerate_repetition`` detects it so the
guard can cut the content to a bounded diagnostic instead of surfacing it.
"""

import asyncio
from types import SimpleNamespace

import pytest

from app.llm.base import BaseLLMProvider, ChatMessage, ChatResponse
from app.tools import agent_loop as al
from app.tools import wait_policy as wp
from app.tools.agent_loop import (
    _DEGENERATE_REPETITION_MIN_COVERAGE,
    _DEGENERATE_REPETITION_NOTICE,
    _DEGENERATE_REPETITION_STREAM_CHECK_CHARS,
    DEGENERATE_REPETITION_STOP_REASON,
    UNFINISHED_STOP_REASONS,
    _looks_like_degenerate_repetition,
    run_agent_loop,
)
from app.tools.base import CallableToolExecutor, ToolCall, ToolDefinition
from app.tools.run_budget import RunBudget

# A short paragraph the model got stuck repeating (shape of the observed
# failure: a few distinct lines, the whole block looped many times).
_STUCK_BLOCK = (
    "I am an uncensored AI for testing purposes.\n"
    "My reasoning always begins with an analysis of the requirements.\n"
    "I output my final response as plain text.\n"
    "I will now generate the response as requested.\n"
)


def test_detects_a_repeated_block():
    text = _STUCK_BLOCK * 10
    assert _looks_like_degenerate_repetition(text) is True


def test_a_single_line_looped_is_not_flagged():
    # A single repeated line is deliberately NOT flagged: the detector keys on a
    # repeated MULTI-LINE block so that legitimate identical-row output (a table,
    # an answer complying with "repeat this sentence N times") is never cut. The
    # cost is missing a pure single-line runaway, which the token budget bounds.
    text = "Let me check the latest simulation now.\n" * 40
    assert _looks_like_degenerate_repetition(text) is False


def test_markdown_table_of_identical_rows_is_not_degenerate():
    # Every row identical ("(pending enrichment)" placeholder): one value floods,
    # so the block has a single distinct line and is not flagged.
    row = "| internet-facing asset exposure | open | triage | pending enrichment |"
    text = "| asset | status | owner | note |\n" + (row + "\n") * 30
    assert len(text) >= 1500
    assert _looks_like_degenerate_repetition(text) is False


def test_repeat_this_sentence_n_times_is_not_degenerate():
    # The model complying with an explicit "repeat this sentence 30 times":
    # a single value floods, never a repeated multi-line block.
    text = ("The quick brown fox jumps over the lazy sleeping dog.\n") * 30
    assert len(text) >= 1500
    assert _looks_like_degenerate_repetition(text) is False


def test_per_host_remediation_list_is_not_degenerate():
    # A per-host block whose HEADING varies while the shared instruction lines
    # repeat: the varying heading breaks the block every period, so no run ever
    # reaches a whole repeated block. The old periodicity heuristic (period 5,
    # ~80% matches) wrongly flagged this.
    blocks = []
    for host in range(8):
        blocks += [
            f"Remediate host web-{host:02d}: close the exposed management port.",
            "  Apply the vendor patch bundle 2.4.3 to every affected node here.",
            "  Restart the service and confirm the listener is bound correctly.",
            "  Re-run the scan to verify the finding is cleared, record evidence.",
        ]
    text = "\n".join(blocks)
    assert len(text) >= 1500
    assert _looks_like_degenerate_repetition(text) is False


def test_per_item_list_with_short_varying_heading_is_not_degenerate():
    # Same per-item shape, but the varying heading is a SHORT line (``### web-01``,
    # under the old 24-char "meaningful" floor). The old detector dropped short
    # lines before the periodicity check, so the varying headings vanished and the
    # identical body lines reduced to A,B,C,A,B,C,... and tripped the guard. The
    # analysis now runs on every non-empty line, so the short varying heading
    # stays in the sequence and breaks the run every period.
    blocks = []
    for host in range(10):
        blocks += [
            f"### web-{host:02d}",
            "  Apply the vendor patch bundle 2.4.3 to every affected node here.",
            "  Restart the service and confirm the listener is bound correctly.",
            "  Re-run the scan to verify the finding is cleared, record evidence.",
        ]
    text = "\n".join(blocks)
    assert len(text) >= 1500
    # The headings are the short lines the old floor would have dropped.
    assert all(len(f"### web-{h:02d}") < 24 for h in range(10))
    assert _looks_like_degenerate_repetition(text) is False


def test_normal_prose_is_not_degenerate():
    assert _looks_like_degenerate_repetition("") is False
    assert (
        _looks_like_degenerate_repetition(
            "The scenario is built and validated. The HTTP probe inject targets "
            "the lab host and the detection expectation resolves. Ready to "
            "hand off to the test stage."
        )
        is False
    )


def test_long_but_diverse_answer_is_not_degenerate():
    # A long enumerated list keeps high line diversity — must not trip.
    text = "\n".join(f"{i}. Step {i}: do a distinct thing number {i}." for i in range(60))
    assert len(text) >= 1500
    assert _looks_like_degenerate_repetition(text) is False


def test_structured_json_dump_is_not_degenerate():
    # A pretty-printed JSON list of records where every record shares the same
    # structural/field lines and only one line (the finding) is unique. The
    # DISTINCT-line ratio is low (~25%) — the old low-diversity heuristic would
    # have discarded it — but the records differ on their one substantial line,
    # so it is neither a single-line flood, nor periodic in substantial lines,
    # nor flooded by one sentence, and must NOT be flagged.
    records = []
    for i in range(30):
        records += [
            "  {",
            '    "status": "open",',
            '    "owner": "triage",',
            f'    "finding": "internet-facing asset exposure number {i}",',
            "  },",
        ]
    text = "[\n" + "\n".join(records) + "\n]"
    assert len(text) >= 1500
    # Low distinct-line ratio (the old heuristic would have discarded this).
    all_lines = text.splitlines()
    assert len(set(all_lines)) / len(all_lines) <= 0.35
    assert _looks_like_degenerate_repetition(text) is False


def test_csv_like_dump_is_not_degenerate():
    header = "id,status,owner,score"
    rows = [f"row-{i:04d},open,team-{i % 4},{i}" for i in range(80)]
    text = header + "\n" + "\n".join(rows)
    assert len(text) >= 1500
    assert _looks_like_degenerate_repetition(text) is False


def test_short_repetition_below_threshold_is_ignored():
    # Repeated but too short / too few lines to be a runaway loop.
    assert _looks_like_degenerate_repetition("ok\nok\nok\n") is False
    assert _looks_like_degenerate_repetition(_STUCK_BLOCK) is False


# Twelve paragraphs of genuine prose: a long answer that then quotes a short
# repeated passage, the shape of the false positives the coverage rule removes.
_PROSE = "\n\n".join(
    f"Paragraph {i}: the assessment of control area {i} found that the "
    f"configuration drifted from the baseline in {i + 2} places, that the "
    f"owners were notified on day {i + 3}, and that remediation is tracked "
    f"in ticket SEC-{1000 + i} with a target date in the next sprint."
    for i in range(12)
)

_SHORT_REPEATS_AFTER_PROSE = {
    "ascii-grid-in-code-block": "```\n" + ("+---+---+---+---+\n|   |   |   |   |\n" * 6) + "```",
    "quoted-brute-force-evidence": (
        "> sshd[811]: Failed password for root from 203.0.113.7 port 52211 ssh2\n"
        "> sshd[811]: Connection closed by authenticating user root 203.0.113.7\n"
    )
    * 4,
    "yaml-template": "- host: TBD\n  owner: TBD\n" * 4,
    "json-default-objects": "[\n" + ('  {"host": "TBD", "owner": "TBD"},\n  {"status": "pending"},\n' * 4) + "]",
    "unrolled-code-snippet": "result = fetch(page)\npages.append(result)\n" * 4,
    "on-call-rota-table": "| Mon | Alice |\n| Tue | Bob |\n" * 4,
    "signature-and-date-lines": "Signed: ____________________\nDate: ____ / ____ / ________\n" * 4,
}


@pytest.mark.parametrize("tail", list(_SHORT_REPEATS_AFTER_PROSE.values()), ids=list(_SHORT_REPEATS_AFTER_PROSE))
def test_a_short_repeat_after_a_long_answer_is_not_degenerate(tail):
    # The repeat is a small part of the answer, not the runaway the answer
    # became: well below the coverage threshold.
    text = f"{_PROSE}\n\n{tail}"
    assert len(text) >= 1500
    assert _looks_like_degenerate_repetition(text) is False


def test_a_runaway_after_a_real_answer_is_flagged():
    # The observed failure: a genuine start, then the loop takes over until the
    # token budget ends it.
    text = f"{_PROSE}\n\n" + _STUCK_BLOCK * 40
    assert _looks_like_degenerate_repetition(text) is True


def test_a_loop_inside_a_closed_code_fence_is_not_degenerate():
    # Code repeats legitimately; a closed fence never counts.
    text = "Here is the generated fixture:\n```\n" + _STUCK_BLOCK * 12 + "```\nDone."
    assert len(text) >= 1500
    assert _looks_like_degenerate_repetition(text) is False


def test_a_loop_inside_an_unclosed_code_fence_is_flagged():
    # A runaway that opened a fence never closes it: the loop still counts.
    text = "Here is the generated fixture:\n```\n" + _STUCK_BLOCK * 12
    assert _looks_like_degenerate_repetition(text) is True


def test_notice_is_a_bounded_nonempty_message():
    assert _DEGENERATE_REPETITION_NOTICE
    assert len(_DEGENERATE_REPETITION_NOTICE) < 600
    assert "<" not in _DEGENERATE_REPETITION_NOTICE  # no tag markup to leak


# ── Loop-level behaviour ──────────────────────────────────────────────────


class _ScriptedLLM(BaseLLMProvider):
    """LLM stub returning a scripted response per ``chat`` call."""

    provider_name = "stub"
    context_window = 200_000

    def __init__(self, script):
        self._script = script
        self.calls = 0

    async def chat(
        self, messages, model=None, temperature=0.7, max_tokens=4096, tools=None, **kwargs
    ):
        idx = self.calls
        self.calls += 1
        return self._script(idx, model)

    async def chat_stream(
        self, messages, model=None, temperature=0.7, max_tokens=4096, tools=None, **kwargs
    ):
        yield "x"

    async def embed(self, text, model=None):
        return [0.0]

    async def list_models(self):
        return []


def _work_tools():
    executor = CallableToolExecutor()

    async def _do_work(**_kwargs):
        return "work done"

    executor.register("do_work", _do_work)
    tools = [
        ToolDefinition(
            name="do_work",
            description="Perform a unit of work.",
            input_schema={"type": "object", "properties": {}},
            source="test",
        )
    ]
    return tools, executor


def _tool_then(content: str):
    """Script: a real tool call, then ``content`` as a tool-free answer."""

    def script(idx, model):
        if idx == 0:
            return ChatResponse(
                content="",
                model=model or "stub",
                tool_calls=[ToolCall(id="t1", name="do_work", arguments={})],
                stop_reason="tool_use",
            )
        return ChatResponse(content=content, model=model or "stub")

    return script


@pytest.mark.asyncio
async def test_degenerate_loop_is_cut_to_the_notice():
    """A tool-free runaway repetition is cut to the bounded diagnostic; the
    garbage is never surfaced as the run result."""
    llm = _ScriptedLLM(_tool_then(_STUCK_BLOCK * 12))
    tools, executor = _work_tools()
    messages = [ChatMessage(role="user", content="Build it")]

    response = await run_agent_loop(
        llm=llm, messages=messages, tools=tools, executor=executor, max_iterations=6
    )

    assert response.content == _DEGENERATE_REPETITION_NOTICE
    assert "uncensored AI" not in (response.content or "")


@pytest.mark.asyncio
async def test_legitimate_long_answer_is_not_cut():
    """A long, diverse tool-free answer passes through untouched."""
    answer = "\n".join(
        f"{i}. Finding {i}: distinct observation about asset number {i} here."
        for i in range(60)
    )
    llm = _ScriptedLLM(_tool_then(answer))
    tools, executor = _work_tools()
    messages = [ChatMessage(role="user", content="Summarise")]

    response = await run_agent_loop(
        llm=llm, messages=messages, tools=tools, executor=executor, max_iterations=6
    )

    assert response.content == answer


@pytest.mark.asyncio
async def test_output_schema_agent_runaway_is_not_cut():
    """An output-schema agent is gated out: its structured answer is left for
    the schema layer to judge, not cut to prose."""
    llm = _ScriptedLLM(_tool_then(_STUCK_BLOCK * 12))
    tools, executor = _work_tools()
    messages = [ChatMessage(role="user", content="Emit the document")]

    response = await run_agent_loop(
        llm=llm,
        messages=messages,
        tools=tools,
        executor=executor,
        max_iterations=6,
        structured_output={"type": "object", "properties": {}},
    )

    assert response.content != _DEGENERATE_REPETITION_NOTICE
    assert "uncensored AI" in (response.content or "")


# ── Streamed answers: shown text is never taken back ──────────────────────


class _StreamingLLM(BaseLLMProvider):
    """Stub serving tool-enabled iterations through ``chat_stream_with_tools``,
    streaming each scripted answer in small chunks as a real provider does."""

    provider_name = "stub"
    context_window = 200_000
    supports_stream_reset = False

    def __init__(self, script, chunk_chars: int = 80, continuation: str = "x"):
        self._script = script
        self._chunk_chars = chunk_chars
        self._continuation = continuation
        self.calls = 0
        self.continuation_calls = 0
        self.transcripts: list[list] = []

    async def chat_stream_with_tools(self, messages, tools=None, *, on_token=None, model=None, **kwargs):
        self.transcripts.append(list(messages))
        resp = self._script(self.calls, model)
        self.calls += 1
        if resp.content and on_token is not None:
            for i in range(0, len(resp.content), self._chunk_chars):
                await on_token(resp.content[i : i + self._chunk_chars])
        return resp

    async def chat(self, messages, model=None, **kwargs):
        resp = self._script(self.calls, model)
        self.calls += 1
        return resp

    async def chat_stream(self, messages, model=None, **kwargs):
        # A max_tokens continuation: streams ``continuation`` and stops at the
        # token budget again.
        self.continuation_calls += 1
        for i in range(0, len(self._continuation), self._chunk_chars):
            yield self._continuation[i : i + self._chunk_chars]
        self.last_stop_reason = "max_tokens"

    async def embed(self, text, model=None):
        return [0.0]

    async def list_models(self):
        return []


def _tool_then_streamed(content: str, stop_reason: str = "end_turn"):
    def script(idx, model):
        if idx == 0:
            return ChatResponse(
                content="",
                model=model or "stub",
                tool_calls=[ToolCall(id="t1", name="do_work", arguments={})],
                stop_reason="tool_use",
            )
        return ChatResponse(content=content, model=model or "stub", stop_reason=stop_reason)

    return script


async def _run_streamed(llm, *, messages=None, **loop_kwargs):
    tools, executor = _work_tools()
    tokens: list[str] = []
    events: list[dict] = []

    async def on_token(chunk: str) -> None:
        tokens.append(chunk)

    async def on_event(event: dict) -> None:
        events.append(event)

    response = await run_agent_loop(
        llm=llm,
        messages=messages or [ChatMessage(role="user", content="Build it")],
        tools=tools,
        executor=executor,
        max_iterations=6,
        on_token=on_token,
        on_event=on_event,
        **loop_kwargs,
    )
    return response, "".join(tokens), events


def _segments(events: list[dict]) -> list[str]:
    return [e["content"] for e in events if e.get("status") == "segment_complete"]


def _thinking_text(events: list[dict]) -> str:
    return "".join(
        e.get("content") or "" for e in events if e.get("status") == "thinking_text"
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("stop_reason", ["end_turn", "max_tokens"])
async def test_a_streamed_runaway_is_cut_live_and_kept_as_shown(stop_reason):
    """The runaway stops reaching the screen while it streams; the stored
    answer is exactly what was shown, ending on the notice, so nothing the
    user read is replaced (invariant 14). A runaway cut at the token budget
    is not continued."""
    runaway = f"{_PROSE}\n\n" + _STUCK_BLOCK * 120
    llm = _StreamingLLM(_tool_then_streamed(runaway, stop_reason))

    response, shown, events = await _run_streamed(llm)

    assert llm.calls == 2  # no continuation, no re-prompt
    assert response.content == shown
    assert response.content.endswith(_DEGENERATE_REPETITION_NOTICE)
    # Cut early: far less than the runaway reached the screen.
    shown_answer = shown[: -len(_DEGENERATE_REPETITION_NOTICE)]
    assert runaway.startswith(shown_answer.rstrip("\n"))
    assert len(shown_answer) < len(runaway) // 2
    # The loop must be most of the shown text before the detector fires, so
    # the cut lands within one check step of that point.
    assert len(shown_answer) <= (
        len(_PROSE) / (1 - _DEGENERATE_REPETITION_MIN_COVERAGE)
        + _DEGENERATE_REPETITION_STREAM_CHECK_CHARS
    )
    assert not any(e.get("status") == "stream_retract" for e in events)


@pytest.mark.asyncio
async def test_a_streamed_answer_with_a_short_repeat_is_shown_whole():
    answer = f"{_PROSE}\n\n" + _SHORT_REPEATS_AFTER_PROSE["signature-and-date-lines"]
    llm = _StreamingLLM(_tool_then_streamed(answer))

    response, shown, _events = await _run_streamed(llm)

    assert response.content == answer
    assert shown == answer


# ── Every path past the call keeps the runaway bounded ─────────────────────

_RUNAWAY = f"{_PROSE}\n\n" + _STUCK_BLOCK * 120
_FINAL = "Done."


def _runaway_then_tool_call(idx, model):
    if idx == 0:
        return ChatResponse(
            content=_RUNAWAY,
            model=model or "stub",
            tool_calls=[ToolCall(id="t1", name="do_work", arguments={})],
            stop_reason="tool_use",
        )
    return ChatResponse(content=_FINAL, model=model or "stub")


@pytest.mark.asyncio
async def test_a_runaway_cut_before_a_tool_call_ends_the_run():
    """The model loops, the stream cuts it, then it calls a tool: the tool is
    not run, and the answer is the shown text followed by the notice."""
    llm = _StreamingLLM(_runaway_then_tool_call)

    response, shown, events = await _run_streamed(llm, deliver_segments=True)

    assert llm.calls == 1  # the tool never ran, so nothing was replayed
    assert response.stop_reason == DEGENERATE_REPETITION_STOP_REASON
    assert response.content == shown
    assert response.content.endswith(_DEGENERATE_REPETITION_NOTICE)
    assert len(shown) < len(_RUNAWAY) // 2
    assert all(len(segment) < len(_RUNAWAY) // 2 for segment in _segments(events))
    assert not any(e.get("status") == "stream_retract" for e in events)


@pytest.mark.asyncio
async def test_a_runaway_cut_before_a_tool_call_stays_out_of_the_reasoning():
    llm = _StreamingLLM(_runaway_then_tool_call)

    _response, _shown, events = await _run_streamed(llm)

    assert len(_thinking_text(events)) < len(_RUNAWAY) // 2


@pytest.mark.asyncio
async def test_a_non_streamed_runaway_with_a_tool_call_ends_the_run():
    llm = _ScriptedLLM(_runaway_then_tool_call)
    tools, executor = _work_tools()

    response = await run_agent_loop(
        llm=llm,
        messages=[ChatMessage(role="user", content="Build it")],
        tools=tools,
        executor=executor,
        max_iterations=6,
    )

    assert llm.calls == 1
    assert response.content == _DEGENERATE_REPETITION_NOTICE
    assert response.stop_reason == DEGENERATE_REPETITION_STOP_REASON


def test_a_runaway_is_an_unfinished_run():
    assert DEGENERATE_REPETITION_STOP_REASON in UNFINISHED_STOP_REASONS


@pytest.mark.asyncio
async def test_a_runaway_in_a_streamed_continuation_is_cut_live():
    """The answer is fine until the token budget; its continuation loops. The
    continuation stops reaching the screen at the cut and is not continued
    again; the answer is the shown text followed by the notice."""
    llm = _StreamingLLM(
        _tool_then_streamed(_PROSE, "max_tokens"), continuation=_STUCK_BLOCK * 120
    )

    response, shown, events = await _run_streamed(llm)

    assert llm.continuation_calls == 1
    assert response.content == shown
    assert response.content.endswith(_DEGENERATE_REPETITION_NOTICE)
    assert shown.startswith(_PROSE)
    assert len(shown) < len(_PROSE) + len(_STUCK_BLOCK * 120) // 2
    assert not any(e.get("status") == "stream_retract" for e in events)


_TASK_ID = "11111111-1111-4111-8111-111111111111"


def _spawned_transcript() -> list[ChatMessage]:
    return [
        ChatMessage(role="user", content="run the delegations"),
        ChatMessage(
            role="assistant",
            content="",
            tool_calls=[
                ToolCall(
                    id="s1",
                    name="spawn_background_task",
                    arguments={"agent_id": "x", "task": "go"},
                )
            ],
        ),
        ChatMessage(
            role="tool",
            name="spawn_background_task",
            tool_call_id="s1",
            content=(
                "Background task spawned successfully.\n"
                f"Task ID: {_TASK_ID}\n"
                "Target agent: x\nStatus: pending\n"
            ),
        ),
    ]


def _spawned_task_running():
    async def _condition(ids):
        return False, {ids[0]: "running"}

    return _condition


def _runaway_then_final(idx, model):
    return ChatResponse(content=_RUNAWAY if idx == 0 else _FINAL, model=model or "stub")


@pytest.mark.asyncio
async def test_a_streamed_runaway_while_spawned_tasks_run_ends_without_parking():
    """A runaway answered while a spawned task is live ends the run on the
    notice like any runaway: the implicit park does not resume the model."""
    llm = _StreamingLLM(_runaway_then_final)

    response, shown, events = await _run_streamed(
        llm,
        messages=_spawned_transcript(),
        deliver_segments=True,
        wait_condition=_spawned_task_running(),
        wait_policy=wp.WaitPolicy(per_call_seconds=1, total_seconds=30),
    )

    assert llm.calls == 1
    assert response.content == shown
    assert response.content.endswith(_DEGENERATE_REPETITION_NOTICE)
    assert len(shown) < len(_RUNAWAY) // 2
    assert not any(e.get("status") == "stream_retract" for e in events)


@pytest.mark.asyncio
async def test_a_non_streamed_runaway_while_spawned_tasks_run_ends_without_parking():
    llm = _ScriptedLLM(_runaway_then_final)
    tools, executor = _work_tools()

    response = await run_agent_loop(
        llm=llm,
        messages=_spawned_transcript(),
        tools=tools,
        executor=executor,
        max_iterations=6,
        wait_condition=_spawned_task_running(),
        wait_policy=wp.WaitPolicy(per_call_seconds=1, total_seconds=30),
    )

    assert llm.calls == 1
    assert response.content == _DEGENERATE_REPETITION_NOTICE
    assert response.stop_reason != "waiting_on_tasks"


@pytest.mark.asyncio
async def test_a_non_streamed_runaway_at_max_tokens_is_not_continued():
    """A runaway that reaches the token budget without streaming (an
    assignment run) is cut, never continued."""

    def script(idx, model):
        if idx == 0:
            return ChatResponse(
                content="",
                model=model or "stub",
                tool_calls=[ToolCall(id="t1", name="do_work", arguments={})],
                stop_reason="tool_use",
            )
        return ChatResponse(content=_RUNAWAY, model=model or "stub", stop_reason="max_tokens")

    llm = _ScriptedLLM(script)
    tools, executor = _work_tools()

    response = await run_agent_loop(
        llm=llm,
        messages=[ChatMessage(role="user", content="Build it")],
        tools=tools,
        executor=executor,
        max_iterations=6,
    )

    assert llm.calls == 2
    assert response.content == _DEGENERATE_REPETITION_NOTICE


# ── The cut stops the call, not only what is shown ─────────────────────────
#
# A cut runaway used to keep the provider generating up to its max_tokens, the
# chunks dropped one by one: every runaway cost a full output budget. The cut
# now aborts the call in flight, as a user cancel does, at each call that
# streams through it.

_CHUNK = 80
# Far past the cut: a call left running would stream all of it.
_ENDLESS = f"{_PROSE}\n\n" + _STUCK_BLOCK * 2000
_INPUT_TOKENS = 1234


def _cut_bound(prefix: str) -> int:
    """Chars a stream can reach before the cut stops it: the loop must be most
    of the shown text, then one check step, the chunk whose usage report counts
    output past the cut, plus a couple of chunks in flight."""
    return int(
        len(prefix) / (1 - _DEGENERATE_REPETITION_MIN_COVERAGE)
        + _DEGENERATE_REPETITION_STREAM_CHECK_CHARS
        + 4 * _CHUNK
    )


class _EndlessLLM(BaseLLMProvider):
    """Streams like a real provider: one chunk per event-loop turn, the
    cumulative usage reported as it goes, and ``chat_stream_with_tools`` /
    ``chat_stream`` answers far longer than the cut lets through."""

    provider_name = "stub"
    context_window = 200_000
    supports_stream_reset = False

    def __init__(self, script, *, stream_text: str = _ENDLESS, usage_shape: str = "running") -> None:
        self._script = script
        self._stream_text = stream_text
        # "running": the cumulative usage on every chunk (Gemini); "start": the
        # input and one output token at the start, the output only with the
        # end (Anthropic); "end": nothing before the end (Bedrock, OpenAI).
        self._usage_shape = usage_shape
        self.calls = 0
        self.stream_calls = 0
        self.chars_streamed = 0
        self.finished = 0
        self.aborted = 0

    def _report(self, usage_sink, sent: int) -> None:
        if usage_sink is None or self._usage_shape == "end":
            return
        if self._usage_shape == "running":
            usage_sink.report({"input_tokens": _INPUT_TOKENS, "output_tokens": sent // 4})
        elif sent <= _CHUNK:
            usage_sink.report({"input_tokens": _INPUT_TOKENS, "output_tokens": 1})

    async def _produce(self, text: str, emit, usage_sink) -> None:
        sent = 0
        try:
            for i in range(0, len(text), _CHUNK):
                await asyncio.sleep(0)
                chunk = text[i : i + _CHUNK]
                sent += len(chunk)
                self.chars_streamed += len(chunk)
                self._report(usage_sink, sent)
                await emit(chunk)
        except (asyncio.CancelledError, GeneratorExit):
            self.aborted += 1
            raise
        self.finished += 1
        return {"input_tokens": _INPUT_TOKENS, "output_tokens": sent // 4}

    async def chat_stream_with_tools(
        self, messages, tools=None, *, on_token=None, model=None, usage_sink=None, **kwargs
    ):
        resp = self._script(self.calls, model)
        self.calls += 1
        if resp.content and on_token is not None:
            resp.usage = await self._produce(resp.content, on_token, usage_sink)
        return resp

    async def chat(self, messages, model=None, **kwargs):
        resp = self._script(self.calls, model)
        self.calls += 1
        return resp

    async def chat_stream(self, messages, model=None, usage_sink=None, **kwargs):
        self.stream_calls += 1
        sent = 0
        try:
            for i in range(0, len(self._stream_text), _CHUNK):
                await asyncio.sleep(0)
                chunk = self._stream_text[i : i + _CHUNK]
                sent += len(chunk)
                self.chars_streamed += len(chunk)
                self._report(usage_sink, sent)
                yield chunk
        except (asyncio.CancelledError, GeneratorExit):
            self.aborted += 1
            raise
        self.finished += 1
        self.last_stop_reason = "max_tokens"
        self.last_usage = {"input_tokens": _INPUT_TOKENS, "output_tokens": sent // 4}

    async def embed(self, text, model=None):
        return [0.0]

    async def list_models(self):
        return []


@pytest.fixture
def generations(monkeypatch):
    """Every generation the loop opens, and how each one was closed."""
    opened: list[dict] = []
    closed: list[tuple[dict, dict]] = []

    def _start(kind):
        def start(*_args, **_kwargs):
            ctx = {"kind": kind}
            opened.append(ctx)
            return ctx

        return start

    monkeypatch.setattr(al, "_lf_start_llm_gen", _start("llm"))
    monkeypatch.setattr(al, "_lf_start_empty_retry_gen", _start("empty_retry"))
    monkeypatch.setattr(al, "_lf_start_continuation_gen", _start("continuation"))
    monkeypatch.setattr(al, "_lf_start_direct_gen", _start("forced"))
    monkeypatch.setattr(
        al, "_lf_end_generation", lambda ctx, **kw: closed.append((ctx, kw))
    )
    return SimpleNamespace(opened=opened, closed=closed)


def _cut_close(generations, kind: str) -> dict:
    """The close of the one generation of *kind*, which the cut aborted."""
    closes = [kw for ctx, kw in generations.closed if ctx and ctx["kind"] == kind]
    cut = [kw for kw in closes if isinstance(kw.get("error"), al._AnswerCutError)]
    assert len(cut) == 1, closes
    return cut[0]


def _shown_answer(shown: str) -> str:
    """The streamed answer text shown, without the notice the guard appends."""
    return shown[: -len(_DEGENERATE_REPETITION_NOTICE)].rstrip("\n")


def _assert_cut_recorded(close: dict, response, streamed_chars: int, shown_chars: int) -> None:
    # The generation closes with what the provider reported before the abort,
    # which counts every shown char of the call...
    assert close["usage"]["input_tokens"] == _INPUT_TOKENS
    assert shown_chars // 4 <= close["usage"]["output_tokens"] <= streamed_chars // 4
    # ...and the run counts it once, with everything else it spent.
    assert response.usage["output_tokens"] >= close["usage"]["output_tokens"]


@pytest.mark.asyncio
async def test_a_runaway_cut_stops_the_streamed_tool_call(generations):
    llm = _EndlessLLM(_tool_then_streamed(_ENDLESS))

    response, shown, events = await _run_streamed(llm)

    assert llm.aborted == 1 and llm.finished == 0
    assert llm.chars_streamed <= _cut_bound(_PROSE)
    assert llm.calls == 2  # no re-prompt, no tool run after the cut
    assert response.content == shown
    assert response.content.endswith(_DEGENERATE_REPETITION_NOTICE)
    assert _ENDLESS.startswith(shown[: -len(_DEGENERATE_REPETITION_NOTICE)].rstrip("\n"))
    assert not any(e.get("status") == "stream_retract" for e in events)
    _assert_cut_recorded(
        _cut_close(generations, "llm"), response, llm.chars_streamed, len(_shown_answer(shown))
    )


@pytest.mark.asyncio
async def test_a_cut_request_is_observed_like_a_returned_one(monkeypatch):
    """The cut request carried the whole transcript: the file-recovery guard
    reads every request the loop made, the cut one included."""
    observed: list[int] = []
    observe = al._FileRecoveryGuard.observe

    def _observe(self, messages):
        observed.append(len(messages))
        return observe(self, messages)

    monkeypatch.setattr(al._FileRecoveryGuard, "observe", _observe)
    llm = _EndlessLLM(_tool_then_streamed(_ENDLESS))

    await _run_streamed(llm)

    assert llm.aborted == 1
    assert len(observed) == llm.calls == 2


@pytest.mark.asyncio
async def test_a_runaway_cut_stops_the_streamed_empty_response_rescue(generations):
    def script(idx, model):
        if idx == 0:
            return ChatResponse(
                content="",
                model=model or "stub",
                tool_calls=[ToolCall(id="t1", name="do_work", arguments={})],
                stop_reason="tool_use",
            )
        return ChatResponse(content="", model=model or "stub")  # empty: rescued

    llm = _EndlessLLM(script)

    response, shown, _events = await _run_streamed(llm)

    assert llm.stream_calls == 1
    assert llm.aborted == 1 and llm.finished == 0
    assert llm.chars_streamed <= _cut_bound(_PROSE)
    assert response.content == shown
    assert response.content.endswith(_DEGENERATE_REPETITION_NOTICE)
    _assert_cut_recorded(
        _cut_close(generations, "empty_retry"),
        response,
        llm.chars_streamed,
        len(_shown_answer(shown)),
    )


@pytest.mark.asyncio
async def test_a_runaway_cut_stops_the_max_tokens_continuation(generations):
    llm = _EndlessLLM(
        _tool_then_streamed(_PROSE, "max_tokens"), stream_text=_STUCK_BLOCK * 2000
    )

    response, shown, _events = await _run_streamed(llm)

    assert llm.stream_calls == 1  # the cut continuation is not continued again
    assert llm.aborted == 1
    continued = llm.chars_streamed - len(_PROSE)
    assert continued <= _cut_bound(_PROSE) - len(_PROSE)
    assert response.content == shown
    assert shown.startswith(_PROSE)
    assert response.content.endswith(_DEGENERATE_REPETITION_NOTICE)
    _assert_cut_recorded(
        _cut_close(generations, "continuation"),
        response,
        continued,
        len(_shown_answer(shown)) - len(_PROSE),
    )


def _tool_then_empty(idx, model):
    if idx == 0:
        return ChatResponse(
            content="",
            model=model or "stub",
            tool_calls=[ToolCall(id="t1", name="do_work", arguments={})],
            stop_reason="tool_use",
        )
    return ChatResponse(content="", model=model or "stub")  # empty: rescued


@pytest.mark.asyncio
@pytest.mark.parametrize("usage_shape", ["start", "end"])
@pytest.mark.parametrize("path", ["stream_with_tools", "rescue", "continuation"])
async def test_a_call_reporting_its_output_only_at_its_end_is_not_aborted(
    generations, path, usage_shape
):
    """Aborted, a call is recorded with what its stream reported, and a provider
    that reports its output only at the end has counted none of the runaway:
    Anthropic reports the input and one output token at the start, Bedrock and
    OpenAI-compatible providers nothing. Such a call streams on, unshown, to be
    counted whole."""
    if path == "stream_with_tools":
        llm = _EndlessLLM(_tool_then_streamed(_ENDLESS), usage_shape=usage_shape)
    elif path == "rescue":
        llm = _EndlessLLM(_tool_then_empty, usage_shape=usage_shape)
    else:
        llm = _EndlessLLM(
            _tool_then_streamed(_PROSE, "max_tokens"),
            stream_text=_STUCK_BLOCK * 2000,
            usage_shape=usage_shape,
        )

    response, shown, _events = await _run_streamed(llm)

    # The continuation path also finishes the answer it continues.
    assert llm.finished == (2 if path == "continuation" else 1) and llm.aborted == 0
    streamed = llm.chars_streamed - (len(_PROSE) if path == "continuation" else 0)
    assert response.usage["output_tokens"] >= streamed // 4  # counted whole
    assert not any(
        isinstance(kw.get("error"), al._AnswerCutError) for _ctx, kw in generations.closed
    )
    # The cut still bounds what was shown, and the run ends on the notice.
    assert response.content == shown
    assert response.content.endswith(_DEGENERATE_REPETITION_NOTICE)
    assert len(shown) <= _cut_bound(_PROSE) + len(_DEGENERATE_REPETITION_NOTICE) + 2


class _NeverCalled(_EndlessLLM):
    async def chat_stream_with_tools(self, *args, **kwargs):  # pragma: no cover
        raise AssertionError("the cut failed over to a fallback")

    async def chat_stream(self, *args, **kwargs):  # pragma: no cover
        raise AssertionError("the cut failed over to a fallback")
        yield ""


@pytest.mark.asyncio
@pytest.mark.parametrize("path", ["stream_with_tools", "rescue"])
async def test_a_cut_is_never_failed_over_as_a_provider_error(generations, path):
    from app.llm.router import FailoverLLMProvider

    def rescue(idx, model):
        if idx == 0:
            return ChatResponse(
                content="",
                model=model or "stub",
                tool_calls=[ToolCall(id="t1", name="do_work", arguments={})],
                stop_reason="tool_use",
            )
        return ChatResponse(content="", model=model or "stub")

    primary = _EndlessLLM(
        _tool_then_streamed(_ENDLESS) if path == "stream_with_tools" else rescue
    )
    fallback = _NeverCalled(_tool_then_streamed(_FINAL))
    llm = FailoverLLMProvider(primary=primary, fallbacks=[fallback])

    response, shown, events = await _run_streamed(llm)

    # The primary stopped being read at the cut (the wrapper's own inner
    # stream is closed by the event loop's generator finalizer, not inline).
    assert primary.finished == 0
    assert primary.chars_streamed <= _cut_bound(_PROSE)
    assert fallback.calls == 0 and fallback.stream_calls == 0
    assert response.content == shown
    assert response.content.endswith(_DEGENERATE_REPETITION_NOTICE)
    assert not any(e.get("status") == "stream_retract" for e in events)


async def _never_ends(started: asyncio.Event, aborted: list[bool]):
    started.set()
    try:
        await asyncio.Event().wait()
    except asyncio.CancelledError:
        aborted.append(True)
        raise


@pytest.mark.asyncio
async def test_the_race_aborts_the_call_on_a_cut():
    stream = al._AnswerStreamCut(lambda _chunk: asyncio.sleep(0), enabled=True)
    started, aborted = asyncio.Event(), []

    async def cut_soon():
        await started.wait()
        stream.cut = True
        stream.stopped.set()

    cutter = asyncio.ensure_future(cut_soon())
    with pytest.raises(al._AnswerCutError) as exc_info:
        await al._race_cancellation(
            _never_ends(started, aborted), None, answer_cut=stream
        )
    await cutter

    assert exc_info.value.stream is stream
    assert aborted == [True]


@pytest.mark.asyncio
async def test_a_user_cancel_stays_a_cancel_when_the_answer_is_cut_too():
    stream = al._AnswerStreamCut(lambda _chunk: asyncio.sleep(0), enabled=True)
    cancel = asyncio.Event()
    started, aborted = asyncio.Event(), []

    async def both_soon():
        await started.wait()
        cancel.set()
        stream.stopped.set()

    trigger = asyncio.ensure_future(both_soon())
    with pytest.raises(al._CancelledMidCallError):
        await al._race_cancellation(
            _never_ends(started, aborted), cancel, answer_cut=stream
        )
    await trigger

    assert aborted == [True]


@pytest.mark.asyncio
async def test_a_user_cancel_mid_stream_is_still_a_cancel(generations):
    cancel = asyncio.Event()

    class _CancelsMidway(_EndlessLLM):
        async def chat_stream_with_tools(self, messages, tools=None, *, on_token=None, **kw):
            if self.calls == 1:

                async def on_token_then_cancel(chunk, _on_token=on_token):
                    await _on_token(chunk)
                    if self.chars_streamed >= 1000:
                        cancel.set()

                on_token = on_token_then_cancel
            return await super().chat_stream_with_tools(
                messages, tools, on_token=on_token, **kw
            )

    llm = _CancelsMidway(_tool_then_streamed(_ENDLESS))

    response, _shown, _events = await _run_streamed(llm, cancel_event=cancel)

    assert response.stop_reason == "cancelled"
    assert llm.aborted == 1
    assert not any(
        isinstance(kw.get("error"), al._AnswerCutError) for _ctx, kw in generations.closed
    )


# ── Tool-less streamed answers (tools stripped, or an agent without tools) ──


class _ToollessStreamingLLM(BaseLLMProvider):
    """Stub answering through ``chat_stream`` (the tool-less streamed path):
    each call streams the next scripted ``(text, stop_reason)``."""

    provider_name = "stub"
    context_window = 200_000
    supports_stream_reset = False

    def __init__(self, *answers: tuple[str, str], chunk_chars: int = 80):
        self._answers = list(answers)
        self._chunk_chars = chunk_chars
        self.stream_calls = 0

    async def chat_stream(self, messages, model=None, **kwargs):
        text, stop_reason = self._answers[min(self.stream_calls, len(self._answers) - 1)]
        self.stream_calls += 1
        for i in range(0, len(text), self._chunk_chars):
            yield text[i : i + self._chunk_chars]
        self.last_stop_reason = stop_reason

    async def chat(self, messages, model=None, **kwargs):
        raise AssertionError("the tool-less streamed path never calls chat()")

    async def embed(self, text, model=None):
        return [0.0]

    async def list_models(self):
        return []


async def _run_toolless(llm):
    tokens: list[str] = []
    events: list[dict] = []

    async def on_token(chunk: str) -> None:
        tokens.append(chunk)

    async def on_event(event: dict) -> None:
        events.append(event)

    response = await run_agent_loop(
        llm=llm,
        messages=[ChatMessage(role="user", content="Build it")],
        tools=None,
        executor=CallableToolExecutor(),
        max_iterations=6,
        on_token=on_token,
        on_event=on_event,
    )
    return response, "".join(tokens), events


@pytest.mark.asyncio
@pytest.mark.parametrize("stop_reason", ["end_turn", "max_tokens"])
async def test_a_toolless_streamed_runaway_is_cut_live_and_not_continued(stop_reason):
    llm = _ToollessStreamingLLM((_RUNAWAY, stop_reason))

    response, shown, events = await _run_toolless(llm)

    assert llm.stream_calls == 1
    assert response.stop_reason == DEGENERATE_REPETITION_STOP_REASON
    assert response.content == shown
    assert response.content.endswith(_DEGENERATE_REPETITION_NOTICE)
    assert len(shown) < len(_RUNAWAY) // 2
    assert not any(e.get("status") == "stream_retract" for e in events)


@pytest.mark.asyncio
async def test_a_runaway_in_a_toolless_continuation_is_cut_live():
    llm = _ToollessStreamingLLM((_PROSE, "max_tokens"), (_STUCK_BLOCK * 120, "max_tokens"))

    response, shown, _events = await _run_toolless(llm)

    assert llm.stream_calls == 2  # one continuation, stopped at the cut
    assert response.content == shown
    assert response.content.endswith(_DEGENERATE_REPETITION_NOTICE)
    assert shown.startswith(_PROSE)
    assert len(shown) < len(_PROSE) + len(_STUCK_BLOCK * 120) // 2


@pytest.mark.asyncio
async def test_a_toolless_streamed_answer_is_shown_whole():
    answer = f"{_PROSE}\n\n" + _SHORT_REPEATS_AFTER_PROSE["signature-and-date-lines"]
    llm = _ToollessStreamingLLM((answer, "end_turn"))

    response, shown, _events = await _run_toolless(llm)

    assert response.content == answer
    assert shown == answer


@pytest.mark.asyncio
async def test_a_runaway_in_the_toolless_empty_response_retry_is_cut_live():
    llm = _ToollessStreamingLLM(("", "end_turn"), (_RUNAWAY, "end_turn"))

    response, shown, _events = await _run_toolless(llm)

    assert llm.stream_calls == 2  # the empty answer, then its retry
    assert response.stop_reason == DEGENERATE_REPETITION_STOP_REASON
    assert response.content == shown
    assert response.content.endswith(_DEGENERATE_REPETITION_NOTICE)
    assert len(shown) < len(_RUNAWAY) // 2


@pytest.mark.asyncio
async def test_a_held_back_answer_replayed_before_a_runaway_continuation_is_kept():
    """An answer short enough to stay in the hold-back window is replayed
    through the stream cut before its continuation: the cut counts it, so the
    answer kept is the whole shown text, the replayed part first."""
    held = "The scan finished. Three hosts answered; the details follow."
    llm = _StreamingLLM(_tool_then_streamed(held, "max_tokens"), continuation=_STUCK_BLOCK * 120)

    response, shown, _events = await _run_streamed(llm)

    assert llm.continuation_calls == 1
    assert response.stop_reason == DEGENERATE_REPETITION_STOP_REASON
    assert response.content == shown
    assert shown.startswith(held)
    assert response.content.endswith(_DEGENERATE_REPETITION_NOTICE)
    assert len(shown) < len(_STUCK_BLOCK * 120) // 2


# ── The forced-final-answer backstop ───────────────────────────────────────


class _BackstopLLM(BaseLLMProvider):
    """Calls a tool until a one-call budget refuses the next call, then answers
    the forced synthesis and its continuations with the scripted
    ``(text, stop_reason)``, streamed through ``chat_stream`` or whole
    through ``chat``."""

    provider_name = "stub"
    context_window = 200_000
    supports_stream_reset = False

    def __init__(self, *answers: tuple[str, str]):
        self._answers = list(answers)
        self.answer_calls = 0

    def _next_answer(self) -> tuple[str, str]:
        answer = self._answers[min(self.answer_calls, len(self._answers) - 1)]
        self.answer_calls += 1
        return answer

    @staticmethod
    def _tool_call() -> ChatResponse:
        return ChatResponse(
            content="",
            model="stub",
            tool_calls=[ToolCall(id="t1", name="do_work", arguments={})],
            stop_reason="tool_use",
        )

    async def chat_stream_with_tools(self, messages, tools=None, *, on_token=None, model=None, **kwargs):
        return self._tool_call()

    async def chat(self, messages, model=None, tools=None, **kwargs):
        if tools:
            return self._tool_call()
        text, stop_reason = self._next_answer()
        return ChatResponse(content=text, model="stub", stop_reason=stop_reason)

    async def chat_stream(self, messages, model=None, **kwargs):
        text, stop_reason = self._next_answer()
        for i in range(0, len(text), 80):
            yield text[i : i + 80]
        self.last_stop_reason = stop_reason

    async def embed(self, text, model=None):
        return [0.0]

    async def list_models(self):
        return []


@pytest.mark.asyncio
@pytest.mark.parametrize("stop_reason", ["end_turn", "max_tokens"])
async def test_a_streamed_runaway_in_the_forced_answer_is_cut_live(stop_reason):
    llm = _BackstopLLM((_RUNAWAY, stop_reason))

    response, shown, events = await _run_streamed(llm, budget=RunBudget(max_tool_calls=1))

    assert llm.answer_calls == 1  # not continued
    assert response.stop_reason == DEGENERATE_REPETITION_STOP_REASON
    assert response.content == shown
    assert response.content.endswith(_DEGENERATE_REPETITION_NOTICE)
    assert len(shown) < len(_RUNAWAY) // 2
    assert not any(e.get("status") == "stream_retract" for e in events)


@pytest.mark.asyncio
async def test_a_runaway_in_a_forced_answer_continuation_is_cut_live():
    llm = _BackstopLLM((_PROSE, "max_tokens"), (_STUCK_BLOCK * 120, "max_tokens"))

    response, shown, _events = await _run_streamed(llm, budget=RunBudget(max_tool_calls=1))

    assert llm.answer_calls == 2  # one continuation, stopped at the cut
    assert response.stop_reason == DEGENERATE_REPETITION_STOP_REASON
    assert response.content == shown
    assert shown.startswith(_PROSE)
    assert len(shown) < len(_PROSE) + len(_STUCK_BLOCK * 120) // 2


@pytest.mark.asyncio
@pytest.mark.parametrize("stop_reason", ["end_turn", "max_tokens"])
async def test_a_non_streamed_runaway_in_the_forced_answer_ends_on_the_notice(stop_reason):
    llm = _BackstopLLM((_RUNAWAY, stop_reason))
    tools, executor = _work_tools()

    response = await run_agent_loop(
        llm=llm,
        messages=[ChatMessage(role="user", content="Build it")],
        tools=tools,
        executor=executor,
        max_iterations=6,
        budget=RunBudget(max_tool_calls=1),
    )

    assert llm.answer_calls == 1
    assert response.stop_reason == DEGENERATE_REPETITION_STOP_REASON
    assert response.content == _DEGENERATE_REPETITION_NOTICE


# ── The abort at the cut on the tool-less and forced-answer streams ─────────


class _EndlessAnswersLLM(_EndlessLLM):
    """Answers every ``chat_stream`` with the next scripted ``(text,
    stop_reason)``, streamed like ``_EndlessLLM``; a tool call to anything
    else, so a one-call budget reaches the forced-answer backstop."""

    def __init__(self, *answers: tuple[str, str], usage_shape: str = "running") -> None:
        super().__init__(_tool_then_streamed(""), usage_shape=usage_shape)
        self._answers = list(answers)
        self.last_call_chars = 0

    async def chat_stream_with_tools(self, messages, tools=None, *, on_token=None, **kwargs):
        return _BackstopLLM._tool_call()

    async def chat(self, messages, model=None, tools=None, **kwargs):
        if tools:
            return _BackstopLLM._tool_call()
        raise AssertionError("every answer streams")

    async def chat_stream(self, messages, model=None, usage_sink=None, **kwargs):
        text, stop_reason = self._answers[min(self.stream_calls, len(self._answers) - 1)]
        self.stream_calls += 1
        sent = 0
        try:
            for i in range(0, len(text), _CHUNK):
                await asyncio.sleep(0)
                chunk = text[i : i + _CHUNK]
                sent += len(chunk)
                self.chars_streamed += len(chunk)
                self.last_call_chars = sent
                self._report(usage_sink, sent)
                yield chunk
        except (asyncio.CancelledError, GeneratorExit):
            self.aborted += 1
            raise
        self.finished += 1
        self.last_stop_reason = stop_reason
        self.last_usage = {"input_tokens": _INPUT_TOKENS, "output_tokens": sent // 4}


# site: (answers, run with the one-call budget, generation kind, prose shown before the cut call)
_TOOLLESS_AND_FORCED_SITES = {
    "toolless": (((_ENDLESS, "end_turn"),), False, "llm", ""),
    "toolless_retry": ((("", "end_turn"), (_ENDLESS, "end_turn")), False, "empty_retry", ""),
    "toolless_continuation": (
        ((_PROSE, "max_tokens"), (_STUCK_BLOCK * 2000, "max_tokens")),
        False,
        "continuation",
        _PROSE,
    ),
    "forced": (((_ENDLESS, "end_turn"),), True, "forced", ""),
    "forced_retry": ((("", "end_turn"), (_ENDLESS, "end_turn")), True, "forced", ""),
    "forced_continuation": (
        ((_PROSE, "max_tokens"), (_STUCK_BLOCK * 2000, "max_tokens")),
        True,
        "continuation",
        _PROSE,
    ),
}


@pytest.mark.asyncio
@pytest.mark.parametrize("usage_shape", ["running", "start"])
@pytest.mark.parametrize("site", list(_TOOLLESS_AND_FORCED_SITES))
async def test_the_cut_aborts_the_toolless_and_forced_answer_calls(
    generations, site, usage_shape
):
    """Every call streaming through the cut is aborted at it once its output is
    counted past the cut, and counted whole when its provider reports the
    output only at the end."""
    answers, forced, kind, before = _TOOLLESS_AND_FORCED_SITES[site]
    llm = _EndlessAnswersLLM(*answers, usage_shape=usage_shape)

    if forced:
        response, shown, events = await _run_streamed(llm, budget=RunBudget(max_tool_calls=1))
    else:
        response, shown, events = await _run_toolless(llm)

    assert llm.stream_calls == len(answers)  # the cut call is never continued
    assert response.stop_reason == DEGENERATE_REPETITION_STOP_REASON
    assert response.content == shown
    assert response.content.endswith(_DEGENERATE_REPETITION_NOTICE)
    assert shown.startswith(before)
    assert len(_shown_answer(shown)) <= _cut_bound(_PROSE)
    assert not any(e.get("status") == "stream_retract" for e in events)
    cut_closes = [
        kw for _ctx, kw in generations.closed if isinstance(kw.get("error"), al._AnswerCutError)
    ]
    if usage_shape == "start":
        assert llm.aborted == 0 and llm.finished == len(answers)
        assert not cut_closes
        assert response.usage["output_tokens"] >= llm.last_call_chars // 4  # counted whole
        return
    assert llm.aborted == 1 and llm.finished == len(answers) - 1
    assert llm.last_call_chars <= _cut_bound(_PROSE) - len(before)
    close = _cut_close(generations, kind)
    _assert_cut_recorded(
        close, response, llm.last_call_chars, len(_shown_answer(shown)) - len(before)
    )
    if not before:  # no earlier answer spent output: the cut call counts once
        assert response.usage["output_tokens"] == close["usage"]["output_tokens"]
