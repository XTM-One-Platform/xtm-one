"""A tool-approval pause never holds its assignment; a re-ask keeps its clock (#1647)."""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any

import pytest
from sqlalchemy.dialects import postgresql

from app.core.config import settings
from app.models.assignment import AssignmentRun
from app.models.decision_hooks import carried_asked_at
from app.services import decision_waits as dw


def _sql(clause: Any) -> str:
    dialect: Any = postgresql.dialect
    compiled: Any = clause.compile(dialect=dialect(), compile_kwargs={"literal_binds": True})
    return str(compiled)


def test_tool_approval_kind_is_released_at_once() -> None:
    assert "tool_approval" in dw.RELEASED_AT_ONCE_KINDS
    assert "hitl" not in dw.RELEASED_AT_ONCE_KINDS


def test_release_clause_names_the_tool_approval_kind(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "human_gate_queue_release_hours", 4)
    sql = _sql(dw.released_wait_clause(AssignmentRun))
    assert "'tool_approval'" in sql
    assert "decisions.created_at <=" in sql
    assert "chat_handover" in sql


def test_release_off_still_frees_a_tool_approval(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "human_gate_queue_release_hours", 0)
    sql = _sql(dw.released_wait_clause(AssignmentRun))
    assert "'tool_approval'" in sql
    assert "decisions.created_at <=" not in sql


def _previous(status: str, kind: str = "tool_approval", question: str = "Approve the call?") -> SimpleNamespace:
    return SimpleNamespace(
        status=status, kind=kind, ask={"question": question}, created_at=datetime(2026, 11, 23, 9, 55, tzinfo=UTC)
    )


def test_re_ask_after_an_unanswered_pause_keeps_the_first_time() -> None:
    ask = {"question": "Approve the call?"}
    assert carried_asked_at(_previous("cancelled"), "tool_approval", ask) == datetime(2026, 11, 23, 9, 55, tzinfo=UTC)
    assert carried_asked_at(_previous("open"), "tool_approval", ask) is not None


@pytest.mark.parametrize(
    "previous",
    [
        None,
        _previous("answered"),
        _previous("expired"),
        _previous("cancelled", kind="hitl"),
        _previous("cancelled", question="Another question?"),
    ],
)
def test_a_new_question_starts_a_new_clock(previous: SimpleNamespace | None) -> None:
    assert carried_asked_at(previous, "tool_approval", {"question": "Approve the call?"}) is None
