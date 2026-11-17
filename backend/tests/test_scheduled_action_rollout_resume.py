"""A scheduled action cut by a rollout goes on by itself.

The rule of a background task and an assignment run: a graceful stop is
resumed up to 3 times in a row with no progress (200 in all), a loss
without a word 3 times, the cause recorded; past
a cap, or for an exact call that may already have reached its target, the
owner agent gets a fresh run with the previous outcome in its prompt. The
person is never asked to check or reschedule: no "reschedule" wording on a
restart, no Reschedule control on an item a restart cut.
"""

from __future__ import annotations

import inspect
import uuid
from types import SimpleNamespace

import pytest

from app.services import background_checkpoint
from app.services import scheduled_work_item_runner as runner
from app.services import scheduled_work_items as swi


def _graceful_resumes(n: int, completed_calls: int = 5) -> list[dict]:
    """*n* graceful resumes with no progress between them (the run stayed at *completed_calls*)."""
    return [
        {
            "attempt": 1,
            "cause": "its worker was asked to stop (SIGTERM)",
            background_checkpoint.GRACEFUL_KEY: True,
            background_checkpoint.COMPLETED_CALLS_KEY: completed_calls,
        }
        for _ in range(n)
    ]


def _lost_resumes(n: int) -> list[dict]:
    return [
        {"attempt": i + 2, "cause": "its worker lost its lease", background_checkpoint.GRACEFUL_KEY: False}
        for i in range(n)
    ]


def _handed_back(context: dict, attempt: int = 1) -> dict:
    return background_checkpoint.handed_back(context, attempt=attempt, signal="SIGTERM")


class TestTheDecision:
    def test_an_instruction_handed_back_is_resumed(self):
        context = _handed_back({"action_started_at": "x", "attempt": 1})
        assert runner.resume_decision(context, attempt=1, mode="instruction", read_only=False) == (runner.RESUME, None)

    def test_an_instruction_lost_without_a_word_is_resumed(self):
        context = {"action_started_at": "x", "attempt": 1}
        assert runner.resume_decision(context, attempt=2, mode="instruction", read_only=False)[0] == runner.RESUME

    def test_three_graceful_resumes_with_no_progress_then_its_agent_gets_it(self):
        under = _handed_back({background_checkpoint.RESUMES_KEY: _graceful_resumes(2)})
        assert runner.resume_decision(under, attempt=1, mode="instruction", read_only=False)[0] == runner.RESUME
        at_cap = _handed_back({background_checkpoint.RESUMES_KEY: _graceful_resumes(3)})
        decision, why = runner.resume_decision(at_cap, attempt=1, mode="instruction", read_only=False)
        assert decision == runner.REQUEUE and why and "graceful" in why

    def test_graceful_resumes_that_progressed_are_resumed_up_to_the_bound_in_all(self):
        progressed = [
            {**resume, background_checkpoint.COMPLETED_CALLS_KEY: i * 10}
            for i, resume in enumerate(_graceful_resumes(25), start=1)
        ]
        context = _handed_back({background_checkpoint.RESUMES_KEY: progressed})
        assert runner.resume_decision(context, attempt=1, mode="instruction", read_only=False)[0] == runner.RESUME

    def test_three_wordless_resumes_then_its_agent_gets_it(self):
        under = {background_checkpoint.RESUMES_KEY: _lost_resumes(2)}
        assert runner.resume_decision(under, attempt=4, mode="instruction", read_only=False)[0] == runner.RESUME
        at_cap = {background_checkpoint.RESUMES_KEY: _lost_resumes(3)}
        assert runner.resume_decision(at_cap, attempt=5, mode="instruction", read_only=False)[0] == runner.REQUEUE

    def test_a_read_only_exact_call_is_run_again(self):
        context = {"action_started_at": "x", "attempt": 1}
        assert runner.resume_decision(context, attempt=2, mode="exact", read_only=True)[0] == runner.RERUN

    def test_an_exact_call_that_may_change_something_goes_to_its_agent(self):
        context = {"action_started_at": "x", "attempt": 1}
        decision, why = runner.resume_decision(context, attempt=2, mode="exact", read_only=False)
        assert decision == runner.REQUEUE and why == runner.NOT_IDEMPOTENT_REASON

    def test_a_fresh_run_of_its_agent_resumes_as_an_instruction(self):
        context = {"action_started_at": "x", runner.OWNER_PROMPT_KEY: "read first"}
        assert runner.resume_decision(context, attempt=2, mode="exact", read_only=False)[0] == runner.RESUME

    @pytest.mark.parametrize("mode", ["exact", "instruction"])
    @pytest.mark.parametrize("read_only", [True, False])
    @pytest.mark.parametrize(
        "context",
        [
            {"action_started_at": "x"},
            _handed_back({"action_started_at": "x"}),
            {background_checkpoint.RESUMES_KEY: _lost_resumes(3)},
            _handed_back({background_checkpoint.RESUMES_KEY: _graceful_resumes(20)}),
        ],
    )
    def test_a_restart_is_never_handed_to_the_person(self, mode, read_only, context):
        decision, _ = runner.resume_decision(context, attempt=2, mode=mode, read_only=read_only)
        assert decision in (runner.RESUME, runner.RERUN, runner.REQUEUE)


class TestTheOwnerPrompt:
    def test_an_exact_call_reads_the_state_first(self):
        prompt = runner.owner_requeue_prompt(
            title="Post the EMT reminder",
            action={"tool": "send_teams_channel_message", "arguments": {"text": "hi"}},
            mode="exact",
            previous_outcome="cut by a rollout",
            lost=0,
            graceful=2,
            requeue=1,
        )
        assert "send_teams_channel_message" in prompt and "First read the target's state" in prompt
        assert "cut by a rollout" in prompt and "2 graceful stop(s)" in prompt
        assert f"fresh run 1 of at most {runner.MAX_OWNER_REQUEUES}" in prompt

    def test_an_instruction_does_only_what_remains(self):
        prompt = runner.owner_requeue_prompt(
            title="Weekly digest",
            action={"prompt": "Write the digest"},
            mode="instruction",
            previous_outcome=None,
            lost=3,
            graceful=0,
            requeue=2,
        )
        assert "Write the digest" in prompt and "do only what remains" in prompt


class TestNoRescheduleWording:
    """No restart path asks the person to check or reschedule."""

    def test_the_runner_never_says_reschedule_on_a_restart(self):
        source = inspect.getsource(runner)
        assert "Check, then reschedule" not in source
        assert "reschedule it if needed" not in source

    def test_the_exhausted_reason_asks_nothing(self):
        context = {
            runner.REQUEUE_KEY: {"count": runner.MAX_OWNER_REQUEUES},
            background_checkpoint.RESUMES_KEY: _graceful_resumes(20),
        }
        text = runner.exhausted_reason(context, cause="a rollout").lower()
        assert "reschedule" not in text and "check" not in text
        assert "20 graceful resume(s)" in text

    def test_the_outcome_says_the_restarts_without_asking(self):
        item = SimpleNamespace(
            action_mode="instruction",
            status="done",
            outcome_code=None,
            title="Weekly digest",
            due_at=None,
            result="Sent.",
            payload={swi.RESTARTS_KEY: {"resumed": 2, "requeued": 1}},
        )
        text = swi.outcome_text(item, None)
        assert "resumed 2 time(s)" in text and "run again by its agent 1 time(s)" in text
        assert "reschedule" not in text.lower()

    def test_the_view_counts_the_restarts(self):
        assert swi.restarts_of({swi.RESTARTS_KEY: {"resumed": 3, "requeued": True}}) == {"resumed": 3, "requeued": 0}
        assert swi.restarts_of(None) == {"resumed": 0, "requeued": 0}


class TestTheReceipts:
    def test_a_resumed_turn_writes_its_own_receipt(self):
        run_id = uuid.uuid4()
        first = runner.turn_receipt_token(run_id, 1, {})
        resumed = runner.turn_receipt_token(run_id, 1, {background_checkpoint.RESUMES_KEY: _graceful_resumes(1)})
        assert first == runner.receipt_token(run_id, 1) and resumed != first

    def test_the_cut_turn_receipt_is_the_one_it_wrote(self):
        run_id = uuid.uuid4()
        context = {"attempt": 1, background_checkpoint.RESUMES_KEY: _graceful_resumes(2)}
        assert runner.cut_turn_receipt(run_id, context) == runner.turn_receipt_token(run_id, 1, context)
        assert runner.cut_turn_receipt(run_id, {}) is None


class TestTheDeadLetter:
    def test_a_run_lost_to_restarts_is_not_the_persons(self):
        assert runner._cut_by_restarts(SimpleNamespace(status="running", trigger_context={}))
        assert runner._cut_by_restarts(SimpleNamespace(status="pending", trigger_context=_handed_back({})))
        assert not runner._cut_by_restarts(SimpleNamespace(status="pending", trigger_context={}))

    def test_a_fresh_owner_run_lost_before_it_started_is_still_recovery(self):
        pending = SimpleNamespace(status="pending", trigger_context={runner.OWNER_PROMPT_KEY: "read first"})
        assert runner._cut_by_restarts(pending)
        counted = SimpleNamespace(status="pending", trigger_context={runner.REQUEUE_KEY: {"count": 1}})
        assert runner._cut_by_restarts(counted)


class TestTheReadOnlyClassification:
    @pytest.mark.asyncio
    async def test_an_admin_whitelisted_tool_is_never_run_again(self, monkeypatch):
        # The approval whitelist is an exemption an administrator can extend;
        # it is never proof that a tool only reads.
        from app.services import hitl_whitelist

        async def _no_platform_reads(_db):
            return frozenset()

        async def _admin_whitelist(_db, **_kwargs):
            return frozenset({"send_teams_channel_message"})

        monkeypatch.setattr(hitl_whitelist, "platform_read_default", _no_platform_reads)
        monkeypatch.setattr(hitl_whitelist, "resolve_whitelist", _admin_whitelist)
        assert await runner._is_read_only_tool(None, "send_teams_channel_message") is False

    @pytest.mark.asyncio
    async def test_a_shipped_read_is_run_again(self):
        from app.services.hitl_whitelist_default import SHIPPED_DEFAULT_WHITELIST

        assert await runner._is_read_only_tool(None, next(iter(sorted(SHIPPED_DEFAULT_WHITELIST))))

    @pytest.mark.asyncio
    async def test_an_unreadable_classification_is_may_change_something(self, monkeypatch):
        from app.services import hitl_whitelist

        async def _down(_db):
            raise RuntimeError("registrations unreadable")

        monkeypatch.setattr(hitl_whitelist, "platform_read_default", _down)
        assert await runner._is_read_only_tool(None, "some_platform_tool") is False


class TestTheCheckpoint:
    @pytest.mark.asyncio
    @pytest.mark.parametrize("kind", ["blocked", "retry", "done"])
    async def test_it_is_discarded_on_every_exit_but_a_hand_back(self, monkeypatch, kind):
        from app.services import assignment_run_resume

        dropped: list[str] = []

        async def _body(*_args, **_kwargs):
            return runner._Executed(kind, "x")

        async def _drop(run_id, _checkpoint, _place):
            dropped.append(run_id)

        monkeypatch.setattr(runner, "_execute_instruction_body", _body)
        monkeypatch.setattr(runner, "_drop_checkpoint", _drop)
        monkeypatch.setattr(assignment_run_resume, "checkpoint_for", lambda *_a, **_k: object())
        await runner._execute_instruction(
            "a", "u", {"prompt": "p"}, "run-1", attempt=1, cancel_event=None, deadline_at=None
        )
        assert dropped == ["run-1"]

    @pytest.mark.asyncio
    async def test_it_is_discarded_when_the_turn_raises(self, monkeypatch):
        from app.services import assignment_run_resume

        dropped: list[str] = []

        async def _body(*_args, **_kwargs):
            raise TimeoutError

        async def _drop(run_id, _checkpoint, _place):
            dropped.append(run_id)

        monkeypatch.setattr(runner, "_execute_instruction_body", _body)
        monkeypatch.setattr(runner, "_drop_checkpoint", _drop)
        monkeypatch.setattr(assignment_run_resume, "checkpoint_for", lambda *_a, **_k: object())
        with pytest.raises(TimeoutError):
            await runner._execute_instruction(
                "a", "u", {"prompt": "p"}, "run-1", attempt=1, cancel_event=None, deadline_at=None
            )
        assert dropped == ["run-1"]

    @pytest.mark.asyncio
    async def test_a_hand_back_keeps_it(self, monkeypatch):
        from app.services import assignment_run_resume

        dropped: list[str] = []

        async def _body(*_args, **_kwargs):
            return runner._Executed(runner.HANDED_BACK, "handed back")

        async def _drop(run_id, _checkpoint, _place):
            dropped.append(run_id)

        monkeypatch.setattr(runner, "_execute_instruction_body", _body)
        monkeypatch.setattr(runner, "_drop_checkpoint", _drop)
        monkeypatch.setattr(assignment_run_resume, "checkpoint_for", lambda *_a, **_k: object())
        await runner._execute_instruction(
            "a", "u", {"prompt": "p"}, "run-1", attempt=1, cancel_event=None, deadline_at=None
        )
        assert dropped == []


class TestTheRescheduleRefusal:
    """The REST route and the scheduling tool share the service: neither moves a restart-cut action."""

    def _item(self, **kwargs):
        base = {"title": "Post the EMT reminder", "outcome_code": None, "payload": {}}
        return SimpleNamespace(**{**base, **kwargs})

    @pytest.mark.parametrize(
        "item_kwargs",
        [
            {"payload": {swi.RESTARTS_KEY: {"resumed": 1, "requeued": 0}}},
            {"payload": {swi.RESTARTS_KEY: {"resumed": 0, "requeued": 2}}},
            {"outcome_code": swi.OUTCOME_RESUME_EXHAUSTED},
        ],
    )
    def test_a_restart_cut_item_is_refused(self, item_kwargs):
        with pytest.raises(swi.ScheduledWorkItemError) as refused:
            swi._refuse_restart_cut_reschedule(self._item(**item_kwargs))
        assert refused.value.status_code == 409

    @pytest.mark.parametrize("outcome_code", [None, "interrupted", "binding", "missed"])
    def test_an_item_no_restart_cut_is_movable(self, outcome_code):
        swi._refuse_restart_cut_reschedule(self._item(outcome_code=outcome_code))


@pytest.mark.asyncio
async def test_the_deadline_budgets_every_wordless_resume(monkeypatch):
    from datetime import UTC, datetime

    from app.services import assignment_run_resume

    monkeypatch.setattr(runner.settings, "scheduled_work_item_max_attempts", 1, raising=False)
    now = datetime.now(UTC)
    due = runner._DueItem(uuid.uuid4(), None, uuid.uuid4(), "exact", now, "t", {})
    deadline = await runner._execution_deadline(None, due, now)
    attempts = assignment_run_resume.MAX_ATTEMPTS
    assert (deadline - now).total_seconds() >= attempts * runner._EXACT_ATTEMPT_SECONDS


class TestTheAdapter:
    @pytest.mark.asyncio
    async def test_a_hand_back_is_deferred_to_the_head_without_an_attempt(self, monkeypatch):
        from app.services.orchestration_adapters import scheduled_work_item as adapter_module
        from app.services.orchestration_adapters import sub_agent

        async def _execute(run_id, **_kwargs):
            return runner.RunOutcome(runner.HANDED_BACK, "handed back")

        async def _end(_ctx, execution):
            return execution

        async def _commit():
            return None

        monkeypatch.setattr(runner, "execute_scheduled_run", _execute)
        monkeypatch.setattr(sub_agent, "_end_hand_back", _end)
        ctx = SimpleNamespace(db=SimpleNamespace(commit=_commit), cancel_event=None)
        execution = SimpleNamespace(payload={"run_id": str(uuid.uuid4())}, attempt_count=1, deadline_at=None)
        result = await adapter_module.ScheduledWorkItemAdapter().execute(ctx, execution)
        assert result.kind == "defer" and result.place == "head" and result.backoff_seconds == 0.0

    @pytest.mark.asyncio
    async def test_a_requeue_ends_the_execution(self, monkeypatch):
        from app.services.orchestration_adapters import scheduled_work_item as adapter_module
        from app.services.orchestration_adapters import sub_agent

        async def _execute(run_id, **_kwargs):
            return runner.RunOutcome(runner.REQUEUED, "run again")

        async def _end(_ctx, execution):
            return execution

        async def _commit():
            return None

        monkeypatch.setattr(runner, "execute_scheduled_run", _execute)
        monkeypatch.setattr(sub_agent, "_end_hand_back", _end)
        ctx = SimpleNamespace(db=SimpleNamespace(commit=_commit), cancel_event=None)
        execution = SimpleNamespace(payload={"run_id": str(uuid.uuid4())}, attempt_count=1, deadline_at=None)
        result = await adapter_module.ScheduledWorkItemAdapter().execute(ctx, execution)
        assert result.kind == "completed"


class TestATakeBackOnItsWorkersWord:
    """A run whose worker said it was stopping and was killed before its hand-back."""

    def test_the_adapter_is_taken_back_when_its_worker_is_gone(self):
        from app.services import orchestration_reconciler
        from app.services.orchestration_adapters.scheduled_work_item import ScheduledWorkItemAdapter

        adapter = ScheduledWorkItemAdapter()
        assert ScheduledWorkItemAdapter.resume_when_worker_gone is True
        assert adapter.name in orchestration_reconciler.resumable_adapters([adapter])
        assert adapter.raised_attempt_bound(4) >= 4

    @pytest.mark.asyncio
    async def test_the_adapter_passes_the_take_back_on_its_word(self, monkeypatch):
        from app.services import orchestration_engine
        from app.services.orchestration_adapters import scheduled_work_item as adapter_module
        from app.services.orchestration_adapters import sub_agent

        seen: dict = {}

        async def _execute(run_id, **kwargs):
            seen.update(kwargs)
            return runner.RunOutcome("ended")

        async def _end(_ctx, execution):
            return execution

        async def _commit():
            return None

        monkeypatch.setattr(runner, "execute_scheduled_run", _execute)
        monkeypatch.setattr(sub_agent, "_end_hand_back", _end)
        monkeypatch.setattr(sub_agent, "_taken_back_on_its_word", lambda execution: True)
        ctx = SimpleNamespace(db=SimpleNamespace(commit=_commit), cancel_event=None)
        execution = SimpleNamespace(
            payload={"run_id": str(uuid.uuid4())},
            attempt_count=2,
            deadline_at=None,
            adapter_state=orchestration_engine.OWNER_GONE_STATE,
        )
        result = await adapter_module.ScheduledWorkItemAdapter().execute(ctx, execution)
        assert result.kind == "completed"
        assert seen["taken_back_on_its_word"] is True

    def test_the_decision_reads_the_take_back_as_a_graceful_stop(self, monkeypatch):
        from app.services import assignment_run_resume

        calls: list = []

        def _refused(context, **kwargs):
            calls.append(kwargs)
            return None

        monkeypatch.setattr(assignment_run_resume, "resume_refused", _refused)
        decision, why = runner.resume_decision(
            {"action_started_at": "x"},
            attempt=2,
            mode="instruction",
            read_only=False,
            taken_back_on_its_word=True,
        )
        assert (decision, why) == (runner.RESUME, None)
        assert calls == [{"attempt": 2, "taken_back_on_its_word": True}]

    def test_a_take_back_on_its_word_is_not_refused_at_the_wordless_cap(self):
        from app.services import assignment_run_resume

        # Wordless resumes already at their in-a-row cap: a further wordless
        # loss is refused, a take-back on the worker's word is not.
        lost = [
            {"attempt": n, "cause": "lost", "graceful": False, "completed_calls": 0}
            for n in range(1, assignment_run_resume.MAX_RESUMES + 1)
        ]
        context = {"resumes": lost}
        attempt = assignment_run_resume.MAX_RESUMES + 1
        assert assignment_run_resume.resume_refused(context, attempt=attempt) is not None
        assert (
            assignment_run_resume.resume_refused(context, attempt=attempt, taken_back_on_its_word=True)
            is None
        )


def test_the_execution_has_room_for_every_wordless_resume():
    from app.services import assignment_run_resume

    assert runner.execution_max_attempts() >= assignment_run_resume.MAX_ATTEMPTS
