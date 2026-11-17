"""Dismissed fleet health findings (#12014): the stable id, the store, the
status computation and the two tools, without a database."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from app.services import fleet_health_dismissals as dismissals
from app.services import fleet_health_signals as signals

NOW = datetime(2026, 11, 15, 12, 0, tzinfo=UTC)
FLEET_A = uuid.UUID("00000000-0000-0000-0000-00000000000a")
FLEET_B = uuid.UUID("00000000-0000-0000-0000-00000000000b")


def _findings(fleet_id=FLEET_A):
    return signals.with_ids(
        fleet_id,
        [
            signals._finding("cost_spike", "1 member spiked", ["agent-1"]),
            signals._finding("assignment_silent", "1 schedule silent", ["asg-1"]),
        ],
    )


def _dismiss(raw, ids, findings, **kw):
    kw.setdefault("reason", "accepted by the owner")
    return dismissals.dismiss(
        raw, ids, findings=findings, known_kinds=signals.FINDING_FIX, by="u1", by_name="Owner", now=NOW, **kw
    )


# ── The stable id ──────────────────────────────────────────────────────────


def test_the_id_is_stable_and_order_free():
    one = dismissals.finding_id(FLEET_A, "past_due", ["b", "a"])
    assert one == dismissals.finding_id(FLEET_A, "past_due", ["a", "b"])
    assert one.startswith("past_due:")


def test_new_evidence_or_another_fleet_gives_another_id():
    base = dismissals.finding_id(FLEET_A, "past_due", ["a"])
    assert base != dismissals.finding_id(FLEET_A, "past_due", ["a", "c"])
    assert base != dismissals.finding_id(FLEET_B, "past_due", ["a"])


def test_the_id_reads_the_whole_evidence_beyond_the_cap():
    ids = [f"i{n}" for n in range(signals.MAX_EVIDENCE + 3)]
    full = signals.with_ids(FLEET_A, [signals._finding("past_due", "x", ids)])[0]
    capped = signals.with_ids(FLEET_A, [signals._finding("past_due", "x", ids[:-1])])[0]
    assert full.id != capped.id
    # The uncapped list is never answered.
    assert "full_evidence" not in full.as_dict() and full.as_dict()["id"] == full.id


# ── The store ──────────────────────────────────────────────────────────────


def test_dismissing_a_finding_records_who_why_and_when():
    findings = _findings()
    stored, written = _dismiss(None, [findings[0].id], findings)
    assert stored == written
    record = written[0]
    assert record["id"] == findings[0].id and record["kind"] == "cost_spike" and record["scope"] == "finding"
    assert record["reason"] == "accepted by the owner" and record["by"] == "u1" and record["by_name"] == "Owner"
    assert record["at"] == NOW.isoformat(timespec="seconds") and record["until"] is None


@pytest.mark.parametrize(
    ("ids", "reason", "until", "message"),
    [
        ([], "why", None, "ids is required"),
        (["cost_spike"], "  ", None, "reason is required"),
        (["nope:123"], "why", None, "No such health finding"),
        (["cost_spike"], "why", "not a date", "until must be"),
        (["cost_spike"], "why", "2026-11-01", "until must be"),
    ],
)
def test_a_bad_dismissal_is_refused(ids, reason, until, message):
    with pytest.raises(dismissals.DismissalRefusedError, match=message):
        _dismiss(None, ids, _findings(), reason=reason, until=until)


def test_a_kind_is_dismissed_for_the_whole_fleet():
    stored, written = _dismiss(None, ["cost_spike"], [])
    assert written[0]["scope"] == "kind" and written[0]["kind"] == "cost_spike"
    # Any cost spike, whatever its evidence, is hidden.
    other = signals.with_ids(FLEET_A, [signals._finding("cost_spike", "2 members", ["x", "y"])])
    active, live = dismissals.split_findings(other, stored, NOW)
    assert active == [] and live[0]["hides"] == 1


def test_dismissing_again_replaces_the_record():
    findings = _findings()
    stored, _ = _dismiss(None, [findings[0].id], findings, reason="first")
    stored, _ = _dismiss(stored, [findings[0].id], findings, reason="second")
    assert len(stored) == 1 and stored[0]["reason"] == "second"


def test_restore_one_or_every_dismissal():
    findings = _findings()
    stored, _ = _dismiss(None, [f.id for f in findings], findings)
    kept, count = dismissals.restore(stored, [findings[0].id], NOW)
    assert count == 1 and [r["id"] for r in kept] == [findings[1].id]
    kept, count = dismissals.restore(stored, None, NOW)
    assert (kept, count) == ([], 2)
    with pytest.raises(dismissals.DismissalRefusedError, match="No such dismissed"):
        dismissals.restore(stored, ["unknown"], NOW)


def test_a_lapsed_dismissal_hides_nothing_and_is_dropped():
    findings = _findings()
    stored, _ = _dismiss(None, [findings[0].id], findings, until=(NOW + timedelta(days=1)).isoformat())
    active, live = dismissals.split_findings(findings, stored, NOW)
    assert findings[0] not in active and live[0]["until"]
    later = NOW + timedelta(days=2)
    active, live = dismissals.split_findings(findings, stored, later)
    assert findings[0] in active and live == []
    assert dismissals.live_records(stored, later) == []


# ── The status computation ─────────────────────────────────────────────────


def _report(findings, raw):
    active, dismissed = dismissals.split_findings(findings, raw, NOW)
    return signals.FleetHealthReport(
        fleet_id=str(FLEET_A), fleet_name="Ops", status="ok", members=[], findings=active, dismissed=dismissed
    )


def test_a_fleet_whose_only_findings_are_dismissed_reads_healthy():
    from app.services.assignment_activation_check import condition_met

    findings = _findings()
    stored, _ = _dismiss(None, [f.id for f in findings], findings)
    report = _report(findings, stored)
    assert report.healthy
    data = report.structured()
    assert data["healthy"] is True and data["findings"] == [] and len(data["dismissed"]) == 2
    # The weekly check's gate stands down.
    assert not condition_met(data, {"path": "$.healthy", "in": [False]})
    text = signals.format_report(report)
    assert "healthy: no finding" in text and "Dismissed (2" in text and "accepted by the owner" in text


def test_one_dismissed_finding_leaves_the_other_counted():
    findings = _findings()
    stored, _ = _dismiss(None, [findings[0].id], findings)
    report = _report(findings, stored)
    assert not report.healthy and [f.kind for f in report.findings] == ["assignment_silent"]
    assert f"`{findings[1].id}`" in signals.format_report(report)


def test_a_dismissal_on_one_fleet_does_not_hide_the_same_finding_on_another():
    on_a = _findings(FLEET_A)
    stored, _ = _dismiss(None, [on_a[0].id], on_a)
    on_b = _findings(FLEET_B)
    active, live = dismissals.split_findings(on_b, stored, NOW)
    assert active == on_b and live[0]["hides"] == 0


# ── The tools ──────────────────────────────────────────────────────────────


class _Session:
    def __init__(self) -> None:
        self.commits = 0
        self.rollbacks = 0

    async def commit(self) -> None:
        self.commits += 1

    async def rollback(self) -> None:
        self.rollbacks += 1


@pytest.fixture
def tools(monkeypatch):
    from contextlib import asynccontextmanager

    from app.services.builtin_tools import fleets as fleet_tools

    db = _Session()

    @asynccontextmanager
    async def _factory():
        yield db

    monkeypatch.setattr(fleet_tools.database, "async_session", _factory)
    user = SimpleNamespace(id=uuid.uuid4(), is_admin=False, full_name="Owner")

    async def _load(_db, _uid):
        return user

    monkeypatch.setattr(fleet_tools._builder, "_load_caller_user_for_builder", _load)
    fleets = {
        FLEET_A: SimpleNamespace(id=FLEET_A, name="A", slug="a", is_builtin=False, health_dismissals=None),
        FLEET_B: SimpleNamespace(id=FLEET_B, name="B", slug="b", is_builtin=False, health_dismissals=None),
    }

    async def _entity(_db, _model, _uid, fleet_id, **_kw):
        return fleets.get(fleet_id), None

    monkeypatch.setattr(fleet_tools._builder, "_load_manageable_entity_for_builder", _entity)

    async def _visible(_db, _user, fleet_id, *a, **k):
        return fleets[fleet_id]

    async def _report(_db, fleet, **_kw):
        findings = _findings(fleet.id)
        active, dismissed = dismissals.split_findings(findings, fleet.health_dismissals, datetime.now(UTC))
        return signals.FleetHealthReport(
            fleet_id=str(fleet.id), fleet_name=fleet.name, status="ok", members=[], findings=active, dismissed=dismissed
        )

    monkeypatch.setattr(signals, "build_fleet_health_report", _report)
    audits: list[dict] = []

    async def _log_audit(_db, **kw):
        audits.append(kw)

    monkeypatch.setattr("app.services.audit.log_audit", _log_audit)
    _defs, executor = fleet_tools.build_fleet_tools(str(user.id))
    return SimpleNamespace(executor=executor, fleets=fleets, db=db, audits=audits, user=user)


def _health_ids(tools, fleet_id):
    return [f.id for f in _findings(fleet_id)]


@pytest.mark.asyncio
async def test_the_tool_dismisses_then_restores(tools):
    dismiss = tools.executor.handler("dismiss_fleet_health_findings")
    cost = _health_ids(tools, FLEET_A)[0]
    out = await dismiss(fleet_id=str(FLEET_A), ids=[cost], reason="cost is not a concern here")
    assert "1 health finding(s) dismissed" in out and cost in out
    stored = tools.fleets[FLEET_A].health_dismissals
    assert stored[0]["id"] == cost and stored[0]["reason"] == "cost is not a concern here"
    assert stored[0]["by"] == str(tools.user.id)
    assert tools.fleets[FLEET_B].health_dismissals is None
    assert tools.db.commits == 1 and tools.audits

    out = await dismiss(fleet_id=str(FLEET_A), action="restore", ids=[cost])
    assert "1 dismissal(s) restored" in out
    assert tools.fleets[FLEET_A].health_dismissals is None


@pytest.mark.asyncio
async def test_the_tool_refuses_without_a_reason_and_writes_nothing(tools):
    dismiss = tools.executor.handler("dismiss_fleet_health_findings")
    out = await dismiss(fleet_id=str(FLEET_A), ids=["cost_spike"])
    assert "reason is required" in out
    assert tools.fleets[FLEET_A].health_dismissals is None and tools.db.commits == 0


@pytest.mark.asyncio
async def test_a_kind_dismissal_through_the_tool_makes_get_fleet_health_skip_it(tools):
    dismiss = tools.executor.handler("dismiss_fleet_health_findings")
    await dismiss(fleet_id=str(FLEET_A), ids=["cost_spike", "assignment_silent"], reason="known and accepted")
    report = await signals.build_fleet_health_report(None, tools.fleets[FLEET_A])
    assert report.healthy and len(report.dismissed) == 2
    other = await signals.build_fleet_health_report(None, tools.fleets[FLEET_B])
    assert not other.healthy


def test_the_tool_is_described_with_its_arguments():
    from app.services.builtin_tools import fleets as fleet_tools

    defs, _executor = fleet_tools.build_fleet_tools(str(uuid.uuid4()))
    tool = next(d for d in defs if d.name == "dismiss_fleet_health_findings")
    assert set(tool.input_schema["properties"]) == {"fleet_id", "action", "ids", "reason", "until"}
    assert tool.input_schema["required"] == ["fleet_id"]
