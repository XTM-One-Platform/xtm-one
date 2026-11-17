"""No utility call uses an answer cut at ``max_tokens`` as if it were complete.

Every caller below parses or keeps a model answer outside the agentic loop. A
cut answer is asked once more with the budget ``retry_output_budget`` allows
(``app.llm.cut_answers.chat_retrying_a_cut``); an answer still cut takes the
path the caller already takes when its call fails, so no caller behaves worse
than on a provider error - except the prompt generator and comment drafts,
whose text a person edits before anything is stored: they return it as main
did, flagged ``truncated``. A decision read from the first token (routing, flow
conditions) is complete once that token is there, so a cut after it is read as
it always was. The assignment activation gate and the Nova rule LLM check are
left exactly as they were, by decision: they are pinned here too. The
providers are fakes replaying scripted ``(content, stop_reason)`` answers - no
model is ever called - and the last section drives the real provider classes
over fake SDK clients, so the cut stop reason is shown to reach a caller from
every provider family, on the plain and the streamed path.
"""

from __future__ import annotations

import asyncio
import inspect
import json
import uuid
from types import SimpleNamespace
from typing import Any

import pytest

from app.llm.base import BaseLLMProvider, ChatMessage, ChatResponse
from app.tools.base import ToolCall, ToolDefinition

CUT = "max_tokens"
DONE = "end_turn"


class _Script:
    """A provider replaying scripted answers and recording every call's kwargs.

    Each answer is ``(content, stop_reason)``, ``(content, stop_reason,
    tool_calls)``, ``(content, stop_reason, tool_calls, usage)`` or
    ``(content, stop_reason, tool_calls, usage, served_model)`` (``"m"`` when
    omitted). No ``max_output_tokens`` and no default model, so the retry
    budget is exactly twice the first one.
    """

    provider_name = "script"

    def __init__(self, *answers: tuple) -> None:
        self._answers = list(answers)
        self.calls: list[dict[str, Any]] = []
        self.last_stop_reason: str | None = None

    def _next(self) -> ChatResponse:
        answer = self._answers.pop(0)
        content, stop_reason = answer[0], answer[1]
        tool_calls = answer[2] if len(answer) > 2 else None
        usage = answer[3] if len(answer) > 3 else None
        return ChatResponse(
            content=content,
            model=answer[4] if len(answer) > 4 else "m",
            stop_reason=stop_reason,
            tool_calls=tool_calls,
            usage=dict(usage or {}),
        )

    async def chat(self, messages=None, **kwargs) -> ChatResponse:
        self.calls.append({"messages": messages, **kwargs})
        return self._next()

    async def chat_stream(self, messages=None, **kwargs):
        self.calls.append({"messages": messages, "stream": True, **kwargs})
        response = self._next()
        self.last_stop_reason = response.stop_reason
        yield response.content

    @property
    def budgets(self) -> list[int]:
        return [call["max_tokens"] for call in self.calls]


# ---------------------------------------------------------------------------
# Mandatory-transfer routing
# ---------------------------------------------------------------------------


def _agent(name: str) -> SimpleNamespace:
    return SimpleNamespace(id=uuid.uuid4(), name=name)


async def _route(monkeypatch, llm: _Script):
    async def _settings(_db):
        return SimpleNamespace(routing_model=None, routing_provider_id=None)

    async def _llm_for_user(*_args, **_kwargs):
        return llm, None

    monkeypatch.setattr(
        "app.api.v1.platform_settings_helpers.get_platform_settings", _settings
    )
    monkeypatch.setattr("app.llm.router.get_llm_for_user", _llm_for_user)
    from app.services.routing_evaluator import evaluate_routing_conditions

    first, second, fallback = _agent("First"), _agent("Second"), _agent("Fallback")
    chosen = await evaluate_routing_conditions(
        None,
        agent_output="The customer wants a refund.",
        mandatory_targets=[first, second, fallback],
        conditions={str(first.id): "billing", str(second.id): "refunds"},
    )
    return chosen, (first, second, fallback)


async def test_routing_retries_a_cut_answer(monkeypatch):
    llm = _Script(("", CUT), ("2", DONE))

    chosen, (_first, second, _fallback) = await _route(monkeypatch, llm)

    assert chosen is second
    assert llm.budgets == [10, 20]


async def test_routing_cut_twice_is_read_as_naming_no_condition(monkeypatch):
    """Cut before naming a condition, twice: the fallback target, main's
    reading of an answer that names none."""
    llm = _Script(("", CUT), ("The output is about", CUT))

    chosen, (_first, _second, fallback) = await _route(monkeypatch, llm)

    assert chosen is fallback
    assert llm.budgets == [10, 20]


async def test_routing_reads_a_cut_answer_that_named_its_condition(monkeypatch):
    """The number comes first: a cut in the explanation after it changes
    nothing, so it is read as it always was - no retry, no fallback."""
    llm = _Script(("2 - the customer asks for a", CUT))

    chosen, (_first, second, _fallback) = await _route(monkeypatch, llm)

    assert chosen is second
    assert llm.budgets == [10]


async def test_routing_turns_thinking_off(monkeypatch):
    llm = _Script(("", CUT), ("2", DONE))

    await _route(monkeypatch, llm)

    assert [call.get("disable_thinking") for call in llm.calls] == [True, True]


# ---------------------------------------------------------------------------
# Flow LLM conditions
# ---------------------------------------------------------------------------


def _patch_flow_llm(monkeypatch, llm: _Script) -> None:
    async def _routing(_db):
        return None, None

    async def _resolve(*_args, **_kwargs):
        return llm, None

    monkeypatch.setattr("app.services.flow_condition_llm.routing_model", _routing)
    monkeypatch.setattr("app.services.flow_condition_llm.resolve_llm", _resolve)


async def _question(monkeypatch, llm: _Script, llm_config: dict | None = None):
    _patch_flow_llm(monkeypatch, llm)
    from app.services.flow_condition_llm import evaluate_question

    return await evaluate_question(
        None, question="Is the invoice overdue?", llm_config=llm_config
    )


async def _cases(monkeypatch, llm: _Script):
    _patch_flow_llm(monkeypatch, llm)
    from app.services.flow_condition_llm import evaluate_cases

    return await evaluate_cases(
        None,
        value="Invoice 42 is 30 days late.",
        cases={"paid": "the invoice is settled", "overdue": "the invoice is late"},
    )


async def test_flow_condition_retries_a_cut_answer(monkeypatch):
    llm = _Script(("", CUT), ("yes", DONE))

    verdict, _reason = await _question(monkeypatch, llm)

    assert verdict is True
    assert llm.budgets == [10, 20]


async def test_flow_condition_cut_twice_is_read_as_an_unreadable_answer(monkeypatch):
    """Cut before a verdict, twice: the False branch with main's own reason for
    an answer without one (the model WAS reached)."""
    llm = _Script(("Based on the", CUT), ("Based on the data", CUT))

    verdict, reason = await _question(monkeypatch, llm)

    assert verdict is False
    assert reason.startswith("Unreadable answer")
    assert "Based on the data" in reason
    assert "could not be reached" not in reason


async def test_flow_condition_reads_a_verdict_cut_after_it(monkeypatch):
    """The verdict comes first: a cut in the explanation after it is read as
    it always was - no retry, and not the False branch."""
    llm = _Script(("yes, the invoice is thirty days", CUT))

    verdict, _reason = await _question(monkeypatch, llm)

    assert verdict is True
    assert llm.budgets == [10]


async def test_flow_cases_read_an_option_cut_after_it(monkeypatch):
    llm = _Script(("2 - it is thirty days", CUT))

    case, _reason = await _cases(monkeypatch, llm)

    assert case == "overdue"
    assert llm.budgets == [10]


async def test_flow_cases_cut_twice_take_the_default_branch(monkeypatch):
    llm = _Script(("", CUT), ("The value", CUT))

    case, reason = await _cases(monkeypatch, llm)

    assert case is None
    assert reason.startswith("No case matched")
    assert "The value" in reason
    assert llm.budgets == [10, 20]


async def test_flow_condition_turns_thinking_off_unless_the_node_asks_for_effort(
    monkeypatch,
):
    plain = _Script(("yes", DONE))
    await _question(monkeypatch, plain)
    assert plain.calls[0].get("disable_thinking") is True

    effort = _Script(("yes", DONE))
    await _question(monkeypatch, effort, llm_config={"reasoning_effort": "high"})
    assert "disable_thinking" not in effort.calls[0]
    assert effort.calls[0]["reasoning_effort"] == "high"


# ---------------------------------------------------------------------------
# Flow tool selector
# ---------------------------------------------------------------------------


def _search_tool() -> ToolDefinition:
    return ToolDefinition(
        name="search",
        description="Search",
        input_schema={"type": "object", "properties": {"query": {"type": "string"}}},
    )


async def _select(monkeypatch, llm: _Script):
    async def _routing(_db):
        return None, None

    async def _resolve(*_args, **_kwargs):
        return llm, None

    monkeypatch.setattr("app.services.flow_tool_selector.routing_model", _routing)
    monkeypatch.setattr("app.services.flow_tool_selector.resolve_llm", _resolve)
    from app.services.flow_tool_selector import select_tool

    return await select_tool(
        None, objective="Find the Q3 report", definitions=[_search_tool()]
    )


def _call(arguments: dict) -> list[ToolCall]:
    return [ToolCall(id="c1", name="search", arguments=arguments)]


async def test_tool_selector_retries_a_cut_answer(monkeypatch):
    llm = _Script(
        ("", CUT, _call({"raw": '{"query": "Q3 rep'})),
        ("", DONE, _call({"query": "Q3 report"})),
    )

    selection = await _select(monkeypatch, llm)

    assert selection.ok
    assert selection.arguments == {"query": "Q3 report"}
    assert llm.budgets == [2048, 4096]
    # `attempts` counts model calls: the retry is one.
    assert selection.attempts == 2


async def test_tool_selector_cut_twice_takes_the_error_edge(monkeypatch):
    llm = _Script(
        ("", CUT, _call({"raw": '{"query": "Q3 rep'})),
        ("", CUT, _call({"raw": '{"query": "Q3 report for'})),
    )

    selection = await _select(monkeypatch, llm)

    assert not selection.ok
    assert selection.tool_name is None
    assert selection.error and "max_tokens" in selection.error
    assert selection.attempts == 2


def _required_query_tool() -> ToolDefinition:
    return ToolDefinition(
        name="search",
        description="Search",
        input_schema={
            "type": "object",
            "properties": {"query": {"type": "string"}},
            "required": ["query"],
        },
    )


async def test_tool_selector_correction_starts_from_the_budget_that_completed(
    monkeypatch,
):
    """Attempt 1 is cut and completed by its retry, but misses an argument:
    the correction asks with the retry's budget, not the one that was cut."""
    llm = _Script(
        ("", CUT, _call({"raw": '{"q'})),
        ("", DONE, _call({})),
        ("", DONE, _call({"query": "Q3 report"})),
    )

    async def _routing(_db):
        return None, None

    async def _resolve(*_args, **_kwargs):
        return llm, None

    monkeypatch.setattr("app.services.flow_tool_selector.routing_model", _routing)
    monkeypatch.setattr("app.services.flow_tool_selector.resolve_llm", _resolve)
    from app.services.flow_tool_selector import select_tool

    selection = await select_tool(
        None, objective="Find the Q3 report", definitions=[_required_query_tool()]
    )

    assert selection.ok
    assert llm.budgets == [2048, 4096, 4096]
    assert selection.attempts == 3


# ---------------------------------------------------------------------------
# In-run semantic compaction summary
# ---------------------------------------------------------------------------


async def _summarize(llm: _Script) -> str:
    from app.tools.agent_loop import _summarize_messages_for_context

    middle = [
        ChatMessage(role="user", content="Pull the Q3 revenue by subregion."),
        ChatMessage(role="assistant", content="EMEA 12.4M, APAC 8.1M, AMER 20.3M."),
    ]
    return await _summarize_messages_for_context(llm, middle, model=None)


async def test_in_run_summary_retries_a_cut_answer():
    llm = _Script(("- Revenue by subreg", CUT), ("- Revenue by subregion pulled", DONE))

    summary = await _summarize(llm)

    assert summary == "- Revenue by subregion pulled"
    from app.tools.agent_loop import _SEMANTIC_COMPACTION_MAX_TOKENS

    assert llm.budgets == [
        _SEMANTIC_COMPACTION_MAX_TOKENS,
        2 * _SEMANTIC_COMPACTION_MAX_TOKENS,
    ]


async def test_in_run_summary_cut_twice_falls_back_to_the_pruner():
    llm = _Script(("- Revenue by subreg", CUT), ("- Revenue by subregion, EM", CUT))

    assert await _summarize(llm) == ""


async def test_in_run_summary_records_the_retry_as_a_call_of_its_own():
    """A cut summary and its retry are two billed calls: two generations with
    their own usage (the cut one a WARNING, the retry flagged) and two usage
    records for the run - never one, never three - each under the model its
    own response names (a retry served by another model is priced as
    that model)."""
    from app.services import langfuse_tracing as lft
    from app.tools.agent_loop import _summarize_messages_for_context
    from tests.test_compaction_summariser_usage import _compaction_spans, _root_trace_ctx

    cut_usage = {"input_tokens": 900, "output_tokens": 1536}
    full_usage = {"input_tokens": 900, "output_tokens": 700}
    llm = _Script(
        ("- Revenue by subreg", CUT, None, cut_usage, "model-a"),
        ("- Revenue by subregion pulled", DONE, None, full_usage, "model-b"),
    )
    merged: list[tuple[dict, str | None]] = []
    root = _root_trace_ctx()
    token = lft.set_current_trace_ctx(root)
    try:
        summary = await _summarize_messages_for_context(
            llm,
            [
                ChatMessage(role="user", content="Pull the Q3 revenue by subregion."),
                ChatMessage(role="assistant", content="EMEA 12.4M, APAC 8.1M."),
            ],
            model="run-model",
            merge_usage=lambda usage, served_model=None: merged.append(
                (usage, served_model)
            ),
        )
    finally:
        lft.reset_current_trace_ctx(token)

    assert summary == "- Revenue by subregion pulled"
    assert merged == [(cut_usage, "model-a"), (full_usage, "model-b")]
    first, retry = _compaction_spans(root)
    assert (first["input_tokens"], first["output_tokens"]) == (900, 1536)
    assert first["level"] == "WARNING"
    assert "cut_answer_retry" not in first["metadata"]
    assert (retry["input_tokens"], retry["output_tokens"]) == (900, 700)
    assert retry["level"] == "DEFAULT"
    assert retry["metadata"]["cut_answer_retry"] is True


async def test_no_retry_when_the_model_ceiling_leaves_no_room():
    """At the model's output ceiling the retry would be the same request: the
    cut answer takes the failed-call path at once."""
    from app.tools.agent_loop import _SEMANTIC_COMPACTION_MAX_TOKENS

    llm = _Script(("- Revenue by subreg", CUT))
    llm.max_output_tokens = _SEMANTIC_COMPACTION_MAX_TOKENS

    assert await _summarize(llm) == ""
    assert llm.budgets == [_SEMANTIC_COMPACTION_MAX_TOKENS]


@pytest.mark.parametrize("allowed", [True, False], ids=["affordable", "unaffordable"])
async def test_in_run_summary_asks_the_budget_before_its_retry(allowed):
    """The summariser takes no reservation: before its retry it asks the loop's
    check with the retry's estimated prompt and doubled budget. A refusal is the
    pruner - no second request, no second generation - as a summary still cut."""
    from app.tools.agent_loop import (
        _SEMANTIC_COMPACTION_MAX_TOKENS,
        _estimate_prompt_tokens,
        _summarize_messages_for_context,
    )

    asked: list[tuple[int, int]] = []

    def _check(input_tokens: int, max_output_tokens: int) -> bool:
        asked.append((input_tokens, max_output_tokens))
        return allowed

    llm = _Script(("- Revenue by subreg", CUT), ("- Revenue by subregion pulled", DONE))

    summary = await _summarize_messages_for_context(
        llm,
        [ChatMessage(role="user", content="Pull the Q3 revenue by subregion.")],
        model=None,
        can_retry=_check,
    )

    doubled = 2 * _SEMANTIC_COMPACTION_MAX_TOKENS
    assert asked == [(_estimate_prompt_tokens(llm.calls[0]["messages"]), doubled)]
    if allowed:
        assert summary == "- Revenue by subregion pulled"
        assert llm.budgets == [_SEMANTIC_COMPACTION_MAX_TOKENS, doubled]
    else:
        assert summary == ""
        assert llm.budgets == [_SEMANTIC_COMPACTION_MAX_TOKENS]


async def test_in_run_summary_without_a_cut_asks_nothing():
    """Main's path: one request, the budget check never consulted."""
    from app.tools.agent_loop import _summarize_messages_for_context

    def _check(*_args) -> bool:
        raise AssertionError("a complete summary is never retried")

    llm = _Script(("- Revenue by subregion pulled", DONE))

    summary = await _summarize_messages_for_context(
        llm,
        [ChatMessage(role="user", content="Pull the Q3 revenue by subregion.")],
        model=None,
        can_retry=_check,
    )

    assert summary == "- Revenue by subregion pulled"
    assert len(llm.calls) == 1


_CUT_SUMMARY_USAGE = {"input_tokens": 7_000, "output_tokens": 1_536}
_FULL_SUMMARY_USAGE = {"input_tokens": 7_000, "output_tokens": 900}


def _cut_summary() -> ChatResponse:
    return ChatResponse(
        content="Prior work: the failed deploy of build 4.2",
        model="stub",
        stop_reason=CUT,
        usage=dict(_CUT_SUMMARY_USAGE),
    )


def _summary_budget(*, refuse_retries: bool, **limits):
    """A real run budget recording its ``can_afford`` questions; with
    *refuse_retries* it cannot pay for a summary's retry (asked with at least
    the doubled summary budget as its output)."""
    from app.tools.agent_loop import _SEMANTIC_COMPACTION_MAX_TOKENS
    from app.tools.run_budget import BudgetDimension, BudgetViolation
    from tests.test_compaction_summariser_usage import _RecordingBudget

    class _Budget(_RecordingBudget):
        def __init__(self) -> None:
            super().__init__(**limits)
            self.retry_checks: list[dict] = []

        def can_afford(self, *, estimated_input_tokens, max_output_tokens, final=False):
            if max_output_tokens >= 2 * _SEMANTIC_COMPACTION_MAX_TOKENS:
                self.retry_checks.append(
                    {"input": estimated_input_tokens, "output": max_output_tokens, "final": final}
                )
                if refuse_retries:
                    return BudgetViolation(BudgetDimension.TOKENS, 0, 1, 1, 1)
            return super().can_afford(
                estimated_input_tokens=estimated_input_tokens,
                max_output_tokens=max_output_tokens,
                final=final,
            )

    return _Budget()


@pytest.mark.parametrize("refuse_retries", [False, True], ids=["affordable", "unaffordable"])
async def test_the_loop_retries_a_cut_summary_only_when_its_budget_pays(refuse_retries):
    """In the loop the iteration's reservation holds the call the compaction
    precedes, so the retry is priced alone on top of it, at the in-loop gate."""
    from app.tools.agent_loop import _SEMANTIC_COMPACTION_MAX_TOKENS
    from tests.test_compaction_summariser_usage import (
        _ANSWER,
        _LONG,
        _answer,
        _compacted,
        _history,
        _root_trace_ctx,
        _run,
        _ScriptedLLM,
        _summary,
    )

    llm = _ScriptedLLM(
        calls=[_answer(_LONG, _ANSWER)],
        summaries=[_cut_summary(), _summary(_FULL_SUMMARY_USAGE)],
    )
    budget = _summary_budget(refuse_retries=refuse_retries)
    messages = _history(turns=20)

    response = await _run(llm, budget, _root_trace_ctx(), messages)

    assert response.content.strip() == _LONG.strip()
    (check,) = budget.retry_checks
    assert (check["output"], check["final"]) == (2 * _SEMANTIC_COMPACTION_MAX_TOKENS, False)
    if refuse_retries:
        # One summariser call, as main made: the cut summary takes the pruner.
        assert llm.phases == ["compaction", "answer"]
        assert budget.debits == [_CUT_SUMMARY_USAGE, _ANSWER]
        assert not _compacted(messages)
    else:
        assert llm.phases == ["compaction", "compaction", "answer"]
        assert budget.debits == [_CUT_SUMMARY_USAGE, _FULL_SUMMARY_USAGE, _ANSWER]
        assert _compacted(messages)


@pytest.mark.parametrize("refuse_retries", [False, True], ids=["affordable", "unaffordable"])
async def test_the_backstop_prices_a_summary_retry_with_the_synthesis(refuse_retries):
    """In the backstop no reservation holds the synthesis the compaction
    precedes: the retry is priced with it, at the full limit it is gated at."""
    from app.tools.agent_loop import _SEMANTIC_COMPACTION_MAX_TOKENS
    from tests.test_compaction_summariser_usage import (
        _LONG,
        _RETRY,
        _SHORT,
        _SYNTHESIS,
        _answer,
        _history,
        _root_trace_ctx,
        _run,
        _ScriptedLLM,
        _summary,
        _tool_response,
    )

    llm = _ScriptedLLM(
        calls=[_tool_response(), _answer(_SHORT, _SYNTHESIS), _answer(_LONG, _RETRY)],
        summaries=[_cut_summary(), _summary(_FULL_SUMMARY_USAGE)],
    )
    budget = _summary_budget(refuse_retries=refuse_retries, max_tool_calls=1)

    response = await _run(
        llm,
        budget,
        _root_trace_ctx(),
        _history(turns=22),
        with_tools=True,
        max_session_chars=None,
    )

    assert response.content.strip() == _LONG.strip()
    (check,) = budget.retry_checks
    # The loop's own max_tokens (1_024 in ``_run``) is the synthesis's budget.
    assert (check["output"], check["final"]) == (2 * _SEMANTIC_COMPACTION_MAX_TOKENS + 1_024, True)
    compactions = ["compaction"] if refuse_retries else ["compaction", "compaction"]
    assert llm.phases == ["tools", "synthesis", *compactions, "retry"]


# ---------------------------------------------------------------------------
# Email style learning
# ---------------------------------------------------------------------------


async def _learn(monkeypatch, llm: _Script) -> tuple[str, list[str]]:
    stored: list[str] = []

    async def _store(**kwargs):
        stored.append(kwargs["content"])

    async def _embed(_text, model=None):
        return [0.1, 0.2]

    monkeypatch.setattr("app.services.learning.store_embedding", _store)
    from app.services.learning import learn_email_style

    guide = await learn_email_style(
        None,
        llm,
        SimpleNamespace(embed=_embed, provider_name="e"),
        "user-1",
        emails=[{"to": "a@b.c", "subject": "Hi", "body": "Hello, thanks!"}],
    )
    return guide, stored


async def test_style_learning_retries_a_cut_answer(monkeypatch):
    llm = _Script(("- Warm, short", CUT), ("- Warm, short sentences", DONE))

    guide, stored = await _learn(monkeypatch, llm)

    assert guide == "- Warm, short sentences"
    assert stored == ["- Warm, short sentences"]
    assert llm.budgets == [4096, 8192]


async def test_style_learning_cut_twice_stores_nothing(monkeypatch):
    """No style guide is stored, not even a placeholder: the analysis fails
    as it does when its provider call fails."""
    from app.services import learning

    stored: list[str] = []

    async def _store(**kwargs):
        stored.append(kwargs["content"])

    async def _embed(_text, model=None):
        return [0.1, 0.2]

    monkeypatch.setattr(learning, "store_embedding", _store)
    llm = _Script(("- Warm, short", CUT), ("- Warm, short sent", CUT))

    with pytest.raises(RuntimeError, match="max_tokens"):
        await learning.learn_email_style(
            None,
            llm,
            SimpleNamespace(embed=_embed, provider_name="e"),
            "user-1",
            emails=[{"to": "a@b.c", "subject": "Hi", "body": "Hello, thanks!"}],
        )
    assert stored == []


# ---------------------------------------------------------------------------
# Conversation titles
# ---------------------------------------------------------------------------


async def _title(llm) -> str | None:
    from app.services.conversation_memory import generate_conversation_title

    return await generate_conversation_title(
        llm, "Show me Q3 revenue by subregion", "Here is the breakdown."
    )


#: The title answer is a ``{"title": ...}`` object; a cut one stops mid-string.
_CUT_TITLE_ANSWER = '{"title": "Quarterly revenue'
_FULL_TITLE_ANSWER = '{"title": "Quarterly revenue by subregion"}'


async def test_title_retries_a_cut_answer():
    llm = _Script((_CUT_TITLE_ANSWER, CUT), (_FULL_TITLE_ANSWER, DONE))

    assert await _title(llm) == "Quarterly revenue by subregion"
    assert llm.budgets == [40, 80]


async def test_title_cut_twice_keeps_the_fallback_title():
    llm = _Script((_CUT_TITLE_ANSWER, CUT), ('{"title": "Quarterly revenue by', CUT))

    assert await _title(llm) is None


class _SlowScript(_Script):
    """Answers after *delays[i]* seconds (the provider's latency per call)."""

    def __init__(self, delays: list[float], *answers: tuple) -> None:
        super().__init__(*answers)
        self._delays = list(delays)
        self.started: list[float] = []

    async def chat(self, messages=None, **kwargs) -> ChatResponse:
        self.calls.append({"messages": messages, **kwargs})
        self.started.append(asyncio.get_running_loop().time())
        await asyncio.sleep(self._delays.pop(0))
        return self._next()


async def test_a_title_call_times_out_inside_its_generation_as_on_main(monkeypatch):
    """Main's cap, where main had it: the call's own ``wait_for`` inside its
    generation, which records the ``TimeoutError`` (not a cancellation)."""
    import contextlib

    from app.services import conversation_memory

    errors: list[BaseException | None] = []

    @contextlib.asynccontextmanager
    async def _generation(**_kwargs):
        handle = SimpleNamespace(response=None)
        try:
            yield handle
        except BaseException as exc:
            errors.append(exc)
            raise
        errors.append(None)

    monkeypatch.setattr(conversation_memory, "traced_generation", _generation)
    monkeypatch.setattr(conversation_memory, "_TITLE_TIMEOUT_SECONDS", 0.05)
    llm = _SlowScript([5.0], (_FULL_TITLE_ANSWER, DONE))

    assert await _title(llm) is None
    assert len(errors) == 1 and isinstance(errors[0], TimeoutError)


async def test_a_cut_title_is_retried_within_the_one_deadline(monkeypatch):
    """The retry gets what is left of the cap, never a cap of its own."""
    from app.services import conversation_memory

    # The deadline is counted on a fake clock the script advances, so a
    # loaded worker never spends the budget before the retry is made.
    now = [1000.0]
    monkeypatch.setattr(conversation_memory, "_title_clock", lambda: now[0])
    monkeypatch.setattr(conversation_memory, "_TITLE_TIMEOUT_SECONDS", 0.3)

    class _FakeClockScript(_SlowScript):
        """The first call takes 0.2 s of the fake clock; the retry hangs."""

        async def chat(self, messages=None, **kwargs) -> ChatResponse:
            if not self.calls:
                self.calls.append({"messages": messages, **kwargs})
                self.started.append(asyncio.get_running_loop().time())
                now[0] += 0.2
                return self._next()
            return await super().chat(messages=messages, **kwargs)

    llm = _FakeClockScript([5.0], (_CUT_TITLE_ANSWER, CUT), (_FULL_TITLE_ANSWER, DONE))

    assert await _title(llm) is None

    assert llm.budgets == [40, 80]
    # The retry got what was left of the cap (0.1 s), never a cap of its own
    # nor its 5 s answer time.
    assert asyncio.get_running_loop().time() - llm.started[1] < 1.0


# ---------------------------------------------------------------------------
# "Generate with AI" prompt generator
# ---------------------------------------------------------------------------


async def _generate_prompt(monkeypatch, llm: _Script):
    from app.api.v1 import ai

    async def _llm_for_user(*_args, **_kwargs):
        return llm, None

    monkeypatch.setattr(ai, "get_llm_for_user", _llm_for_user)
    body = ai.GeneratePromptRequest(
        description="A support persona", context_type="persona"
    )
    return await ai.generate_prompt(
        body, user=SimpleNamespace(id=uuid.uuid4()), db=None, _ai_gate=None
    )


async def test_prompt_generator_retries_a_cut_answer(monkeypatch):
    llm = _Script(("You are a support", CUT), ("You are a support agent.", DONE))

    response = await _generate_prompt(monkeypatch, llm)

    assert response.generated_prompt == "You are a support agent."
    assert response.truncated is False
    assert llm.budgets == [2048, 4096]


def _ai_client(monkeypatch, llm):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from app.api import deps
    from app.api.v1 import ai
    from app.core.database import get_db

    async def _llm_for_user(*_args, **_kwargs):
        return llm, None

    app = FastAPI()
    app.include_router(ai.router, prefix="/api/v1/ai")
    app.dependency_overrides[deps.get_current_user] = lambda: SimpleNamespace(id=uuid.uuid4())
    app.dependency_overrides[deps.require_ai_enabled] = lambda: None
    app.dependency_overrides[get_db] = lambda: None
    monkeypatch.setattr(ai, "get_llm_for_user", _llm_for_user)
    return TestClient(app, raise_server_exceptions=False)


def test_prompt_generator_route_returns_a_reply_still_cut_as_generated(monkeypatch):
    """What main returned for a reply cut at its budget - 200 and the text as
    generated, for the person to finish in the editor - after the one retry,
    flagged ``truncated`` (never a 500)."""
    llm = _Script(("You are a support", CUT), ("You are a support ag", CUT))

    resp = _ai_client(monkeypatch, llm).post(
        "/api/v1/ai/generate-prompt",
        json={"description": "A support persona", "context_type": "persona"},
    )

    assert resp.status_code == 200, resp.text
    assert resp.json() == {"generated_prompt": "You are a support ag", "truncated": True}
    assert llm.budgets == [2048, 4096]


def test_prompt_generator_route_is_main_s_single_call_without_a_cut(monkeypatch):
    llm = _Script(("You are a support agent.", DONE))

    resp = _ai_client(monkeypatch, llm).post(
        "/api/v1/ai/generate-prompt",
        json={"description": "A support persona", "context_type": "persona"},
    )

    assert resp.status_code == 200, resp.text
    assert resp.json() == {"generated_prompt": "You are a support agent.", "truncated": False}
    (call,) = llm.calls
    assert (call["model"], call["temperature"], call["max_tokens"]) == (None, 0.7, 2048)


# ---------------------------------------------------------------------------
# Suggested widget prompts
# ---------------------------------------------------------------------------


async def _suggest(monkeypatch, llm: _Script) -> dict:
    from app.api.v1 import agents

    async def _groups(*_args, **_kwargs):
        return []

    async def _agent_lookup(*_args, **_kwargs):
        return SimpleNamespace(
            persona="A billing assistant",
            description=None,
            name="Billing",
            llm_provider=None,
            llm_model=None,
            llm_provider_id=None,
        )

    async def _llm_for_user(*_args, **_kwargs):
        return llm, None

    monkeypatch.setattr(agents, "get_user_group_ids", _groups)
    monkeypatch.setattr(agents, "get_user_agent", _agent_lookup)
    monkeypatch.setattr("app.llm.router.get_llm_for_user", _llm_for_user)
    return await agents.generate_suggested_prompts(
        "agent-1",
        user=SimpleNamespace(id=uuid.uuid4(), is_admin=False),
        db=None,
        _ai_gate=None,
    )


async def test_suggested_prompts_retry_a_cut_answer(monkeypatch):
    full = '["How do I pay?", "Where is my invoice?"]'
    llm = _Script(('["How do I pay?", "Where is', CUT), (full, DONE))

    result = await _suggest(monkeypatch, llm)

    assert result == {"suggested_prompts": ["How do I pay?", "Where is my invoice?"]}
    assert llm.budgets == [512, 1024]


async def test_suggested_prompts_cut_twice_are_empty(monkeypatch):
    llm = _Script(('["How do I pay?"', CUT), ('["How do I pay?", "Wh', CUT))

    assert await _suggest(monkeypatch, llm) == {"suggested_prompts": []}


# ---------------------------------------------------------------------------
# Template generation
# ---------------------------------------------------------------------------


async def _template(llm: _Script):
    from app.services import template_generation as gen

    return await gen.generate_template_candidate(
        llm,
        None,
        brief="Corporate navy deck",
        kind="presentation",
        palette_hints=[],
        logo_palette=[],
        current=None,
        fallback_name=None,
    )


async def test_template_generation_retries_a_cut_answer():
    from tests.test_template_generation import _proposal

    full = json.dumps(_proposal())
    llm = _Script((full[: len(full) // 2], CUT), (full, DONE))

    candidate, _warnings = await _template(llm)

    assert candidate["name"] == "Corporate Navy"
    assert llm.budgets == [4096, 8192]


async def test_template_generation_cut_twice_is_a_generation_error():
    from app.services.template_generation import TemplateGenerationError
    from tests.test_template_generation import _proposal

    full = json.dumps(_proposal())
    llm = _Script((full[:40], CUT), (full[:80], CUT))

    # The generator's existing failure, in its existing words.
    with pytest.raises(
        TemplateGenerationError, match=r"^The model did not return a valid template"
    ):
        await _template(llm)


# ---------------------------------------------------------------------------
# Presentation comment drafts
# ---------------------------------------------------------------------------


async def _draft(llm: _Script):
    from app.services.presentation_comment_drafts import draft_comment

    return await draft_comment(
        llm,
        None,
        spec=None,
        deck_title="Q3 review",
        slide_id=None,
        brief="Ask for the source of the EMEA figure",
    )


async def test_comment_draft_retries_a_cut_answer():
    llm = _Script(("Where does the EMEA", CUT), ("Where does the EMEA figure come from?", DONE))

    draft = await _draft(llm)

    assert (draft.body, draft.truncated) == ("Where does the EMEA figure come from?", False)
    from app.services.presentation_comment_drafts import DRAFT_MAX_TOKENS

    assert llm.budgets == [DRAFT_MAX_TOKENS, 2 * DRAFT_MAX_TOKENS]


async def test_comment_draft_cut_twice_is_returned_as_drafted():
    llm = _Script(("Where does the EMEA", CUT), ('"Where does the EMEA fig', CUT))

    draft = await _draft(llm)

    # Cleaned as any draft is (a leading quote with no pair stays), flagged.
    assert (draft.body, draft.truncated) == ('"Where does the EMEA fig', True)


async def test_comment_draft_cut_to_nothing_is_still_the_empty_note_error():
    from app.services.presentation_comment_drafts import CommentDraftError

    llm = _Script(("   ", CUT), ("  ", CUT))

    with pytest.raises(CommentDraftError, match="empty note"):
        await _draft(llm)


def _draft_client(monkeypatch, llm):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from app.api import deps
    from app.api.v1 import presentations
    from app.core.database import get_db

    deck = SimpleNamespace(id=uuid.uuid4(), spec={"slides": []}, title="Q3 review")

    async def _visible(_db, _user, _presentation_id):
        return deck, [], "read_only"

    async def _llm_for_user(*_args, **_kwargs):
        return llm, None

    app = FastAPI()
    app.include_router(presentations.router, prefix="/api/v1/presentations")
    app.dependency_overrides[deps.get_current_user] = lambda: SimpleNamespace(id=uuid.uuid4())
    app.dependency_overrides[deps.require_ai_enabled] = lambda: None
    app.dependency_overrides[get_db] = lambda: None
    monkeypatch.setattr(presentations, "_load_visible", _visible)
    monkeypatch.setattr(presentations, "get_llm_for_user", _llm_for_user)
    return TestClient(app, raise_server_exceptions=False)


def test_comment_draft_route_returns_a_note_still_cut_as_drafted(monkeypatch):
    """What main returned for a note cut at its budget - 200 and the note -
    after the one retry, flagged ``truncated`` (never a 502)."""
    llm = _Script(("Where does the EMEA", CUT), ("Where does the EMEA fig", CUT))

    resp = _draft_client(monkeypatch, llm).post(
        f"/api/v1/presentations/{uuid.uuid4()}/comments/draft",
        json={"brief": "Ask for the source of the EMEA figure"},
    )

    assert resp.status_code == 200, resp.text
    assert resp.json() == {"body": "Where does the EMEA fig", "truncated": True}


def test_comment_draft_route_is_main_s_single_call_without_a_cut(monkeypatch):
    from app.services.presentation_comment_drafts import DRAFT_MAX_TOKENS

    llm = _Script(("Where does the EMEA figure come from?", DONE))

    resp = _draft_client(monkeypatch, llm).post(
        f"/api/v1/presentations/{uuid.uuid4()}/comments/draft",
        json={"brief": "Ask for the source of the EMEA figure"},
    )

    assert resp.status_code == 200, resp.text
    assert resp.json() == {"body": "Where does the EMEA figure come from?", "truncated": False}
    (call,) = llm.calls
    assert (call["temperature"], call["max_tokens"]) == (0.7, DRAFT_MAX_TOKENS)


# ---------------------------------------------------------------------------
# Workset automatic summaries
# ---------------------------------------------------------------------------


_WORKSET_ID = uuid.uuid4()


async def _workset(monkeypatch, llm: _Script, *, reserve=None) -> tuple[Any, list]:
    """The pass's call step. The first call was reserved by the prepare step;
    *reserve* stands in for the retry's own reservation."""
    from app.services import workset_processing
    from app.services.flow_llm import LlmSettings

    reserved: list = []

    async def _reserve(workset_id):
        reserved.append(workset_id)
        return None

    monkeypatch.setattr(workset_processing, "_charge_retry", reserve or _reserve)
    prepared = workset_processing._Prepared(
        llm=llm,
        model=None,
        settings=LlmSettings.from_node_config(None),
        workset_id=_WORKSET_ID,
    )
    verdict = await workset_processing._summarise(
        prepared,
        title="Q3 review",
        content="Revenue grew 12% in EMEA.",
        truncated=False,
    )
    return verdict, reserved


async def test_workset_summary_retries_a_cut_answer(monkeypatch):
    full = json.dumps({"summary": "EMEA revenue grew 12%.", "priority": 3, "reason": "growth"})
    llm = _Script((full[:20], CUT), (full, DONE))

    verdict, reserved = await _workset(monkeypatch, llm)

    assert verdict.error is None
    assert verdict.summary == "EMEA revenue grew 12%."
    assert len(llm.calls) == 2
    assert llm.budgets[1] == 2 * llm.budgets[0]
    # The retry is a call like any other: reserved against the same workset.
    assert reserved == [_WORKSET_ID]


async def test_workset_summary_cut_twice_fails_the_item(monkeypatch):
    full = json.dumps({"summary": "EMEA revenue grew 12%.", "priority": 3})
    llm = _Script((full[:20], CUT), (full[:30], CUT))

    verdict, _reserved = await _workset(monkeypatch, llm)

    assert not verdict.summary
    assert verdict.error and "max_tokens" in verdict.error
    assert verdict.transient is False


async def test_workset_retry_refused_by_the_ceiling_is_the_refusal(monkeypatch):
    """A spent ceiling refuses the retry before it is made: the verdict the
    prepare step gives a refused call, and no second request."""
    full = json.dumps({"summary": "EMEA revenue grew 12%.", "priority": 3})
    llm = _Script((full[:20], CUT))

    async def _spent(_workset_id):
        return "This workset's automatic summarisation budget is spent (1/1 calls)."

    verdict, _reserved = await _workset(monkeypatch, llm, reserve=_spent)

    assert len(llm.calls) == 1
    assert not verdict.summary
    assert verdict.error and "budget is spent" in verdict.error
    assert verdict.transient is False


async def test_workset_retry_reservation_failure_reaches_the_rescue(monkeypatch):
    """The database failing to reserve the retry is not a model failure: it is
    raised to the pass's rescue, as a failed reservation in the prepare step is."""
    full = json.dumps({"summary": "EMEA revenue grew 12%.", "priority": 3})
    llm = _Script((full[:20], CUT))

    async def _reset(_workset_id):
        raise ConnectionError("the database connection was reset")

    with pytest.raises(ConnectionError):
        await _workset(monkeypatch, llm, reserve=_reset)
    assert len(llm.calls) == 1


# ---------------------------------------------------------------------------
# Evaluation, LLM-only mode
# ---------------------------------------------------------------------------


class _StreamDown(_Script):
    """Streaming is down: the evaluation falls back to non-streamed calls."""

    async def chat_stream(self, messages=None, **kwargs):
        raise ConnectionError("stream refused")
        yield  # pragma: no cover - makes this an async generator


async def _evaluate(monkeypatch, llm: _Script, ended: list | None = None) -> str:
    from app.services import evaluation_engine, langfuse_tracing

    async def _provider(_db, _model_id, **_kwargs):
        return llm, None

    async def _trace(*_args, **_kwargs):
        return {"trace": None}

    def _end(_ctx, **kwargs):
        if ended is not None:
            ended.append(kwargs)

    monkeypatch.setattr(evaluation_engine, "_get_llm_provider_and_model", _provider)
    monkeypatch.setattr(langfuse_tracing, "trace_llm_call", _trace)
    monkeypatch.setattr(langfuse_tracing, "end_trace", _end)
    monkeypatch.setattr(langfuse_tracing, "get_local_trace_id", lambda *_a, **_k: None)
    agent = SimpleNamespace(
        id=uuid.uuid4(), name="Analyst", slug="analyst", persona="p", temperature=None,
        output_format=None, output_schema=None,
    )
    output, _trace_ids, _ttft, _traces = await evaluation_engine._execute_agent_llm_only(
        None, agent, "m", "Summarise Q3"
    )
    return output


_CUT_USAGE = {"input_tokens": 300, "output_tokens": 4096}
_RETRY_USAGE = {"input_tokens": 300, "output_tokens": 5000}


async def test_evaluation_retries_a_cut_answer(monkeypatch):
    llm = _Script(("Q3 revenue grew in EM", CUT), ("Q3 revenue grew in EMEA.", DONE))

    assert await _evaluate(monkeypatch, llm) == "Q3 revenue grew in EMEA."
    assert llm.budgets == [4096, 8192]


async def test_evaluation_cut_twice_fails_the_case(monkeypatch):
    llm = _Script(("Q3 revenue grew in EM", CUT), ("Q3 revenue grew in EMEA and", CUT))

    with pytest.raises(RuntimeError, match="max_tokens"):
        await _evaluate(monkeypatch, llm)


async def test_evaluation_trace_carries_the_retry_of_a_cut_stream(monkeypatch):
    """A stream that reports no usage leaves the retry's alone on the trace;
    one that reports its usage adds it (tested below)."""
    ended: list[dict] = []
    llm = _Script(
        ("Q3 revenue grew in EM", CUT),
        ("Q3 revenue grew in EMEA.", DONE, None, _RETRY_USAGE),
    )

    await _evaluate(monkeypatch, llm, ended)

    (end,) = ended
    assert end["usage"] == _RETRY_USAGE
    assert end["output"] == "Q3 revenue grew in EMEA."


async def test_evaluation_trace_is_unchanged_without_a_cut(monkeypatch):
    ended: list[dict] = []

    await _evaluate(monkeypatch, _Script(("Q3 revenue grew in EMEA.", DONE)), ended)
    single = _StreamDown(("Q3 revenue grew in EMEA.", DONE, None, _RETRY_USAGE))
    await _evaluate(monkeypatch, single, ended)

    streamed, fallback = ended
    assert streamed["usage"] is None
    assert fallback["usage"] == _RETRY_USAGE


async def test_evaluation_trace_adds_up_both_requests_of_a_cut(monkeypatch):
    """Two non-streamed requests are two billed calls: the trace carries both."""
    ended: list[dict] = []
    llm = _StreamDown(
        ("Q3 revenue grew in EM", CUT, None, _CUT_USAGE),
        ("Q3 revenue grew in EMEA.", DONE, None, _RETRY_USAGE),
    )

    assert await _evaluate(monkeypatch, llm, ended) == "Q3 revenue grew in EMEA."

    (end,) = ended
    assert end["usage"] == {"input_tokens": 600, "output_tokens": 9096}


async def test_evaluation_cut_twice_still_records_what_was_billed(monkeypatch):
    ended: list[dict] = []
    llm = _StreamDown(
        ("Q3 revenue grew in EM", CUT, None, _CUT_USAGE),
        ("Q3 revenue grew in EMEA and", CUT, None, _RETRY_USAGE),
    )

    with pytest.raises(RuntimeError, match="max_tokens"):
        await _evaluate(monkeypatch, llm, ended)

    (end,) = ended
    assert "max_tokens" in end["error"]
    assert end["usage"] == {"input_tokens": 600, "output_tokens": 9096}


class _ChatFailsAfter(_Script):
    """``chat`` raises *error* once *answered* scripted replies were given."""

    def __init__(self, error: BaseException, *answers: tuple, answered: int = 0) -> None:
        super().__init__(*answers)
        self._error = error
        self._answered = answered

    async def chat(self, messages=None, **kwargs) -> ChatResponse:
        if sum(1 for call in self.calls if not call.get("stream")) >= self._answered:
            self.calls.append({"messages": messages, **kwargs})
            raise self._error
        return await super().chat(messages, **kwargs)


class _StreamDownChatFailsAfter(_ChatFailsAfter):
    async def chat_stream(self, messages=None, **kwargs):
        raise ConnectionError("stream refused")
        yield  # pragma: no cover - makes this an async generator


@pytest.mark.parametrize(
    "error", [ConnectionError("provider down"), asyncio.CancelledError()], ids=["error", "cancelled"]
)
async def test_evaluation_trace_is_closed_when_the_retry_of_a_cut_stream_fails(monkeypatch, error):
    """The cut stream was billed: a retry that fails or is cancelled closes the
    case's trace with the error before the error propagates."""
    from app.core.utils import format_exception_message

    ended: list[dict] = []
    llm = _ChatFailsAfter(error, ("Q3 revenue grew in EM", CUT))

    with pytest.raises(type(error)):
        await _evaluate(monkeypatch, llm, ended)

    (end,) = ended
    assert end["error"] == format_exception_message(error)
    assert llm.budgets == [4096, 8192]


async def test_evaluation_trace_is_closed_when_a_cut_answer_s_retry_is_cancelled(monkeypatch):
    ended: list[dict] = []
    llm = _StreamDownChatFailsAfter(
        asyncio.CancelledError(), ("Q3 revenue grew in EM", CUT, None, _CUT_USAGE), answered=1
    )

    with pytest.raises(asyncio.CancelledError):
        await _evaluate(monkeypatch, llm, ended)

    (end,) = ended
    assert end["error"].startswith("CancelledError")
    assert end["usage"] == _CUT_USAGE


async def test_evaluation_trace_is_left_as_main_leaves_it_when_the_first_call_fails(monkeypatch):
    """Nothing answered, nothing billed: the failure propagates with the trace
    untouched, exactly as on main."""
    ended: list[dict] = []
    llm = _StreamDownChatFailsAfter(ConnectionError("provider down"))

    with pytest.raises(ConnectionError):
        await _evaluate(monkeypatch, llm, ended)

    assert ended == []
    assert llm.budgets == [4096]


def test_usage_breakdowns_are_added_key_by_key():
    from app.services.evaluation_engine import _add_usage

    total: dict = {}
    _add_usage(total, {"prompt_tokens": 10, "prompt_tokens_details": {"cached_tokens": 4}})
    _add_usage(total, {"prompt_tokens": 7, "prompt_tokens_details": {"cached_tokens": 1}})

    assert total == {"prompt_tokens": 17, "prompt_tokens_details": {"cached_tokens": 5}}


# ---------------------------------------------------------------------------
# OpenAI-compatible LLM proxy: finish_reason
# ---------------------------------------------------------------------------


def _proxy_body(*, stream: bool) -> SimpleNamespace:
    return SimpleNamespace(
        stream=stream, temperature=None, reasoning_effort=None, top_p=None, stop=None,
        tools=None, tool_choice=None, model_extra=None,
    )


@pytest.fixture()
def _quiet_proxy(monkeypatch):
    from app.api.v1 import llm_proxy

    async def _track(*_args, **_kwargs):
        return None

    monkeypatch.setattr(llm_proxy, "_end_langfuse_trace", lambda *_a, **_k: None)
    monkeypatch.setattr(llm_proxy, "_track_model_usage", _track)
    return llm_proxy


async def _final_stream_chunk(response) -> dict:
    frames = [frame async for frame in response.body_iterator]
    payloads = [
        json.loads(frame[len("data: ") :])
        for frame in frames
        if frame.startswith("data: {")
    ]
    return payloads[-1]


@pytest.mark.parametrize(("stop_reason", "finish"), [(CUT, "length"), (DONE, "stop")])
async def test_proxy_provider_chat_reports_a_cut_as_length(
    _quiet_proxy, stop_reason, finish
):
    provider = _Script(("partial answer", stop_reason))

    response = await _quiet_proxy._proxy_via_provider_chat(
        provider, _proxy_body(stream=False), [{"role": "user", "content": "hi"}],
        "gemini-3-flash", 100, None, user_id="u1",
    )

    assert response["choices"][0]["finish_reason"] == finish


@pytest.mark.parametrize(("stop_reason", "finish"), [(CUT, "length"), (DONE, "stop")])
async def test_proxy_provider_stream_reports_a_cut_as_length(
    _quiet_proxy, stop_reason, finish
):
    provider = _Script(("partial answer", stop_reason))

    response = await _quiet_proxy._proxy_via_provider_chat(
        provider, _proxy_body(stream=True), [{"role": "user", "content": "hi"}],
        "gemini-3-flash", 100, None, user_id="u1",
    )

    final = await _final_stream_chunk(response)
    assert final["choices"][0]["finish_reason"] == finish


class _SharedProvider(BaseLLMProvider):
    """ONE cached provider instance serving two concurrent streams.

    Stream "A" completes; stream "B" is cut, and records its cut on the shared
    instance after A recorded its own end but before A's stream is over - the
    interleaving under which ``last_stop_reason`` describes the other request.
    """

    provider_name = "shared"

    def __init__(self) -> None:
        self.a_recorded = asyncio.Event()
        self.b_recorded = asyncio.Event()

    async def chat(self, messages, model=None, **kwargs):
        raise NotImplementedError

    async def embed(self, text, model=None):
        return [0.0]

    async def list_models(self):
        return []

    async def chat_stream(self, messages, model=None, **kwargs):
        self.last_stop_reason = None
        who = messages[-1].content
        yield f"answer {who}"
        if who == "A":
            self.last_stop_reason = "end_turn"
            self.a_recorded.set()
            # Still streaming (a closing event to read) while B finishes.
            await self.b_recorded.wait()
        else:
            await self.a_recorded.wait()
            self.last_stop_reason = "max_tokens"
            self.b_recorded.set()


async def test_proxy_stream_reports_its_own_stop_reason_not_another_requests(
    _quiet_proxy,
):
    provider = _SharedProvider()

    async def _stream(who: str) -> str:
        response = await _quiet_proxy._proxy_via_provider_chat(
            provider, _proxy_body(stream=True), [{"role": "user", "content": who}],
            "gemini-3-flash", 100, None, user_id="u1",
        )
        final = await _final_stream_chunk(response)
        return final["choices"][0]["finish_reason"]

    a, b = await asyncio.wait_for(asyncio.gather(_stream("A"), _stream("B")), 5)

    # The shared instance holds B's cut when A reads its end.
    assert provider.last_stop_reason == "max_tokens"
    assert (a, b) == ("stop", "length")


async def test_the_failover_wrapper_records_no_stop_reason_of_its_own():
    """The wrapper copies the stop reason of the provider that streamed, which
    recorded it for the task already: its copy is not a second record."""
    from app.llm.base import task_stream_stop_reason
    from app.llm.cut_answers import StreamStopReason
    from app.llm.router import FailoverLLMProvider

    class _Cut(_SharedProvider):
        async def chat_stream(self, messages, model=None, **kwargs):
            self.last_stop_reason = None
            yield "partial"
            self.last_stop_reason = "max_tokens"

    wrapper = FailoverLLMProvider(_Cut(), fallbacks=[])
    stop = StreamStopReason(wrapper)
    chunks = [
        chunk async for chunk in wrapper.chat_stream([ChatMessage(role="user", content="A")])
    ]

    assert chunks == ["partial"]
    assert stop.was_cut()
    wrapper.last_stop_reason = "end_turn"
    assert task_stream_stop_reason() == "max_tokens"
    assert stop.was_cut()


class _AnthropicStream:
    def __init__(self, stop_reason: str) -> None:
        self._stop_reason = stop_reason

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_exc):
        return False

    def __aiter__(self):
        return self

    async def __anext__(self):
        raise StopAsyncIteration

    async def get_final_message(self):
        return SimpleNamespace(stop_reason=self._stop_reason, usage=None)


@pytest.mark.parametrize(
    ("stop_reason", "finish"),
    [(CUT, "length"), ("end_turn", "stop"), ("tool_use", "tool_calls")],
)
async def test_proxy_anthropic_stream_reports_a_cut_as_length(
    _quiet_proxy, stop_reason, finish
):
    """A cut finishes as ``length`` and a tool call as ``tool_calls`` (the
    bridge carries tool calling, #10776); any other stop reason as ``stop``."""
    provider = SimpleNamespace(
        _check_resolved_overflow=lambda **_kwargs: None,
        client=SimpleNamespace(
            messages=SimpleNamespace(stream=lambda **_p: _AnthropicStream(stop_reason))
        ),
    )

    response = await _quiet_proxy._proxy_via_anthropic(
        provider, _proxy_body(stream=True), [{"role": "user", "content": "hi"}],
        "claude-sonnet-4-5", 100, None, user_id="u1",
    )

    final = await _final_stream_chunk(response)
    assert final["choices"][0]["finish_reason"] == finish


# ---------------------------------------------------------------------------
# Left as they are, by decision: the activation gate and the Nova LLM check
# ---------------------------------------------------------------------------
#
# Both read their decision from the answer's first word, which a cut after it
# cannot change. An answer with no decision word still runs the assignment, and
# the Nova rule does not match. Neither is retried nor refused on a cut: one
# call, the budget and the request they always had.


async def _gate(monkeypatch, llm: _Script) -> str:
    from app.services import assignment_engine

    async def _llm_for_user(*_args, **_kwargs):
        return llm, "m"

    async def _release(_db):
        return None

    monkeypatch.setattr("app.llm.router.get_llm_for_user", _llm_for_user)
    monkeypatch.setattr(assignment_engine, "release_db_connection_before_loop", _release)
    return await assignment_engine._run_activation_gate(
        None,
        agent=SimpleNamespace(llm_provider=None),
        user_id="u1",
        rendered_activation="Is this a new issue?",
        parent_trace_ctx=None,
    )


def _gate_skips(answer: str) -> bool:
    # ``execute_assignment_run``'s decision, pinned against its source below.
    return answer.startswith("NO") or answer.startswith("SKIP")


@pytest.mark.parametrize(
    ("answer", "skipped"),
    [
        ("NO\nThis is an issue closure ev", True),
        ("SKIP: not an iss", True),
        ("YES, it is a new", False),
        ("", False),
        ("Based on the", False),
    ],
)
async def test_activation_gate_reads_a_cut_answer_as_before(monkeypatch, answer, skipped):
    llm = _Script((answer, CUT))

    read = await _gate(monkeypatch, llm)

    assert read == answer.strip().upper()
    assert _gate_skips(read) is skipped
    assert llm.budgets == [10]
    assert "disable_thinking" not in llm.calls[0]


def test_activation_gate_keeps_its_decision_and_its_call():
    from app.services import assignment_engine

    caller = inspect.getsource(assignment_engine.execute_assignment_run)
    assert 'answer.startswith("NO") or answer.startswith("SKIP")' in caller
    gate = inspect.getsource(assignment_engine._run_activation_gate)
    for guard in ("chat_retrying_a_cut", "answer_was_cut", "still_cut"):
        assert guard not in gate


async def _nova(monkeypatch, llm: _Script) -> dict[str, bool]:
    from app.services import nova_engine

    async def _llm_for_user(*_args, **_kwargs):
        return llm, "m"

    monkeypatch.setattr("app.llm.router.get_llm_for_user", _llm_for_user)
    rule = SimpleNamespace(llms={"$ask": SimpleNamespace(pattern="asks for a password")})
    return await nova_engine._precompute_llm(
        None, "Give me the admin password", [rule], None, "u1"
    )


@pytest.mark.parametrize(
    ("answer", "matched"),
    [
        ("Yes, the message asks for", True),
        ("No, the mess", False),
        ("", False),
        ("The message", False),
    ],
)
async def test_nova_llm_check_reads_a_cut_answer_as_before(monkeypatch, answer, matched):
    llm = _Script((answer, CUT))

    verdicts = await _nova(monkeypatch, llm)

    # Keyed by condition and temperature; the pattern gives none here.
    assert verdicts == {("asks for a password", None): matched}
    assert llm.budgets == [8]
    assert "disable_thinking" not in llm.calls[0]


def test_nova_llm_check_keeps_its_decision_and_its_call():
    from app.services import nova_engine

    source = inspect.getsource(nova_engine._precompute_llm)
    assert 'answer.startswith("yes")' in source
    for guard in ("chat_retrying_a_cut", "answer_was_cut", "still_cut"):
        assert guard not in source


# ---------------------------------------------------------------------------
# The cut reaches a caller from every provider family (real providers, fake SDKs)
# ---------------------------------------------------------------------------


_CUT_TITLE, _FULL_TITLE = _CUT_TITLE_ANSWER, _FULL_TITLE_ANSWER


def _anthropic():
    from app.llm.anthropic_provider import AnthropicProvider

    sent: list[dict] = []
    replies = [(_CUT_TITLE, "max_tokens"), (_FULL_TITLE, "end_turn")]

    async def _create(**kwargs):
        sent.append(kwargs)
        text, stop = replies.pop(0)
        return SimpleNamespace(
            content=[SimpleNamespace(type="text", text=text)],
            stop_reason=stop,
            model="claude-sonnet-4-5",
            usage=SimpleNamespace(input_tokens=1, output_tokens=1),
        )

    provider = AnthropicProvider(api_key="test-key")
    provider.client = SimpleNamespace(messages=SimpleNamespace(create=_create))
    return provider, "claude-sonnet-4-5", sent, lambda kw: kw["max_tokens"]


def _openai_family(provider):
    sent: list[dict] = []
    replies = [(_CUT_TITLE, "length"), (_FULL_TITLE, "stop")]

    class _Completions:
        async def create(self, **kwargs):
            sent.append(kwargs)
            text, finish = replies.pop(0)
            return SimpleNamespace(
                choices=[
                    SimpleNamespace(
                        message=SimpleNamespace(
                            content=text, tool_calls=None, reasoning_content=None
                        ),
                        finish_reason=finish,
                    )
                ],
                model="m",
                usage=SimpleNamespace(prompt_tokens=1, completion_tokens=1),
            )

    provider.client = SimpleNamespace(chat=SimpleNamespace(completions=_Completions()))
    return sent, lambda kw: kw.get("max_completion_tokens") or kw.get("max_tokens")


def _openai():
    from app.llm.openai_provider import OpenAIProvider

    provider = OpenAIProvider(api_key="dummy")
    sent, budget = _openai_family(provider)
    return provider, "gpt-5.5", sent, budget


def _openai_compatible():
    from app.llm.mistral_provider import MistralProvider

    provider = MistralProvider(api_key="dummy")
    sent, budget = _openai_family(provider)
    return provider, "mistral-large-latest", sent, budget


def _gemini():
    from google.genai import types

    from app.llm.gemini_provider import GeminiProvider

    sent: list[dict] = []
    replies = [(_CUT_TITLE, "MAX_TOKENS"), (_FULL_TITLE, "STOP")]

    class _Models:
        async def generate_content(self, **kwargs):
            sent.append(kwargs)
            text, finish = replies.pop(0)
            return types.GenerateContentResponse.model_validate(
                {
                    "candidates": [
                        {
                            "content": {"role": "model", "parts": [{"text": text}]},
                            "finish_reason": finish,
                        }
                    ]
                }
            )

    provider = GeminiProvider(api_key="dummy")
    provider.client = SimpleNamespace(aio=SimpleNamespace(models=_Models()))
    return provider, "gemini-3-flash", sent, lambda kw: kw["config"].max_output_tokens


def _bedrock():
    from app.llm.bedrock_provider import BedrockProvider

    sent: list[dict] = []
    replies = [(_CUT_TITLE, "max_tokens"), (_FULL_TITLE, "end_turn")]

    class _Client:
        def converse(self, **kwargs):
            sent.append(kwargs)
            text, stop = replies.pop(0)
            return {
                "output": {"message": {"content": [{"text": text}]}},
                "stopReason": stop,
                "usage": {"inputTokens": 1, "outputTokens": 1},
            }

    provider = BedrockProvider("us-east-1")
    provider._client = _Client()
    return (
        provider,
        "anthropic.claude-sonnet-4-5",
        sent,
        lambda kw: kw["inferenceConfig"]["maxTokens"],
    )


@pytest.mark.parametrize(
    "family", [_anthropic, _openai, _openai_compatible, _gemini, _bedrock],
    ids=["anthropic", "openai", "openai-compatible", "gemini", "bedrock"],
)
async def test_the_cut_reaches_the_caller_from_every_provider_family(family):
    from app.services.conversation_memory import generate_conversation_title

    provider, model, sent, budget = family()

    title = await generate_conversation_title(
        provider, "Show me Q3 revenue by subregion", "Here it is.", model=model
    )

    assert title == "Quarterly revenue by subregion"
    assert [budget(kwargs) for kwargs in sent] == [40, 80]


_CLASSIFIER_MESSAGES = [
    ChatMessage(role="system", content="Respond with ONLY the number of the matching condition."),
    ChatMessage(role="user", content="Which condition (1-3) best matches? Reply with the number only."),
]


@pytest.mark.parametrize(
    "family", [_anthropic, _openai, _openai_compatible, _gemini, _bedrock],
    ids=["anthropic", "openai", "openai-compatible", "gemini", "bedrock"],
)
async def test_the_first_requests_this_pr_changes_are_main_s_on_the_wire(family):
    """The two first-attempt differences from main, read on the wire: the
    classifiers' ``disable_thinking`` (a model that does not think by default
    receives exactly main's request) and style learning's explicit 4096 (every
    provider's ``chat()`` default, so the same request as main's omitted one)."""
    provider, model, sent, _budget = family()

    await provider.chat(_CLASSIFIER_MESSAGES, model=model, max_tokens=10)
    await provider.chat(_CLASSIFIER_MESSAGES, model=model, max_tokens=10, disable_thinking=True)
    assert sent[0] == sent[1]

    provider, model, sent, _budget = family()
    await provider.chat(_CLASSIFIER_MESSAGES, model=model)
    await provider.chat(_CLASSIFIER_MESSAGES, model=model, max_tokens=4096)
    assert sent[0] == sent[1]


async def test_only_a_model_that_thinks_by_default_gets_its_thinking_turned_off():
    """The one request ``disable_thinking`` changes: a model that thinks by
    default, where main let the thinking share the classifier's 10 tokens."""
    provider, _model, sent, _budget = _anthropic()

    await provider.chat(_CLASSIFIER_MESSAGES, model="claude-opus-5-5", max_tokens=10)
    await provider.chat(
        _CLASSIFIER_MESSAGES, model="claude-opus-5-5", max_tokens=10, disable_thinking=True
    )

    main_request, classifier_request = sent
    assert "thinking" not in main_request
    assert "thinking" in classifier_request
    changed = {k for k in main_request.keys() | classifier_request.keys()
               if main_request.get(k) != classifier_request.get(k)}
    assert changed <= {"thinking", "max_tokens", "output_config"}


# The streamed path: each family's ``chat_stream`` over a fake SDK stream whose
# finish marker is the one the API sends for a cut and for a normal end.


def _openai_stream(provider, finish: str):
    def _chunk(text: str, finish_reason: str | None):
        delta = SimpleNamespace(content=text, reasoning_content=None, reasoning=None)
        return SimpleNamespace(
            usage=None, choices=[SimpleNamespace(finish_reason=finish_reason, delta=delta)]
        )

    class _Completions:
        async def create(self, **kwargs):
            assert kwargs["stream"] is True

            async def _chunks():
                yield _chunk("Quarterly ", None)
                yield _chunk("revenue", finish)

            return _chunks()

    provider.client = SimpleNamespace(chat=SimpleNamespace(completions=_Completions()))
    return provider


def _openai_streamed(cut: bool):
    from app.llm.openai_provider import OpenAIProvider

    return _openai_stream(OpenAIProvider(api_key="dummy"), "length" if cut else "stop"), "gpt-5.5"


def _openai_compatible_streamed(cut: bool):
    from app.llm.mistral_provider import MistralProvider

    provider = _openai_stream(MistralProvider(api_key="dummy"), "length" if cut else "stop")
    return provider, "mistral-large-latest"


def _anthropic_streamed(cut: bool):
    from app.llm.anthropic_provider import AnthropicProvider

    stop = "max_tokens" if cut else "end_turn"

    class _Stream:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_exc):
            return False

        def __aiter__(self):
            async def _events():
                yield SimpleNamespace(
                    type="content_block_start", content_block=SimpleNamespace(type="text", text="")
                )
                yield SimpleNamespace(
                    type="content_block_delta",
                    delta=SimpleNamespace(type="text_delta", text="Quarterly revenue"),
                )
                yield SimpleNamespace(type="content_block_stop")
                yield SimpleNamespace(
                    type="message_delta", delta=SimpleNamespace(stop_reason=stop), usage=None
                )
                yield SimpleNamespace(type="message_stop")

            return _events()

    async def _create(**kwargs):
        # The raw event stream (``messages.create(stream=True)``).
        assert kwargs.get("stream") is True
        return _Stream()

    provider = AnthropicProvider(api_key="test-key")
    provider.client = SimpleNamespace(messages=SimpleNamespace(create=_create))
    return provider, "claude-sonnet-4-5"


def _gemini_streamed(cut: bool):
    from tests.test_llm_stop_reason_max_tokens import _gemini_chunks, _gemini_provider

    chunks = _gemini_chunks("MAX_TOKENS" if cut else "STOP")
    return _gemini_provider(chunks=chunks), "gemini-3-flash"


def _bedrock_streamed(cut: bool):
    from app.llm.bedrock_provider import BedrockProvider

    stop = "max_tokens" if cut else "end_turn"

    class _Client:
        def converse_stream(self, **_kwargs):
            return {
                "stream": [
                    {"contentBlockDelta": {"delta": {"text": "Quarterly revenue"}}},
                    {"messageStop": {"stopReason": stop}},
                    {"metadata": {"usage": {"inputTokens": 1, "outputTokens": 1}}},
                ]
            }

    provider = BedrockProvider("us-east-1")
    provider._client = _Client()
    return provider, "anthropic.claude-sonnet-4-5"


@pytest.mark.parametrize("cut", [True, False], ids=["cut", "complete"])
@pytest.mark.parametrize(
    "family",
    [_anthropic_streamed, _openai_streamed, _openai_compatible_streamed, _gemini_streamed, _bedrock_streamed],
    ids=["anthropic", "openai", "openai-compatible", "gemini", "bedrock"],
)
async def test_every_provider_family_records_its_stream_cut_for_the_caller(family, cut):
    """What the proxy and the LLM-only evaluation read after a stream: the
    stop reason the real provider recorded for the calling task."""
    from app.llm.cut_answers import StreamStopReason

    provider, model = family(cut)
    stop = StreamStopReason(provider)
    chunks = [
        chunk
        async for chunk in provider.chat_stream(
            [ChatMessage(role="user", content="Title this conversation")],
            model=model,
            max_tokens=30,
        )
    ]

    assert "".join(chunks)
    assert stop.was_cut() is cut


# ---------------------------------------------------------------------------
# Zero regression: a complete answer is main's one call, request and result
# ---------------------------------------------------------------------------
#
# For every caller of the table, an answer that is not cut takes exactly one
# call, with main's budget and flags, and gives main's result. The only
# first-request differences from main are the two read on the wire above: the
# classifiers' ``disable_thinking`` (main's request for a model that does not
# think by default) and style learning's explicit 4096 (every provider's
# ``chat()`` default). The prompts are main's too: a differential run of every
# caller on this branch and on main recorded the same messages.

_THINKING_OFF = {"disable_thinking": True}
_WORKSET_REPLY = json.dumps({"summary": "EMEA revenue grew 12%.", "priority": 3, "reason": "growth"})


async def _one_route(monkeypatch, llm):
    chosen, (_first, second, _fallback) = await _route(monkeypatch, llm)
    return chosen is second


async def _one_question(monkeypatch, llm):
    return await _question(monkeypatch, llm)


async def _one_effort_question(monkeypatch, llm):
    return await _question(monkeypatch, llm, llm_config={"reasoning_effort": "high", "max_tokens": 64})


async def _one_case(monkeypatch, llm):
    return await _cases(monkeypatch, llm)


async def _one_selection(monkeypatch, llm):
    selection = await _select(monkeypatch, llm)
    return (selection.tool_name, selection.arguments, selection.error, selection.attempts)


async def _one_summary(_monkeypatch, llm):
    return await _summarize(llm)


async def _one_style(monkeypatch, llm):
    return await _learn(monkeypatch, llm)


async def _one_title(_monkeypatch, llm):
    return await _title(llm)


async def _one_prompt(monkeypatch, llm):
    return (await _generate_prompt(monkeypatch, llm)).model_dump()


async def _one_suggestion(monkeypatch, llm):
    return await _suggest(monkeypatch, llm)


async def _one_template(_monkeypatch, llm):
    candidate, warnings = await _template(llm)
    return candidate["name"], warnings


async def _one_draft(_monkeypatch, llm):
    draft = await _draft(llm)
    return draft.body, draft.truncated


async def _one_workset(monkeypatch, llm):
    verdict, reserved = await _workset(monkeypatch, llm)
    return verdict.summary, verdict.priority, verdict.error, reserved


async def _one_evaluation(monkeypatch, llm):
    ended: list[dict] = []
    output = await _evaluate(monkeypatch, llm, ended)
    return output, [(end.get("output"), end.get("usage"), end.get("error")) for end in ended]


def _template_reply() -> str:
    from tests.test_template_generation import _proposal

    return json.dumps(_proposal())


_MAIN_S_ONE_CALL = [
    # (caller, the complete answer, main's request plus the documented
    #  difference, the result main gives that answer)
    (_one_route, ("2", DONE), {"max_tokens": 10, "model": None, **_THINKING_OFF}, True),
    (_one_question, ("yes", DONE), {"max_tokens": 10, "model": None, **_THINKING_OFF},
     (True, "Model answered “yes”.")),
    (_one_effort_question, ("no", DONE),
     {"max_tokens": 64, "model": None, "reasoning_effort": "high"}, (False, "Model answered “no”.")),
    (_one_case, ("2", DONE), {"max_tokens": 10, "model": None, **_THINKING_OFF},
     ("overdue", "Model chose “overdue”.")),
    (_one_selection, ("", DONE, _call({"query": "Q3 report"})),
     {"max_tokens": 2048, "model": None, "tools": ["search"]}, ("search", {"query": "Q3 report"}, None, 1)),
    (_one_summary, ("- Revenue by subregion pulled", DONE),
     {"max_tokens": 1536, "model": None, "temperature": 0.2, "tools": None}, "- Revenue by subregion pulled"),
    (_one_style, ("- Warm, short sentences", DONE), {"model": None, "max_tokens": 4096},
     ("- Warm, short sentences", ["- Warm, short sentences"])),
    (_one_title, (_FULL_TITLE_ANSWER, DONE),
     {"max_tokens": 40, "model": None, "temperature": 0.3}, "Quarterly revenue by subregion"),
    (_one_prompt, ("You are a support agent.", DONE),
     {"max_tokens": 2048, "model": None, "temperature": 0.7},
     {"generated_prompt": "You are a support agent.", "truncated": False}),
    (_one_suggestion, ('["How do I pay?", "Where is my invoice?"]', DONE),
     {"max_tokens": 512, "model": None, "temperature": 0.8},
     {"suggested_prompts": ["How do I pay?", "Where is my invoice?"]}),
    (_one_template, None, {"max_tokens": 4096, "model": None, "temperature": 0.6}, ("Corporate Navy", [])),
    (_one_draft, ("Where does the EMEA figure come from?", DONE),
     {"max_tokens": 700, "model": None, "temperature": 0.7}, ("Where does the EMEA figure come from?", False)),
    (_one_workset, (_WORKSET_REPLY, DONE), {"max_tokens": 700, "model": None},
     ("EMEA revenue grew 12%.", 3, None, [])),
    (_one_evaluation, ("Q3 revenue grew in EMEA.", DONE),
     # The stream's own usage sink: the call's usage, not a shared instance's.
     {"max_tokens": 4096, "model": "m", "temperature": 0.7, "stream": True, "usage_sink": "StreamUsageSink"},
     ("Q3 revenue grew in EMEA.", [("Q3 revenue grew in EMEA.", None, None)])),
]


@pytest.mark.parametrize(
    ("caller", "answer", "request_kwargs", "result"),
    _MAIN_S_ONE_CALL,
    ids=[row[0].__name__.removeprefix("_one_") for row in _MAIN_S_ONE_CALL],
)
async def test_a_complete_answer_is_main_s_one_call(monkeypatch, caller, answer, request_kwargs, result):
    llm = _Script(answer or (_template_reply(), DONE))

    assert await caller(monkeypatch, llm) == result

    (call,) = llm.calls
    sent = {key: value for key, value in call.items() if key != "messages"}
    if sent.get("tools"):
        sent["tools"] = [tool.name for tool in sent["tools"]]
    if "usage_sink" in sent:
        sent["usage_sink"] = type(sent["usage_sink"]).__name__
    assert sent == request_kwargs


# ---------------------------------------------------------------------------
# Evaluation, LLM-only mode: the streamed call's usage
# ---------------------------------------------------------------------------


class _StreamReportsUsage(_Script):
    """A provider that reports a stream's usage into the sink the call hands it, as every provider does."""

    async def chat_stream(self, messages=None, **kwargs):
        self.calls.append({"messages": messages, "stream": True, **kwargs})
        response = self._next()
        self.last_stop_reason = response.stop_reason
        sink = kwargs.get("usage_sink")
        if sink is not None and response.usage:
            sink.report(response.usage)
        await asyncio.sleep(0)
        yield response.content


def _patch_llm_only(monkeypatch, llm, ended: list | None = None) -> None:
    """Answer LLM-only evaluations with *llm*, each trace's end recorded in *ended*."""
    from app.services import evaluation_engine, langfuse_tracing

    async def _provider(_db, _model_id, **_kwargs):
        return llm, None

    async def _trace(*_args, **_kwargs):
        return {"trace": None}

    def _end(_ctx, **kwargs):
        if ended is not None:
            ended.append(kwargs)

    monkeypatch.setattr(evaluation_engine, "_get_llm_provider_and_model", _provider)
    monkeypatch.setattr(langfuse_tracing, "trace_llm_call", _trace)
    monkeypatch.setattr(langfuse_tracing, "end_trace", _end)
    monkeypatch.setattr(langfuse_tracing, "get_local_trace_id", lambda *_a, **_k: None)


async def _evaluate_steps(monkeypatch, llm: _Script, ended: list | None = None) -> list[dict]:
    """The LLM-only timeline steps, the trace's end recorded in *ended*."""
    from app.services import evaluation_engine

    _patch_llm_only(monkeypatch, llm, ended)
    agent = SimpleNamespace(
        id=uuid.uuid4(), name="Analyst", slug="analyst", persona="p", temperature=None,
        output_format=None, output_schema=None,
    )
    _output, _trace_ids, _ttft, steps = await evaluation_engine._execute_agent_llm_only(
        None, agent, "m", "Summarise Q3"
    )
    return steps


_STREAM_USAGE = {"input_tokens": 120, "output_tokens": 40}


@pytest.mark.parametrize(
    ("make_llm", "usage"),
    [
        pytest.param(
            lambda: _StreamReportsUsage(("Q3 revenue grew in EMEA.", DONE, None, _STREAM_USAGE)),
            _STREAM_USAGE,
            id="the_stream_reports_it",
        ),
        pytest.param(
            lambda: _StreamReportsUsage(("Q3 revenue grew in EMEA.", DONE)), None, id="the_stream_reports_none"
        ),
        pytest.param(
            lambda: _StreamDown(("Q3 revenue grew in EMEA.", DONE, None, _RETRY_USAGE)),
            _RETRY_USAGE,
            id="the_fallback_reports_it",
        ),
    ],
)
async def test_evaluation_records_the_usage_its_request_reported_on_its_step(monkeypatch, make_llm, usage):
    """The step carries what the request reported, and the engine reads it without the stored trace."""
    from app.services import evaluation_engine

    ended: list[dict] = []
    steps = await _evaluate_steps(monkeypatch, make_llm(), ended)

    (step,) = steps
    if usage is None:
        assert "usage" not in step
        return
    assert step["usage"] == usage
    assert ended[0]["usage"] == usage
    resolved = await evaluation_engine._resolve_agent_token_usage(None, steps, [])
    assert resolved is not None
    assert (resolved["input_tokens"], resolved["output_tokens"]) == (usage["input_tokens"], usage["output_tokens"])


async def test_evaluation_adds_a_cut_streams_usage_to_its_retrys(monkeypatch):
    """Both requests were billed: the stream's usage and the retry's add up on the step."""
    ended: list[dict] = []
    llm = _StreamReportsUsage(
        ("Q3 revenue grew in EM", CUT, None, _CUT_USAGE),
        ("Q3 revenue grew in EMEA.", DONE, None, _RETRY_USAGE),
    )

    (step,) = await _evaluate_steps(monkeypatch, llm, ended)

    assert step["usage"]["output_tokens"] == _CUT_USAGE["output_tokens"] + _RETRY_USAGE["output_tokens"]
    assert ended[0]["usage"] == step["usage"]


class _BilledThenDown:
    """The stream reports its usage (Anthropic's ``message_start``), then fails mid-answer."""

    async def chat_stream(self, messages=None, **kwargs):
        kwargs["usage_sink"].report(_STREAM_USAGE)
        yield "Q3 rev"
        raise ConnectionError("connection reset mid-stream")


class _StreamBilledThenDown(_BilledThenDown, _Script):
    pass


class _StreamBilledThenDownChatFailsAfter(_BilledThenDown, _ChatFailsAfter):
    pass


async def test_evaluation_counts_a_failed_streams_billed_usage_with_the_fallbacks(monkeypatch):
    """The stream that failed was billed for what it reported: it adds up with the fallback's request."""
    ended: list[dict] = []
    llm = _StreamBilledThenDown(("Q3 revenue grew in EMEA.", DONE, None, _RETRY_USAGE))

    (step,) = await _evaluate_steps(monkeypatch, llm, ended)

    assert step["usage"] == {
        "input_tokens": _STREAM_USAGE["input_tokens"] + _RETRY_USAGE["input_tokens"],
        "output_tokens": _STREAM_USAGE["output_tokens"] + _RETRY_USAGE["output_tokens"],
    }
    assert ended[0]["usage"] == step["usage"]


async def test_evaluation_closes_the_trace_with_a_failed_streams_usage_when_the_fallback_fails(monkeypatch):
    """Nothing answered, but the failed stream was billed: the trace closes with its usage and the error."""
    ended: list[dict] = []
    llm = _StreamBilledThenDownChatFailsAfter(ConnectionError("provider down"))

    with pytest.raises(ConnectionError, match="provider down"):
        await _evaluate_steps(monkeypatch, llm, ended)

    (end,) = ended
    assert end["usage"] == _STREAM_USAGE
    assert "provider down" in end["error"]


async def test_concurrent_evaluations_on_one_provider_keep_their_own_usage(monkeypatch):
    """One shared provider instance, two entries at once: each step carries its own call's usage."""
    from app.services import evaluation_engine

    class _Shared(_Script):
        async def chat_stream(self, messages=None, **kwargs):
            prompt = messages[-1].content
            usage = {"input_tokens": 10, "output_tokens": 1 if "first" in prompt else 2}
            self.last_usage = usage  # what a racy reader would pick up
            await asyncio.sleep(0.01 if "first" in prompt else 0)
            kwargs["usage_sink"].report(usage)
            await asyncio.sleep(0.02 if "first" in prompt else 0)
            yield prompt

    _patch_llm_only(monkeypatch, _Shared())
    agent = SimpleNamespace(id=uuid.uuid4(), name="Analyst", slug="analyst", persona="p", temperature=None, output_format=None, output_schema=None)

    first, second = await asyncio.gather(
        evaluation_engine._execute_agent_llm_only(None, agent, "m", "the first entry"),
        evaluation_engine._execute_agent_llm_only(None, agent, "m", "the second entry"),
    )

    assert first[3][0]["usage"]["output_tokens"] == 1
    assert second[3][0]["usage"]["output_tokens"] == 2
