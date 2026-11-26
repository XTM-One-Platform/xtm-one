"""Shared state hygiene (#11696): leases end with their hold, lists and archives
are capped, an entry knows what ends it, staleness reads the value's age, and
the sweep resolves and compacts what agents leave behind.
"""

import uuid
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

import app.services.shared_state as shared_state_service
from app.services.builtin_tools import build_shared_state_tools
from app.services.shared_state import (
    ARCHIVE_STAMP,
    compact_value,
    is_shared_state_stale,
    list_cap_refusal,
    serialize_state,
    value_size,
)
from app.services.work_item import HoldingRun
from tests.test_shared_state_hold_7519 import (
    HOLDER,
    _holders,
    _run,
    _state,
    _SweepSession,
    _upsert,
)
from tests.test_shared_state_list_ops import COMPANY_AGENT, _call, _Session
from tests.test_shared_state_list_ops import _state as _list_state


@pytest.fixture
def caps(monkeypatch):
    def _set(*, hard: int = 0, items: int = 0, soft: int = 0) -> None:
        monkeypatch.setattr(shared_state_service.settings, "shared_state_value_soft_limit_chars", soft)
        monkeypatch.setattr(shared_state_service.settings, "shared_state_value_hard_limit_chars", hard)
        monkeypatch.setattr(shared_state_service.settings, "shared_state_list_max_items", items)

    return _set


# ── Leases ─────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_an_entry_created_by_a_take_is_a_lease_and_resolves_at_release(monkeypatch):
    created = _state()
    created.id = None  # a new row: the take creates it
    _holders(monkeypatch, **{str(HOLDER): _run("running")})
    await _upsert(monkeypatch, created, value={"pr": 12}, hold="take", run_id=HOLDER)
    assert created.is_lease is True
    assert created.status == "active"
    await _upsert(monkeypatch, created, value={}, hold="release", run_id=HOLDER)
    assert created.held_by_run_id is None
    assert created.status == "resolved"
    assert created.resolved_at is not None
    assert "lease released" in created.notes[-1]["note"]


@pytest.mark.asyncio
async def test_a_take_on_an_existing_record_is_not_a_lease_and_stays_active(monkeypatch):
    # A take on a record needs lease=false said on purpose (#2208).
    state = _state()
    state.is_lease = None
    _holders(monkeypatch, **{str(HOLDER): _run("running")})
    await _upsert(monkeypatch, state, value={"a": 1}, hold="take", run_id=HOLDER, lease=False)
    assert not state.is_lease
    await _upsert(monkeypatch, state, value={}, hold="release", run_id=HOLDER)
    assert state.status == "active"


@pytest.mark.asyncio
async def test_lease_false_keeps_a_created_record_active_after_release(monkeypatch):
    created = _state()
    created.id = None
    _holders(monkeypatch, **{str(HOLDER): _run("running")})
    await _upsert(monkeypatch, created, value={"x": 1}, hold="take", run_id=HOLDER, lease=False)
    await _upsert(monkeypatch, created, value={}, hold="release", run_id=HOLDER)
    assert created.is_lease is False
    assert created.status == "active"


@pytest.mark.asyncio
async def test_the_tick_resolves_a_lease_whose_holder_ended(monkeypatch):
    session = _SweepSession([(uuid.uuid4(), HOLDER, [], True)])
    monkeypatch.setattr("app.core.database.async_session", lambda: session)
    _holders(monkeypatch, **{str(HOLDER): _run("completed")})
    assert await shared_state_service.release_ended_run_holds() == 1
    params = session.updates[0].compile().params
    assert params["held_by_run_id"] is None
    assert params["status"] == "resolved"
    assert "lease entry resolved" in params["notes"][-1]["note"]


@pytest.mark.asyncio
async def test_the_tick_keeps_a_held_record_active(monkeypatch):
    session = _SweepSession([(uuid.uuid4(), HOLDER, [], None)])
    monkeypatch.setattr("app.core.database.async_session", lambda: session)
    _holders(monkeypatch, **{str(HOLDER): HoldingRun(status="failed", ended_at=None, kind="run")})
    assert await shared_state_service.release_ended_run_holds() == 1
    params = session.updates[0].compile().params
    assert "status" not in params


# ── Caps ───────────────────────────────────────────────────────────────────


def test_a_list_growing_past_the_cap_is_refused_with_the_fix(caps):
    caps(items=3)
    refusal = list_cap_refusal({"runs": [1, 2, 3]}, {"runs": [1, 2, 3, 4]})
    assert refusal and "cap of 3 items" in refusal and "keep_last_json" in refusal
    # Already over the cap and shrinking: the compaction lands.
    assert list_cap_refusal({"runs": list(range(9))}, {"runs": list(range(5))}) is None
    caps(items=0)
    assert list_cap_refusal({}, {"runs": list(range(9))}) is None


@pytest.mark.asyncio
async def test_upsert_refuses_a_list_over_the_cap_and_commits_nothing(monkeypatch, caps):
    caps(items=3)
    session = _Session(_list_state({"runs": [{"n": 1}]}))
    monkeypatch.setattr("app.core.database.async_session", lambda: session)
    with pytest.raises(ValueError, match="cap of 3 items"):
        await shared_state_service.upsert_state(
            user_id=str(uuid.uuid4()),
            key="metrics",
            append={"runs": [{"n": i} for i in range(2, 6)]},
            writer=COMPANY_AGENT,
            is_company_managed=True,
        )
    assert session.committed is False


@pytest.mark.asyncio
async def test_an_archive_drops_its_oldest_items_past_the_list_cap(monkeypatch, caps):
    caps(items=4)
    old = [{"n": i, ARCHIVE_STAMP: "2026-11-01T00:00:00+00:00"} for i in range(4)]
    session = _Session(_list_state({"runs": old}))
    monkeypatch.setattr("app.core.database.async_session", lambda: session)
    out = await shared_state_service.upsert_state(
        user_id=str(uuid.uuid4()),
        key="metrics-archive",
        append={"runs": [{"n": 10}, {"n": 11}]},
        writer=COMPANY_AGENT,
        is_company_managed=True,
        archive=True,
    )
    assert [r["n"] for r in out["value"]["runs"]] == [2, 3, 10, 11]
    assert out["archive_trimmed"] == 2


def test_compact_value_cuts_lists_to_the_cap_and_the_size_to_the_limit(caps):
    caps(items=5, hard=200)
    value = {"runs": [{"n": i, "t": "x" * 20} for i in range(12)], "meta": {"k": 1}}
    out, dropped = compact_value(value)
    assert value_size(out) <= 200
    assert out["meta"] == {"k": 1}
    assert dropped == 12 - len(out["runs"])
    assert out["runs"][-1]["n"] == 11  # the newest stay


def test_compact_value_leaves_a_value_within_limits_alone(caps):
    caps(items=50, hard=10_000)
    value = {"runs": [1, 2, 3]}
    assert compact_value(value) == (value, 0)


@pytest.mark.asyncio
async def test_archiving_from_an_archive_is_refused(monkeypatch):
    archive = _list_state({"runs": [{"n": 1, ARCHIVE_STAMP: "2026-11-01T00:00:00+00:00"}]})

    async def fake_get_state(**_kw):
        return {"key": archive.key, "value": archive.value}

    async def never_upsert(**_kw):
        raise AssertionError("nothing may be written")

    monkeypatch.setattr(shared_state_service, "get_state", fake_get_state)
    monkeypatch.setattr(shared_state_service, "upsert_state", never_upsert)
    _, executor = build_shared_state_tools(SimpleNamespace(), str(uuid.uuid4()), str(uuid.uuid4()))
    result = await _call(
        executor,
        "set_shared_state",
        key="metrics-archive",
        keep_last_json='{"runs": 0}',
        confirm_trim=True,
        archive_key="metrics-archive-older",
    )
    assert "is itself an archive" in result.content and "Nothing was written" in result.content


# ── Lifecycle and liveness ─────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_lifecycle_fields_are_written_and_cleared(monkeypatch):
    state = _state()
    _holders(monkeypatch)
    item = uuid.uuid4()
    await _upsert(
        monkeypatch,
        state,
        value={"a": 1},
        linked_work_item_id=str(item),
        expires_at="2026-12-01T09:00:00Z",
        terminal_condition="the release is cut",
    )
    assert state.linked_work_item_id == item
    assert state.expires_at == datetime(2026, 12, 1, 9, tzinfo=UTC)
    assert state.terminal_condition == "the release is cut"
    await _upsert(monkeypatch, state, value={"a": 1}, linked_work_item_id="", expires_at="", terminal_condition="")
    assert state.linked_work_item_id is None
    assert state.expires_at is None
    assert state.terminal_condition is None


@pytest.mark.asyncio
async def test_bad_lifecycle_arguments_are_refused(monkeypatch):
    _holders(monkeypatch)
    with pytest.raises(ValueError, match="full UUID"):
        await _upsert(monkeypatch, _state(), value={}, linked_work_item_id="4228cd03")
    with pytest.raises(ValueError, match="ISO-8601"):
        await _upsert(monkeypatch, _state(), value={}, expires_at="tomorrow")


@pytest.mark.asyncio
async def test_a_note_alone_never_moves_the_value_age(monkeypatch):
    state = _state()
    stamp = datetime(2026, 11, 1, tzinfo=UTC)
    state.value_updated_at = stamp
    _holders(monkeypatch)
    await _upsert(monkeypatch, state, note="still waiting")
    assert state.value_updated_at == stamp
    await _upsert(monkeypatch, state, value={"state": "held"})  # same value
    assert state.value_updated_at == stamp
    await _upsert(monkeypatch, state, value={"state": "moved"})
    assert state.value_updated_at > stamp


def _entry(**kw) -> SimpleNamespace:
    base = {
        "status": "active",
        "created_at": datetime(2026, 10, 1, tzinfo=UTC),
        "updated_at": datetime(2026, 11, 13, tzinfo=UTC),
        "value_updated_at": None,
        "expires_at": None,
    }
    base.update(kw)
    return SimpleNamespace(**base)


def test_staleness_reads_the_value_age_not_the_last_note(monkeypatch):
    monkeypatch.setattr(shared_state_service.settings, "shared_state_stale_after_days", 14)
    now = datetime(2026, 11, 14, tzinfo=UTC)
    kept_warm = _entry(value_updated_at=datetime(2026, 10, 2, tzinfo=UTC))
    assert is_shared_state_stale(kept_warm, now) is True
    fresh = _entry(value_updated_at=datetime(2026, 11, 10, tzinfo=UTC))
    assert is_shared_state_stale(fresh, now) is False
    legacy = _entry()  # no value stamp yet: its last write counts, as before
    assert is_shared_state_stale(legacy, now) is False


def test_an_entry_past_its_date_is_stale_even_with_staleness_off(monkeypatch):
    monkeypatch.setattr(shared_state_service.settings, "shared_state_stale_after_days", 0)
    now = datetime(2026, 11, 14, tzinfo=UTC)
    assert is_shared_state_stale(_entry(expires_at=now - timedelta(hours=1)), now) is True
    assert is_shared_state_stale(_entry(expires_at=now + timedelta(hours=1)), now) is False
    assert is_shared_state_stale(_entry(status="resolved", expires_at=now - timedelta(days=1)), now) is False


def test_serialize_state_carries_the_lifecycle_and_the_value_age():
    state = _state()
    state.is_lease = True
    state.linked_work_item_id = uuid.uuid4()
    state.expires_at = datetime(2026, 12, 1, tzinfo=UTC)
    state.terminal_condition = "pr merged"
    state.value_updated_at = datetime(2026, 11, 4, tzinfo=UTC)
    out = serialize_state(state, now=datetime(2026, 11, 14, tzinfo=UTC))
    assert out["is_lease"] is True
    assert out["linked_work_item_id"] == str(state.linked_work_item_id)
    assert out["expires_at"].startswith("2026-12-01")
    assert out["terminal_condition"] == "pr merged"
    assert out["value_age_days"] == 10


@pytest.mark.asyncio
async def test_the_listing_shows_the_value_age_and_what_ends_each_entry(monkeypatch):
    async def _list(**_kwargs):
        return [
            {
                "key": "a",
                "status": "active",
                "value_age_days": 3,
                "terminal_condition": "release cut",
                "is_lease": True,
            },
            {"key": "b", "status": "active", "value_age_days": 40},
        ]

    async def _count(**_kwargs):
        return 2

    from app.services import coordination_scope

    async def _scope(*_args):
        return SimpleNamespace(label=lambda _w: "", shared_state_clause=lambda: None)

    monkeypatch.setattr(shared_state_service, "list_states", _list)
    monkeypatch.setattr(shared_state_service, "count_states", _count)
    monkeypatch.setattr(coordination_scope, "resolve_coordination_scope", _scope)
    _, executor = build_shared_state_tools(SimpleNamespace(), str(uuid.uuid4()), str(uuid.uuid4()))
    result = await _call(executor, "list_shared_state")
    assert not result.is_error, result.content
    assert "`a` [active, lease]" in result.content
    assert "value unchanged 3d; ends: release cut" in result.content
    assert "value unchanged 40d; ends: not set" in result.content


# ── The sweep ──────────────────────────────────────────────────────────────


class _LinkSession(_SweepSession):
    """Answers the entries, then the linked items' statuses."""

    def __init__(self, rows, statuses):
        super().__init__(rows)
        self.statuses = statuses
        self.selects = 0

    async def execute(self, statement):
        if statement.is_select:
            self.selects += 1
            from tests.test_shared_state_hold_7519 import _Result

            return _Result(rows=self.rows if self.selects == 1 else list(self.statuses.items()))
        return await super().execute(statement)


@pytest.mark.asyncio
async def test_an_entry_whose_linked_item_ended_is_resolved_and_a_live_one_kept(monkeypatch):
    done, open_ = uuid.uuid4(), uuid.uuid4()
    session = _LinkSession(
        [(uuid.uuid4(), done, []), (uuid.uuid4(), open_, [])],
        {done: "done", open_: "in_progress"},
    )
    monkeypatch.setattr("app.core.database.async_session", lambda: session)
    assert await shared_state_service.resolve_terminal_linked_states() == 1
    params = session.updates[0].compile().params
    assert params["status"] == "resolved"
    assert f"linked work item {done} is done" in params["notes"][-1]["note"]


@pytest.mark.asyncio
async def test_an_entry_past_its_date_is_resolved(monkeypatch):
    session = _SweepSession([(uuid.uuid4(), datetime(2026, 11, 1, tzinfo=UTC), [])])
    monkeypatch.setattr("app.core.database.async_session", lambda: session)
    assert await shared_state_service.resolve_expired_states() == 1
    params = session.updates[0].compile().params
    assert params["status"] == "resolved"
    assert "expired at 2026-11-01" in params["notes"][-1]["note"]


@pytest.mark.asyncio
async def test_the_sweep_runs_every_part_even_when_one_fails(monkeypatch):
    async def boom(**_kw):
        raise RuntimeError("db down")

    async def one(**_kw):
        return 1

    monkeypatch.setattr(shared_state_service, "resolve_terminal_linked_states", boom)
    monkeypatch.setattr(shared_state_service, "resolve_expired_states", one)
    monkeypatch.setattr(shared_state_service, "compact_oversize_states", one)
    monkeypatch.setattr(shared_state_service, "report_shared_state_hygiene", one)
    out = await shared_state_service.sweep_shared_state_lifecycle()
    assert out == {"linked_resolved": 0, "expired_resolved": 1, "items_compacted": 1, "orchestrator_items": 1}


# ── Fleet hygiene counts and the orchestrator report ─────────────────────────


class _CountSession:
    """Answers one aggregate row; records the statement."""

    def __init__(self, row):
        self.row = row
        self.statements = []

    async def execute(self, statement):
        self.statements.append(statement)
        from tests.test_shared_state_hold_7519 import _Result

        return _Result(value=self.row)


@pytest.mark.asyncio
async def test_hygiene_counts_read_one_aggregate_row(caps):
    caps(soft=100)
    session = _CountSession((2, 3, 1, 4))
    counts = await shared_state_service.shared_state_hygiene_counts(session, shared_state_service.true())
    assert counts == {"oversize": 2, "stale": 3, "orphaned_leases": 1, "terminal_linked": 4}
    assert len(session.statements) == 1


@pytest.mark.asyncio
async def test_hygiene_counts_of_an_empty_scope_are_zero(caps):
    caps()
    counts = await shared_state_service.shared_state_hygiene_counts(
        _CountSession((None, None, None, None)), shared_state_service.true()
    )
    assert counts == dict.fromkeys(shared_state_service.SHARED_STATE_HYGIENE_FIELDS, 0)


def test_the_report_names_the_counts_and_the_fixes(caps):
    caps(soft=32768)
    text = shared_state_service.hygiene_report_description("Ops", {"oversize": 2, "stale": 5})
    assert "'Ops'" in text and "Oversize (value over 32768 characters): 2" in text
    assert "keep_last_json with archive_key" in text and "Stale" in text and ": 5." in text


class _FleetSession:
    def __init__(self, fleets):
        self.fleets = fleets

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def execute(self, _statement):
        from tests.test_shared_state_hold_7519 import _Result

        return _Result(rows=self.fleets)


@pytest.mark.asyncio
async def test_the_report_files_one_deduped_item_per_fleet_with_work_left(monkeypatch):
    dirty, clean, seeded = (uuid.uuid4() for _ in range(3))
    owner, orchestrator, ws = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    fleets = [
        (dirty, "Custom", owner, ws, orchestrator),
        (clean, "Clean", owner, ws, orchestrator),
        (seeded, "Seeded", None, ws, orchestrator),
    ]
    monkeypatch.setattr("app.core.database.async_session", lambda: _FleetSession(fleets))
    by_fleet = {
        dirty: {"oversize": 1, "stale": 2, "orphaned_leases": 0, "terminal_linked": 0},
        clean: {"oversize": 0, "stale": 0, "orphaned_leases": 3, "terminal_linked": 1},
        seeded: {"oversize": 0, "stale": 1, "orphaned_leases": 0, "terminal_linked": 0},
    }

    async def counts(_db, scope, **_kw):
        return by_fleet[scope]

    monkeypatch.setattr(shared_state_service, "shared_state_hygiene_counts", counts)
    monkeypatch.setattr("app.services.coordination_scope.fleet_shared_states_clause", lambda fid: fid)
    calls = []

    async def fake_create(**kw):
        calls.append(kw)
        return {"id": str(uuid.uuid4()), "deduplicated": kw["correlation_key"].endswith(str(seeded))}

    monkeypatch.setattr("app.services.work_item.create_work_item", fake_create)
    assert await shared_state_service.report_shared_state_hygiene() == 1
    assert [c["correlation_key"] for c in calls] == [
        f"shared-state-hygiene:{dirty}",
        f"shared-state-hygiene:{seeded}",
    ]
    first, second = calls
    assert first["owner_agent_id"] == str(orchestrator) and first["dedupe"] and first["dedupe_by"] == "key"
    assert first["user_id"] == str(owner) and not first["is_company_managed"]
    assert second["user_id"] is None and second["is_company_managed"]
    assert first["payload"]["shared_state_hygiene"]["stale"] == 2


@pytest.mark.asyncio
async def test_one_fleets_refusal_never_stops_the_others(monkeypatch):
    a, b = uuid.uuid4(), uuid.uuid4()
    fleets = [(a, "A", uuid.uuid4(), uuid.uuid4(), uuid.uuid4()), (b, "B", uuid.uuid4(), uuid.uuid4(), uuid.uuid4())]
    monkeypatch.setattr("app.core.database.async_session", lambda: _FleetSession(fleets))

    async def counts(_db, _scope, **_kw):
        return {"oversize": 1, "stale": 0, "orphaned_leases": 0, "terminal_linked": 0}

    monkeypatch.setattr(shared_state_service, "shared_state_hygiene_counts", counts)
    monkeypatch.setattr("app.services.coordination_scope.fleet_shared_states_clause", lambda fid: fid)

    async def fake_create(**kw):
        if kw["correlation_key"].endswith(str(a)):
            raise ValueError("workspace archived")
        return {"id": "x"}

    monkeypatch.setattr("app.services.work_item.create_work_item", fake_create)
    assert await shared_state_service.report_shared_state_hygiene() == 1
