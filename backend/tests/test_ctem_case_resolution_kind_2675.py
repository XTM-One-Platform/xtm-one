# SPDX-License-Identifier: Apache-2.0
"""A resolved CTEM case always says how it was closed, and its members open at source are counted apart (#2675).

An agent that resolves a case with evidence and no ``resolution_kind`` (the loop closing it
as "not relevant") no longer leaves the kind null: the newest passing validation reference
backs ``fixed`` or ``not_exposed``, otherwise ``closed_by_decision``. An agent passes
``not_relevant`` for an out-of-scope close; ``dismissed`` stays a person's decision (#2252).
Members keep the state the source reports (#1646): on a closed case the read answers
``open_issue_count`` 0 and ``open_at_source_count`` for the members still open at source.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any, cast

import pytest

from app.models.ctem_case import DEFAULT_RESOLUTION_KIND, RESOLUTION_KINDS, CtemCase, CtemCaseIssue
from app.services import ctem_cases as rules
from app.services import ctem_cases_store as store
from app.services import ctem_cases_view as view_of

NOW = datetime(2026, 11, 27, 18, 0, tzinfo=UTC)


class _Db:
    """The calls ``update_case`` makes on a close: nothing to read, the rows it adds kept."""

    def __init__(self) -> None:
        self.added: list[Any] = []

    async def scalars(self, _query: Any) -> list[Any]:
        return []

    async def scalar(self, _query: Any) -> None:
        return None

    async def execute(self, _query: Any) -> Any:
        return SimpleNamespace(scalars=lambda: [])

    def add(self, row: Any) -> None:
        self.added.append(row)


def _case(*, validations: list[dict[str, Any]] | None = None, validation_plan: bool = False) -> Any:
    return SimpleNamespace(
        id=uuid.uuid4(),
        state="analyzing",
        owner_user_id=None,
        due_at=None,
        validation_plan=validation_plan,
        validations=validations or [],
        resolution_kind=None,
        accepted_reason=None,
        accepted_until=None,
        state_since=None,
    )


async def _resolve(case: Any, **kw: Any) -> Any:
    return await store.update_case(
        cast(Any, _Db()),
        case,
        actor="agent-1",
        actor_kind=kw.pop("actor_kind", "agent"),
        state="resolved",
        evidence=kw.pop("evidence", "Not relevant: score 4/100 (threshold 50)"),
        now=NOW,
        **kw,
    )


def test_the_new_kinds_are_resolution_kinds() -> None:
    assert "not_relevant" in RESOLUTION_KINDS
    assert DEFAULT_RESOLUTION_KIND in RESOLUTION_KINDS


async def test_an_agent_resolve_with_evidence_and_no_kind_is_never_null() -> None:
    case = await _resolve(_case())
    assert (case.state, case.resolution_kind) == ("resolved", DEFAULT_RESOLUTION_KIND)


async def test_an_agent_closes_out_of_scope_as_not_relevant() -> None:
    case = await _resolve(_case(), resolution_kind="not_relevant")
    assert case.resolution_kind == "not_relevant"


@pytest.mark.parametrize(
    ("validation", "kind"),
    [
        ({"result": "passed"}, "fixed"),
        ({"result": "not_exposed"}, "not_exposed"),
        ({"result": "passed", "conclusive": False}, DEFAULT_RESOLUTION_KIND),
        ({"result": "failed"}, DEFAULT_RESOLUTION_KIND),
    ],
)
def test_the_derived_kind_needs_a_passing_validation(validation: dict[str, Any], kind: str) -> None:
    assert rules.derived_resolution_kind([validation]) == kind


def test_no_validation_is_never_not_exposed_by_default() -> None:
    assert rules.derived_resolution_kind(None) == DEFAULT_RESOLUTION_KIND


async def test_a_passing_validation_backs_the_derived_kind() -> None:
    case = await _resolve(_case(validations=[{"result": "not_exposed"}], validation_plan=True))
    assert case.resolution_kind == "not_exposed"


async def test_a_person_still_resolves_with_the_kind_given() -> None:
    case = await _resolve(_case(), actor_kind="user", resolution_kind="mitigated")
    assert case.resolution_kind == "mitigated"


async def test_dismissed_still_needs_a_person() -> None:
    with pytest.raises(rules.CaseRuleError, match="only a person dismisses"):
        await _resolve(_case(), resolution_kind="dismissed")


def _summary(state: str) -> dict[str, Any]:
    issue = rules.SourceIssue(source="signal", external_id="s1", name="minio", assets=("s3-1",), first_seen_at=NOW)
    case = CtemCase(
        id=uuid.uuid4(),
        organization_id=uuid.uuid4(),
        vertical="cyber",
        title="Upgrade minio",
        remediation_kind="upgrade",
        keys=["upgrade:minio/minio@latest"],
        pinned_issues=[],
        state=state,
        state_since=NOW,
    )
    rows = [CtemCaseIssue(case_id=case.id, source=issue.source, external_id=issue.external_id, open=True)]
    view = view_of.ViewerView(issues={issue.member_id: issue}, open_ids={issue.member_id})
    row = view_of.summary(case, rows, view)
    assert row is not None
    return row


def test_a_closed_case_counts_its_members_open_at_source_apart() -> None:
    row = _summary("resolved")
    assert (row["open_issue_count"], row["open_at_source_count"], row["issue_count"]) == (0, 1, 1)


def test_an_open_case_counts_its_open_members_as_open_issues() -> None:
    row = _summary("analyzing")
    assert (row["open_issue_count"], row["open_at_source_count"]) == (1, 0)
