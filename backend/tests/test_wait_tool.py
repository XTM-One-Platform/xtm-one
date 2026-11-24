"""Tests for the agentic ``wait`` primitive.

Three things must hold for waiting to be safe to make iteration-free, and each
has a test here:

* **Bounded.** Every pause is clamped by the per-call cap, the per-run
  cumulative budget and the run's wall-clock deadline — and a clamp is always
  reported back to the model, because a silent clamp reads as a completed wait.
* **Interruptible.** A skip ends the current pause and only the current one; a
  satisfied ``until`` condition ends it early.
* **Not free.** The loop refunds the iteration only for rounds that actually
  paused, so a refused or malformed ``wait`` cannot be spammed for free rounds.
"""

import asyncio
import re
import time
import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.llm.base import BaseLLMProvider, ChatMessage, ChatResponse
from app.services import sub_agent as sub_agent_module
from app.services.builtin_tools import build_sub_agent_tools
from app.services.builtin_tools.wait import make_task_condition, watched_job_statuses
from app.tools import agent_loop as al
from app.tools import wait_policy as wp
from app.tools.base import (
    CallableToolExecutor,
    CompositeToolExecutor,
    ToolCall,
    ToolDefinition,
)


@pytest.fixture(autouse=True)
def _no_watched_runs(monkeypatch):
    """No ``until`` id is a run or a Video Studio job unless a test says so: no database here."""

    async def _none(run_ids, user_id, **_kwargs):
        return {}

    monkeypatch.setattr("app.services.builtin_tools.wait.watched_run_statuses", _none)
    monkeypatch.setattr("app.services.builtin_tools.wait.watched_job_statuses", _none)
    monkeypatch.setattr("app.services.builtin_tools.wait.watched_delegation_statuses", _none)


# ── Duration parsing / formatting ─────────────────────────────────────────


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("30s", 30),
        ("5m", 300),
        ("1h", 3600),
        ("90", 90),
        (90, 90),
        ("5 minutes", 300),
        ("  10M  ", 600),
        # Not positive durations — the tool must answer with usage guidance
        # rather than silently not waiting.
        ("0s", None),
        ("0", None),
        (-5, None),
        ("soon", None),
        ("", None),
        (None, None),
        (True, None),  # bool is an int subclass — never a duration
        ({"duration": "5m"}, None),
    ],
)
def test_parse_wait_duration(raw, expected):
    assert wp.parse_wait_duration(raw) == expected


@pytest.mark.parametrize(
    ("seconds", "expected"),
    [(0, "0s"), (45, "45s"), (60, "1m"), (90, "1m 30s"), (300, "5m"), (3600, "1h"), (7500, "2h 5m")],
)
def test_format_wait_duration(seconds, expected):
    assert wp.format_wait_duration(seconds) == expected


# ── Policy resolution ─────────────────────────────────────────────────────


class _Config:
    def __init__(self, per_call, total):
        self.maximum_wait_time = per_call
        self.maximum_total_wait_time = total


def test_policy_from_missing_config_uses_platform_defaults():
    """A fresh install with no config row must still get a working timer."""
    policy = wp.WaitPolicy.from_config(None)
    assert policy.per_call_seconds == wp.DEFAULT_MAXIMUM_WAIT_TIME
    assert policy.total_seconds == wp.DEFAULT_MAXIMUM_TOTAL_WAIT_TIME
    assert policy.enabled


def test_policy_from_config_clamps_out_of_range_values():
    """A value written out-of-band (direct DB edit) is clamped on read too."""
    policy = wp.WaitPolicy.from_config(_Config(99_999, 0))
    assert policy.per_call_seconds == wp.MAXIMUM_WAIT_TIME_CEILING
    assert policy.total_seconds == 1  # floored, not silently defaulted


def test_disabled_policy_is_not_enabled():
    """The public embed surface opts out entirely — no tool is offered."""
    assert not wp.WaitPolicy.disabled().enabled


# ── Granting ──────────────────────────────────────────────────────────────


def test_grant_clamps_to_per_call_cap():
    budget = wp.WaitBudget(wp.WaitPolicy(per_call_seconds=60, total_seconds=600))
    grant = wp.grant_wait(300, budget=budget)
    assert grant.granted == 60
    assert grant.clamped
    assert grant.limit == "maximum_wait_time"


def test_grant_clamps_to_remaining_run_budget():
    budget = wp.WaitBudget(wp.WaitPolicy(per_call_seconds=300, total_seconds=100))
    budget.charge(70)
    grant = wp.grant_wait(300, budget=budget)
    assert grant.granted == pytest.approx(30)
    assert grant.limit == "maximum_total_wait_time"


def test_grant_leaves_room_after_the_run_deadline():
    """A wait must never eat the budget the final answer needs."""
    budget = wp.WaitBudget(wp.WaitPolicy(per_call_seconds=300, total_seconds=600))
    grant = wp.grant_wait(300, budget=budget, remaining_wall_clock=120)
    assert grant.granted == pytest.approx(120 - wp.WAIT_DEADLINE_RESERVE_SECONDS)
    assert grant.limit == "run_deadline"


def test_grant_refuses_when_nothing_useful_is_left():
    budget = wp.WaitBudget(wp.WaitPolicy(per_call_seconds=300, total_seconds=600))
    budget.charge(600)
    grant = wp.grant_wait(60, budget=budget)
    assert grant.refused
    assert grant.granted == 0


def test_budget_charges_only_elapsed_time():
    """A wait that woke early costs what it took, not what it asked for."""
    budget = wp.WaitBudget(wp.WaitPolicy(total_seconds=100))
    budget.charge(10)
    budget.charge(-5)  # defensive: never credits back
    assert budget.spent_seconds == 10
    assert budget.remaining_total() == 90
    assert budget.wait_count == 2


# ── sleep_or_wake ─────────────────────────────────────────────────────────


async def test_sleep_or_wake_returns_false_when_the_timer_wins():
    assert await wp.sleep_or_wake(0.02, asyncio.Event()) is False


async def test_sleep_or_wake_returns_immediately_on_a_set_event():
    event = asyncio.Event()
    event.set()
    started = time.monotonic()
    assert await wp.sleep_or_wake(30, event) is True
    assert time.monotonic() - started < 1, (
        "a skip must end the pause immediately, not when the timer expires — "
        "otherwise 'continue now' is indistinguishable from doing nothing"
    )


# ── execute_wait ──────────────────────────────────────────────────────────

def _spent_budget() -> wp.WaitBudget:
    """A budget with nothing left, so the next wait is refused outright."""
    budget = wp.WaitBudget(wp.WaitPolicy(total_seconds=60))
    budget.charge(60)
    return budget



async def test_execute_wait_rejects_an_unparseable_duration():
    budget = wp.WaitBudget()
    result = await wp.execute_wait({"duration": "soon"}, budget=budget)
    assert result.reason == wp.WAIT_REASON_INVALID
    assert "Invalid duration" in result.message
    assert not result.paused
    assert budget.spent_seconds == 0


async def test_execute_wait_refuses_when_the_budget_is_spent():
    budget = wp.WaitBudget(wp.WaitPolicy(total_seconds=60))
    budget.charge(60)
    result = await wp.execute_wait({"duration": "1m"}, budget=budget)
    assert result.reason == wp.WAIT_REASON_REFUSED
    assert "Cannot wait" in result.message
    assert "Do not retry" in result.message
    assert not result.paused, "a refused wait must not earn an iteration refund"


async def test_execute_wait_refuses_a_pause_the_clamps_ground_down_to_nothing():
    """A 10-minute request clamped to a fraction of a second is not a wait —
    the model is told so instead of being handed a pause that reads as a bug."""
    budget = wp.WaitBudget(wp.WaitPolicy(per_call_seconds=300, total_seconds=600))
    result = await wp.execute_wait(
        {"duration": "10m"}, budget=budget, remaining_wall_clock=wp.WAIT_DEADLINE_RESERVE_SECONDS + 0.5
    )
    assert result.reason == wp.WAIT_REASON_REFUSED
    assert "run's own time budget" in result.message


async def test_execute_wait_honours_a_deliberately_short_request():
    """``wait("1s")`` is a legal request: the refusal floor applies to pauses
    the clamps destroyed, never to a small duration the agent chose."""
    budget = wp.WaitBudget()
    result = await wp.execute_wait({"duration": "1s"}, budget=budget)
    assert result.reason == wp.WAIT_REASON_ELAPSED
    assert result.granted == 1
    assert budget.spent_seconds >= 1


async def test_execute_wait_reports_the_clamp_and_the_bound_that_caused_it(monkeypatch):
    """A model told *why* it got less waits again on purpose; a silent clamp
    reads as a finished wait and the model moves on too early."""
    # The pause is real, just scaled down so the suite stays fast.
    monkeypatch.setattr(wp, "MIN_GRANTED_WAIT_SECONDS", 0.05)
    budget = wp.WaitBudget(wp.WaitPolicy(per_call_seconds=1, total_seconds=600))
    result = await wp.execute_wait({"duration": "10m"}, budget=budget)
    assert result.granted == 1
    assert "You asked for 10m" in result.message
    assert "maximum_wait_time" in result.message
    assert "Remaining wait budget" in result.message


async def test_execute_wait_does_not_pause_when_until_is_already_satisfied():
    calls: list[list[str]] = []

    async def _condition(ids):
        calls.append(ids)
        return True, {ids[0]: "completed"}

    task_id = "11111111-2222-3333-4444-555555555555"
    budget = wp.WaitBudget()
    started = time.monotonic()
    result = await wp.execute_wait(
        {"duration": "5m", "until": task_id}, budget=budget, condition=_condition
    )
    assert result.reason == wp.WAIT_REASON_ALREADY_DONE
    assert "No wait needed" in result.message
    assert "get_task_result" in result.message
    assert "Do not end the turn with a checkpoint" in result.message
    assert time.monotonic() - started < 1
    assert calls == [[task_id]]
    assert budget.spent_seconds == 0


async def test_execute_wait_wakes_as_soon_as_the_condition_is_met():
    """The whole point of naming a condition: a 5-minute request costs what the
    delegated work actually took."""
    states = iter([(False, {}), (True, {"11111111-2222-3333-4444-555555555555": "completed"})])

    async def _condition(_ids):
        return next(states)

    budget = wp.WaitBudget()
    started = time.monotonic()
    result = await wp.execute_wait(
        {"duration": "5m", "until": "11111111-2222-3333-4444-555555555555"},
        budget=budget,
        condition=_condition,
        poll_interval=0.01,
    )
    assert result.reason == wp.WAIT_REASON_CONDITION
    assert time.monotonic() - started < 1
    assert "everything you were waiting for has finished" in result.message.lower()


async def test_execute_wait_ignores_until_when_it_cannot_be_observed():
    """An ``until`` that silently does nothing would make the agent trust an
    early wake-up that can never happen."""
    result = await wp.execute_wait(
        {"duration": "1s", "until": "not-a-uuid"},
        budget=wp.WaitBudget(),
        condition=lambda ids: (_ for _ in ()).throw(AssertionError("must not be called")),
    )
    assert "ignored 'until'" in result.message


async def test_execute_wait_skip_ends_one_pause_only():
    """One skip ends one wait — a still-set event would turn every later wait
    in the run into a no-op."""
    skip = asyncio.Event()
    budget = wp.WaitBudget(wp.WaitPolicy(per_call_seconds=30, total_seconds=600))
    task = asyncio.create_task(
        wp.execute_wait({"duration": "5m"}, budget=budget, skip_event=skip)
    )
    await asyncio.sleep(0.05)
    skip.set()  # the user pressed "continue now" mid-pause
    result = await task
    assert result.reason == wp.WAIT_REASON_SKIPPED
    assert "the user chose to continue" in result.message
    assert "Tasks may still be running" in result.message
    assert "Do not end the turn with a checkpoint" in result.message
    assert result.elapsed < 30, "the skip must cut the pause short"
    assert not skip.is_set()


@pytest.mark.parametrize(
    ("arguments", "budget_factory", "expected_reason"),
    [
        # Malformed duration — answered with usage guidance, never paused.
        ({"duration": "soon"}, wp.WaitBudget, wp.WAIT_REASON_INVALID),
        # Nothing left to wait with — refused before the pause.
        (
            {"duration": "1m"},
            lambda: _spent_budget(),
            wp.WAIT_REASON_REFUSED,
        ),
        # Named children still running: a spent budget is a hand-off, not a
        # generic refusal the model should retry.
        (
            {
                "duration": "1m",
                "until": "11111111-1111-4111-8111-111111111111",
            },
            lambda: _spent_budget(),
            wp.WAIT_REASON_HANDOFF,
        ),
    ],
)
async def test_execute_wait_drops_a_skip_with_no_pause_in_flight(
    arguments, budget_factory, expected_reason
):
    """A skip pressed while nothing was pausing must not auto-skip the NEXT
    wait: these paths return before the pause, so the event has to be cleared
    on the way in, not only on the way out."""
    skip = asyncio.Event()
    skip.set()
    result = await wp.execute_wait(
        arguments, budget=budget_factory(), skip_event=skip
    )
    assert result.reason == expected_reason
    assert not skip.is_set(), (
        "a stale skip left set here is consumed by the next legitimate wait, "
        "which then returns instantly without ever pausing"
    )


async def test_execute_wait_drops_a_stale_skip_when_until_is_already_satisfied():
    """Same leak, via the ``until`` shortcut: the call returns before the pause,
    so the stale event must already be gone."""
    skip = asyncio.Event()
    skip.set()
    task_id = "11111111-1111-4111-8111-111111111111"

    async def _condition(ids):
        return True, {ids[0]: "completed"}

    result = await wp.execute_wait(
        {"duration": "5m", "until": task_id},
        budget=wp.WaitBudget(),
        skip_event=skip,
        condition=_condition,
    )
    assert result.reason == wp.WAIT_REASON_ALREADY_DONE
    assert not skip.is_set()


async def test_execute_wait_does_not_let_a_stale_skip_cancel_its_own_pause(
    monkeypatch,
):
    """The cleared event must not come back to bite the very call that cleared
    it: this wait pauses for real and reports ``elapsed``, not ``skipped``."""
    monkeypatch.setattr(wp, "MIN_GRANTED_WAIT_SECONDS", 0.05)
    skip = asyncio.Event()
    skip.set()
    budget = wp.WaitBudget(wp.WaitPolicy(per_call_seconds=1, total_seconds=600))
    result = await wp.execute_wait({"duration": "5m"}, budget=budget, skip_event=skip)
    assert result.reason == wp.WAIT_REASON_ELAPSED
    assert result.elapsed >= 1


async def test_execute_wait_announces_the_granted_duration_not_the_requested_one(
    monkeypatch,
):
    """The UI countdown ticks down the real pause: announcing 10m for a pause
    clamped to 1s would promise something the run never honours."""
    monkeypatch.setattr(wp, "MIN_GRANTED_WAIT_SECONDS", 0.05)
    seen: list[float] = []

    async def _on_grant(grant):
        seen.append(grant.granted)

    budget = wp.WaitBudget(wp.WaitPolicy(per_call_seconds=1, total_seconds=600))
    await wp.execute_wait({"duration": "10m"}, budget=budget, on_grant=_on_grant)
    assert seen == [1]


async def test_execute_wait_charges_the_budget_even_when_cancelled():
    """The run's cumulative budget must reflect time really spent, including a
    pause the loop cancelled mid-flight."""
    budget = wp.WaitBudget()
    task = asyncio.create_task(wp.execute_wait({"duration": "5m"}, budget=budget))
    await asyncio.sleep(0.05)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert budget.spent_seconds > 0


# ── Tool definition ──────────────────────────────────────────────────────


def test_definition_states_the_resolved_caps():
    """A model that knows the ceiling asks for a legal duration instead of
    discovering it from a clamp notice."""
    definition = wp.build_wait_tool_definition(
        wp.WaitPolicy(per_call_seconds=120, total_seconds=600)
    )
    assert definition.name == wp.WAIT_TOOL_NAME
    assert "2m" in definition.description
    assert "10m" in definition.description
    assert definition.input_schema["required"] == ["duration"]
    assert set(definition.input_schema["properties"]) == {"duration", "until", "reason"}
    # Priority 0 keeps the timer out of the provider tool-count cap's firing
    # line: a run that loses it falls back to tight polling.
    assert definition.priority == 0
    assert definition.source.startswith("builtin")
    assert "do not wait if you still have independent" in definition.description.lower()
    assert "sibling tools" in definition.description.lower()


def test_system_prompt_states_the_call_forms_and_the_caps():
    """``wait`` is a default primitive of every agent, never a bindable built-in
    an owner can forget to enable — so the prompt block is the only thing that
    tells the model it exists, and it must carry the resolved limits."""
    prompt = wp.build_wait_system_prompt(
        wp.WaitPolicy(per_call_seconds=120, total_seconds=600)
    )
    assert "wait(duration=" in prompt
    assert "until=" in prompt
    assert "2m per wait" in prompt
    assert "10m per run" in prompt
    assert "does NOT consume your step budget" in prompt
    assert "keep calling independent tools" in prompt
    assert "other tools run first" in prompt


@pytest.mark.parametrize("per_call", [1, 10, 45, 120, 180, 300, 3600])
def test_examples_are_legal_and_parseable_under_the_resolved_cap(per_call):
    """The "it always waits 3 minutes" report.

    The prompt used to hardcode ``5m`` in both call forms while stating the
    *resolved* cap right next to it.  Under a 3-minute ``maximum_wait_time`` the
    model copied the example, asked for 5m and was clamped back to exactly the
    cap on every single wait.  Every duration a model can copy must both parse
    and fit under the cap it is shown.
    """
    policy = wp.WaitPolicy(per_call_seconds=per_call, total_seconds=1800)
    definition = wp.build_wait_tool_definition(policy)
    examples = re.findall(
        r'duration="([^"]+)"', wp.build_wait_system_prompt(policy)
    )
    examples += re.findall(
        r"'(\d+\s*[a-z]*)'",
        definition.input_schema["properties"]["duration"]["description"],
    )
    assert examples
    for raw in examples:
        seconds = wp.parse_wait_duration(raw)
        assert seconds is not None, f"unparseable example {raw!r}"
        assert seconds <= per_call, f"example {raw!r} exceeds the {per_call}s cap"


# 1s is deliberately absent: at a one-second cap no two distinct legal
# single-unit durations exist, so an equal pair is the correct degenerate
# outcome there rather than a failure.
@pytest.mark.parametrize("per_call", [10, 45, 60, 120, 180, 300, 3600])
def test_prompt_shows_two_different_durations(per_call):
    """One hardcoded example is what an LLM copies verbatim; two different sizes
    are what say the duration is a choice.  This has to hold across the whole
    configurable range: a sub-minute cap is the regime where a naive short/long
    derivation silently collapses to a single value while staying legal."""
    policy = wp.WaitPolicy(per_call_seconds=per_call, total_seconds=1800)
    examples = re.findall(r'duration="([^"]+)"', wp.build_wait_system_prompt(policy))
    assert len(examples) == 2
    assert examples[0] != examples[1], (
        f"cap {per_call}s renders the same example twice ({examples[0]!r}) — "
        "a single repeated example is precisely the anchoring this fixes"
    )


def test_prompt_carries_no_angle_bracket_placeholders():
    """``until="<task_id>,<task_id>"`` reads as a tag: the Markdown-based trace
    viewer drops it entirely (the block renders as ``until=","``), and a model
    has no reason to read it as a placeholder either."""
    prompt = wp.build_wait_system_prompt()
    assert "<" not in prompt
    assert ">" not in prompt


_PLACEHOLDER_RE = re.compile(r"<[^<>\n]+>")


def test_continuation_prompt_carries_no_angle_bracket_placeholders():
    """Same rendering defect as ``build_wait_system_prompt``: the
    Markdown-based trace viewer drops ``<their IDs>``, leaving ``until=``."""
    prompt = sub_agent_module._wait_continuation_prompt("original task", ["task-1", "task-2"])
    assert "<" not in prompt
    assert ">" not in prompt
    assert "until" in prompt and "comma-separated" in prompt


@pytest.mark.parametrize(
    "text",
    [
        wp._POST_WAIT_PROTOCOL,
        wp._POST_SKIP_PROTOCOL,
        wp._POST_PARTIAL_PROTOCOL,
        al._DEDUP_CACHE_NUDGE,
        al._NEAR_DUPLICATE_NUDGE,
    ],
)
def test_agent_facing_tool_instructions_carry_no_bracketed_placeholder(text):
    assert not _PLACEHOLDER_RE.search(text), _PLACEHOLDER_RE.search(text).group(0)


def test_skip_protocol_says_what_the_next_wait_watches():
    """A skipped wait hands back tasks that may still run: the protocol names
    them as the next ``until``, never an empty ``until=`` the wait would ignore
    before sleeping the whole duration."""
    assert "until set to the task IDs still running" in wp._POST_SKIP_PROTOCOL
    assert not re.search(r"\buntil=(?=[\s,.;:)\]]|$)", wp._POST_SKIP_PROTOCOL)


def test_prompt_tells_the_model_to_size_the_duration():
    """Stating the caps says what is *legal*, not what is *appropriate* — with
    only the caps the model has no instruction to estimate at all."""
    assert "Size the pause to the work" in wp.build_wait_system_prompt()


def test_make_task_condition_without_a_user_returns_none():
    """Anonymous surfaces get no condition resolver — the tool result then says
    ``until`` was ignored instead of waiting blind."""
    assert make_task_condition(None) is None
    assert make_task_condition("") is None


# ── Agent-loop integration ───────────────────────────────────────────────


def test_wait_is_dedup_exempt():
    """Repeated identical waits are legitimate — the dedup cache must not
    serve a cached "Waited 5m" for a pause that never happened."""
    assert al._is_dedup_exempt(wp.WAIT_TOOL_NAME)


def test_wait_gets_its_own_tool_timeout():
    """A pause longer than the regular per-tool ceiling must not be killed and
    reported to the model as a tool failure."""
    assert al._tool_timeout_for("some_tool", 300) == 300
    assert al._tool_timeout_for("consult_agent", 300) == al._AGENT_TOOL_TIMEOUT
    assert al._tool_timeout_for(wp.WAIT_TOOL_NAME, 300) == wp.WAIT_TOOL_TIMEOUT
    assert wp.WAIT_TOOL_TIMEOUT > wp.MAXIMUM_WAIT_TIME_CEILING


def test_wait_only_round_detection_handles_sanitised_names():
    call = ToolCall(id="1", name="wait_x", arguments={})
    assert al._is_wait_only_round([call], {"wait_x": "wait"})
    assert not al._is_wait_only_round([call], {})
    assert not al._is_wait_only_round([], {})
    # ``check_task_status`` also only waits, so a polling round is refundable.
    assert al._is_wait_only_round(
        [ToolCall(id="2", name="check_task_status", arguments={})], {}
    )
    # A mixed round acted, so it is charged.
    assert not al._is_wait_only_round(
        [
            ToolCall(id="2", name="check_task_status", arguments={}),
            ToolCall(id="3", name="search_knowledge", arguments={}),
        ],
        {},
    )


@pytest.mark.parametrize(
    ("budget", "expected"),
    [(25, (21, 23)), (12, (8, 10)), (6, (3, 4)), (4, (-1, 2)), (3, (-1, 2)), (2, (-1, 1))],
)
def test_wind_down_points_match_the_documented_stages(budget, expected):
    """Regression guard on the extraction of the wind-down thresholds into a
    function (they are now recomputed whenever a waiver extends the budget)."""
    assert al._wind_down_points(budget) == expected


class _ScriptedLLM(BaseLLMProvider):
    provider_name = "stub"
    context_window = 100_000

    def __init__(self, script):
        self._script = script
        self.call_count = 0
        self.offered_tools: list[list[str]] = []
        self.seen_messages: list[list] = []

    async def chat(self, messages, model=None, tools=None, **kwargs):
        self.offered_tools.append([t.name for t in (tools or [])])
        self.seen_messages.append(list(messages))
        response = self._script[min(self.call_count, len(self._script) - 1)]
        self.call_count += 1
        return response

    async def chat_stream(self, messages, **kwargs):
        yield ""

    async def embed(self, text, model=None):
        return [0.0]

    async def list_models(self):
        return []


def _wait_call(call_id: str) -> ChatResponse:
    return ChatResponse(
        content="",
        model="stub",
        stop_reason="tool_calls",
        tool_calls=[ToolCall(id=call_id, name="wait", arguments={"duration": "10m"})],
    )


def _dummy_executor() -> tuple[list[ToolDefinition], CompositeToolExecutor]:
    inner = CallableToolExecutor()

    async def _noop() -> str:
        return "ok"

    inner.register("noop", _noop)
    composite = CompositeToolExecutor()
    definitions = [
        ToolDefinition(
            name="noop",
            description="Does nothing",
            input_schema={"type": "object", "properties": {}},
            source="builtin",
        )
    ]
    composite.add_tools(definitions, inner)
    return definitions, composite


@pytest.fixture
def _quiet_langfuse(monkeypatch):
    monkeypatch.setattr(al, "_lf_start_llm_gen", lambda *a, **kw: None)
    monkeypatch.setattr(al, "_lf_end_generation", lambda *a, **kw: None)
    monkeypatch.setattr(al, "_lf_end_span", lambda *a, **kw: None)
    monkeypatch.setattr(al, "_lf_end_tool_span", lambda *a, **kw: None)
    monkeypatch.setattr(al, "_lf_start_iter_span", lambda **kw: None)
    monkeypatch.setattr(al, "_lf_start_tool_span", lambda **kw: None)


async def test_loop_offers_the_wait_tool_and_does_not_charge_a_wait_round(
    monkeypatch, _quiet_langfuse
):
    """The mechanic that motivated the feature: two pauses on a 2-iteration
    budget still leave room for the real work, because a round that only waited
    is refunded.  Without the waiver the second wait would exhaust the budget
    and the loop would be pushed to a final answer with the delegated work
    still running.
    """
    # Keep the test fast without weakening what it checks: the pause is real,
    # just short (the policy clamps 10m to 1s), and the "did it actually pause"
    # threshold is scaled to match.
    monkeypatch.setattr(wp, "MIN_GRANTED_WAIT_SECONDS", 0.05)
    monkeypatch.setattr(al, "MIN_GRANTED_WAIT_SECONDS", 0.05)

    definitions, executor = _dummy_executor()
    llm = _ScriptedLLM(
        [
            _wait_call("wait-1"),
            _wait_call("wait-2"),
            ChatResponse(content="All done.", model="stub", stop_reason="stop"),
        ]
    )
    messages = [ChatMessage(role="user", content="delegate then wait")]

    response = await al.run_agent_loop(
        llm=llm,
        messages=messages,
        tools=definitions,
        executor=executor,
        max_iterations=2,
        wait_policy=wp.WaitPolicy(per_call_seconds=1, total_seconds=600),
    )

    assert response.content == "All done."
    # The timer is offered by the loop itself — no call site has to add it.
    assert "wait" in llm.offered_tools[0]
    # ...and every agent is told how to use it, exactly once per run.
    wait_prompts = [
        m
        for m in messages
        if m.role == "system" and "[Waiting]" in (m.content or "")
    ]
    assert len(wait_prompts) == 1
    # Three LLM calls on a 2-iteration budget: both wait rounds were refunded.
    assert llm.call_count == 3
    wait_results = [m for m in messages if m.role == "tool" and m.name == "wait"]
    assert len(wait_results) == 2
    assert all("Waited" in (m.content or "") for m in wait_results)


async def test_loop_omits_the_wait_tool_when_the_policy_disables_it(
    _quiet_langfuse,
):
    """The public embed surface must not be able to hold a worker idle."""
    definitions, executor = _dummy_executor()
    llm = _ScriptedLLM([ChatResponse(content="hi", model="stub", stop_reason="stop")])

    await al.run_agent_loop(
        llm=llm,
        messages=[ChatMessage(role="user", content="hello")],
        tools=definitions,
        executor=executor,
        max_iterations=2,
        wait_policy=wp.WaitPolicy.disabled(),
    )

    assert llm.offered_tools[0] == ["noop"]
    # No timer, no guidance about one.
    assert not any("[Waiting]" in (m.content or "") for m in llm.seen_messages[0])


async def test_a_run_that_owns_a_tool_named_wait_keeps_it(_quiet_langfuse):
    """An MCP server or custom tool may own the name ``wait``.

    The built-in is then not offered — and, critically, the loop must NOT
    intercept the call either: swallowing it would make the run's own tool
    unreachable while looking like a successful pause.
    """
    executed: list[dict] = []
    inner = CallableToolExecutor()

    async def _their_wait(reason: str = "") -> str:
        executed.append({"reason": reason})
        return "their wait ran"

    inner.register("wait", _their_wait)
    definitions = [
        ToolDefinition(
            name="wait",
            description="An MCP server's own wait tool",
            input_schema={
                "type": "object",
                "properties": {"reason": {"type": "string"}},
            },
            source="mcp:Their Server",
        )
    ]
    composite = CompositeToolExecutor()
    composite.add_tools(definitions, inner)

    llm = _ScriptedLLM(
        [
            ChatResponse(
                content="",
                model="stub",
                stop_reason="tool_calls",
                tool_calls=[
                    ToolCall(id="t1", name="wait", arguments={"reason": "theirs"})
                ],
            ),
            ChatResponse(content="Done.", model="stub", stop_reason="stop"),
        ]
    )
    messages = [ChatMessage(role="user", content="use your wait tool")]

    await al.run_agent_loop(
        llm=llm,
        messages=messages,
        tools=definitions,
        executor=composite,
        max_iterations=3,
    )

    assert executed == [{"reason": "theirs"}], (
        "the run's own 'wait' tool must execute — the built-in primitive stands "
        "aside when the name is already taken"
    )
    assert llm.offered_tools[0] == ["wait"], "no second 'wait' is offered"
    assert not any("[Waiting]" in (m.content or "") for m in messages)
    tool_results = [m for m in messages if m.role == "tool"]
    assert tool_results and tool_results[0].content == "their wait ran"


# ── Wait-budget cliff / hand-off (#wait-budget-continuation) ───────────────

_PENDING_TASK_ID = "11111111-1111-4111-8111-111111111111"


def test_parse_until_task_ids_keeps_valid_uuids_in_order():
    assert wp.parse_until_task_ids(None) == []
    assert wp.parse_until_task_ids("not-a-uuid") == []
    assert wp.parse_until_task_ids(_PENDING_TASK_ID) == [_PENDING_TASK_ID]
    assert wp.parse_until_task_ids(
        f"{_PENDING_TASK_ID}, not-a-uuid, 22222222-2222-4222-8222-222222222222"
    ) == [_PENDING_TASK_ID, "22222222-2222-4222-8222-222222222222"]
    assert wp.parse_until_task_ids([_PENDING_TASK_ID, "nope"]) == [_PENDING_TASK_ID]


async def test_execute_wait_handoffs_when_budget_is_spent_with_until():
    """A spent budget plus named children is a hand-off, not a retryable refusal."""
    result = await wp.execute_wait(
        {"duration": "1m", "until": _PENDING_TASK_ID},
        budget=_spent_budget(),
    )
    assert result.reason == wp.WAIT_REASON_HANDOFF
    assert _PENDING_TASK_ID in result.message
    assert "Do not retry" in result.message
    assert not result.paused


def _wait_call_until(call_id: str, until: str) -> ChatResponse:
    return ChatResponse(
        content="",
        model="stub",
        stop_reason="tool_calls",
        tool_calls=[
            ToolCall(
                id=call_id,
                name="wait",
                arguments={"duration": "10m", "until": until},
            )
        ],
    )


async def test_loop_exits_waiting_on_tasks_instead_of_digesting_wait(
    monkeypatch, _quiet_langfuse
):
    """The production failure: last useful pause spent, child still running.

    The loop must return ``waiting_on_tasks`` after the one wait round, with
    an honest message, and must NOT spend another LLM call that would then
    be digested as Wait / Wait / Wait.
    """
    monkeypatch.setattr(wp, "MIN_GRANTED_WAIT_SECONDS", 0.05)
    monkeypatch.setattr(al, "MIN_GRANTED_WAIT_SECONDS", 0.05)

    async def _still_running(ids):
        return False, {ids[0]: "running"}

    definitions, executor = _dummy_executor()
    llm = _ScriptedLLM(
        [
            _wait_call_until("wait-1", _PENDING_TASK_ID),
            ChatResponse(
                content="should not run", model="stub", stop_reason="stop"
            ),
        ]
    )
    messages = [ChatMessage(role="user", content="wait for the child")]

    response = await al.run_agent_loop(
        llm=llm,
        messages=messages,
        tools=definitions,
        executor=executor,
        max_iterations=5,
        wait_policy=wp.WaitPolicy(per_call_seconds=1, total_seconds=1),
        wait_condition=_still_running,
    )

    assert response.stop_reason == "waiting_on_tasks"
    assert response.pending_task_ids == [_PENDING_TASK_ID]
    assert "I wasn't able to finish" not in (response.content or "")
    assert "Waited" not in (response.content or "")
    # The id travels structurally (``pending_task_ids``); the user-facing text
    # never quotes it. Nothing in this transcript or condition can
    # name the task, so it is counted instead.
    assert _PENDING_TASK_ID not in (response.content or "")
    assert "1 background task is still running" in (response.content or "")
    # The park is a clean auto-resume, not a manual dead-end: the
    # message must promise automatic continuation, never ask the user to
    # re-prompt or say the work "wasn't finished".
    assert "automatically" in (response.content or "")
    assert "still running" in (response.content or "")
    assert "Ask me to continue" not in (response.content or "")
    assert llm.call_count == 1


async def test_loop_answers_user_instead_of_reparking_on_prior_turn_tasks(
    monkeypatch, _quiet_langfuse
):
    """A fresh interactive user turn that re-waits on PRE-EXISTING
    background tasks must answer the user when the wait budget dies — not
    swallow the turn with another canned "Background work is still running"
    park. The model is forced into one tool-free round so it must reply."""
    monkeypatch.setattr(wp, "MIN_GRANTED_WAIT_SECONDS", 0.05)
    monkeypatch.setattr(al, "MIN_GRANTED_WAIT_SECONDS", 0.05)

    async def _still_running(ids):
        return False, {tid: "running" for tid in ids}

    definitions, executor = _dummy_executor()
    llm = _ScriptedLLM(
        [
            _wait_call_until("wait-1", _PENDING_TASK_ID),
            ChatResponse(
                content="Answering you now; the background task is still running.",
                model="stub",
                stop_reason="stop",
            ),
        ]
    )
    # The watched task was spawned in a PRIOR turn (before the last user
    # message): this turn did not launch it, the user is just talking to a
    # conversation that previously parked.
    messages = [
        ChatMessage(role="user", content="spawn something"),
        ChatMessage(
            role="tool",
            name="spawn_background_task",
            content=(
                "Background task spawned successfully.\n"
                f"Task ID: {_PENDING_TASK_ID}\n"
            ),
        ),
        ChatMessage(
            role="assistant",
            content="Incomplete — Background work is still running.",
        ),
        ChatMessage(role="user", content="hello? are you still there?"),
    ]

    response = await al.run_agent_loop(
        llm=llm,
        messages=messages,
        tools=definitions,
        executor=executor,
        max_iterations=5,
        wait_policy=wp.WaitPolicy(per_call_seconds=1, total_seconds=1),
        wait_condition=_still_running,
        # Interactive chat turn: a human is waiting for a reply.
        steering_queue=asyncio.Queue(),
    )

    assert response.stop_reason != "waiting_on_tasks"
    assert (
        response.content
        == "Answering you now; the background task is still running."
    )
    # One wait round + one forced final-answer round.
    assert llm.call_count == 2
    # The final round ran tool-free, so the model could not re-wait.
    assert llm.offered_tools[1] == []
    # The deterministic nudge named the still-running task and told the model
    # to answer the user now.
    nudges = [
        m
        for m in messages
        if m.role == "system"
        and _PENDING_TASK_ID in (m.content or "")
        and "answer" in (m.content or "")
    ]
    assert nudges, "the answer-after-park nudge must be injected"


async def test_loop_still_parks_when_the_turn_spawned_its_own_tasks(
    monkeypatch, _quiet_langfuse
):
    """The guard must not touch the intended park: a turn that spawned
    its OWN background work and hit the wait-budget cliff still parks with the
    canned notice, even in an interactive chat."""
    monkeypatch.setattr(wp, "MIN_GRANTED_WAIT_SECONDS", 0.05)
    monkeypatch.setattr(al, "MIN_GRANTED_WAIT_SECONDS", 0.05)

    async def _still_running(ids):
        return False, {tid: "running" for tid in ids}

    definitions, executor = _dummy_executor()
    llm = _ScriptedLLM(
        [
            _wait_call_until("wait-1", _PENDING_TASK_ID),
            ChatResponse(
                content="should not run", model="stub", stop_reason="stop"
            ),
        ]
    )
    # The spawn happened THIS turn (after the last user message).
    messages = [
        ChatMessage(role="user", content="do the work"),
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
                f"Task ID: {_PENDING_TASK_ID}\n"
                "Target agent: x\nStatus: pending\n"
            ),
        ),
    ]

    response = await al.run_agent_loop(
        llm=llm,
        messages=messages,
        tools=definitions,
        executor=executor,
        max_iterations=5,
        wait_policy=wp.WaitPolicy(per_call_seconds=1, total_seconds=1),
        wait_condition=_still_running,
        steering_queue=asyncio.Queue(),
    )

    assert response.stop_reason == "waiting_on_tasks"
    assert response.pending_task_ids == [_PENDING_TASK_ID]
    assert llm.call_count == 1


def test_wait_continuation_prompt_tells_it_to_wait_for_running_children():
    """The continuation must keep waiting for a genuinely-running delegation,
    not do a one-shot status check and give up with a partial answer."""
    from app.services.sub_agent import _wait_continuation_prompt

    text = _wait_continuation_prompt("Investigate the leak", [_PENDING_TASK_ID])
    assert "[Platform continuation]" in text
    assert _PENDING_TASK_ID in text
    assert "Investigate the leak" in text
    assert "wait with until set to their IDs" in text
    assert "do NOT skip the wait" in text
    assert "partial answer" in text


# ── Loop contract: park / protocol / extracts / unknown ids ────────


async def test_execute_wait_condition_injects_post_wait_protocol(monkeypatch):
    """A CONDITION wake-up must tell the model to fetch and synthesise."""
    monkeypatch.setattr(wp, "MIN_GRANTED_WAIT_SECONDS", 0.01)
    polls = {"n": 0}

    async def _condition(ids):
        polls["n"] += 1
        if polls["n"] == 1:
            return False, {ids[0]: "running"}
        return True, {ids[0]: "completed"}

    result = await wp.execute_wait(
        {"duration": "5s", "until": _PENDING_TASK_ID},
        budget=wp.WaitBudget(wp.WaitPolicy(per_call_seconds=5, total_seconds=30)),
        condition=_condition,
        poll_interval=0.05,
    )
    assert result.reason == wp.WAIT_REASON_CONDITION
    assert "everything you were waiting for has finished" in result.message
    assert "get_task_result" in result.message
    assert "Do not end the turn with a checkpoint" in result.message


async def test_execute_wait_already_done_appends_result_extracts():
    """Completion extracts belong on the waiting transcript, not a sibling bubble."""

    async def _condition(ids):
        return True, {ids[0]: "completed"}

    async def _extracts(ids):
        return {ids[0]: "the leak is in foo.py"}

    _condition.extracts = _extracts  # type: ignore[attr-defined]
    result = await wp.execute_wait(
        {"duration": "5m", "until": _PENDING_TASK_ID},
        budget=wp.WaitBudget(),
        condition=_condition,
    )
    assert result.reason == wp.WAIT_REASON_ALREADY_DONE
    assert "the leak is in foo.py" in result.message
    assert "Extract from" in result.message


async def test_make_task_condition_unknown_ids_stay_pending(monkeypatch):
    """A typo or other-user id must not look like 'already done'."""

    async def _statuses(task_ids, user_id):
        return {tid: None for tid in task_ids}

    monkeypatch.setattr("app.services.sub_agent.get_task_statuses", _statuses)
    cond = make_task_condition("aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa")
    assert cond is not None
    done, statuses = await cond([_PENDING_TASK_ID])
    assert done is False
    assert statuses[_PENDING_TASK_ID] == "unknown"


async def test_make_task_condition_mixed_completed_and_unknown_stays_pending(
    monkeypatch,
):
    done_id = "22222222-2222-4222-8222-222222222222"

    async def _statuses(task_ids, user_id):
        out: dict = {}
        for tid in task_ids:
            out[tid] = {"status": "completed"} if tid == done_id else None
        return out

    monkeypatch.setattr("app.services.sub_agent.get_task_statuses", _statuses)
    cond = make_task_condition("aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa")
    assert cond is not None
    done, statuses = await cond([done_id, _PENDING_TASK_ID])
    assert done is False
    assert statuses[done_id] == "completed"
    assert statuses[_PENDING_TASK_ID] == "unknown"


async def test_execute_wait_unknown_until_ids_are_not_already_done(monkeypatch):
    monkeypatch.setattr(wp, "MIN_GRANTED_WAIT_SECONDS", 0.05)

    async def _unknown(ids):
        return False, {ids[0]: "unknown"}

    result = await wp.execute_wait(
        {"duration": "1s", "until": _PENDING_TASK_ID},
        budget=wp.WaitBudget(wp.WaitPolicy(per_call_seconds=1, total_seconds=30)),
        condition=_unknown,
        poll_interval=0.05,
    )
    assert result.reason != wp.WAIT_REASON_ALREADY_DONE
    assert result.reason == wp.WAIT_REASON_ELAPSED


def test_waiting_for_narration_is_a_stall_when_open_task_ids_exist():
    text = "I launched the investigations, waiting for the background tasks."
    assert al._looks_like_unfinished_action(text, open_task_ids=[_PENDING_TASK_ID])
    # Without open ids, "waiting for" is still a genuine handback.
    assert not al._looks_like_unfinished_action(text)


def test_collect_spawned_task_ids_parses_spawn_tool_results():
    messages = [
        ChatMessage(role="user", content="go"),
        ChatMessage(
            role="tool",
            name="spawn_background_task",
            content=(
                "Background task spawned successfully.\n"
                f"Task ID: {_PENDING_TASK_ID}\n"
                "Target agent: helper\nStatus: pending\n"
            ),
        ),
        ChatMessage(
            role="tool",
            name="other_tool",
            content=f"Task ID: {_PENDING_TASK_ID}\nignored",
        ),
    ]
    assert al.collect_spawned_task_ids(messages) == [_PENDING_TASK_ID]


async def test_loop_parks_on_tool_free_exit_while_spawned_tasks_run(
    monkeypatch, _quiet_langfuse
):
    """A tool-free 'I launched work, waiting' must not end the turn.

    The first condition poll (implicit-wait live-id check) reports running;
    the execute_wait already-done check then reports completed so the test
    does not sleep a real pause.
    """
    monkeypatch.setattr(wp, "MIN_GRANTED_WAIT_SECONDS", 0.05)
    monkeypatch.setattr(al, "MIN_GRANTED_WAIT_SECONDS", 0.05)
    polls = {"n": 0}

    async def _condition(ids):
        polls["n"] += 1
        if polls["n"] == 1:
            return False, {ids[0]: "running"}
        return True, {ids[0]: "completed"}

    definitions, executor = _dummy_executor()
    llm = _ScriptedLLM(
        [
            ChatResponse(
                content="I launched the work, waiting for the background tasks.",
                model="stub",
                stop_reason="stop",
            ),
            ChatResponse(content="All done.", model="stub", stop_reason="stop"),
        ]
    )
    messages = [
        ChatMessage(role="user", content="do the work"),
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
                f"Task ID: {_PENDING_TASK_ID}\n"
                "Target agent: x\nStatus: pending\n"
            ),
        ),
    ]

    response = await al.run_agent_loop(
        llm=llm,
        messages=messages,
        tools=definitions,
        executor=executor,
        max_iterations=5,
        wait_policy=wp.WaitPolicy(per_call_seconds=1, total_seconds=30),
        wait_condition=_condition,
    )
    assert response.content == "All done."
    assert llm.call_count == 2
    wait_tools = [
        m for m in messages if m.role == "tool" and m.name == "wait"
    ]
    assert wait_tools, "implicit wait must be injected before a tool-free exit"
    assert "get_task_result" in (wait_tools[0].content or "")
    assert "I launched the work" not in (response.content or "")
    # The model's narration is preserved on the injected assistant message
    # (transcript continuity — the next round sees what it said).
    implicit_assistant = [
        m
        for m in messages
        if m.role == "assistant"
        and m.tool_calls
        and any(tc.name == "wait" for tc in m.tool_calls)
    ]
    assert implicit_assistant
    assert "I launched the work" in (implicit_assistant[0].content or "")


async def test_final_answer_with_only_never_started_children_ends_the_run(
    monkeypatch: pytest.MonkeyPatch, _quiet_langfuse: None
) -> None:
    """A child still never started after one whole park no longer holds the run.

    The first final answer parks once (a child is ``pending`` for the moments
    before a worker picks it up, and a delegating run stays open until its
    child finishes). When the child is still ``pending`` after that park, the
    next final answer ends the run and names the child left running on its own.
    """
    monkeypatch.setattr(wp, "MIN_GRANTED_WAIT_SECONDS", 0.05)
    monkeypatch.setattr(al, "MIN_GRANTED_WAIT_SECONDS", 0.05)

    async def _condition(ids: list[str]) -> tuple[bool, dict[str, str]]:
        return False, dict.fromkeys(ids, "pending")

    definitions, executor = _dummy_executor()
    llm = _ScriptedLLM(  # type: ignore[no-untyped-call]
        [
            ChatResponse(content="Report: launched.", model="stub", stop_reason="stop"),
            ChatResponse(content='{"verdict": "done"}', model="stub", stop_reason="stop"),
        ]
    )
    messages = [
        ChatMessage(role="user", content="do the work"),
        ChatMessage(
            role="assistant",
            content="",
            tool_calls=[ToolCall(id="s1", name="spawn_background_task", arguments={"agent_id": "x", "task": "go"})],
        ),
        ChatMessage(
            role="tool",
            name="spawn_background_task",
            tool_call_id="s1",
            content=(
                f"Background task spawned successfully.\nTask ID: {_PENDING_TASK_ID}\nTarget agent: x\nStatus: pending\n"
            ),
        ),
    ]

    response = await al.run_agent_loop(
        llm=llm,
        messages=messages,
        tools=definitions,
        executor=executor,
        max_iterations=5,
        wait_policy=wp.WaitPolicy(per_call_seconds=1, total_seconds=30),
        wait_condition=_condition,
    )
    assert response.content == '{"verdict": "done"}'
    assert llm.call_count == 2
    assert response.stop_reason != "waiting_on_tasks"
    assert response.pending_task_ids == [_PENDING_TASK_ID]
    assert len([m for m in messages if m.role == "tool" and m.name == "wait"]) == 1


@pytest.mark.parametrize(
    ("statuses", "answer", "expected"),
    [
        ({"a": "pending"}, "Report: done.", ["a"]),
        ({"a": "pending", "b": "running"}, "Report: done.", []),
        ({"a": "pending"}, "Still waiting for the analyst.", []),
        ({}, "Report: done.", []),
        ({"a": "pending"}, "  ", []),
    ],
)
def test_never_started_task_ids(statuses: dict[str, str], answer: str, expected: list[str]) -> None:
    """Only an all-pending set under a deliverable answer is left behind."""
    assert al.never_started_task_ids(["a", *(["b"] if "b" in statuses else [])], statuses, answer) == expected


async def test_implicit_park_announces_wait_start_to_the_ui(
    monkeypatch, _quiet_langfuse
):
    """The implicit spawned-task park must emit ``wait_start``.

    The explicit ``wait`` tool announces its settled grant so the chat shows
    the "Waiting: {reason}" indicator (countdown + skip button).  The
    implicit park used to pause silently, leaving the indicator on a stale
    "Thinking…" for the whole wait.  The pause must announce itself with the
    same event and payload shape — the frontend needs no new event type.
    """
    monkeypatch.setattr(wp, "MIN_GRANTED_WAIT_SECONDS", 0.05)
    monkeypatch.setattr(al, "MIN_GRANTED_WAIT_SECONDS", 0.05)
    polls = {"n": 0}

    async def _condition(ids):
        polls["n"] += 1
        # Poll 1: the loop's live-id check.  Poll 2: execute_wait's entry
        # check (must still be running so the pause — and the announce —
        # really happen).  Poll 3+ (inside the pause): completed.
        if polls["n"] <= 2:
            return False, {ids[0]: "running"}
        return True, {ids[0]: "completed"}

    events: list[dict] = []

    async def _on_event(event: dict) -> None:
        events.append(dict(event))

    definitions, executor = _dummy_executor()
    llm = _ScriptedLLM(
        [
            ChatResponse(
                content="I launched the work, waiting for the background tasks.",
                model="stub",
                stop_reason="stop",
            ),
            ChatResponse(content="All done.", model="stub", stop_reason="stop"),
        ]
    )
    messages = [
        ChatMessage(role="user", content="do the work"),
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
                f"Task ID: {_PENDING_TASK_ID}\n"
                "Target agent: x\nStatus: pending\n"
            ),
        ),
    ]

    response = await al.run_agent_loop(
        llm=llm,
        messages=messages,
        tools=definitions,
        executor=executor,
        max_iterations=5,
        wait_policy=wp.WaitPolicy(per_call_seconds=1, total_seconds=30),
        wait_condition=_condition,
        on_event=_on_event,
    )
    assert response.content == "All done."
    wait_starts = [e for e in events if e.get("status") == "wait_start"]
    assert wait_starts, "implicit park must announce itself with wait_start"
    announce = wait_starts[0]
    assert announce["tool"] == "wait"
    assert announce["reason"] == "open background tasks"
    assert announce["until"] == _PENDING_TASK_ID
    assert announce["seconds"] > 0


async def test_execute_wait_yields_to_pending_input_before_pausing():
    """A queued steering message outranks any pause.

    The steering skip event is set when the message arrives; when that happens
    while the model is mid-generation the stray-skip guard would drop it, so
    ``pending_input`` must refuse the wait up front — no pause, no budget
    charge, and the skip that belonged to the message is consumed.
    """
    skip = asyncio.Event()
    skip.set()  # set by the steering enqueue, with no pause in flight
    budget = wp.WaitBudget(wp.WaitPolicy(per_call_seconds=30, total_seconds=600))
    result = await wp.execute_wait(
        {"duration": "5m", "until": _PENDING_TASK_ID},
        budget=budget,
        skip_event=skip,
        pending_input=lambda: True,
    )
    assert result.reason == wp.WAIT_REASON_STEERED
    assert "the user sent a new message" in result.message
    assert not result.paused
    assert budget.spent_seconds == 0, "nothing was waited, nothing is charged"
    assert not skip.is_set(), "the message's skip is consumed, not leaked"


async def test_execute_wait_pending_input_ends_a_running_pause(monkeypatch):
    """A steering message that lands mid-pause ends the pause at the next poll
    even if its skip event was lost."""
    monkeypatch.setattr(wp, "MIN_GRANTED_WAIT_SECONDS", 0.05)
    calls = {"n": 0}

    def _pending() -> bool:
        calls["n"] += 1
        # Call 1 is the entry check (nothing queued yet); the message arrives
        # during the first sleep slice.
        return calls["n"] >= 2

    async def _condition(ids):
        return False, {ids[0]: "running"}

    budget = wp.WaitBudget(wp.WaitPolicy(per_call_seconds=30, total_seconds=600))
    result = await wp.execute_wait(
        {"duration": "5m", "until": _PENDING_TASK_ID},
        budget=budget,
        condition=_condition,
        poll_interval=0.01,
        pending_input=_pending,
    )
    assert result.reason == wp.WAIT_REASON_STEERED
    assert result.elapsed < 5, "the pending message must cut the pause short"
    assert "the user sent a new message" in result.message


async def test_execute_wait_ends_a_duration_only_pause_when_the_worker_stops():
    """A checkpointed turn's pause ends once its worker winds down.

    The call completes with ``worker_stopping`` instead of being cancelled by
    the drain, so it is checkpointed and the attempt that resumes the run
    counts it as progress.
    """
    calls = {"n": 0}

    def _stopping() -> bool:
        calls["n"] += 1
        return calls["n"] >= 3

    budget = wp.WaitBudget(wp.WaitPolicy(per_call_seconds=300, total_seconds=1800))
    result = await wp.execute_wait(
        {"duration": "5m"},
        budget=budget,
        poll_interval=0.01,
        stopping=_stopping,
    )
    assert result.reason == wp.WAIT_REASON_WORKER_STOPPING
    assert result.elapsed < 5, "a stopping worker must cut the pause short"
    assert "Wait cut short after" in result.message
    assert "wait again there" in result.message


async def test_execute_wait_ends_a_condition_pause_when_the_worker_stops():
    """A wait on ``until`` ends on a stopping worker too, before its next poll."""
    stop = {"now": False}

    async def _condition(ids):
        stop["now"] = True
        return False, {ids[0]: "running"}

    budget = wp.WaitBudget(wp.WaitPolicy(per_call_seconds=300, total_seconds=1800))
    result = await wp.execute_wait(
        {"duration": "5m", "until": _PENDING_TASK_ID},
        budget=budget,
        condition=_condition,
        poll_interval=0.01,
        stopping=lambda: stop["now"],
    )
    assert result.reason == wp.WAIT_REASON_WORKER_STOPPING
    assert result.elapsed < 5


async def test_execute_wait_without_a_stopping_probe_sleeps_as_before():
    """No probe (a turn with no checkpoint): the pause is not sliced for it."""
    budget = wp.WaitBudget(wp.WaitPolicy(per_call_seconds=300, total_seconds=1800))
    result = await wp.execute_wait({"duration": "1s"}, budget=budget, poll_interval=0.01)
    assert result.reason == wp.WAIT_REASON_ELAPSED


async def test_execute_wait_pending_input_ends_a_duration_only_pause():
    """A steering message that lands mid-pause ends a duration-only wait too.

    Some callers wire a steering queue with NO skip event (the platform
    embedded chat proxy, autonomous runs): for a ``wait`` call without
    ``until`` the pending-input poll is then the only thing that can cut the
    sleep short, so the duration-only branch must slice and poll exactly like
    the condition branch.
    """
    calls = {"n": 0}

    def _pending() -> bool:
        calls["n"] += 1
        # Call 1 is the entry check (nothing queued yet); the message arrives
        # during the first sleep slice.
        return calls["n"] >= 2

    budget = wp.WaitBudget(wp.WaitPolicy(per_call_seconds=30, total_seconds=600))
    result = await wp.execute_wait(
        {"duration": "5m"},
        budget=budget,
        poll_interval=0.01,
        pending_input=_pending,
    )
    assert result.reason == wp.WAIT_REASON_STEERED
    assert result.elapsed < 5, "the pending message must cut the pause short"
    assert "the user sent a new message" in result.message


async def test_execute_wait_calls_refuse_the_pause_when_input_is_pending():
    """The explicit ``wait`` tool call path threads ``pending_input`` through:
    the tool result reports the steered refusal instead of pausing."""
    messages: list = []
    tracked: list = []
    budget = wp.WaitBudget(wp.WaitPolicy(per_call_seconds=30, total_seconds=600))
    paused, handoff_ids, elapsed = await al._execute_wait_calls(
        [ToolCall(id="w1", name="wait", arguments={"duration": "5m"})],
        iteration=0,
        messages=messages,
        on_event=None,
        wait_budget=budget,
        wall_clock_deadline=None,
        wait_skip_event=None,
        wait_condition=None,
        cancel_event=None,
        track_tool_result=lambda *a, **kw: tracked.append(a),
        pending_input=lambda: True,
    )
    assert not paused
    assert handoff_ids == []
    assert elapsed == 0
    assert len(messages) == 1
    assert "the user sent a new message" in (messages[0].content or "")


async def test_loop_implicit_wait_yields_to_pending_steering(
    monkeypatch, _quiet_langfuse
):
    """Steering during a spawned-task park must be answered, not slept on.

    The agent spawned a background task and gave a tool-free
    "waiting" answer, so the loop parks in an implicit wait. A user message
    arrives (its skip event may already have been consumed) — the pause must
    be refused, the message injected at the next iteration boundary, and the
    model's reply to it returned instead of another silent wait slice.
    """
    monkeypatch.setattr(wp, "MIN_GRANTED_WAIT_SECONDS", 0.05)
    monkeypatch.setattr(al, "MIN_GRANTED_WAIT_SECONDS", 0.05)
    steering_queue: asyncio.Queue = asyncio.Queue()
    polls = {"n": 0}

    async def _condition(ids):
        # The task never finishes during this test. The first poll is the
        # park's live-id check: the user's message lands exactly then —
        # after the iteration-start drain, before the implicit wait.
        polls["n"] += 1
        if polls["n"] == 1:
            steering_queue.put_nowait("What are you waiting for?")
        return False, {ids[0]: "running"}

    definitions, executor = _dummy_executor()
    llm = _ScriptedLLM(
        [
            ChatResponse(
                content="I launched the work, waiting for the background tasks.",
                model="stub",
                stop_reason="stop",
            ),
            ChatResponse(
                content="Here is what is happening right now.",
                model="stub",
                stop_reason="stop",
            ),
        ]
    )
    messages = [
        ChatMessage(role="user", content="do the work"),
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
                f"Task ID: {_PENDING_TASK_ID}\n"
                "Target agent: x\nStatus: pending\n"
            ),
        ),
    ]

    response = await al.run_agent_loop(
        llm=llm,
        messages=messages,
        tools=definitions,
        executor=executor,
        max_iterations=5,
        # Small caps so a REGRESSION fails fast (a slept slice shows up in the
        # wait tool result assertion below) instead of hanging a full 5m park.
        wait_policy=wp.WaitPolicy(per_call_seconds=5, total_seconds=30),
        wait_condition=_condition,
        steering_queue=steering_queue,
    )
    assert response.content == "Here is what is happening right now."
    assert llm.call_count == 2
    # The implicit wait was refused because the message was pending — its tool
    # result says so instead of reporting a slept slice.
    wait_tools = [m for m in messages if m.role == "tool" and m.name == "wait"]
    assert wait_tools
    assert "the user sent a new message" in (wait_tools[0].content or "")
    # The steered message reached the model on the second call.
    steered_seen = [
        m
        for m in llm.seen_messages[1]
        if m.role == "user" and m.content == "What are you waiting for?"
    ]
    assert steered_seen, "the steered message must be injected before the reply"


def test_collect_spawned_task_ids_ignores_prior_turns():
    """Chat history rehydrates old tool messages: a task spawned three turns
    ago must not park (or retract) an unrelated later answer."""
    old_spawn = ChatMessage(
        role="tool",
        name="spawn_background_task",
        content=f"Background task spawned successfully.\nTask ID: {_PENDING_TASK_ID}\n",
    )
    messages = [
        ChatMessage(role="user", content="spawn something"),
        old_spawn,
        ChatMessage(role="assistant", content="Launched."),
        ChatMessage(role="user", content="what's 2+2?"),
    ]
    assert al.collect_spawned_task_ids(messages) == []

    fresh_id = "22222222-2222-4222-8222-222222222222"
    messages.append(
        ChatMessage(
            role="tool",
            name="spawn_background_task",
            content=f"Background task spawned successfully.\nTask ID: {fresh_id}\n",
        )
    )
    assert al.collect_spawned_task_ids(messages) == [fresh_id]


# ── Park message: tasks named, never ids ──────────────────────────

_OTHER_TASK_ID = "33333333-3333-4333-8333-333333333333"


def _spawn_exchange(
    call_id: str, task_id: str, *, agent: str, task: str
) -> list[ChatMessage]:
    """An assistant spawn call paired with its tool result."""
    return [
        ChatMessage(
            role="assistant",
            content="",
            tool_calls=[
                ToolCall(
                    id=call_id,
                    name="spawn_background_task",
                    arguments={"agent_id": "irrelevant-slug", "task": task},
                )
            ],
        ),
        ChatMessage(
            role="tool",
            name="spawn_background_task",
            tool_call_id=call_id,
            content=(
                "Background task spawned successfully.\n"
                f"Task ID: {task_id}\n"
                f"Target agent: {agent}\n"
                "Status: pending\n"
                "Use check_task_status(task_ids) or get_task_result(task_ids) to monitor."
            ),
        ),
    ]


def test_collect_task_labels_pairs_spawn_call_with_result_across_turns():
    """The description comes from the tool CALL (``task`` argument), the agent
    from the tool RESULT (``Target agent:``), paired by ``tool_call_id`` — and
    prior turns count: a park can wait on a task spawned turns ago."""
    messages = [
        ChatMessage(role="user", content="run the fidelity analysis"),
        *_spawn_exchange(
            "s1",
            _PENDING_TASK_ID,
            agent="Research Analyst",
            task="Run the external leak analysis for Fidelity",
        ),
        ChatMessage(role="assistant", content="Launched."),
        ChatMessage(role="user", content="and the PDFs?"),
        *_spawn_exchange(
            "s2", _OTHER_TASK_ID, agent="yourself (parallel copy)", task="Build the PDFs"
        ),
    ]
    labels = al.collect_task_labels(messages)
    assert labels[_PENDING_TASK_ID] == al.TaskLabel(
        agent_name="Research Analyst",
        task_description="Run the external leak analysis for Fidelity",
    )
    # The model-facing self-copy wording is not a name the user knows — and
    # the omission is recorded as deliberate, so the database fallback does
    # not spend a query re-discovering the calling agent's own name.
    assert labels[_OTHER_TASK_ID] == al.TaskLabel(
        agent_name=None, task_description="Build the PDFs", agent_omitted=True
    )
    assert labels[_OTHER_TASK_ID].complete
    assert labels[_PENDING_TASK_ID].complete


def test_collect_task_labels_survives_a_pruned_call_or_result():
    """A result with no matching call still yields the agent; a result pruned
    to a stub (no ``Task ID:``) yields nothing rather than a bogus entry."""
    messages = [
        ChatMessage(
            role="tool",
            name="spawn_background_task",
            tool_call_id="gone",
            content=(
                "Background task spawned successfully.\n"
                f"Task ID: {_PENDING_TASK_ID}\nTarget agent: Helper\n"
            ),
        ),
        ChatMessage(
            role="tool",
            name="spawn_background_task",
            tool_call_id="s2",
            content="[Pruned tool result — originally 240 chars. Content omitted.]",
        ),
    ]
    labels = al.collect_task_labels(messages)
    assert labels == {
        _PENDING_TASK_ID: al.TaskLabel(agent_name="Helper", task_description=None)
    }
    # Known enough to render, but NOT complete: the description is still owed.
    assert labels[_PENDING_TASK_ID].known
    assert not labels[_PENDING_TASK_ID].complete


def test_collect_task_labels_reads_a_multi_line_agent_name_whole():
    """An agent name is free text with no one-line constraint. The spawn
    result's ``Target agent:`` value runs to the ``Status:`` field, so a
    name holding a newline is captured whole (and flattened on render), not
    cut at the first line - which, with the description present, would have
    passed for a COMPLETE label the fallback never repairs."""
    messages = _spawn_exchange(
        "s1", _PENDING_TASK_ID, agent="Research\nAnalyst", task="Leak analysis"
    )
    labels = al.collect_task_labels(messages)
    assert labels[_PENDING_TASK_ID] == al.TaskLabel(
        agent_name="Research\nAnalyst", task_description="Leak analysis"
    )
    assert labels[_PENDING_TASK_ID].complete
    text = al.waiting_on_tasks_message([_PENDING_TASK_ID], labels)
    assert '**Research Analyst** - "Leak analysis"' in text

    # No name at all: the field is empty, never the ``Status:`` line after it
    # - whether the result carries the rest of the block or ends there.
    for tail in ("Status: pending\nUse check_task_status(task_ids) to monitor.", ""):
        blank = [
            ChatMessage(
                role="tool",
                name="spawn_background_task",
                tool_call_id="s1",
                content=(
                    "Background task spawned successfully.\n"
                    f"Task ID: {_PENDING_TASK_ID}\nTarget agent: \n{tail}"
                ),
            ),
        ]
        label = al.collect_task_labels(blank)[_PENDING_TASK_ID]
        assert label.agent_name is None and not label.agent_omitted
        assert not label.complete

    # The self-copy sentinel is still recognised through the wider capture.
    self_copy = _spawn_exchange(
        "s2", _OTHER_TASK_ID, agent="yourself (parallel copy)", task="PDFs"
    )
    label = al.collect_task_labels(self_copy)[_OTHER_TASK_ID]
    assert label.agent_name is None and label.agent_omitted and label.complete


def test_collect_task_labels_reads_a_name_whose_line_starts_like_a_field_label():
    """Legacy transcripts (spawned before the source flattened the name) may
    hold a multi-line name whose OWN line opens like one of the block's
    labels. The value runs to the LAST ``Status:`` line - nothing free-text
    follows the block's real ``Status:`` - so ``Research\nStatus: Analyst``
    reads whole instead of stopping at the first look-alike, and a line
    opening with ``Target agent:`` or ``Task ID:`` inside the name is part
    of the name, never a second field: the block's own ``Task ID:`` line
    comes first, one result carries one task (first match wins), and the id
    regex is anchored to line starts."""
    tricky = "Research\nStatus: Analyst"
    labels = al.collect_task_labels(
        _spawn_exchange("s1", _PENDING_TASK_ID, agent=tricky, task="Leak analysis")
    )
    assert labels[_PENDING_TASK_ID].agent_name == tricky
    assert labels[_PENDING_TASK_ID].complete
    text = al.waiting_on_tasks_message([_PENDING_TASK_ID], labels)
    assert '**Research Status: Analyst** - "Leak analysis"' in text

    nested = f"Research\nTarget agent: Impostor\nTask ID: {_OTHER_TASK_ID}\nAnalyst"
    messages = _spawn_exchange("s1", _PENDING_TASK_ID, agent=nested, task="Leak analysis")
    labels = al.collect_task_labels(messages)
    assert list(labels) == [_PENDING_TASK_ID]
    assert labels[_PENDING_TASK_ID].agent_name == nested
    # The liveness probe reads the same block: the look-alike ``Task ID:``
    # inside the name is not a second spawned task.
    assert al.collect_spawned_task_ids(messages) == [_PENDING_TASK_ID]
    # ...and an id merely mentioned mid-line is not one either.
    assert al.collect_spawned_task_ids(
        [
            ChatMessage(
                role="tool",
                name="spawn_background_task",
                tool_call_id="s1",
                content=f"Background task spawned successfully. Task ID: {_OTHER_TASK_ID}",
            )
        ]
    ) == []

    # A legacy block whose LAST line is the look-alike (no real ``Status:``
    # after it) is ambiguous by construction; the accepted reading is the
    # fixed-format one - the look-alike is taken as the ``Status:`` field.
    legacy_tail = [
        ChatMessage(
            role="tool",
            name="spawn_background_task",
            tool_call_id="s1",
            content=(
                "Background task spawned successfully.\n"
                f"Task ID: {_PENDING_TASK_ID}\nTarget agent: Research\nStatus: Analyst\n"
            ),
        ),
    ]
    assert al.collect_task_labels(legacy_tail)[_PENDING_TASK_ID].agent_name == "Research"


async def test_spawn_result_block_is_one_field_per_line_by_construction():
    """The source fix: the spawn tool flattens the agent name when it renders
    the result block, so the transcript never carries a multi-line value
    from our own code and the parser is not load-bearing for it."""
    peer = SimpleNamespace(
        id=uuid.uuid4(),
        name="Research\n  Status: Analyst\tTarget agent: X",
        slug="research",
        is_company_managed=False,
        description="",
        persona="",
    )
    delegations: list[dict] = []
    result = build_sub_agent_tools(
        MagicMock(),
        user_id=str(uuid.uuid4()),
        parent_agent_id=str(uuid.uuid4()),
        handover_agents=[peer],
        parent_run_id=str(uuid.uuid4()),
        delegations_out=delegations,
    )
    assert result is not None
    _, executor = result
    fake_task = SimpleNamespace(id=uuid.UUID(_PENDING_TASK_ID), status="pending")
    with (
        patch.object(
            sub_agent_module,
            "count_active_sub_agent_executions",
            new=AsyncMock(return_value=0),
        ),
        patch.object(
            sub_agent_module,
            "create_sub_agent_task",
            new=AsyncMock(return_value=fake_task),
        ),
    ):
        spawn = executor._handlers["spawn_background_task"]
        out = await spawn(agent_id=str(peer.id), task="Leak analysis")

    lines = out.splitlines()
    assert lines[1] == f"Task ID: {_PENDING_TASK_ID}"
    assert lines[2] == "Target agent: Research Status: Analyst Target agent: X"
    assert lines[3] == "Status: pending"
    # The flow trace is a structured consumer and keeps the raw name.
    assert delegations[0]["target_agent_name"] == peer.name

    # Round trip through the parser: the flattened name reads back whole.
    labels = al.collect_task_labels(
        [
            ChatMessage(
                role="assistant",
                content="",
                tool_calls=[
                    ToolCall(
                        id="s1",
                        name="spawn_background_task",
                        arguments={"agent_id": str(peer.id), "task": "Leak analysis"},
                    )
                ],
            ),
            ChatMessage(
                role="tool", name="spawn_background_task", tool_call_id="s1", content=out
            ),
        ]
    )
    assert labels[_PENDING_TASK_ID] == al.TaskLabel(
        agent_name="Research Status: Analyst Target agent: X",
        task_description="Leak analysis",
    )


_GEMINI_SPAWN_ID = "call_spawn_background_task_0"


def test_collect_task_labels_pairs_repeated_tool_call_ids_in_transcript_order():
    """Regression: ``tool_call_id`` is only unique within one response. Gemini
    mints ``call_{function_name}_{index}`` deterministically, so two turns
    whose first call is a spawn share ``call_spawn_background_task_0``. A
    global id -> description map let the newer call overwrite the older one,
    and the OLDER pending task was then parked under the NEWER task's
    description. Calls and results must pair in transcript order."""
    messages = [
        ChatMessage(role="user", content="analyse the leak"),
        *_spawn_exchange(
            _GEMINI_SPAWN_ID, _PENDING_TASK_ID, agent="Research Analyst", task="Leak analysis"
        ),
        ChatMessage(role="assistant", content="Launched the analysis."),
        ChatMessage(role="user", content="and the PDFs"),
        *_spawn_exchange(
            _GEMINI_SPAWN_ID, _OTHER_TASK_ID, agent="Report Writer", task="Build the PDFs"
        ),
    ]

    labels = al.collect_task_labels(messages)

    assert labels[_PENDING_TASK_ID] == al.TaskLabel(
        agent_name="Research Analyst", task_description="Leak analysis"
    )
    assert labels[_OTHER_TASK_ID] == al.TaskLabel(
        agent_name="Report Writer", task_description="Build the PDFs"
    )
    # The older task, parked on its own, is rendered with ITS description.
    text = al.waiting_on_tasks_message([_PENDING_TASK_ID], labels)
    assert 'Background work is still running: **Research Analyst** - "Leak analysis". ' in text
    assert "Build the PDFs" not in text

    # Same id, result seen before the later duplicate call: pairs with its
    # own call, and the later call's result pairs with the later call.
    same_turn_then_later = [
        *_spawn_exchange(_GEMINI_SPAWN_ID, _PENDING_TASK_ID, agent="A", task="first"),
        ChatMessage(
            role="assistant",
            content="",
            tool_calls=[
                ToolCall(
                    id=_GEMINI_SPAWN_ID,
                    name="spawn_background_task",
                    arguments={"agent_id": "x", "task": "second"},
                )
            ],
        ),
    ]
    labels = al.collect_task_labels(same_turn_then_later)
    assert labels == {_PENDING_TASK_ID: al.TaskLabel(agent_name="A", task_description="first")}

    # Two spawns in ONE response carry distinct indexes; a pruned result
    # (stub, no Task ID) still consumes its own call so the next duplicate id
    # does not inherit a description that was never its own.
    two_in_one = [
        ChatMessage(
            role="assistant",
            content="",
            tool_calls=[
                ToolCall(
                    id="call_spawn_background_task_0",
                    name="spawn_background_task",
                    arguments={"agent_id": "x", "task": "zero"},
                ),
                ToolCall(
                    id="call_spawn_background_task_1",
                    name="spawn_background_task",
                    arguments={"agent_id": "y", "task": "one"},
                ),
            ],
        ),
        ChatMessage(
            role="tool",
            name="spawn_background_task",
            tool_call_id="call_spawn_background_task_0",
            content="[Pruned tool result — originally 240 chars. Content omitted.]",
        ),
        ChatMessage(
            role="tool",
            name="spawn_background_task",
            tool_call_id="call_spawn_background_task_1",
            content=f"Background task spawned successfully.\nTask ID: {_OTHER_TASK_ID}\nTarget agent: Y\n",
        ),
        *_spawn_exchange("call_spawn_background_task_0", _PENDING_TASK_ID, agent="Z", task="later"),
    ]
    labels = al.collect_task_labels(two_in_one)
    assert labels == {
        _OTHER_TASK_ID: al.TaskLabel(agent_name="Y", task_description="one"),
        _PENDING_TASK_ID: al.TaskLabel(agent_name="Z", task_description="later"),
    }

    # The pruned-CALL case is unchanged: a result with no pending call yields
    # the agent alone and stays incomplete, so the database fallback still runs.
    orphan = [
        ChatMessage(
            role="tool",
            name="spawn_background_task",
            tool_call_id=_GEMINI_SPAWN_ID,
            content=f"Background task spawned successfully.\nTask ID: {_PENDING_TASK_ID}\nTarget agent: Helper\n",
        ),
        *_spawn_exchange(_GEMINI_SPAWN_ID, _OTHER_TASK_ID, agent="Report Writer", task="PDFs"),
    ]
    labels = al.collect_task_labels(orphan)
    assert labels[_PENDING_TASK_ID] == al.TaskLabel(agent_name="Helper", task_description=None)
    assert not labels[_PENDING_TASK_ID].complete
    assert labels[_OTHER_TASK_ID].complete


def test_waiting_on_tasks_message_names_the_task_instead_of_its_id():
    text = al.waiting_on_tasks_message(
        [_PENDING_TASK_ID],
        {
            _PENDING_TASK_ID: al.TaskLabel(
                agent_name="Research Analyst",
                task_description="Run the external leak analysis for Fidelity",
            )
        },
    )
    assert _PENDING_TASK_ID not in text
    assert text.startswith(
        "Background work is still running: **Research Analyst** - "
        '"Run the external leak analysis for Fidelity". '
    )
    assert "as soon as it finishes" in text
    assert "automatically" in text
    assert "check task status" in text


def test_waiting_on_tasks_message_pluralises_and_counts_unlabelled_tasks():
    labels = {
        _PENDING_TASK_ID: al.TaskLabel(agent_name="Research Analyst"),
    }
    text = al.waiting_on_tasks_message([_PENDING_TASK_ID, _OTHER_TASK_ID], labels)
    assert _PENDING_TASK_ID not in text and _OTHER_TASK_ID not in text
    assert (
        "Background work is still running: **Research Analyst** and 1 other "
        "background task." in text
    )
    assert "as soon as they finish" in text

    # Nothing resolvable at all: a count, still never an id.
    text = al.waiting_on_tasks_message([_PENDING_TASK_ID, _OTHER_TASK_ID], {})
    assert text.startswith("2 background tasks are still running. ")
    assert "as soon as they finish" in text
    assert _PENDING_TASK_ID not in text

    text = al.waiting_on_tasks_message([_PENDING_TASK_ID])
    assert text.startswith("1 background task is still running. ")
    assert "as soon as it finishes" in text
    assert _PENDING_TASK_ID not in text

    # Casing of the id must not lose the label (transcript keys are lower-cased).
    text = al.waiting_on_tasks_message(
        [_PENDING_TASK_ID.upper()],
        {_PENDING_TASK_ID: al.TaskLabel(agent_name="Research Analyst")},
    )
    assert "**Research Analyst**" in text


def test_waiting_on_tasks_message_keeps_descriptions_short_and_one_line():
    long_task = (
        'Run the "external leak analysis" for Fidelity,\n'
        "then build the PDFs from the databases afterwards and archive everything "
        "into the shared drive under the Q3 folder"
    )
    text = al.waiting_on_tasks_message(
        [_PENDING_TASK_ID],
        {_PENDING_TASK_ID: al.TaskLabel(task_description=long_task)},
    )
    assert "\n" not in text
    # Inner double quotes become single quotes so the delimiters stay unambiguous.
    assert "'external leak analysis'" in text
    assert "Q3 folder" not in text, "the description is truncated"
    assert "…" in text


def test_task_label_keeps_the_agent_name_one_line_and_bounded():
    """Both halves are user-controlled text (an admin names the agent, the
    model writes the task): a newline or a runaway name must not break the
    one-sentence park message, and a whitespace-only half is not a label."""
    label = al.TaskLabel(
        agent_name="Research\n\tAnalyst  " + "x" * 200,
        task_description="  Leak   analysis\n",
    )
    rendered = label.render()
    assert "\n" not in rendered and "\t" not in rendered
    assert rendered.startswith("**Research Analyst x")
    assert "x" * 200 not in rendered, "the agent name is truncated"
    assert rendered.endswith(' - "Leak analysis"')

    blank = al.TaskLabel(agent_name="   ", task_description="\n")
    assert not blank.known
    text = al.waiting_on_tasks_message([_PENDING_TASK_ID], {_PENDING_TASK_ID: blank})
    assert text.startswith("1 background task is still running. ")
    assert "****" not in text


async def test_build_waiting_on_tasks_message_is_the_shared_entry_point():
    """Transcript label + condition fallback + wording, in one call — the
    shape every park site and the digest use, so they cannot drift."""
    messages = _spawn_exchange("s1", _PENDING_TASK_ID, agent="Research Analyst", task="Leak analysis")

    async def _condition(ids):
        return False, {tid: "running" for tid in ids}

    async def _labels(ids):
        return {_OTHER_TASK_ID: {"agent_name": "Report Writer", "task_description": None}}

    _condition.labels = _labels
    text = await al.build_waiting_on_tasks_message(
        messages, [_PENDING_TASK_ID, _OTHER_TASK_ID], _condition
    )
    assert text.startswith(
        'Background work is still running: **Research Analyst** - "Leak analysis"; '
        "**Report Writer**. "
    )
    assert _PENDING_TASK_ID not in text and _OTHER_TASK_ID not in text
    assert "as soon as they finish" in text


def test_waiting_on_tasks_message_lists_at_most_five_then_counts():
    ids = [f"4444444{i}-4444-4444-8444-444444444444" for i in range(7)]
    labels = {tid: al.TaskLabel(agent_name=f"Agent {i}") for i, tid in enumerate(ids)}
    text = al.waiting_on_tasks_message(ids, labels)
    assert "**Agent 4**" in text
    assert "**Agent 5**" not in text
    assert "and 2 other background tasks." in text


async def test_resolve_task_labels_falls_back_to_the_condition_lookup():
    """A task the transcript cannot name (pruned result, prior window,
    ``until=`` on foreign work) is looked up through the wait condition's
    ``labels`` hook; a lookup failure degrades to a count, never an error."""
    messages = [
        *_spawn_exchange("s1", _PENDING_TASK_ID, agent="Research Analyst", task="Leak analysis"),
    ]
    asked: list[list[str]] = []

    async def _condition(ids):
        return False, {tid: "running" for tid in ids}

    async def _labels(ids):
        asked.append(list(ids))
        return {
            _OTHER_TASK_ID: {
                "agent_name": "Report Writer",
                "task_description": "Build the PDFs",
            }
        }

    _condition.labels = _labels
    labels = await al.resolve_task_labels(
        messages, [_PENDING_TASK_ID, _OTHER_TASK_ID], _condition
    )
    # Only the unlabelled id hits the database.
    assert asked == [[_OTHER_TASK_ID]]
    assert labels[_PENDING_TASK_ID].agent_name == "Research Analyst"
    assert labels[_OTHER_TASK_ID] == al.TaskLabel(
        agent_name="Report Writer", task_description="Build the PDFs"
    )

    async def _broken(ids):
        raise RuntimeError("db down")

    _condition.labels = _broken
    labels = await al.resolve_task_labels(messages, [_OTHER_TASK_ID], _condition)
    assert _OTHER_TASK_ID not in labels
    text = al.waiting_on_tasks_message([_OTHER_TASK_ID], labels)
    assert text.startswith("1 background task is still running.")

    # A condition without the hook (tests, custom callers) is fine too.
    async def _bare(ids):
        return False, {}

    labels = await al.resolve_task_labels(messages, [_OTHER_TASK_ID], _bare)
    assert _OTHER_TASK_ID not in labels


def _recording_condition(rows: dict, asked: list[list[str]]):
    """A wait condition whose ``.labels`` hook serves *rows* and records
    every id list it was asked for."""

    async def _condition(ids):
        return False, {tid: "running" for tid in ids}

    async def _labels(ids):
        asked.append(list(ids))
        return rows

    _condition.labels = _labels
    return _condition


async def test_resolve_task_labels_completes_a_half_known_transcript_label():
    """The spawn RESULT survived but its tool CALL was pruned, so the
    transcript yields the agent alone. That half-label used to count as
    "known" and suppress the fallback, parking on ``**Helper**`` while the
    persisted description was one query away. A missing half now triggers
    the lookup, and the database fills exactly that half."""
    messages = [
        ChatMessage(
            role="tool",
            name="spawn_background_task",
            tool_call_id="pruned-away",
            content=(
                "Background task spawned successfully.\n"
                f"Task ID: {_PENDING_TASK_ID}\nTarget agent: Helper\n"
            ),
        ),
    ]
    asked: list[list[str]] = []
    condition = _recording_condition(
        {
            _PENDING_TASK_ID: {
                "agent_name": "Helper (renamed since)",
                "task_description": "Run the external leak analysis for Fidelity",
            }
        },
        asked,
    )

    text = await al.build_waiting_on_tasks_message(messages, [_PENDING_TASK_ID], condition)

    assert asked == [[_PENDING_TASK_ID]]
    # The database supplied the missing description; the agent name the
    # transcript already had is kept, not overridden by the row.
    assert (
        'Background work is still running: **Helper** - '
        '"Run the external leak analysis for Fidelity". ' in text
    )
    assert "renamed since" not in text
    assert _PENDING_TASK_ID not in text


async def test_resolve_task_labels_never_queries_for_a_complete_label():
    """Agent + description both in the transcript: no database read. The
    deliberate self-copy omission counts as settled too — it must not cost
    a query re-discovering the calling agent's own name."""
    messages = [
        *_spawn_exchange("s1", _PENDING_TASK_ID, agent="Research Analyst", task="Leak analysis"),
        *_spawn_exchange(
            "s2", _OTHER_TASK_ID, agent="yourself (parallel copy)", task="Build the PDFs"
        ),
    ]
    asked: list[list[str]] = []
    condition = _recording_condition(
        {_OTHER_TASK_ID: {"agent_name": "Research Analyst", "task_description": "x"}}, asked
    )

    text = await al.build_waiting_on_tasks_message(
        messages, [_PENDING_TASK_ID, _OTHER_TASK_ID], condition
    )

    assert asked == []
    assert '**Research Analyst** - "Leak analysis"; "Build the PDFs".' in text


async def test_resolve_task_labels_keeps_the_transcript_half_when_the_row_is_gone():
    """The fallback found nothing (foreign id, deleted row, ``{}`` from a
    swallowed read error): the transcript half is still rendered on its own,
    with no empty ``""`` / ``****`` fragment beside it."""
    messages = [
        ChatMessage(
            role="tool",
            name="spawn_background_task",
            tool_call_id="pruned-away",
            content=(
                "Background task spawned successfully.\n"
                f"Task ID: {_PENDING_TASK_ID}\nTarget agent: Helper\n"
            ),
        ),
    ]
    asked: list[list[str]] = []

    text = await al.build_waiting_on_tasks_message(
        messages, [_PENDING_TASK_ID], _recording_condition({}, asked)
    )
    assert asked == [[_PENDING_TASK_ID]]
    assert text.startswith("Background work is still running: **Helper**. ")
    assert '""' not in text and "****" not in text and " - ." not in text

    # A row with the description but NO agent name: the transcript agent
    # survives the merge instead of being thrown away with the row's None.
    text = await al.build_waiting_on_tasks_message(
        messages,
        [_PENDING_TASK_ID],
        _recording_condition(
            {_PENDING_TASK_ID: {"agent_name": None, "task_description": "Leak analysis"}},
            [],
        ),
    )
    assert 'Background work is still running: **Helper** - "Leak analysis". ' in text

    # A self-copy whose description was pruned: the row fills the description
    # but the deliberate agent omission stands (the row names the caller).
    self_copy = [
        ChatMessage(
            role="tool",
            name="spawn_background_task",
            tool_call_id="pruned-away",
            content=(
                "Background task spawned successfully.\n"
                f"Task ID: {_OTHER_TASK_ID}\nTarget agent: yourself (parallel copy)\n"
            ),
        ),
    ]
    text = await al.build_waiting_on_tasks_message(
        self_copy,
        [_OTHER_TASK_ID],
        _recording_condition(
            {_OTHER_TASK_ID: {"agent_name": "Research Analyst", "task_description": "PDFs"}},
            [],
        ),
    )
    assert text.startswith('Background work is still running: "PDFs". ')
    assert "Research Analyst" not in text


async def test_resolve_task_labels_bounds_the_database_lookup():
    """The label is cosmetic: a lookup that stalls past the bound degrades to
    the transcript label instead of holding the park past the run's own
    deadline; a zero budget skips the read outright."""
    messages = [
        ChatMessage(
            role="tool",
            name="spawn_background_task",
            tool_call_id="pruned-away",
            content=(
                "Background task spawned successfully.\n"
                f"Task ID: {_PENDING_TASK_ID}\nTarget agent: Helper\n"
            ),
        ),
    ]
    entered = asyncio.Event()
    never = asyncio.Event()
    asked: list[list[str]] = []

    async def _condition(ids):
        return False, {tid: "running" for tid in ids}

    async def _hanging(ids):
        asked.append(list(ids))
        entered.set()
        await never.wait()  # a pool acquisition that never returns
        return {_PENDING_TASK_ID: {"agent_name": "Helper", "task_description": "x"}}

    _condition.labels = _hanging

    started = time.monotonic()
    text = await al.build_waiting_on_tasks_message(
        messages, [_PENDING_TASK_ID, _OTHER_TASK_ID], _condition, lookup_timeout=0.05
    )
    assert time.monotonic() - started < 1
    assert entered.is_set(), "the lookup was attempted"
    # Transcript-only outcome: the agent half survives, the rest is counted.
    assert text.startswith(
        "Background work is still running: **Helper** and 1 other background task. "
    )
    assert _PENDING_TASK_ID not in text and _OTHER_TASK_ID not in text

    asked.clear()
    text = await al.build_waiting_on_tasks_message(
        messages, [_PENDING_TASK_ID], _condition, lookup_timeout=0
    )
    assert asked == [], "a zero budget means no database read at all"
    assert text.startswith("Background work is still running: **Helper**. ")


def test_label_lookup_budget_shrinks_with_the_wall_clock(monkeypatch):
    """No deadline: the flat cap. A deadline: whatever remains after the
    forced answer's return reserve, capped — and zero once the reserve is
    all that is left."""
    now = 1_000.0
    monkeypatch.setattr(al.time, "monotonic", lambda: now)
    reserve = al._FORCED_ANSWER_DEADLINE_RESERVE_SECS
    cap = al._PARK_LABEL_LOOKUP_TIMEOUT_SECS

    assert al._label_lookup_budget(None) == cap
    assert al._label_lookup_budget(now + 600) == cap
    assert al._label_lookup_budget(now + reserve + 0.5) == pytest.approx(0.5)
    assert al._label_lookup_budget(now + reserve) == 0
    assert al._label_lookup_budget(now - 1) == 0


async def test_make_task_condition_exposes_a_labels_lookup(monkeypatch):
    """The production condition carries the DB label hook, delegating to
    ``get_task_labels`` for the calling user; a read failure yields ``{}``."""
    seen: list[tuple[list[str], str]] = []

    async def _get_task_labels(task_ids, user_id):
        seen.append((list(task_ids), user_id))
        return {_PENDING_TASK_ID: {"agent_name": "Helper", "task_description": "x"}}

    monkeypatch.setattr("app.services.sub_agent.get_task_labels", _get_task_labels)
    cond = make_task_condition("aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa")
    assert cond is not None
    labels = await cond.labels([_PENDING_TASK_ID])
    assert seen == [([_PENDING_TASK_ID], "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa")]
    assert labels[_PENDING_TASK_ID]["agent_name"] == "Helper"

    async def _boom(task_ids, user_id):
        raise RuntimeError("db down")

    monkeypatch.setattr("app.services.sub_agent.get_task_labels", _boom)
    assert await cond.labels([_PENDING_TASK_ID]) == {}


async def test_loop_park_message_uses_database_label_when_transcript_has_none(
    monkeypatch, _quiet_langfuse
):
    """End to end: the model waits on a task it never spawned in this
    transcript; the park names it through the condition's label hook."""
    monkeypatch.setattr(wp, "MIN_GRANTED_WAIT_SECONDS", 0.05)
    monkeypatch.setattr(al, "MIN_GRANTED_WAIT_SECONDS", 0.05)

    async def _still_running(ids):
        return False, {ids[0]: "running"}

    async def _labels(ids):
        return {
            _PENDING_TASK_ID: {
                "agent_name": "Research Analyst",
                "task_description": "Run the external leak analysis for Fidelity",
            }
        }

    _still_running.labels = _labels

    definitions, executor = _dummy_executor()
    llm = _ScriptedLLM(
        [
            _wait_call_until("wait-1", _PENDING_TASK_ID),
            ChatResponse(content="should not run", model="stub", stop_reason="stop"),
        ]
    )
    messages = [ChatMessage(role="user", content="wait for the child")]

    response = await al.run_agent_loop(
        llm=llm,
        messages=messages,
        tools=definitions,
        executor=executor,
        max_iterations=5,
        wait_policy=wp.WaitPolicy(per_call_seconds=1, total_seconds=1),
        wait_condition=_still_running,
    )

    assert response.stop_reason == "waiting_on_tasks"
    assert response.pending_task_ids == [_PENDING_TASK_ID]
    assert _PENDING_TASK_ID not in (response.content or "")
    assert (
        '**Research Analyst** - "Run the external leak analysis for Fidelity"'
        in (response.content or "")
    )
    assert llm.call_count == 1


async def test_wall_clock_shortcut_never_reads_task_labels_from_the_database(
    _quiet_langfuse,
):
    """The forced-answer backstop's wall-clock shortcut exists because the
    outer deadline is nearly upon us and is documented to return immediately.
    Its park digest must therefore stay transcript-only: a database round-trip
    for a cosmetic label that stalls past the remaining seconds would get the
    loop cancelled with NO output — the very failure the shortcut avoids."""
    asked: list[list[str]] = []

    async def _still_running(ids):
        return False, {tid: "running" for tid in ids}

    async def _labels(ids):
        asked.append(list(ids))
        return {
            _PENDING_TASK_ID: {
                "agent_name": "Research Analyst",
                "task_description": "Leak analysis",
            }
        }

    _still_running.labels = _labels

    definitions, executor = _dummy_executor()
    llm = _ScriptedLLM([ChatResponse(content="should not run", model="stub", stop_reason="stop")])
    # A prior wait round on a task the transcript cannot name: exactly the
    # shape where the resolved digest WOULD go to the database.
    messages = [
        ChatMessage(role="user", content="wait for the child"),
        ChatMessage(
            role="assistant",
            content="",
            tool_calls=[
                ToolCall(
                    id="w0",
                    name="wait",
                    arguments={"duration": "5m", "until": _PENDING_TASK_ID},
                )
            ],
        ),
        ChatMessage(
            role="tool",
            name="wait",
            tool_call_id="w0",
            content="Waited 5m. Remaining wait budget for this run: 0s.",
        ),
    ]

    response = await al.run_agent_loop(
        llm=llm,
        messages=messages,
        tools=definitions,
        executor=executor,
        max_iterations=3,
        wait_policy=wp.WaitPolicy(per_call_seconds=1, total_seconds=1),
        wait_condition=_still_running,
        # Under the in-loop minimum AND the forced-answer minimum: the loop
        # breaks straight into the backstop, which takes the shortcut.
        wall_clock_deadline=time.monotonic() + al._FORCED_ANSWER_MIN_REMAINING_SECS - 2,
    )

    assert llm.call_count == 0, "no LLM call fits in the remaining budget"
    assert asked == [], "the shortcut must not touch the database"
    content = response.content or ""
    assert al.FORCED_ANSWER_TIMEOUT_MESSAGE in content
    # Transcript-only label: counted, never the id, never the DB name.
    assert "1 background task is still running" in content
    assert _PENDING_TASK_ID not in content
    assert "Research Analyst" not in content


async def test_execute_wait_spent_budget_with_finished_until_is_already_done():
    """A spent budget must NOT hand off when the named children have already
    finished — the results are ready, and exiting as waiting_on_tasks would
    kill a run whose work all completed during the last pause."""

    async def _all_done(ids):
        return True, {ids[0]: "completed"}

    result = await wp.execute_wait(
        {"duration": "1m", "until": _PENDING_TASK_ID},
        budget=_spent_budget(),
        condition=_all_done,
    )
    assert result.reason == wp.WAIT_REASON_ALREADY_DONE
    assert "No wait needed" in result.message
    assert "Do not end the turn with a checkpoint" in result.message
    assert not result.paused


async def test_execute_wait_spent_budget_with_running_until_still_handoffs():
    """The already-done shortcut must not weaken the hand-off for genuinely
    running children."""

    async def _still_running(ids):
        return False, {ids[0]: "running"}

    result = await wp.execute_wait(
        {"duration": "1m", "until": _PENDING_TASK_ID},
        budget=_spent_budget(),
        condition=_still_running,
    )
    assert result.reason == wp.WAIT_REASON_HANDOFF
    assert _PENDING_TASK_ID in result.message

# ── Partial wake: one of several watched tasks finished ────────────

_SECOND_TASK_ID = "22222222-2222-4222-8222-222222222222"


def test_terminal_status_set_is_single_sourced():
    """The poll gate, the implicit-wait probe and the partial-wake detection
    must agree on what "finished" means — an alias, not a re-spelled copy."""
    from app.services import sub_agent

    assert sub_agent._STATUS_TERMINAL is wp.WAIT_TERMINAL_STATUSES
    assert al._TASK_TERMINAL_STATUSES is wp.WAIT_TERMINAL_STATUSES


async def test_execute_wait_partial_wakes_before_the_pause():
    """A watched task that finished before the pause even started hands
    control straight back: the agent must react to the one that came back
    (report it, relaunch it if it failed), not sleep on the slowest sibling."""

    async def _condition(ids):
        return False, {_PENDING_TASK_ID: "failed", _SECOND_TASK_ID: "running"}

    budget = wp.WaitBudget()
    started = time.monotonic()
    result = await wp.execute_wait(
        {"duration": "5m", "until": f"{_PENDING_TASK_ID},{_SECOND_TASK_ID}"},
        budget=budget,
        condition=_condition,
    )
    assert result.reason == wp.WAIT_REASON_PARTIAL
    assert time.monotonic() - started < 1, "must not pause on the sibling"
    assert budget.spent_seconds == 0
    assert "failed" in result.message
    assert f"Not yet finished: {_SECOND_TASK_ID[:12]}: running" in result.message
    assert "spawn_background_task" in result.message, "relaunch instruction"
    assert "wait again" in result.message
    assert "Do not end the turn with a checkpoint" in result.message


async def test_execute_wait_partial_wakes_mid_pause():
    """The first of several watched tasks reaching a terminal state ends the
    pause — the 'parent stays on Thinking while a child delivered' failure."""
    polls = {"n": 0}

    async def _condition(ids):
        polls["n"] += 1
        if polls["n"] == 1:  # pre-pause check: both still running
            return False, {
                _PENDING_TASK_ID: "running",
                _SECOND_TASK_ID: "running",
            }
        return False, {
            _PENDING_TASK_ID: "completed",
            _SECOND_TASK_ID: "running",
        }

    started = time.monotonic()
    result = await wp.execute_wait(
        {"duration": "5m", "until": f"{_PENDING_TASK_ID},{_SECOND_TASK_ID}"},
        budget=wp.WaitBudget(),
        condition=_condition,
        poll_interval=0.05,
    )
    assert result.reason == wp.WAIT_REASON_PARTIAL
    assert time.monotonic() - started < 2, "must wake on the first finisher"
    assert polls["n"] >= 2, "must have entered the poll loop, not pre-pause exit"
    assert f"{_PENDING_TASK_ID[:12]}: completed" in result.message
    assert f"Not yet finished: {_SECOND_TASK_ID[:12]}: running" in result.message
    assert "get_task_result" in result.message


async def test_execute_wait_partial_labels_unknown_ids_not_found():
    """An id the status probe does not know must read as ``not found`` in the
    partial-wake message, not be silently mislabelled as still running."""

    async def _condition(ids):
        # The probe only knows the finished task; the sibling id is absent.
        return False, {_PENDING_TASK_ID: "completed"}

    result = await wp.execute_wait(
        {"duration": "5m", "until": f"{_PENDING_TASK_ID},{_SECOND_TASK_ID}"},
        budget=wp.WaitBudget(),
        condition=_condition,
    )
    assert result.reason == wp.WAIT_REASON_PARTIAL
    assert f"Not yet finished: {_SECOND_TASK_ID[:12]}: not found" in result.message


async def test_execute_wait_partial_message_carries_extracts_of_finished_only():
    """The finished task's result extract rides the partial wake; the
    still-running sibling must not be queried for a result it does not have."""
    asked: list[list[str]] = []

    async def _condition(ids):
        return False, {_PENDING_TASK_ID: "completed", _SECOND_TASK_ID: "running"}

    async def _extracts(ids):
        asked.append(list(ids))
        return {tid: "child report body" for tid in ids}

    _condition.extracts = _extracts  # type: ignore[attr-defined]
    result = await wp.execute_wait(
        {"duration": "5m", "until": f"{_PENDING_TASK_ID},{_SECOND_TASK_ID}"},
        budget=wp.WaitBudget(),
        condition=_condition,
    )
    assert result.reason == wp.WAIT_REASON_PARTIAL
    assert "child report body" in result.message
    assert asked == [[_PENDING_TASK_ID]]


async def test_execute_wait_single_watched_task_never_partial_wakes(monkeypatch):
    """For one watched task 'any terminal' IS 'all terminal': a defensive
    condition that reports a terminal status without done=True must elapse,
    not partial-wake into a report-and-rewait protocol with nothing to rewait."""
    monkeypatch.setattr(wp, "MIN_GRANTED_WAIT_SECONDS", 0.05)

    async def _condition(ids):
        return False, {_PENDING_TASK_ID: "failed"}

    result = await wp.execute_wait(
        {"duration": "1s", "until": _PENDING_TASK_ID},
        budget=wp.WaitBudget(wp.WaitPolicy(per_call_seconds=1, total_seconds=30)),
        condition=_condition,
        poll_interval=0.05,
    )
    assert result.reason == wp.WAIT_REASON_ELAPSED


async def test_loop_gives_the_model_a_round_after_a_partial_wake(
    monkeypatch, _quiet_langfuse
):
    """After a partial wake the model must get a fresh iteration to report the
    finished task and relaunch it — not park, not exit waiting_on_tasks."""
    monkeypatch.setattr(wp, "MIN_GRANTED_WAIT_SECONDS", 0.05)
    monkeypatch.setattr(al, "MIN_GRANTED_WAIT_SECONDS", 0.05)

    async def _one_failed(ids):
        return False, {
            _PENDING_TASK_ID: "failed",
            _SECOND_TASK_ID: "running",
        }

    definitions, executor = _dummy_executor()
    llm = _ScriptedLLM(
        [
            _wait_call_until(
                "wait-1", f"{_PENDING_TASK_ID},{_SECOND_TASK_ID}"
            ),
            ChatResponse(
                content="Interim: the report task failed, relaunching it.",
                model="stub",
                stop_reason="stop",
            ),
        ]
    )
    messages = [ChatMessage(role="user", content="wait for both children")]

    response = await al.run_agent_loop(
        llm=llm,
        messages=messages,
        tools=definitions,
        executor=executor,
        max_iterations=5,
        wait_policy=wp.WaitPolicy(per_call_seconds=60, total_seconds=600),
        wait_condition=_one_failed,
    )

    assert llm.call_count == 2, "the partial wake must buy the model a round"
    assert response.stop_reason != "waiting_on_tasks"
    assert "Interim" in (response.content or "")
    wait_results = [m for m in messages if m.role == "tool" and m.name == "wait"]
    assert wait_results
    assert "spawn_background_task" in (wait_results[0].content or "")


async def test_mixed_wait_round_runs_sibling_tools_before_pause(
    monkeypatch, _quiet_langfuse
):
    """wait + another tool in the same round must do the work first.

    Parking before independent work is what made keep-working-after-spawn
    the exception instead of the rule.
    """
    monkeypatch.setattr(wp, "MIN_GRANTED_WAIT_SECONDS", 0.05)
    monkeypatch.setattr(al, "MIN_GRANTED_WAIT_SECONDS", 0.05)

    events: list[str] = []

    async def _on_event(event: dict) -> None:
        status = event.get("status")
        tool = event.get("tool")
        if status == "wait_start":
            events.append("wait_start")
        elif status == "tool_done" and tool:
            events.append(f"done:{tool}")

    definitions, executor = _dummy_executor()
    llm = _ScriptedLLM(
        [
            ChatResponse(
                content="",
                model="stub",
                stop_reason="tool_calls",
                tool_calls=[
                    ToolCall(
                        id="w1",
                        name="wait",
                        arguments={"duration": "10m", "reason": "bg"},
                    ),
                    ToolCall(id="n1", name="noop", arguments={}),
                ],
            ),
            ChatResponse(content="All done.", model="stub", stop_reason="stop"),
        ]
    )
    messages = [ChatMessage(role="user", content="do work while waiting")]

    response = await al.run_agent_loop(
        llm=llm,
        messages=messages,
        tools=definitions,
        executor=executor,
        max_iterations=3,
        wait_policy=wp.WaitPolicy(per_call_seconds=1, total_seconds=30),
        on_event=_on_event,
    )

    assert response.content == "All done."
    assert "done:noop" in events
    assert "wait_start" in events
    assert events.index("done:noop") < events.index("wait_start")
    tool_names = [m.name for m in messages if m.role == "tool"]
    assert "noop" in tool_names
    assert "wait" in tool_names
    assert tool_names.index("noop") < tool_names.index("wait")


# ── Waiting on a run the agent started ─────────────────────────────

_RUN_ID = "33333333-3333-4333-8333-333333333333"
_USER_ID = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"


def _runs(table: dict[str, dict]):
    """A ``watched_run_statuses`` stand-in answering *table* for the ids it holds."""

    async def _lookup(run_ids, user_id):
        assert user_id == _USER_ID
        return {rid: table[rid] for rid in run_ids if rid in table}

    return _lookup


def _no_tasks(monkeypatch):
    async def _statuses(task_ids, user_id):
        return {tid: None for tid in task_ids}

    monkeypatch.setattr("app.services.sub_agent.get_task_statuses", _statuses)


async def test_a_finished_run_ends_the_wait(monkeypatch):
    """The reported bug: 'Waiting - test run still collecting' on a run that had
    already completed. An id that is no task is looked up as a run."""
    _no_tasks(monkeypatch)
    monkeypatch.setattr(
        "app.services.builtin_tools.wait.watched_run_statuses",
        _runs({_RUN_ID: {"status": "completed", "name": "Daily product brief", "summary": "Sent to Sam."}}),
    )
    cond = make_task_condition(_USER_ID)
    done, statuses = await cond([_RUN_ID])
    assert done is True
    assert statuses == {_RUN_ID: "completed"}

    result = await wp.execute_wait(
        {"duration": "5m", "until": _RUN_ID, "reason": "Test run still collecting"},
        budget=wp.WaitBudget(),
        condition=cond,
    )
    assert result.reason == wp.WAIT_REASON_ALREADY_DONE
    assert not result.paused
    # What the run did comes back with the wake.
    assert "Run 'Daily product brief' (completed): Sent to Sam." in result.message


async def test_a_running_run_keeps_the_wait_until_it_finishes(monkeypatch):
    _no_tasks(monkeypatch)
    table = {_RUN_ID: {"status": "running", "name": "Brief", "summary": ""}}
    monkeypatch.setattr("app.services.builtin_tools.wait.watched_run_statuses", _runs(table))
    monkeypatch.setattr(wp, "MIN_GRANTED_WAIT_SECONDS", 0.05)
    cond = make_task_condition(_USER_ID)

    async def _finish_soon():
        await asyncio.sleep(0.15)
        table[_RUN_ID] = {"status": "failed", "name": "Brief", "summary": "The mail server refused it."}

    finisher = asyncio.create_task(_finish_soon())
    started = time.monotonic()
    result = await wp.execute_wait(
        {"duration": "5s", "until": _RUN_ID},
        budget=wp.WaitBudget(wp.WaitPolicy(per_call_seconds=5, total_seconds=30)),
        condition=cond,
        poll_interval=0.05,
    )
    await finisher
    assert result.reason == wp.WAIT_REASON_CONDITION
    assert time.monotonic() - started < 2
    assert "The mail server refused it." in result.message


async def test_a_task_and_a_run_are_watched_together(monkeypatch):
    async def _statuses(task_ids, user_id):
        return {tid: ({"status": "running"} if tid == _PENDING_TASK_ID else None) for tid in task_ids}

    monkeypatch.setattr("app.services.sub_agent.get_task_statuses", _statuses)
    monkeypatch.setattr(
        "app.services.builtin_tools.wait.watched_run_statuses",
        _runs({_RUN_ID: {"status": "completed", "name": "Brief", "summary": "ok"}}),
    )
    done, statuses = await make_task_condition(_USER_ID)([_PENDING_TASK_ID, _RUN_ID])
    assert done is False
    assert statuses == {_PENDING_TASK_ID: "running", _RUN_ID: "completed"}


async def test_an_id_that_is_neither_a_task_nor_a_run_of_the_person_stays_pending(monkeypatch):
    """Another person's run reads as unknown: never 'already done'."""
    _no_tasks(monkeypatch)
    monkeypatch.setattr("app.services.builtin_tools.wait.watched_run_statuses", _runs({}))
    done, statuses = await make_task_condition(_USER_ID)([_RUN_ID])
    assert done is False
    assert statuses == {_RUN_ID: "unknown"}


async def test_a_run_lookup_that_fails_keeps_the_wait_on_its_timer(monkeypatch):
    _no_tasks(monkeypatch)

    async def _broken(run_ids, user_id):
        raise RuntimeError("database unreachable")

    monkeypatch.setattr("app.services.builtin_tools.wait.watched_run_statuses", _broken)
    done, statuses = await make_task_condition(_USER_ID)([_RUN_ID])
    assert done is False
    assert statuses == {_RUN_ID: "unknown"}


def test_the_wait_tool_says_it_can_wait_on_a_run():
    tool = wp.build_wait_tool_definition()
    assert "runs" in tool.input_schema["properties"]["until"]["description"]
    assert "task or run IDs" in wp.build_wait_system_prompt()



# ── What a wait tells, and what its card shows ────────────────────

_CONVERSATION = "44444444-4444-4444-8444-444444444444"


def test_every_run_end_is_a_wait_end():
    """A wait watching a run ends on every state the engine never leaves."""
    from app.services.assignment_engine import TERMINAL_RUN_STATUSES
    from app.services.builtin_tools import wait as wait_tools

    for status in TERMINAL_RUN_STATUSES:
        assert wait_tools._RUN_TERMINAL_AS_WAIT.get(status) in wp.WAIT_TERMINAL_STATUSES, status


@pytest.mark.parametrize(("status", "told"), [("running", False), ("completed", True), ("failed", True)])
async def test_a_wait_that_reads_a_run_ended_tells_its_conversation(monkeypatch, status, told):
    """The run is no longer owed to the chat whose wait saw it end, so the
    worker's wake says nothing twice; a wait outside a chat owes nothing."""
    _no_tasks(monkeypatch)
    marked: list = []

    async def _mark_told(run_ids, conversation_id):
        marked.append((list(run_ids), conversation_id))

    monkeypatch.setattr(
        "app.services.builtin_tools.wait.watched_run_statuses",
        _runs({_RUN_ID: {"status": status, "name": "Brief", "summary": "", "started_at": None}}),
    )
    monkeypatch.setattr("app.services.assignment_run_wake.mark_told", _mark_told)

    await make_task_condition(_USER_ID, conversation_id=_CONVERSATION)([_RUN_ID])
    await make_task_condition(_USER_ID)([_RUN_ID])

    assert marked == ([([_RUN_ID], _CONVERSATION)] if told else [])


async def test_a_run_is_named_by_its_assignment_in_the_park_message(monkeypatch):
    async def _labels(task_ids, user_id):
        return {}

    monkeypatch.setattr("app.services.sub_agent.get_task_labels", _labels)
    monkeypatch.setattr(
        "app.services.builtin_tools.wait.watched_run_statuses",
        _runs({_RUN_ID: {"status": "running", "name": "Eng recap", "summary": "", "started_at": None}}),
    )
    labels = await make_task_condition(_USER_ID).labels([_RUN_ID])
    assert labels[_RUN_ID]["agent_name"] == "Eng recap"


async def test_the_card_describes_each_watched_task_and_run(monkeypatch):
    async def _statuses(task_ids, user_id):
        return {
            tid: (
                {"status": "running", "task_description": "dig", "started_at": "t0"}
                if tid == _PENDING_TASK_ID
                else None
            )
            for tid in task_ids
        }

    async def _labels(task_ids, user_id):
        return {_PENDING_TASK_ID: {"agent_name": "Analyst", "task_description": "dig"}}

    monkeypatch.setattr("app.services.sub_agent.get_task_statuses", _statuses)
    monkeypatch.setattr("app.services.sub_agent.get_task_labels", _labels)
    monkeypatch.setattr(
        "app.services.builtin_tools.wait.watched_run_statuses",
        _runs({_RUN_ID: {"status": "running", "name": "Eng recap", "summary": "", "started_at": "t1"}}),
    )
    gone = "55555555-5555-4555-8555-555555555555"

    items = await make_task_condition(_USER_ID).details([_PENDING_TASK_ID, _RUN_ID, gone])

    assert items == [
        {"id": _PENDING_TASK_ID, "kind": "task", "label": "Analyst", "status": "running", "started_at": "t0"},
        {"id": _RUN_ID, "kind": "run", "label": "Eng recap", "status": "running", "started_at": "t1"},
        {"id": gone, "kind": None, "label": None, "status": "unknown", "started_at": None},
    ]


async def test_execute_wait_reports_every_poll_to_the_status_hook(monkeypatch):
    monkeypatch.setattr(wp, "MIN_GRANTED_WAIT_SECONDS", 0.01)
    polls = {"n": 0}
    seen: list[dict] = []

    async def _condition(ids):
        polls["n"] += 1
        return (polls["n"] > 2), {ids[0]: "completed" if polls["n"] > 2 else "running"}

    async def _on_status(statuses):
        seen.append(dict(statuses))

    result = await wp.execute_wait(
        {"duration": "5s", "until": _RUN_ID},
        budget=wp.WaitBudget(wp.WaitPolicy(per_call_seconds=5, total_seconds=30)),
        condition=_condition,
        poll_interval=0.02,
        on_status=_on_status,
    )
    assert result.reason == wp.WAIT_REASON_CONDITION
    assert seen[-1] == {_RUN_ID: "completed"}
    assert {_RUN_ID: "running"} in seen


async def test_a_failing_status_hook_never_ends_the_pause(monkeypatch):
    monkeypatch.setattr(wp, "MIN_GRANTED_WAIT_SECONDS", 0.01)
    polls = {"n": 0}

    async def _condition(ids):
        polls["n"] += 1
        return polls["n"] > 2, {ids[0]: "running"}

    async def _boom(statuses):
        raise RuntimeError("socket gone")

    result = await wp.execute_wait(
        {"duration": "5s", "until": _RUN_ID},
        budget=wp.WaitBudget(wp.WaitPolicy(per_call_seconds=5, total_seconds=30)),
        condition=_condition,
        poll_interval=0.02,
        on_status=_boom,
    )
    assert result.reason == wp.WAIT_REASON_CONDITION


async def test_the_wait_card_shows_what_it_watches_and_follows_its_state(monkeypatch):
    """``wait_start`` carries the watched items, and a state change during the
    pause sends ``wait_status`` with the fresh ones."""
    monkeypatch.setattr(wp, "MIN_GRANTED_WAIT_SECONDS", 0.01)
    state = {"status": "pending", "polls": 0}
    events: list[dict] = []

    async def _condition(ids):
        state["polls"] += 1
        if state["polls"] == 2:
            state["status"] = "running"
        return False, {ids[0]: state["status"]}

    async def _details(ids):
        return [{"id": ids[0], "kind": "run", "label": "Eng recap", "status": state["status"], "started_at": None}]

    _condition.details = _details  # type: ignore[attr-defined]

    async def _on_event(event):
        if event.get("status") in ("wait_start", "wait_status"):
            events.append(event)

    # The first condition read is the "already done" check before the grant;
    # the second, two seconds into the pause, sees the run start.
    await al._execute_wait_calls(
        [ToolCall(id="w1", name="wait", arguments={"duration": "3s", "until": _RUN_ID})],
        iteration=0,
        messages=[],
        on_event=_on_event,
        wait_budget=wp.WaitBudget(wp.WaitPolicy(per_call_seconds=5, total_seconds=30)),
        wall_clock_deadline=None,
        wait_skip_event=None,
        wait_condition=_condition,
        cancel_event=None,
        track_tool_result=lambda *a, **k: None,
    )

    assert events[0]["status"] == "wait_start"
    assert events[0]["items"][0] == {
        "id": _RUN_ID, "kind": "run", "label": "Eng recap", "status": "pending", "started_at": None,
    }
    assert [(e["status"], e["items"][0]["status"]) for e in events[1:]] == [("wait_status", "running")]


async def test_the_card_is_refreshed_only_when_a_watched_state_changes():
    """One card for the ``wait`` tool and the implicit park: a poll that sees
    what the card shows sends nothing, a change sends the fresh items, and an
    unreadable description leaves the card as it was."""
    state = {"status": "running", "readable": True}
    events: list[dict] = []

    async def _details(ids):
        if not state["readable"]:
            raise RuntimeError("pool exhausted")
        return [{"id": ids[0], "kind": "task", "label": "Analyst", "status": state["status"], "started_at": None}]

    async def _condition(ids):
        return False, {}

    _condition.details = _details  # type: ignore[attr-defined]

    async def _on_event(event):
        events.append(event)

    card = al._WaitCard(_condition, [_PENDING_TASK_ID], _on_event, "wait")
    assert (await card.items())[0]["status"] == "running"

    await card.report({_PENDING_TASK_ID: "running"})
    await card.report({})
    assert events == []

    state["readable"] = False
    await card.report({_PENDING_TASK_ID: "completed"})
    assert events == []

    state.update(readable=True, status="completed")
    await card.report({_PENDING_TASK_ID: "completed"})
    assert [(e["status"], e["tool"], e["items"][0]["status"]) for e in events] == [
        ("wait_status", "wait", "completed")
    ]


async def test_a_run_keeps_its_name_when_the_task_labels_cannot_be_read(monkeypatch):
    async def _labels(task_ids, user_id):
        raise RuntimeError("pool exhausted")

    monkeypatch.setattr("app.services.sub_agent.get_task_labels", _labels)
    monkeypatch.setattr(
        "app.services.builtin_tools.wait.watched_run_statuses",
        _runs({_RUN_ID: {"status": "running", "name": "Eng recap", "summary": "", "started_at": None}}),
    )
    labels = await make_task_condition(_USER_ID).labels([_RUN_ID])
    assert labels[_RUN_ID]["agent_name"] == "Eng recap"


async def test_the_card_keeps_a_task_s_state_when_its_label_cannot_be_read(monkeypatch):
    async def _statuses(task_ids, user_id):
        return {tid: {"status": "running", "task_description": "dig", "started_at": "t0"} for tid in task_ids}

    async def _labels(task_ids, user_id):
        raise RuntimeError("pool exhausted")

    monkeypatch.setattr("app.services.sub_agent.get_task_statuses", _statuses)
    monkeypatch.setattr("app.services.sub_agent.get_task_labels", _labels)

    items = await make_task_condition(_USER_ID).details([_PENDING_TASK_ID])

    assert items == [
        {"id": _PENDING_TASK_ID, "kind": "task", "label": "dig", "status": "running", "started_at": "t0"}
    ]


# ── Waiting on a Video Studio job the agent queued ─────────────────

_CLIP_ID = "66666666-6666-4666-8666-666666666666"
_PREVIEW_ID = "77777777-7777-4777-8777-777777777777"


def _video_jobs(table: dict[str, dict]):
    """A ``watched_job_statuses`` stand-in answering *table* for the ids it holds, noting each ``told_to``."""
    told: list = []

    async def _lookup(job_ids, user_id, *, told_to=None):
        assert user_id == _USER_ID
        told.append(told_to)
        return {jid: table[jid] for jid in job_ids if jid in table}

    _lookup.told = told  # type: ignore[attr-defined]
    return _lookup


def _job(status, *, kind="clip", name="s03", noun="AI shot", reason=None, started_at=None):
    return {"kind": kind, "status": status, "name": name, "noun": noun, "reason": reason, "started_at": started_at}


def _no_task_results(monkeypatch):
    async def _none(_db, task_id, user_id):
        return None

    monkeypatch.setattr("app.services.sub_agent.get_task_result", _none)


async def test_an_ai_shot_the_agent_queued_ends_the_wait(monkeypatch):
    """The reported bug: 'Two Veo shots (s03, s04) being saved and checked'
    listed as Not found while they were being made. An id that is neither a
    task nor a run is looked up as a Video Studio job."""
    _no_tasks(monkeypatch)
    _no_task_results(monkeypatch)
    lookup = _video_jobs({_CLIP_ID: _job("completed")})
    monkeypatch.setattr("app.services.builtin_tools.wait.watched_job_statuses", lookup)
    cond = make_task_condition(_USER_ID, conversation_id=_CONVERSATION)

    done, statuses = await cond([_CLIP_ID])
    assert (done, statuses) == (True, {_CLIP_ID: "completed"})
    # The condition reads in the chat's name: the job it saw end is told there.
    assert lookup.told == [_CONVERSATION]

    result = await wp.execute_wait({"duration": "5m", "until": _CLIP_ID}, budget=wp.WaitBudget(), condition=cond)
    assert result.reason == wp.WAIT_REASON_ALREADY_DONE
    assert f"AI shot 's03' (completed). get_video_jobs(clip_ids=['{_CLIP_ID}']) reports it in full." in result.message


async def test_a_queued_scene_preview_keeps_the_wait_until_it_ends(monkeypatch):
    _no_tasks(monkeypatch)
    _no_task_results(monkeypatch)
    preview = {"kind": "render", "name": "Cats - scene demo", "noun": "scene preview"}
    table = {_PREVIEW_ID: _job("queued", **preview)}
    monkeypatch.setattr("app.services.builtin_tools.wait.watched_job_statuses", _video_jobs(table))
    monkeypatch.setattr(wp, "MIN_GRANTED_WAIT_SECONDS", 0.05)

    async def _fail_soon():
        await asyncio.sleep(0.15)
        table[_PREVIEW_ID] = _job("failed", reason="The page ran out of memory.", **preview)

    failer = asyncio.create_task(_fail_soon())
    started = time.monotonic()
    result = await wp.execute_wait(
        {"duration": "5s", "until": _PREVIEW_ID},
        budget=wp.WaitBudget(wp.WaitPolicy(per_call_seconds=5, total_seconds=30)),
        condition=make_task_condition(_USER_ID),
        poll_interval=0.05,
    )
    await failer
    assert result.reason == wp.WAIT_REASON_CONDITION
    assert time.monotonic() - started < 2
    assert "Scene preview 'Cats - scene demo' (failed): The page ran out of memory." in result.message
    assert f"get_video_jobs(render_ids=['{_PREVIEW_ID}'])" in result.message


async def test_the_card_and_the_park_message_name_a_video_studio_job(monkeypatch):
    _no_tasks(monkeypatch)

    async def _labels(task_ids, user_id):
        return {}

    monkeypatch.setattr("app.services.sub_agent.get_task_labels", _labels)
    monkeypatch.setattr(
        "app.services.builtin_tools.wait.watched_job_statuses",
        _video_jobs(
            {
                _CLIP_ID: _job("running", name=None, started_at=None),
                _PREVIEW_ID: _job("queued", kind="render", name="Cats - scene demo", noun="scene preview"),
            }
        ),
    )
    cond = make_task_condition(_USER_ID)

    assert await cond.details([_CLIP_ID, _PREVIEW_ID]) == [
        {"id": _CLIP_ID, "kind": "job", "label": None, "status": "running", "started_at": None},
        {"id": _PREVIEW_ID, "kind": "job", "label": "Cats - scene demo", "status": "queued", "started_at": None},
    ]
    labels = await cond.labels([_CLIP_ID, _PREVIEW_ID])
    assert labels[_CLIP_ID] == {"agent_name": None, "task_description": "AI shot"}
    assert labels[_PREVIEW_ID] == {"agent_name": "Cats - scene demo", "task_description": "scene preview"}


def test_the_wait_tool_says_it_can_wait_on_a_video_studio_job():
    assert "Video Studio jobs" in wp.build_wait_tool_definition().input_schema["properties"]["until"]["description"]


def _job_row(status, **extra):
    fields = {
        "id": uuid.uuid4(), "status": status, "progress": 40, "progress_at": None, "stage": None, "error": None,
        "provider_state": None, "provider_eta_at": None, "params": {}, "scene_id": None,
    }
    return SimpleNamespace(**{**fields, **extra})


@pytest.mark.parametrize(
    ("status", "wait_status"),
    [("pending", "queued"), ("running", "running"), ("ready", "completed"), ("failed", "failed"), ("cancelled", "cancelled")],
)
async def test_a_job_s_state_reads_in_the_wait_s_words(monkeypatch, status, wait_status):
    from app.services.video_studio import jobs

    async def _watched(job_ids, user_id):
        return {job_ids[0]: ("clip", _job_row(status), "s03")}

    monkeypatch.setattr(jobs, "watched_jobs", _watched)
    found = await watched_job_statuses([_CLIP_ID], _USER_ID)
    assert found[_CLIP_ID]["status"] == wait_status
    assert (found[_CLIP_ID]["kind"], found[_CLIP_ID]["name"], found[_CLIP_ID]["noun"]) == ("clip", "s03", "AI shot")


def test_every_job_end_is_a_wait_end():
    from app.models.video_studio import JOB_TERMINAL_STATUSES
    from app.services.builtin_tools import wait as wait_tools
    from app.services.video_studio import jobs

    for status in JOB_TERMINAL_STATUSES:
        state = jobs.job_state("clip", _job_row(status)).status
        assert wait_tools._JOB_STATE_AS_WAIT[state] in wp.WAIT_TERMINAL_STATUSES, status


async def test_a_wait_marks_the_jobs_it_read_told_in_its_own_chat_only(monkeypatch):
    """A job the wait read is no longer owed to the chat the wait reports to
    (``jobs.mark_told`` keeps only the ended ones); a wait outside a chat
    marks nothing, and a mark that fails keeps what was read."""
    from datetime import UTC, datetime

    from app.services.video_studio import jobs

    started = datetime(2026, 10, 21, 12, 0, tzinfo=UTC)
    clip = _job_row("ready")
    preview = _job_row("running", scene_id="demo", project_id=uuid.uuid4(), started_at=started)

    async def _watched(job_ids, user_id):
        return {_CLIP_ID: ("clip", clip, "s03"), _PREVIEW_ID: ("render", preview, "Cats - scene demo")}

    marked: list = []

    async def _mark(kind, rows, conversation_id):
        marked.append((kind, [row.id for row in rows], conversation_id))

    monkeypatch.setattr(jobs, "watched_jobs", _watched)
    monkeypatch.setattr(jobs, "mark_told", _mark)

    found = await watched_job_statuses([_CLIP_ID, _PREVIEW_ID], _USER_ID, told_to=_CONVERSATION)
    await watched_job_statuses([_CLIP_ID, _PREVIEW_ID], _USER_ID)

    assert marked == [
        ("clip", [clip.id], uuid.UUID(_CONVERSATION)),
        ("render", [preview.id], uuid.UUID(_CONVERSATION)),
    ]
    assert found[_PREVIEW_ID] == {
        "kind": "render",
        "status": "running",
        "name": "Cats - scene demo",
        "noun": "scene preview",
        "reason": None,
        "started_at": started.isoformat(),
    }

    async def _broken(kind, rows, conversation_id):
        raise RuntimeError("database unreachable")

    monkeypatch.setattr(jobs, "mark_told", _broken)
    again = await watched_job_statuses([_CLIP_ID], _USER_ID, told_to=_CONVERSATION)
    assert again[_CLIP_ID]["status"] == "completed"


# ── Waiting on work delegated to an A2A partner ──────────────

_PARTNER_TASK_ID = "88888888-8888-4888-8888-888888888888"
_AGENT_ID = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"


def _delegations(table: dict[str, dict], *, owner=(_USER_ID, _AGENT_ID)):
    """A ``watched_delegation_statuses`` stand-in: *table* is visible to *owner* only."""

    async def _lookup(task_ids, user_id, agent_id):
        if (user_id, agent_id) != owner:
            return {}
        return {tid: table[tid] for tid in task_ids if tid in table}

    return _lookup


def _delegation(status, *, name="Partner TI", started_at="2026-11-15T13:00:00+00:00"):
    return {"status": status, "name": name, "started_at": started_at}


async def test_a_partner_task_the_agent_delegated_is_running_not_unknown(monkeypatch):
    """The reported bug: wait(until=<collect_task_id>) answered 'unknown' and
    the card read 'Not found' while the partner was working."""
    _no_tasks(monkeypatch)
    table = {_PARTNER_TASK_ID: _delegation("running")}
    monkeypatch.setattr("app.services.builtin_tools.wait.watched_delegation_statuses", _delegations(table))
    cond = make_task_condition(_USER_ID, agent_id=_AGENT_ID)

    assert await cond([_PARTNER_TASK_ID]) == (False, {_PARTNER_TASK_ID: "running"})
    assert await cond.details([_PARTNER_TASK_ID]) == [
        {
            "id": _PARTNER_TASK_ID,
            "kind": "a2a",
            "label": "Partner TI",
            "status": "running",
            "started_at": "2026-11-15T13:00:00+00:00",
        }
    ]
    labels = await cond.labels([_PARTNER_TASK_ID])
    assert labels[_PARTNER_TASK_ID]["agent_name"] == "Partner TI"


async def test_a_concluded_delegation_ends_the_wait_and_names_the_collect(monkeypatch):
    """The wait ends on the poll that reads the row concluded; its extract says
    how to collect, never the partner's own text."""
    _no_tasks(monkeypatch)
    _no_task_results(monkeypatch)
    table = {_PARTNER_TASK_ID: _delegation("running")}
    monkeypatch.setattr("app.services.builtin_tools.wait.watched_delegation_statuses", _delegations(table))
    cond = make_task_condition(_USER_ID, agent_id=_AGENT_ID)
    assert (await cond([_PARTNER_TASK_ID]))[0] is False

    table[_PARTNER_TASK_ID] = _delegation("completed")
    assert await cond([_PARTNER_TASK_ID]) == (True, {_PARTNER_TASK_ID: "completed"})
    extract = (await cond.extracts([_PARTNER_TASK_ID]))[_PARTNER_TASK_ID]
    assert f"continue_task_id='{_PARTNER_TASK_ID}'" in extract
    assert "no message" in extract

    result = await wp.execute_wait(
        {"duration": "5m", "until": _PARTNER_TASK_ID}, budget=wp.WaitBudget(), condition=cond
    )
    assert "continue_task_id" in result.message


@pytest.mark.parametrize("status", ["failed", "cancelled"])
async def test_a_delegation_that_did_not_complete_ends_the_wait(monkeypatch, status):
    _no_tasks(monkeypatch)
    table = {_PARTNER_TASK_ID: _delegation(status)}
    monkeypatch.setattr("app.services.builtin_tools.wait.watched_delegation_statuses", _delegations(table))
    cond = make_task_condition(_USER_ID, agent_id=_AGENT_ID)
    assert await cond([_PARTNER_TASK_ID]) == (True, {_PARTNER_TASK_ID: status})


async def test_another_user_s_or_agent_s_delegation_stays_unknown(monkeypatch):
    """The ownership rule a collect follows: nothing of another caller's
    delegation is visible, and an id it cannot see stays pending."""
    _no_tasks(monkeypatch)
    table = {_PARTNER_TASK_ID: _delegation("completed")}
    lookup = _delegations(table, owner=("cccccccc-cccc-4ccc-8ccc-cccccccccccc", _AGENT_ID))
    monkeypatch.setattr("app.services.builtin_tools.wait.watched_delegation_statuses", lookup)
    cond = make_task_condition(_USER_ID, agent_id=_AGENT_ID)
    assert await cond([_PARTNER_TASK_ID]) == (False, {_PARTNER_TASK_ID: "unknown"})


async def test_without_an_agent_no_id_is_read_as_a_delegation(monkeypatch):
    """A context with no agent cannot delegate, so it never reads delegations."""
    _no_tasks(monkeypatch)
    called: list = []

    async def _lookup(task_ids, user_id, agent_id):
        called.append(task_ids)
        return {}

    monkeypatch.setattr("app.services.builtin_tools.wait.watched_delegation_statuses", _lookup)
    cond = make_task_condition(_USER_ID)
    assert await cond([_PARTNER_TASK_ID]) == (False, {_PARTNER_TASK_ID: "unknown"})
    assert called == []


async def test_a_delegation_read_that_fails_keeps_the_wait_pending(monkeypatch):
    _no_tasks(monkeypatch)

    async def _broken(task_ids, user_id, agent_id):
        raise RuntimeError("database unreachable")

    monkeypatch.setattr("app.services.builtin_tools.wait.watched_delegation_statuses", _broken)
    cond = make_task_condition(_USER_ID, agent_id=_AGENT_ID)
    assert await cond([_PARTNER_TASK_ID]) == (False, {_PARTNER_TASK_ID: "unknown"})


def test_every_stored_delegation_outcome_reads_as_a_wait_end():
    """An outcome the map does not name is a failure, never a running state."""
    from app.models.a2a import DELEGATION_OUTCOMES
    from app.services.builtin_tools.wait import _DELEGATION_OUTCOME_AS_WAIT

    for outcome in DELEGATION_OUTCOMES:
        assert _DELEGATION_OUTCOME_AS_WAIT.get(outcome, "failed") in wp.WAIT_TERMINAL_STATUSES


def test_both_run_paths_pass_the_agent_to_the_wait_condition():
    """Chat and assignment runs both delegate, so both resolve delegation ids."""
    import inspect

    from app.services import agent_chat, assignment_engine

    for module in (agent_chat, assignment_engine):
        src = inspect.getsource(module)
        call = src[src.index("make_task_condition(") :][:200]
        assert "agent_id=str(agent.id)" in call, module.__name__
