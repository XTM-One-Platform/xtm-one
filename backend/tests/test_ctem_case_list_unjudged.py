"""A case the viewer's view could not judge is unjudged, never hidden, never dropped (#2113).

``list_visibility`` splits the cases a viewer sees none of into ``hidden`` (every member was
judged and refused) and ``unjudged`` (a member its product did not answer for, a capped read, or
a case opened after an older view was built): for each state, the store's cases = the list's
count + hidden + unjudged. ``drop_gone`` leaves out a case deleted between the case read and the
view read, so a bulk delete never shows as hidden cases.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any

import pytest

from app.models.ctem_case import CtemCase, CtemCaseIssue
from app.services import ctem_cases as rules
from app.services import ctem_cases_view as view_of

NOW = datetime.now(UTC)


def _case(state: str = "new") -> CtemCase:
    return CtemCase(
        id=uuid.uuid4(),
        organization_id=uuid.uuid4(),
        vertical="cyber",
        title="Upgrade Acme Browser",
        remediation_kind="upgrade",
        keys=["upgrade:acme-browser"],
        pinned_issues=[],
        state=state,
        state_since=NOW,
        severity="P2",
    )


def _issue(source: str, external_id: str) -> rules.SourceIssue:
    return rules.SourceIssue(source=source, external_id=external_id, name=f"CVE-2026-{external_id}", cvss=7.5)


def _with(source: str, external_id: str, state: str = "new") -> tuple[CtemCase, list[CtemCaseIssue]]:
    case = _case(state)
    return case, [CtemCaseIssue(case_id=case.id, source=source, external_id=external_id, open=True)]


def _view(seen: list[str], judged: list[str], **kw: Any) -> view_of.ViewerView:
    issues = {m: _issue(*m.split(":", 1)) for m in seen}
    return view_of.ViewerView(issues=issues, open_ids=set(issues), judged=set(judged), built_at=NOW, **kw)


def _reconciles(cases: list[tuple[CtemCase, list[CtemCaseIssue]]], view: view_of.ViewerView) -> dict[str, Any]:
    _, counts = view_of.list_for_viewer(cases, view, limit=500)
    answer = view_of.list_visibility(cases, view)
    for state in {c.state for c, _ in cases}:
        stored = sum(1 for c, _ in cases if c.state == state)
        listed = counts.get(state, 0)
        assert listed + answer["hidden_by_state"].get(state, 0) + answer["unjudged_by_state"].get(state, 0) == stored
    return answer


def test_a_product_that_timed_out_leaves_its_cases_unjudged_never_hidden() -> None:
    cases = [_with("opencti", "1"), _with("openaev", "2"), _with("openaev", "3", "waiting_decision")]
    cases.append(_with("opencti", "9"))  # judged by OpenCTI and refused to this viewer
    view = _view(["opencti:1"], ["opencti:1", "openaev:2", "openaev:3", "opencti:9"])
    view.missing, view.reasons = ["openaev"], {"openaev": "timeout"}
    answer = _reconciles(cases, view)
    assert answer["hidden_cases"] == 1 and answer["unjudged_cases"] == 2
    assert answer["unjudged_by_state"] == {"new": 1, "waiting_decision": 1}
    assert answer["counts_partial"] is True
    assert answer["view"]["complete"] is False and answer["view"]["products_missing"] == {"openaev": "timeout"}


def test_a_product_this_viewer_has_no_access_to_hides_its_cases() -> None:
    """An identity reason (no credentials, token refused) is a refusal: hidden, not unjudged."""
    cases = [_with("openaev", "2")]
    view = _view([], ["openaev:2"])
    view.missing, view.reasons = ["openaev"], {"openaev": "not_connected"}
    answer = _reconciles(cases, view)
    assert answer["hidden_cases"] == 1 and answer["unjudged_cases"] == 0 and answer["counts_partial"] is False


def test_a_capped_read_leaves_the_unseen_cases_unjudged() -> None:
    cases = [_with("opencti", "1"), _with("opencti", "2")]
    answer = _reconciles(cases, _view(["opencti:1"], ["opencti:1", "opencti:2"], complete=False))
    assert answer["hidden_cases"] == 0 and answer["unjudged_cases"] == 1 and answer["view"]["capped"] is True


def test_an_older_view_reports_the_cases_opened_since_as_unjudged() -> None:
    """An administrator who reads every member, during a generation bump: hidden 0, no count drop."""
    before = [_with("opencti", str(n)) for n in range(5)]
    opened_since = [_with("opencti", str(n)) for n in range(5, 12)]
    older = _view([f"opencti:{n}" for n in range(5)], [f"opencti:{n}" for n in range(5)], stale=True)
    answer = _reconciles(before + opened_since, older)
    assert answer["hidden_cases"] == 0 and answer["unjudged_cases"] == 7
    assert answer["view"]["stale"] is True and answer["view"]["age_seconds"] is not None
    # Once the rebuild lands, every case is counted.
    rebuilt = _view([f"opencti:{n}" for n in range(12)], [f"opencti:{n}" for n in range(12)])
    _, counts = view_of.list_for_viewer(before + opened_since, rebuilt, limit=500)
    assert counts == {"new": 12} and view_of.list_visibility(before + opened_since, rebuilt)["unjudged_cases"] == 0


def test_a_view_cached_before_the_judged_field_keeps_the_old_reading_unless_stale() -> None:
    cases = [_with("opencti", "2")]
    legacy = view_of.ViewerView()
    assert view_of.list_visibility(cases, legacy)["hidden_cases"] == 1
    legacy.stale = True
    assert view_of.list_visibility(cases, legacy)["unjudged_cases"] == 1


def test_the_judged_members_and_build_time_survive_the_cache() -> None:
    view = _view(["opencti:1"], ["opencti:1", "opencti:2"])
    again = view_of.ViewerView.from_json(view.to_json())
    assert again.judged == {"opencti:1", "opencti:2"} and again.built_at == NOW and again.stale is False


class _Result:
    def __init__(self, ids: set[uuid.UUID]) -> None:
        self.ids = ids

    def scalars(self) -> set[uuid.UUID]:
        return self.ids


class _Db:
    def __init__(self, present: set[uuid.UUID]) -> None:
        self.present = present

    async def execute(self, _query: Any) -> _Result:
        return _Result(self.present)


@pytest.mark.asyncio
async def test_a_bulk_delete_between_the_case_read_and_the_view_read_is_never_hidden() -> None:
    """The cases were read, then a delete released their signals: an administrator's hidden stays 0."""
    signal = str(uuid.uuid4())
    kept = _case()
    kept_members = [CtemCaseIssue(case_id=kept.id, source="signal", external_id=signal, open=True)]
    deleted = [_case() for _ in range(100)]
    cases = [(kept, kept_members)] + [
        (c, [CtemCaseIssue(case_id=c.id, source="signal", external_id=str(uuid.uuid4()), open=True)]) for c in deleted
    ]
    # The view read after the delete: the released signals are no member of any case any more.
    view = view_of.ViewerView(signals={f"signal:{signal}"}, judged=set(), built_at=NOW)
    assert view_of.list_visibility(cases, view)["hidden_cases"] == 100  # what #2113 observed
    remaining = await view_of.drop_gone(_Db({kept.id}), cases, view)
    assert [c.id for c, _ in remaining] == [kept.id]
    assert view_of.list_visibility(remaining, view)["hidden_cases"] == 0


@pytest.mark.asyncio
async def test_an_unreadable_store_keeps_the_list_as_read() -> None:
    class _Broken:
        async def execute(self, _query: Any) -> Any:
            raise RuntimeError("pool exhausted")

    cases = [_with("opencti", "1")]
    assert await view_of.drop_gone(_Broken(), cases, view_of.ViewerView()) is cases
