"""Shared-state entries held as leases by a run.

``set_shared_state(hold="take")`` records the run the tool runs in as the
entry's holder. Another run's take is refused while that run lives and takes
the entry over once it ended; the scheduler tick releases the holds of runs
that ended (``release_ended_run_holds``), so a lease whose holder failed is
free without any agent-side sweep.
"""

import uuid
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

import app.services.shared_state as shared_state_service
from app.services.builtin_tools import build_shared_state_tools
from app.services.company_managed_writes import agent_writer
from app.services.shared_state import SharedStateHeldError
from app.services.work_item import HoldingRun
from app.tools.base import ToolCall

COMPANY_AGENT = agent_writer(is_company_managed=True)
HOLDER = uuid.uuid4()
OTHER = uuid.uuid4()


def _state(held_by=None) -> SimpleNamespace:
    return SimpleNamespace(
        id=uuid.uuid4(),
        user_id=None,
        is_company_managed=True,
        workspace_id=None,
        key="dev:branch-lock:feature/x",
        label=None,
        owner_agent_id=None,
        held_by_run_id=held_by,
        status="active",
        value={"state": "held"},
        notes=[],
        resolved_at=None,
        created_at=datetime(2026, 11, 2, tzinfo=UTC),
        updated_at=datetime(2026, 11, 2, tzinfo=UTC),
    )


class _Result:
    def __init__(self, value=None, rows=None, rowcount=0):
        self._value = value
        self._rows = rows or []
        self.rowcount = rowcount

    def scalars(self):
        return self

    def first(self):
        return self._value

    def all(self):
        return self._rows


class _Session:
    """The key lookup and the row lock both answer *existing*."""

    def __init__(self, existing):
        self._existing = existing
        self.committed = False

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def get(self, _model, _ident):
        return None

    async def execute(self, _statement):
        return _Result(self._existing)

    def add(self, obj):
        self._existing = obj

    async def commit(self):
        self.committed = True

    async def rollback(self):
        return None

    async def refresh(self, obj):
        if getattr(obj, "id", None) is None:
            obj.id = uuid.uuid4()


def _holders(monkeypatch, **by_run):
    async def fake(_db, run_ids):
        return {rid: by_run.get(str(rid), HoldingRun(None, None, None)) for rid in run_ids if rid}

    monkeypatch.setattr("app.services.work_item.holding_runs", fake)


def _run(status: str) -> HoldingRun:
    return HoldingRun(status=status, ended_at=None, kind="run")


async def _upsert(monkeypatch, state, **kw):
    monkeypatch.setattr("app.core.database.async_session", lambda: _Session(state))
    return await shared_state_service.upsert_state(
        user_id=str(uuid.uuid4()), key=state.key, writer=COMPANY_AGENT, **kw
    )


# ── upsert_state(hold=...) ─────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_take_records_the_run_and_answers_it(monkeypatch):
    state = _state()
    state.is_lease = True
    _holders(monkeypatch)
    out = await _upsert(monkeypatch, state, value={"state": "held"}, hold="take", run_id=HOLDER)
    assert state.held_by_run_id == HOLDER
    assert out["held_by_run_id"] == str(HOLDER)


@pytest.mark.asyncio
async def test_take_of_an_entry_a_live_run_holds_is_refused_and_writes_nothing(monkeypatch):
    state = _state(held_by=HOLDER)
    _holders(monkeypatch, **{str(HOLDER): _run("running")})
    with pytest.raises(SharedStateHeldError, match="still running") as raised:
        await _upsert(monkeypatch, state, value={"state": "mine"}, note="n", hold="take", run_id=OTHER)
    assert state.held_by_run_id == HOLDER
    assert state.value == {"state": "held"}
    assert state.notes == []
    assert raised.value.holder_run_id == HOLDER
    assert raised.value.holder_status == "running"
    assert raised.value.pending_fix is None


@pytest.mark.asyncio
async def test_a_held_refusal_carries_the_pending_fix_of_the_entry(monkeypatch):
    state = _state(held_by=HOLDER)
    state.value = {"state": "held", "pending_fix": {"by": "run r", "what": "rebase"}}
    _holders(monkeypatch, **{str(HOLDER): _run("running")})
    with pytest.raises(SharedStateHeldError) as raised:
        await _upsert(monkeypatch, state, value={}, hold="take", run_id=OTHER)
    assert raised.value.pending_fix == {"by": "run r", "what": "rebase"}


@pytest.mark.asyncio
async def test_take_of_an_entry_whose_holder_failed_takes_it_over_with_a_note(monkeypatch):
    state = _state(held_by=HOLDER)
    _holders(monkeypatch, **{str(HOLDER): _run("failed")})
    await _upsert(monkeypatch, state, value={"state": "mine"}, note="taking it", hold="take", run_id=OTHER)
    assert state.held_by_run_id == OTHER
    assert state.value == {"state": "mine"}
    texts = [n["note"] for n in state.notes]
    assert "taken over" in texts[0] and str(HOLDER) in texts[0] and "run failed" in texts[0]
    assert texts[1] == "taking it"


@pytest.mark.asyncio
async def test_take_of_an_entry_whose_holder_is_no_longer_recorded_takes_it_over(monkeypatch):
    state = _state(held_by=HOLDER)
    _holders(monkeypatch)
    await _upsert(monkeypatch, state, value={}, hold="take", run_id=OTHER)
    assert state.held_by_run_id == OTHER
    assert "no longer recorded" in state.notes[0]["note"]


@pytest.mark.asyncio
async def test_the_holder_takes_again_and_releases_without_a_note(monkeypatch):
    state = _state(held_by=HOLDER)
    _holders(monkeypatch, **{str(HOLDER): _run("running")})
    await _upsert(monkeypatch, state, value={"a": 1}, hold="take", run_id=HOLDER)
    assert state.held_by_run_id == HOLDER
    await _upsert(monkeypatch, state, value={"state": "free"}, hold="release", run_id=HOLDER)
    assert state.held_by_run_id is None
    assert state.notes == []


@pytest.mark.asyncio
async def test_release_by_another_run_is_refused_while_the_holder_lives(monkeypatch):
    state = _state(held_by=HOLDER)
    _holders(monkeypatch, **{str(HOLDER): _run("running")})
    with pytest.raises(SharedStateHeldError):
        await _upsert(monkeypatch, state, value={"state": "free"}, hold="release", run_id=OTHER)
    assert state.held_by_run_id == HOLDER


@pytest.mark.asyncio
async def test_a_write_without_hold_never_changes_the_holder(monkeypatch):
    state = _state(held_by=HOLDER)
    _holders(monkeypatch, **{str(HOLDER): _run("running")})
    await _upsert(monkeypatch, state, value={"last_push": "abc"}, run_id=OTHER)
    assert state.held_by_run_id == HOLDER
    assert state.value["last_push"] == "abc"


@pytest.mark.asyncio
async def test_take_without_a_run_and_an_unknown_action_are_refused(monkeypatch):
    with pytest.raises(ValueError, match="needs a run"):
        await _upsert(monkeypatch, _state(), value={}, hold="take")
    with pytest.raises(ValueError, match="take' or 'release"):
        await _upsert(monkeypatch, _state(), value={}, hold="grab", run_id=HOLDER)


# ── release_ended_run_holds ────────────────────────────────────────────────


class _SweepSession:
    def __init__(self, rows, rowcount=1):
        self.rows = rows
        self.rowcount = rowcount
        self.updates: list = []
        self.committed = False

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def execute(self, statement):
        if statement.is_select:
            return _Result(rows=self.rows)
        self.updates.append(statement)
        return _Result(rowcount=self.rowcount)

    async def commit(self):
        self.committed = True


@pytest.mark.asyncio
async def test_release_clears_the_hold_of_a_run_that_ended_with_a_note(monkeypatch):
    state_id = uuid.uuid4()
    session = _SweepSession([(state_id, HOLDER, [{"note": "old"}])])
    monkeypatch.setattr("app.core.database.async_session", lambda: session)
    _holders(monkeypatch, **{str(HOLDER): _run("failed")})
    assert await shared_state_service.release_ended_run_holds() == 1
    assert session.committed is True
    params = session.updates[0].compile().params
    assert params["held_by_run_id"] is None
    assert "hold released" in params["notes"][-1]["note"]
    assert "run failed" in params["notes"][-1]["note"]
    assert params["notes"][0] == {"note": "old"}


@pytest.mark.asyncio
async def test_release_skips_a_holder_that_came_back_alive(monkeypatch):
    session = _SweepSession([(uuid.uuid4(), HOLDER, [])])
    monkeypatch.setattr("app.core.database.async_session", lambda: session)
    _holders(monkeypatch, **{str(HOLDER): _run("running")})
    assert await shared_state_service.release_ended_run_holds() == 0
    assert session.updates == []


@pytest.mark.asyncio
async def test_release_scoped_to_unreadable_run_ids_releases_nothing(monkeypatch):
    def _never():
        raise AssertionError("no session for an empty scope")

    monkeypatch.setattr("app.core.database.async_session", _never)
    assert await shared_state_service.release_ended_run_holds(run_ids=[None, "x"]) == 0


# ── The tool ───────────────────────────────────────────────────────────────


async def _call(executor, name, /, **arguments):
    return await executor.execute_tool(ToolCall(id=str(uuid.uuid4()), name=name, arguments=arguments))


def test_set_shared_state_declares_hold():
    defs, _ = build_shared_state_tools(None, "", "")  # type: ignore[arg-type]
    schema = next(d for d in defs if d.name == "set_shared_state").input_schema
    assert schema["properties"]["hold"]["enum"] == ["take", "release"]


def _held_executor(monkeypatch, seen: dict, pending_fix=None):
    async def fake_upsert_state(**kw):
        seen.update(kw)
        raise SharedStateHeldError(
            "the shared state 'k' is held by run r, which is still running; nothing was written",
            holder_run_id=OTHER,
            holder_status="running",
            pending_fix=pending_fix,
        )

    monkeypatch.setattr(shared_state_service, "upsert_state", fake_upsert_state)
    _, executor = build_shared_state_tools(
        None, str(uuid.uuid4()), str(uuid.uuid4()), company_managed_agent=True, run_id=HOLDER  # type: ignore[arg-type]
    )
    return executor


@pytest.mark.asyncio
async def test_the_tool_passes_its_run_and_answers_a_held_lease_as_a_result(monkeypatch):
    seen: dict = {}
    executor = _held_executor(monkeypatch, seen, pending_fix={"what": "rebase on main"})
    result = await _call(executor, "set_shared_state", key="k", value_json="{}", hold="take")
    assert seen["hold"] == "take"
    assert seen["run_id"] == HOLDER
    # The expected outcome of a lease: no error, so no friction and no
    # repeated-failure count in the agent loop.
    assert result.is_error is False
    assert not result.content.startswith("Error")
    assert f"Held by live run {OTHER}; stand down or write a pending_fix" in result.content
    assert "held by run" in result.content
    assert "rebase on main" in result.content
    assert result.structured == {
        "held": True,
        "holder_run_id": str(OTHER),
        "holder_status": "running",
        "pending_fix": {"what": "rebase on main"},
    }


@pytest.mark.asyncio
async def test_two_held_answers_record_no_friction(monkeypatch):
    from app.tools.agent_loop import _log_tool_result

    errors: list = []
    monkeypatch.setattr("app.services.frictions.note_tool_error", lambda *a, **k: errors.append(a))
    executor = _held_executor(monkeypatch, {})
    for _ in range(2):
        call = ToolCall(id=str(uuid.uuid4()), name="set_shared_state", arguments={"key": "k", "hold": "take"})
        result = await executor.execute_tool(call)
        assert result.is_error is False
        _log_tool_result(call, result)
    assert errors == []


def test_the_hold_description_says_a_held_lease_is_not_an_error():
    defs, _ = build_shared_state_tools(None, "", "")  # type: ignore[arg-type]
    schema = next(d for d in defs if d.name == "set_shared_state").input_schema
    assert "held, not an error" in schema["properties"]["hold"]["description"]


@pytest.mark.asyncio
async def test_get_shared_state_shows_a_holder_that_ended(monkeypatch):
    async def fake_get_state(**_kw):
        return {
            "key": "k",
            "status": "active",
            "value": {"a": 1},
            "notes": [],
            "held_by_run_id": str(HOLDER),
            "is_company_managed": True,
        }

    class _Ctx:
        async def __aenter__(self):
            return object()

        async def __aexit__(self, *exc):
            return False

    monkeypatch.setattr(shared_state_service, "get_state", fake_get_state)
    monkeypatch.setattr("app.core.database.async_session", lambda: _Ctx())
    _holders(monkeypatch, **{str(HOLDER): _run("failed")})
    _, executor = build_shared_state_tools(
        None, str(uuid.uuid4()), str(uuid.uuid4()), company_managed_agent=True  # type: ignore[arg-type]
    )
    result = await _call(executor, "get_shared_state", key="k")
    text = str(getattr(result, "content", result))
    assert f"held by run {HOLDER}, which ENDED (failed)" in text
