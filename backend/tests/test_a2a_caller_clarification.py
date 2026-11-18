"""An exposed agent can ask its caller for a detail, and the task continues.

A2A blocks a task on its CLIENT — the calling agent — so ``input-required``
here asks a machine that answers in its own turn. Nothing parks for a person:
the run that asked is over, the question lives on the task, and the answer
starts a new run on the same task.
"""

from __future__ import annotations

import uuid

import pytest

from app.a2a import protocol as proto
from app.a2a import tasks as task_svc
from tests.a2a_server_stubs import (
    DedupRedis,
    FakeDb,
    Result,
    agent,
    drive_executor,
    exposure,
    own_dedup_claims,
    partner_client,
    run,
    task,
)

pytestmark = pytest.mark.asyncio


@pytest.fixture(autouse=True)
def dedup_redis(monkeypatch) -> DedupRedis:
    """An answer claims its new messageId the way a submission does. Each test
    owns the store, or ``m-2`` from an earlier run fails the next one."""
    return own_dedup_claims(monkeypatch)


class TestTheStateAQuestionPutsTheTaskIn:
    async def test_a_finished_run_with_a_question_is_input_required(self):
        _run = run(status="completed")
        _task = task(_run.id, caller_question="Which tenant?")
        assert (
            await task_svc.resolve_state(FakeDb(_run), _task)
            is proto.A2AState.INPUT_REQUIRED
        )

    async def test_without_one_it_is_whatever_the_run_says(self):
        _run = run(status="completed")
        assert (
            await task_svc.resolve_state(FakeDb(_run), task(_run.id))
            is proto.A2AState.COMPLETED
        )

    async def test_a_cancelled_task_is_not_reopened_by_a_question(self):
        """Cancel is terminal. A question outstanding when it lands must not
        put the task back in play."""
        from datetime import UTC, datetime

        _run = run(status="running")
        _task = task(
            _run.id,
            caller_question="Which tenant?",
            cancel_requested_at=datetime.now(UTC),
        )
        assert (
            await task_svc.resolve_state(FakeDb(_run), _task)
            is proto.A2AState.CANCELED
        )


class TestAskingIsOfferedOnlyToAnExternalTask:
    async def test_the_executor_hands_the_run_somewhere_to_put_a_question(
        self, monkeypatch
    ):
        captured = drive_executor(monkeypatch)
        await task_svc.execute_task_run(uuid.uuid4())
        assert captured["chat_kwargs"]["ask_caller_out"] == {}

    async def test_the_run_is_told_an_agent_asked_and_gets_the_answer(
        self, monkeypatch
    ):
        """A consult by the partner: the run acts for its run-as user rather
        than addressing them, so a question it needs answered goes to the
        tool, not into a reply the partner reads as the answer."""
        captured = drive_executor(monkeypatch)
        await task_svc.execute_task_run(uuid.uuid4())
        handed = captured["chat_kwargs"]["handover_context"]
        assert handed == task_svc._PARTNER_HANDOVER_CONTEXT
        # Its own copy: a run never writes into the module's constant.
        assert handed is not task_svc._PARTNER_HANDOVER_CONTEXT

    async def test_it_is_withheld_once_the_rounds_are_spent(self, monkeypatch):
        """At the bound the agent answers with what it has — which is what a
        caller asked a fourth time would insist on anyway."""
        captured = drive_executor(monkeypatch)
        captured["_task"].clarification_count = task_svc._MAX_CLARIFICATION_ROUNDS
        await task_svc.execute_task_run(uuid.uuid4())
        assert captured["chat_kwargs"]["ask_caller_out"] is None

    def test_no_other_caller_offers_it(self):
        """The tool exists for this surface. A chat turn has a person in it,
        who is not the caller A2A means."""
        import inspect

        from app.services import agent_chat

        src = inspect.getsource(agent_chat.run_agent_chat)
        assert "if ask_caller_out is not None:" in src


class TestWhatARunThatAskedRecords:
    async def test_the_question_lands_on_the_task_not_the_answer(
        self, monkeypatch
    ):
        captured = drive_executor(monkeypatch, returns=("ignore me", None))

        async def _chat(**kwargs):
            kwargs["ask_caller_out"]["question"] = "Which tenant?"
            return "ignore me", None

        monkeypatch.setattr("app.services.agent_chat.run_agent_chat", _chat)
        await task_svc.execute_task_run(uuid.uuid4())

        assert captured["_task"].caller_question == "Which tenant?"
        # No summary: an answer would render as an artifact, and the caller
        # asked nothing yet.
        assert captured["summary"] is None
        assert captured["status"] == "completed"

    async def test_the_round_is_counted(self, monkeypatch):
        captured = drive_executor(monkeypatch)

        async def _chat(**kwargs):
            kwargs["ask_caller_out"]["question"] = "Which tenant?"
            return "", None

        monkeypatch.setattr("app.services.agent_chat.run_agent_chat", _chat)
        await task_svc.execute_task_run(uuid.uuid4())
        assert captured["_task"].clarification_count == 1


class TestAnsweringContinuesTheSameTask:
    async def test_it_reuses_the_task_and_starts_a_new_run(self):
        _agent, _exposure = agent(), exposure()
        old_run = run(_agent.id, status="completed")
        _task = task(old_run.id, caller_question="Which tenant?")
        db = FakeDb(old_run)

        new = await task_svc.answer_question(
            db,
            task=_task,
            exposure=_exposure,
            agent=_agent,
            client_id="partner-1",
            provider=None,
            message_id="m-2",
            parts=[{"text": "the eu one"}],
        )

        assert _task.run_id == new.id != old_run.id
        # Cleared, or the task would keep reporting a question nobody will
        # answer again.
        assert _task.caller_question is None

    async def test_the_exchange_travels_with_it(self):
        """An external run starts with no history, so the question and what
        prompted it have to ride on the run that answers."""
        _agent, _exposure = agent(), exposure()
        old_run = run(_agent.id, status="completed")
        old_run.trigger_payload = {"message": "enrich CVE-2024-3400"}
        _task = task(old_run.id, caller_question="Which tenant?")

        new = await task_svc.answer_question(
            FakeDb(old_run),
            task=_task,
            exposure=_exposure,
            agent=_agent,
            client_id="partner-1",
            provider=None,
            message_id="m-2",
            parts=[{"text": "the eu one"}],
        )

        assert new.trigger_payload["a2a_question"] == "Which tenant?"
        assert new.trigger_payload["a2a_prior_message"] == "enrich CVE-2024-3400"
        assert new.trigger_payload["message"] == "the eu one"

    async def test_a_task_that_asked_nothing_refuses(self):
        """Not found would be wrong: the client owns this task, and what it
        got wrong is a fact about its own work."""
        _agent, _exposure = agent(), exposure()
        _task = task(run(_agent.id).id)

        with pytest.raises(task_svc.TaskNotAskingError):
            await task_svc.answer_question(
                FakeDb(),
                task=_task,
                exposure=_exposure,
                agent=_agent,
                client_id="partner-1",
                provider=None,
                message_id="m-2",
                parts=[{"text": "unasked for"}],
            )

    def test_the_executor_replays_it_as_history(self):
        import inspect

        src = inspect.getsource(task_svc.execute_task_run)
        assert 'payload.get("a2a_question")' in src
        assert "history=prior_history" in src


class TestTheRouteAnswersTheRightTask:
    def test_a_message_naming_a_task_continues_it(self):
        import inspect

        from app.api.v1 import a2a_server

        src = inspect.getsource(a2a_server.a2a_jsonrpc)
        named = src.index('message.get("taskId")')
        created = src.index("task_svc.create_task(")
        # Before the create path, or an answer would start a second task.
        assert named < created
        assert "task_svc.answer_question(" in src[named:created]

    def test_another_client_s_task_is_not_found(self):
        """The same uniform answer every other task lookup gives, so a
        partner cannot discover which ids exist by answering them."""
        import inspect

        from app.api.v1 import a2a_server

        src = inspect.getsource(a2a_server.a2a_jsonrpc)
        named = src.index('message.get("taskId")')
        window = src[named : named + 1200]
        assert "get_task(" in window
        assert "TASK_NOT_FOUND" in window


class TestAContinuationStaysOnItsOwnCard:
    """One registration spans every card it reaches, so ``get_task`` finds a
    task created on another card. A continuation EXECUTES, and the run it
    writes takes the addressed card's agent while the task keeps the original
    card's run-as user — so answering on the wrong card runs one owner's agent
    as another owner's identity.
    """

    def _send(self, monkeypatch, *, asking, addressed_exposure):
        import uuid as _uuid

        from fastapi import FastAPI

        from app.a2a import exposure as exposure_mod
        from app.api.v1 import a2a_server
        from app.core.database import get_db
        from tests.a2a_chat_gate import stub_chat_gate

        stub_chat_gate(monkeypatch)

        addressed_exposure.agent_id = _uuid.uuid4()

        async def _get_exposure(_db, _token):
            return addressed_exposure

        monkeypatch.setattr(exposure_mod, "get_exposure_by_token", _get_exposure)

        async def _noop(*_a, **_k):
            return None

        monkeypatch.setattr(a2a_server, "_enforce_preauth_rate_limit", _noop)
        monkeypatch.setattr(a2a_server, "_enforce_a2a_rate_limit", _noop)

        async def _auth(_request, _db, _exposure):
            return "partner-1", "", "oauth_a2a"

        monkeypatch.setattr(a2a_server, "_authenticate", _auth)

        async def _get_task(*_a, **_k):
            return asking

        monkeypatch.setattr(task_svc, "get_task", _get_task)

        answered: list = []

        async def _answer(*_a, **kw):
            answered.append(kw)
            return run()

        monkeypatch.setattr(task_svc, "answer_question", _answer)
        monkeypatch.setattr(a2a_server, "_spawn_task_run", lambda *_a: None)

        app = FastAPI()
        app.include_router(a2a_server.router)
        app.dependency_overrides[get_db] = lambda: FakeDb(agent())
        response = partner_client(app).post(
            "/a2a/tok/jsonrpc",
            json={
                "jsonrpc": "2.0",
                "id": "r1",
                "method": "message/send",
                "params": {
                    "message": {
                        "messageId": str(_uuid.uuid4()),
                        "taskId": str(asking.id),
                        "parts": [{"text": "acme"}],
                    }
                },
            },
            headers={"Authorization": "Bearer t"},
        )
        return response, answered

    def test_a_task_from_another_card_is_refused(self, monkeypatch):
        _exposure = exposure()
        asking = task(run().id, caller_question="Which tenant?")
        assert asking.exposure_id != _exposure.id
        response, answered = self._send(
            monkeypatch, asking=asking, addressed_exposure=_exposure
        )
        body = response.json()
        assert answered == [], "the crossing must be refused before it executes"
        assert body["error"]["code"] == proto.UNSUPPORTED_OPERATION
        assert "another agent card" in body["error"]["message"]

    def test_not_as_not_found(self, monkeypatch):
        """The partner owns this task and reads it through ``tasks/get`` on
        either card, so a uniform not-found hides nothing and only leaves it
        unable to act."""
        response, _ = self._send(
            monkeypatch,
            asking=task(run().id, caller_question="Which tenant?"),
            addressed_exposure=exposure(),
        )
        assert response.json()["error"]["code"] != proto.TASK_NOT_FOUND

    def test_its_own_card_still_continues_it(self, monkeypatch):
        _exposure = exposure()
        asking = task(
            run().id, caller_question="Which tenant?", exposure_id=_exposure.id
        )
        response, answered = self._send(
            monkeypatch, asking=asking, addressed_exposure=_exposure
        )
        assert "error" not in response.json()
        assert len(answered) == 1


class TestTheContinuationIsReapableAgain:
    async def test_answering_clears_the_reaped_flag(self):
        """The run that asked marked the task reaped when it finalised. Left
        set, the continuation is outside both the lease reaper and the total
        lifetime bound, and a replica dying mid-answer leaves the partner
        polling a task nothing will ever drive terminal."""
        _run = run(status="completed")
        _task = task(_run.id, caller_question="Which tenant?", reaped=True)
        await task_svc.answer_question(
            FakeDb(_run),
            task=_task,
            exposure=exposure(),
            agent=agent(),
            client_id="partner-1",
            provider="oauth_a2a",
            message_id="m2",
            parts=[{"text": "acme"}],
        )
        assert _task.reaped is False


class TestTheTwoRunsOfOneQuestionAreLinked:
    async def test_the_answer_links_the_run_that_asked_and_the_one_it_starts(self):
        """``run_id`` moves to the new run, so the run pages would otherwise
        have no way from one half of the exchange to the other."""
        from app.a2a.run_exchange import ANSWERED_BY_RUN_KEY, PREVIOUS_RUN_KEY

        _run = run(status="completed")
        _task = task(_run.id, caller_question="Which tenant?")
        new_run = await task_svc.answer_question(
            FakeDb(_run),
            task=_task,
            exposure=exposure(),
            agent=agent(),
            client_id="partner-1",
            provider="oauth_a2a",
            message_id="m2",
            parts=[{"text": "acme"}],
        )
        assert new_run.trigger_payload[PREVIOUS_RUN_KEY] == str(_run.id)
        assert new_run.trigger_payload["a2a_question"] == "Which tenant?"
        assert _run.trigger_payload[ANSWERED_BY_RUN_KEY] == str(new_run.id)
        # What the run was given stays as it was.
        assert _run.trigger_payload["message"] == "do the thing"


class TestTheSweepLeavesAnAskingTaskAlone:
    def test_a_task_waiting_on_its_caller_is_not_a_candidate(self):
        """``input-required`` is not terminal, so nothing claims it and the
        predicate would match on every pass — each one spending one of the
        callback's five lifetime attempts, until the terminal notification
        this sweep exists to deliver can no longer be sent."""
        import inspect

        src = inspect.getsource(task_svc.announce_missed)
        assert "A2ATask.caller_question.is_(None)" in src


class TestATaskWaitingOnItsCallerIsStillBounded:
    """`input-required` is neither terminal nor live, and three things read
    only those two: the cancel guard, the blocking wait and the reaper. A task
    parked on a question was invisible to all of them.
    """

    async def test_a_cancel_reaches_it(self):
        _run = run(status="completed")
        _task = task(_run.id, caller_question="Which tenant?")
        assert await task_svc.request_cancel(FakeDb(_run), _task) is True
        assert _task.cancel_requested_at is not None

    async def test_and_the_state_it_reports_is_cancelled(self):
        """The run that asked is already terminal, so the cancel cannot be
        written to it — the question is what the cancel withdraws."""
        _run = run(status="completed")
        _task = task(_run.id, caller_question="Which tenant?")
        await task_svc.request_cancel(FakeDb(_run), _task)
        # Read back on a session of its own, as a poll would.
        assert (
            await task_svc.resolve_state(FakeDb(_run), _task)
            is proto.A2AState.CANCELED
        )

    async def test_without_a_cancel_it_is_still_input_required(self):
        _run = run(status="completed")
        assert (
            await task_svc.resolve_state(
                FakeDb(_run), task(_run.id, caller_question="Which tenant?")
            )
            is proto.A2AState.INPUT_REQUIRED
        )

    async def test_the_lifetime_bound_withdraws_an_abandoned_question(self):
        """A partner that asks and walks away leaves a task no run executes, no
        lease covers and no cancel reached."""
        from datetime import UTC, datetime, timedelta

        from app.core.config import settings

        _run = run(status="completed")
        _task = task(
            _run.id,
            caller_question="Which tenant?",
            reaped=True,
            created_at=datetime.now(UTC)
            - timedelta(seconds=settings.a2a_task_max_lifetime_seconds + 60),
        )
        reaped: list = []
        assert (
            await task_svc.reap_stale_tasks(
                FakeDb(Result([_task]), Result([_run])), reaped_out=reaped
            )
            == 1
        )
        assert _task.cancel_requested_at is not None
        assert reaped == [_task]

    def test_the_blocking_wait_ends_on_it(self):
        """A2A's blocking contract is "until an interrupted or terminal state",
        and v1.0 blocks by default with no polling loop — so waiting out the
        120s cap only delays the question."""
        import inspect

        src = inspect.getsource(task_svc.wait_until_terminal)
        assert "proto.A2AState.INPUT_REQUIRED" in src

    def test_answering_honours_blocking_too(self):
        import inspect

        from app.api.v1 import a2a_server

        src = inspect.getsource(a2a_server.a2a_jsonrpc)
        window = src[src.index("_spawn_task_run(asking.id)") :][:600]
        assert "wait_until_terminal(db, asking)" in window


class TestAnAnswerCannotReuseAnotherTasksMessageId:
    async def test_a_claimed_id_is_refused_rather_than_a_500(self, monkeypatch):
        """`(client_id, message_id)` is unique across live tasks, so reusing one
        would fail the flush on the constraint — a 500 with the session
        poisoned, where every other partner-supplied value gets a protocol
        error."""
        async def _taken(*_a, **_k):
            return False

        monkeypatch.setattr(task_svc, "_claim_message_id", _taken)
        _run = run(status="completed")
        with pytest.raises(task_svc.TaskRejectedError):
            await task_svc.answer_question(
                FakeDb(_run),
                task=task(_run.id, caller_question="Which tenant?"),
                exposure=exposure(),
                agent=agent(),
                client_id="partner-1",
                provider="oauth_a2a",
                message_id="already-used",
                parts=[{"text": "acme"}],
            )

    async def test_the_id_an_answer_takes_is_claimed(self, dedup_redis):
        """So a second task answered with it is refused."""
        _agent, _exposure = agent(), exposure()
        first_run, second_run = run(status="completed"), run(status="completed")
        await task_svc.answer_question(
            FakeDb(first_run),
            task=task(first_run.id, caller_question="Which tenant?"),
            exposure=_exposure,
            agent=_agent,
            client_id="partner-1",
            provider="oauth_a2a",
            message_id="m-2",
            parts=[{"text": "acme"}],
        )
        assert dedup_redis.keys == {task_svc._dedup_key("partner-1", "m-2"): "1"}

        with pytest.raises(task_svc.TaskRejectedError) as refused:
            await task_svc.answer_question(
                FakeDb(second_run),
                task=task(second_run.id, caller_question="Which tenant?"),
                exposure=_exposure,
                agent=_agent,
                client_id="partner-1",
                provider="oauth_a2a",
                message_id="m-2",
                parts=[{"text": "acme"}],
            )
        assert refused.value.reason == "duplicate_message_id"

    async def test_nor_can_it_take_the_id_of_a_submission(self):
        """A submission and an answer claim in one store. Apart, a partner
        could answer with the id of a task it submitted, and the flush would
        fail on the constraint."""
        _agent, _exposure = agent(), exposure()
        _, created = await task_svc.create_task(
            FakeDb(),
            exposure=_exposure,
            agent=_agent,
            client_id="partner-1",
            message_id="m-1",
            parts=[{"text": "enrich CVE-2024-3400"}],
        )
        assert created is True

        _run = run(status="completed")
        with pytest.raises(task_svc.TaskRejectedError) as refused:
            await task_svc.answer_question(
                FakeDb(_run),
                task=task(_run.id, caller_question="Which tenant?"),
                exposure=_exposure,
                agent=_agent,
                client_id="partner-1",
                provider="oauth_a2a",
                message_id="m-1",
                parts=[{"text": "acme"}],
            )
        assert refused.value.reason == "duplicate_message_id"

    async def test_re_answering_with_the_tasks_own_id_is_not_refused(self):
        """The id it already holds is not another task's — a retry of the same
        answer must not be turned into a rejection."""
        _run = run(status="completed")
        _task = task(_run.id, caller_question="Which tenant?", message_id="m-1")
        await task_svc.answer_question(
            FakeDb(_run),
            task=_task,
            exposure=exposure(),
            agent=agent(),
            client_id="partner-1",
            provider="oauth_a2a",
            message_id="m-1",
            parts=[{"text": "acme"}],
        )
        assert _task.caller_question is None


class TestAnAnswerIsIdempotentLikeASubmission:
    """The dedup contract for a submission is that resending it returns the
    task it created rather than making a second. An answer is a submission, and
    the continuation path had no equivalent: a partner that retried after a
    lost response got `UnsupportedOperationError` — "this task is not waiting
    on an answer" — which is true, and true precisely because its own earlier
    answer is what stopped it waiting.
    """

    def _send(self, monkeypatch, *, asking, exposure_, message_id, answer=None):
        import uuid as _uuid

        from fastapi import FastAPI

        from app.a2a import exposure as exposure_mod
        from app.api.v1 import a2a_server
        from app.core.database import get_db
        from tests.a2a_chat_gate import stub_chat_gate

        stub_chat_gate(monkeypatch)
        exposure_.agent_id = _uuid.uuid4()

        async def _get_exposure(_db, _token):
            return exposure_

        monkeypatch.setattr(exposure_mod, "get_exposure_by_token", _get_exposure)

        async def _noop(*_a, **_k):
            return None

        monkeypatch.setattr(a2a_server, "_enforce_preauth_rate_limit", _noop)
        monkeypatch.setattr(a2a_server, "_enforce_a2a_rate_limit", _noop)

        async def _auth(_request, _db, _exposure):
            return "partner-1", "", "oauth_a2a"

        monkeypatch.setattr(a2a_server, "_authenticate", _auth)

        async def _get_task(*_a, **_k):
            return asking

        monkeypatch.setattr(task_svc, "get_task", _get_task)

        answered: list = []

        async def _answer(*_a, **kw):
            answered.append(kw)
            return run()

        monkeypatch.setattr(task_svc, "answer_question", answer or _answer)
        spawned: list = []
        monkeypatch.setattr(
            a2a_server, "_spawn_task_run", lambda i: spawned.append(i)
        )

        app = FastAPI()
        app.include_router(a2a_server.router)
        app.dependency_overrides[get_db] = lambda: FakeDb(agent())
        response = partner_client(app).post(
            "/a2a/tok/jsonrpc",
            json={
                "jsonrpc": "2.0",
                "id": "r1",
                "method": "message/send",
                "params": {
                    "message": {
                        "messageId": message_id,
                        "taskId": str(asking.id),
                        "parts": [{"text": "acme"}],
                    }
                },
            },
            headers={"Authorization": "Bearer t"},
        )
        return response, answered, spawned

    def test_resending_the_answer_returns_the_task(self, monkeypatch):
        """No second run, and no refusal: the task it already produced."""
        _exposure = exposure()
        asking = task(
            run().id,
            caller_question=None,
            message_id="m-answer",
            exposure_id=_exposure.id,
        )
        response, answered, spawned = self._send(
            monkeypatch, asking=asking, exposure_=_exposure, message_id="m-answer"
        )
        body = response.json()
        assert "error" not in body, body
        assert answered == [], "the answer already landed"
        assert spawned == [], "and a second run would make one exchange look like two"

    def test_a_different_message_on_a_task_that_is_not_asking_is_still_refused(
        self, monkeypatch
    ):
        """The refusal has to survive: a task that never asked, or has already
        been answered by something else, is not continuable."""
        _exposure = exposure()
        asking = task(
            run().id,
            caller_question=None,
            message_id="m-answer",
            exposure_id=_exposure.id,
        )

        async def _not_asking(*_a, **_k):
            raise task_svc.TaskNotAskingError("This task is not waiting on an answer.")

        response, _answered, _spawned = self._send(
            monkeypatch,
            asking=asking,
            exposure_=_exposure,
            message_id="m-other",
            answer=_not_asking,
        )
        assert response.json()["error"]["code"] == proto.UNSUPPORTED_OPERATION
