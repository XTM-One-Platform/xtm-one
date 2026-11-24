"""CTEM cases opened from signals, updated on events, closed on proof (#857).

Rules of ``services/ctem_case_loop.py`` and the proof rule of
``services/ctem_cases.check_transition``, with a fake session: every
transition, a case opened from a signal with no CVE, a validation result that
updates a case without a recompute, a resolve refused without proof, an SLA
breach raised once.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import TYPE_CHECKING, cast

import pytest

from app.models.ctem_case import CtemCase, CtemCaseEvent, CtemCaseIssue
from app.services import ctem_case_loop as loop
from app.services import ctem_cases as rules

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

NOW = datetime(2026, 11, 21, 10, 0, tzinfo=UTC)
ORG = uuid.uuid4()


class FakeSession:
    """Collects the rows a step adds; a case lookup answers the cases given."""

    def __init__(self, open_cases: list[CtemCase] | None = None):
        self.added: list[object] = []
        self.open_cases = open_cases or []

    def add(self, row: object) -> None:
        self.added.append(row)

    async def flush(self) -> None:
        return None

    async def execute(self, _statement):
        cases = self.open_cases
        return SimpleNamespace(scalars=lambda: iter(cases))

    def events(self, kind: str) -> list[CtemCaseEvent]:
        return [r for r in self.added if isinstance(r, CtemCaseEvent) and r.kind == kind]

    def begin_nested(self):
        return _Savepoint()


class _Savepoint:
    """A savepoint that lets the error through, as the real one does after its rollback."""

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


class _Rows(list):
    def all(self) -> list:
        return list(self)


class SweepSession(FakeSession):
    """The sweep's reads in order: the waiting signals, each case lookup, then the open cases past their SLA."""

    def __init__(self, *reads: list):
        super().__init__()
        self.reads = [_Rows(r) for r in reads]

    async def execute(self, _statement):
        rows = self.reads.pop(0) if self.reads else _Rows()
        return SimpleNamespace(scalars=lambda: rows)


class _ExpiringSignal(SimpleNamespace):
    """A signal whose attributes fail once expired, as a lazy refresh on an AsyncSession raises MissingGreenlet."""

    expired = False

    def __getattribute__(self, name):
        if name != "expired" and not name.startswith("__") and object.__getattribute__(self, "expired"):
            raise RuntimeError("MissingGreenlet: greenlet_spawn has not been called")
        return object.__getattribute__(self, name)


def _signal(**kw) -> SimpleNamespace:
    base = dict(
        id=uuid.uuid4(),
        organization_id=ORG,
        trigger_path="new_exposure",
        lens="outside",
        subject_kind="asset",
        subject_ref="admin.example.com",
        subject_label="admin.example.com",
        state="confirmed",
        case_id=None,
        updated_at=None,
        decision={
            "outcome": "case",
            "state": "confirmed",
            "severity": "P1",
            "sla_hours": 24,
            "scope": {
                "criticality": "critical",
                "risk_scenarios": [{"id": "rs-1", "name": "Data theft"}],
                "sources": [{"from": "crown_jewel", "name": "Customer SaaS hosting", "criticality": "critical"}],
            },
        },
    )
    base.update(kw)
    return SimpleNamespace(**base)


def _case(**kw) -> CtemCase:
    base = dict(
        id=uuid.uuid4(),
        organization_id=ORG,
        vertical="cyber",
        title="Close the exposure of admin.example.com",
        remediation_kind="close_exposure",
        keys=["close_exposure:asset/admin.example.com"],
        pinned_issues=[],
        state="new",
        state_since=NOW,
        trigger_path="new_exposure",
        risk_scenario_ids=[],
        severity="P2",
        sla_due_at=NOW + timedelta(hours=72),
        sla_breached_at=None,
        tickets=[],
        validations=[],
        validation_plan=False,
        resolution_kind=None,
    )
    base.update(kw)
    return CtemCase(**base)


# --- Opening from a signal ---------------------------------------------------


@pytest.mark.asyncio
async def test_signal_with_no_cve_opens_a_case_with_severity_sla_and_scenarios():
    db = FakeSession()
    events = loop.PendingEvents()
    signal = _signal()
    case = await loop.open_from_signal(db, signal, events=events, now=NOW)
    assert case is not None
    assert case.remediation_kind == "close_exposure"
    assert case.trigger_path == "new_exposure"
    assert case.severity == "P1"
    assert case.sla_due_at == NOW + timedelta(hours=24)
    assert case.risk_scenario_ids == ["rs-1"]
    assert signal.state == "case_opened" and signal.case_id == case.id
    members = [r for r in db.added if isinstance(r, CtemCaseIssue)]
    assert [(m.source, m.external_id) for m in members] == [("signal", str(signal.id))]
    assert [name for name, _, _ in events.items] == [loop.CASE_OPENED]
    payload = events.items[0][1]
    assert "title" not in payload and "signal_id" not in payload and payload["case_id"] == str(case.id)
    assert "severity" not in payload and "risk_scenario_ids" not in payload  # read on the case (#933)


@pytest.mark.asyncio
async def test_second_signal_on_the_same_fix_joins_the_open_case():
    existing = _case(severity="P3", sla_due_at=NOW + timedelta(days=30))
    db = FakeSession(open_cases=[existing])
    events = loop.PendingEvents()
    case = await loop.open_from_signal(db, _signal(), events=events, now=NOW)
    assert case is existing
    assert case.severity == "P1"  # the most urgent wins
    assert case.sla_due_at == NOW + timedelta(hours=24)  # the earliest SLA wins
    assert events.items == []  # joining raises no opened event
    assert events.verticals == {"cyber"}  # but the views still go: the case has a new member


@pytest.mark.asyncio
async def test_opening_or_joining_a_case_drops_every_cached_view_after_the_commit(monkeypatch):
    """#915: a case opened from a signal shows on the board at once, not after the view TTL."""
    dropped: list[str] = []
    published: list[str] = []

    async def forget_all(vertical):
        dropped.append(vertical)

    async def publish(name, data, *, event_id=None):
        published.append(name)
        return True

    monkeypatch.setattr("app.services.ctem_cases_view.forget_all_views", forget_all)
    monkeypatch.setattr("app.services.platform_events.publish", publish)
    events = loop.PendingEvents()
    await loop.open_from_signal(FakeSession(), _signal(), events=events, now=NOW)
    assert await loop.publish_pending(events) == 1
    assert dropped == ["cyber"] and published == [loop.CASE_OPENED]
    assert events.verticals == set() and events.items == []
    # A signal that opens nothing drops nothing.
    idle = loop.PendingEvents()
    await loop.open_from_signal(
        FakeSession(), _signal(state="watchlist", decision={"outcome": "watchlist"}), events=idle
    )
    assert await loop.publish_pending(idle) == 0
    assert dropped == ["cyber"]


@pytest.mark.parametrize(
    "state,outcome,case_id",
    [("watchlist", "watchlist", None), ("to_validate", "to_validate", None), ("confirmed", "case", uuid.uuid4())],
)
@pytest.mark.asyncio
async def test_a_signal_that_is_not_a_new_case_opens_nothing(state, outcome, case_id):
    signal = _signal(state=state, case_id=case_id, decision={"outcome": outcome})
    assert await loop.open_from_signal(FakeSession(), signal, now=NOW) is None


# --- Installed versions read at opening, before the threat package --------------


def _upgrade_signal() -> SimpleNamespace:
    evidence = {"affected_package": "lodash", "fixed_version": "4.17.21"}
    return _signal(trigger_path="code_flaw", subject_kind="code", subject_ref="lodash", evidence=evidence)


@pytest.mark.asyncio
async def test_an_upgrade_case_every_asset_already_runs_the_fix_of_resolves_at_opening(monkeypatch):
    """The inventory is read when the case opens: no loop, no review, no gate for a case nobody is exposed to."""

    async def on_fixed(_db, _case):
        return {"fixed_version": "4.17.21", "assets_on_fixed": 3, "assets_below_fixed": 0, "assets_not_comparable": 0}

    monkeypatch.setattr("app.services.ctem_case_versions.for_case", on_fixed)
    db = FakeSession()
    events = loop.PendingEvents()
    case = await loop.open_from_signal(db, _upgrade_signal(), events=events, now=NOW)
    assert case is not None and case.state == "resolved" and case.resolution_kind == "fixed"
    [state] = db.events("state")
    assert state.from_value == "new" and state.to_value == "resolved"
    assert state.evidence.startswith(rules.FIXED_IN_INVENTORY) and "3 asset(s) on 4.17.21" in state.evidence
    assert [name for name, _, _ in events.items] == [loop.CASE_RESOLVED]  # never opened: the loop does not start


@pytest.mark.parametrize(
    "running",
    [
        None,  # no asset reports the product: unknown, never discarded
        {"fixed_version": "4.17.21", "assets_on_fixed": 2, "assets_below_fixed": 1, "assets_not_comparable": 0},
        {"fixed_version": "4.17.21", "assets_on_fixed": 2, "assets_below_fixed": 0, "assets_not_comparable": 1},
        {"fixed_version": "4.17.21", "assets_on_fixed": 2, "platform_unverified": True},
    ],
)
@pytest.mark.asyncio
async def test_an_upgrade_case_with_an_asset_below_or_unknown_opens_and_starts_the_loop(monkeypatch, running):
    async def read(_db, _case):
        return running

    monkeypatch.setattr("app.services.ctem_case_versions.for_case", read)
    db = FakeSession()
    events = loop.PendingEvents()
    case = await loop.open_from_signal(db, _upgrade_signal(), events=events, now=NOW)
    assert case is not None and case.state == "new" and case.resolution_kind is None
    assert db.events("state") == []
    assert [name for name, _, _ in events.items] == [loop.CASE_OPENED]


@pytest.mark.asyncio
async def test_a_case_that_needs_its_validation_proof_is_not_resolved_on_the_inventory(monkeypatch):
    async def on_fixed(_db, _case):
        raise AssertionError("the inventory is not read for a case that needs its proof")

    monkeypatch.setattr("app.services.ctem_case_versions.for_case", on_fixed)
    signal = _upgrade_signal()
    signal.trigger_path = "proven_exploitable"  # its case carries a validation plan
    case = await loop.open_from_signal(FakeSession(), signal, now=NOW)
    assert case is not None and case.validation_plan and case.fixed_version == "4.17.21" and case.state == "new"


@pytest.mark.parametrize(
    "path,kind",
    [
        ("stale_asset", "decommission"),
        ("credential_leak", "rotate_credential"),
        ("third_party_event", "supplier_action"),
        ("blind_spot", "restore_coverage"),
        ("mitigation_regression", "restore_control"),
    ],
)
def test_paths_with_no_cve_map_to_their_remediation_kind(path, kind):
    assert loop.fix_of(_signal(trigger_path=path)).kind == kind


def test_a_software_subject_on_a_vulnerability_path_is_an_upgrade():
    fix = loop.fix_of(_signal(trigger_path="new_vulnerability", subject_kind="software", subject_ref="NGINX"))
    assert fix.kind == "upgrade" and fix.key == "upgrade:-/nginx@latest"


def test_a_dependency_flaw_with_a_fixed_version_is_an_upgrade_to_that_version():
    """CTEM-25: an Aikido dependency issue names the package and its fix; a second issue on it joins the case."""
    evidence = {"affected_package": "lodash", "fixed_version": "4.17.21"}
    fix = loop.fix_of(_signal(trigger_path="code_flaw", subject_kind="code", subject_ref="lodash", evidence=evidence))
    assert fix.kind == "upgrade" and fix.key == "upgrade:-/lodash@4.17.21"
    assert fix.title == "Upgrade lodash to 4.17.21" and fix.fixed_version == "4.17.21"
    other = loop.fix_of(_signal(trigger_path="code_flaw", subject_kind="code", subject_ref="x", evidence=evidence))
    assert other.key == fix.key
    plain = loop.fix_of(_signal(trigger_path="code_flaw", subject_kind="code", subject_ref="XSS in login"))
    assert plain.kind == "patch" and plain.fixed_version is None
    leak = loop.fix_of(
        _signal(trigger_path="credential_leak", subject_kind="code", subject_ref=".env", evidence=evidence)
    )
    assert leak.kind == "rotate_credential"


def test_a_dependabot_alert_is_an_upgrade_in_its_repository():
    """#1331: the same package and fix in two repositories are two cases, each named after its repository."""
    evidence = {"affected_package": "lodash", "fixed_version": "4.17.21", "repository": "Filigran/Portal"}
    fix = loop.fix_of(_signal(trigger_path="code_flaw", subject_kind="code", subject_ref="lodash", evidence=evidence))
    assert fix.kind == "upgrade" and fix.key == "upgrade:filigran/portal/lodash@4.17.21"
    assert fix.title == "Upgrade lodash to 4.17.21 in Filigran/Portal" and fix.fixed_version == "4.17.21"
    other = loop.fix_of(
        _signal(
            trigger_path="code_flaw",
            subject_kind="code",
            subject_ref="lodash",
            evidence={**evidence, "repository": "Filigran/Docs"},
        )
    )
    assert other.key != fix.key


class CloseSession(FakeSession):
    """The reads of ``close_signal_members`` in order: the signals, then per signal its member and what stays open."""

    def __init__(self, signals: list, members: list, still_open: list, case: CtemCase):
        super().__init__()
        self.reads = [_Rows(signals)]
        for member, left in zip(members, still_open, strict=True):
            self.reads.append(member)
            self.reads.append(left)
        self.case = case

    async def execute(self, _statement):
        value = self.reads.pop(0)
        if isinstance(value, _Rows):
            return SimpleNamespace(scalars=lambda: value)
        return SimpleNamespace(scalar_one_or_none=lambda: value, first=lambda: value)

    async def get(self, _model, _id):
        return self.case


@pytest.mark.asyncio
async def test_closed_at_source_closes_the_member_and_the_last_one_resolves_on_a_rescan():
    case = _case(trigger_path="code_flaw", remediation_kind="upgrade")
    signal = _signal(case_id=case.id, trigger_path="code_flaw", state="case_opened")
    member = CtemCaseIssue(case_id=case.id, source="signal", external_id=str(signal.id), open=True)
    db = CloseSession([signal], [member], [None], case)
    events = loop.PendingEvents()
    closed = await loop.close_signal_members(
        db, organization_id=ORG, provider="aikido", external_id="42", events=events, now=NOW
    )
    assert closed == 1 and member.open is False
    assert db.events("issue_closed")[0].issue == f"signal:{signal.id}"
    assert case.state == "resolved" and case.resolution_kind == "fixed"
    assert case.validations[-1]["kind"] == loop.RESCAN_KIND and case.validations[-1]["provider"] == "aikido"
    assert [name for name, _, _ in events.items] == [loop.CASE_RESOLVED]


@pytest.mark.asyncio
async def test_closed_at_source_keeps_the_case_open_while_other_members_are():
    case = _case(trigger_path="code_flaw")
    signal = _signal(case_id=case.id, trigger_path="code_flaw", state="case_opened")
    member = CtemCaseIssue(case_id=case.id, source="signal", external_id=str(signal.id), open=True)
    db = CloseSession([signal], [member], [(uuid.uuid4(),)], case)
    closed = await loop.close_signal_members(db, organization_id=ORG, provider="aikido", external_id="42", now=NOW)
    assert closed == 1 and case.state == "new" and case.validations == []
    assert await loop.close_signal_members(db, organization_id=ORG, provider="aikido", external_id="") == 0


@pytest.mark.parametrize("path,kind", [("new_exposure", "asset"), ("new_vulnerability", "software")])
def test_a_long_subject_keeps_the_key_and_title_within_their_columns(path, kind):
    long_ref = "https://cdn.example.com/" + "a" * 400
    fix = loop.fix_of(_signal(trigger_path=path, subject_kind=kind, subject_ref=long_ref, subject_label=long_ref))
    assert len(fix.key) <= loop.COLUMN_MAX and len(fix.title) <= loop.COLUMN_MAX
    other = loop.fix_of(
        _signal(trigger_path=path, subject_kind=kind, subject_ref=long_ref + "b", subject_label=long_ref)
    )
    assert other.key != fix.key  # two long subjects never share a case
    short = loop.fix_of(_signal(trigger_path=path, subject_kind=kind, subject_ref="nginx"))
    assert "#" not in short.key  # a key within the column is unchanged


@pytest.mark.asyncio
async def test_the_sweep_skips_a_signal_it_cannot_open_and_goes_on(monkeypatch):
    bad, good = _signal(), _signal(subject_ref="vpn.example.com", subject_label="vpn.example.com")
    late = _case(sla_due_at=NOW - timedelta(hours=1))
    bad = _ExpiringSignal(**vars(bad))
    # signals, the good signal's case lookup, its owner lookup (#1736 slice 3), late cases
    db = SweepSession([bad, good], [], [], [late])
    real = loop.open_from_signal

    async def open_or_fail(session, signal, **kw):
        if signal is bad:
            kw["events"].add(loop.CASE_OPENED, {}, "half-written")
            # The real savepoint rollback expires the rows changed inside it, the signal included.
            signal.expired = True
            raise RuntimeError("value too long for type character varying(300)")
        return await real(session, signal, **kw)

    monkeypatch.setattr(loop, "open_from_signal", open_or_fail)
    events = loop.PendingEvents()
    counts = await loop.sweep(db, events=events, now=NOW)
    assert counts == {"opened": 1, "joined": 0, "skipped": 1, "sla_breached": 1, "announced": 0, "stalled": 0}
    bad.expired = False
    assert bad.state == "confirmed" and good.state == "case_opened"
    assert [name for name, _, _ in events.items] == [loop.CASE_OPENED, loop.CASE_SLA_BREACHED]
    assert "half-written" not in [event_id for _, _, event_id in events.items]


def test_severity_defaults_to_the_policy_sla_when_the_record_has_none():
    assert loop.severity_of({"severity": "P2"}) == ("P2", 72, [])
    assert loop.severity_of({"severity": "P3", "sla_hours": 0}) == ("P3", 720, [])
    assert loop.severity_of(None) == (None, None, [])


# --- Proof -------------------------------------------------------------------


def _check(target: str, **kw) -> None:
    rules.check_transition(
        current=kw.pop("current", "remediating"),
        target=target,
        actor_kind=kw.pop("actor_kind", "user"),
        owner_user_id=uuid.uuid4(),
        now=NOW,
        **kw,
    )


def test_resolve_is_refused_without_proof_when_the_case_has_a_validation_plan():
    # An agent closes a planned case only on a passing reference (#857).
    with pytest.raises(rules.CaseRuleError, match="validation plan"):
        _check("resolved", evidence="ticket closed", validation_plan=True, actor_kind="agent")
    with pytest.raises(rules.CaseRuleError, match="validation plan"):
        _check(
            "resolved",
            evidence="ok",
            validation_plan=True,
            validations=[{"result": "failed"}],
            actor_kind="agent",
        )
    _check("resolved", evidence="ok", validation_plan=True, validations=[{"result": "passed"}], actor_kind="agent")


def test_a_person_or_an_answered_decision_closes_a_planned_case_without_the_retest():
    # #1675: a person's own move, or an answered ctem.close_without_retest decision, is the proof.
    _check("resolved", evidence="ticket closed", validation_plan=True)
    _check(
        "resolved",
        evidence="ticket closed",
        validation_plan=True,
        actor_kind="agent",
        close_without_retest_answered=True,
    )


def test_resolve_without_a_plan_still_takes_evidence_only():
    _check("resolved", evidence="ticket PLAT-1 done")


def test_mitigated_watching_needs_a_passing_reference():
    with pytest.raises(rules.CaseRuleError, match="passing validation"):
        _check("mitigated_watching", actor_kind="agent")
    _check("mitigated_watching", validations=[{"result": "passed", "mitigation_only": True}], actor_kind="agent")
    _check("mitigated_watching")  # a person decided (#1675)


def test_unknown_resolution_kind_is_refused():
    with pytest.raises(rules.CaseRuleError, match="resolution kind"):
        _check("resolved", evidence="x", resolution_kind="gone")


def test_a_case_with_a_plan_never_resolves_on_absence_at_source():
    snap = rules.CaseSnapshot(
        id="c1", keys=["k"], state="remediating", members={"openaev:f1": True}, validation_plan=True
    )
    plans = rules.plan_recompute([snap], [], closed=["openaev:f1"])
    assert plans[0].resolve == ["openaev:f1"] and plans[0].new_state is None


# --- Validation results (an event updates the case, no recompute) ------------


@pytest.mark.asyncio
async def test_a_passing_validation_resolves_the_case_and_raises_one_event():
    db, events = FakeSession(), loop.PendingEvents()
    case = _case(state="remediating")
    await loop.record_validation(
        db, case, provider="openaev", kind="simulation", ref="sim-1", result="passed", events=events, now=NOW
    )
    assert case.state == "resolved" and case.resolution_kind == "fixed"
    assert case.validation_plan is True and case.validations[-1]["ref"] == "sim-1"
    assert [n for n, _, _ in events.items] == [loop.CASE_RESOLVED]


@pytest.mark.asyncio
async def test_not_exposed_closes_as_not_exposed():
    case = _case()
    await loop.record_validation(
        FakeSession(), case, provider="openaev", kind="check", ref="r", result="not_exposed", now=NOW
    )
    assert case.state == "resolved" and case.resolution_kind == "not_exposed"


@pytest.mark.asyncio
async def test_a_mitigation_pass_keeps_the_case_watched():
    case = _case(state="remediating")
    events = loop.PendingEvents()
    await loop.record_validation(
        FakeSession(),
        case,
        provider="openaev",
        kind="retest",
        ref="r1",
        result="passed",
        mitigation_only=True,
        events=events,
        now=NOW,
    )
    assert case.state == "mitigated_watching" and case.resolution_kind is None
    assert events.items == []


@pytest.mark.parametrize("state", ["resolved", "mitigated_watching"])
@pytest.mark.asyncio
async def test_a_failed_retest_reopens_and_raises_one_event(state):
    db, events = FakeSession(), loop.PendingEvents()
    case = _case(state=state, resolution_kind="fixed" if state == "resolved" else None)
    await loop.record_validation(
        db, case, provider="openaev", kind="retest", ref="r2", result="failed", events=events, now=NOW
    )
    assert case.state == "new" and case.resolution_kind is None
    assert [n for n, _, _ in events.items] == [loop.CASE_REOPENED]
    assert db.events("state")[0].reason.startswith("re-test failed")


@pytest.mark.asyncio
async def test_a_failed_validation_on_an_open_case_only_records_it():
    case = _case(state="new")
    await loop.record_validation(
        FakeSession(), case, provider="openaev", kind="check", ref="r", result="failed", now=NOW
    )
    assert case.state == "new" and case.validations[-1]["result"] == "failed"


@pytest.mark.asyncio
async def test_a_validation_records_the_run_order_when_given() -> None:
    case = _case()
    await loop.record_validation(
        cast("AsyncSession", FakeSession()),
        case,
        provider="openaev",
        kind="validation",
        ref="sc-1",
        result="pending",
        run_order=[" DMZ web ", "", "Core"] + [f"g{i}" for i in range(30)],
        now=NOW,
    )
    entry = case.validations[-1]
    assert "safe_mode" not in entry
    assert entry["run_order"][:2] == ["DMZ web", "Core"] and len(entry["run_order"]) == loop.RUN_ORDER_MAX


@pytest.mark.asyncio
async def test_a_validation_without_run_order_keeps_the_entry_shape() -> None:
    case = _case()
    await loop.record_validation(
        cast("AsyncSession", FakeSession()), case, provider="openaev", kind="check", ref="r", result="pending", now=NOW
    )
    assert "safe_mode" not in case.validations[-1] and "run_order" not in case.validations[-1]


@pytest.mark.asyncio
async def test_a_validation_reference_needs_provider_ref_and_a_known_result():
    with pytest.raises(rules.CaseRuleError):
        await loop.record_validation(FakeSession(), _case(), provider="", kind="k", ref="r", result="passed", now=NOW)
    with pytest.raises(rules.CaseRuleError):
        await loop.record_validation(FakeSession(), _case(), provider="p", kind="k", ref="r", result="green", now=NOW)


# --- SLA ---------------------------------------------------------------------


def test_sla_breach_is_raised_once():
    case = _case(sla_due_at=NOW - timedelta(minutes=1))
    assert loop.breached(case, NOW)
    case.sla_breached_at = NOW
    assert not loop.breached(case, NOW)


def test_no_breach_before_the_due_date_or_on_a_closed_case():
    assert not loop.breached(_case(sla_due_at=NOW + timedelta(hours=1)), NOW)
    assert not loop.breached(_case(state="resolved", sla_due_at=NOW - timedelta(hours=1)), NOW)


def test_every_case_event_is_registered_and_checked_against_the_viewer():
    from app.services.platform_events import PLATFORM_EVENTS, PLATFORM_WIDE_EVENTS, VIEWER_CHECKED_EVENTS

    for name in loop.CASE_EVENTS:
        assert name in PLATFORM_EVENTS and name in VIEWER_CHECKED_EVENTS and name not in PLATFORM_WIDE_EVENTS


def test_every_case_state_is_counted_by_the_list_route_and_the_tool():
    """A new state reaches the counts, the route filter and the tool enum (UX round 1, B1)."""
    import inspect
    from typing import get_args

    from app.api.v1 import ctem_cases as api
    from app.models.ctem_case import CASE_STATES
    from app.services.builtin_tools import ctem_cases as tool

    assert set(get_args(api.CaseState)) == set(CASE_STATES)
    assert tool._STATES == CASE_STATES
    source = inspect.getsource(api)
    assert '"needs_attention", "in_progress", "resolved"' not in source
