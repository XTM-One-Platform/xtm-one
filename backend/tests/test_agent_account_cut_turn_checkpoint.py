"""An agent account turn a platform stop cuts resumes its completed tool calls (issue #10651).

The cut turn's tool rounds are checkpointed under an id of the account, the
conversation and the message; the run that answers the same message again
replays them instead of starting over, and opens its trace with
``resumed_from``. A turn that ends in its process deletes the checkpoint. A
cut posts nothing in the conversation, for any account mode and any response
mode, the agent's own account in its owner's direct chat included (#10772).
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from app.core import worker_shutdown
from app.core.shutdown_finalizers import BACKGROUND_TURN_CANCEL_MESSAGE
from app.llm.base import ChatMessage
from app.services import background_checkpoint
from app.services.agent_accounts import cut_turn_checkpoint, pipeline, state
from app.tools.base import ToolCall


def _event(message_id: str = "msg-1", conversation_type: str = "direct"):
    return SimpleNamespace(
        conversation_id="chat-1", message_id=message_id, conversation_type=conversation_type, route={"chat": "chat-1"}
    )


def _round(call_id: str) -> list[ChatMessage]:
    return [
        ChatMessage(
            role="assistant",
            content="",
            tool_calls=[ToolCall(id=call_id, name="create_work_item", arguments={"title": call_id})],
        ),
        ChatMessage(role="tool", content=f"created {call_id}", tool_call_id=call_id, name="create_work_item"),
    ]


class _FakeRedis:
    def __init__(self) -> None:
        self.store: dict[str, str] = {}

    async def set(self, key, value, ex=None, nx=False):
        if nx and key in self.store:
            return None
        self.store[key] = value
        return True

    async def get(self, key):
        return self.store.get(key)

    async def delete(self, key):
        self.store.pop(key, None)


@pytest.fixture
def redis(monkeypatch):
    fake = _FakeRedis()

    async def _client():
        return fake

    monkeypatch.setattr(background_checkpoint, "_redis", _client)
    monkeypatch.setattr("app.core.redis.redis_client", _client)
    return fake


@pytest.fixture(autouse=True)
def _no_stop_request(monkeypatch):
    monkeypatch.setattr(worker_shutdown, "_request", None)


def test_the_checkpoint_id_is_the_same_for_every_attempt_at_one_message():
    first = cut_turn_checkpoint.checkpoint_id("acc-1", _event())
    assert first == cut_turn_checkpoint.checkpoint_id("acc-1", _event())
    assert first != cut_turn_checkpoint.checkpoint_id("acc-1", _event("msg-2"))
    assert first != cut_turn_checkpoint.checkpoint_id("acc-2", _event())


async def test_the_resumed_attempt_replays_the_calls_the_cut_one_completed(redis):
    first = cut_turn_checkpoint.writer("acc-1", _event(), await cut_turn_checkpoint.load("acc-1", _event()))
    first.offer([*_round("a"), *_round("b")])
    await first.aclose(flush=True)

    replay = await cut_turn_checkpoint.load("acc-1", _event())

    assert replay.replayed_calls == 2
    assert [m.tool_call_id for m in replay.history if m.role == "tool"] == ["a", "b"]
    assert replay.history[-1].content.startswith("[Resumed:")
    meta = cut_turn_checkpoint.trace_meta("acc-1", _event(), replay)
    assert meta["resumed_from"]["entry"] == "agent_account"
    assert meta["resumed_from"]["replayed_calls"] == 2
    # The next attempt carries the count on: a third attempt sees every call.
    second = cut_turn_checkpoint.writer("acc-1", _event(), replay)
    assert second.completed_calls() == 2
    await second.aclose(flush=False)


async def test_a_first_attempt_replays_nothing_and_has_no_resumed_from(redis):
    replay = await cut_turn_checkpoint.load("acc-1", _event())
    assert replay.history == []
    assert cut_turn_checkpoint.trace_meta("acc-1", _event(), replay) is None


async def test_a_turn_that_ends_leaves_nothing_to_resume(redis):
    writer = cut_turn_checkpoint.writer("acc-1", _event(), await cut_turn_checkpoint.load("acc-1", _event()))
    writer.offer(_round("a"))
    await writer.aclose(flush=True)
    await cut_turn_checkpoint.discard("acc-1", _event())
    assert (await cut_turn_checkpoint.load("acc-1", _event())).history == []


class _Writer:
    def __init__(self) -> None:
        self.flushed: list[bool] = []

    async def aclose(self, *, flush: bool) -> None:
        self.flushed.append(flush)


@pytest.fixture
def answer_env(monkeypatch):
    env = SimpleNamespace(parked=[], discarded=[], writer=_Writer())

    async def _park(account_id, event, exc):
        env.parked.append(account_id)
        return True

    async def _discard(account_id, event):
        env.discarded.append(account_id)

    monkeypatch.setattr(pipeline, "_park_cut_turn", _park)
    monkeypatch.setattr(cut_turn_checkpoint, "discard", _discard)
    return env


def _turn(env, *, acted: bool, cut: bool):
    async def _answer_turn(db, account, event, decision, *, after_grace, progress, absorbed=None, trace_out=None):
        progress.checkpoint = env.writer
        progress.acted = acted
        if cut:
            raise asyncio.CancelledError(BACKGROUND_TURN_CANCEL_MESSAGE)

    return _answer_turn


async def test_a_turn_cut_before_it_acted_keeps_its_checkpoint_for_the_resume(answer_env, monkeypatch):
    monkeypatch.setattr(pipeline, "_answer_turn", _turn(answer_env, acted=False, cut=True))

    with pytest.raises(asyncio.CancelledError):
        await pipeline._answer(None, SimpleNamespace(id="acc-1"), _event(), None, after_grace=False)

    assert answer_env.parked == ["acc-1"]
    assert answer_env.writer.flushed == [True]
    assert answer_env.discarded == []


async def test_a_turn_cut_after_it_acted_drops_its_checkpoint(answer_env, monkeypatch):
    monkeypatch.setattr(pipeline, "_answer_turn", _turn(answer_env, acted=True, cut=True))

    with pytest.raises(asyncio.CancelledError):
        await pipeline._answer(None, SimpleNamespace(id="acc-1"), _event(), None, after_grace=False)

    assert answer_env.parked == []
    assert answer_env.writer.flushed == [False]
    assert answer_env.discarded == ["acc-1"]


async def test_a_turn_that_answered_drops_its_checkpoint(answer_env, monkeypatch):
    monkeypatch.setattr(pipeline, "_answer_turn", _turn(answer_env, acted=True, cut=False))

    assert await pipeline._answer(None, SimpleNamespace(id="acc-1"), _event(), None, after_grace=False) is True

    assert answer_env.writer.flushed == [False]
    assert answer_env.discarded == ["acc-1"]


@pytest.fixture
def chat_env(redis, monkeypatch):
    """A chat provider that records every post, with its session and send bookkeeping."""
    env = SimpleNamespace(sent=[], marked=[])

    class _Provider:
        label = "Teams"
        capabilities = SimpleNamespace(triage=False, verified_sender=True)

        async def send_reply(self, session, route, body):
            env.sent.append((route, body))
            return f"sent-{len(env.sent)}"

    async def _open(db, account, provider):
        return SimpleNamespace()

    async def _mark(*args):
        env.marked.append(args)

    monkeypatch.setattr(pipeline, "get_provider", lambda name: _Provider())
    monkeypatch.setattr(pipeline, "open_session", _open)
    monkeypatch.setattr(state, "mark_sent", _mark)
    monkeypatch.setattr(state, "mark_agent_post", _mark)
    return env


@pytest.mark.parametrize("mode", ["ai_user", "on_behalf"])
@pytest.mark.parametrize("response_mode", ["auto", "approval", "shadow", None])
@pytest.mark.parametrize("conversation_type", ["direct", "group", "channel"])
async def test_a_cut_turn_posts_nothing_in_the_conversation(
    chat_env, monkeypatch, mode, response_mode, conversation_type
):
    """No interrupted-turn notice on any account, response mode or conversation (#10772).

    The agent's own account in auto mode in its owner's direct chat included:
    the message is parked for the next process, and the chat reads nothing.
    """
    parked: list[str] = []

    async def _park(account_id, event, exc):
        parked.append(account_id)
        return True

    async def _answer_turn(db, account, event, decision, *, after_grace, progress, absorbed=None, trace_out=None):
        raise asyncio.CancelledError(BACKGROUND_TURN_CANCEL_MESSAGE)

    monkeypatch.setattr(pipeline, "_park_cut_turn", _park)
    monkeypatch.setattr(pipeline, "_answer_turn", _answer_turn)
    account = SimpleNamespace(id="acc-1", provider="microsoft_teams", mode=mode, agent_id=None)
    decision = SimpleNamespace(response_mode=response_mode) if response_mode else None

    for _ in range(2):
        with pytest.raises(asyncio.CancelledError):
            await pipeline._answer(
                None, account, _event(conversation_type=conversation_type), decision, after_grace=False
            )

    assert parked == ["acc-1", "acc-1"]
    assert chat_env.sent == []
    assert chat_env.marked == []


def test_the_resuming_notice_is_gone():
    """The post path and its Redis key are removed, not only gated (#10772)."""
    for name in ("RESUMING_NOTICE", "_tell_resuming", "_may_tell_resuming", "_resuming_notice_key"):
        assert not hasattr(pipeline, name), name


# --- The cut trace and its resume name each other (item 70f145e7, retest of #10781) ---


async def test_the_resumed_trace_names_the_cut_trace_it_resumes(redis):
    """``resumed_from.trace_id`` is the cut turn's trace, as on a cut web turn's resume."""
    first = cut_turn_checkpoint.writer("acc-1", _event(), await cut_turn_checkpoint.load("acc-1", _event()))
    first.offer(_round("send"))
    await first.aclose(flush=True)
    await cut_turn_checkpoint.remember_cut_trace("acc-1", _event(), "cut-trace-1")

    replay = await cut_turn_checkpoint.load("acc-1", _event())
    cut = await cut_turn_checkpoint.cut_trace("acc-1", _event())
    meta = cut_turn_checkpoint.trace_meta("acc-1", _event(), replay, cut_trace_id=cut)

    assert cut == "cut-trace-1"
    assert meta["resumed_from"]["trace_id"] == "cut-trace-1"
    await cut_turn_checkpoint.discard("acc-1", _event())
    assert await cut_turn_checkpoint.cut_trace("acc-1", _event()) is None


async def test_a_resume_without_a_kept_cut_trace_has_no_trace_id(redis):
    first = cut_turn_checkpoint.writer("acc-1", _event(), await cut_turn_checkpoint.load("acc-1", _event()))
    first.offer(_round("a"))
    await first.aclose(flush=True)
    replay = await cut_turn_checkpoint.load("acc-1", _event())
    assert "trace_id" not in cut_turn_checkpoint.trace_meta("acc-1", _event(), replay)["resumed_from"]


@pytest.fixture
def trace_env(redis, monkeypatch):
    """The shutdown finalizers a cut starts, by name (the trace update itself is not run)."""
    env = SimpleNamespace(skipped=[])

    def _finalizer(coro, *, name):
        coro.close()
        env.skipped.append(name)

    monkeypatch.setattr("app.core.shutdown_finalizers.start_shutdown_finalizer", _finalizer)
    return env


def _traced_turn(*, acted: bool, sent: list | None = None):
    """A turn that opens trace ``cut-trace-1``, sends (when *acted*) and is cut before its final answer."""

    async def _answer_turn(db, account, event, decision, *, after_grace, progress, absorbed=None, trace_out=None):
        trace_out["trace_id"] = "cut-trace-1"
        progress.checkpoint = cut_turn_checkpoint.writer(
            account.id, event, await cut_turn_checkpoint.load(account.id, event)
        )
        progress.checkpoint.offer(_round("send"))
        if acted:
            progress.acted = True
            if sent is not None:
                sent.append("posted")
        raise asyncio.CancelledError(BACKGROUND_TURN_CANCEL_MESSAGE)

    return _answer_turn


async def test_a_turn_cut_after_a_tool_send_before_its_answer_is_resumed_once_and_linked(trace_env, monkeypatch):
    """A tool call sent a message (not the turn's reply), then the stop cut the final answer.

    The turn had not acted on the platform as the account, so it is parked;
    the resumed attempt replays the send and its trace names the cut trace.
    """
    parked: list[str] = []

    async def _park(account_id, event, exc):
        parked.append(account_id)
        return True

    monkeypatch.setattr(pipeline, "_park_cut_turn", _park)
    monkeypatch.setattr(pipeline, "_answer_turn", _traced_turn(acted=False))
    account = SimpleNamespace(id="acc-1", provider="microsoft_teams", mode="on_behalf", agent_id=None)

    with pytest.raises(asyncio.CancelledError):
        await pipeline._answer(None, account, _event(), None, after_grace=False)

    assert parked == ["acc-1"]
    assert trace_env.skipped == []
    replay = await cut_turn_checkpoint.load("acc-1", _event())
    assert replay.replayed_calls == 1
    cut = await cut_turn_checkpoint.cut_trace("acc-1", _event())
    assert cut_turn_checkpoint.trace_meta("acc-1", _event(), replay, cut_trace_id=cut)["resumed_from"][
        "trace_id"
    ] == "cut-trace-1"


async def test_a_turn_cut_after_it_acted_says_why_it_is_not_resumed(trace_env, monkeypatch):
    async def _park(account_id, event, exc):  # pragma: no cover - never reached once acted
        raise AssertionError("an acted turn is never parked")

    monkeypatch.setattr(pipeline, "_park_cut_turn", _park)
    monkeypatch.setattr(pipeline, "_answer_turn", _traced_turn(acted=True))
    account = SimpleNamespace(id="acc-1", provider="microsoft_teams", mode="on_behalf", agent_id=None)

    with pytest.raises(asyncio.CancelledError):
        await pipeline._answer(None, account, _event(), None, after_grace=False)

    assert trace_env.skipped == ["cut-trace-skip:cut-trace-1"]
    assert await cut_turn_checkpoint.cut_trace("acc-1", _event()) is None


async def test_a_person_stopping_the_turn_marks_nothing(trace_env, monkeypatch):
    async def _answer_turn(db, account, event, decision, *, after_grace, progress, absorbed=None, trace_out=None):
        trace_out["trace_id"] = "cut-trace-1"
        raise asyncio.CancelledError()

    async def _park(account_id, event, exc):
        return False

    monkeypatch.setattr(pipeline, "_park_cut_turn", _park)
    monkeypatch.setattr(pipeline, "_answer_turn", _answer_turn)

    with pytest.raises(asyncio.CancelledError):
        await pipeline._answer(None, SimpleNamespace(id="acc-1"), _event(), None, after_grace=False)

    assert trace_env.skipped == []


async def test_the_resume_marks_the_cut_trace_resumed_as(monkeypatch):
    marked: list[tuple] = []

    async def _mark(db, cut, resumed):
        marked.append((cut, resumed))
        return True

    monkeypatch.setattr("app.services.cut_turn_resume.mark_trace_resumed_as", _mark)

    await pipeline._link_resumed_trace(None, "cut-trace-1", {"trace_id": "resumed-trace-2"})
    await pipeline._link_resumed_trace(None, None, {"trace_id": "resumed-trace-2"})
    await pipeline._link_resumed_trace(None, "cut-trace-1", {})

    assert marked == [("cut-trace-1", "resumed-trace-2")]
