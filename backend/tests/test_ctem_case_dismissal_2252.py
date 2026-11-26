"""A person rejects and closes a CTEM case at a gate (#2252): resolved, dismissed, the reason kept.

The state rules accept ``resolution_kind`` ``dismissed`` from a person's own move, or
from the loop once a person answered a gate of the case with ``reject_and_close``;
an agent never dismisses on its own. The history keeps who answered and the reason,
or says that none was given; a plain reject still reads as rejected.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any, cast

import pytest

from app.models.ctem_case import RESOLUTION_KINDS
from app.services import ctem_cases as rules
from app.services import ctem_cases_store as store
from app.services import ctem_cases_view as view_of

NOW = datetime(2026, 11, 25, 16, 0, tzinfo=UTC)


def _check(target: str, **kw: Any) -> None:
    rules.check_transition(
        current=kw.pop("current", "waiting_decision"),
        target=target,
        actor_kind=kw.pop("actor_kind", "agent"),
        owner_user_id=None,
        now=NOW,
        **kw,
    )


def test_dismissed_is_a_resolution_kind() -> None:
    assert "dismissed" in RESOLUTION_KINDS


def test_an_agent_never_dismisses_on_its_own() -> None:
    with pytest.raises(rules.CaseRuleError, match="only a person dismisses"):
        _check("resolved", resolution_kind="dismissed")


def test_an_answered_close_option_lets_the_loop_dismiss_without_evidence_or_retest() -> None:
    _check("resolved", resolution_kind="dismissed", dismissal_answered=True, validation_plan=True)


def test_a_person_dismisses_a_case_directly() -> None:
    _check("resolved", resolution_kind="dismissed", actor_kind="user")


def test_dismissed_goes_with_resolved_only() -> None:
    with pytest.raises(rules.CaseRuleError, match="dismissed case is resolved"):
        _check("analyzing", resolution_kind="dismissed", dismissal_answered=True)


def test_a_plain_resolve_still_needs_evidence() -> None:
    with pytest.raises(rules.CaseRuleError, match="needs evidence"):
        _check("resolved", dismissal_answered=True)


@pytest.mark.parametrize(
    ("option", "closes"),
    [
        ("reject_and_close", True),
        ("Reject and close (not relevant, no action)", True),
        ("reject-and-close", True),
        ("reject", False),
        ("Rejected", False),
        ("", False),
        (None, False),
    ],
)
def test_the_close_option_is_read_by_key_or_label(option: str | None, closes: bool) -> None:
    assert rules.closes_case(option) is closes


def _gate(option: str, *, by: str = "Jane Analyst") -> Any:
    return SimpleNamespace(
        kind="gate", from_value=view_of.REVIEW_GATE, to_value="answered", reason=by, evidence=option, at=NOW
    )


def test_the_review_reads_reject_and_close_as_rejected_and_plain_reject_unchanged() -> None:
    assert view_of.review_outcome([_gate("reject_and_close")]) == view_of.REVIEW_REJECTED
    assert view_of.review_outcome([_gate("reject")]) == view_of.REVIEW_REJECTED


class _Db:
    """The calls ``update_case`` makes: the gate events read, then the rows it adds."""

    def __init__(self, gates: list[SimpleNamespace]) -> None:
        self.gates = gates
        self.added: list[Any] = []

    async def scalars(self, _query: Any) -> list[SimpleNamespace]:
        return list(self.gates)

    async def scalar(self, _query: Any) -> None:
        return None

    async def execute(self, _query: Any) -> Any:
        # The close cancels the case's open items and timers (#2494): none here.
        return SimpleNamespace(scalars=lambda: [])

    def add(self, row: Any) -> None:
        self.added.append(row)


def _case() -> Any:
    return SimpleNamespace(
        id=uuid.uuid4(),
        state="waiting_decision",
        owner_user_id=None,
        due_at=None,
        validation_plan=True,
        validations=[],
        resolution_kind=None,
        accepted_reason=None,
        accepted_until=None,
        state_since=None,
    )


async def test_the_close_answer_resolves_dismissed_with_the_person_and_reason() -> None:
    db = _Db([_gate("reject_and_close")])
    case = _case()
    await store.update_case(
        cast(Any, db),
        case,
        actor="agent-1",
        actor_kind="agent",
        state="resolved",
        resolution_kind="dismissed",
        reason="not relevant: product not deployed",
        now=NOW,
    )
    assert (case.state, case.resolution_kind) == ("resolved", "dismissed")
    event = db.added[-1]
    assert event.to_value == "resolved" and event.reason == "not relevant: product not deployed"
    assert "Jane Analyst" in event.evidence


async def test_no_reason_given_is_written_and_the_case_still_closes() -> None:
    db = _Db([_gate("Reject and close (not relevant, no action)")])
    case = _case()
    await store.update_case(
        cast(Any, db),
        case,
        actor="agent-1",
        actor_kind="agent",
        state="resolved",
        resolution_kind="dismissed",
        now=NOW,
    )
    assert (case.state, case.resolution_kind) == ("resolved", "dismissed")
    assert db.added[-1].reason == rules.NO_REASON_GIVEN


async def test_a_plain_reject_answer_does_not_let_the_agent_dismiss() -> None:
    db = _Db([_gate("reject")])
    case = _case()
    with pytest.raises(rules.CaseRuleError, match="only a person dismisses"):
        await store.update_case(
            cast(Any, db),
            case,
            actor="agent-1",
            actor_kind="agent",
            state="resolved",
            resolution_kind="dismissed",
            now=NOW,
        )
    assert case.state == "waiting_decision" and not db.added
