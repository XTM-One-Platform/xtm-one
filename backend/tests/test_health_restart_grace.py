"""Platform Health opens nothing and mails nothing for a restart (#958, dev-docs/platform-health.md
sections 3, 7 and 9): a finding opens on its second failing read in a row, its immediate email goes
out once it has been open for the alert window, and a restart of XTM One or of a module is a grace."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from app.models.health_check import STATUS_OPEN, STATUS_PENDING, STATUS_RESOLVED, HealthCheckFinding
from app.services.health import alerts, engine, snapshots
from app.services.health.engine import CheckResult, apply_grace, finding_key, in_startup_grace, record_results
from app.services.health.settings import HealthAlertEmailSettings

T0 = datetime(2026, 11, 21, 18, 0, tzinfo=UTC)
PASS = timedelta(minutes=5)


class _Rows:
    def __init__(self, rows: list[Any]) -> None:
        self._rows = rows

    def scalars(self) -> list[Any]:
        return list(self._rows)


class _Store:
    """An in-memory finding store standing for the AsyncSession the engine and the alerts use."""

    def __init__(self) -> None:
        self.rows: dict[str, HealthCheckFinding] = {}

    async def get(self, _model: Any, key: str) -> HealthCheckFinding | None:
        return self.rows.get(key)

    def add(self, row: HealthCheckFinding) -> None:
        self.rows[row.id] = row

    async def delete(self, row: HealthCheckFinding) -> None:
        self.rows.pop(row.id, None)

    async def flush(self) -> None:
        return None

    async def commit(self) -> None:
        return None

    async def execute(self, _stmt: Any) -> _Rows:
        # The alerts select open critical findings not yet alerted; alert_due re-reads each.
        return _Rows([r for r in self.rows.values() if r.status == STATUS_OPEN])


def _store() -> Any:
    return _Store()


def _worker_absent(failing: bool, *, in_grace: bool = False) -> CheckResult:
    return CheckResult(
        check_id="xtm_one.worker_absent",
        module="xtm_one",
        category="availability",
        severity="critical",
        failing=failing,
        evidence={"values": {"running": 0 if failing else 1}},
        in_grace=in_grace,
    )


def _queue_consumers(failing: bool, registration: str = "r1") -> CheckResult:
    return CheckResult(
        check_id="opencti.queue_consumers",
        module="opencti",
        category="availability",
        severity="critical",
        failing=failing,
        subject={"registration_id": registration},
    )


def _row(db: Any, result: CheckResult) -> HealthCheckFinding | None:
    row: HealthCheckFinding | None = db.rows.get(finding_key(result.check_id, result.subject))
    return row


def _open_row(db: Any, result: CheckResult) -> HealthCheckFinding:
    """The row of *result*, which the test expects to exist."""
    row = _row(db, result)
    assert row is not None
    return row


@pytest.fixture(autouse=True)
def _production_rule(monkeypatch: pytest.MonkeyPatch) -> None:
    """The production values the conftest relaxes for the check suites."""
    monkeypatch.setattr(engine, "OPEN_AFTER_FAILS", 2)
    monkeypatch.setattr(engine, "STARTUP_GRACE", timedelta(minutes=10))


@pytest.fixture
def mails(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    outbox: list[dict[str, Any]] = []

    async def fake_deliver(**kwargs: Any) -> None:
        outbox.append(kwargs)

    monkeypatch.setattr("app.services.email.deliver_email", fake_deliver)
    return outbox


async def _always(_key: str) -> bool:
    return True


CONFIG = HealthAlertEmailSettings(enabled=True, recipients=["ops@example.com"])


async def _pass(db: Any, results: list[CheckResult], now: datetime) -> int:
    """One health pass: record, then the immediate alerts; the number of findings mailed."""
    await record_results(db, results, now=now)
    return await alerts.send_immediate_alerts(db, CONFIG, now=now, claim=_always)


@pytest.mark.asyncio
async def test_one_failing_read_opens_nothing_and_sends_nothing(mails: list[dict[str, Any]]) -> None:
    db = _store()
    counts = await record_results(db, [_worker_absent(True)], now=T0)
    assert counts["opened"] == 0
    assert _open_row(db, _worker_absent(True)).status == STATUS_PENDING
    assert await alerts.send_immediate_alerts(db, CONFIG, now=T0 + timedelta(hours=1), claim=_always) == 0
    assert mails == []


@pytest.mark.asyncio
async def test_fail_pass_fail_never_opens() -> None:
    db = _store()
    for step, failing in enumerate([True, False, True, False, True]):
        counts = await record_results(db, [_worker_absent(failing)], now=T0 + step * PASS)
        assert counts["opened"] == 0
    row = _row(db, _worker_absent(True))
    assert row is not None and row.status == STATUS_PENDING and row.fail_streak == 1


@pytest.mark.asyncio
async def test_failing_for_fifteen_minutes_sends_exactly_one_email(mails: list[dict[str, Any]]) -> None:
    db = _store()
    mailed = [await _pass(db, [_worker_absent(True)], T0 + step * PASS) for step in range(8)]
    # Opens on the second read (T0 + 5 min), mails once open for 15 minutes (T0 + 20 min).
    assert mailed == [0, 0, 0, 0, 1, 0, 0, 0]
    assert len(mails) == 1
    row = _open_row(db, _worker_absent(True))
    assert (row.status, row.opened_at, row.alerted_at) == (STATUS_OPEN, T0 + PASS, T0 + 4 * PASS)


@pytest.mark.asyncio
async def test_a_finding_that_resolves_within_the_window_never_mails(mails: list[dict[str, Any]]) -> None:
    db = _store()
    for step, failing in enumerate([True, True, True, False, False, False]):
        await _pass(db, [_worker_absent(failing)], T0 + step * PASS)
    assert _open_row(db, _worker_absent(True)).status == STATUS_RESOLVED
    assert mails == []


@pytest.mark.asyncio
async def test_a_reopened_finding_is_a_new_episode_and_mails_once_more(mails: list[dict[str, Any]]) -> None:
    db = _store()
    steps = [True] * 5 + [False] * 2 + [True] * 5
    for step, failing in enumerate(steps):
        await _pass(db, [_worker_absent(failing)], T0 + step * PASS)
    assert len(mails) == 2


@pytest.mark.asyncio
async def test_an_xtm_one_restart_sends_nothing(mails: list[dict[str, Any]]) -> None:
    started = T0
    db = _store()
    # Worker presence is 0 for the first two passes after the process starts, then the workers are up.
    for step, failing in enumerate([True, True, False, False]):
        now = started + step * PASS
        results = [_worker_absent(failing)]
        if in_startup_grace(now, started_at=started):
            results = apply_grace(results, lambda r: r.module == "xtm_one")
        await _pass(db, results, now)
    assert _row(db, _worker_absent(True)) is None
    assert mails == []


def test_the_startup_grace_lasts_ten_minutes() -> None:
    assert in_startup_grace(T0 + timedelta(minutes=9, seconds=59), started_at=T0)
    assert not in_startup_grace(T0 + engine.STARTUP_GRACE, started_at=T0)


@pytest.mark.asyncio
async def test_a_failure_read_in_grace_refreshes_an_open_finding_but_opens_none() -> None:
    db = _store()
    await record_results(db, [_worker_absent(True)], now=T0)
    await record_results(db, [_worker_absent(True)], now=T0 + PASS)
    assert _open_row(db, _worker_absent(True)).status == STATUS_OPEN
    counts = await record_results(db, [_worker_absent(True, in_grace=True)], now=T0 + 2 * PASS)
    assert counts == {"opened": 0, "updated": 1, "resolved": 0}


@pytest.mark.asyncio
async def test_a_module_restart_sends_nothing_for_queue_consumers(mails: list[dict[str, Any]]) -> None:
    db = _store()
    restarted = {"r1"}
    # OpenCTI restarted: its workers come back within the grace, consumers return.
    for step, failing in enumerate([True, True, True, False, False]):
        results = apply_grace([_queue_consumers(failing)], lambda r: r.subject.get("registration_id") in restarted)
        await _pass(db, results, T0 + step * PASS)
    assert _row(db, _queue_consumers(True)) is None
    assert mails == []


@pytest.mark.asyncio
async def test_a_real_outage_longer_than_the_window_still_opens_and_mails_once(mails: list[dict[str, Any]]) -> None:
    db = _store()
    for step in range(20):
        await _pass(db, [_queue_consumers(True)], T0 + step * PASS)
    assert _open_row(db, _queue_consumers(True)).status == STATUS_OPEN
    assert len(mails) == 1


def test_apply_grace_marks_only_failing_availability_reads() -> None:
    capacity = CheckResult(
        check_id="xtm_one.trace_backlog", module="xtm_one", category="capacity", severity="warning", failing=True
    )
    marked = apply_grace([_worker_absent(True), _worker_absent(False), capacity], lambda _r: True)
    assert [r.in_grace for r in marked] == [True, False, False]


def test_a_registration_restarts_on_a_version_change_or_after_a_gap() -> None:
    restarted = snapshots.registration_restarted
    assert restarted("6.8.0", "6.8.1", T0, T0 + PASS)
    assert restarted("6.8.0", "6.8.0", T0, T0 + timedelta(minutes=9))
    assert not restarted("6.8.0", "6.8.0", T0, T0 + PASS)
    assert not restarted(None, "6.8.0", None, T0)
