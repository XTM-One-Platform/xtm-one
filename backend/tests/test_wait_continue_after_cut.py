"""A ``wait`` cut by a stopping worker is finished by the resumed turn itself (#8607).

The cut pause carries its start, grant and ``until`` ids on its tool result
(``ChatMessage.wait_cut``), the turn checkpoint keeps them, and the resumed
turn waits the rest before its first model call: the model reads one wait
result covering the whole pause. Clocks are faked; nothing sleeps for real.
"""

import asyncio
import uuid
from types import SimpleNamespace

import pytest

from app.llm.base import ChatMessage
from app.services import frictions, turn_checkpoint
from app.tools import agent_loop as al
from app.tools import wait_policy as wp
from app.tools.base import ToolCall


@pytest.fixture
def no_sleep(monkeypatch):
    """``sleep_or_wake`` returns at once, the full duration counted as elapsed by the fake clocks.

    The ``time`` module of the two modules is replaced by a fake, never the
    global one: the event loop keeps its own clock.
    """
    clock = {"mono": 5000.0, "wall": 0.0}

    async def _sleep(seconds, _event):
        clock["mono"] += max(0.0, seconds)
        clock["wall"] += max(0.0, seconds)
        return False

    fake = SimpleNamespace(monotonic=lambda: clock["mono"], time=lambda: clock["wall"])
    monkeypatch.setattr(wp, "sleep_or_wake", _sleep)
    monkeypatch.setattr(wp, "time", fake)
    monkeypatch.setattr(al, "time", fake)
    return clock


def _cut_transcript(cut: dict, *, last_tool: str = wp.WAIT_TOOL_NAME) -> list[ChatMessage]:
    call = ToolCall(id="call-1", name=last_tool, arguments={"duration": "5m"})
    return [
        ChatMessage(role="system", content="system"),
        ChatMessage(role="user", content="wait for CI"),
        ChatMessage(role="assistant", content="", tool_calls=[call]),
        ChatMessage(
            role="tool",
            content="Wait cut short after 50s: the server running this turn is stopping (a deployment).",
            tool_call_id="call-1",
            name=last_tool,
            wait_cut=cut,
        ),
        ChatMessage(role="assistant", content="[Resumed: this turn continues one that was interrupted ...]"),
    ]


def _continue(messages, **overrides):
    kwargs = {
        "on_event": None,
        "wait_budget": wp.WaitBudget(wp.WaitPolicy()),
        "wall_clock_deadline": None,
        "wait_skip_event": None,
        "wait_condition": None,
        "cancel_event": None,
        "pending_input": None,
        "stopping": None,
    }
    kwargs.update(overrides)
    return asyncio.run(al._continue_cut_wait(messages, **kwargs))


# ── The remaining duration ────────────────────────────────────────────────


def test_remaining_is_the_grant_less_the_time_since_the_pause_started():
    budget = wp.WaitBudget(wp.WaitPolicy())
    cut = {"started_at": 1000.0, "granted": 300.0, "elapsed": 50.0, "until": []}
    assert wp.cut_wait_remaining(cut, now=1100.0, budget=budget) == pytest.approx(200.0)


def test_remaining_never_goes_below_zero_and_the_downtime_counts():
    budget = wp.WaitBudget(wp.WaitPolicy())
    cut = {"started_at": 1000.0, "granted": 300.0, "elapsed": 50.0, "until": []}
    assert wp.cut_wait_remaining(cut, now=2000.0, budget=budget) == 0.0


def test_the_pause_made_before_the_cut_is_charged_to_the_budget():
    budget = wp.WaitBudget(wp.WaitPolicy())
    before = budget.remaining_total()
    wp.cut_wait_remaining({"started_at": 0.0, "granted": 300.0, "elapsed": 50.0}, now=60.0, budget=budget)
    assert budget.remaining_total() == pytest.approx(before - 50.0)


def test_a_malformed_cut_reads_as_nothing_left():
    budget = wp.WaitBudget(wp.WaitPolicy())
    assert wp.cut_wait_remaining({"granted": "x", "started_at": None}, now=10.0, budget=budget) == 0.0


# ── The cut itself ────────────────────────────────────────────────────────


def test_a_pause_cut_by_the_worker_stopping_carries_what_a_resume_needs(no_sleep):
    no_sleep["wall"] = 1234.0
    result = asyncio.run(
        wp.execute_wait({"duration": "5m"}, budget=wp.WaitBudget(wp.WaitPolicy()), stopping=lambda: True)
    )
    assert result.reason == wp.WAIT_REASON_WORKER_STOPPING
    assert result.cut == {
        "started_at": 1234.0,
        "granted": result.granted,
        "requested": 300,
        "until": [],
        "elapsed": pytest.approx(0.0),
    }


def test_a_pause_that_ends_normally_carries_no_cut(no_sleep):
    result = asyncio.run(wp.execute_wait({"duration": "30s"}, budget=wp.WaitBudget(wp.WaitPolicy())))
    assert result.reason == wp.WAIT_REASON_ELAPSED
    assert result.cut is None


def test_the_checkpoint_keeps_the_cut():
    cut = {"started_at": 1.0, "granted": 300.0, "requested": 300, "until": [], "elapsed": 50.0}
    message = ChatMessage(role="tool", content="cut", tool_call_id="c", name="wait", wait_cut=cut)
    (entry,) = turn_checkpoint._entries([message])
    assert entry["wait_cut"] == cut
    (read_back,) = turn_checkpoint._messages([entry])
    assert read_back.wait_cut == cut


# ── The resumed turn ──────────────────────────────────────────────────────


def test_the_resumed_turn_finishes_the_cut_wait_and_answers_once(no_sleep):
    no_sleep["wall"] = 1290.0
    messages = _cut_transcript({"started_at": 1000.0, "granted": 300.0, "requested": 300, "until": [], "elapsed": 50.0})
    waited = _continue(messages)
    assert waited == pytest.approx(10.0)
    result = messages[3]
    assert result.tool_call_id == "call-1"
    assert result.name == wp.WAIT_TOOL_NAME
    assert "cut short" not in result.content
    assert result.content.startswith("Waited 5m")
    assert result.wait_cut is None
    # One call, one result: nothing was added to the transcript.
    assert len(messages) == 5


def test_a_target_that_finished_during_the_downtime_ends_the_continued_wait_at_once(no_sleep):
    task_id = str(uuid.uuid4())

    async def _done(ids):
        return True, {i: "completed" for i in ids}

    no_sleep["wall"] = 1100.0
    messages = _cut_transcript(
        {"started_at": 1000.0, "granted": 300.0, "requested": 300, "until": [task_id], "elapsed": 50.0}
    )
    waited = _continue(messages, wait_condition=_done)
    assert waited == 0.0
    assert messages[3].content.startswith("No wait needed")
    assert no_sleep["mono"] == 5000.0


def test_the_continued_wait_is_bounded_by_the_policy(no_sleep):
    no_sleep["wall"] = 1010.0
    policy = wp.WaitPolicy(per_call_seconds=60, total_seconds=600)
    budget = wp.WaitBudget(policy)
    messages = _cut_transcript({"started_at": 1000.0, "granted": 300.0, "requested": 300, "until": [], "elapsed": 10.0})
    waited = _continue(messages, wait_budget=budget)
    assert waited == pytest.approx(60.0)
    assert budget.spent_seconds == pytest.approx(70.0)


def test_cut_again_the_continued_wait_keeps_its_first_start(no_sleep):
    no_sleep["wall"] = 1100.0
    messages = _cut_transcript({"started_at": 1000.0, "granted": 300.0, "requested": 300, "until": [], "elapsed": 50.0})
    _continue(messages, stopping=lambda: True)
    assert messages[3].wait_cut["started_at"] == 1000.0
    assert messages[3].wait_cut["granted"] == 300.0
    assert messages[3].wait_cut["elapsed"] == pytest.approx(50.0)


def test_a_cut_on_another_tool_is_left_as_it_is(no_sleep):
    messages = _cut_transcript({"started_at": 1000.0, "granted": 300.0}, last_tool="read_file")
    before = messages[3]
    assert _continue(messages) is None
    assert messages[3] is before


def test_a_wait_that_was_not_cut_is_left_as_it_is(no_sleep):
    messages = _cut_transcript(None)  # type: ignore[arg-type]
    before = messages[3]
    assert _continue(messages) is None
    assert messages[3] is before


def test_a_turn_that_called_tools_after_the_cut_is_left_as_it_is():
    messages = _cut_transcript({"started_at": 1000.0, "granted": 300.0})
    messages.append(ChatMessage(role="assistant", content="", tool_calls=[ToolCall(id="c2", name="x", arguments={})]))
    messages.append(ChatMessage(role="tool", content="ok", tool_call_id="c2", name="x"))
    assert al.cut_wait_to_continue(messages) is None


def test_a_task_message_after_the_replayed_rounds_does_not_hide_the_cut():
    messages = _cut_transcript({"started_at": 1000.0, "granted": 300.0})
    messages.append(ChatMessage(role="user", content="the task description"))
    found = al.cut_wait_to_continue(messages)
    assert found is not None and found[0] == 3


# ── What the chat shows during the continued pause (#9265) ───────────────────────


def _recording():
    events: list[dict] = []

    async def _on_event(event):
        events.append(dict(event))

    return events, _on_event


def test_the_continued_wait_is_announced_before_it_pauses(no_sleep, monkeypatch):
    events, on_event = _recording()
    order: list[str] = []
    real_execute = al.execute_wait

    async def _execute(*args, **kwargs):
        order.append("execute_wait")
        return await real_execute(*args, **kwargs)

    monkeypatch.setattr(al, "execute_wait", _execute)

    async def _record(event):
        order.append(str(event.get("status")))
        await on_event(event)

    no_sleep["wall"] = 1100.0
    messages = _cut_transcript({"started_at": 1000.0, "granted": 300.0, "requested": 300, "until": [], "elapsed": 50.0})
    messages[2].tool_calls[0].arguments["reason"] = "CI on the PR"
    _continue(messages, on_event=_record)
    starts = [e for e in events if e["status"] == "wait_start"]
    assert len(starts) == 1
    start = starts[0]
    assert start["tool"] == wp.WAIT_TOOL_NAME
    assert start["seconds"] == pytest.approx(200.0)
    assert start["reason"] == "CI on the PR"
    # Announced before the pause, and the pause over is said after it.
    assert order.index("execute_wait") < order.index("wait_start") < order.index("wait_continued")
    assert events[-1]["status"] == "wait_continued"


def test_the_continued_wait_shows_what_it_watches(no_sleep):
    task_id = str(uuid.uuid4())
    events, on_event = _recording()
    polls = {"n": 0}

    async def _running_then_done(ids):
        polls["n"] += 1
        state = "completed" if polls["n"] > 1 else "running"
        return polls["n"] > 1, {i: state for i in ids}

    no_sleep["wall"] = 1100.0
    messages = _cut_transcript(
        {"started_at": 1000.0, "granted": 300.0, "requested": 300, "until": [task_id], "elapsed": 50.0}
    )
    _continue(messages, on_event=on_event, wait_condition=_running_then_done)
    (start,) = [e for e in events if e["status"] == "wait_start"]
    assert start["until"] == task_id
    assert start["seconds"] > 0


def test_a_target_already_finished_announces_no_pause(no_sleep):
    task_id = str(uuid.uuid4())
    events, on_event = _recording()

    async def _done(ids):
        return True, {i: "completed" for i in ids}

    no_sleep["wall"] = 1100.0
    messages = _cut_transcript(
        {"started_at": 1000.0, "granted": 300.0, "requested": 300, "until": [task_id], "elapsed": 50.0}
    )
    _continue(messages, on_event=on_event, wait_condition=_done)
    assert [e["status"] for e in events] == ["wait_continued"]


def test_the_cut_call_arguments_are_read_from_the_transcript():
    messages = _cut_transcript({"started_at": 1000.0, "granted": 300.0})
    assert al._cut_call_arguments(messages, "call-1") == {"duration": "5m"}
    assert al._cut_call_arguments(messages, "missing") == {}


# ── A run resumed more than once (#8652) ──────────────────────────────────


def _replayed_entries(messages: list[ChatMessage]) -> list[dict]:
    """What a resume replays: the completed rounds of the turn as the checkpoint stores them."""
    return turn_checkpoint.completed_work(messages)


def test_the_continued_round_is_this_turns_work():
    messages = _cut_transcript({"started_at": 1000.0, "granted": 300.0})
    round_ = al._round_of_call(messages, "call-1")
    assert [m.role for m in round_] == ["assistant", "tool"]
    assert round_[1] is messages[3]
    assert al._round_of_call(messages, "missing") == []


def test_an_inherited_round_offered_again_is_replaced_not_duplicated():
    call = {"id": "call-1", "name": "wait", "arguments": {"duration": "5m"}}
    other = {"id": "call-0", "name": "read_file", "arguments": {}}
    inherited = [
        {"role": "assistant", "content": "", "tool_calls": [other]},
        {"role": "tool", "content": "ok", "tool_call_id": "call-0", "name": "read_file"},
        {"role": "assistant", "content": "", "tool_calls": [call]},
        {
            "role": "tool",
            "content": "Wait cut short after 50s",
            "tool_call_id": "call-1",
            "name": "wait",
            "wait_cut": {"started_at": 1000.0, "granted": 300.0},
        },
    ]
    work = [
        {"role": "assistant", "content": "", "tool_calls": [call]},
        {"role": "tool", "content": "Waited 5m.", "tool_call_id": "call-1", "name": "wait"},
    ]
    kept = turn_checkpoint._superseded_dropped(inherited, work)
    assert kept == inherited[:2]
    assert turn_checkpoint._superseded_dropped(inherited, []) is inherited


def test_an_inherited_round_offered_again_unchanged_is_kept_as_stored():
    """A task checkpoint offers its replayed rounds again: the stored copy (its spill record) stays."""
    call = {"id": "call-0", "name": "read_file", "arguments": {}}
    inherited = [
        {"role": "assistant", "content": "", "tool_calls": [call]},
        {"role": "tool", "content": "head", "tool_call_id": "call-0", "spill": {"file_id": "f"}},
    ]
    work = [
        {"role": "assistant", "content": "", "tool_calls": [call]},
        {"role": "tool", "content": "head", "tool_call_id": "call-0"},
    ]
    assert turn_checkpoint._superseded_dropped(inherited, work) is inherited


def test_a_run_resumed_twice_during_waits_never_replays_cut_short(no_sleep):
    """Attempt 1 is cut waiting; attempt 2 continues the wait and is cut again
    later; attempt 3 replays one call and one "Waited" result for it."""
    # Attempt 2 starts from attempt 1's checkpoint: the cut wait replayed.
    no_sleep["wall"] = 1290.0
    first = _cut_transcript({"started_at": 1000.0, "granted": 300.0, "requested": 300, "until": [], "elapsed": 50.0})
    inherited = _replayed_entries(first)
    checkpoint = turn_checkpoint.TurnCheckpoint(
        uuid.uuid4(),
        resumed=turn_checkpoint.ResumedWork(
            messages=turn_checkpoint._messages(inherited), entries=inherited, found=True
        ),
    )
    checkpoint._start_writer = lambda: None  # type: ignore[method-assign]
    # As the loop does (``_entry_messages``): the caller's messages held by
    # reference, so a freed message's id reused by a new one is never mistaken.
    entry = {id(m): m for m in first}
    _continue(first)
    # The loop hands the continued round to the checkpoint as this turn's work.
    for message in al._round_of_call(first, "call-1"):
        entry.pop(id(message), None)
    # Attempt 2 then waits again (a new call) and is cut again.
    second_call = ToolCall(id="call-2", name=wp.WAIT_TOOL_NAME, arguments={"duration": "4m"})
    first.append(ChatMessage(role="assistant", content="", tool_calls=[second_call]))
    first.append(
        ChatMessage(
            role="tool",
            content="Wait cut short after 1m: the server running this turn is stopping (a deployment).",
            tool_call_id="call-2",
            name=wp.WAIT_TOOL_NAME,
            wait_cut={"started_at": 1300.0, "granted": 240.0, "requested": 240, "until": [], "elapsed": 60.0},
        )
    )
    checkpoint.offer([m for m in first if entry.get(id(m)) is not m])
    stored = checkpoint._pending or []
    # Attempt 3 replays what attempt 2 stored.
    results = {e["tool_call_id"]: e["content"] for e in stored if e.get("role") == "tool"}
    assert list(results) == ["call-1", "call-2"]
    assert results["call-1"].startswith("Waited 5m")
    assert "cut short" not in results["call-1"]
    # One call per id across the replay.
    ids = [c["id"] for e in stored for c in e.get("tool_calls") or ()]
    assert ids == ["call-1", "call-2"]
    # And attempt 3 continues the second cut wait the same way.
    no_sleep["wall"] = 1600.0
    third = [
        ChatMessage(role="system", content="system"),
        ChatMessage(role="user", content="wait for CI"),
        *turn_checkpoint._completed_rounds(turn_checkpoint._messages(stored)),
    ]
    assert al.cut_wait_to_continue(third) is not None
    _continue(third)
    assert not any("cut short" in (m.content or "") for m in third if m.role == "tool")
    assert sum(1 for m in third if m.role == "tool") == 2


# ── The friction ledger ───────────────────────────────────────────────────


def test_the_resume_friction_says_the_wait_was_continued(monkeypatch):
    seen: list[tuple] = []
    monkeypatch.setattr(frictions, "note_friction", lambda *args, **kwargs: seen.append(args))
    monkeypatch.setattr(frictions, "run_scope", lambda run: None)
    frictions.note_turn_resumed(object(), cause="c", replayed_calls=3, graceful=True, wait_continued=True)
    frictions.note_turn_resumed(object(), cause="c", replayed_calls=3, graceful=True)
    assert seen[0][0] == "turn_resumed"
    assert seen[0][2].endswith("; wait_continued: true")
    assert "wait_continued" not in seen[1][2]
