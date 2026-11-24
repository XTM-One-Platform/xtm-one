"""#1692: an inconclusive pass is stored as ``inconclusive``; validations are one entry per (provider, ref).

A pass with ``conclusive`` false proves nothing (#1458): the entry, the history
event and the measures read ``inconclusive``, never ``passed``. A second answer
on the same reference replaces a pending entry, and the same outcome repeated
refreshes the entry without a second history row.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from app.models.ctem_case import CtemCase, CtemCaseEvent
from app.services import ctem_case_loop as loop
from app.services import ctem_cases as rules

NOW = datetime(2026, 11, 24, 6, 0, tzinfo=UTC)


class _Session:
    def __init__(self) -> None:
        self.added: list[Any] = []

    def add(self, row: Any) -> None:
        self.added.append(row)

    def validation_events(self) -> list[CtemCaseEvent]:
        return [r for r in self.added if isinstance(r, CtemCaseEvent) and r.kind == "validation"]


def _case(state: str = "remediating") -> CtemCase:
    return CtemCase(
        id=uuid.uuid4(),
        organization_id=uuid.uuid4(),
        vertical="cyber",
        title="Upgrade the dashboard server",
        remediation_kind="config",
        keys=["config:dashboard"],
        pinned_issues=[],
        state=state,
        state_since=NOW,
        validations=[],
    )


async def _record(db: _Session, case: CtemCase, result: str, *, at: datetime = NOW, **kw: Any) -> None:
    await loop.record_validation(
        db,  # type: ignore[arg-type]
        case,
        provider="openaev",
        kind="validation",
        ref="sim-1",
        result=result,
        now=at,
        **kw,
    )


@pytest.mark.asyncio
async def test_an_inconclusive_pass_is_stored_and_logged_as_inconclusive() -> None:
    db, case = _Session(), _case()
    await _record(db, case, "passed", conclusive=False)
    entry = case.validations[-1]
    assert entry["result"] == "inconclusive" and entry["reported_result"] == "passed"
    assert entry["conclusive"] is False and rules.passing(entry) is False
    assert [e.to_value for e in db.validation_events()] == ["inconclusive"]
    assert case.state == "remediating" and case.resolution_kind is None


@pytest.mark.asyncio
async def test_pending_then_two_identical_passes_keep_one_entry_and_one_pass_row() -> None:
    db, case = _Session(), _case()
    await _record(db, case, "pending")
    await _record(db, case, "passed", at=NOW + timedelta(minutes=3), conclusive=False)
    await _record(db, case, "passed", at=NOW + timedelta(minutes=3, seconds=4), conclusive=False)
    assert len(case.validations) == 1
    assert case.validations[0]["result"] == "inconclusive"
    assert case.validations[0]["at"] == (NOW + timedelta(minutes=3, seconds=4)).isoformat()
    assert [e.to_value for e in db.validation_events()] == ["pending", "inconclusive"]


@pytest.mark.asyncio
async def test_a_different_outcome_on_the_same_reference_is_a_new_entry() -> None:
    db, case = _Session(), _case(state="resolved")
    await _record(db, case, "passed")
    await _record(db, case, "failed", at=NOW + timedelta(hours=1))
    assert [v["result"] for v in case.validations] == ["passed", "failed"]
    assert case.state != "resolved"


@pytest.mark.asyncio
async def test_another_reference_is_appended() -> None:
    db, case = _Session(), _case()
    await _record(db, case, "pending")
    await loop.record_validation(
        db,  # type: ignore[arg-type]
        case,
        provider="openaev",
        kind="validation",
        ref="sim-2",
        result="pending",
        now=NOW,
    )
    assert [v["ref"] for v in case.validations] == ["sim-1", "sim-2"]


@pytest.mark.asyncio
async def test_a_conclusive_pass_still_resolves_and_reads_passed() -> None:
    db, case = _Session(), _case()
    await _record(db, case, "pending")
    await _record(db, case, "passed", conclusive=True)
    assert case.state == "resolved" and case.validations[-1]["result"] == "passed"
    assert "reported_result" not in case.validations[-1]
