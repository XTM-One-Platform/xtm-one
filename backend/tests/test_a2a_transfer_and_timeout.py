"""What an external task reports when it transfers, or runs out of clock.

A partner reads a task's state and its artifact; both must say what happened.

* a **transfer** carries the peer's work, so the artifact names the agent that
  produced it — the partner validated its request against a different card;
* a **timeout** is a failure, not an answer: the loop reports it as reply text
  a polling partner cannot tell from a result;
* ``mandatory_transfer`` is not offered — an automatic hand-off with no tool
  call behind it would route every submission to one peer.
"""

import asyncio
import contextlib
import uuid

import pytest

from app.a2a import tasks as task_svc
from tests import a2a_server_stubs as stubs

pytestmark = pytest.mark.asyncio


class _Result:
    def __init__(self, value):
        self._value = value
        self.rowcount = 1

    def scalar_one_or_none(self):
        return self._value


class _DB:
    def __init__(self, *answers):
        self._answers = list(answers)

    async def execute(self, _stmt):
        return _Result(self._answers.pop(0) if self._answers else None)

    async def flush(self):
        pass

    async def commit(self):
        pass


class _Ctx:
    def __init__(self, db):
        self._db = db

    async def __aenter__(self):
        return self._db

    async def __aexit__(self, *_exc):
        return False


@pytest.fixture
def run_task(monkeypatch):
    """Drive ``execute_task_run`` with a scripted chat outcome.

    Call it with the ``(text, transferred_to)`` the runtime should return and,
    optionally, the ``error_out`` it should report. Returns what
    ``_finalise_run`` was called with — the terminal decision under test.
    """

    def _drive(*, returns=("answer", None), error_out=None, raises=None):
        agent_id, run_id = uuid.uuid4(), uuid.uuid4()
        task = type("T", (), {"id": uuid.uuid4(), "run_id": run_id,
                              "exposure_id": uuid.uuid4(),
                              "context_id": uuid.uuid4(),
                              "caller_question": None,
                              "clarification_count": 0,
                              "lease_heartbeat_at": None})()
        run = type("R", (), {"id": run_id, "status": "pending",
                             "agent_id": agent_id,
                             "trigger_context": {"client_id": "partner-1"},
                             "trigger_payload": {"message": "go"}})()
        exposure = type("E", (), {"id": task.exposure_id, "revoked_at": None,
                                  "run_as_user_id": uuid.uuid4()})()
        agent = type("A", (), {"id": agent_id, "name": "Exposed",
                               "builtin_tool_ids": [], "handover_agent_ids": [],
                               "handover_types": {}})()
        finalised: dict = {}

        async def _fake_chat(**kwargs):
            if error_out is not None:
                kwargs["error_out"].update(error_out)
            if raises is not None:
                if callable(raises):
                    raises(kwargs)
                raise asyncio.CancelledError()
            return returns

        async def _fake_finalise(_db, _task, _run_id, status, summary, error, **kw):
            finalised.update(
                status=status, summary=summary, error=error, **kw
            )

        async def _noop(*_a, **_k):
            return None

        monkeypatch.setattr("app.services.agent_chat.run_agent_chat", _fake_chat)
        monkeypatch.setattr(task_svc, "_supervise_run", _noop)
        monkeypatch.setattr(task_svc, "_finalise_run", _fake_finalise)
        monkeypatch.setattr(
            "app.core.database.async_session",
            lambda: _Ctx(_DB(task, run, exposure, agent)),
        )
        return finalised

    return _drive


@pytest.fixture(autouse=True)
def _no_delegated_work(monkeypatch):
    stubs.silence_delegated_work(monkeypatch, task_svc)


class TestATimeoutIsAFailureNotAnAnswer:
    async def test_loop_timeout_fails_the_task(self, run_task):
        """The loop returns "this took too long" as ordinary reply text rather
        than raising, so ``error_out`` is what the terminal decision reads."""
        finalised = run_task(
            returns=("I'm sorry, this request took too long...", None),
            error_out={"error_code": "loop_timeout", "error": "timed out after 900s"},
        )
        await task_svc.execute_task_run(uuid.uuid4())

        assert finalised["status"] == "failed"
        assert finalised["error"] == task_svc.TIMEOUT_ERROR_TEXT
        # No summary means no artifact: the apology must not reach the partner
        # as a result.
        assert finalised["summary"] is None

    async def test_a_pool_timeout_fails_busy_not_with_the_queuepool_text(self, run_task):
        """#2561: a run that could not check a connection out of the pool ends
        with a partner-visible busy reason, never the driver's QueuePool text."""
        from sqlalchemy import exc as sa_exc

        def _pool_timeout(_kwargs):
            raise sa_exc.TimeoutError("QueuePool limit of size 15 overflow 10 reached, connection timed out")

        finalised = run_task(raises=_pool_timeout)
        await task_svc.execute_task_run(uuid.uuid4())

        assert finalised["status"] == "failed"
        assert finalised["error"] == task_svc.BUSY_ERROR_TEXT
        assert "QueuePool" not in finalised["error"]
        assert task_svc.BUSY_ERROR_TEXT.startswith(task_svc._PARTNER_VISIBLE_ERROR_PREFIXES)

    async def test_the_reason_is_partner_visible(self):
        """A FAILED task's reason is whitelisted before it crosses the trust
        boundary; an unlisted one becomes a generic line the partner cannot
        tell from any other failure."""
        assert task_svc.TIMEOUT_ERROR_TEXT.startswith(
            task_svc._PARTNER_VISIBLE_ERROR_PREFIXES
        )

    def test_the_chat_loop_reports_the_timeout_it_swallows(self):
        """The producing half of the contract: the loop catches
        ``TimeoutError`` and substitutes reply text, so ``error_out`` is the
        only signal a machine caller has."""
        import inspect

        from app.services.agent_chat import run_agent_chat

        src = inspect.getsource(run_agent_chat)
        handler = src[src.index("except TimeoutError:") :][:1200]
        assert 'error_out.setdefault("error_code", "loop_timeout")' in handler
        assert "error_out is not None" in handler

    async def test_a_normal_run_still_completes(self, run_task):
        finalised = run_task(returns=("the real answer", None))
        await task_svc.execute_task_run(uuid.uuid4())
        assert finalised["status"] == "completed"
        assert finalised["summary"] == "the real answer"


class TestAPlatformStopFailsTheTaskWithAReason:
    """A run cut by a deployment fails with a reason its partner reads."""

    @pytest.fixture
    def stopping(self, monkeypatch):
        from app.core import worker_shutdown

        monkeypatch.setattr(worker_shutdown, "_request", None)
        worker_shutdown.note_shutdown(worker_shutdown.STOP, "SIGTERM")
        yield
        monkeypatch.setattr(worker_shutdown, "_request", None)

    async def test_a_run_cut_by_a_stop_is_failed_not_cancelled(self, run_task, stopping):
        finalised = run_task(raises=True)
        with pytest.raises(asyncio.CancelledError):
            await task_svc.execute_task_run(uuid.uuid4())
        assert finalised["status"] == "failed"
        assert finalised["error"].startswith(task_svc.INTERRUPTED_ERROR_TEXT)
        assert "send the task again" in finalised["error"]

    async def test_the_reason_crosses_to_the_partner(self):
        assert task_svc.INTERRUPTED_ERROR_TEXT.startswith(task_svc._PARTNER_VISIBLE_ERROR_PREFIXES)

    async def test_a_cancel_the_partner_asked_for_stays_cancelled(self, run_task, stopping):
        finalised = run_task(raises=lambda kwargs: kwargs["cancel_event"].set())
        with pytest.raises(asyncio.CancelledError):
            await task_svc.execute_task_run(uuid.uuid4())
        assert finalised["status"] == "cancelled" and finalised["error"] is None

    async def test_a_cut_with_no_platform_cause_stays_cancelled(self, run_task, monkeypatch):
        from app.core import worker_shutdown

        monkeypatch.setattr(worker_shutdown, "_request", None)
        finalised = run_task(raises=True)
        with pytest.raises(asyncio.CancelledError):
            await task_svc.execute_task_run(uuid.uuid4())
        assert finalised["status"] == "cancelled"


class TestATransferredAnswerIsAttributed:
    async def test_the_responding_agent_is_recorded(self, run_task):
        peer = uuid.uuid4()
        finalised = run_task(returns=("peer's answer", str(peer)))
        await task_svc.execute_task_run(uuid.uuid4())
        assert finalised["responding_agent_id"] == peer

    async def test_no_transfer_records_nothing(self, run_task):
        """NULL means "the addressed agent answered", not "unknown"."""
        finalised = run_task(returns=("answer", None))
        await task_svc.execute_task_run(uuid.uuid4())
        assert finalised["responding_agent_id"] is None

    async def test_a_non_uuid_is_dropped_rather_than_stored(self, run_task):
        finalised = run_task(returns=("answer", "not-a-uuid"))
        await task_svc.execute_task_run(uuid.uuid4())
        assert finalised["responding_agent_id"] is None


class TestTheArtifactSaysWhoAnswered:
    def _task(self, run_id, responder=None):
        return type("T", (), {"id": uuid.uuid4(), "context_id": uuid.uuid4(),
                              "run_id": run_id, "rejected_reason": None,
                              "cancel_requested_at": None, "client_id": "c1",
                              "responding_agent_id": responder})()

    def _run(self, run_id, result="answer"):
        return type("R", (), {"id": run_id, "status": "completed",
                              "result_summary": result, "error": None})()

    def _agent(self, agent_id):
        return type("A", (), {"id": agent_id, "name": "Addressed",
                              "output_format": "text"})()

    async def test_a_transferred_artifact_names_the_peer(self):
        run_id, agent_id, peer = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
        db = _DB(self._run(run_id), "Peer Specialist")
        arts = await task_svc.build_artifacts(
            db, self._task(run_id, peer), self._agent(agent_id), "1.0"
        )
        assert arts[0]["metadata"] == {
            "respondingAgent": {"id": str(peer), "name": "Peer Specialist"}
        }

    async def test_an_untransferred_artifact_carries_no_metadata(self):
        run_id, agent_id = uuid.uuid4(), uuid.uuid4()
        arts = await task_svc.build_artifacts(
            _DB(self._run(run_id)), self._task(run_id), self._agent(agent_id), "1.0"
        )
        assert "metadata" not in arts[0]

    async def test_a_deleted_peer_still_attributes_by_id(self):
        """The name lookup misses; the id is the attribution and must survive."""
        run_id, agent_id, peer = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
        arts = await task_svc.build_artifacts(
            _DB(self._run(run_id), None),
            self._task(run_id, peer),
            self._agent(agent_id),
            "1.0",
        )
        assert arts[0]["metadata"] == {"respondingAgent": {"id": str(peer)}}

    async def test_a_self_transfer_is_not_reported_as_one(self):
        run_id, agent_id = uuid.uuid4(), uuid.uuid4()
        arts = await task_svc.build_artifacts(
            _DB(self._run(run_id)),
            self._task(run_id, agent_id),
            self._agent(agent_id),
            "1.0",
        )
        assert "metadata" not in arts[0]


class TestAPipelineEntryIsTaskable:
    """``mandatory_transfer`` is offered externally like any other type.

    It is how the shipped detection-rule chain is wired
    (``agents/builtin_agents.py``): the addressed agent is a pipeline stage and
    the terminal stage's output is the deliverable. Attribution is what makes
    that legible to a partner, so the two belong together.
    """

    @pytest.mark.parametrize(
        "handover_type",
        ["consult", "transfer", "delegate", "mandatory_transfer"],
    )
    def test_no_type_is_filtered_out(self, handover_type):
        from app.services.agent_chat import filter_handover_agents_by_type

        peer = type("A", (), {"id": uuid.uuid4()})()
        assert filter_handover_agents_by_type(
            [peer], {str(peer.id): handover_type}, None
        ) == [peer]

    async def test_the_terminal_stage_answers_and_is_named(self, run_task):
        """A pipeline's reply is the last stage's, under the addressed agent's
        task — so the artifact has to say which agent produced it."""
        terminal = uuid.uuid4()
        finalised = run_task(returns=("the generated rule", str(terminal)))
        await task_svc.execute_task_run(uuid.uuid4())
        assert finalised["summary"] == "the generated rule"
        assert finalised["responding_agent_id"] == terminal


class TestAStageThatHandsOnStillRecordsItsOwnPrompt:
    """Every pipeline stage but the last returns through the transfer branch,
    so that branch owes the caller the same trace the loop's own exit gives.
    """

    def test_a_stage_records_its_prompt_when_it_is_created(self):
        """A pipeline stage runs for minutes; its prompt should not wait for it
        to finish."""
        import inspect

        from app.services import agent_chat

        src = inspect.getsource(agent_chat.run_agent_chat)
        # The child's own construction: the transfer marker, then the prompt it
        # is created with, before the row is added.
        child = src.index('"delegation_type": "transfer"')
        added = src.index("db.add(_t_child)", child)
        assert 'messages=[{"role": "user", "content": user_message}]' in src[child:added]

    def test_both_exits_populate_the_trace(self):
        import inspect

        from app.services import agent_chat

        src = inspect.getsource(agent_chat.run_agent_chat)
        transfer_return = src.index("return transferred_text, final_agent_id")
        assert "messages_out.extend(messages)" in src[transfer_return - 600 : transfer_return]
        assert src.count("messages_out.extend(messages)") == 2


class TestNothingAfterTheLoopRunsOnACancelledTurn:
    """Nothing after the loop may do work once the turn is cancelled.

    ``cancel_event`` stops the agent LOOP, which returns a ``ChatResponse``
    carrying ``stop_reason="cancelled"`` and "The request was cancelled." as
    its content. Everything after the loop then ran anyway: the post-loop
    region neither consults that stop reason — ``agent_chat`` only ever packs
    it into telemetry payloads — nor the event, which is in scope the whole
    time as a parameter of ``run_agent_chat``.

    Three of those steps do real work, and one of them is worse than it
    sounds. The mandatory transfer copies ``response.content`` into
    ``source_response``, so the next agent receives the CANCELLATION NOTICE as
    its input and runs a full turn on it — and with two targets it makes an
    extra model call first, through ``evaluate_routing_conditions``, to pick
    between them.

    What is deliberately NOT guarded: tracking tool usage (it really was
    consumed), the deliverable rescue and the structured-output repair. All
    three only reshape text that is already in hand — ``emit_repaired_json``
    is synchronous and parses rather than asking a model — and guarding the
    rescue would hide the cancellation message itself.

    The predicate is tested for behaviour here; that each site consults it
    before doing its work is an ordering claim about one very large function,
    and is asserted as one rather than dressed up as an end-to-end test.
    """

    def _db(self, status):
        from types import SimpleNamespace

        class _Db:
            # ``no_autoflush`` because the gate reads under it — these run on
            # every turn, and an autoflush would push half-built turn state.
            no_autoflush = contextlib.nullcontext()

            async def execute(self, _stmt):
                return SimpleNamespace(scalar_one_or_none=lambda: status)

        return _Db()

    async def test_a_set_event_is_a_cancellation(self):
        import asyncio

        from app.services.agent_chat import _turn_was_cancelled

        event = asyncio.Event()
        event.set()
        assert await _turn_was_cancelled(self._db("running"), None, event) is True

    async def test_so_is_a_run_the_database_says_is_cancelled(self):
        """The event is POLLED, and the poll sleeps before its first tick, so
        it can still be unset after a cancel has been written and committed.
        Reading only the event is reading a signal nobody has armed yet."""
        import asyncio

        from app.services.agent_chat import _turn_was_cancelled

        assert (
            await _turn_was_cancelled(
                self._db("cancelled"), "11111111-1111-1111-1111-111111111111",
                asyncio.Event(),
            )
            is True
        )

    async def test_neither_is_not(self):
        import asyncio

        from app.services.agent_chat import _turn_was_cancelled

        assert (
            await _turn_was_cancelled(
                self._db("running"), "11111111-1111-1111-1111-111111111111",
                asyncio.Event(),
            )
            is False
        )

    async def test_no_run_to_read_falls_back_to_the_event(self):
        """Evaluation mode and anonymous chat have no run row at all."""
        from app.services.agent_chat import _turn_was_cancelled

        assert await _turn_was_cancelled(self._db(None), None, None) is False

    def test_every_step_that_does_work_checks_first(self):
        """Each of the three consults it INSIDE its own block: read once after
        the loop, a cancel arriving during the finalize stage would still let
        the transfer through."""
        import inspect

        from app.services import agent_chat

        src = inspect.getsource(agent_chat.run_agent_chat)
        # Full headers, because "── Mandatory transfer" alone also matches a
        # section far above the loop — the one that EXCLUDES those targets
        # from what the LLM sees — and a guard asserted against the wrong
        # block is worth nothing.
        for section, work in (
            ("── Pipeline: FINALIZE stage", "_load_pipeline_tool("),
            (
                "── Mandatory transfer: evaluate conditions post-loop",
                "transfer_result[\"mandatory\"]",
            ),
            (
                "── Handle transfer (if the agent decided to transfer)",
                "target_agent_id = transfer_result",
            ),
        ):
            start = src.index(section)
            block = src[start : start + 2500]
            guard = block.find("await _turn_was_cancelled(")
            assert guard != -1, f"{section} is not guarded"
            assert guard < block.index(work), (
                f"{section} does its work before checking"
            )

    def test_the_cancelled_loop_reply_is_what_would_have_been_forwarded(self):
        """Not a guess about the content: the loop's own constant is the thing
        a mandatory transfer copies into ``source_response``."""
        from app.tools.agent_loop import _CANCELLED_MESSAGE

        assert _CANCELLED_MESSAGE == "The request was cancelled."


class TestTheChildOfATransferIsCancelledWithItsParent:
    """A transfer runs the next agent INSIDE this turn — ``run_agent_chat`` calls
    itself and awaits it — so the child's loop is where the rest of the work
    happens. The loop learns of a cancel from ``cancel_event`` and from nothing
    else; a child called without one has nothing to check, so the cancel
    reaches the parent's own loop, the parent then blocks on the child, and the
    child runs to completion.

    ``_turn_was_cancelled`` does not cover this. It guards the boundaries
    BEFORE a transfer starts; once the child is running, the event is the only
    thing that stops it.
    """

    def _transfer_call(self):
        """The nested ``run_agent_chat`` inside the transfer block, alone.

        Sliced rather than searched whole-file: ``run_agent_chat`` calls itself
        for a consult too, and an argument asserted against the wrong call is
        worth nothing.
        """
        import inspect

        from app.services import agent_chat

        src = inspect.getsource(agent_chat.run_agent_chat)
        start = src.index("── Handle transfer (if the agent decided to transfer)")
        # Through ``_run_helper_turn``, which passes its arguments through.
        call = src.index("await _run_helper_turn(", start)
        return src[call : src.index("\n                    )", call)]

    def test_the_child_is_given_the_cancel_event(self):
        assert "cancel_event=cancel_event" in self._transfer_call()

    def test_the_child_run_is_the_one_being_traced(self):
        """The guard reads the run named here, so the two have to agree: a
        child given the parent's run id would report the parent's status."""
        assert "parent_run_id=_transfer_run_id" in self._transfer_call()

    def test_the_loop_stops_on_the_event_it_is_given(self):
        """What makes passing it worth anything: the loop checks it at the top
        of every iteration."""
        import inspect

        from app.tools import agent_loop

        src = inspect.getsource(agent_loop.run_agent_loop)
        assert "if cancel_event and cancel_event.is_set():" in src
