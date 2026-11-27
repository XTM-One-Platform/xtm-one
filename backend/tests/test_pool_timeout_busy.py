"""A pool checkout timeout answers busy, retry (#2561).

Three surfaces: the CTEM case reads and writes (a busy answer the agent retries,
never "could not be read"), the inbound A2A task (a partner-visible busy reason,
never the QueuePool text) and the default cap of a platform-event assignment
(bounded by the request pool).
"""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import exc as sa_exc

from app.core import db_availability
from app.core.db_availability import POOL_BUSY_ANSWER, POOL_BUSY_WRITE_ANSWER, is_pool_timeout
from app.services import assignment_dispatch as dispatch
from app.services.builtin_tools import ctem_cases

_QUEUE_POOL_TEXT = "QueuePool limit of size 15 overflow 10 reached, connection timed out, timeout 30.00"


def _pool_timeout() -> sa_exc.TimeoutError:
    return sa_exc.TimeoutError(_QUEUE_POOL_TEXT)


class TestIsPoolTimeout:
    def test_the_pool_timeout_itself(self):
        assert is_pool_timeout(_pool_timeout())

    def test_through_a_cause(self):
        try:
            try:
                raise _pool_timeout()
            except sa_exc.TimeoutError as inner:
                raise RuntimeError("service failed") from inner
        except RuntimeError as outer:
            assert is_pool_timeout(outer)

    def test_through_orig(self):
        wrapper = RuntimeError("wrapper")
        wrapper.orig = _pool_timeout()  # type: ignore[attr-defined]
        assert is_pool_timeout(wrapper)

    @pytest.mark.parametrize(
        "exc",
        [RuntimeError("boom"), TimeoutError("asyncio timeout"), sa_exc.OperationalError("SELECT 1", {}, Exception())],
    )
    def test_anything_else_is_not(self, exc):
        assert not is_pool_timeout(exc)

    def test_a_self_cycle_ends(self):
        exc = RuntimeError("loop")
        exc.__cause__ = exc
        assert not is_pool_timeout(exc)

    def test_the_answers_name_no_pool_size(self):
        for answer in (POOL_BUSY_ANSWER, POOL_BUSY_WRITE_ANSWER):
            assert answer.startswith("Busy: ")
            assert "QueuePool" not in answer and "Retry this call" in answer


class TestCaseTools:
    def test_a_read_on_a_pool_timeout_answers_busy(self):
        answer = ctem_cases.case_read_failed(_pool_timeout(), "CTEM case read failed", ctem_cases.CASE_READ_FAILED)
        assert answer == POOL_BUSY_ANSWER
        assert "could not be read" not in answer

    def test_a_read_on_another_failure_keeps_the_generic_answer(self):
        answer = ctem_cases.case_read_failed(RuntimeError("bug"), "CTEM case read failed", ctem_cases.CASE_READ_FAILED)
        assert answer == ctem_cases.CASE_READ_FAILED

    def test_a_write_on_a_pool_timeout_answers_busy(self):
        answer = ctem_cases._write_failed(_pool_timeout(), uuid.uuid4(), "the case could not be changed")
        assert answer == POOL_BUSY_WRITE_ANSWER

    def test_a_write_on_another_database_error_keeps_its_answer(self):
        exc = sa_exc.OperationalError("UPDATE ctem_cases", {}, Exception("x"))
        answer = ctem_cases._write_failed(exc, uuid.uuid4(), "the case could not be changed")
        assert (
            answer == "Error: the case could not be changed right now (a database error). Say so; nothing was changed."
        )

    async def test_list_and_get_answer_busy_when_no_connection_is_free(self, monkeypatch):
        """The tools themselves: the session cannot be checked out, the agent reads busy."""

        class _NoConnection:
            async def __aenter__(self):
                raise _pool_timeout()

            async def __aexit__(self, *_exc):
                return False

        from app.tools.base import ToolCall

        monkeypatch.setattr("app.core.database.async_session", lambda: _NoConnection())
        _defs, executor = ctem_cases.build_ctem_case_tools(user_id=str(uuid.uuid4()), agent_id=str(uuid.uuid4()))
        for name, args in (("list_ctem_cases", {}), ("get_ctem_case", {"case_id": str(uuid.uuid4())})):
            result = await executor.execute_tool(ToolCall(id=name, name=name, arguments=args))
            assert result.content == POOL_BUSY_ANSWER, (name, result.content)


class TestInboundA2A:
    def test_the_busy_reason_crosses_to_the_partner(self):
        from app.a2a import tasks as task_svc

        assert task_svc.BUSY_ERROR_TEXT.startswith(task_svc._PARTNER_VISIBLE_ERROR_PREFIXES)
        assert "QueuePool" not in task_svc.BUSY_ERROR_TEXT


class TestPlatformEventCap:
    def test_the_default_is_bounded_by_the_pool(self, monkeypatch):
        monkeypatch.setattr(dispatch.settings, "assignment_max_concurrent_runs", 10)
        monkeypatch.setattr(dispatch.settings, "db_pool_size", 15)
        monkeypatch.setattr(dispatch.settings, "db_max_overflow", 10)
        assert dispatch.assignment_concurrency_cap({"event": "ctem_case.opened"}, "platform_event") == 5
        assert dispatch.concurrency_cap("platform_event", {}) == 5

    def test_a_small_pool_still_runs_one(self, monkeypatch):
        monkeypatch.setattr(dispatch.settings, "assignment_max_concurrent_runs", 10)
        monkeypatch.setattr(dispatch.settings, "db_pool_size", 2)
        monkeypatch.setattr(dispatch.settings, "db_max_overflow", 0)
        assert dispatch.assignment_concurrency_cap(None, "platform_event") == 1

    def test_never_above_the_platform_default(self, monkeypatch):
        monkeypatch.setattr(dispatch.settings, "assignment_max_concurrent_runs", 3)
        monkeypatch.setattr(dispatch.settings, "db_pool_size", 100)
        monkeypatch.setattr(dispatch.settings, "db_max_overflow", 100)
        assert dispatch.assignment_concurrency_cap(None, "platform_event") == 3

    def test_an_explicit_cap_wins(self, monkeypatch):
        monkeypatch.setattr(dispatch.settings, "assignment_max_concurrent_runs", 10)
        monkeypatch.setattr(dispatch.settings, "db_pool_size", 15)
        monkeypatch.setattr(dispatch.settings, "db_max_overflow", 10)
        assert dispatch.assignment_concurrency_cap({"max_concurrent_runs": 8}, "platform_event") == 8

    def test_other_triggers_keep_the_platform_default(self, monkeypatch):
        monkeypatch.setattr(dispatch.settings, "assignment_max_concurrent_runs", 10)
        monkeypatch.setattr(dispatch.settings, "db_pool_size", 15)
        monkeypatch.setattr(dispatch.settings, "db_max_overflow", 10)
        assert dispatch.assignment_concurrency_cap(None, "integration_event") == 10
        assert dispatch.assignment_concurrency_cap(None) == 10


def test_the_helper_lives_beside_the_503_handler():
    """One definition of a pool timeout: the HTTP 503 and the tools read the same class."""
    assert db_availability.database_unavailable_reason(_pool_timeout()) is not None
