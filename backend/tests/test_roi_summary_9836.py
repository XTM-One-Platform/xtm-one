"""The list column's summary (#9836 R6a, design ``dev-docs/roi-outcomes.md`` 10.8; ledger since #12077 L7a).

``GET /roi/summary?subject_type=agent|fleet&ids=`` answers the figures of one
page of the agent or fleet list in one read. Since slice L7a each row carries
the ledger's ``figures`` of that object (``roi_ledger.figures_for_subjects``),
so the list column, the agent page hero and the administrator's by-agent row
show one figure (invariant d), the k rule and the masked costs included. An id
outside the reader's scope is left out; an id that declares nothing and counts
nothing answers ``declared: false``.
"""

from __future__ import annotations

import inspect
import uuid
from datetime import date, timedelta
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from app.api.v1 import roi as roi_api
from app.services import roi, roi_access, roi_report
from app.services.roi_figures import CLASS_AUTOMATION, LedgerFact, Viewer, compute, compute_subjects, run_fact

A1, A2, A3, A4 = uuid.uuid4(), uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
F1, F2 = uuid.uuid4(), uuid.uuid4()
USER = uuid.uuid4()
PEOPLE = [uuid.UUID(int=i + 1) for i in range(4)]
FIRST = date(2026, 10, 5)
LAST = FIRST + timedelta(days=29)
BASELINE = roi.BASELINES_BY_UNIT["threat_report_triaged"]
FLEET_OF = {A1: F1, A2: F1, A3: F2}


def _facts() -> list[LedgerFact]:
    """Runs of three agents by several people, one rejected, and a cost fact of each run."""
    facts: list[LedgerFact] = []
    for n, (agent, person, units, outcome) in enumerate(
        [
            (A1, PEOPLE[0], 3, None),
            (A1, PEOPLE[1], 2, roi.OUTCOME_ACCEPTED),
            (A2, PEOPLE[1], 4, None),
            (A2, PEOPLE[2], 1, roi.OUTCOME_REJECTED),
            (A3, PEOPLE[3], 2, None),
        ]
    ):
        day = FIRST + timedelta(days=n)
        facts.append(
            run_fact(
                day=day,
                person_id=person,
                attribution="user",
                agent_id=agent,
                fleet_id=FLEET_OF[agent],
                unit=BASELINE.unit,
                units=units,
                outcome=outcome,
                review_min=None,
                baseline=BASELINE,
                rate=40.0,
            )
        )
        facts.append(
            LedgerFact(
                day=day,
                person_id=person,
                attribution="user",
                agent_id=agent,
                fleet_id=FLEET_OF[agent],
                cost_class=CLASS_AUTOMATION,
                cost=0.25 * (n + 1),
            )
        )
    return facts


def _summary(facts, *, subject_type, ids, viewer, cost_masked=False):
    return compute_subjects(
        facts,
        subject_type=subject_type,
        ids=ids,
        viewer=viewer,
        window_first=FIRST,
        window_last=LAST,
        cost_masked=cost_masked,
        period_days=30,
    )


def _hero(facts, *, scope, scope_id, viewer, cost_masked=False):
    """The agent page (or fleet view) hero: ``GET /roi?scope=agent|fleet&id=`` ``figures``."""
    return compute(
        facts,
        scope=scope,
        viewer=viewer,
        scope_id=scope_id,
        window_first=FIRST,
        window_last=LAST,
        cost_masked=cost_masked,
        period_days=30,
    ).as_dict()


VIEWERS = {
    "administrator": Viewer(user_id=PEOPLE[0], organization=True),
    "member under k": Viewer(
        user_id=PEOPLE[1], agent_ids=frozenset({A1, A2, A3}), fleet_ids=frozenset({F1, F2}), min_group_size=3
    ),
    "member, rule off": Viewer(user_id=PEOPLE[1], agent_ids=frozenset({A1, A2, A3}), fleet_ids=frozenset({F1, F2})),
}


@pytest.mark.parametrize("viewer_name", list(VIEWERS))
@pytest.mark.parametrize("cost_masked", [False, True])
@pytest.mark.parametrize("subject_type", ["agent", "fleet"])
def test_a_list_row_equals_the_agent_page_hero_of_the_same_object(subject_type, cost_masked, viewer_name):
    """Invariant d: the list column value is the hero of the agent page (fleet view) for the same viewer."""
    facts = _facts()
    viewer = VIEWERS[viewer_name]
    ids = [A1, A2, A3] if subject_type == "agent" else [F1, F2]
    by_id = _summary(facts, subject_type=subject_type, ids=ids, viewer=viewer, cost_masked=cost_masked)
    for sid in ids:
        row = roi_api._summary_item(sid, by_id[sid], declares=True)
        hero = _hero(facts, scope=subject_type, scope_id=sid, viewer=viewer, cost_masked=cost_masked)
        assert row["declared"] is True
        assert row["figures"]["totals"] == hero["totals"], (sid, viewer_name)
        assert row["figures"]["by_source"] == hero["by_source"]
        assert row["figures"]["suppressed"] == hero["suppressed"]
        if cost_masked:
            assert row["figures"]["totals"]["net_value"] is None and row["figures"]["totals"]["ai_cost"] is None


def test_under_k_the_row_is_the_viewer_s_own_share_as_on_the_agent_page():
    facts = _facts()
    viewer = VIEWERS["member under k"]
    row = roi_api._summary_item(A1, _summary(facts, subject_type="agent", ids=[A1], viewer=viewer)[A1], declares=True)
    assert row["figures"]["suppressed"]["own_share_only"] is True
    own = [f for f in facts if f.agent_id == A1 and f.person_id == viewer.user_id]
    assert row["figures"]["totals"]["runs"] == sum(f.runs for f in own)


def test_an_object_that_counts_nothing_and_declares_nothing_has_no_figure():
    facts = _facts()
    viewer = VIEWERS["administrator"]
    by_id = _summary(facts, subject_type="agent", ids=[A4], viewer=viewer)
    assert roi_api._summary_item(A4, by_id[A4], declares=False) == {"id": str(A4), "declared": False}
    zero = roi_api._summary_item(A4, by_id[A4], declares=True)
    assert zero["declared"] is True and zero["figures"]["totals"]["runs"] == 0
    assert roi_api._summary_item(A4, None, declares=True) == {"id": str(A4), "declared": False}


def test_spend_alone_stays_undeclared():
    """A cost fact with no counted run (#10441) declares nothing: the list keeps "-"."""
    spend = [
        LedgerFact(
            day=FIRST,
            person_id=PEOPLE[0],
            attribution="user",
            agent_id=A4,
            fleet_id=None,
            cost_class=CLASS_AUTOMATION,
            cost=3.0,
        )
    ]
    by_id = _summary(spend, subject_type="agent", ids=[A4], viewer=VIEWERS["administrator"])
    assert roi_api._summary_item(A4, by_id[A4], declares=False)["declared"] is False


def _patch(monkeypatch, seen, reader):
    from app.services import roi_ledger

    async def _scope(_db, *, user_id, is_admin):
        return reader

    async def _viewer(_db, *, user_id, is_admin):
        return VIEWERS["administrator"]

    async def _ledger(_db, **kw):
        seen["ledger"] = kw
        return "EUR", _summary(_facts(), subject_type=kw["subject_type"], ids=kw["ids"], viewer=kw["viewer"])

    async def _declared(_db, subject_type, ids):
        seen["declared"] = list(ids)
        return set()

    async def _details(
        _db: object, subject_type: str, ids: list[uuid.UUID], *, window: str, currency: str | None
    ) -> dict[uuid.UUID, dict[str, object]]:
        seen["details"] = list(ids)
        return {
            sid: {"rejected": 1, "work_items": 0, "unreviewed_work_items": 0, "assumption_facts": []} for sid in ids
        }

    monkeypatch.setattr(roi_access, "reader_scope", _scope)
    monkeypatch.setattr(roi_ledger, "viewer_for", _viewer)
    monkeypatch.setattr(roi_ledger, "figures_for_subjects", _ledger)
    monkeypatch.setattr(roi_api.roi_report, "declared_subjects", _declared)
    monkeypatch.setattr(roi_report, "record_details", _details)


@pytest.mark.asyncio
async def test_an_id_outside_the_reader_s_scope_is_left_out_in_one_read(monkeypatch):
    seen: dict = {}
    _patch(monkeypatch, seen, roi_access.RoiScope(agent_ids=frozenset({A1, A2})))
    user = SimpleNamespace(id=USER, is_admin=False)
    answer = await roi_api.get_roi_summary(
        subject_type="agent", ids=[A1, A3, A2, A1], window="30d", currency=None, db=None, user=user
    )
    assert seen["ledger"]["ids"] == [A1, A2]  # one read for the page, the out-of-scope id dropped, no duplicate
    assert seen["declared"] == [A1, A2]
    assert [item["id"] for item in answer["items"]] == [str(A1), str(A2)]
    assert answer["currency"] == "EUR" and answer["subject_type"] == "agent" and "cost_masked" in answer
    assert all(item["declared"] and "totals" in item["figures"] for item in answer["items"])
    # The hero's record counts and inputs travel with the row, read once for the page (UX r1 B1).
    assert seen["details"] == [A1, A2]
    assert all(item["records"]["rejected"] == 1 for item in answer["items"])


def test_a_row_carries_the_records_the_agent_page_popover_reads() -> None:
    """UX r1 B1: the row's popover says the rejected records, work items and inputs the agent page says."""
    facts = _facts()
    viewer = VIEWERS["administrator"]
    by_id = compute_subjects(
        facts, subject_type="agent", ids=[A2], viewer=viewer, window_first=FIRST, window_last=LAST, period_days=30
    )
    records = {"rejected": 1, "work_items": 0, "unreviewed_work_items": 0, "assumption_facts": [{"kind": "unit"}]}
    row = roi_api._summary_item(A2, by_id[A2], declares=True, records=records)
    assert row["records"] == records
    assert "records" not in roi_api._summary_item(A2, by_id[A2], declares=True)
    assert roi_api._summary_item(A4, None, declares=True, records=records) == {"id": str(A4), "declared": False}


@pytest.mark.asyncio
async def test_an_administrator_reads_every_id(monkeypatch):
    seen: dict = {}
    _patch(monkeypatch, seen, roi_access.RoiScope(organization=True))
    user = SimpleNamespace(id=USER, is_admin=True)
    await roi_api.get_roi_summary(subject_type="fleet", ids=[F1, F2], window="7d", currency=None, db=None, user=user)
    assert seen["ledger"]["ids"] == [F1, F2] and seen["ledger"]["subject_type"] == "fleet"
    assert seen["ledger"]["window"] == "7d"


@pytest.mark.asyncio
async def test_a_page_is_bounded(monkeypatch):
    seen: dict = {}
    _patch(monkeypatch, seen, roi_access.RoiScope(organization=True))
    user = SimpleNamespace(id=USER, is_admin=True)
    too_many = [uuid.uuid4() for _ in range(roi_report.SUMMARY_MAX_IDS + 1)]
    with pytest.raises(HTTPException) as refused:
        await roi_api.get_roi_summary(
            subject_type="agent", ids=too_many, window="30d", currency=None, db=None, user=user
        )
    assert refused.value.status_code == 422 and "ledger" not in seen


def test_the_summary_reads_the_ledger_only_and_starts_no_run():
    source = inspect.getsource(roi_api.get_roi_summary) + inspect.getsource(roi_api._summary_item)
    assert "figures_for_subjects" in source and "load_report" not in source
    for forbidden in ("execute_agent", "run_agent", "completion"):
        assert forbidden not in source
