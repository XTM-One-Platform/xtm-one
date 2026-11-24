"""Who decided a gated tool call, and what the trail says about it.

* A refusal no person gave (the decision's deadline, a call the set carried no
  verdict for, a tool the run no longer offers) never reads "Rejected by human
  reviewer" and never records ``human_reject``.
* Every decision set is audited once, with the actor, each verdict, its reason,
  the tool, the run and the surface; the deadline's row is the platform's.
* get_run shows ``human_input_history`` and its search reads it.
"""

from __future__ import annotations

import uuid
from types import SimpleNamespace
from typing import Any, cast

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.services import chat_approval_resume as resume_mod
from app.services.assignment_engine import _apply_approval_decisions
from app.services.builtin_tools.agent_builder import (
    _human_input_history_lines,
    _iter_run_search_sections,
)
from app.tools.approval import (
    DECISION_SOURCE_DEADLINE,
    DECISION_SOURCE_KEY,
    DECISION_SOURCE_NOT_OFFERED,
    OUTCOME_EXPIRED,
    OUTCOME_NOT_OFFERED,
    OUTCOME_UNDECIDED,
)


class _Executor:
    def __init__(self) -> None:
        self.calls: list[Any] = []

    async def execute_tool(self, call: Any) -> SimpleNamespace:
        self.calls.append(call)
        return SimpleNamespace(content="ok", is_error=False)


def _proposal(call_id: str, tool_name: str = "delete_entity") -> dict[str, Any]:
    return {"tool_call_id": call_id, "tool_name": tool_name, "arguments": {}, "input_schema": {}, "source": "test"}


async def _apply(decisions: dict[str, Any]) -> tuple[list[Any], dict[str, str]]:
    messages: list[Any] = []
    outcomes: dict[str, str] = {}
    await _apply_approval_decisions(
        executor=_Executor(),
        messages=messages,
        proposals=[_proposal("c1")],
        decisions=decisions,
        outcomes=outcomes,
    )
    return messages, outcomes


@pytest.mark.asyncio
async def test_a_deadline_refusal_is_not_a_human_reject() -> None:
    messages, outcomes = await _apply(
        {
            "c1": {
                "decision": "reject",
                "rejection_reason": "No answer before the deadline",
                DECISION_SOURCE_KEY: DECISION_SOURCE_DEADLINE,
            }
        }
    )

    assert "Rejected by human" not in messages[0].content
    assert "before its deadline" in messages[0].content
    assert outcomes == {"c1": OUTCOME_EXPIRED}


@pytest.mark.asyncio
async def test_a_call_with_no_verdict_is_refused_by_default_not_by_a_person() -> None:
    messages, outcomes = await _apply({})

    assert "Rejected by human" not in messages[0].content
    assert outcomes == {"c1": OUTCOME_UNDECIDED}


@pytest.mark.asyncio
async def test_a_tool_no_longer_offered_is_named_as_such() -> None:
    messages, outcomes = await _apply(
        {
            "c1": {
                "decision": "reject",
                "rejection_reason": "This tool is no longer available.",
                DECISION_SOURCE_KEY: DECISION_SOURCE_NOT_OFFERED,
            }
        }
    )

    assert "Rejected by human" not in messages[0].content
    assert "no longer available" in messages[0].content
    assert outcomes == {"c1": OUTCOME_NOT_OFFERED}


@pytest.mark.asyncio
async def test_a_persons_reject_keeps_its_wording_and_outcome() -> None:
    messages, outcomes = await _apply({"c1": {"decision": "reject", "rejection_reason": "wrong target"}})

    assert "Rejected by human reviewer: wrong target" in messages[0].content
    assert outcomes == {"c1": "human_reject"}


def test_verdict_rows_name_every_call_and_an_undecided_one() -> None:
    rows = resume_mod.approval_verdict_rows(
        [_proposal("c1"), _proposal("c2", "send_mail")],
        {"c1": {"decision": "reject", "rejection_reason": "no"}},
    )

    assert rows == [
        {"tool_call_id": "c1", "tool": "delete_entity", "verdict": "reject", "reason": "no"},
        {"tool_call_id": "c2", "tool": "send_mail", "verdict": "undecided", "reason": None},
    ]


class _Nested:
    async def __aenter__(self) -> _Nested:
        return self

    async def __aexit__(self, *exc: object) -> bool:
        return False


class _Db:
    def begin_nested(self) -> _Nested:
        return _Nested()


def _db() -> AsyncSession:
    return cast(AsyncSession, _Db())


def _run() -> SimpleNamespace:
    return SimpleNamespace(
        id=uuid.uuid4(),
        user_id=uuid.uuid4(),
        pending_approvals=[_proposal("c1")],
        approval_decisions={"c1": {"decision": "approve"}},
    )


def _capture(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, dict[str, Any]]]:
    rows: list[tuple[str, dict[str, Any]]] = []

    async def _log_audit(db: Any, **kwargs: Any) -> None:
        rows.append(("person", kwargs))

    async def _log_system_audit(db: Any, **kwargs: Any) -> None:
        rows.append(("system", kwargs))

    monkeypatch.setattr("app.services.audit.log_audit", _log_audit)
    monkeypatch.setattr("app.services.audit.log_system_audit", _log_system_audit)
    return rows


@pytest.mark.asyncio
async def test_a_persons_decision_is_audited_under_the_person(monkeypatch: pytest.MonkeyPatch) -> None:
    rows = _capture(monkeypatch)
    run = _run()
    user = SimpleNamespace(id=uuid.uuid4(), email="r@example.com")

    await resume_mod.record_approval_decision(_db(), run=run, user=user, surface="assignment_respond")

    assert [kind for kind, _ in rows] == ["person"]
    kwargs = rows[0][1]
    assert kwargs["user"] is user
    assert kwargs["action"] == "decide"
    assert kwargs["entity_type"] == resume_mod.RUN_APPROVAL_ENTITY
    assert kwargs["entity_id"] == run.id
    assert kwargs["details"]["surface"] == "assignment_respond"
    assert kwargs["details"]["verdicts"][0]["verdict"] == "approve"


@pytest.mark.asyncio
async def test_a_deadline_decision_is_audited_as_the_platform(monkeypatch: pytest.MonkeyPatch) -> None:
    rows = _capture(monkeypatch)
    run = _run()

    await resume_mod.record_approval_decision(
        _db(), run=run, user=None, surface="deadline", system_actor="decision_deadline"
    )

    assert [kind for kind, _ in rows] == ["system"]
    assert rows[0][1]["actor"] == "decision_deadline"
    assert rows[0][1]["anchor_user_id"] == run.user_id


@pytest.mark.asyncio
async def test_an_audit_failure_never_costs_the_decision(monkeypatch: pytest.MonkeyPatch) -> None:
    async def _boom(db: Any, **kwargs: Any) -> None:
        raise RuntimeError("audit down")

    monkeypatch.setattr("app.services.audit.log_audit", _boom)

    await resume_mod.record_approval_decision(_db(), run=_run(), user=SimpleNamespace(id=uuid.uuid4()), surface="x")


def _history_run() -> SimpleNamespace:
    return SimpleNamespace(
        human_input_history=[
            {
                "question": "Approval required",
                "response": "Approval decisions: c1:reject",
                "resolution": "answered",
                "responded_at": "2026-11-24T05:00:00+00:00",
                "responded_by": "Reviewer One",
                "responded_by_id": "u-1",
                "surface": "teams_card",
            },
            {
                "resolution": "expired",
                "responded_at": "2026-11-24T06:00:00+00:00",
                "responded_by": "Deadline",
                "surface": "deadline",
                "gate_outcomes": [{"tool_call_id": "c2", "tool_name": "send_mail", "outcome": OUTCOME_EXPIRED}],
            },
        ]
    )


def test_get_run_lists_who_answered_each_pause() -> None:
    text = "\n".join(_human_input_history_lines(_history_run()))

    assert "Human input history (2):" in text
    assert "answered by Reviewer One (id: `u-1`) via teams_card" in text
    assert "expired by Deadline via deadline" in text
    assert f"send_mail (c2): {OUTCOME_EXPIRED}" in text


def test_get_run_search_reads_the_history() -> None:
    run = _history_run()
    for name in (
        "result_summary",
        "error",
        "human_input_question",
        "human_input_response",
        "trigger_payload",
        "trigger_context",
        "actions_taken",
        "messages",
    ):
        setattr(run, name, None)

    labels = [label for label, text in _iter_run_search_sections(run) if "Reviewer One" in text]

    assert labels == ["human_input_history[0]"]
