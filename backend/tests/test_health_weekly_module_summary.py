"""The weekly digest's Modules part (#101 S15d, dev-docs/platform-health.md 13.8).

Per module, the check rounds of the week and the checks failed most, read from the stored
check reports only, and carried in the digest's trigger payload so the model restates
figures it did not count.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any, cast

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.services.health import check_reports, cycles, digest
from app.services.health.check_reports import weekly_module_summary

UNTIL = datetime(2026, 11, 22, 12, 0, tzinfo=UTC)
SINCE = UNTIL - timedelta(days=7)
REG_A = uuid.UUID("00000000-0000-4000-8000-00000000000a")
REG_B = uuid.UUID("00000000-0000-4000-8000-00000000000b")


def _report(module: str, reg: uuid.UUID, checks: list[tuple[str, str]]) -> SimpleNamespace:
    tally = {v: sum(1 for _, verdict in checks if verdict == v) for v in ("pass", "warn", "fail", "not_run")}
    return SimpleNamespace(
        module=module,
        registration_id=reg,
        passed=tally["pass"],
        warned=tally["warn"],
        failed=tally["fail"],
        not_run=tally["not_run"],
        checks=[{"id": check, "verdict": verdict} for check, verdict in checks],
    )


class _Result:
    def __init__(self, rows: list[Any]) -> None:
        self._rows = rows

    def scalars(self) -> list[Any]:
        return self._rows


class _Session:
    def __init__(self, rows: list[Any]) -> None:
        self.rows = rows
        self.statements: list[Any] = []

    async def execute(self, statement: Any) -> _Result:
        self.statements.append(statement)
        return _Result(self.rows)


@pytest.mark.asyncio
async def test_rounds_and_most_failed_per_module() -> None:
    rows = [
        _report("opencti", REG_A, [("managers_running", "fail"), ("queues", "warn"), ("licence", "pass")]),
        _report("opencti", REG_A, [("managers_running", "fail"), ("queues", "pass"), ("licence", "not_run")]),
        _report("opencti", REG_B, [("managers_running", "pass"), ("queues", "warn"), ("licence", "pass")]),
        _report("opencrq", REG_A, [("jobs_state", "pass")]),
    ]
    summary = await weekly_module_summary(cast(AsyncSession, _Session(rows)), since=SINCE, until=UNTIL)

    assert list(summary) == ["opencrq", "opencti"]
    cti = summary["opencti"]
    assert (cti["rounds"], cti["registrations"]) == (3, 2)
    assert (cti["passed"], cti["warned"], cti["failed"], cti["not_run"]) == (4, 2, 2, 1)
    assert cti["most_failed"] == [
        {"id": "managers_running", "failed": 2, "warned": 0},
        {"id": "queues", "failed": 0, "warned": 2},
    ]
    assert summary["opencrq"]["most_failed"] == []


@pytest.mark.asyncio
async def test_most_failed_is_capped_and_ties_break_by_id() -> None:
    checks = [(f"check_{i}", "fail") for i in range(check_reports.WEEKLY_MOST_FAILED + 3)]
    summary = await weekly_module_summary(
        cast(AsyncSession, _Session([_report("openaev", REG_A, list(reversed(checks)))])), since=SINCE, until=UNTIL
    )
    ranked = summary["openaev"]["most_failed"]
    assert len(ranked) == check_reports.WEEKLY_MOST_FAILED
    assert [entry["id"] for entry in ranked] == sorted(entry["id"] for entry in ranked)


@pytest.mark.asyncio
async def test_no_report_no_module() -> None:
    assert await weekly_module_summary(cast(AsyncSession, _Session([])), since=SINCE, until=UNTIL) == {}


@pytest.mark.asyncio
async def test_the_window_is_half_open_on_finished_at() -> None:
    session = _Session([])
    await weekly_module_summary(cast(AsyncSession, session), since=SINCE, until=UNTIL)
    sql = str(session.statements[0].compile(compile_kwargs={"literal_binds": False}))
    assert "health_module_check_reports.finished_at >=" in sql
    assert "health_module_check_reports.finished_at <" in sql


class _Nested:
    async def __aenter__(self) -> None:
        return None

    async def __aexit__(self, *exc: object) -> bool:
        return False


class _DigestSession:
    def begin_nested(self) -> _Nested:
        return _Nested()


@pytest.mark.asyncio
async def test_the_digest_payload_carries_the_modules_part(monkeypatch: pytest.MonkeyPatch) -> None:
    async def fake(_db: Any, *, since: datetime, until: datetime) -> dict[str, Any]:
        assert (since, until) == (SINCE, UNTIL)
        return {"opencti": {"rounds": 7}}

    monkeypatch.setattr(check_reports, "weekly_module_summary", fake)
    payload = await cycles._digest_payload(cast(AsyncSession, _DigestSession()), SINCE, UNTIL)
    assert payload == {"since": SINCE.isoformat(), "modules": {"opencti": {"rounds": 7}}}


@pytest.mark.asyncio
async def test_a_failed_read_says_unavailable(monkeypatch: pytest.MonkeyPatch) -> None:
    async def broken(_db: Any, **_kw: Any) -> dict[str, Any]:
        raise RuntimeError("no table")

    monkeypatch.setattr(check_reports, "weekly_module_summary", broken)
    payload = await cycles._digest_payload(cast(AsyncSession, _DigestSession()), SINCE, UNTIL)
    assert payload == {"since": SINCE.isoformat(), "modules_unavailable": True}


# The Cycles tab shows the part (#101 S15e): the weekly digest cycle's digest copies it from its run's trigger.


def test_the_cycle_digest_takes_the_modules_part_from_its_run() -> None:
    part = {"opencti": {"rounds": 7, "most_failed": []}}
    run = SimpleNamespace(trigger_payload={"since": SINCE.isoformat(), "modules": part})
    assert digest._weekly_modules(run) == (part, False)


def test_the_cycle_digest_says_the_part_is_unavailable() -> None:
    run = SimpleNamespace(trigger_payload={"since": SINCE.isoformat(), "modules_unavailable": True})
    assert digest._weekly_modules(run) == (None, True)


@pytest.mark.parametrize("payload", [None, {}, {"cycle_id": "c1"}, {"modules": "not a mapping"}])
def test_another_cycle_has_no_modules_part(payload: Any) -> None:
    assert digest._weekly_modules(SimpleNamespace(trigger_payload=payload)) == (None, False)
    assert digest._weekly_modules(None) == (None, False)
