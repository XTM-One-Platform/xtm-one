"""A run re-run after a lost lease is charged one agentic action, not two.

When the orchestration engine loses an execution's lease - a worker restart, or
a heartbeat that could not reach the database for a lease period - it runs the
execution again from the start, and the re-run is charged again. The attempt
that lost its lease is not necessarily gone: only its heartbeat stops, and it
may still finish, or fail and credit its own charge back. So each attempt's
charge is fenced to that attempt (``app/services/attempt_charges.py``) and
credited by at most one of: the re-run that replaces it, its own failure
handler, the dead-letter.

The cases the fix is about, for a delegated task (a metering receipt per
attempt) and for an assignment run (a charge record on the run row):

- the lost attempt is still alive: the re-run credits its charge once, and
  neither its own failure handler nor a later charge of its own counts;
- the worker is dead: the dead-letter credits what nothing credited yet, once;
- the re-run completes: the run is charged one action in total.
"""

from __future__ import annotations

import inspect
import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.services import attempt_charges
from app.services import quota as quota_module
from app.services.attempt_charges import (
    QUOTA_ATTEMPT_FENCE_KEY,
    QUOTA_METERED_ATTEMPT_KEY,
    QUOTA_METERED_CHARGE_KEY,
    QUOTA_METERED_KEY,
    QUOTA_METERED_USER_KEY,
    RecordedCharge,
    attempt_fence,
    claim_charge_record,
    credit_abandoned_task,
    credit_lost_attempts,
    fence_attempt,
    lost_attempt_charge,
    open_pause_epoch,
    recorded_charge,
    release_charge_record,
    replaceable_charge_id,
    take_lost_charge,
    task_receipt_token,
    with_recorded_charge,
    without_charge_state,
    without_recorded_charge,
)
from app.services.quota import (
    _current_period_key,
    _failure_pool_key,
    _free_refund_key,
    _usage_key,
    claim_own_receipt,
    increment_agentic_usage,
    refund_agentic_usage_by_receipt,
)
from tests.agentic_gate_stub import stub_agentic_gate
from tests.test_agent_failure_quota_pool import _enable, _fake_redis, _window_key

TASK = "0c9b5d2e-3f41-4d8e-9a7b-6c1e2f3a4b5c"


def _user_count(redis, user_id) -> int:
    return int(redis.store.get(_usage_key(user_id, "agentic", None, _current_period_key("monthly")), 0))


async def _free_credits(redis, user_id, agent_id) -> int:
    return int(redis.store.get(_free_refund_key(user_id, agent_id, await _window_key()), 0))


async def _charge(user_id, agent_id, *, attempt: int | None) -> None:
    """What the task's agent turn charges, receipt included."""
    await increment_agentic_usage(
        user_id, agent_id=agent_id, receipt_token=task_receipt_token(TASK, attempt=attempt)
    )


# ---------------------------------------------------------------------------
# The receipt: redeemed once, and closed for good
# ---------------------------------------------------------------------------


async def test_a_redeemed_receipt_stays_redeemed(monkeypatch):
    redis = _fake_redis(monkeypatch)
    user_id, agent_id = uuid.uuid4(), uuid.uuid4()
    await _charge(user_id, agent_id, attempt=1)
    token = task_receipt_token(TASK, attempt=1)

    assert await refund_agentic_usage_by_receipt(token) is True
    assert redis.store[f"quota:receipt:{token}"] == "redeemed"
    # Nothing else credits it again: not a second dead-letter pass, not the
    # attempt's own failure handler.
    assert await refund_agentic_usage_by_receipt(token) is False
    assert await claim_own_receipt(token) is False
    assert _user_count(redis, user_id) == 0
    assert await _free_credits(redis, user_id, agent_id) == 1


async def test_a_charge_under_a_token_already_redeemed_is_given_straight_back(monkeypatch):
    """The re-run closed the attempt it replaced before that attempt reached
    its charge: the charge lands, and is given back as it lands."""
    redis = _fake_redis(monkeypatch)
    user_id, agent_id = uuid.uuid4(), uuid.uuid4()
    await increment_agentic_usage(user_id, agent_id=agent_id)  # another run's usage
    token = task_receipt_token(TASK, attempt=1)

    assert await refund_agentic_usage_by_receipt(token) is False
    await _charge(user_id, agent_id, attempt=1)

    assert _user_count(redis, user_id) == 1, "the other run's charge survives"
    assert redis.store[f"quota:receipt:{token}"] == "redeemed"
    # Not a credit: nothing was consumed, so nothing is tallied.
    assert await _free_credits(redis, user_id, agent_id) == 0


async def test_the_charging_turn_takes_its_own_receipt_before_crediting_itself(monkeypatch):
    redis = _fake_redis(monkeypatch)
    user_id, agent_id = uuid.uuid4(), uuid.uuid4()
    await _charge(user_id, agent_id, attempt=1)
    token = task_receipt_token(TASK, attempt=1)

    assert await claim_own_receipt(token) is True
    # The turn credits it; the dead-letter then has nothing to credit.
    assert await refund_agentic_usage_by_receipt(token) is False
    assert redis.store[f"quota:receipt:{token}"] == "redeemed"


async def test_a_turn_whose_receipt_was_never_written_still_credits_itself(monkeypatch):
    """The charge happened; only its receipt was lost. Nothing else can credit
    it, so the turn's own handler does, as it always did."""
    _fake_redis(monkeypatch)
    assert await claim_own_receipt(task_receipt_token(TASK, attempt=1)) is True


async def test_a_receipt_that_cannot_be_read_credits_nothing(monkeypatch):
    monkeypatch.setattr(quota_module, "redis_client", AsyncMock(side_effect=RuntimeError("down")))
    assert await claim_own_receipt(task_receipt_token(TASK, attempt=1)) is False


def test_the_receipt_names_the_attempt():
    assert task_receipt_token(TASK, attempt=2) == f"sub_agent_task:{TASK}:2"
    assert task_receipt_token(TASK, attempt=None) == f"sub_agent_task:{TASK}"


# ---------------------------------------------------------------------------
# Delegated tasks
# ---------------------------------------------------------------------------


async def test_a_stale_attempt_still_alive_is_credited_once_by_the_rerun(monkeypatch):
    """Attempt 1 lost its lease and kept running; attempt 2 re-runs the task.
    Attempt 1's charge goes back once, its failure handler credits nothing,
    and the task is charged the one action of attempt 2."""
    redis = _fake_redis(monkeypatch)
    _enable(monkeypatch)
    user_id, agent_id = uuid.uuid4(), uuid.uuid4()

    await _charge(user_id, agent_id, attempt=1)
    assert await credit_lost_attempts(TASK, attempt=2) == 1
    await _charge(user_id, agent_id, attempt=2)
    # Attempt 1's turn now fails: its handler asks before it credits.
    assert await claim_own_receipt(task_receipt_token(TASK, attempt=1)) is False
    # A held re-claim of attempt 2 asks again: nothing more comes back.
    assert await credit_lost_attempts(TASK, attempt=2) == 0

    assert _user_count(redis, user_id) == 1
    assert await _free_credits(redis, user_id, agent_id) == 1
    assert _failure_pool_key(user_id, agent_id, await _window_key()) not in redis.store


async def test_a_stale_attempt_that_charges_after_the_rerun_started_runs_uncharged(monkeypatch):
    redis = _fake_redis(monkeypatch)
    user_id, agent_id = uuid.uuid4(), uuid.uuid4()

    assert await credit_lost_attempts(TASK, attempt=2) == 0
    await _charge(user_id, agent_id, attempt=2)
    await _charge(user_id, agent_id, attempt=1)  # the stale attempt, late

    assert _user_count(redis, user_id) == 1


async def test_a_dead_worker_is_credited_by_the_dead_letter_once(monkeypatch):
    """Both attempts died: the re-run credited the first, the dead-letter
    credits the second, and a second dead-letter pass nothing."""
    redis = _fake_redis(monkeypatch)
    user_id, agent_id = uuid.uuid4(), uuid.uuid4()
    await increment_agentic_usage(user_id, agent_id=agent_id)  # another run's usage

    await _charge(user_id, agent_id, attempt=1)
    assert await credit_lost_attempts(TASK, attempt=2) == 1
    await _charge(user_id, agent_id, attempt=2)
    assert await credit_abandoned_task(TASK, attempts=2) == 1
    assert await credit_abandoned_task(TASK, attempts=2) == 0

    assert _user_count(redis, user_id) == 1, "only the other run's charge is left"
    assert await _free_credits(redis, user_id, agent_id) == 2


async def test_the_dead_letter_credits_a_charge_made_before_charges_were_fenced(monkeypatch):
    """A task charged by an older release wrote the task-wide receipt."""
    redis = _fake_redis(monkeypatch)
    user_id, agent_id = uuid.uuid4(), uuid.uuid4()
    await _charge(user_id, agent_id, attempt=None)

    assert await credit_abandoned_task(TASK, attempts=1) == 1
    assert _user_count(redis, user_id) == 0


async def test_a_rerun_that_completes_is_charged_one_action(monkeypatch):
    redis = _fake_redis(monkeypatch)
    user_id, agent_id = uuid.uuid4(), uuid.uuid4()

    await _charge(user_id, agent_id, attempt=1)
    await credit_lost_attempts(TASK, attempt=2)
    await _charge(user_id, agent_id, attempt=2)

    assert _user_count(redis, user_id) == 1


def test_the_turn_asks_before_each_credit_of_its_own():
    """``run_agent_chat`` credits a failed turn in three places; for a
    receipted charge each one redeems the receipt first."""
    from app.services import agent_chat

    src = inspect.getsource(agent_chat.run_agent_chat)
    refund = src[src.index("async def _refund_failed_turn(") :]
    refund = refund[: refund.index("all_kb_ids = ")]
    assert refund.index("if not await _charge_still_ours():") < refund.index(
        "refund_agentic_usage_for_provider_outage("
    )
    assert "if _refund_safe and _quota_metered and await _charge_still_ours():" in src
    assert "if _quota_metered and await _charge_still_ours():" in src
    assert "return await claim_own_receipt(quota_receipt_token)" in src


async def test_a_stale_turn_does_not_credit_a_charge_its_replacement_credited(monkeypatch):
    """Driven through ``run_agent_chat``: the attempt's turn is charged, the
    re-run credits that charge while the turn is still running, and the turn
    then fails before it reached a model - it credits nothing more."""
    from app.core import config
    from app.services.agent_chat import run_agent_chat
    from app.services.quota import _global_usage_key
    from tests.test_accountless_quota import DB, _chat_agent, _use_store

    redis = _fake_redis(monkeypatch)
    _use_store(monkeypatch, redis)
    # A turn with no account behind it: the platform-wide counter alone.
    monkeypatch.setattr(config.settings, "platform_mode", "copilot")
    monkeypatch.setattr(
        quota_module,
        "_load_platform_wide_config",
        AsyncMock(return_value={"platform_limit": 10, "period": "monthly"}),
    )
    monkeypatch.setattr("app.services.quota.check_agentic_quota", AsyncMock())
    ceiling = _global_usage_key("agentic", _current_period_key("monthly"))
    agent = _chat_agent(skip_llm=False)
    await increment_agentic_usage(None, db=DB, agent_id=agent.id)  # another run's usage

    async def _rerun_then_fail(*_args, **_kwargs):
        assert int(redis.store[ceiling]) == 2, "this turn was charged"
        await credit_lost_attempts(TASK, attempt=2)
        raise RuntimeError("no provider configured")

    monkeypatch.setattr("app.llm.router.get_llm_for_user", _rerun_then_fail)

    with pytest.raises(RuntimeError, match="no provider configured"):
        await run_agent_chat(
            db=DB,
            user_id="anonymous",
            agent=agent,
            history=[],
            user_message="ping",
            quota_receipt_token=task_receipt_token(TASK, attempt=1),
        )

    # Credited once, by the re-run: the other run's charge survives.
    assert int(redis.store[ceiling]) == 1


async def test_a_rerun_task_credits_the_attempts_before_it_before_its_turn(monkeypatch):
    from app.services import sub_agent
    from tests.test_background_edition_gate import (
        ENTERPRISE,
        _patch_task_collaborators,
        _pin,
        _task_row,
        _TaskSession,
    )

    _pin(monkeypatch, **ENTERPRISE)
    monkeypatch.setattr("app.services.quota.quota_store_out", AsyncMock(return_value=False))
    task = _task_row()
    target = SimpleNamespace(id=uuid.uuid4(), name="Exposure analyst", handover_agent_ids=[])
    monkeypatch.setattr("app.core.database.async_session", lambda: _TaskSession([task, target, task]))
    monkeypatch.setattr("app.api.v1.helpers.get_user_group_ids", AsyncMock(return_value=[]))
    collaborators = _patch_task_collaborators(monkeypatch)
    order: list[str] = []
    credit = AsyncMock(side_effect=lambda *_a, **_k: order.append("credit"))
    monkeypatch.setattr(sub_agent, "credit_lost_attempts", credit)
    collaborators.run_agent_chat.side_effect = lambda *_a, **_k: order.append("turn") or ("done", None)

    outcome = await sub_agent._execute_sub_agent_task(
        task_id=str(task.id),
        user_id=str(task.user_id),
        target_agent_id=str(target.id),
        task_description="Summarise the new exposures",
        timeout_seconds=600,
        attempt=3,
    )

    assert outcome.succeeded
    assert order == ["credit", "turn"]
    assert credit.await_args.args == (str(task.id),)
    assert credit.await_args.kwargs["attempt"] == 3
    turn = collaborators.run_agent_chat.await_args.kwargs
    assert turn["quota_receipt_token"] == f"sub_agent_task:{task.id}:3"


async def test_a_timeout_the_deadline_cut_credits_the_attempts_own_receipt(monkeypatch):
    """A re-run with a second and a half of its deadline left times its loop
    out on that bound: the engine's timeout, credited back free -
    through this attempt's receipt, the one its turn was charged under."""
    import asyncio
    from datetime import UTC, datetime, timedelta
    from unittest.mock import patch

    from app.core.config import settings
    from app.services import sub_agent
    from tests.test_sub_agent_result_files import (
        _exec_db,
        _FakeSessionCM,
        _incremental_writer,
        _task_and_agent,
    )

    stub_agentic_gate(monkeypatch)
    receipt = AsyncMock(return_value=True)
    monkeypatch.setattr("app.services.quota.refund_agentic_usage_by_receipt", receipt)
    lost = AsyncMock(return_value=1)
    monkeypatch.setattr(sub_agent, "credit_lost_attempts", lost)
    seen: dict = {}

    async def _chat(*_args, **kwargs):
        seen["token"] = kwargs["quota_receipt_token"]
        await asyncio.sleep(30)
        return "never", None

    task, agent = _task_and_agent()
    task_id = str(uuid.uuid4())
    now = datetime.now(UTC)
    spawned = now - timedelta(seconds=1800 - 1.5)

    class _FrozenClock(datetime):
        """The deadline's clock stands still: 1.5 s of runnable time stay
        1.5 s however slow the setup runs under pytest -n."""

        @classmethod
        def now(cls, tz=None):
            return now if tz is not None else now.replace(tzinfo=None)

    with (
        patch("app.services.orchestration_engine.datetime", _FrozenClock),
        patch("app.core.database.async_session", lambda: _FakeSessionCM(_exec_db(task, agent))),
        patch("app.services.agent_chat.run_agent_chat", new=AsyncMock(side_effect=_chat)),
        patch("app.services.sub_agent._update_task_delegation", new=AsyncMock()),
        patch("app.services.sub_agent_files.write_task_result_file_ids", new=_incremental_writer()),
    ):
        outcome = await sub_agent._execute_sub_agent_task(
            task_id=task_id,
            user_id=str(uuid.uuid4()),
            target_agent_id=str(uuid.uuid4()),
            task_description="triage the open alerts",
            timeout_seconds=1800,
            deadline_at=spawned + timedelta(seconds=1800 + settings.orchestration_grace_seconds),
            attempt=2,
        )

    assert outcome.status == "failed"
    assert lost.await_args.kwargs["attempt"] == 2
    assert seen["token"] == f"sub_agent_task:{task_id}:2"
    receipt.assert_awaited_once()
    assert receipt.await_args.args[0] == f"sub_agent_task:{task_id}:2"


async def test_a_first_attempt_credits_nothing(monkeypatch):
    from app.services import sub_agent
    from tests.test_background_edition_gate import (
        ENTERPRISE,
        _patch_task_collaborators,
        _pin,
        _task_row,
        _TaskSession,
    )

    _pin(monkeypatch, **ENTERPRISE)
    monkeypatch.setattr("app.services.quota.quota_store_out", AsyncMock(return_value=False))
    task = _task_row()
    target = SimpleNamespace(id=uuid.uuid4(), name="Exposure analyst", handover_agent_ids=[])
    monkeypatch.setattr("app.core.database.async_session", lambda: _TaskSession([task, target, task]))
    monkeypatch.setattr("app.api.v1.helpers.get_user_group_ids", AsyncMock(return_value=[]))
    collaborators = _patch_task_collaborators(monkeypatch)
    credit = AsyncMock()
    monkeypatch.setattr(sub_agent, "credit_lost_attempts", credit)

    await sub_agent._execute_sub_agent_task(
        task_id=str(task.id),
        user_id=str(task.user_id),
        target_agent_id=str(target.id),
        task_description="Summarise the new exposures",
        timeout_seconds=600,
        attempt=1,
    )

    credit.assert_not_awaited()
    turn = collaborators.run_agent_chat.await_args.kwargs
    assert turn["quota_receipt_token"] == f"sub_agent_task:{task.id}:1"


async def test_the_adapter_runs_the_task_under_its_attempt(monkeypatch):
    from app.services.orchestration_adapters.sub_agent import SubAgentAdapter
    from app.services.sub_agent import SubAgentTaskOutcome

    body = AsyncMock(return_value=SubAgentTaskOutcome(status="completed"))
    monkeypatch.setattr("app.services.sub_agent._execute_sub_agent_task", body)
    session = SimpleNamespace(
        execute=AsyncMock(return_value=SimpleNamespace(scalar_one_or_none=lambda: None)),
        commit=AsyncMock(),
    )
    execution = SimpleNamespace(
        payload={
            "task_id": TASK,
            "target_agent_id": str(uuid.uuid4()),
            "user_id": str(uuid.uuid4()),
            "task_description": "do it",
        },
        attempt_count=2,
    )

    result = await SubAgentAdapter().execute(SimpleNamespace(db=session), execution)

    assert result.kind == "completed"
    assert body.await_args.kwargs["attempt"] == 2


@pytest.mark.parametrize(
    ("status", "credited"),
    [("running", True), ("pending", True), ("completed", False), ("incomplete", False), ("failed", True)],
)
async def test_the_dead_letter_credits_a_task_only_if_it_had_not_ended(monkeypatch, status, credited):
    """A task whose work ran keeps its charge. A failed one may still hold the
    charge of the attempt that replaced the one that failed it; a handler that
    credited its own charge redeemed that receipt already."""
    from app.services.orchestration_adapters.sub_agent import SubAgentAdapter

    credit = AsyncMock(return_value=0)
    monkeypatch.setattr(attempt_charges, "credit_abandoned_task", credit)
    monkeypatch.setattr("app.services.sub_agent._update_task_delegation", AsyncMock())
    task = SimpleNamespace(status=status, error=None, completed_at=None, conversation_id=None)
    session = SimpleNamespace(
        execute=AsyncMock(return_value=SimpleNamespace(scalar_one_or_none=lambda: task)),
        commit=AsyncMock(),
    )
    execution = SimpleNamespace(payload={"task_id": TASK}, last_error="worker lost", attempt_count=3)

    await SubAgentAdapter().on_dead_letter(SimpleNamespace(db=session), execution)

    if credited:
        credit.assert_awaited_once()
        assert credit.await_args.args == (TASK,)
        assert credit.await_args.kwargs["attempts"] == 3
        # Only an abandoned task's task-wide receipt: a failed task's is a
        # release whose failure handler credited without redeeming it.
        assert credit.await_args.kwargs["include_task_wide"] is (status != "failed")
    else:
        credit.assert_not_awaited()


async def test_a_failed_task_is_credited_the_attempt_receipts_nobody_redeemed(monkeypatch):
    """Attempt 1 failed the task after attempt 2 redeemed its receipt; attempt 2
    is then dead-lettered with its own charge outstanding. A task-wide receipt
    of an older release is left alone: its failure credited it already."""
    redis = _fake_redis(monkeypatch)
    user_id, agent_id = uuid.uuid4(), uuid.uuid4()
    await _charge(user_id, agent_id, attempt=None)
    await _charge(user_id, agent_id, attempt=1)
    assert await credit_lost_attempts(TASK, attempt=2) == 1
    await _charge(user_id, agent_id, attempt=2)

    assert await credit_abandoned_task(TASK, attempts=2, include_task_wide=False) == 1
    assert await credit_abandoned_task(TASK, attempts=2, include_task_wide=False) == 0
    assert _user_count(redis, user_id) == 1, "the task-wide charge is not credited"


# ---------------------------------------------------------------------------
# Assignment runs: the charge record on the run row
# ---------------------------------------------------------------------------


class _RowSession:
    """One run row, as the database holds it, next to the run in memory.

    The locked read of the run's context answers with the row; a commit writes
    the in-memory context to the row only when it was assigned since the last
    read or commit, as an ORM flush of a changed attribute does.
    """

    def __init__(self, run, row_context):
        self.run = run
        self.row = {"trigger_context": row_context}
        self.commits = 0
        self.fail = False
        self._synced = run.trigger_context

    async def execute(self, _stmt, *_args, **_kwargs):
        if self.fail:
            raise RuntimeError("database is gone")
        context = self.row["trigger_context"]
        return SimpleNamespace(scalars=lambda: SimpleNamespace(all=lambda: [context]))

    async def commit(self):
        if self.fail:
            raise RuntimeError("database is gone")
        if self.run.trigger_context is not self._synced:
            self.row["trigger_context"] = self.run.trigger_context
            self._synced = self.run.trigger_context
        self.commits += 1


def _run(status="running", context=None):
    return SimpleNamespace(id=uuid.uuid4(), agent_id=uuid.uuid4(), status=status, trigger_context=context)


USER = uuid.UUID("5d9a3c1e-7b2f-4e8d-a6c4-1f0e9b8d7c6a")


def _recorded(charge_id: str | None, attempt: int | None = None, **extra) -> dict:
    context = {"type": "schedule", QUOTA_METERED_KEY: True, QUOTA_METERED_USER_KEY: str(USER), **extra}
    if charge_id is not None:
        context[QUOTA_METERED_CHARGE_KEY] = charge_id
    if attempt is not None:
        context[QUOTA_METERED_ATTEMPT_KEY] = attempt
    return context


def test_the_record_reads_back_whom_and_which_attempt():
    assert recorded_charge(_recorded("c1", 2)) == RecordedCharge(user_id=USER, charge_id="c1", attempt=2)
    assert recorded_charge(_recorded(None)) == RecordedCharge(user_id=USER, charge_id=None)
    no_account = with_recorded_charge({}, charge_id="c1", user_id=None, attempt=1)
    assert recorded_charge(no_account) == RecordedCharge(user_id=None, charge_id="c1", attempt=1)
    # A new record replaces the attempt of the one before it.
    assert QUOTA_METERED_ATTEMPT_KEY not in with_recorded_charge(
        _recorded("c1", 2), charge_id="c2", user_id=USER
    )
    # Not a charge anyone may credit back.
    assert recorded_charge({QUOTA_METERED_KEY: True}) is None
    assert recorded_charge({QUOTA_METERED_KEY: True, QUOTA_METERED_USER_KEY: "nope"}) is None
    assert recorded_charge({"type": "schedule"}) is None
    assert without_recorded_charge(_recorded("c1", 2)) == {"type": "schedule"}
    assert without_recorded_charge(None) is None


def test_only_the_charge_of_an_earlier_attempt_is_a_lost_one():
    assert lost_attempt_charge(_run("running", _recorded("c1", 1)), attempt=2).charge_id == "c1"
    assert lost_attempt_charge(_run("pending", _recorded("c1", 1)), attempt=2).charge_id == "c1"
    # The charge of a later attempt: this one is the stale attempt.
    assert lost_attempt_charge(_run("running", _recorded("c3", 3)), attempt=2) is None
    assert lost_attempt_charge(_run("running", _recorded("c2", 2)), attempt=2) is None
    # A paused run keeps the charge of the attempt that paused it.
    assert lost_attempt_charge(_run("awaiting_human", _recorded("c1", 1)), attempt=2) is None
    # A charge recorded before charges were fenced: the dead-letter's alone.
    assert lost_attempt_charge(_run("running", _recorded(None)), attempt=2) is None
    assert lost_attempt_charge(_run("running", {"type": "schedule"}), attempt=2) is None


def test_only_a_paused_runs_charge_may_be_replaced_by_a_record():
    assert replaceable_charge_id(_run("awaiting_human", _recorded("paused", 3))) == "paused"
    assert replaceable_charge_id(_run("running", _recorded("newer", 3))) is None
    assert replaceable_charge_id(_run("pending", {"type": "schedule"})) is None


async def test_the_lost_attempts_charge_is_taken_once():
    run = _run(context=_recorded("lost", 1))
    db = _RowSession(run, _recorded("lost", 1))

    charge = await take_lost_charge(db, run, attempt=2)

    assert charge == RecordedCharge(user_id=USER, charge_id="lost", attempt=1)
    # Marked settling, kept until the credit landed.
    assert attempt_charges.is_settling(db.row["trigger_context"])
    assert recorded_charge(db.row["trigger_context"]).charge_id == "lost"
    assert await take_lost_charge(db, run, attempt=2) is None
    await attempt_charges.settle_lost_charge(db, run, charge_id="lost")
    assert db.row["trigger_context"] == {"type": "schedule"}
    assert run.trigger_context == {"type": "schedule"}
    assert await take_lost_charge(db, run, attempt=2) is None


# ---------------------------------------------------------------------------
# The lost attempt's credit is durable
# ---------------------------------------------------------------------------


async def _dead_letter_credits(monkeypatch, run, db) -> AsyncMock:
    """The dead letter's settlement of a failed run, its credit counted."""
    from app.services.orchestration_adapters.assignment import AssignmentAdapter

    refund = AsyncMock(return_value=quota_module.CHARGE_CREDIT_LANDED)
    monkeypatch.setattr(quota_module, "credit_agentic_charge_once", refund)
    run.status = "failed"
    run.trigger_context = db.row["trigger_context"]
    await AssignmentAdapter._settle_failed_run_charge(SimpleNamespace(db=db), run)
    return refund


async def test_a_failed_credit_leaves_the_record_to_the_dead_letter_once(monkeypatch):
    run = _run(context=_recorded("lost", 1))
    db = _RowSession(run, _recorded("lost", 1))
    assert await take_lost_charge(db, run, attempt=2) is not None
    # The credit returned False: nothing settles the record.

    assert recorded_charge(db.row["trigger_context"]).charge_id == "lost"
    assert lost_attempt_charge(_run("running", db.row["trigger_context"]), attempt=3) is None, (
        "another attempt never credits it again"
    )
    refund = await _dead_letter_credits(monkeypatch, run, db)
    refund.assert_awaited_once()
    assert recorded_charge(db.row["trigger_context"]) is None
    refund2 = await _dead_letter_credits(monkeypatch, run, db)
    refund2.assert_not_awaited()


async def test_a_worker_dying_after_the_take_leaves_the_record_to_the_dead_letter(monkeypatch):
    run = _run(context=_recorded("lost", 1))
    db = _RowSession(run, _recorded("lost", 1))
    assert await take_lost_charge(db, run, attempt=2) is not None
    # The worker dies here (or the credit raises): the marked record stays.
    dead = _run("running", db.row["trigger_context"])
    dead.id = run.id

    refund = await _dead_letter_credits(monkeypatch, dead, db)

    refund.assert_awaited_once()


async def test_a_credit_that_landed_leaves_nothing_to_the_dead_letter(monkeypatch):
    run = _run(context=_recorded("lost", 1))
    db = _RowSession(run, _recorded("lost", 1))
    assert await take_lost_charge(db, run, attempt=2) is not None
    await attempt_charges.settle_lost_charge(db, run, charge_id="lost")

    refund = await _dead_letter_credits(monkeypatch, run, db)

    refund.assert_not_awaited()


async def test_settling_a_record_that_changed_meanwhile_leaves_it():
    run = _run(context=_recorded("lost", 1))
    db = _RowSession(run, _recorded("newer", 3))

    await attempt_charges.settle_lost_charge(db, run, charge_id="lost")

    assert recorded_charge(db.row["trigger_context"]).charge_id == "newer"


def test_the_engine_settles_the_lost_charge_only_once_credited():
    from app.services import assignment_engine

    src = inspect.getsource(assignment_engine.execute_assignment_run)
    credit = src.index("and await credit_recorded_charge(")
    assert credit < src.index("await settle_lost_charge(db, run, charge_id=lost_charge.charge_id)")


# ---------------------------------------------------------------------------
# The lost attempt's credit lands once
# ---------------------------------------------------------------------------


async def _engine_credit(run, charge) -> bool:
    """The engine's credit of the lost attempt's charge, through the atomic
    script against the in-memory Redis."""
    return await attempt_charges.credit_recorded_charge(
        run, charge, db=None, reason="lost attempt", cause="engine_abandoned"
    )


class _NoDatabase:
    """The quota service's own session, for a credit handed the run's: no
    quota config or ceiling is read, so the default period applies."""

    async def __aenter__(self):
        return None

    async def __aexit__(self, *_exc):
        return False


def _quota_redis(monkeypatch):
    """The in-memory Redis, the credit's own session standing in for none."""
    monkeypatch.setattr("app.core.database.async_session", _NoDatabase)
    return _fake_redis(monkeypatch)


def _charged(monkeypatch, count: int = 1):
    """An in-memory Redis holding *count* agentic actions charged to USER."""
    redis = _quota_redis(monkeypatch)
    period_key = quota_module._current_period_key("monthly")
    redis.store[quota_module._usage_key(USER, "agentic", None, period_key)] = count
    redis.store[quota_module._global_usage_key("agentic", period_key)] = count
    return redis, quota_module._usage_key(USER, "agentic", None, period_key)


async def _dead_letter(db, run) -> None:
    from app.services.orchestration_adapters.assignment import AssignmentAdapter

    run.status = "failed"
    run.trigger_context = db.row["trigger_context"]
    await AssignmentAdapter._settle_failed_run_charge(SimpleNamespace(db=db), run)


async def test_e_a_credit_that_landed_before_the_worker_died_is_not_replayed(monkeypatch):
    """(e) The credit lands, the worker dies before ``settle_lost_charge``: the
    marked record stays on the run, and the dead letter credits nothing more
    (the receipt reads credited) but takes the record off."""
    redis, user_key = _charged(monkeypatch)
    run = _run(context=_recorded("lost", 1))
    db = _RowSession(run, _recorded("lost", 1))
    charge = await take_lost_charge(db, run, attempt=2)

    assert await _engine_credit(run, charge) is True
    assert redis.store[user_key] == 0
    # The worker dies here: settle_lost_charge never runs.
    assert attempt_charges.is_settling(db.row["trigger_context"])
    redis.store[user_key] = 1  # another charge since: must not be taken
    await _dead_letter(db, run)

    assert redis.store[user_key] == 1
    assert recorded_charge(db.row["trigger_context"]) is None


async def test_g_two_settlers_of_one_charge_credit_it_once(monkeypatch):
    """(g) The engine and the dead letter both reach the same charge: the one
    script keyed on the charge id lets one credit through."""
    import asyncio

    redis, user_key = _charged(monkeypatch, count=5)
    run = _run(context=_recorded("lost", 1))
    charge = RecordedCharge(user_id=USER, charge_id="lost", attempt=1)

    results = await asyncio.gather(_engine_credit(run, charge), _engine_credit(run, charge))

    assert results == [True, True], "both may take the record off the run"
    assert redis.store[user_key] == 4
    # Another charge of the same run has a receipt of its own.
    other = RecordedCharge(user_id=USER, charge_id="rerun", attempt=2)
    assert await _engine_credit(run, other) is True
    assert redis.store[user_key] == 3


async def test_h_bookkeeping_that_raises_after_the_script_is_not_replayed(monkeypatch):
    """(h) The counters moved, then the free
    tally raised. The credit landed (one decrement, the receipt credited), and
    the dead letter credits nothing more."""
    redis, user_key = _charged(monkeypatch, count=2)
    monkeypatch.setattr(
        quota_module, "_tally_free_credit", AsyncMock(side_effect=RuntimeError("tally down"))
    )
    run = _run(context=_recorded("lost", 1))
    db = _RowSession(run, _recorded("lost", 1))
    charge = await take_lost_charge(db, run, attempt=2)

    assert await _engine_credit(run, charge) is True
    assert redis.store[user_key] == 1
    token = attempt_charges.charge_receipt_token(run.id, "lost")
    assert redis.store[f"quota:receipt:{token}"] == "credited"
    # The worker dies before settling: the dead letter credits nothing more.
    await _dead_letter(db, run)

    assert redis.store[user_key] == 1
    assert recorded_charge(db.row["trigger_context"]) is None


async def test_j_a_script_whose_answer_is_lost_is_credited_once(monkeypatch):
    """(j) The script ran but its answer was lost (the call raised after it):
    the credit says not applied, the record stays, and the retry reads the
    receipt credited - one decrement in all."""
    redis, user_key = _charged(monkeypatch, count=2)
    real_eval = redis.eval
    calls = []

    async def _eval_then_lose_the_answer(script, numkeys, *keys_and_args):
        result = await real_eval(script, numkeys, *keys_and_args)
        if script == quota_module._CREDIT_CHARGE_ONCE_LUA and not calls:
            calls.append(result)
            raise ConnectionError("connection reset after the reply was sent")
        return result

    monkeypatch.setattr(redis, "eval", _eval_then_lose_the_answer)
    run = _run(context=_recorded("lost", 1))
    db = _RowSession(run, _recorded("lost", 1))
    charge = await take_lost_charge(db, run, attempt=2)

    assert await _engine_credit(run, charge) is False
    assert recorded_charge(db.row["trigger_context"]).charge_id == "lost"
    assert redis.store[user_key] == 1
    await _dead_letter(db, run)

    assert redis.store[user_key] == 1, "one decrement in all"
    assert recorded_charge(db.row["trigger_context"]) is None


async def test_m_a_dead_letter_whose_credit_did_not_apply_keeps_the_record(monkeypatch):
    """(m) The dead letter's credit answers False: the record stays on the
    run, marked settling, and the next pass credits it once."""
    credit = AsyncMock(return_value=quota_module.CHARGE_CREDIT_NOT_APPLIED)
    monkeypatch.setattr(quota_module, "credit_agentic_charge_once", credit)
    run = _run(context=_recorded("lost", 1))
    db = _RowSession(run, _recorded("lost", 1))

    await _dead_letter(db, run)

    assert recorded_charge(db.row["trigger_context"]).charge_id == "lost"
    assert attempt_charges.is_settling(db.row["trigger_context"])
    assert lost_attempt_charge(_run("running", db.row["trigger_context"]), attempt=3) is None
    credit.return_value = quota_module.CHARGE_CREDIT_LANDED
    await _dead_letter(db, run)
    await _dead_letter(db, run)

    assert credit.await_count == 2
    assert recorded_charge(db.row["trigger_context"]) is None


async def test_o_redis_down_on_the_first_command_moves_nothing(monkeypatch):
    """(o) Redis raises on the first command: not applied, nothing moved, the
    record stays, and the next pass credits it once."""
    redis, user_key = _charged(monkeypatch, count=3)
    monkeypatch.setattr(quota_module, "redis_client", AsyncMock(side_effect=ConnectionError("down")))
    run = _run(context=_recorded("lost", 1))
    db = _RowSession(run, _recorded("lost", 1))
    charge = await take_lost_charge(db, run, attempt=2)

    assert await _engine_credit(run, charge) is False
    assert redis.store[user_key] == 3
    assert recorded_charge(db.row["trigger_context"]).charge_id == "lost"

    monkeypatch.setattr(quota_module, "redis_client", AsyncMock(return_value=redis))
    await _dead_letter(db, run)
    await _dead_letter(db, run)

    assert redis.store[user_key] == 2
    assert recorded_charge(db.row["trigger_context"]) is None


async def test_the_credit_floors_every_counter_at_zero(monkeypatch):
    """A counter already at zero (a period reset) or missing stays as it is;
    the receipt is credited all the same."""
    redis = _quota_redis(monkeypatch)
    run = _run(context=_recorded("lost", 1))
    charge = RecordedCharge(user_id=USER, charge_id="lost", attempt=1)
    period_key = quota_module._current_period_key("monthly")
    user_key = quota_module._usage_key(USER, "agentic", None, period_key)
    redis.store[user_key] = 0

    assert await _engine_credit(run, charge) is True

    assert redis.store[user_key] == 0
    assert quota_module._global_usage_key("agentic", period_key) not in redis.store
    token = attempt_charges.charge_receipt_token(run.id, "lost")
    assert redis.store[f"quota:receipt:{token}"] == "credited"


async def test_a_settling_record_is_left_by_the_running_attempt(monkeypatch):
    """The attempt the dead letter gave up on is still running: its own
    failure handler finds the record settling and credits nothing of its own."""
    credit = AsyncMock(return_value=quota_module.CHARGE_CREDIT_NOT_APPLIED)
    monkeypatch.setattr(quota_module, "credit_agentic_charge_once", credit)
    run = _run(context=_recorded("owner", 1))
    db = _RowSession(run, _recorded("owner", 1))
    await _dead_letter(db, run)

    owner = _run("running", db.row["trigger_context"])
    owner.id = run.id
    assert await release_charge_record(db, owner, charge_id="owner") is False
    assert recorded_charge(db.row["trigger_context"]).charge_id == "owner"


async def test_a_charge_taken_meanwhile_is_not_taken_again():
    run = _run(context=_recorded("lost", 1))
    db = _RowSession(run, {"type": "schedule"})

    assert await take_lost_charge(db, run, attempt=2) is None
    assert run.trigger_context == {"type": "schedule"}, "memory follows the row"


async def test_the_charge_is_recorded_when_nothing_changed_since_the_run_was_loaded():
    run = _run("pending", {"type": "schedule"})
    db = _RowSession(run, {"type": "schedule"})

    assert await claim_charge_record(db, run, charge_id="mine", attempt=1, user_id=USER, expected=None)
    await db.commit()

    assert recorded_charge(db.row["trigger_context"]) == RecordedCharge(
        user_id=USER, charge_id="mine", attempt=1
    )
    assert db.row["trigger_context"]["type"] == "schedule"


async def test_a_paused_runs_record_is_replaced_by_its_resume():
    """A resume starts its attempts over: its record replaces the charge of
    the attempt that paused the run, whatever that attempt's number."""
    run = _run("awaiting_human", _recorded("paused", 3))
    db = _RowSession(run, _recorded("paused", 3))

    expected = replaceable_charge_id(run)
    assert await claim_charge_record(db, run, charge_id="resume", attempt=1, user_id=USER, expected=expected)
    await db.commit()

    assert recorded_charge(db.row["trigger_context"]).charge_id == "resume"


async def test_an_attempt_replaced_before_it_charged_records_nothing():
    """Loaded with no charge recorded; by the time it charged, the attempt that
    replaced it had recorded its own. Its commit must not erase that one."""
    run = _run("pending", {"type": "schedule"})
    db = _RowSession(run, _recorded("newer", 2))

    assert not await claim_charge_record(db, run, charge_id="mine", attempt=1, user_id=USER, expected=None)
    await db.commit()

    assert recorded_charge(db.row["trigger_context"]).charge_id == "newer"


async def test_a_stale_attempt_that_starts_late_neither_takes_nor_replaces_the_newer_charge():
    """Attempt 1 only loads the run after attempt 2 recorded its charge: that
    charge is not a lost attempt's, and attempt 1 gives its own back."""
    run = _run("running", _recorded("newer", 2))
    db = _RowSession(run, _recorded("newer", 2))

    assert await take_lost_charge(db, run, attempt=1) is None
    assert not await claim_charge_record(
        db, run, charge_id="stale", attempt=1, user_id=USER, expected=replaceable_charge_id(run)
    )
    await db.commit()

    assert recorded_charge(db.row["trigger_context"]).charge_id == "newer"


async def test_the_failure_handler_credits_only_its_own_charge():
    run = _run(context=_recorded("mine"))
    db = _RowSession(run, _recorded("mine"))
    assert await release_charge_record(db, run, charge_id="mine") is True
    assert db.row["trigger_context"] == {"type": "schedule"}, "taken off, and committed"
    assert db.commits == 1

    # The re-run already took it: nothing to credit, nothing written.
    run = _run(context=_recorded("mine"))
    db = _RowSession(run, _recorded("newer"))
    assert await release_charge_record(db, run, charge_id="mine") is False
    assert recorded_charge(db.row["trigger_context"]).charge_id == "newer"
    assert db.commits == 0


async def test_the_failure_handler_of_a_deleted_run_still_credits():
    run = _run(context=_recorded("mine"))
    db = SimpleNamespace(
        execute=AsyncMock(return_value=SimpleNamespace(scalars=lambda: SimpleNamespace(all=list))),
        commit=AsyncMock(),
    )
    assert await release_charge_record(db, run, charge_id="mine") is True


async def test_a_record_that_cannot_be_read_is_left_to_the_engine():
    run = _run(context=_recorded("mine"))
    db = _RowSession(run, _recorded("mine"))
    db.fail = True
    assert await release_charge_record(db, run, charge_id="mine") is False
    assert recorded_charge(db.row["trigger_context"]).charge_id == "mine"


def test_the_record_read_never_flushes_the_attempts_own_copy():
    """Flushing first would write the attempt's in-memory context over what
    another attempt committed meanwhile."""
    src = inspect.getsource(attempt_charges._locked_trigger_context)
    assert ".with_for_update()" in src
    assert ".execution_options(autoflush=False)" in src


def test_a_retry_run_never_inherits_the_failed_runs_charge():
    """Its first attempt would otherwise take that charge for a lost one and
    credit it again."""
    from app.services import assignment_engine

    src = inspect.getsource(assignment_engine.execute_assignment_run)
    # Wrapped by ``without_resume_state``: a retry starts without the resumes too.
    assert "without_charge_state(run.trigger_context)" in src
    assert "trigger_context=assignment_run_resume.without_resume_state(" in src
    context = attempt_charges._with_fence(_recorded("c1", 2), (1, 2))
    assert without_charge_state(context) == {"type": "schedule"}
    # Taking a record keeps the fence: it orders the run's attempts.
    assert attempt_fence(without_recorded_charge(context)) == (1, 2)


def test_the_fence_reads_back_and_tolerates_what_it_cannot_parse():
    assert attempt_fence({QUOTA_ATTEMPT_FENCE_KEY: [2, 3]}) == (2, 3)
    for broken in (None, {}, {QUOTA_ATTEMPT_FENCE_KEY: "2:3"}, {QUOTA_ATTEMPT_FENCE_KEY: [1]},
                   {QUOTA_ATTEMPT_FENCE_KEY: [True, 2]}, {QUOTA_ATTEMPT_FENCE_KEY: [-1, 2]}):
        assert attempt_fence(broken) == (0, 0)
    record = with_recorded_charge({}, charge_id="c1", user_id=USER, attempt=2, epoch=3)
    assert recorded_charge(record) == RecordedCharge(user_id=USER, charge_id="c1", attempt=2, epoch=3)


async def test_a_first_attempt_places_itself_without_writing():
    run = _run("pending", {"type": "schedule", QUOTA_ATTEMPT_FENCE_KEY: [1, 0]})
    db = _RowSession(run, dict(run.trigger_context))

    assert await fence_attempt(db, run, attempt=1) == (1, 1)
    assert await fence_attempt(db, run, attempt=None) is None
    assert db.commits == 0


async def test_an_older_attempt_that_charges_after_the_rerun_started_records_nothing():
    """The re-run started before the attempt it replaces had charged: that
    attempt's claim is refused although the re-run has not charged yet, so it
    can neither record a charge the re-run would give back, nor credit the only
    one left when it fails."""
    run2 = _run("running", {"type": "schedule"})
    db = _RowSession(run2, {"type": "schedule"})
    assert await fence_attempt(db, run2, attempt=2) == (0, 2)
    assert db.commits == 1
    assert attempt_fence(db.row["trigger_context"]) == (0, 2)

    run1 = _run("running", {"type": "schedule"})  # loaded before the fence
    db.run, db._synced = run1, run1.trigger_context
    assert not await claim_charge_record(db, run1, charge_id="stale", attempt=1, user_id=USER, expected=None)
    await db.commit()
    assert recorded_charge(db.row["trigger_context"]) is None

    db.run, db._synced = run2, run2.trigger_context
    assert await claim_charge_record(db, run2, charge_id="rerun", attempt=2, user_id=USER, expected=None)
    await db.commit()
    assert recorded_charge(db.row["trigger_context"]).charge_id == "rerun"
    assert attempt_fence(db.row["trigger_context"]) == (0, 2)


async def test_a_pause_opens_the_next_epoch_for_its_resume():
    """A resume counts its attempts from 1 again: attempt 1 of the resume is
    the newest, and an attempt of before the pause is replaced."""
    paused = _recorded("paused", 2, **{QUOTA_ATTEMPT_FENCE_KEY: [0, 2]})
    run = _run("running", dict(paused))
    db = _RowSession(run, dict(paused))
    await open_pause_epoch(db, run)
    await db.commit()
    assert attempt_fence(db.row["trigger_context"]) == (1, 0)
    assert recorded_charge(db.row["trigger_context"]).charge_id == "paused"

    resume = _run("awaiting_human", dict(db.row["trigger_context"]))
    db.run, db._synced = resume, resume.trigger_context
    place = await fence_attempt(db, resume, attempt=1)
    assert place == (1, 1)
    assert await claim_charge_record(
        db, resume, charge_id="resume", attempt=1, user_id=USER,
        expected=replaceable_charge_id(resume), epoch=place[0],
    )
    await db.commit()
    assert recorded_charge(db.row["trigger_context"]) == RecordedCharge(
        user_id=USER, charge_id="resume", attempt=1, epoch=1
    )

    before_pause = _run("running", dict(paused))
    db.run, db._synced = before_pause, before_pause.trigger_context
    assert not await claim_charge_record(
        db, before_pause, charge_id="old", attempt=2, user_id=USER, expected=None, epoch=0
    )


def test_a_charge_of_another_pause_cycle_is_never_a_lost_one():
    """A resume attempt that ran unmetered left the paused work's charge on the
    run: the resume's re-run must not credit that work."""
    before = _recorded("paused", 1)
    assert lost_attempt_charge(_run("running", before), attempt=2, epoch=1) is None
    same = with_recorded_charge({}, charge_id="c1", user_id=USER, attempt=1, epoch=1)
    assert lost_attempt_charge(_run("running", same), attempt=2, epoch=1).charge_id == "c1"


def test_the_pause_opens_its_epoch_before_it_commits():
    from app.services import assignment_engine

    src = inspect.getsource(assignment_engine.execute_assignment_run)
    pause = src.index("run.messages = serialize_messages(hitl.conversation)")
    assert src.index("await open_pause_epoch(db, run)", pause) < src.index("await db.commit()", pause)


# -- ``execute_assignment_run`` end to end ------------------------------------


class _EngineRowSession(_RowSession):
    """The engine test session of ``test_assignment_stale_reference``, whose run
    row may diverge from the run in memory (another attempt's writes)."""

    def __init__(self, run, assignment, agent):
        super().__init__(run, run.trigger_context)
        self._queue = [run, assignment, agent]
        self.added: list = []
        self.statements: list = []

    async def execute(self, stmt, *args, **kwargs):
        from tests.test_assignment_stale_reference import INTEGRATION_ID, _EngineResult

        self.statements.append(stmt)
        if str(stmt).startswith("SELECT assignment_runs.status, assignment_runs.trigger_context"):
            # The ending check: no other writer ended the run.
            return _EngineResult(rows=[("running", self.row["trigger_context"])])
        if str(stmt).startswith("SELECT assignment_runs.trigger_context"):
            return _EngineResult(rows=[self.row["trigger_context"]])
        if str(stmt).startswith("SELECT tool_approval_preferences.tool_key"):
            # No standing approvals; the catch-all below reads as one.
            return _EngineResult()
        if str(stmt).startswith("SELECT file_attachments."):
            # A reclaimed run resumes: the lost attempt produced no file.
            return _EngineResult()
        if self._queue:
            return _EngineResult(scalar=self._queue.pop(0))
        return _EngineResult(rows=[(uuid.UUID(INTEGRATION_ID), "Filigran Copilot - Prod")])

    async def rollback(self):
        return None

    async def flush(self):
        return None

    def add(self, obj):
        self.added.append(obj)

    def in_transaction(self):
        return False

    def begin_nested(self):
        return self

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_exc):
        return False


async def _drive(
    monkeypatch,
    *,
    context,
    attempt,
    status="running",
    on_loop=None,
    report_stale=False,
    deadline_at=None,
):
    from tests import test_assignment_stale_reference as engine_tests

    stub_agentic_gate(monkeypatch)
    run, assignment, agent = engine_tests._engine_objects()
    run.status = status
    run.trigger_context = context
    sessions: list[_EngineRowSession] = []

    def _session(r, a, g):
        sessions.append(_EngineRowSession(r, a, g))
        return sessions[-1]

    monkeypatch.setattr(engine_tests, "_EngineSession", _session)
    give_back = AsyncMock()
    monkeypatch.setattr("app.services.quota.refund_agentic_usage", give_back)
    credit_once = AsyncMock(return_value=quota_module.CHARGE_CREDIT_LANDED)
    monkeypatch.setattr("app.services.quota.credit_agentic_charge_once", credit_once)
    _run, _session_used, mocks = await engine_tests._drive_run(
        monkeypatch,
        report_integration_id=engine_tests.INTEGRATION_ID if report_stale else None,
        engine_objects=(run, assignment, agent),
        on_loop=(lambda: on_loop(sessions[0])) if on_loop else None,
        attempt=attempt,
        deadline_at=deadline_at,
    )
    # The charge ``_drive_run`` stands in for.
    mocks["increment"] = quota_module.increment_agentic_usage
    mocks["give_back"] = give_back
    mocks["credit_once"] = credit_once
    return run, sessions[0], mocks


async def test_a_rerun_credits_the_lost_attempt_free_then_charges_its_own(monkeypatch):
    run, db, mocks = await _drive(monkeypatch, context=_recorded("lost", 1), attempt=2)

    assert run.status == "completed", run.error
    mocks["refund_free"].assert_not_awaited()
    mocks["credit_once"].assert_awaited_once()
    credit = mocks["credit_once"].await_args
    assert credit.args == (attempt_charges.charge_receipt_token(run.id, "lost"), USER, run.agent_id)
    assert credit.kwargs["cause"] == "engine_abandoned"
    mocks["increment"].assert_awaited_once()
    mocks["refund_pool"].assert_not_awaited()
    recorded = recorded_charge(db.row["trigger_context"])
    assert recorded is not None and recorded.charge_id not in (None, "lost")
    assert recorded.attempt == 2
    assert db.row["trigger_context"]["type"] == "schedule"


async def test_a_rerun_with_nothing_left_of_its_deadline_is_charged_nothing(monkeypatch):
    """Both attempts were ended by infrastructure: the lost attempt's charge
    goes back, and the re-run times out before it is charged."""
    from datetime import UTC, datetime, timedelta

    from app.core.config import settings

    deadline = datetime.now(UTC) + timedelta(seconds=settings.orchestration_grace_seconds - 30)
    run, db, mocks = await _drive(
        monkeypatch, context=_recorded("lost", 1), attempt=2, deadline_at=deadline
    )

    assert run.status == "failed"
    assert run.failure_cause == "engine_deadline_exceeded"
    mocks["credit_once"].assert_awaited_once()
    assert mocks["credit_once"].await_args.kwargs["cause"] == "engine_abandoned"
    mocks["increment"].assert_not_awaited()
    assert recorded_charge(db.row["trigger_context"]) is None


async def test_a_stale_attempts_failure_does_not_credit_the_charge_its_rerun_took(monkeypatch):
    """This attempt's lease was lost mid-loop and the re-run took its charge
    (credited it, recorded its own). This attempt then fails: it credits
    nothing, and its commit leaves the re-run's record as it is."""

    def _rerun_takes_over(db):
        db.row["trigger_context"] = _recorded("rerun", 2)

    run, db, mocks = await _drive(
        monkeypatch,
        context={"type": "schedule"},
        attempt=1,
        status="pending",
        on_loop=_rerun_takes_over,
        report_stale=True,
    )

    assert run.status == "failed"
    mocks["increment"].assert_awaited_once()
    mocks["refund_pool"].assert_not_awaited()
    mocks["refund_free"].assert_not_awaited()
    assert recorded_charge(db.row["trigger_context"]).charge_id == "rerun"


async def test_a_failed_run_that_still_owns_its_charge_credits_it_once(monkeypatch):
    run, db, mocks = await _drive(
        monkeypatch, context={"type": "schedule"}, attempt=1, status="pending", report_stale=True
    )

    assert run.status == "failed"
    mocks["refund_pool"].assert_awaited_once()
    # Taken off the row before the credit: the dead-letter finds nothing.
    assert recorded_charge(db.row["trigger_context"]) is None


async def test_a_run_replaced_before_it_charged_gives_its_charge_back(monkeypatch):
    """The attempt that replaced this one recorded its charge after this one
    loaded the run: this one's charge goes back and it records nothing."""
    from tests import test_assignment_stale_reference as engine_tests

    stub_agentic_gate(monkeypatch)
    run, assignment, agent = engine_tests._engine_objects()
    session = _EngineRowSession(run, assignment, agent)
    session.row["trigger_context"] = _recorded("rerun", 2)
    monkeypatch.setattr(engine_tests, "_EngineSession", lambda *_a: session)
    give_back = AsyncMock()
    monkeypatch.setattr("app.services.quota.refund_agentic_usage", give_back)

    _run, _session_used, mocks = await engine_tests._drive_run(
        monkeypatch,
        report_integration_id=engine_tests.INTEGRATION_ID,
        engine_objects=(run, assignment, agent),
        attempt=1,
    )

    quota_module.increment_agentic_usage.assert_awaited_once()
    give_back.assert_awaited_once()
    # Nothing of its own to credit when it then fails.
    mocks["refund_pool"].assert_not_awaited()
    assert give_back.await_args.args[0] == run.user_id
    assert recorded_charge(session.row["trigger_context"]).charge_id == "rerun"


async def test_the_adapter_runs_the_assignment_under_its_attempt(monkeypatch):
    from app.services import assignment_engine
    from app.services.orchestration_adapters import assignment as assignment_adapter

    execute = AsyncMock(return_value=None)
    monkeypatch.setattr(assignment_engine, "execute_assignment_run", execute)
    monkeypatch.setattr(assignment_adapter, "_bind_memory_chain", AsyncMock(return_value=None))
    run = SimpleNamespace(status="completed", error=None)
    session = SimpleNamespace(
        execute=AsyncMock(return_value=SimpleNamespace(scalar_one_or_none=lambda: run)),
        commit=AsyncMock(),
    )
    execution = SimpleNamespace(payload={"run_id": str(uuid.uuid4())}, attempt_count=2)

    result = await assignment_adapter.AssignmentAdapter().execute(SimpleNamespace(db=session), execution)

    assert result.kind == "completed"
    assert execute.await_args.kwargs["attempt"] == 2


# -- The dead-letter ----------------------------------------------------------


def _run_session(run) -> SimpleNamespace:
    """A session whose every read answers with *run*: the run row locked by
    the dead letter, and its context read back under the row lock."""
    return SimpleNamespace(
        execute=AsyncMock(
            side_effect=lambda *_a, **_k: SimpleNamespace(
                scalar_one_or_none=lambda: run,
                scalars=lambda: SimpleNamespace(all=lambda: [run.trigger_context]),
            )
        ),
        commit=AsyncMock(),
    )


async def test_the_dead_letter_marks_the_record_before_it_credits_and_removes_it_after(monkeypatch):
    from app.services.orchestration_adapters.assignment import AssignmentAdapter

    run = _run("running", _recorded("lost"))
    session = _run_session(run)
    committed_before_credit = MagicMock()

    async def _credit(*_args, **_kwargs):
        committed_before_credit(session.commit.await_count, attempt_charges.is_settling(run.trigger_context))
        return quota_module.CHARGE_CREDIT_LANDED

    monkeypatch.setattr("app.services.quota.credit_agentic_charge_once", _credit)
    execution = SimpleNamespace(
        payload={"run_id": str(run.id)},
        last_error="Lease expired, last heartbeat 74 s ago, with no attempt left.",
        state="dead_letter",
        attempt_count=3,
        max_attempts=3,
        deadline_at=None,
    )

    await AssignmentAdapter().on_dead_letter(SimpleNamespace(db=session), execution)

    assert run.status == "failed"
    assert run.trigger_context == {"type": "schedule"}
    committed_before_credit.assert_called_once_with(1, True)
    # A retried pass finds a terminal run with nothing recorded.
    await AssignmentAdapter().on_dead_letter(SimpleNamespace(db=session), execution)
    committed_before_credit.assert_called_once()


@pytest.mark.parametrize(
    ("status", "context", "credited"),
    [
        # A stale attempt failed the run while its re-run was charged.
        ("failed", _recorded("rerun", 2), True),
        # A record written before charges were fenced may be credited already.
        ("failed", _recorded(None), False),
        ("failed", {"type": "schedule"}, False),
        # The work ran: it keeps its charge.
        ("completed", _recorded("rerun", 2), False),
    ],
)
async def test_the_dead_letter_settles_a_charge_a_terminal_run_still_records(
    monkeypatch, status, context, credited
):
    from app.services.orchestration_adapters.assignment import AssignmentAdapter

    run = _run(status, dict(context))
    run.error = "stale attempt failure"
    session = _run_session(run)
    credit = AsyncMock(return_value=quota_module.CHARGE_CREDIT_LANDED)
    monkeypatch.setattr("app.services.quota.credit_agentic_charge_once", credit)
    execution = SimpleNamespace(
        payload={"run_id": str(run.id)}, last_error="worker lost", state="dead_letter",
        attempt_count=2, max_attempts=3, deadline_at=None,
    )

    await AssignmentAdapter().on_dead_letter(SimpleNamespace(db=session), execution)
    await AssignmentAdapter().on_dead_letter(SimpleNamespace(db=session), execution)

    assert run.status == status and run.error == "stale attempt failure", "the ending stands"
    if credited:
        credit.assert_awaited_once()
        assert credit.await_args.args[1] == USER
        assert recorded_charge(run.trigger_context) is None
    else:
        credit.assert_not_awaited()
        assert run.trigger_context == context
