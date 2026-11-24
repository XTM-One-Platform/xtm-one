"""Long waits of a human gate, F3 (#842, S7; dev-docs/human-gates.md section 7).

Pure rules here; the queue release and the expiry against PostgreSQL are in
``test_decision_waits_postgres.py``.
"""

from __future__ import annotations

from typing import Any

import pytest
from sqlalchemy.dialects import postgresql

from app.core.config import settings
from app.models.assignment import AssignmentRun
from app.services import decision_waits as dw


def _ask(recommendation: str | None = None) -> dict[str, Any]:
    ask: dict[str, Any] = {
        "question": "Publish the report?",
        "options": [{"key": "A", "label": "Publish"}, {"key": "B", "label": "Hold"}],
    }
    if recommendation is not None:
        ask["recommendation"] = recommendation
    return ask


def test_expiry_takes_the_recommended_option() -> None:
    assert dw.expiry_outcome(_ask("b")) == {"option": "B", "label": "Hold", "response": "B"}


def test_expiry_without_a_default_is_no_answer() -> None:
    assert dw.expiry_outcome(_ask()) == {"text": dw.NO_ANSWER, "response": dw.NO_ANSWER}
    assert dw.expiry_outcome(None)["response"] == dw.NO_ANSWER
    # A recommendation that names no option is no default.
    assert dw.expiry_outcome(_ask("Z"))["response"] == dw.NO_ANSWER


def test_expired_tool_approval_rejects() -> None:
    assert dw.expiry_outcome(_ask("A"), approval=True)["option"] == "reject"


def test_release_is_off_at_zero(monkeypatch: pytest.MonkeyPatch) -> None:
    # 0 turns the timed release off; a tool approval still never holds (#1647).
    monkeypatch.setattr(settings, "human_gate_queue_release_hours", 0)
    dialect: Any = postgresql.dialect
    compiled: Any = dw.released_wait_clause(AssignmentRun).compile(
        dialect=dialect(), compile_kwargs={"literal_binds": True}
    )
    sql = str(compiled)
    assert "decisions.created_at <=" not in sql
    assert "'tool_approval'" in sql


def test_release_reads_the_open_decision_and_spares_chat_runs(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "human_gate_queue_release_hours", 4)
    sql = str(dw.holds_assignment(AssignmentRun, ("running", "awaiting_human")).compile(dialect=postgresql.dialect()))
    assert "decisions" in sql
    assert "chat_handover" in dw.CHAT_STARTED_TRIGGER_TYPES
