"""The CTEM threat package is validated through the analyst review gate (#1480).

- The view: a package whose page is its OpenCTI draft reads ``drafted``, its
  coverage ``coverage_drafted`` and never ``coverage_requested``; past the case
  SLA it is ``draft_overdue``. Once the Grouping page and the live ids are
  written (validated, new internal id included), ``coverage_requested``.
- The gate: ``ctem.analyst_review`` is a decision item, never a tool pause
  (#1675): it declares no tools and no grant, and every call runs.
- The tool: ``apply_opencti_draft`` is offered with Live writes off when the
  run's fleet has the analyst review gate, by its key (``gate_tool_names``).
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any, cast

import pytest

from app.agents.builtin_fleet_docs import _ctem_gates
from app.models.ctem_case import CtemCase, CtemCaseEvent
from app.services import ctem_case_loop as loop
from app.services import ctem_cases as rules
from app.services import ctem_cases_view as view_of
from app.services import ctem_threat_package_live as package_live
from app.services import fleet_autonomy

NOW = datetime(2026, 11, 23, 9, 0, tzinfo=UTC)
GROUPING = "8a3e2f10-4c5d-4b6e-9f70-1a2b3c4d5e6f"
LIVE_GROUPING = "1f2e3d4c-5b6a-4978-8a9b-0c1d2e3f4a5b"
DRAFT = "5d4c3b2a-1f0e-4d9c-8b7a-6f5e4d3c2b1a"
COVERAGE = "0c6f9d3a-2b1e-4f5a-8d7c-6e5f4a3b2c1d"
LIVE_COVERAGE = "9e8d7c6b-5a49-4382-a1b0-c9d8e7f6a5b4"
DRAFT_URL = f"https://cti.example.test/dashboard/data/import/draft/{DRAFT}"
LIVE_URL = f"https://cti.example.test/dashboard/analyses/groupings/{LIVE_GROUPING}"


class FakeSession:
    def __init__(self) -> None:
        self.added: list[object] = []

    def add(self, row: object) -> None:
        self.added.append(row)


def _events(db: FakeSession) -> list[CtemCaseEvent]:
    return [r for r in db.added if isinstance(r, CtemCaseEvent) and r.kind == "threat_package"]


def _record(db: FakeSession, case: CtemCase, package: dict[str, Any], at: datetime) -> None:
    previous = view_of.threat_package_of(_events(db))
    loop.record_threat_package(cast(Any, db), case, package, previous, actor="agent-1", now=at)


def _shown(
    events: list[CtemCaseEvent],
    view: view_of.ViewerView,
    *,
    sla_due_at: datetime | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    shown = view_of.threat_package_for(events, view, sla_due_at=sla_due_at, now=now)
    assert shown is not None
    return shown


def _drafted() -> dict[str, Any]:
    return {"grouping_id": GROUPING, "name": "CVE-2026-1234", "url": DRAFT_URL, "coverage_id": COVERAGE}


def test_drafted_package_never_reads_coverage_requested() -> None:
    db, case = FakeSession(), CtemCase(id=uuid.uuid4(), title="Upgrade nginx")
    _record(db, case, _drafted(), NOW)
    shown = _shown(_events(db), view_of.ViewerView(opencti=True), now=NOW)
    assert shown["state"] == "drafted"
    assert shown["coverage_requested"] is False
    assert shown["coverage_drafted"] is True
    assert shown["draft_overdue"] is False
    restricted = _shown(_events(db), view_of.ViewerView(opencti=False), now=NOW)
    assert (restricted["state"], restricted["coverage_requested"]) == ("drafted", False)


def test_drafted_package_past_the_case_sla_is_overdue() -> None:
    db, case = FakeSession(), CtemCase(id=uuid.uuid4(), title="Upgrade nginx")
    _record(db, case, _drafted(), NOW)
    shown = _shown(
        _events(db), view_of.ViewerView(opencti=True), sla_due_at=NOW + timedelta(hours=1), now=NOW + timedelta(hours=2)
    )
    assert shown["draft_overdue"] is True
    naive_due = (NOW - timedelta(hours=1)).replace(tzinfo=None)
    shown = _shown(_events(db), view_of.ViewerView(opencti=True), sla_due_at=naive_due, now=NOW)
    assert shown["draft_overdue"] is True


@pytest.mark.parametrize(
    ("grouping", "coverage"),
    [(GROUPING, COVERAGE), (LIVE_GROUPING, LIVE_COVERAGE), (f"grouping--{LIVE_GROUPING}", LIVE_COVERAGE)],
    ids=["same-ids", "new-internal-ids", "standard-id"],
)
def test_validated_package_switches_to_the_live_grouping(grouping: str, coverage: str) -> None:
    db, case = FakeSession(), CtemCase(id=uuid.uuid4(), title="Upgrade nginx")
    _record(db, case, _drafted(), NOW)
    page = f"https://cti.example.test/dashboard/analyses/groupings/{grouping.removeprefix('grouping--')}"
    _record(
        db,
        case,
        {**_drafted(), "grouping_id": grouping, "url": page, "coverage_id": coverage},
        NOW + timedelta(minutes=5),
    )
    assert len(_events(db)) == 2
    shown = _shown(
        _events(db), view_of.ViewerView(opencti=True), sla_due_at=NOW - timedelta(days=1), now=NOW + timedelta(hours=1)
    )
    assert (shown["grouping_id"], shown["url"], shown["coverage_id"]) == (grouping, page, coverage)
    assert shown["state"] == "validated"
    assert shown["coverage_requested"] is True
    assert shown["coverage_drafted"] is False
    assert shown["draft_overdue"] is False


def test_validated_package_without_coverage_is_not_requested() -> None:
    db, case = FakeSession(), CtemCase(id=uuid.uuid4(), title="Upgrade nginx")
    _record(db, case, {**_drafted(), "url": LIVE_URL, "grouping_id": LIVE_GROUPING, "coverage_id": ""}, NOW)
    shown = _shown(_events(db), view_of.ViewerView(opencti=True), now=NOW)
    assert (shown["state"], shown["coverage_requested"], shown["coverage_drafted"]) == ("validated", False, False)


def _review_gate() -> dict[str, Any]:
    gates = _ctem_gates(launch_tools=True, mobilize_action="mobilize")
    return next(g for g in gates if g["key"] == "ctem.analyst_review")


def test_analyst_review_gate_offers_the_draft_validation_by_its_key() -> None:
    gate = _review_gate()
    assert "tools" not in gate and "grant" not in gate
    assert "publishes the package and sends the coverage to OpenAEV" in gate["action"]
    assert "apply_opencti_draft" in fleet_autonomy.gate_tool_names([gate])


def _call(call_id: str, name: str, arguments: dict[str, Any]) -> SimpleNamespace:
    return SimpleNamespace(id=call_id, name=name, arguments=arguments)


def test_the_review_gate_never_pauses_the_request_or_the_draft_validation() -> None:
    """#1675: pinned wait, full human gates, the calls still run; the wait is a decision item."""
    gate = {**_review_gate(), "pin": fleet_autonomy.PIN_WAIT}
    check = fleet_autonomy.make_fleet_autonomy_gate(None, fleet_autonomy.gated_tool_rules("full_gates", [gate]))
    check(_call("c1", "manage_opencti_security_coverage", {"action": "create", "covered_id": GROUPING}))
    check(_call("c2", "apply_opencti_draft", {"draft_id": DRAFT}))


def _tool_names(**kwargs: Any) -> set[str]:
    from app.integrations.providers.opencti.knowledge import register_knowledge
    from app.tools.base import CallableToolExecutor

    defs = register_knowledge("https://cti.example.test", {}, CallableToolExecutor(), autonomous=True, **kwargs)
    return {d.name for d in defs}


def test_apply_draft_is_offered_without_live_writes_only_when_a_fleet_gate_decides_it() -> None:
    assert "apply_opencti_draft" not in _tool_names()
    assert "apply_opencti_draft" in _tool_names(gated_draft_validation=True)
    assert "create_opencti_label" not in _tool_names(gated_draft_validation=True)
    assert "apply_opencti_draft" in _tool_names(live_writes=True)


# #1547: the state is not inferred from the text of the link alone.

GROUPING_URL = f"https://cti.example.test/dashboard/analyses/groupings/{GROUPING}"
OPEN_DRAFT = "7c6b5a49-3829-4176-a5b4-c3d2e1f0a9b8"


def test_package_with_no_page_is_not_proven_live() -> None:
    db, case = FakeSession(), CtemCase(id=uuid.uuid4(), title="Upgrade nginx")
    _record(db, case, {"grouping_id": GROUPING, "coverage_id": COVERAGE}, NOW)
    shown = _shown(_events(db), view_of.ViewerView(opencti=True), now=NOW)
    assert (shown["state"], shown["coverage_requested"], shown["coverage_drafted"]) == ("drafted", False, True)


def _fake_opencti(
    monkeypatch: pytest.MonkeyPatch, *, live: set[str], drafts: dict[str, set[str]], fail: bool = False
) -> list[tuple[str, str]]:
    """OpenCTI as seen by the user: *live* Groupings, and the Groupings inside each open draft."""
    sent: list[tuple[str, str]] = []

    async def creds(db: Any, user_id: str, provider: str) -> tuple[str, dict[str, str]]:
        assert (user_id, provider) == ("u-1", "opencti")
        return "https://cti.example.test", {"Authorization": "Bearer user-jwt"}

    async def graphql(
        client: Any, base_url: str, headers: Any, query: str, variables: Any = None, **_kw: Any
    ) -> tuple[dict[str, Any], str | None]:
        draft = headers.get(package_live._DRAFT_HEADER, "")
        if fail:
            return {}, "OpenCTI unreachable"
        if "draftWorkspaces" in query:
            sent.append(("drafts", ""))
            return {"draftWorkspaces": {"edges": [{"node": {"id": d}} for d in drafts]}}, None
        sent.append(("grouping", draft))
        gid = variables["id"]
        found = (gid in drafts.get(draft, set())) if draft else (gid in live)
        return {"grouping": {"id": gid} if found else None}, None

    monkeypatch.setattr(package_live, "_user_creds", creds)
    monkeypatch.setattr(package_live, "_graphql", graphql)
    monkeypatch.setattr(package_live, "attach_probe", lambda headers, base_url: headers)
    return sent


@pytest.mark.asyncio
async def test_grouping_page_of_a_grouping_still_in_an_open_draft_is_recorded_drafted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The acceptance case of #1547: Grouping page given, Grouping only in an open draft."""
    sent = _fake_opencti(monkeypatch, live=set(), drafts={OPEN_DRAFT: {GROUPING}})
    package = {"grouping_id": GROUPING, "name": "CVE-2026-1234", "url": GROUPING_URL, "coverage_id": COVERAGE}
    checked = await package_live.verify_package(cast(Any, None), "u-1", package)
    assert checked["url"] == f"https://cti.example.test/dashboard/data/import/draft/{OPEN_DRAFT}"
    assert sent == [("grouping", ""), ("drafts", ""), ("grouping", OPEN_DRAFT)]
    db, case = FakeSession(), CtemCase(id=uuid.uuid4(), title="Upgrade nginx")
    _record(db, case, checked, NOW)
    shown = _shown(_events(db), view_of.ViewerView(opencti=True), now=NOW)
    assert (shown["state"], shown["coverage_requested"], shown["coverage_drafted"]) == ("drafted", False, True)


@pytest.mark.asyncio
async def test_grouping_page_of_a_live_grouping_is_kept_and_reads_validated(monkeypatch: pytest.MonkeyPatch) -> None:
    sent = _fake_opencti(monkeypatch, live={GROUPING}, drafts={OPEN_DRAFT: set()})
    package = {"grouping_id": GROUPING, "url": GROUPING_URL, "coverage_id": COVERAGE}
    checked = await package_live.verify_package(cast(Any, None), "u-1", package)
    assert checked == package and sent == [("grouping", "")]
    db, case = FakeSession(), CtemCase(id=uuid.uuid4(), title="Upgrade nginx")
    _record(db, case, checked, NOW)
    shown = _shown(_events(db), view_of.ViewerView(opencti=True), now=NOW)
    assert (shown["state"], shown["coverage_requested"]) == ("validated", True)


@pytest.mark.asyncio
@pytest.mark.parametrize("fail", [False, True], ids=["absent", "unreadable"])
async def test_grouping_page_not_placed_live_is_refused(monkeypatch: pytest.MonkeyPatch, fail: bool) -> None:
    _fake_opencti(monkeypatch, live=set(), drafts={OPEN_DRAFT: set()}, fail=fail)
    with pytest.raises(rules.CaseRuleError, match="draft page"):
        await package_live.verify_package(cast(Any, None), "u-1", {"grouping_id": GROUPING, "url": GROUPING_URL})


@pytest.mark.asyncio
async def test_draft_page_and_no_page_are_not_read_in_opencti(monkeypatch: pytest.MonkeyPatch) -> None:
    sent = _fake_opencti(monkeypatch, live=set(), drafts={})
    for package in ({"grouping_id": GROUPING, "url": DRAFT_URL}, {"grouping_id": GROUPING}, "not a package"):
        assert await package_live.verify_package(cast(Any, None), "u-1", package) == package
    assert sent == []


class _Rows:
    def __init__(self, rows: list[CtemCaseEvent]) -> None:
        self._rows = rows

    def scalars(self) -> list[CtemCaseEvent]:
        return self._rows


class _RepairSession(FakeSession):
    def __init__(self, rows: list[CtemCaseEvent]) -> None:
        super().__init__()
        self.rows = rows

    async def execute(self, _query: Any) -> _Rows:
        return _Rows(self.rows)


def _stored(case_id: uuid.UUID, grouping: str, url: str | None, at: datetime) -> CtemCaseEvent:
    return CtemCaseEvent(
        case_id=case_id,
        actor="a",
        kind="threat_package",
        at=at,
        to_value=grouping,
        reason="pkg",
        evidence=url,
        from_value=COVERAGE,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("dry_run", [True, False])
async def test_repair_corrects_stored_grouping_pages_not_in_the_live_graph(
    monkeypatch: pytest.MonkeyPatch, dry_run: bool
) -> None:
    live_id, absent_id = LIVE_GROUPING, "2a3b4c5d-6e7f-4809-9a1b-2c3d4e5f6a7b"
    _fake_opencti(monkeypatch, live={live_id}, drafts={OPEN_DRAFT: {GROUPING}})
    in_draft, live_case, absent_case, draft_case = (uuid.uuid4() for _ in range(4))
    page = "https://cti.example.test/dashboard/analyses/groupings/{}".format
    rows = [
        _stored(in_draft, GROUPING, DRAFT_URL, NOW - timedelta(hours=2)),
        _stored(in_draft, GROUPING, page(GROUPING), NOW - timedelta(hours=1)),
        _stored(live_case, live_id, page(live_id), NOW),
        _stored(absent_case, absent_id, page(absent_id), NOW),
        _stored(draft_case, GROUPING, DRAFT_URL, NOW),
    ]
    db = _RepairSession(rows)
    answer = await package_live.repair_stored_packages(
        cast(Any, db), uuid.uuid4(), "u-1", dry_run=dry_run, now=NOW + timedelta(hours=1)
    )
    assert (answer["checked"], answer["corrected"], answer["unreadable"], answer["dry_run"]) == (3, 2, 0, dry_run)
    fixed = {c["case_id"]: c["url"] for c in answer["cases"]}
    assert fixed == {
        str(in_draft): f"https://cti.example.test/dashboard/data/import/draft/{OPEN_DRAFT}",
        str(absent_case): None,
    }
    written = _events(db)
    if dry_run:
        assert written == []
        return
    assert {(e.case_id, e.evidence, e.from_value, e.actor) for e in written} == {
        (in_draft, fixed[str(in_draft)], COVERAGE, rules.SYSTEM_ACTOR),
        (absent_case, None, COVERAGE, rules.SYSTEM_ACTOR),
    }
    corrected = _shown(rows[:2] + [w for w in written if w.case_id == in_draft], view_of.ViewerView(opencti=True))
    assert (corrected["state"], corrected["coverage_requested"]) == ("drafted", False)


# #1573: the state follows the analyst review gate, not the page alone.


def _gate(case_id: uuid.UUID, outcome: str, at: datetime, *, option: str = "", by: str = "") -> CtemCaseEvent:
    return CtemCaseEvent(
        case_id=case_id,
        actor="agent-1",
        kind="gate",
        at=at,
        from_value="ctem.analyst_review",
        to_value=outcome,
        reason=by or None,
        evidence=option or None,
    )


def _live_package(case_id: uuid.UUID, at: datetime) -> CtemCaseEvent:
    return _stored(case_id, GROUPING, GROUPING_URL, at)


@pytest.mark.parametrize(
    ("gates", "state", "requested"),
    [
        ([("waited", "", "")], "awaiting_review", False),
        (
            [("waited", "", ""), ("answered", "Hold it, re-scope to the internet-facing hosts first", "u")],
            "replan",
            False,
        ),
        ([("waited", "", ""), ("answered", "Reject", "u")], "rejected", False),
        ([("waited", "", ""), ("answered", "Approve", "u")], "validated", True),
        ([("waited", "", ""), ("answered", "", "OpenCTI analyst mark")], "validated", True),
        ([("passed_automatically", "", "full_autonomous")], "validated", True),
        ([("waited", "", ""), ("answered", "", "u")], "validated", True),
        ([], "validated", True),
    ],
    ids=["waiting", "free-text", "refused", "approved", "mark", "policy", "answer-unread", "no-gate"],
)
def test_grouping_page_reads_validated_only_when_the_review_approved(
    gates: list[tuple[str, str, str]], state: str, requested: bool
) -> None:
    case_id = uuid.uuid4()
    events = [_gate(case_id, o, NOW + timedelta(minutes=i), option=opt, by=by) for i, (o, opt, by) in enumerate(gates)]
    events.append(_live_package(case_id, NOW + timedelta(minutes=10)))
    for view in (view_of.ViewerView(opencti=True), view_of.ViewerView(opencti=False)):
        shown = _shown(events, view, now=NOW + timedelta(hours=1))
        assert (shown["state"], shown["coverage_requested"]) == (state, requested)


def test_replanned_package_is_neither_drafted_coverage_nor_overdue() -> None:
    case_id = uuid.uuid4()
    events = [
        _gate(case_id, "waited", NOW),
        _gate(case_id, "answered", NOW + timedelta(minutes=1), option="Rework the package"),
        _stored(case_id, GROUPING, DRAFT_URL, NOW + timedelta(minutes=2)),
    ]
    shown = _shown(events, view_of.ViewerView(opencti=True), sla_due_at=NOW, now=NOW + timedelta(hours=1))
    assert (shown["state"], shown["coverage_drafted"], shown["draft_overdue"]) == ("replan", False, False)


def test_waiting_review_past_the_sla_is_overdue() -> None:
    case_id = uuid.uuid4()
    events = [_gate(case_id, "waited", NOW), _stored(case_id, GROUPING, DRAFT_URL, NOW)]
    shown = _shown(events, view_of.ViewerView(opencti=True), sla_due_at=NOW, now=NOW + timedelta(hours=1))
    assert (shown["state"], shown["coverage_drafted"], shown["draft_overdue"]) == ("awaiting_review", True, True)


def test_newest_review_gate_decides() -> None:
    case_id = uuid.uuid4()
    events = [
        _gate(case_id, "answered", NOW, option="Rework the package"),
        _gate(case_id, "waited", NOW + timedelta(minutes=1)),
        _gate(case_id, "answered", NOW + timedelta(minutes=2), option="Approve and request coverage"),
    ]
    assert view_of.review_outcome(events) == view_of.REVIEW_APPROVED
    assert view_of.review_outcome(list(reversed(events))) == view_of.REVIEW_APPROVED
    assert view_of.review_outcome([]) is None


@pytest.mark.parametrize(
    ("option", "outcome"),
    [
        ("Approve", "approved"),
        ("Approve and request coverage", "approved"),
        ("approved.", "approved"),
        ("Reject", "rejected"),
        ("Decline the package", "rejected"),
        ("Go back and re-scope to the internet-facing hosts", "replan"),
        ("Yes, but split the package first", "replan"),
        ("No problem, publish it", "replan"),
        ("Approve, but split the package first", "replan"),
        ("yes", "replan"),
        ("no", "replan"),
    ],
)
def test_review_option_is_matched_on_the_whole_answer(option: str, outcome: str) -> None:
    case_id = uuid.uuid4()
    events = [_gate(case_id, "waited", NOW), _gate(case_id, "answered", NOW + timedelta(minutes=1), option=option)]
    assert view_of.review_outcome(events) == outcome


@pytest.mark.asyncio
@pytest.mark.parametrize("review", ["waiting", "replan", "rejected"])
async def test_grouping_page_of_a_not_approved_review_is_recorded_as_its_draft(
    monkeypatch: pytest.MonkeyPatch, review: str
) -> None:
    """The Grouping resolves live AND sits in an open draft: the draft wins while the review is not approved."""
    _fake_opencti(monkeypatch, live={GROUPING}, drafts={OPEN_DRAFT: {GROUPING}})
    package = {"grouping_id": GROUPING, "url": GROUPING_URL, "coverage_id": COVERAGE}
    checked = await package_live.verify_package(cast(Any, None), "u-1", package, review)
    assert checked["url"] == f"https://cti.example.test/dashboard/data/import/draft/{OPEN_DRAFT}"
    approved = await package_live.verify_package(cast(Any, None), "u-1", package, "approved")
    assert approved == package


@pytest.mark.asyncio
async def test_live_grouping_page_of_a_refused_review_with_no_draft_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    _fake_opencti(monkeypatch, live={GROUPING}, drafts={OPEN_DRAFT: set()})
    with pytest.raises(rules.CaseRuleError, match="did not approve"):
        await package_live.verify_package(
            cast(Any, None), "u-1", {"grouping_id": GROUPING, "url": GROUPING_URL}, "replan"
        )


@pytest.mark.asyncio
async def test_repair_corrects_grouping_pages_of_cases_whose_review_did_not_approve(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    other = "3b4c5d6e-7f80-4911-a2b3-c4d5e6f7a8b9"
    _fake_opencti(monkeypatch, live={GROUPING, other}, drafts={OPEN_DRAFT: {GROUPING}})
    replan, refused, approved = (uuid.uuid4() for _ in range(3))
    page = "https://cti.example.test/dashboard/analyses/groupings/{}".format
    rows = [
        _gate(replan, "answered", NOW, option="Rework the package"),
        _stored(replan, GROUPING, page(GROUPING), NOW + timedelta(minutes=1)),
        _gate(refused, "answered", NOW, option="Reject"),
        _stored(refused, other, page(other), NOW + timedelta(minutes=1)),
        _gate(approved, "answered", NOW, option="Approve"),
        _stored(approved, other, page(other), NOW + timedelta(minutes=1)),
    ]
    db = _RepairSession(rows)
    answer = await package_live.repair_stored_packages(cast(Any, db), uuid.uuid4(), "u-1", dry_run=True)
    assert (answer["checked"], answer["corrected"], answer["unreadable"]) == (3, 2, 0)
    fixed = {c["case_id"]: (c["url"], c["review"]) for c in answer["cases"]}
    assert fixed == {
        str(replan): (f"https://cti.example.test/dashboard/data/import/draft/{OPEN_DRAFT}", "replan"),
        str(refused): (None, "rejected"),
    }
    assert _events(db) == []
