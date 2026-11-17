"""ROI everywhere, slice R1 (#9836, ``dev-docs/roi-outcomes.md`` 10.2): who reads what.

Every user reads the ROI of the agents and fleets they can see (``scope=mine``);
a non-administrator reads the organization-wide totals while the setting
``roi_org_totals_visible`` is on, the rows outside their scope folded; every
list of a report adds up to its totals; masked costs are ``null``, never 0.
"""

from __future__ import annotations

import inspect
import uuid
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from app.api.deps import get_current_user, require_admin
from app.api.v1 import roi as roi_api
from app.services import roi, roi_access, roi_report

A1, A2, A3 = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
F1, F2 = uuid.uuid4(), uuid.uuid4()
NOW = datetime(2026, 11, 7, 12, 0, tzinfo=UTC)
USER = uuid.uuid4()


def _rows():
    return [
        roi_report.RecordRow(A1, F1, roi.RunMeasure("threat_report_triaged", units=6, llm_cost=1.0)),
        roi_report.RecordRow(A2, F1, roi.RunMeasure("cve_assessed", units=10, llm_cost=0.5)),
        roi_report.RecordRow(A2, None, roi.RunMeasure("cve_assessed", units=3, llm_cost=0.2, outcome="rejected")),
        roi_report.RecordRow(A3, F2, roi.RunMeasure("cve_assessed", units=4, llm_cost=0.3)),
        roi_report.RecordRow(A3, None, roi.RunMeasure("threat_report_triaged", units=2, llm_cost=0.1)),
    ]


def _build(rows=None):
    currency, rates, label = roi_report.rates_for("USD")
    rows = _rows() if rows is None else rows
    report = roi_report.build_report(
        rows,
        window="30d",
        since=roi_report.window_since("30d", now=NOW),
        currency=currency,
        rates=rates,
        rate_label=label,
        agent_names={A1: "Threat relevance", A2: "Vuln triage", A3: "Someone else's"},
        fleet_names={F1: "CTEM", F2: "Other team"},
    )

    def figures_of(measures):
        return roi.compute_roi(measures, currency=currency, rates=rates, period_days=30)

    return report, rows, figures_of


def _sum(groups, attr):
    return round(sum(getattr(g, attr) for g in groups if g is not None), 2)


FIGURES = ("minutes_saved", "value", "llm_cost", "net_value", "runs")


def _assert_lists_add_up(report):
    org = report.organization
    agents = [g.figures for g in report.agents] + [report.other_agents]
    fleets = [g.figures for g in report.fleets] + [report.other_fleets, report.no_fleet]
    for attr in FIGURES:
        assert _sum(agents, attr) == pytest.approx(getattr(org, attr), abs=0.02), f"agents {attr}"
        assert _sum(fleets, attr) == pytest.approx(getattr(org, attr), abs=0.02), f"fleets {attr}"
    # The by-unit rows are never folded: a unit names no object.
    assert sum(org.units_by_kind.values()) == org.units


# --- The scope ---------------------------------------------------------------


def test_a_record_is_held_by_its_agent_or_its_fleet_and_counted_once():
    scope = roi_access.RoiScope(agent_ids=frozenset({A1}), fleet_ids=frozenset({F1}))
    assert scope.holds_record(A1, F1)  # both in scope: one record
    assert scope.holds_record(A2, F1)  # its fleet only
    assert scope.holds_record(A1, None)  # its agent only
    assert not scope.holds_record(A3, F2)
    assert not scope.holds_record(None, None)
    org = roi_access.RoiScope(organization=True)
    assert org.holds_record(A3, F2) and org.holds_agent(None)


@pytest.mark.asyncio
async def test_an_administrator_reads_the_organization_and_a_missing_caller_nothing():
    assert (await roi_access.reader_scope(None, user_id=USER, is_admin=True)).organization
    assert await roi_access.reader_scope(None, user_id=None, is_admin=False) == roi_access.EMPTY_SCOPE


@pytest.mark.asyncio
async def test_a_failed_scope_read_answers_nothing_and_never_raises():
    class _Broken:
        async def execute(self, *_a):
            raise RuntimeError("database gone")

    assert await roi_access.reader_scope(_Broken(), user_id=USER, is_admin=False) == roi_access.EMPTY_SCOPE
    assert not await roi_access.fleet_visible(_Broken(), user_id=USER, fleet_id=F1)
    assert not await roi_access.fleet_visible(None, user_id=None, fleet_id=F1)


# --- Every list adds up to its totals (round 2 B1) ---------------------------


def test_an_administrator_s_lists_add_up_with_a_no_fleet_row():
    report, *_ = _build()
    assert report.no_fleet is not None and report.no_fleet.runs == 2
    assert report.other_agents is None and report.other_fleets is None
    _assert_lists_add_up(report)


def test_a_non_administrator_s_platform_report_folds_the_rows_outside_scope():
    report, rows, figures_of = _build()
    before = roi_report.figures_dict(report.organization)
    reader = roi_access.RoiScope(agent_ids=frozenset({A1, A2}), fleet_ids=frozenset({F1}))
    roi_report.fold_outside(report, rows, reader, figures_of)
    assert {g.name for g in report.agents} == {"Threat relevance", "Vuln triage"}
    assert [g.name for g in report.fleets] == ["CTEM"]
    assert report.other_agents is not None and report.other_agents.runs == 2
    # F2's run and A3's fleetless run (no_fleet is folded too, Sam 15 Nov 2026).
    assert report.other_fleets is not None and report.other_fleets.runs == 2
    # Totals and by-unit figures untouched by the fold.
    assert roi_report.figures_dict(report.organization) == before
    _assert_lists_add_up(report)
    data = roi_report.report_dict(report)
    assert "Someone else's" not in {a["name"] for a in data["agents"]}
    assert "Other team" not in {f["name"] for f in data["fleets"]}
    assert data["other_agents"]["runs"] == 2


def test_the_no_fleet_row_of_a_non_administrator_holds_only_agents_in_scope():
    """Sam, 15 Nov 2026: "Agents outside a fleet" never carries a colleague's agent spend."""
    report, rows, figures_of = _build()
    assert report.no_fleet is not None and report.no_fleet.runs == 2  # A2 and A3, unfolded
    reader = roi_access.RoiScope(agent_ids=frozenset({A1, A2}), fleet_ids=frozenset({F1}))
    roi_report.fold_outside(report, rows, reader, figures_of)
    # Only A2's fleetless run stays; A3's goes to "Other agents and fleets".
    assert report.no_fleet is not None and report.no_fleet.runs == 1
    assert report.no_fleet.llm_cost == pytest.approx(0.2)
    assert report.other_fleets is not None and report.other_fleets.runs == 2
    _assert_lists_add_up(report)


def test_a_reader_with_no_fleetless_agent_has_no_no_fleet_row():
    report, rows, figures_of = _build()
    reader = roi_access.RoiScope(agent_ids=frozenset({A1}), fleet_ids=frozenset({F1}))
    roi_report.fold_outside(report, rows, reader, figures_of)
    assert report.no_fleet is None
    _assert_lists_add_up(report)
    assert roi_report.report_dict(report)["no_fleet"] is None


def test_an_organization_reader_is_never_folded():
    report, rows, figures_of = _build()
    no_fleet_before = roi_report.figures_dict(report.no_fleet)
    roi_report.fold_outside(report, rows, roi_access.RoiScope(organization=True), figures_of)
    assert len(report.agents) == 3 and report.other_agents is None
    assert roi_report.figures_dict(report.no_fleet) == no_fleet_before


def test_a_mine_report_s_lists_add_up():
    reader = roi_access.RoiScope(agent_ids=frozenset({A1}), fleet_ids=frozenset({F1}))
    mine = [r for r in _rows() if reader.holds_record(r.agent_id, r.fleet_id)]
    assert len(mine) == 2  # A1 in F1 counted once, A2 in F1 by its fleet
    report, *_ = _build(mine)
    assert report.organization.runs == 2
    _assert_lists_add_up(report)


# --- Figures unchanged for an administrator (zero regression) ----------------


def test_the_administrator_figures_are_unchanged_by_the_new_rows():
    report, *_ = _build()
    data = roi_report.report_dict(report)
    org = report.organization
    assert data["organization"]["net_value"] == org.net_value and data["organization"]["llm_cost"] == org.llm_cost
    assert data["cost_masked"] is False
    assert [a["id"] for a in data["agents"]] == [g.id for g in report.agents]


# --- Cost masking: null, never 0 --------------------------------------------


def test_masked_costs_are_null_never_zero():
    report, *_ = _build()
    report.cost_masked = True
    data = roi_report.report_dict(report)
    assert data["cost_masked"] is True
    for group in [data["organization"], data["no_fleet"], *data["agents"], *data["fleets"]]:
        for key in roi_report.COST_FIELDS:
            assert group[key] is None, key
        assert group["hours_saved"] is not None and group["value"] is not None
        total = [s for s in group["computation"] if s["kind"] == "total"]
        assert all(s["llm_cost"] is None and s["net_value"] is None for s in total)


# --- The routes ----------------------------------------------------------------


def test_the_settings_routes_read_for_everyone_and_write_for_administrators():
    def deps(fn):
        return [
            p.default.dependency for p in inspect.signature(fn).parameters.values() if hasattr(p.default, "dependency")
        ]

    assert get_current_user in deps(roi_api.get_roi_settings) and require_admin not in deps(roi_api.get_roi_settings)
    assert require_admin in deps(roi_api.put_roi_settings)
    assert "log_audit" in inspect.getsource(roi_api.put_roi_settings)


def _patch_report(monkeypatch, seen):
    async def _scope(_db, *, user_id, is_admin):
        seen["scope_of"] = (user_id, is_admin)
        return roi_access.RoiScope(agent_ids=frozenset({A1}))

    async def _load(_db, **kw):
        seen["load"] = kw
        return object()

    monkeypatch.setattr(roi_access, "reader_scope", _scope)
    monkeypatch.setattr(roi_api.roi_report, "load_report", _load)
    monkeypatch.setattr(roi_api.roi_report, "report_dict", lambda _r: {"ok": True})

    async def _same(answer: dict[str, object], *_a: object, **_kw: object) -> dict[str, object]:
        return answer

    monkeypatch.setattr(roi_api, "_with_ledger_figures", _same)


@pytest.mark.asyncio
async def test_every_user_reads_mine(monkeypatch):
    seen: dict = {}
    _patch_report(monkeypatch, seen)
    user = SimpleNamespace(id=USER, is_admin=False)
    assert await roi_api.get_roi_report(window="30d", currency=None, scope="mine", id=None, db=None, user=user) == {
        "ok": True
    }
    assert seen["scope_of"] == (USER, False)
    assert seen["load"]["scope"] == "mine" and seen["load"]["reader"].agent_ids == frozenset({A1})


@pytest.mark.asyncio
@pytest.mark.parametrize("visible", [True, False])
async def test_a_non_administrator_reads_the_platform_only_while_the_setting_is_on(monkeypatch, visible):
    seen: dict = {}
    _patch_report(monkeypatch, seen)

    async def _settings(_db):
        return SimpleNamespace(roi_org_totals_visible=visible)

    monkeypatch.setattr(roi_api, "get_platform_settings", _settings)
    user = SimpleNamespace(id=USER, is_admin=False)
    if visible:
        await roi_api.get_roi_report(window="30d", currency=None, scope="platform", id=None, db=None, user=user)
        assert seen["load"]["scope"] == "platform" and seen["load"]["reader"] is not None
    else:
        with pytest.raises(HTTPException) as refused:
            await roi_api.get_roi_report(window="30d", currency=None, scope="platform", id=None, db=None, user=user)
        assert refused.value.status_code == 403
        assert "load" not in seen


@pytest.mark.asyncio
async def test_an_administrator_s_platform_report_takes_no_reader(monkeypatch):
    seen: dict = {}
    _patch_report(monkeypatch, seen)
    user = SimpleNamespace(id=USER, is_admin=True)
    await roi_api.get_roi_report(window="30d", currency=None, scope="platform", id=None, db=None, user=user)
    assert "reader" not in seen["load"] and "scope_of" not in seen


@pytest.mark.asyncio
async def test_whoever_sees_a_fleet_reads_its_report_but_not_its_export(monkeypatch):
    async def _not_manager(*_a, **_kw):
        return False

    async def _sees(_db, *, user_id, fleet_id):
        return user_id == USER and fleet_id == F1

    monkeypatch.setattr(roi_access, "fleet_roi_reader", _not_manager)
    monkeypatch.setattr(roi_access, "fleet_visible", _sees)
    user = SimpleNamespace(id=USER, is_admin=False)
    await roi_api._require_roi_reader(None, user, fleet_id=F1, fleet_viewers=True)
    with pytest.raises(HTTPException):
        await roi_api._require_roi_reader(None, user, fleet_id=F1)
    with pytest.raises(HTTPException):
        await roi_api._require_roi_reader(None, user, fleet_id=F2, fleet_viewers=True)


@pytest.mark.asyncio
async def test_a_mine_report_needs_its_reader():
    with pytest.raises(ValueError, match="reader"):
        await roi_report.load_report(None, scope="mine")
