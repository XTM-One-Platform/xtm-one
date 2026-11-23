# SPDX-License-Identifier: Apache-2.0

"""Unmatched CTEM signals (#1505): dismiss, restore and re-check, and the re-sighting re-check.

The gate's I/O is faked (no profile row, no OpenCRQ read) so the triage rules
are checked on plain rows; the gate's own rules are ``test_ctem_scope_gate.py``.
"""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

from app.services import ctem_scope_gate as gate
from app.services import ctem_scope_gate_store as store
from app.services import organization_context as oc

NOW = datetime(2026, 11, 23, 12, 0, tzinfo=UTC)


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
    monkeypatch.setattr(store, "read_scenarios", _scenarios)
    return inventory


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
    """A non-administrator triages the signals recorded for them; ids or a filter, never both."""
    import uuid

    import httpx
    from fastapi import FastAPI

    from app.api.deps import get_current_user
    from app.api.v1 import ctem_signals as api_module
    from app.core.config import settings
    from app.core.database import get_db
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
    assert seen["recorded_by"] == str(alice) and seen["state"] == "to_validate"
    assert both.status_code == 422 and unknown.status_code == 422


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
    # A re-sighting of any other state changes nothing.
    watched = _row(state="watchlist", ref="w.example.com", decision={"reason_code": "below_scope_threshold"})
    await store.evaluate_after_record(db, watched, created=False)
    assert watched.state == "watchlist" and opened == ["langfuse.example.com"]
