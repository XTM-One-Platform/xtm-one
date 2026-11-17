"""The mandatory-transfer routing call counts in its turn's usage.

With two or more mandatory-transfer targets, ``run_agent_chat`` asks a model
which one gets the turn once its loop has returned. That call is not in the
usage the loop returned, so the routing evaluator hands its usage to the
caller (``merge_usage``) and ``add_call_usage`` adds it to the run's usage the
way the loop adds a call: summed, with the parts that keep a call another model
served or that reports in another convention priced on its own.

The turn itself, on PostgreSQL: ``test_mandatory_transfer_routing_trace_postgres.py``.
"""

from __future__ import annotations

import copy
import uuid
from types import SimpleNamespace

import pytest

from app.llm.base import ChatResponse
from app.services import langfuse_tracing as lft
from app.tools.agent_loop import add_call_usage

_ANTHROPIC_RUN = {"input_tokens": 1_000, "output_tokens": 200, "cache_read_input_tokens": 4_000}
_ANTHROPIC_CALL = {"input_tokens": 250, "output_tokens": 2}
_OPENAI_CALL = {
    "prompt_tokens": 300,
    "completion_tokens": 3,
    "total_tokens": 303,
    "prompt_tokens_details": {"cached_tokens": 100},
}


def _total(usage: dict) -> int:
    return lft._normalize_usage(lft._without_usage_details(usage))["total"]


# ---------------------------------------------------------------------------
# add_call_usage
# ---------------------------------------------------------------------------


def test_a_call_the_run_model_served_sums_into_the_run_without_parts():
    summed = add_call_usage(_ANTHROPIC_RUN, _ANTHROPIC_CALL, run_model="claude", served_model="claude")

    assert summed == {"input_tokens": 1_250, "output_tokens": 202, "cache_read_input_tokens": 4_000}


def test_a_call_without_its_model_goes_to_the_run_model():
    summed = add_call_usage(_ANTHROPIC_RUN, _ANTHROPIC_CALL, run_model="claude", served_model=None)

    assert lft.USAGE_PARTS_KEY not in summed
    assert summed["input_tokens"] == 1_250


def test_a_call_another_model_served_is_kept_as_its_own_part():
    summed = add_call_usage(_ANTHROPIC_RUN, _ANTHROPIC_CALL, run_model="claude", served_model="router")

    assert summed[lft.USAGE_PARTS_KEY] == [
        {"model": "claude", "usage": _ANTHROPIC_RUN},
        {"model": "router", "usage": _ANTHROPIC_CALL},
    ]
    assert _total(summed) == _total(_ANTHROPIC_RUN) + _total(_ANTHROPIC_CALL)


def test_a_call_in_another_convention_is_read_with_its_own():
    """The flat sum of an Anthropic run and an OpenAI call reads the run only."""
    summed = add_call_usage(_ANTHROPIC_RUN, _OPENAI_CALL, run_model="claude", served_model="gpt")

    assert [part["model"] for part in summed[lft.USAGE_PARTS_KEY]] == ["claude", "gpt"]
    assert _total(summed) == _total(_ANTHROPIC_RUN) + _total(_OPENAI_CALL)
    assert lft.usage_token_totals(summed) == (5_000 + 300, 200 + 3)


def test_a_run_already_in_parts_keeps_them_and_gains_the_call():
    run = add_call_usage(_ANTHROPIC_RUN, _OPENAI_CALL, run_model="claude", served_model="gpt")

    summed = add_call_usage(run, _ANTHROPIC_CALL, run_model="claude", served_model="router")

    assert [part["model"] for part in summed[lft.USAGE_PARTS_KEY]] == ["claude", "gpt", "router"]
    assert _total(summed) == _total(_ANTHROPIC_RUN) + _total(_OPENAI_CALL) + _total(_ANTHROPIC_CALL)


def test_a_run_with_no_usage_takes_the_call_whole():
    assert add_call_usage(None, _OPENAI_CALL, run_model="m", served_model="m") == _OPENAI_CALL
    assert add_call_usage({}, _OPENAI_CALL, run_model="m", served_model="router") == _OPENAI_CALL


def test_neither_input_is_changed():
    run = {"prompt_tokens": 10, "completion_tokens": 1, "prompt_tokens_details": {"cached_tokens": 4}}
    run_before, call_before = copy.deepcopy(run), copy.deepcopy(_OPENAI_CALL)

    summed = add_call_usage(run, _OPENAI_CALL, run_model="gpt", served_model="gpt")

    assert (run, _OPENAI_CALL) == (run_before, call_before)
    assert summed["prompt_tokens_details"] == {"cached_tokens": 104}


# ---------------------------------------------------------------------------
# The routing evaluator hands its calls' usage over
# ---------------------------------------------------------------------------


class _RoutingLLM:
    """Answers the routing classifier from a script, one response per call."""

    def __init__(self, *responses: ChatResponse) -> None:
        self._responses = list(responses)
        self.calls = 0

    async def chat(self, messages=None, **kwargs) -> ChatResponse:
        self.calls += 1
        return self._responses.pop(0)


async def _route(monkeypatch, llm: _RoutingLLM, *, merge_usage) -> object:
    async def _settings(_db):
        return SimpleNamespace(routing_model=None, routing_provider_id=None)

    async def _llm_for_user(*_args, **_kwargs):
        return llm, "router-requested"

    monkeypatch.setattr("app.api.v1.platform_settings_helpers.get_platform_settings", _settings)
    monkeypatch.setattr("app.llm.router.get_llm_for_user", _llm_for_user)
    from app.services.routing_evaluator import evaluate_routing_conditions

    first = SimpleNamespace(id=uuid.uuid4(), name="First")
    second = SimpleNamespace(id=uuid.uuid4(), name="Second")
    return await evaluate_routing_conditions(
        None,
        agent_output="The customer wants a refund.",
        mandatory_targets=[first, second],
        conditions={str(first.id): "billing", str(second.id): "refunds"},
        merge_usage=merge_usage,
    )


@pytest.mark.asyncio
async def test_each_routing_call_hands_its_usage_and_model_over(monkeypatch):
    """A cut answer is asked again: both calls were paid for."""
    llm = _RoutingLLM(
        ChatResponse(content="", model="router-dated", stop_reason="max_tokens", usage=dict(_ANTHROPIC_CALL)),
        ChatResponse(content="2", model="", stop_reason="end_turn", usage=dict(_OPENAI_CALL)),
    )
    seen: list[tuple[dict, str | None]] = []

    chosen = await _route(monkeypatch, llm, merge_usage=lambda usage, model=None: seen.append((usage, model)))

    assert chosen.name == "Second"
    assert llm.calls == 2
    # The second response names no model: the one the call was made with.
    assert seen == [(_ANTHROPIC_CALL, "router-dated"), (_OPENAI_CALL, "router-requested")]


@pytest.mark.asyncio
async def test_a_routing_call_that_reports_no_usage_hands_nothing_over(monkeypatch):
    llm = _RoutingLLM(ChatResponse(content="1", model="router", stop_reason="end_turn", usage={}))
    seen: list[tuple[dict, str | None]] = []

    chosen = await _route(monkeypatch, llm, merge_usage=lambda usage, model=None: seen.append((usage, model)))

    assert chosen.name == "First"
    assert seen == []


@pytest.mark.asyncio
async def test_without_a_hook_the_routing_decision_is_unchanged(monkeypatch):
    llm = _RoutingLLM(ChatResponse(content="1", model="router", stop_reason="end_turn", usage=dict(_ANTHROPIC_CALL)))

    chosen = await _route(monkeypatch, llm, merge_usage=None)

    assert chosen.name == "First"
