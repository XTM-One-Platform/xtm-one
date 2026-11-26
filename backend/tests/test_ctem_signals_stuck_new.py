# SPDX-License-Identifier: Apache-2.0

"""CTEM signals never stay ``new`` (#2516).

Three paths: the OpenCTI CVE lookup never commits the gate's savepoint, a gate
that fails at record time is decided again with local reads only, and the
scheduled ``recheck_new`` decides what is still ``new`` past its grace, a row it
cannot decide never taking the batch down.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from app.services import ctem_cve_software
from app.services import ctem_scope_gate as gate
from app.services import ctem_scope_gate_store as store
from app.services import organization_context as oc

NOW = datetime(2026, 11, 26, 14, 0, tzinfo=UTC)


def _row(ref: str, state: str = "new"):
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
        decision=None,
        updated_at=NOW - timedelta(hours=1),
        recorded_by="system",
    )


@pytest.fixture
def fake_io(monkeypatch):
    inventory: list[str] = []

    async def _no_related(_db, _row):
        return []

    async def _profile_of(_db, _org):
        return oc.parse_profile({"assets_exposure": [{"name": h, "criticality": "high"} for h in inventory]})

    async def _scenarios(_db, *, user_id):
        return gate.ScenarioIndex()

    async def _no_reader(_db):
        return None

    async def _no_links(_db, _org):
        return None

    monkeypatch.setattr(store, "related_signals", _no_related)
    monkeypatch.setattr(store, "_profile", _profile_of)
    monkeypatch.setattr(store, "_primary_assets", _no_links)
    monkeypatch.setattr(store, "read_scenarios", _scenarios)
    monkeypatch.setattr(store, "default_scenario_reader", _no_reader)
    return inventory


class _Nested:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


async def test_the_cve_lookup_never_commits_the_gates_session(monkeypatch):
    """The gate decides inside a savepoint: the credential read must not release (commit) the session."""
    from app.services import ctem_metrics_collector

    seen: dict = {}

    async def _creds(_db, user_id, provider, *, release=True):
        seen["release"] = release
        return None

    monkeypatch.setattr(ctem_metrics_collector, "_user_creds", _creds)
    found = await ctem_cve_software.cve_software(object(), "00000000-0000-0000-0000-000000000001", ["CVE-2015-5123"])
    assert found == ((), ctem_cve_software.LOOKUP_NO_ACCESS)
    assert seen["release"] is False


async def test_a_slow_opencti_answer_is_a_failed_lookup_never_a_hang(monkeypatch):
    from app.integrations.providers.opencti import capabilities
    from app.services import ctem_metrics_collector

    async def _creds(_db, user_id, provider, *, release=True):
        return "https://opencti.example", {}

    async def _slow(*_a, **_k):
        await asyncio.sleep(5)
        return ["never"]

    monkeypatch.setattr(ctem_metrics_collector, "_user_creds", _creds)
    monkeypatch.setattr(capabilities, "attach_probe", lambda headers, _url: headers)
    monkeypatch.setattr(ctem_cve_software, "read_cve_software", _slow)
    monkeypatch.setattr(ctem_cve_software, "LOOKUP_TIMEOUT_SECONDS", 0.05)
    found = await ctem_cve_software.cve_software(object(), "00000000-0000-0000-0000-000000000001", ["CVE-2015-5123"])
    assert found == ((), ctem_cve_software.LOOKUP_FAILED)


async def test_a_gate_that_fails_at_record_time_is_decided_with_local_reads(fake_io, monkeypatch):
    """The first evaluation raises (a remote read broke the session); the retry without remote reads decides."""
    from app.services import ctem_case_loop, ctem_case_opening

    calls: list[bool] = []
    real = store.evaluate

    async def _flaky(db, row, **kwargs):
        calls.append(kwargs.get("remote_reads", True))
        if len(calls) == 1:
            raise RuntimeError("Can't operate on closed transaction inside context manager")
        return await real(db, row, **kwargs)

    async def _off(_db, _lens):
        return False

    monkeypatch.setattr(store, "evaluate", _flaky)
    monkeypatch.setattr(ctem_case_opening, "is_on", _off)
    monkeypatch.setattr(ctem_case_loop, "open_from_signal", None)
    fake_io.append("langfuse.example.com")
    row = _row("langfuse.example.com")
    db = SimpleNamespace(begin_nested=lambda: _Nested(), info={})
    await store.evaluate_after_record(db, row, created=True)
    assert calls == [True, False]
    assert row.state == "confirmed" and row.decision is not None


async def test_a_gate_that_fails_twice_leaves_the_signal_new_for_the_recheck(fake_io, monkeypatch):
    async def _broken(db, row, **kwargs):
        raise RuntimeError("down")

    monkeypatch.setattr(store, "evaluate", _broken)
    row = _row("langfuse.example.com")
    db = SimpleNamespace(begin_nested=lambda: _Nested(), info={})
    await store.evaluate_after_record(db, row, created=True)
    assert row.state == "new" and row.decision is None


class _Result:
    def __init__(self, rows):
        self._rows = rows

    def scalars(self):
        return iter(self._rows)


class _Db:
    def __init__(self, rows):
        self.rows = rows
        self.statements: list[str] = []
        self.info: dict = {}

    async def execute(self, statement):
        self.statements.append(str(statement))
        return _Result(self.rows)

    def begin_nested(self):
        return _Nested()


async def test_the_recheck_decides_the_signals_left_new_past_the_grace(fake_io):
    rows = [_row("langfuse.example.com"), _row("other.example.com")]
    db = _Db(rows)
    fake_io.append("langfuse.example.com")
    counts = await store.recheck_new(db, now=NOW, batch=50)
    assert counts == {"confirmed": 1, "to_validate": 1}
    assert all(r.decision is not None and r.updated_at == NOW for r in rows)
    sql = db.statements[0]
    assert "ctem_signals.state =" in sql and "ctem_signals.updated_at <" in sql and "LIMIT" in sql


async def test_one_signal_the_recheck_cannot_decide_never_takes_the_batch_down(fake_io, monkeypatch):
    rows = [_row("broken.example.com"), _row("langfuse.example.com")]
    real = store.evaluate

    async def _evaluate(db, row, **kwargs):
        if row.id == "broken.example.com":
            raise RuntimeError("bad evidence")
        return await real(db, row, **kwargs)

    monkeypatch.setattr(store, "evaluate", _evaluate)
    fake_io.append("langfuse.example.com")
    counts = await store.recheck_new(_Db(rows), now=NOW)
    assert counts == {"failed": 1, "confirmed": 1}
    assert rows[0].state == "new" and rows[0].updated_at == NOW
    assert rows[1].state == "confirmed"


class _Backlog:
    """A session over a backlog of ``new`` rows: each select answers the oldest still new past the grace."""

    def __init__(self, rows):
        self.rows = rows
        self.commits = 0
        self.selects = 0
        self.info: dict = {}

    async def execute(self, statement):
        self.selects += 1
        limit = statement._limit_clause.value
        due = [r for r in self.rows if r.state == "new" and r.updated_at < NOW - store.UNDECIDED_GRACE]
        return _Result(sorted(due, key=lambda r: r.updated_at)[:limit])

    async def commit(self):
        self.commits += 1

    def begin_nested(self):
        return _Nested()


async def test_a_backlog_of_new_signals_drains_in_one_pass_batch_by_batch(fake_io):
    """#2530: 1,200 signals left new past the grace are decided in one pass, 500 per committed batch."""
    rows = [_row(f"host-{i}.example.com") for i in range(1200)]
    fresh = _row("fresh.example.com")
    fresh.updated_at = NOW - timedelta(minutes=2)
    db = _Backlog([*rows, fresh])
    counts = await store.drain_new(db, now=NOW, batch=500)
    assert counts == {"to_validate": 1200, "batches": 3}
    assert db.commits == 3 and db.selects == 3
    assert all(r.state != "new" for r in rows)
    assert fresh.state == "new" and fresh.decision is None


async def test_the_drain_stops_at_its_time_budget(fake_io):
    rows = [_row(f"host-{i}.example.com") for i in range(1200)]
    db = _Backlog(rows)
    ticks = iter([0.0, 30.0])
    counts = await store.drain_new(db, now=NOW, batch=500, budget_seconds=20.0, clock=lambda: next(ticks))
    assert counts == {"to_validate": 500, "batches": 1}
    assert sum(r.state == "new" for r in rows) == 700


async def test_a_row_the_drain_cannot_decide_leaves_the_selection_and_never_loops(fake_io, monkeypatch):
    rows = [_row("broken.example.com"), _row("langfuse.example.com")]
    real = store.evaluate

    async def _evaluate(db, row, **kwargs):
        if row.id == "broken.example.com":
            raise RuntimeError("bad evidence")
        return await real(db, row, **kwargs)

    monkeypatch.setattr(store, "evaluate", _evaluate)
    fake_io.append("langfuse.example.com")
    db = _Backlog(rows)
    counts = await store.drain_new(db, now=NOW, batch=1)
    assert counts == {"failed": 1, "confirmed": 1, "batches": 2}
    assert rows[0].state == "new" and rows[0].updated_at == NOW
    assert db.selects == 3


async def test_an_empty_drain_answers_nothing(fake_io):
    db = _Backlog([])
    assert await store.drain_new(db, now=NOW) == {}
    assert db.commits == 1


async def test_the_drain_never_opens_a_case(fake_io, monkeypatch):
    """Case opening stays with the case sweep: the drain never calls the case loop."""
    from app.services import ctem_case_loop

    async def _never(*_a, **_k):
        raise AssertionError("the drain opened a case")

    monkeypatch.setattr(ctem_case_loop, "open_from_signal", _never)
    fake_io.append("langfuse.example.com")
    rows = [_row("langfuse.example.com")]
    counts = await store.drain_new(_Backlog(rows), now=NOW)
    assert counts == {"confirmed": 1, "batches": 1}


async def test_only_the_bulk_prefetch_releases_the_connection_during_the_cve_read(fake_io, monkeypatch):
    """#2518 review: the prefetch has no savepoint open and releases; the record-time gate never does."""
    seen: list[bool] = []

    async def _cve_software(_db, _reader, _cves, *, release=False):
        seen.append(release)
        return (), ctem_cve_software.LOOKUP_NO_ACCESS

    monkeypatch.setattr(ctem_cve_software, "cve_software", _cve_software)
    row = _row("CVE-2015-5123")
    row.recorded_by = "00000000-0000-0000-0000-000000000001"
    await store.prepare_cve_reads(_Db([]), [row])
    facts = store.facts_of(row)
    profile = await store._profile(None, "org")
    await store._with_cve_software(_Db([]), row, facts, profile, None)
    assert seen == [True, False]
