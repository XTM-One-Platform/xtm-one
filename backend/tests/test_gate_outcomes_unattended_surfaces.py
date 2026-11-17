"""Gate outcomes on the surfaces that cannot ask.

Some callers install a gate and can supply no approval channel: the public
embed, the REST message endpoint, and any client too old to declare support. A
gated call there unwinds the turn instead of pausing, by design. That makes
these the paths where the record matters most and is easiest to lose — the drain
sits after the loop, and these turns never reach it. FR-020 asks why every tool
call ran, and "the turn died before it could run" is an answer the trail has to
be able to give.

The general assistant sits on both sides of that line: over the streaming socket
it pauses and waits like an agent does, over REST it unwinds. The drain has to
cover the unwinding half either way.

The public embed adds a second wrinkle: nobody to attribute a row to.
``audit_logs.user_id`` is NOT NULL, so the outcome goes to the application log
rather than being dropped.
"""

from __future__ import annotations

import inspect

import pytest

from app.services.tool_tracking import record_chat_gate_outcomes


def test_the_general_assistant_drains_when_the_turn_unwinds():
    from app.services import general_chat

    source = inspect.getsource(general_chat)
    # Either binding form: what matters is that the pause is caught and drained
    # here, not whether the exception is given a name (it is — the handler
    # stamps the pause from it).
    marker = next(
        m
        for m in ("except HumanInputRequestedError as ", "except HumanInputRequestedError:")
        if m in source
    )
    after = source.partition(marker)[2]
    assert "_drain_ga_bookkeeping()" in after.split("except ")[0], (
        "a gated call unwinds this surface, so draining only on the success "
        "path would miss every turn that produced a gate decision"
    )


def test_the_public_embed_drains_and_answers_when_the_turn_unwinds():
    """Recording the outcome is half of it. Re-raising sent the pause into the
    caller's broad handler, so a visitor was told the assistant had broken when
    it had declined to act without a human."""
    from app.api.v1 import public_chat

    source = inspect.getsource(public_chat)
    marker = "except HumanInputRequestedError as _pub_hitl:"
    assert marker in source
    handler = source.partition(marker)[2].split("except ")[0]
    assert "_drain_public_bookkeeping(" in handler
    assert "no_approval_channel_message(" in handler
    assert "mention_admin_setting=False" in handler, (
        "a visitor has no administrator screen to be pointed at"
    )
    assert "\n        raise\n" not in handler


@pytest.mark.asyncio
async def test_an_anonymous_turn_records_without_a_user():
    """No user to attribute the row to must not become no record at all, and
    must not raise into a visitor's chat either."""
    from app.tools.base import CompositeToolExecutor

    composite = CompositeToolExecutor()

    class _Msg:
        role = "assistant"

        class _TC:
            id = "call_1"
            name = "delete_entity"

        tool_calls = [_TC()]

    # user_id None is the anonymous embed. Must complete quietly rather than
    # reaching for a DB session it has nothing to write.
    await record_chat_gate_outcomes(
        [_Msg()],
        composite,
        outcomes={"call_1": "paused_for_approval"},
        user_id=None,
        hitl_enabled=True,
        conversation_id=None,
    )


@pytest.mark.asyncio
async def test_a_foreign_executor_is_left_alone():
    """Defensive: the drain is wired into surfaces generically, so it must be
    inert rather than explode when handed something that is not a composite."""
    await record_chat_gate_outcomes(
        [],
        object(),
        outcomes={},
        user_id=None,
        hitl_enabled=True,
    )


def test_a_gated_call_reads_as_a_decision_not_a_malfunction():
    """Both general-assistant entry points turn the pause into the shared
    explanation rather than letting it reach the generic error handler.

    Still both, now that the socket supplies a channel: REST has none, and the
    socket falls back here for a client that did not declare support. Whichever
    path unwinds, the user read "I encountered an error" for the product
    declining to act without a human, while the same tool on an agent would have
    shown them a prompt.
    """
    import inspect

    from app.api.v1 import chat as chat_module

    source = inspect.getsource(chat_module)
    assert source.count("_GeneralChatPaused as _ga_hitl") == 2, (
        "REST and WebSocket both reach run_general_chat and both must catch it"
    )
    assert source.count("no_approval_channel_message(") >= 2


def test_a_channel_bot_explains_the_pause_instead_of_erroring():
    """The bot surfaces (Slack, Teams, email) hold no turn open, so a gated call
    unwinds there.

    ``run_agent_chat`` catches its own pause and returns the explanation, so an
    agent on a channel already read correctly. ``run_general_chat`` re-raises for
    its callers to handle — and this caller did not, so the router's broad
    ``except Exception`` told the user "I encountered an error" for the gate
    working as designed. Same tool, same channel, two different answers
    depending on whether an agent happened to match.
    """
    import inspect

    from app.channels import router

    source = inspect.getsource(router)
    call = source.rpartition("await run_general_chat(")[2]
    assert call, "the channel router no longer calls run_general_chat"
    handler = call.partition("except HumanInputRequestedError")[2]
    assert handler, (
        "the pause is left to the broad handler, which reads as a malfunction"
    )
    assert "no_approval_channel_message(" in handler.split("except ")[0]


# --- One turn's calls, not the conversation's ------------------------------


@pytest.mark.asyncio
async def test_only_this_turn_s_calls_are_recorded(caplog):
    """``messages`` is the whole restored conversation, so walking it recorded
    every earlier turn's calls again — each with a null outcome, since this
    turn's gate never saw them. FR-020 asks why a call ran; answering null for
    most rows makes the record worse than terse.

    Latent while nothing persists `tool_calls_json` for chat, which is exactly
    why it is worth pinning: the day history carries tool calls, this would
    start silently.
    """
    from app.services.tool_tracking import calls_already_in, record_chat_gate_outcomes
    from app.tools.base import CompositeToolExecutor

    class _Call:
        def __init__(self, cid, name):
            self.id, self.name = cid, name

    class _Assistant:
        role = "assistant"

        def __init__(self, calls):
            self.tool_calls = calls

    history = [
        _Assistant([_Call("old_1", "Additioner")]),
        _Assistant([_Call("old_2", "Address search")]),
    ]
    inherited = calls_already_in(history)
    messages = [*history, _Assistant([_Call("now_1", "Additioner")])]

    with caplog.at_level("INFO", logger="app.services.tool_tracking"):
        await record_chat_gate_outcomes(
            messages,
            CompositeToolExecutor(),
            outcomes={"now_1": "human_approve"},
            user_id=None,  # anonymous: the outcome goes to the log
            hitl_enabled=True,
            exclude_call_ids=inherited,
        )

    recorded = next(
        r.calls for r in caplog.records if r.msg == "chat_tool_gate_outcomes"
    )
    assert [c["gate_outcome"] for c in recorded] == ["human_approve"]


def test_every_gated_surface_snapshots_before_its_loop():
    """The exclusion cannot be inferred inside the recorder — steering injects
    user messages mid-turn and compaction rewrites the list — so each surface
    has to capture it before its loop and hand it over."""
    import inspect

    from app.api.v1 import public_chat
    from app.services import agent_chat, general_chat

    for module in (agent_chat, general_chat, public_chat):
        source = inspect.getsource(module)
        assert "calls_already_in(messages)" in source, module.__name__
        assert "exclude_call_ids=" in source, module.__name__


def test_a_call_nobody_could_decide_says_so():
    """The case a blank could not distinguish from a bug.

    A surface with no way to ask pauses a gated call that is then never decided.
    Every other reason a call ran unattended is named in the trail; this one,
    where the call did *not* run, was a blank.

    Stamped at the pause rather than inferred from a missing outcome, because
    one other thing produces a missing outcome — a gate that failed to install
    lets calls run ungated — and calling those "paused" would claim a tool did
    not run when it did.
    """
    from app.tools.approval import OUTCOME_PAUSED, mark_paused

    outcomes: dict[str, str] = {}
    mark_paused(outcomes, [{"tool_call_id": "call_1", "tool_name": "delete_entity"}])

    assert outcomes == {"call_1": OUTCOME_PAUSED}


def test_a_decision_already_taken_is_not_overwritten_by_the_stamp():
    """A human verdict outranks "nobody decided" — it is the same call."""
    from app.tools.approval import mark_paused

    outcomes = {"call_1": "human_approve"}
    mark_paused(outcomes, [{"tool_call_id": "call_1"}])

    assert outcomes == {"call_1": "human_approve"}


def test_every_channel_less_surface_stamps_its_pause():
    """All three can end a turn on a gated call — the two chat services when the
    caller supplied no channel, the embed always — so all three must name it."""
    import inspect

    from app.api.v1 import public_chat
    from app.services import agent_chat, general_chat

    for module in (agent_chat, general_chat, public_chat):
        assert "mark_paused(" in inspect.getsource(module), module.__name__


@pytest.mark.asyncio
async def test_the_switch_being_off_is_still_reported_as_that(caplog):
    """The two blanks meant different things and must stay distinguishable."""
    from app.services.tool_tracking import record_chat_gate_outcomes
    from app.tools.approval import OUTCOME_HITL_OFF
    from app.tools.base import CompositeToolExecutor

    class _Call:
        def __init__(self, call_id: str, name: str) -> None:
            self.id, self.name = call_id, name

    class _Assistant:
        role = "assistant"
        tool_calls = [_Call("call_1", "delete_entity"), _Call("call_2", "create_entity")]

    # A turn with the gate off records only once something was decided; its
    # other calls then read as the switch being off, never as a blank.
    with caplog.at_level("INFO", logger="app.services.tool_tracking"):
        await record_chat_gate_outcomes(
            [_Assistant()],
            CompositeToolExecutor(),
            outcomes={"call_2": "paused"},
            user_id=None,
            hitl_enabled=False,
        )

    recorded = next(
        r.calls for r in caplog.records if r.msg == "chat_tool_gate_outcomes"
    )
    assert [c["gate_outcome"] for c in recorded] == [OUTCOME_HITL_OFF, "paused"]
