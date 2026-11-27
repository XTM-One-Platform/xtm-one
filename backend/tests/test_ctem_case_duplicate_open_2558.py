"""One open case per fix (#2558): concurrent openings, two key spellings and the repair of duplicates.

ctem.one showed ``Patch CVE-2026-58937`` twice after case opening was switched on: the
listener path opened a case with no lock while a drain pass opened the same fix, and one
CVE could key on its STIX id or on its CVE id. ``open_from_signal`` now takes the key's
advisory lock before its lookup, keys a vulnerability patch on its CVE, and the sweep
folds open duplicates into the oldest case.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any, cast

import pytest

from app.models.ctem_case import CtemCase
from app.services import ctem_case_loop as loop
from app.services import ctem_cases as rules

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from app.models.ctem_signal import CtemSignal

NOW = datetime(2026, 11, 27, 5, 0, tzinfo=UTC)
ORG = uuid.uuid4()


def _signal(ref: str, **kw: Any) -> CtemSignal:
    base: dict[str, Any] = dict(
        id=uuid.uuid4(),
        organization_id=ORG,
        trigger_path="new_vulnerability",
        lens="world",
        subject_kind="vulnerability",
        subject_ref=ref,
        subject_label=ref,
        evidence={},
        state="confirmed",
        case_id=None,
        updated_at=None,
        decision={"outcome": "case", "state": "confirmed", "severity": "P2", "sla_hours": 72, "scope": {}},
    )
    base.update(kw)
    return cast("CtemSignal", SimpleNamespace(**base))


def _case(keys: list[str], created: datetime, **kw: Any) -> CtemCase:
    base: dict[str, Any] = dict(
        id=uuid.uuid4(),
        organization_id=ORG,
        vertical="cyber",
        title="Patch CVE-2026-58937",
        remediation_kind="patch",
        keys=keys,
        pinned_issues=[],
        state="new",
        state_since=created,
        trigger_path="new_vulnerability",
        risk_scenario_ids=[],
        severity="P2",
        sla_due_at=created + timedelta(hours=72),
        sla_breached_at=None,
        tickets=[],
        validations=[],
        validation_plan=False,
        resolution_kind=None,
        created_at=created,
    )
    base.update(kw)
    return CtemCase(**base)


def _db(session: object) -> AsyncSession:
    return cast("AsyncSession", session)


class _Savepoint:
    async def __aenter__(self) -> _Savepoint:
        return self

    async def __aexit__(self, *exc: object) -> bool:
        return False


# --- Two spellings of one CVE meet ---------------------------------------------


def test_a_cve_and_its_stix_id_key_one_patch_case() -> None:
    by_cve = loop.fix_of(_signal("CVE-2026-58937"))
    by_stix = loop.fix_of(
        _signal(
            "vulnerability--4b1e6f2c-0000-4000-8000-000000000001",
            subject_label=None,
            evidence={"cve": "cve-2026-58937"},
        )
    )
    assert by_cve.key == by_stix.key == "patch:vulnerability/cve-2026-58937"
    assert by_stix.title == "Patch CVE-2026-58937"
    assert by_stix.aliases == ("patch:vulnerability/vulnerability--4b1e6f2c-0000-4000-8000-000000000001",)


def test_a_named_vulnerability_keeps_its_title_and_keys_on_its_cve() -> None:
    fix = loop.fix_of(_signal("Log4Shell", evidence={"cve": "CVE-2021-44228"}))
    assert fix.key == "patch:vulnerability/cve-2021-44228"
    assert fix.title == "Patch Log4Shell"


def test_a_vulnerability_with_no_cve_keeps_its_subject_key() -> None:
    fix = loop.fix_of(_signal("GHSA-xxxx-yyyy-zzzz"))
    assert fix.key == "patch:vulnerability/ghsa-xxxx-yyyy-zzzz"
    assert fix.aliases == ()


def test_canonical_key_spells_a_vulnerability_patch_on_its_cve() -> None:
    assert loop.canonical_key("patch:vulnerability/CVE-2026-58867") == "patch:vulnerability/cve-2026-58867"
    assert loop.canonical_key("upgrade:-/chrome@latest") == "upgrade:-/chrome@latest"


class _LookupSession:
    """Answers the open cases for the key lookup."""

    def __init__(self, cases: list[CtemCase]) -> None:
        self.cases = cases

    async def execute(self, _statement: object, *_a: object, **_k: object) -> SimpleNamespace:
        cases = self.cases
        return SimpleNamespace(scalars=lambda: iter(cases))


@pytest.mark.asyncio
async def test_an_open_case_on_the_stix_key_is_joined_by_the_cve_signal() -> None:
    old = _case(["patch:vulnerability/vulnerability--abc"], NOW)
    found = await loop._open_case_for_key(
        _db(_LookupSession([old])),
        ORG,
        "cyber",
        "patch:vulnerability/cve-2026-58937",
        ("patch:vulnerability/vulnerability--abc",),
    )
    assert found is old
    assert "patch:vulnerability/cve-2026-58937" in old.keys


# --- Concurrent openings serialise on the key ---------------------------------


class _PgSession:
    """A session of *dialect* that records every advisory lock asked."""

    def __init__(self, dialect: str = "postgresql") -> None:
        self.locks: list[dict[str, Any]] = []
        self.dialect = dialect

    def get_bind(self) -> SimpleNamespace:
        return SimpleNamespace(dialect=SimpleNamespace(name=self.dialect))

    async def execute(self, statement: object, params: dict[str, Any] | None = None) -> SimpleNamespace:
        if "pg_advisory_xact_lock" in str(statement):
            self.locks.append(dict(params or {}))
        return SimpleNamespace(scalars=lambda: iter([]))


@pytest.mark.asyncio
async def test_two_spellings_of_one_cve_take_the_same_key_lock() -> None:
    session = _PgSession()
    await loop.hold_key_lock(_db(session), ORG, "cyber", "patch:vulnerability/CVE-2026-58937")
    await loop.hold_key_lock(_db(session), ORG, "cyber", "patch:vulnerability/cve-2026-58937")
    assert len(session.locks) == 2 and session.locks[0] == session.locks[1]


@pytest.mark.asyncio
async def test_the_key_lock_is_skipped_off_postgres() -> None:
    session = _PgSession("sqlite")
    await loop.hold_key_lock(_db(session), ORG, "cyber", "patch:vulnerability/cve-1")
    assert session.locks == []


@pytest.mark.asyncio
async def test_open_from_signal_takes_the_key_lock_before_its_lookup(monkeypatch: pytest.MonkeyPatch) -> None:
    order: list[str] = []

    async def lock(_db: object, _org: object, _vertical: str, key: str) -> None:
        order.append(f"lock {key}")

    async def lookup(_db: object, _org: object, _vertical: str, key: str, _aliases: tuple[str, ...] = ()) -> None:
        order.append(f"lookup {key}")

    monkeypatch.setattr(loop, "hold_key_lock", lock)
    monkeypatch.setattr(loop, "_open_case_for_key", lookup)

    class _Session:
        def __init__(self) -> None:
            self.added: list[object] = []

        def add(self, row: object) -> None:
            self.added.append(row)

        async def flush(self) -> None:
            return None

        async def execute(self, _statement: object, *_a: object, **_k: object) -> SimpleNamespace:
            return SimpleNamespace(scalars=lambda: iter([]))

    await loop.open_from_signal(_db(_Session()), _signal("CVE-2026-58867"), events=loop.PendingEvents(), now=NOW)
    key = "patch:vulnerability/cve-2026-58867"
    assert order[:2] == [f"lock {key}", f"lookup {key}"]


# --- The repair folds open duplicates into the oldest case --------------------


def test_duplicate_groups_pair_two_open_cases_of_one_cve_oldest_first() -> None:
    first = _case(["patch:vulnerability/cve-2026-58937"], NOW)
    second = _case(["patch:vulnerability/CVE-2026-58937"], NOW + timedelta(minutes=3))
    other = _case(["patch:vulnerability/cve-2026-58867"], NOW)
    assert loop.duplicate_groups([second, other, first]) == [[first, second]]


def test_duplicate_groups_join_cases_that_share_a_key_transitively() -> None:
    a = _case(["k1"], NOW)
    b = _case(["k1", "k2"], NOW + timedelta(minutes=1))
    c = _case(["k2"], NOW + timedelta(minutes=2))
    assert loop.duplicate_groups([c, b, a]) == [[a, b, c]]


@pytest.mark.asyncio
async def test_merge_open_duplicates_merges_a_seeded_pair_into_the_oldest(monkeypatch: pytest.MonkeyPatch) -> None:
    first = _case(["patch:vulnerability/cve-2026-58937"], NOW)
    second = _case(["patch:vulnerability/cve-2026-58937"], NOW + timedelta(minutes=3))
    merges: list[dict[str, Any]] = []

    from app.services import ctem_cases_store as store

    async def merge_cases(_session: object, **kw: Any) -> CtemCase:
        merges.append(kw)
        return first

    monkeypatch.setattr(store, "merge_cases", merge_cases)

    class _Session:
        async def execute(self, _statement: object, *_a: object, **_k: object) -> SimpleNamespace:
            rows = [second, first]
            return SimpleNamespace(scalars=lambda: SimpleNamespace(all=lambda: rows))

        def begin_nested(self) -> _Savepoint:
            return _Savepoint()

    events = loop.PendingEvents()
    merged = await loop.merge_open_duplicates(_db(_Session()), vertical="cyber", events=events, now=NOW)
    assert merged == 1
    assert len(merges) == 1
    call = merges[0]
    assert call["target_id"] == first.id and call["source_ids"] == [second.id]
    assert call["actor"] == rules.SYSTEM_ACTOR and call["reason"] == loop.DUPLICATE_MERGE_REASON
    assert "cyber" in events.verticals


# --- The sweep never waits on the writer lock ---------------------------------


async def _sweep_with_lock(monkeypatch: pytest.MonkeyPatch, taken: bool) -> tuple[list[str], dict[str, int]]:
    """Run the sweep on PostgreSQL with the writer lock *taken* or busy; the passes it ran."""
    from app.services import ctem_case_asset_retire, ctem_case_owner, ctem_case_stall
    from app.services import ctem_case_opening as case_opening

    ran: list[str] = []

    async def try_lock(_db: object, _vertical: str) -> bool:
        ran.append("try_lock")
        return taken

    async def hold_lock(_db: object, _vertical: str) -> None:
        raise AssertionError("the sweep must not wait on the writer lock")

    async def may_open(_db: object, _vertical: str) -> bool:
        return True

    async def open_waiting(_db: object, **_k: object) -> None:
        ran.append("open")

    async def merge(_db: object, **_k: object) -> int:
        ran.append("merge")
        return 2

    async def zero(*_a: object, **_k: object) -> int:
        return 0

    monkeypatch.setattr(case_opening, "try_vertical_lock", try_lock)
    monkeypatch.setattr(case_opening, "hold_vertical_lock", hold_lock)
    monkeypatch.setattr(case_opening, "may_open_waiting", may_open)
    monkeypatch.setattr(loop, "open_waiting", open_waiting)
    monkeypatch.setattr(loop, "merge_open_duplicates", merge)
    monkeypatch.setattr(loop, "announce_unannounced", zero)
    monkeypatch.setattr(ctem_case_owner, "backfill_owners", zero)
    monkeypatch.setattr(ctem_case_asset_retire, "close_retired_asset_cases", zero)
    monkeypatch.setattr(ctem_case_stall, "sweep_stalled", zero)

    class _Session(_PgSession):
        async def execute(self, statement: object, params: dict[str, Any] | None = None) -> SimpleNamespace:
            ran.append("sla")
            return SimpleNamespace(scalars=lambda: SimpleNamespace(all=lambda: []))

    counts = await loop.sweep(_db(_Session()), events=loop.PendingEvents(), now=NOW)
    return ran, counts


@pytest.mark.asyncio
async def test_a_busy_writer_lock_skips_opening_and_merge_and_keeps_the_other_passes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ran, counts = await _sweep_with_lock(monkeypatch, taken=False)
    assert ran == ["try_lock", "sla"]
    assert counts["merged"] == 0 and counts["opened"] == 0


@pytest.mark.asyncio
async def test_a_free_writer_lock_runs_opening_and_merge(monkeypatch: pytest.MonkeyPatch) -> None:
    ran, counts = await _sweep_with_lock(monkeypatch, taken=True)
    assert ran == ["try_lock", "open", "sla", "merge"]
    assert counts["merged"] == 2
