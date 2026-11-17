"""The agent's wait for a runner command's result.

A delivered command may run for its whole ceiling (the wait for the machine's
turn included) and then retry its result upload through a platform restart:
its result lands up to ``runner_report_window_seconds()`` after delivery. The
wait used to be a fixed 180 s from dispatch, so such a result reached the
machine's command history and never the agent. It now covers that window from
the delivery, ends before the command's lease, keeps an operator-set
``RUNNER_RESULT_WAIT_TIMEOUT_SECONDS`` as a fixed wait, and never outlasts the
agent loop's limit for the tool call - whose end the tool reports as such.

Pure tests (no database, no Redis): the wait runs on a fake clock that only the
fake pub/sub's wait for a message moves, and the tool runs against fakes.
"""

from __future__ import annotations

import asyncio
import re
import time
import uuid
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from app.core.config import settings
from app.models.runner import Runner
from app.services import runner_transport
from app.services.builtin_tools import computer_use
from app.services.builtin_tools.computer_use import (
    TOOL_ANSWER_RESERVE_SECONDS,
    build_computer_use_tools,
)
from app.services.runner_transport import (
    RUNNER_UPLOAD_RETRY_BUDGET_SECONDS,
    _command_lease_seconds,
    await_result,
    default_result_wait_seconds,
    runner_execution_ceiling_seconds,
    runner_report_window_seconds,
)
from app.tools.base import ToolCall
from app.tools.tool_events import bind_tool_event_sink, reset_tool_event_sink

#: The fake monotonic clock at the start of the wait.
MONO0 = 10_000.0
#: The claiming replica's wall clock at the start of the wait.
WALL0 = datetime(2026, 9, 27, 12, 0, tzinfo=UTC)


# ── The wait, on a fake clock ────────────────────────────────────────────────


class _Clock:
    def __init__(self) -> None:
        self.now = MONO0
        #: This replica's wall clock minus the claiming replica's.
        self.skew = 0.0

    def monotonic(self) -> float:
        return self.now

    def wall(self) -> datetime:
        return WALL0 + timedelta(seconds=self.now - MONO0 + self.skew)


class _PubSub:
    """No message ever arrives: each wait for one takes its whole slice."""

    def __init__(self, clock: _Clock) -> None:
        self.clock = clock

    async def subscribe(self, *_channels) -> None:
        return None

    async def unsubscribe(self, *_channels) -> None:
        return None

    async def get_message(self, *, ignore_subscribe_messages: bool, timeout: float):
        assert 0 < timeout <= 1.0
        self.clock.now += timeout
        return None

    async def aclose(self) -> None:
        return None


class _PubSubClient:
    def __init__(self, pubsub: _PubSub) -> None:
        self._pubsub = pubsub

    def pubsub(self) -> _PubSub:
        return self._pubsub

    async def aclose(self) -> None:
        return None


class _Machine:
    """What the command row says at each re-read.

    Taken ``delivered_after`` seconds into the wait (``None``: never), its
    result recorded ``result_after`` seconds after that delivery (``None``:
    never). ``dispatched_at`` is written on the claiming replica's clock.
    """

    def __init__(self, clock: _Clock, delivered_after: float | None, result_after: float | None) -> None:
        self.clock = clock
        self.delivered_after = delivered_after
        self.result_after = result_after

    def row(self) -> dict:
        elapsed = self.clock.now - MONO0
        row = {
            "id": "cmd-1",
            "kind": "run_shell",
            "status": "pending",
            "result": None,
            "error": None,
            "result_file_id": None,
            "dispatched_at": None,
        }
        if self.delivered_after is None or elapsed < self.delivered_after:
            return row
        row["status"] = "dispatched"
        row["dispatched_at"] = WALL0 + timedelta(seconds=self.delivered_after)
        if self.result_after is not None and elapsed >= self.delivered_after + self.result_after:
            row["status"] = "completed"
            row["result"] = {"exit_code": 0, "stdout": "done", "stderr": ""}
        return row


@pytest.fixture
def machine(monkeypatch):
    """``make(delivered_after=0, result_after=None)`` -> the fake clock."""
    clock = _Clock()
    monkeypatch.setattr(runner_transport, "time", SimpleNamespace(monotonic=clock.monotonic))
    monkeypatch.setattr(runner_transport, "_utcnow", clock.wall)
    pubsub = _PubSub(clock)

    async def _client():
        return _PubSubClient(pubsub)

    monkeypatch.setattr(runner_transport, "redis_pubsub_client", _client)
    monkeypatch.setattr(settings, "runner_result_wait_timeout_seconds", None)

    def make(*, delivered_after: float | None = 0.0, result_after: float | None = None) -> _Clock:
        state = _Machine(clock, delivered_after, result_after)

        async def _load(_command_id):
            return state.row()

        monkeypatch.setattr(runner_transport, "load_command", _load)
        return clock

    return make


def _waited(clock: _Clock) -> float:
    return clock.now - MONO0


async def test_a_result_uploaded_at_the_end_of_the_runners_window_reaches_the_agent(machine):
    # Run for the whole ceiling, then retry the upload for the whole budget
    # through a restart: the latest a valid result lands.
    clock = machine(result_after=runner_report_window_seconds())
    result = await await_result("cmd-1")
    assert result["status"] == "completed"
    assert _waited(clock) < default_result_wait_seconds()


async def test_no_result_ends_the_wait_after_the_window_and_before_the_lease(machine):
    clock = machine()
    result = await await_result("cmd-1")
    assert result["status"] == "dispatched"
    assert _waited(clock) == pytest.approx(default_result_wait_seconds())
    assert _waited(clock) < _command_lease_seconds()


async def test_the_window_runs_from_the_delivery(machine):
    # The machine took the command 30 s into the wait (a reconnect): its
    # result, at the end of its own window, still reaches the agent.
    clock = machine(delivered_after=30, result_after=runner_report_window_seconds())
    result = await await_result("cmd-1")
    assert result["status"] == "completed"
    assert _waited(clock) == pytest.approx(30 + runner_report_window_seconds(), abs=1.0)


async def test_a_command_no_machine_takes_is_waited_for_one_window(machine):
    clock = machine(delivered_after=None)
    result = await await_result("cmd-1")
    assert result["status"] == "pending"
    assert _waited(clock) == pytest.approx(default_result_wait_seconds())


async def test_an_operator_set_wait_keeps_its_meaning(machine, monkeypatch):
    clock = machine(result_after=runner_report_window_seconds())
    monkeypatch.setattr(settings, "runner_result_wait_timeout_seconds", 180)
    result = await await_result("cmd-1")
    # 180 s from the start of the wait, as configured, whatever the runner has left.
    assert result["status"] == "dispatched"
    assert _waited(clock) == pytest.approx(180)


async def test_an_explicit_timeout_is_a_fixed_wait(machine):
    clock = machine(result_after=runner_report_window_seconds())
    result = await await_result("cmd-1", timeout=30)
    assert result["status"] == "dispatched"
    assert _waited(clock) == pytest.approx(30)


async def test_the_wait_never_outlasts_the_callers_deadline(machine):
    clock = machine(result_after=runner_report_window_seconds())
    result = await await_result("cmd-1", not_after=MONO0 + 120)
    assert result["status"] == "dispatched"
    assert clock.now == pytest.approx(MONO0 + 120)


async def test_an_operator_set_wait_is_capped_by_the_callers_deadline(machine, monkeypatch):
    clock = machine()
    monkeypatch.setattr(settings, "runner_result_wait_timeout_seconds", 600)
    await await_result("cmd-1", not_after=MONO0 + 50)
    assert clock.now == pytest.approx(MONO0 + 50)


async def test_a_clock_behind_the_claiming_replicas_cannot_carry_the_wait_past_the_lease(machine):
    # This replica reads the delivery 30 s in its future: the moment the wait
    # first saw the command taken still bounds it.
    clock = machine()
    clock.skew = -30.0
    await await_result("cmd-1")
    assert _waited(clock) == pytest.approx(default_result_wait_seconds())
    assert _waited(clock) < _command_lease_seconds()


async def test_a_result_already_in_returns_at_once(machine):
    clock = machine(result_after=0)
    result = await await_result("cmd-1")
    assert result["status"] == "completed"
    assert _waited(clock) == 0


# ── The tool: what the agent reads when the wait ends ────────────────────────


def _runner(owner: uuid.UUID) -> Runner:
    return Runner(
        id=uuid.uuid4(),
        user_id=owner,
        name="build-box",
        platform="linux",
        key_prefix="run-xxxxxxxx",
        key_hash="hash",
        permission_mode="restricted",
        capabilities=["run_shell"],
        labels=[],
        is_active=True,
        status="online",
        last_seen_at=datetime.now(UTC) - timedelta(seconds=5),
    )


class _Rows:
    def __init__(self, rows):
        self._rows = rows

    def scalars(self):
        return self

    def all(self):
        return self._rows


class _Session:
    def __init__(self, runner):
        self._runner = runner

    async def execute(self, *_a, **_k):
        return _Rows([self._runner])

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


def _row(status: str, **fields) -> dict:
    return {
        "id": "cmd-1",
        "kind": "run_shell",
        "status": status,
        "result": None,
        "error": None,
        "result_file_id": None,
        "dispatched_at": None,
        **fields,
    }


DISPATCHED = _row("dispatched", dispatched_at=datetime.now(UTC))
PENDING = _row("pending")


_SHELL_CALL = ToolCall(id="tc-1", name="run_shell", arguments={"command": "make release"})


def _wire_shell(monkeypatch, waited: dict, *, landed: dict | None = None, give_up=None):
    """The ``computer_use`` executor, its result wait ending on *waited*.

    *landed* is the row the give-up finds (a result that came in meanwhile);
    by default the give-up finishes an undelivered command with its reason and
    leaves a delivered one as it is. *give_up* replaces the give-up outright.
    """
    owner = uuid.uuid4()
    runner = _runner(owner)
    monkeypatch.setattr(computer_use, "async_session", lambda: _Session(runner))
    monkeypatch.setattr(computer_use, "authorize_command", lambda *_a, **_k: None)
    monkeypatch.setattr(computer_use, "command_requires_approval", lambda *_a, **_k: False)
    seen: dict = {}

    async def _dispatch(**_kwargs):
        return "cmd-1", "pending"

    async def _await_result(command_id, timeout=None, *, not_after=None):
        seen["not_after"] = not_after
        return dict(waited)

    async def _give_up(command_id, reason, **_kwargs):
        seen["reason"] = reason
        if landed is not None:
            return dict(landed)
        if waited["status"] == "pending":
            return {**waited, "status": "timeout", "error": reason}
        return dict(waited)

    monkeypatch.setattr(computer_use, "dispatch_command", _dispatch)
    monkeypatch.setattr(computer_use, "await_result", _await_result)
    monkeypatch.setattr(computer_use, "give_up_on_command", give_up or _give_up)
    _, executor = build_computer_use_tools(
        user_id=str(owner),
        is_admin=False,
        user_group_ids=[],
        agent_runner_ids=None,
        agent_runner_labels=None,
        conversation_id=str(uuid.uuid4()),
    )
    return executor, seen


def _shell(monkeypatch, waited: dict, *, landed: dict | None = None, call_timeout: float | None = None):
    """``run_shell`` whose result wait ends on *waited*, bound the way the agent
    loop binds a call, with its own limit *call_timeout*."""
    executor, seen = _wire_shell(monkeypatch, waited, landed=landed)
    binding = bind_tool_event_sink(None, tool_name="run_shell", timeout=call_timeout)
    try:
        content = asyncio.run(executor.execute_tool(_SHELL_CALL)).content
    finally:
        reset_tool_event_sink(binding)
    return content, seen, binding


def test_the_wait_ends_in_time_for_the_tool_to_answer_its_call(monkeypatch):
    completed = _row("completed", result={"exit_code": 0, "stdout": "built", "stderr": ""})
    content, seen, binding = _shell(monkeypatch, completed, call_timeout=300)
    assert seen["not_after"] == pytest.approx(binding.deadline - TOOL_ANSWER_RESERVE_SECONDS)
    assert "built" in content


def test_outside_the_agent_loop_the_wait_has_no_call_deadline(monkeypatch):
    completed = _row("completed", result={"exit_code": 0, "stdout": "built", "stderr": ""})
    _, seen, _ = _shell(monkeypatch, completed)
    assert seen["not_after"] is None


def test_a_command_still_running_when_the_call_runs_out_says_so(monkeypatch):
    # The call has no time left to wait: its deadline minus the reserve is now.
    content, _, _ = _shell(monkeypatch, DISPATCHED, call_timeout=TOOL_ANSWER_RESERVE_SECONDS)
    assert "may still be running it" in content
    assert "time limit the agent run sets for one tool call" in content
    assert f"{default_result_wait_seconds()} s" in content
    assert "will not be sent again" in content
    assert "command history" in content


def test_a_command_whose_window_ended_says_what_the_window_was(monkeypatch):
    content, _, _ = _shell(monkeypatch, DISPATCHED)
    assert "no result came back within its window" in content
    assert f"{runner_execution_ceiling_seconds()} s to run it" in content
    assert f"{RUNNER_UPLOAD_RETRY_BUDGET_SECONDS} s to retry" in content
    assert "will not be sent again" in content
    assert "may still be running" not in content


def test_an_operator_set_wait_is_what_the_agent_is_told(monkeypatch):
    monkeypatch.setattr(settings, "runner_result_wait_timeout_seconds", 180)
    content, _, _ = _shell(monkeypatch, DISPATCHED)
    assert "within the 180 s this platform waits" in content
    assert "RUNNER_RESULT_WAIT_TIMEOUT_SECONDS" in content


def test_an_undelivered_command_the_call_ran_out_on_is_cancelled_with_why(monkeypatch):
    content, seen, _ = _shell(monkeypatch, PENDING, call_timeout=TOOL_ANSWER_RESERVE_SECONDS)
    assert "had not picked the command up" in seen["reason"]
    assert "time limit the agent run sets for one tool call" in seen["reason"]
    assert content == f"Runner command timeout: {seen['reason']}"


def test_an_undelivered_command_no_machine_took_keeps_its_message(monkeypatch):
    content, seen, _ = _shell(monkeypatch, PENDING)
    assert "never picked the command up" in seen["reason"]
    assert content == f"Runner command timeout: {seen['reason']}"


def test_a_timed_out_shell_command_hands_the_agent_its_partial_output(monkeypatch):
    # The runner ends the tree at the timeout and reports what the
    # command printed until then; the agent reads it rather than nothing.
    ended = _row(
        "timeout",
        error="command exceeded its 20s timeout",
        result={"exit_code": None, "stopped": "timeout", "stdout": "downloaded 40%", "stderr": ""},
    )
    content, _, _ = _shell(monkeypatch, ended)
    assert content.startswith("Runner command timeout: command exceeded its 20s timeout")
    assert "before it exited, with every process it started" in content
    assert "stdout (partial):\ndownloaded 40%" in content
    assert "stderr" not in content


def test_a_shell_command_the_user_stopped_tells_the_agent_so(monkeypatch):
    # Stop on the card ends the tree; the agent learns the user chose
    # it, when, and what the command printed, and is told not to rerun it.
    ended = _row(
        "failed",
        error="command stopped by the user",
        result={"exit_code": None, "stopped": "user", "stdout": "tick 1\ntick 2", "stderr": ""},
    )
    content, _, _ = _shell(monkeypatch, ended)
    assert content.startswith("The user stopped this command at ")
    assert re.search(r"stopped this command at \d{2}:\d{2}:\d{2} UTC", content)
    assert "Do not run it again unless the user asks" in content
    assert "stdout (partial):\ntick 1\ntick 2" in content


def test_a_timeout_without_output_keeps_its_one_line(monkeypatch):
    ended = _row("timeout", error="command exceeded its 20s timeout")
    content, _, _ = _shell(monkeypatch, ended)
    assert content == "Runner command timeout: command exceeded its 20s timeout"


def test_a_result_that_landed_during_the_give_up_is_the_answer(monkeypatch):
    landed = _row("completed", result={"exit_code": 0, "stdout": "late but here", "stderr": ""})
    content, _, _ = _shell(monkeypatch, DISPATCHED, landed=landed, call_timeout=TOOL_ANSWER_RESERVE_SECONDS)
    assert "late but here" in content
    assert "may still be running" not in content


# ── Recording the give-up, before the call's deadline ────────────────────────

#: A call with this long left: its result wait is over at once (the reserve is
#: longer), and the give-up gets all but the last second of what remains.
_SHORT_CALL = 1.2

#: When a held row lock is let go anyway, so that a call the loop cancels -
#: whose clean-up waits for the give-up - fails the test instead of hanging it.
_LOCK_HELD_AT_MOST = 3.0


def _row_lock() -> asyncio.Event:
    released = asyncio.Event()
    asyncio.get_running_loop().call_later(_LOCK_HELD_AT_MOST, released.set)
    return released


async def _in_flight_give_ups() -> None:
    """Let the give-ups a call left recording finish, and their callbacks run."""
    await asyncio.gather(*list(computer_use._GIVE_UPS_IN_FLIGHT), return_exceptions=True)
    await asyncio.sleep(0)


@pytest.mark.parametrize(
    ("waited", "said"),
    [
        (DISPATCHED, "may still be running it"),
        (PENDING, "Runner command timeout: Runner 'build-box' had not picked the command up"),
    ],
    ids=["delivered", "undelivered"],
)
async def test_a_give_up_that_outlasts_the_call_goes_on_by_itself(monkeypatch, waited, said):
    # The give-up takes the command's row lock, which a result upload in
    # flight holds while it stores its file: the call answers before the loop
    # would cancel it, and the give-up still lands afterwards.
    lock_released = _row_lock()
    recorded: list[str] = []

    async def _held_give_up(command_id, reason, **_kwargs):
        await lock_released.wait()
        recorded.append(command_id)
        return dict(waited)

    executor, _ = _wire_shell(monkeypatch, waited, give_up=_held_give_up)
    binding = bind_tool_event_sink(None, tool_name="run_shell", timeout=_SHORT_CALL)
    try:
        # The loop's own timeout around the call.
        result = await asyncio.wait_for(executor.execute_tool(_SHELL_CALL), _SHORT_CALL)
        assert time.monotonic() < binding.deadline - 0.5
        assert said in result.content
        assert recorded == []
    finally:
        reset_tool_event_sink(binding)
        lock_released.set()

    await _in_flight_give_ups()
    assert recorded == ["cmd-1"]
    assert not computer_use._GIVE_UPS_IN_FLIGHT


async def test_a_give_up_failing_after_its_call_answered_is_logged(monkeypatch, caplog):
    lock_released = _row_lock()

    async def _failing_give_up(command_id, reason, **_kwargs):
        await lock_released.wait()
        raise RuntimeError("database connection lost")

    executor, _ = _wire_shell(monkeypatch, DISPATCHED, give_up=_failing_give_up)
    binding = bind_tool_event_sink(None, tool_name="run_shell", timeout=_SHORT_CALL)
    try:
        result = await asyncio.wait_for(executor.execute_tool(_SHELL_CALL), _SHORT_CALL)
        assert "may still be running it" in result.content
        with caplog.at_level("WARNING"):
            lock_released.set()
            await _in_flight_give_ups()
    finally:
        reset_tool_event_sink(binding)
        lock_released.set()
    assert any(
        "cmd-1" in record.getMessage() and "database connection lost" in record.getMessage()
        for record in caplog.records
    )
