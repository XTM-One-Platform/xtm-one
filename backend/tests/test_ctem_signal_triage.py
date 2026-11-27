# SPDX-License-Identifier: Apache-2.0

"""Unmatched CTEM signals (#1505): dismiss, restore and re-check, and the re-sighting re-check.

The gate's I/O is faked (no profile row, no OpenCRQ read) so the triage rules
are checked on plain rows; the gate's own rules are ``test_ctem_scope_gate.py``.
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from types import SimpleNamespace
from typing import Any

import pytest

from app.services import ctem_scope_gate as gate
from app.services import ctem_scope_gate_store as store
from app.services import organization_context as oc

NOW = datetime(2026, 11, 23, 12, 0, tzinfo=UTC)
TODAY = date(2026, 11, 23)


def _profile(*hosts: str) -> oc.OrganizationProfile:
    return oc.parse_profile({"assets_exposure": [{"name": h, "criticality": "high"} for h in hosts]})


def _row(state: str = "to_validate", ref: str = "langfuse.example.com", decision: dict | None = None):
    return SimpleNamespace(
        id=ref,
        organization_id="org",
        state=state,
        trigger_path="surface_drift",
        lens="outside",
        subject_kind="asset",
        subject_ref=ref,
        source_provider="shodan",
        source_external_id=None,
        subject_label=None,
        evidence={},
        decision=decision
        if decision is not None
        else {"reason_code": "subject_unresolved", "reconcile": {"keys": [ref]}},
        updated_at=None,
        recorded_by="system",
    )


@pytest.fixture
def fake_io(monkeypatch):
    """The profile holds whatever ``inventory`` lists; no related signal, no OpenCRQ read."""
    inventory: list[str] = []

    async def _no_related(_db, _row):
        return []

    async def _profile_of(_db, _org):
        return _profile(*inventory)

    async def _scenarios(_db, *, user_id):
        return gate.ScenarioIndex()

    monkeypatch.setattr(store, "related_signals", _no_related)
    monkeypatch.setattr(store, "_profile", _profile_of)

    async def _no_reader(_db):
        return None

    monkeypatch.setattr(store, "read_scenarios", _scenarios)
    monkeypatch.setattr(store, "default_scenario_reader", _no_reader)
    return inventory


def _host(ref: str) -> gate.SignalFacts:
    return gate.SignalFacts(
        trigger_path="surface_drift",
        lens="outside",
        subject_kind="asset",
        subject_ref=ref,
        source_provider="shodan",
        evidence={},
    )


def test_a_host_under_an_owned_domain_is_in_the_inventory_without_opening_a_case():
    profile = oc.parse_profile({"owned_domains": ["corp.example"]})
    decision = gate.decide(profile, _host("https://langfuse.one.corp.example"), today=TODAY)
    assert decision.outcome == "watchlist" and decision.record["reason_code"] == "criticality_unknown"
    assert decision.record["reconcile"]["hosted_under"] == ["corp.example"]
    # Not under it, an IP, or a look-alike suffix: still unmatched.
    for ref in ("langfuse.other.example", "10.0.0.4", "evilcorp.example"):
        assert gate.decide(profile, _host(ref), today=TODAY).outcome == "to_validate", ref


def test_an_asset_named_in_the_inventory_wins_over_its_owned_domain():
    profile = oc.parse_profile(
        {"owned_domains": ["corp.example"], "assets_exposure": [{"name": "vpn.corp.example", "criticality": "high"}]}
    )
    decision = gate.decide(profile, _host("vpn.corp.example"), today=TODAY)
    assert decision.outcome == "case"
    assert decision.record["reconcile"]["hosted_under"] == []


async def test_dismiss_records_who_and_keeps_the_reconcile(fake_io):
    rows = [_row(ref=f"h{i}.example.com") for i in range(120)]
    counts = await store.triage(None, rows, action="dismiss", actor="Ana", evaluator_id=None, now=NOW)
    assert counts["changed"] == 120 and counts["discarded"] == 120
    row = rows[0]
    assert row.state == "discarded" and store.dismissed(row)
    assert row.decision["reason_code"] == store.DISMISSED_CODE
    assert row.decision["reason_params"] == {"by": "Ana"}
    assert row.decision["dismissed"]["previous_state"] == "to_validate"
    assert row.decision["reconcile"] == {"keys": ["h0.example.com"]}


async def test_triage_outcomes_name_each_signal_before_and_after(fake_io: object) -> None:
    # #2341: the dry run reads these before it rolls back.
    fresh, dismissed_row = _row(ref="a.example.com"), _row(ref="b.example.com")
    db: Any = None
    await store.triage(db, [dismissed_row], action="dismiss", actor="Ana", evaluator_id=None, now=NOW)
    outcomes: list[dict[str, Any]] = []
    await store.triage(
        db, [fresh, dismissed_row], action="dismiss", actor="Ana", evaluator_id=None, now=NOW, outcomes=outcomes
    )
    assert [o["outcome"] for o in outcomes] == ["changed", "skipped"]
    assert outcomes[0] == {"id": str(fresh.id), "before": "to_validate", "after": "discarded", "outcome": "changed"}
    assert outcomes[1]["before"] == outcomes[1]["after"] == "discarded"


async def test_a_dismissed_signal_stays_dismissed_until_restored(fake_io):
    row = _row()
    await store.triage(None, [row], action="dismiss", actor="Ana", evaluator_id=None, now=NOW)
    fake_io.append("langfuse.example.com")
    # Neither a re-check nor a bulk evaluation moves it back.
    counts = await store.triage(None, [row], action="recheck", actor="Ana", evaluator_id=None, now=NOW)
    assert counts["skipped"] == 1 and row.state == "discarded"
    assert await store.evaluate(None, row, profile=_profile("langfuse.example.com")) is None
    counts = await store.triage(None, [row], action="restore", actor="Ana", evaluator_id=None, now=NOW)
    assert counts["changed"] == 1 and row.state == "confirmed"
    assert row.decision["outcome"] == "case"


async def test_restore_skips_a_signal_nobody_dismissed(fake_io):
    row = _row()
    counts = await store.triage(None, [row], action="restore", actor="Ana", evaluator_id=None, now=NOW)
    assert counts == {"changed": 0, "skipped": 1}
    assert row.state == "to_validate"


async def test_recheck_moves_an_unmatched_signal_once_the_inventory_holds_it(fake_io):
    rows = [_row(ref="langfuse.example.com"), _row(ref="other.example.com")]
    fake_io.append("langfuse.example.com")
    counts = await store.triage(None, rows, action="recheck", actor="Ana", evaluator_id=None, now=NOW)
    assert rows[0].state == "confirmed" and rows[1].state == "to_validate"
    assert counts["changed"] == 1 and counts["confirmed"] == 1 and counts["to_validate"] == 1


async def test_a_signal_that_opened_a_case_is_never_triaged(fake_io):
    row = _row(state="case_opened", decision={"outcome": "case", "reason_code": "in_scope"})
    for action in store.TRIAGE_ACTIONS:
        counts = await store.triage(None, [row], action=action, actor="Ana", evaluator_id=None, now=NOW)
        assert counts["skipped"] == 1 and row.state == "case_opened"


async def test_an_unknown_action_is_refused(fake_io):
    with pytest.raises(ValueError):
        await store.triage(None, [_row()], action="delete", actor="Ana", evaluator_id=None)


async def test_the_triage_route_reads_with_the_callers_rights(monkeypatch):
    """A person with a CTEM decide right triages the organization's signals; ids or a filter, never both (#2441)."""
    import uuid

    import httpx
    from fastapi import FastAPI

    from app.api.deps import get_current_user
    from app.api.v1 import ctem_signals as api_module
    from app.core.config import settings
    from app.core.database import get_db
    from app.services import ctem_access
    from app.services import ctem_signals as signals

    alice = uuid.uuid4()
    seen: dict = {}
    monkeypatch.setattr(type(settings), "is_copilot_like", property(lambda _s: False), raising=False)

    async def _primary(_db):
        return SimpleNamespace(id="org")

    async def _select(_db, **kw):
        seen.update(kw)
        return [_row(ref=f"h{i}.example.com") for i in range(150)]

    monkeypatch.setattr(api_module, "get_primary", _primary)
    monkeypatch.setattr(signals, "select_signals", _select)

    async def _decide(_db, _user):
        return "decide"

    monkeypatch.setattr(ctem_access, "level", _decide)

    class _Db:
        async def commit(self):
            return None

    app = FastAPI()
    app.include_router(api_module.router, prefix="/api/v1/ctem-signals")

    async def _db():
        yield _Db()

    async def _user():
        return SimpleNamespace(id=alice, is_admin=False, name="Alice")

    app.dependency_overrides[get_db] = _db
    app.dependency_overrides[get_current_user] = _user
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as http:
        ok = await http.post(
            "/api/v1/ctem-signals/triage", json={"action": "dismiss", "filter": {"state": "to_validate"}}
        )
        both = await http.post(
            "/api/v1/ctem-signals/triage",
            json={"action": "dismiss", "ids": [str(uuid.uuid4())], "filter": {"state": "to_validate"}},
        )
        unknown = await http.post("/api/v1/ctem-signals/triage", json={"action": "delete", "ids": [str(uuid.uuid4())]})
    assert ok.status_code == 200, ok.text
    assert ok.json() == {"matched": 150, "counts": {"changed": 150, "skipped": 0, "discarded": 150}}
    assert seen["recorded_by"] is None and seen["state"] == "to_validate"
    assert both.status_code == 422 and unknown.status_code == 422


async def test_a_profile_change_checks_the_unmatched_signals_again(fake_io):
    """The tick decides again the parked signals older than the newest profile version, in one batch."""
    rows = [_row(ref="langfuse.example.com"), _row(ref="other.example.com")]
    statements: list = []

    class _Result:
        def scalars(self):
            return iter(rows)

    class _Db:
        async def execute(self, statement):
            statements.append(str(statement))
            return _Result()

    fake_io.append("langfuse.example.com")
    counts = await store.recheck_parked(_Db(), now=NOW, batch=50)
    assert counts == {"confirmed": 1, "to_validate": 1}
    assert rows[0].state == "confirmed" and rows[1].updated_at == NOW
    sql = statements[0]
    assert "organization_context_versions" in sql and "ctem_signals.updated_at <" in sql


def _subject(kind: str, ref: str, state: str = "to_validate", label: str | None = None):
    row = _row(state=state, ref=ref)
    row.subject_kind = kind
    row.subject_label = label
    return row


def test_add_to_inventory_writes_one_row_per_subject_and_nothing_already_there():
    profile = oc.parse_profile(
        {
            "assets_exposure": [{"name": "vpn.corp.example"}],
            "software": [{"product": "Jira"}],
            "owned_domains": ["corp.example"],
        }
    )
    rows = [
        _subject("asset", "https://Langfuse.One.Example.com/login"),
        _subject("asset", "langfuse.one.example.com"),  # the same host twice: one row
        _subject("asset", "vpn.corp.example"),  # already an asset
        _subject("software", "cpe:2.3:a:grafana:grafana", label="Grafana"),
        _subject("software", "jira"),  # already in the software inventory
        _subject("identity", "ana@partner.example"),
        _subject("identity", "bob@corp.example"),  # already owned
        _subject("vulnerability", "CVE-2026-1"),  # never an inventory row
        _subject("asset", "seen.example.com", state="watchlist"),  # not unmatched
    ]
    added = store.inventory_additions(profile, rows)
    assert added == {
        "assets": ["langfuse.one.example.com"],
        "products": ["Grafana"],
        "domains": ["partner.example"],
    }


async def test_add_to_inventory_saves_one_version_then_checks_the_signals_again(fake_io, monkeypatch):
    saved: list = []

    async def _current(_db, _org):
        return None

    async def _save(_db, profile, **kw):
        saved.append((profile, kw))
        fake_io.extend(a.name for a in profile.assets_exposure)
        return SimpleNamespace(version=7)

    monkeypatch.setattr(oc, "current_version", _current)
    monkeypatch.setattr(oc, "save_version", _save)
    rows = [_row(ref=f"h{i}.example.com") for i in range(120)] + [_row(state="watchlist", ref="w.example.com")]
    result = await store.add_to_inventory(None, rows, organization_id="org", admin_id=None, actor="Ana")
    assert len(saved) == 1
    profile, kw = saved[0]
    assert len(profile.assets_exposure) == 120
    assert {a.source for a in profile.assets_exposure} == {store.INVENTORY_SOURCE}
    assert {a.category for a in profile.assets_exposure} == {"host"}
    assert kw["sources"] == {
        "assets_exposure": {"source": store.INVENTORY_SOURCE, "basis": "added from the CTEM Signals page by Ana"}
    }
    assert result["version"] == 7
    assert result["counts"]["confirmed"] == 120 and result["counts"]["skipped"] == 1
    assert rows[-1].state == "watchlist"


async def test_add_to_inventory_saves_nothing_when_every_subject_is_there(fake_io, monkeypatch):
    async def _current(_db, _org):
        return SimpleNamespace(profile={"assets_exposure": [{"name": "langfuse.example.com"}]})

    async def _save(*_a, **_kw):
        raise AssertionError("no version to save")

    monkeypatch.setattr(oc, "current_version", _current)
    monkeypatch.setattr(oc, "save_version", _save)
    result = await store.add_to_inventory(None, [_row()], organization_id="org", admin_id=None, actor="Ana")
    assert result["version"] is None and result["added"]["assets"] == []


async def test_the_add_to_inventory_route_is_for_administrators(monkeypatch):
    import uuid

    import httpx
    from fastapi import FastAPI

    from app.api.deps import get_current_user
    from app.api.v1 import ctem_signals as api_module
    from app.core.config import settings
    from app.core.database import get_db

    monkeypatch.setattr(type(settings), "is_copilot_like", property(lambda _s: False), raising=False)
    app = FastAPI()
    app.include_router(api_module.router, prefix="/api/v1/ctem-signals")

    class _Db:
        async def commit(self):
            return None

    async def _db():
        yield _Db()

    async def _user():
        return SimpleNamespace(id=uuid.uuid4(), is_admin=False, is_active=True, name="Bob")

    app.dependency_overrides[get_db] = _db
    app.dependency_overrides[get_current_user] = _user
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as http:
        refused = await http.post("/api/v1/ctem-signals/add-to-inventory", json={"ids": [str(uuid.uuid4())]})
    assert refused.status_code == 403


class _Nested:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


async def test_a_re_sighting_of_an_unmatched_signal_runs_the_gate_again(fake_io, monkeypatch):
    from app.services import ctem_case_loop

    opened: list[str] = []

    async def _open(_db, row, *, events):
        opened.append(row.id)

    monkeypatch.setattr(ctem_case_loop, "open_from_signal", _open)
    db = SimpleNamespace(begin_nested=lambda: _Nested())
    fake_io.append("langfuse.example.com")
    parked = _row()
    await store.evaluate_after_record(db, parked, created=False)
    assert parked.state == "confirmed" and opened == ["langfuse.example.com"]
    # A re-sighting of a discarded signal changes nothing.
    dropped = _row(state="discarded", ref="d.example.com", decision={"reason_code": "out_of_scope"})
    await store.evaluate_after_record(db, dropped, created=False)
    assert dropped.state == "discarded" and opened == ["langfuse.example.com"]


async def test_a_re_sighting_of_a_watchlist_signal_runs_the_gate_again(fake_io, monkeypatch):
    """#2581: the gate decides a watchlist signal again on every new sighting; a confirm opens its case."""
    from app.services import ctem_case_loop

    opened: list[str] = []

    async def _open(_db, row, *, events):
        opened.append(row.id)

    monkeypatch.setattr(ctem_case_loop, "open_from_signal", _open)
    decided: list[str] = []

    async def _evaluate(_db, row, **_kw):
        decided.append(row.id)
        row.state = "confirmed"

    monkeypatch.setattr(store, "evaluate", _evaluate)
    db = SimpleNamespace(begin_nested=lambda: _Nested())
    watched = _row(state="watchlist", ref="w.example.com", decision={"reason_code": "below_scope_threshold"})
    await store.evaluate_after_record(db, watched, created=False)
    assert decided == ["w.example.com"]
    assert watched.state == "confirmed" and opened == ["w.example.com"]


async def test_a_re_sighting_of_a_dismissed_signal_is_not_decided_again(fake_io, monkeypatch):
    decided: list[str] = []

    async def _evaluate(_db, row, **_kw):
        decided.append(row.id)

    monkeypatch.setattr(store, "evaluate", _evaluate)
    db = SimpleNamespace(begin_nested=lambda: _Nested())
    kept = _row(state="discarded", ref="k.example.com", decision={"reason_code": store.DISMISSED_CODE})
    await store.evaluate_after_record(db, kept, created=False)
    assert decided == [] and kept.state == "discarded"
