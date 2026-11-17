"""A consulted turn is bounded by its events; a cancelled consult fails its child run."""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path

import pytest

from app.services import consult_watchdog as cw
from app.services.consult_watchdog import (
    CONSULT_CANCELLED_ERROR,
    CONSULT_RETRY_WARN_AFTER,
    CONSULT_ROLLOUT_ERROR,
    ConsultCutError,
    ConsultRetryLedger,
    ConsultStalledError,
    ConsultWatchdog,
    consult_cancel_error,
    report_consult_retry,
    rollout_cut_reason,
    run_watched,
)


class _Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


class TestConsultWatchdog:
    def test_alive_before_the_start_bound(self) -> None:
        clock = _Clock()
        dog = ConsultWatchdog(start_seconds=90, idle_seconds=600, clock=clock)
        clock.now += 89
        assert dog.stalled() is None

    def test_no_first_event_is_never_started(self) -> None:
        clock = _Clock()
        dog = ConsultWatchdog(start_seconds=90, idle_seconds=600, clock=clock)
        clock.now += 91
        reason = dog.stalled()
        assert reason is not None
        assert "never started" in reason

    def test_events_keep_it_alive_past_the_start_bound(self) -> None:
        clock = _Clock()
        dog = ConsultWatchdog(start_seconds=90, idle_seconds=600, clock=clock)
        clock.now += 10
        dog.observe({"status": "preparing"})
        clock.now += 500
        assert dog.stalled() is None
        dog.observe({"status": "tool_heartbeat"})
        clock.now += 599
        assert dog.stalled() is None

    def test_silence_after_events_is_stopped(self) -> None:
        clock = _Clock()
        dog = ConsultWatchdog(start_seconds=90, idle_seconds=600, clock=clock)
        dog.observe()
        dog.observe()
        clock.now += 601
        reason = dog.stalled()
        assert reason is not None
        assert "stopped" in reason
        assert "2 event(s)" in reason


class TestRunWatched:
    async def test_returns_the_result_of_a_live_turn(self) -> None:
        dog = ConsultWatchdog(start_seconds=5, idle_seconds=5)

        async def turn() -> str:
            dog.observe()
            await asyncio.sleep(0.02)
            return "answer"

        assert await run_watched(turn(), dog, child_run_id="c1", poll_seconds=0.01) == "answer"

    async def test_re_raises_the_turn_error_unchanged(self) -> None:
        dog = ConsultWatchdog(start_seconds=5, idle_seconds=5)

        async def turn() -> str:
            raise ValueError("boom")

        with pytest.raises(ValueError, match="boom"):
            await run_watched(turn(), dog, child_run_id="c1", poll_seconds=0.01)

    async def test_a_turn_that_never_starts_fails_fast_naming_the_child(self) -> None:
        dog = ConsultWatchdog(start_seconds=0.05, idle_seconds=5)
        cancelled = asyncio.Event()

        async def turn() -> str:
            try:
                await asyncio.sleep(30)
            except asyncio.CancelledError:
                cancelled.set()
                raise
            return "never"

        with pytest.raises(ConsultStalledError) as info:
            await asyncio.wait_for(
                run_watched(turn(), dog, child_run_id="76b3891a", poll_seconds=0.01), timeout=5
            )
        assert info.value.child_run_id == "76b3891a"
        assert "76b3891a" in str(info.value)
        assert "never started" in str(info.value)
        assert cancelled.is_set()

    async def test_a_turn_that_goes_silent_fails_fast(self) -> None:
        dog = ConsultWatchdog(start_seconds=5, idle_seconds=0.05)

        async def turn() -> str:
            dog.observe()
            await asyncio.sleep(30)
            return "never"

        with pytest.raises(ConsultStalledError, match="stopped"):
            await asyncio.wait_for(run_watched(turn(), dog, child_run_id="c2", poll_seconds=0.01), timeout=5)

    async def test_outer_cancellation_cancels_the_turn(self) -> None:
        dog = ConsultWatchdog(start_seconds=5, idle_seconds=5)
        cancelled = asyncio.Event()

        async def turn() -> str:
            dog.observe()
            try:
                await asyncio.sleep(30)
            except asyncio.CancelledError:
                cancelled.set()
                raise
            return "never"

        with pytest.raises(TimeoutError):
            await asyncio.wait_for(run_watched(turn(), dog, child_run_id="c3", poll_seconds=0.01), timeout=0.05)
        assert cancelled.is_set()


class TestRolloutCut:
    """A consult on a stopping process is stopped and answered at once."""

    async def test_a_rollout_stops_the_turn_and_names_the_child(self) -> None:
        dog = ConsultWatchdog(start_seconds=60, idle_seconds=60)
        cancelled = asyncio.Event()
        cut: list[str | None] = [None]

        async def turn() -> str:
            dog.observe()
            try:
                await asyncio.sleep(30)
            except asyncio.CancelledError:
                cancelled.set()
                raise
            return "never"

        async def stop_soon() -> None:
            await asyncio.sleep(0.03)
            cut[0] = "its server process is stopping (stop, SIGTERM)"

        stopper = asyncio.ensure_future(stop_soon())
        with pytest.raises(ConsultCutError) as info:
            await asyncio.wait_for(
                run_watched(turn(), dog, child_run_id="r1", poll_seconds=0.01, cut_check=lambda: cut[0]),
                timeout=5,
            )
        await stopper
        assert cancelled.is_set()
        assert isinstance(info.value, ConsultStalledError)
        assert info.value.child_run_id == "r1"
        assert "cut by a rollout" in str(info.value)
        assert "r1" in str(info.value)
        assert "again now" in str(info.value)

    async def test_no_consult_starts_on_a_stopping_process(self) -> None:
        dog = ConsultWatchdog(start_seconds=60, idle_seconds=60)
        started = asyncio.Event()

        async def turn() -> str:
            started.set()
            return "answer"

        with pytest.raises(ConsultCutError):
            await run_watched(turn(), dog, child_run_id=None, cut_check=lambda: "stopping")
        assert not started.is_set()

    async def test_no_cut_returns_the_answer(self) -> None:
        dog = ConsultWatchdog(start_seconds=60, idle_seconds=60)

        async def turn() -> str:
            dog.observe()
            return "answer"

        assert await run_watched(turn(), dog, child_run_id="r2", cut_check=lambda: None) == "answer"

    def test_rollout_cut_reason_follows_the_wind_down(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from app.core import worker_shutdown

        monkeypatch.setattr(worker_shutdown, "wind_down_due", lambda now=None: False)
        assert rollout_cut_reason() is None
        monkeypatch.setattr(worker_shutdown, "wind_down_due", lambda now=None: True)
        monkeypatch.setattr(
            worker_shutdown,
            "shutdown_request",
            lambda: worker_shutdown.ShutdownRequest(reason="stop", detail="SIGTERM", requested_at=0.0),
        )
        reason = rollout_cut_reason()
        assert reason is not None
        assert "stopping" in reason
        assert "SIGTERM" in reason

    def test_cancel_error_names_a_rollout(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from app.core import worker_shutdown
        from app.core.utils import GRACEFUL_SHUTDOWN_CANCEL_HINT

        monkeypatch.setattr(worker_shutdown, "shutdown_request", lambda: None)
        assert consult_cancel_error(asyncio.CancelledError()) == CONSULT_CANCELLED_ERROR
        drain = asyncio.CancelledError(f"Task cancelled, {GRACEFUL_SHUTDOWN_CANCEL_HINT}")
        assert consult_cancel_error(drain) == CONSULT_ROLLOUT_ERROR
        monkeypatch.setattr(
            worker_shutdown,
            "shutdown_request",
            lambda: worker_shutdown.ShutdownRequest(reason="recycle", detail="1000", requested_at=0.0),
        )
        assert consult_cancel_error(asyncio.CancelledError()) == CONSULT_ROLLOUT_ERROR
        assert "cut by a rollout" in CONSULT_ROLLOUT_ERROR


class TestRetryLedger:
    def test_counts_consecutive_failures_per_peer(self) -> None:
        ledger = ConsultRetryLedger()
        assert ledger.failed("a") == 1
        assert ledger.failed("a") == 2
        assert ledger.failed("b") == 1
        ledger.succeeded("a")
        assert ledger.failures("a") == 0
        assert ledger.snapshot() == {"b": 1}

    def test_no_warning_below_the_threshold(self, caplog: pytest.LogCaptureFixture) -> None:
        ledger = ConsultRetryLedger()
        for _ in range(CONSULT_RETRY_WARN_AFTER - 1):
            ledger.failed("peer")
        with caplog.at_level(logging.WARNING, logger=cw.__name__):
            prior = report_consult_retry(ledger, target_id="peer", target_name="UX", parent_run_id="p1")
        assert prior == CONSULT_RETRY_WARN_AFTER - 1
        assert not caplog.records

    def test_warns_and_traces_past_the_threshold(
        self, caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from app.services import langfuse_tracing

        recorded: list[tuple[object, dict]] = []
        root = {"local_trace_id": "t1"}
        monkeypatch.setattr(langfuse_tracing, "get_current_trace_ctx", lambda: root)
        monkeypatch.setattr(
            langfuse_tracing, "enrich_trace_metadata", lambda ctx, meta: recorded.append((ctx, meta))
        )
        ledger = ConsultRetryLedger()
        for _ in range(CONSULT_RETRY_WARN_AFTER):
            ledger.failed("peer")
        with caplog.at_level(logging.WARNING, logger=cw.__name__):
            prior = report_consult_retry(ledger, target_id="peer", target_name="UX", parent_run_id="p1")
        assert prior == CONSULT_RETRY_WARN_AFTER
        assert any("p1" in r.getMessage() and "UX" in r.getMessage() for r in caplog.records)
        assert recorded == [(root, {"consult_retries": {"peer": CONSULT_RETRY_WARN_AFTER}})]


class TestFinalizeConsultChild:
    async def test_no_child_is_a_no_op(self) -> None:
        await cw.finalize_consult_child(None, "x")

    async def test_fails_the_child_in_its_own_session(self, monkeypatch: pytest.MonkeyPatch) -> None:
        calls: list[tuple] = []

        class _Session:
            async def __aenter__(self) -> _Session:
                return self

            async def __aexit__(self, *exc: object) -> None:
                return None

            async def commit(self) -> None:
                calls.append(("commit",))

        async def fake_update(db: object, run_id: str, status: str, *, error: str | None = None) -> None:
            calls.append(("update", run_id, status, error))

        from app.core import database
        from app.services import sub_agent

        monkeypatch.setattr(database, "async_session", lambda: _Session())
        monkeypatch.setattr(sub_agent, "update_assignment_run", fake_update)
        await cw.finalize_consult_child("child-1", cw.CONSULT_CANCELLED_ERROR)
        assert calls == [("update", "child-1", "failed", cw.CONSULT_CANCELLED_ERROR), ("commit",)]

    async def test_a_database_error_is_logged_not_raised(
        self, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        from app.core import database

        def broken() -> object:
            raise RuntimeError("db down")

        monkeypatch.setattr(database, "async_session", broken)
        with caplog.at_level(logging.WARNING, logger=cw.__name__):
            await cw.finalize_consult_child("child-2", "x")
        assert any("child-2" in r.getMessage() for r in caplog.records)


def test_chat_consult_is_watched_and_finalized_on_cancel() -> None:
    """The chat consult handler wires the watchdog, the cancel finalize and the retry report."""
    src = (Path(__file__).resolve().parents[1] / "app" / "services" / "agent_chat.py").read_text()
    start = src.index("async def _consult_agent(agent_id: str, question: str, output_schema: object = None) -> str:")
    body = src[start : src.index('ho_exec.register("consult_agent", _consult_agent)', start)]
    assert "run_watched(" in body
    assert "_watchdog.observe(inner_event)" in body
    assert "except asyncio.CancelledError as _consult_cancel:" in body
    assert "finalize_consult_child(_consult_run_id, consult_cancel_error(_consult_cancel))" in body
    # A rollout stops the consulted turn and answers the call at once.
    assert "cut_check=rollout_cut_reason" in body
    assert "report_consult_retry(" in body
    assert "_consult_retries.failed(" in body
    assert "_consult_retries.succeeded(" in body
    # The child row is committed before the nested turn starts, so the cancel
    # finalize (own session) and the stall rollback both find it.
    assert "consult_db.add(_child)" in body
    added = body.index("consult_db.add(_child)")
    committed = body.index("await consult_db.commit()", added)
    assert committed < body.index("run_watched(")
    # The child id is known before the cancellable commit, and a cancel during
    # the commit fails the child in its own session.
    assert body.index("_consult_run_id = str(_child.id)", added) < committed
    cancel_at_commit = body.index("except asyncio.CancelledError as _commit_cancel:", committed)
    assert cancel_at_commit < body.index("run_watched(")
    assert body.index("finalize_consult_child(", cancel_at_commit) < body.index("run_watched(")


def test_assignment_consult_is_watched() -> None:
    """The consult handler of an assignment run is bounded by the same watchdog."""
    src = (Path(__file__).resolve().parents[1] / "app" / "services" / "assignment_engine.py").read_text()
    start = src.index("async def _consult_agent(")
    body = src[start : src.index('_ho_exec.register("consult_agent", _consult_agent)', start)]
    assert "ConsultWatchdog()" in body
    assert "_watchdog.observe(inner_event)" in body
    assert "on_event=_watch_consult" in body
    assert "await run_watched(" in body
    assert "cut_check=rollout_cut_reason" in body
    # No child run: nothing to finalize, the call answers with the reason.
    assert "AssignmentRun(" not in body
    assert body.index("run_watched(") < body.index("run_agent_chat(", body.index("run_watched("))
    assert "await consult_db.rollback()" in body
    _assert_failure_path_reads_no_expired_target(body)


def _assert_failure_path_reads_no_expired_target(body: str) -> None:
    """After the rollback the target row is expired: only the snapshot is read."""
    rollback = body.index("await consult_db.rollback()")
    after = body[rollback:]
    assert "target.id" not in after
    assert "target.name" not in after
    assert body.index("_target_name = target.name") < rollback


class TestDurableConsultAfterCut:
    """A consult cut by a rollout becomes a durable work item on the peer."""

    @pytest.fixture(autouse=True)
    def _peer_has_an_inbox(self, monkeypatch: pytest.MonkeyPatch) -> None:
        async def has_inbox(**_kwargs: object) -> bool:
            return True

        monkeypatch.setattr(cw, "_peer_has_inbox", has_inbox)

    @staticmethod
    def _capture(monkeypatch: pytest.MonkeyPatch) -> list[dict]:
        from app.services import work_item

        created: list[dict] = []

        async def fake_create(**kwargs: object) -> dict:
            created.append(dict(kwargs))
            return {"id": f"item-{len(created)}"}

        monkeypatch.setattr(work_item, "create_work_item", fake_create)
        return created

    async def test_a_rollout_during_a_consult_reaches_the_peer_and_the_held_item(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Fake worker stop mid-consult: the answer path is a durable item, the held item waits on it."""
        created = self._capture(monkeypatch)
        stopping = {"now": False}

        async def consulted_turn() -> str:
            stopping["now"] = True  # the worker starts stopping while the peer works
            await asyncio.sleep(10)
            return "never"

        def cut() -> str | None:
            return "its server process is stopping (rollout)" if stopping["now"] else None

        question = "UX review of PR 42 on head abcdef12, captures attached"
        with pytest.raises(ConsultCutError) as caught:
            await run_watched(
                consulted_turn(), ConsultWatchdog(), child_run_id=None, poll_seconds=0.01, cut_check=cut
            )
        text = await cw.durable_consult_after_cut(
            user_id="u-1",
            caller_agent_id="dev-agent",
            caller_name="Developer",
            caller_is_company_managed=False,
            target_id="ux-agent-0001",
            target_name="UX Director",
            question=question,
            cuts=cw.CONSULT_DURABLE_AFTER_CUTS,
            source_work_item_id="held-item-1234",
            parent_run_id="run-9",
        )
        assert isinstance(caught.value, ConsultCutError)
        assert len(created) == 2
        ask, follow = created
        # The question reaches the peer as a durable item it owns.
        assert ask["owner_agent_id"] == "ux-agent-0001"
        assert question in str(ask["description"])
        assert "held-item-1234" in str(ask["description"])
        assert ask["dedupe"] is True
        assert ask["correlation_key"] == cw.durable_consult_key("ux-agent-0001", question)
        # The caller's work resumes by itself once the peer completes it.
        assert follow["owner_agent_id"] == "dev-agent"
        assert follow["blocked_on"] == ["item-1"]
        assert "held-item-1234" in str(follow["description"])
        assert text is not None and "item-1" in text and "item-2" in text
        assert "Do not ask again" in text

    async def test_without_a_held_item_only_the_ask_is_written(self, monkeypatch: pytest.MonkeyPatch) -> None:
        created = self._capture(monkeypatch)
        text = await cw.durable_consult_after_cut(
            user_id="u-1",
            caller_agent_id="dev-agent",
            caller_name="Developer",
            caller_is_company_managed=False,
            target_id="ux-agent-0001",
            target_name="UX Director",
            question="q",
            cuts=1,
        )
        assert len(created) == 1
        assert text is not None and "get_work_item(item-1)" in text

    async def test_a_write_failure_answers_none_and_never_raises(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from app.services import work_item

        async def broken(**_kwargs: object) -> dict:
            raise RuntimeError("db down")

        monkeypatch.setattr(work_item, "create_work_item", broken)
        assert (
            await cw.durable_consult_after_cut(
                user_id="u-1",
                caller_agent_id="a",
                caller_name="A",
                caller_is_company_managed=False,
                target_id="b",
                target_name="B",
                question="q",
                cuts=1,
            )
            is None
        )

    async def test_a_peer_with_no_inbox_gets_no_item_and_the_caller_a_named_outcome(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """No inbox drains the peer's queue: no orphan ask, no follow-up, an explicit answer."""
        created = self._capture(monkeypatch)
        asked: list[dict] = []

        async def no_inbox(**kwargs: object) -> bool:
            asked.append(dict(kwargs))
            return False

        monkeypatch.setattr(cw, "_peer_has_inbox", no_inbox)
        text = await cw.durable_consult_after_cut(
            user_id="u-1",
            caller_agent_id="caller-agent",
            caller_name="Caller",
            caller_is_company_managed=False,
            target_id="peer-agent-0001",
            target_name="Peer",
            question="q",
            cuts=1,
            source_work_item_id="held-item-1234",
            parent_run_id="run-9",
        )
        assert created == []
        assert asked == [
            {
                "target_id": "peer-agent-0001",
                "correlation_key": cw.durable_consult_key("peer-agent-0001", "q"),
                "is_company_managed": False,
            }
        ]
        assert text == cw.no_inbox_cut_answer("Peer", "peer-agent-0001", 1)
        assert "no work-item inbox" in text
        assert "Ask the same question again now" in text
        assert "Do not ask again" not in text

    def test_the_key_is_stable_per_peer_and_question(self) -> None:
        assert cw.durable_consult_key("peer-1", " q ") == cw.durable_consult_key("peer-1", "q")
        assert cw.durable_consult_key("peer-1", "q") != cw.durable_consult_key("peer-2", "q")
        assert cw.durable_consult_key("peer-1", "q") != cw.durable_consult_key("peer-1", "r")


class TestPeerInboxTakes:
    """Which inbox would pick the durable ask up from the peer's queue."""

    @staticmethod
    def _inbox(
        owner: str,
        *,
        prefix: str | None = None,
        priority_min: str | None = None,
        may_write: bool = True,
    ) -> object:
        from types import SimpleNamespace

        return SimpleNamespace(
            owner_agent_id=owner,
            correlation_prefix=prefix,
            priority_min=priority_min,
            writer=SimpleNamespace(may_write=may_write),
        )

    def _takes(self, inboxes: list[object], *, company_managed: bool = False) -> bool:
        return cw.peer_inbox_takes(
            inboxes,
            owner_agent_id="peer-1",
            correlation_key="consult:peer-1-abc",
            priority=cw.DURABLE_CONSULT_PRIORITY,
            is_company_managed=company_managed,
        )

    def test_no_inbox_at_all(self) -> None:
        assert self._takes([]) is False

    def test_only_other_agents_inboxes(self) -> None:
        assert self._takes([self._inbox("other-agent")]) is False

    def test_an_inbox_of_the_peer_takes_it(self) -> None:
        assert self._takes([self._inbox("other-agent"), self._inbox("PEER-1")]) is True

    def test_a_correlation_prefix_that_excludes_consults(self) -> None:
        assert self._takes([self._inbox("peer-1", prefix="dev:")]) is False
        assert self._takes([self._inbox("peer-1", prefix="consult:")]) is True

    def test_a_priority_floor_above_the_ask(self) -> None:
        assert self._takes([self._inbox("peer-1", priority_min="urgent")]) is False
        assert self._takes([self._inbox("peer-1", priority_min="high")]) is True

    def test_a_company_managed_ask_needs_a_writer_that_may_write(self) -> None:
        inbox = self._inbox("peer-1", may_write=False)
        assert self._takes([inbox], company_managed=True) is False
        assert self._takes([inbox], company_managed=False) is True


def test_both_handlers_turn_a_cut_into_a_durable_consult() -> None:
    """Chat and assignment consult handlers hand a rollout cut to the durable path."""
    services = Path(__file__).resolve().parents[1] / "app" / "services"
    chat = (services / "agent_chat.py").read_text()
    start = chat.index("async def _consult_agent(agent_id: str, question: str, output_schema: object = None) -> str:")
    body = chat[start : chat.index('ho_exec.register("consult_agent", _consult_agent)', start)]
    assert "isinstance(e, ConsultCutError)" in body
    assert "durable_consult_after_cut(" in body
    engine = (services / "assignment_engine.py").read_text()
    start = engine.index("async def _consult_agent(")
    body = engine[start : engine.index('_ho_exec.register("consult_agent", _consult_agent)', start)]
    assert "isinstance(e, ConsultCutError)" in body
    assert "durable_consult_after_cut(" in body
    # The run's own dispatched item is the one that waits for the answer.
    assert "_inbox_dispatched_work_item(run)" in body


def test_chat_consult_failure_path_reads_no_expired_target() -> None:
    src = (Path(__file__).resolve().parents[1] / "app" / "services" / "agent_chat.py").read_text()
    start = src.index("async def _consult_agent(agent_id: str, question: str, output_schema: object = None) -> str:")
    body = src[start : src.index('ho_exec.register("consult_agent", _consult_agent)', start)]
    _assert_failure_path_reads_no_expired_target(body)
