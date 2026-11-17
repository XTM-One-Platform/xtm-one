"""A work item held by a run is released when that run ends.

Covers the four parts of the change: the claim records the run that holds it,
the scheduler sweep releases the claims of runs that ended (with a dated note,
the item's own trail untouched), a claimant takes over an item whose holder
ended while one held by a live run is still refused, and the inbox records the
run it dispatched as the holder. No database: the service functions open
their own sessions, replaced here by queued fakes, as in
``test_work_item_lifecycle.py``.
"""

import re
import uuid
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from sqlalchemy.dialects import postgresql

import app.services.work_item as wi_service
from app.services.company_managed_writes import agent_writer
from app.tools.base import ToolCall

PERSONAL_AGENT = agent_writer(is_company_managed=False)


def _item(**overrides):
    now = datetime.now(UTC)
    base = dict(
        id=uuid.uuid4(),
        user_id=uuid.uuid4(),
        is_company_managed=False,
        workspace_id=None,
        owner_agent_id=None,
        created_by_agent_id=None,
        title="Fix the thing",
        description="PROGRESS: step 1 done",
        payload={"k": "v"},
        attachment_file_ids=None,
        status="in_progress",
        priority="high",
        correlation_key="dev:thing",
        due_at=None,
        claimed_by_agent_id=uuid.uuid4(),
        claimed_by_run_id=uuid.uuid4(),
        claim_notes=None,
        claimed_at=now - timedelta(hours=2),
        source="agent",
        result=None,
        action_mode=None,
        last_activity_at=now,
        created_at=now,
        updated_at=now,
    )
    base.update(overrides)
    return SimpleNamespace(**base)


class _Result:
    def __init__(self, rows=None, one=None, rowcount=0):
        self._rows = rows or []
        self._one = one
        self.rowcount = rowcount

    def scalars(self):
        return self

    def all(self):
        return self._rows

    def scalar_one_or_none(self):
        return self._one


class _Session:
    """Async-context session answering a queue of results, one per execute."""

    def __init__(self, results):
        self._results = list(results)
        self.statements: list = []
        self.committed = False

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def get(self, _model, _ident):
        return None  # no workspace tier for the caller

    async def execute(self, statement):
        self.statements.append(statement)
        if not self._results:
            raise AssertionError("unexpected extra execute() call")
        return self._results.pop(0)

    async def commit(self):
        self.committed = True


# ``NOT (EXISTS (...) OR EXISTS (...))``, however SQLAlchemy parenthesises it.
_NOT_ALIVE = re.compile(r"not\s*\(+\s*exists")


def _sql(statement) -> str:
    return str(statement.compile(dialect=postgresql.dialect())).lower()


def _params(statement) -> dict:
    return statement.compile(dialect=postgresql.dialect()).params


# -- The holder's state ---------------------------------------------------------


def test_the_live_run_statuses_are_the_engines_active_and_queued_ones():
    from app.services.assignment_engine import ACTIVE_RUN_STATUSES, QUEUED_RUN_STATUS

    assert set(wi_service.LIVE_RUN_STATUSES) == {*ACTIVE_RUN_STATUSES, QUEUED_RUN_STATUS}


@pytest.mark.parametrize(
    ("kind", "status", "alive"),
    [
        ("run", "running", True),
        ("run", "awaiting_human", True),
        ("run", "queued", True),
        ("run", "completed", False),
        ("run", "failed", False),
        ("run", "cancelled", False),
        ("background_task", "running", True),
        ("background_task", "incomplete", False),
        (None, None, False),
    ],
)
def test_a_holder_is_alive_only_in_a_live_status(kind, status, alive):
    assert wi_service.HoldingRun(status=status, ended_at=None, kind=kind).alive is alive


@pytest.mark.asyncio
async def test_holding_runs_reads_runs_then_tasks_and_answers_every_id():
    run_id, task_id, gone_id = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    ended = datetime(2026, 10, 31, 6, 0, tzinfo=UTC)
    db = _Session([
        _Result(rows=[(run_id, "cancelled", ended)]),
        _Result(rows=[(task_id, "running", None)]),
    ])
    out = await wi_service.holding_runs(db, [run_id, task_id, gone_id, None])
    assert out[run_id] == wi_service.HoldingRun("cancelled", ended, "run")
    assert out[task_id].kind == "background_task" and out[task_id].alive
    assert out[gone_id].status is None and not out[gone_id].alive


@pytest.mark.asyncio
async def test_holding_runs_reads_nothing_without_an_id():
    assert await wi_service.holding_runs(_Session([]), [None]) == {}


def test_the_item_says_which_run_holds_it_and_whether_it_lives():
    item = _item()
    holders = {
        item.claimed_by_run_id: wi_service.HoldingRun("running", None, "run"),
    }
    out = wi_service.serialize_work_item(item, holders=holders)
    assert out["claimed_by_run_id"] == str(item.claimed_by_run_id)
    assert out["claimed_by_run"] == {
        "id": str(item.claimed_by_run_id),
        "kind": "run",
        "status": "running",
        "alive": True,
    }
    assert out["claim_notes"] == []
    # Without the holders read, the run is named but its state is not guessed.
    assert wi_service.serialize_work_item(item)["claimed_by_run"] is None


# -- Release when the holding run ended ----------------------------------------


@pytest.mark.asyncio
async def test_release_reopens_the_items_of_a_run_that_ended(monkeypatch):
    item_id, run_id = uuid.uuid4(), uuid.uuid4()
    ended = datetime(2026, 10, 31, 6, 33, tzinfo=UTC)
    earlier = [{"at": "2026-10-30T10:00:00+00:00", "text": "an older note"}]
    session = _Session([
        _Result(rows=[(item_id, run_id, earlier)]),  # held claims of dead runs
        _Result(rows=[(run_id, "cancelled", ended)]),  # holding_runs: runs
        _Result(rowcount=1),  # the release
    ])
    monkeypatch.setattr("app.core.database.async_session", lambda: session)

    assert await wi_service.release_ended_run_claims() == 1
    assert session.committed is True

    select_sql = _sql(session.statements[0])
    assert "claimed_by_run_id is not null" in select_sql
    assert _NOT_ALIVE.search(select_sql)  # the holder is not alive
    assert "assignment_runs" in select_sql and "sub_agent_tasks" in select_sql
    assert "action_mode is null" in select_sql

    release = session.statements[2]
    sql, params = _sql(release), _params(release)
    # The write re-applies the predicate: the same holder, still not alive.
    assert "work_items.claimed_by_run_id =" in sql and _NOT_ALIVE.search(sql)
    assert params["status"] == "open"
    assert params["claimed_by_agent_id"] is None
    assert params["claimed_by_run_id"] is None
    notes = params["claim_notes"]
    assert notes[0] == earlier[0]
    assert notes[1]["text"] == (
        f"released: run {run_id} ended cancelled at 2026-10-31T06:33:00Z, "
        "progress trail kept"
    )
    # The item's own trail is never written by the release.
    for untouched in ("description", "payload", "result", "correlation_key", "title"):
        assert untouched not in params


@pytest.mark.asyncio
async def test_release_names_a_run_that_is_no_longer_recorded(monkeypatch):
    item_id, run_id = uuid.uuid4(), uuid.uuid4()
    session = _Session([
        _Result(rows=[(item_id, run_id, None)]),
        _Result(rows=[]),  # not an assignment run
        _Result(rows=[]),  # nor a background task: purged
        _Result(rowcount=1),
    ])
    monkeypatch.setattr("app.core.database.async_session", lambda: session)

    assert await wi_service.release_ended_run_claims() == 1
    note = _params(session.statements[3])["claim_notes"][0]["text"]
    assert f"released: run {run_id} is no longer recorded at" in note


@pytest.mark.asyncio
async def test_release_only_touches_items_still_held(monkeypatch):
    """An item the run completed, cancelled or blocked keeps its status."""
    session = _Session([_Result(rows=[])])
    monkeypatch.setattr("app.core.database.async_session", lambda: session)

    assert await wi_service.release_ended_run_claims() == 0
    sql = _sql(session.statements[0])
    params = _params(session.statements[0])
    held = [v for v in params.values() if isinstance(v, (list, tuple))]
    assert ["claimed", "in_progress"] in [list(v) for v in held]
    assert "done" not in str(held) and "blocked" not in str(held)
    assert "work_items.status in" in sql


@pytest.mark.asyncio
async def test_release_counts_only_the_rows_it_wrote(monkeypatch):
    """Completed or taken over between the read and the write: not counted."""
    item_id, run_id = uuid.uuid4(), uuid.uuid4()
    session = _Session([
        _Result(rows=[(item_id, run_id, None)]),
        _Result(rows=[(run_id, "failed", None)]),
        _Result(rowcount=0),
    ])
    monkeypatch.setattr("app.core.database.async_session", lambda: session)
    assert await wi_service.release_ended_run_claims() == 0


# -- Take-over by a claimant ----------------------------------------------------


def _claim_session(item, *results):
    return _Session([_Result(one=item), *results])


async def _passthrough(work_item_id, *, user_id):
    return {"id": work_item_id}


@pytest.mark.asyncio
async def test_a_claim_takes_over_an_item_whose_run_ended(monkeypatch):
    item = _item()
    new_run = uuid.uuid4()
    session = _claim_session(
        item,
        _Result(one=None),  # the open -> claimed flip: not open
        _Result(rows=[(item.claimed_by_run_id, "cancelled", None)]),
        _Result(one=item.id),  # the take-over
    )
    monkeypatch.setattr("app.core.database.async_session", lambda: session)
    monkeypatch.setattr(wi_service, "get_work_item", _passthrough)
    agent = uuid.uuid4()

    out = await wi_service.claim_work_item(
        str(item.id), agent_id=str(agent), user_id=str(item.user_id),
        writer=PERSONAL_AGENT, run_id=str(new_run),
    )
    assert out["claimed"] is True and out["taken_over"] is True
    take = session.statements[3]
    sql, params = _sql(take), _params(take)
    assert _NOT_ALIVE.search(sql)  # the holder is re-checked as dead in the write
    assert params["claimed_by_agent_id"] == agent
    assert params["claimed_by_run_id"] == new_run
    assert params["status"] == "claimed"
    note = params["claim_notes"][-1]["text"]
    assert note.startswith(f"released: run {item.claimed_by_run_id} ended cancelled at")
    assert f"taken over by agent {agent} in run {new_run}" in note
    for untouched in ("description", "payload", "result", "correlation_key"):
        assert untouched not in params


@pytest.mark.asyncio
async def test_a_claim_on_an_item_held_by_a_live_run_is_refused(monkeypatch):
    item = _item()
    session = _claim_session(
        item,
        _Result(one=None),
        _Result(rows=[(item.claimed_by_run_id, "running", None)]),
    )
    monkeypatch.setattr("app.core.database.async_session", lambda: session)
    monkeypatch.setattr(wi_service, "get_work_item", _passthrough)

    out = await wi_service.claim_work_item(
        str(item.id), agent_id=str(uuid.uuid4()), user_id=str(item.user_id),
        writer=PERSONAL_AGENT, run_id=str(uuid.uuid4()),
    )
    assert out["claimed"] is False
    assert len(session.statements) == 3  # no take-over write


@pytest.mark.asyncio
async def test_a_recent_claim_held_outside_any_run_is_refused(monkeypatch):
    """A claim with no run that a live chat turn or run may still hold is kept.

    The take-over is one conditional UPDATE whose predicate (quiet for the
    grace, no live run of the agent since the claim) refuses it in SQL.
    """
    item = _item(claimed_by_run_id=None)
    session = _claim_session(item, _Result(one=None), _Result(one=None))
    monkeypatch.setattr("app.core.database.async_session", lambda: session)
    monkeypatch.setattr(wi_service, "get_work_item", _passthrough)

    out = await wi_service.claim_work_item(
        str(item.id), agent_id=str(uuid.uuid4()), user_id=str(item.user_id),
        writer=PERSONAL_AGENT, run_id=str(uuid.uuid4()),
    )
    assert out["claimed"] is False
    take = _sql(session.statements[2])
    assert "work_items.claimed_by_run_id is null" in take
    assert "assignment_runs.agent_id" in take and "sub_agent_tasks.target_agent_id" in take


@pytest.mark.asyncio
async def test_a_claim_takes_over_a_legacy_claim_no_live_run_holds(monkeypatch):
    item = _item(claimed_by_run_id=None)
    new_run = uuid.uuid4()
    session = _claim_session(item, _Result(one=None), _Result(one=item.id))
    monkeypatch.setattr("app.core.database.async_session", lambda: session)
    monkeypatch.setattr(wi_service, "get_work_item", _passthrough)
    agent = uuid.uuid4()

    out = await wi_service.claim_work_item(
        str(item.id), agent_id=str(agent), user_id=str(item.user_id),
        writer=PERSONAL_AGENT, run_id=str(new_run),
    )
    assert out["claimed"] is True and out["taken_over"] is True
    take = session.statements[2]
    sql, params = _sql(take), _params(take)
    assert "work_items.claimed_by_run_id is null" in sql and _NOT_ALIVE.search(sql)
    assert params["claimed_by_agent_id"] == agent
    assert params["claimed_by_run_id"] == new_run
    note = params["claim_notes"][-1]["text"]
    assert note.startswith("released: no live holder found at ")
    assert f"legacy claim by agent {item.claimed_by_agent_id}" in note
    assert f"taken over by agent {agent} in run {new_run}" in note
    for untouched in ("description", "payload", "result", "correlation_key"):
        assert untouched not in params


@pytest.mark.asyncio
async def test_a_finished_item_is_never_taken_over(monkeypatch):
    item = _item(status="done")
    session = _claim_session(item, _Result(one=None))
    monkeypatch.setattr("app.core.database.async_session", lambda: session)
    monkeypatch.setattr(wi_service, "get_work_item", _passthrough)

    out = await wi_service.claim_work_item(
        str(item.id), agent_id=str(uuid.uuid4()), user_id=str(item.user_id),
        writer=PERSONAL_AGENT, run_id=str(uuid.uuid4()),
    )
    assert out["claimed"] is False and item.status == "done"


@pytest.mark.asyncio
async def test_the_holding_run_claiming_again_is_answered_claimed(monkeypatch):
    """An inbox run claiming the item it was dispatched with."""
    item = _item(status="claimed")
    session = _claim_session(item)
    monkeypatch.setattr("app.core.database.async_session", lambda: session)

    out = await wi_service.claim_work_item(
        str(item.id), agent_id=str(item.claimed_by_agent_id), user_id=str(item.user_id),
        writer=PERSONAL_AGENT, run_id=item.claimed_by_run_id,
    )
    assert out["claimed"] is True and out["taken_over"] is False
    assert len(session.statements) == 1


@pytest.mark.asyncio
async def test_a_fresh_claim_records_its_run(monkeypatch):
    item = _item(status="open", claimed_by_agent_id=None, claimed_by_run_id=None)
    run = uuid.uuid4()
    session = _claim_session(item, _Result(one=item.id))
    monkeypatch.setattr("app.core.database.async_session", lambda: session)
    monkeypatch.setattr(wi_service, "get_work_item", _passthrough)

    out = await wi_service.claim_work_item(
        str(item.id), agent_id=str(uuid.uuid4()), user_id=str(item.user_id),
        writer=PERSONAL_AGENT, run_id=str(run),
    )
    assert out == {"claimed": True, "taken_over": False, "work_item": {"id": str(item.id)}}
    assert _params(session.statements[1])["claimed_by_run_id"] == run


# -- Claims that record no run (a chat turn, a claim made by an older release) ---------


def _grace_cutoffs(statement) -> list[datetime]:
    return [v for v in _params(statement).values() if isinstance(v, datetime)]


@pytest.mark.asyncio
async def test_release_reopens_a_legacy_claim_no_live_run_holds(monkeypatch):
    item_id, agent = uuid.uuid4(), uuid.uuid4()
    earlier = [{"at": "2026-10-30T10:00:00+00:00", "text": "an older note"}]
    session = _Session([
        _Result(rows=[(item_id, agent, earlier)]),  # orphaned legacy claims
        _Result(rowcount=1),  # the release
    ])
    monkeypatch.setattr("app.core.database.async_session", lambda: session)
    before = datetime.now(UTC)

    assert await wi_service.release_orphaned_legacy_claims() == 1
    assert session.committed is True

    select_sql = _sql(session.statements[0])
    assert "work_items.claimed_by_run_id is null" in select_sql
    assert "action_mode is null" in select_sql
    # No run or background task of the claimer (else the owner) alive since
    # the claim.
    assert _NOT_ALIVE.search(select_sql)
    assert "assignment_runs.agent_id" in select_sql
    assert "sub_agent_tasks.target_agent_id" in select_sql
    assert "coalesce(work_items.claimed_by_agent_id, work_items.owner_agent_id)" in select_sql
    # Quiet for the grace: a chat turn still working keeps its item.
    cutoffs = _grace_cutoffs(session.statements[0])
    assert any(
        abs((before - wi_service.LEGACY_CLAIM_GRACE) - c) < timedelta(seconds=5) for c in cutoffs
    )

    release = session.statements[1]
    sql, params = _sql(release), _params(release)
    assert "work_items.claimed_by_run_id is null" in sql and _NOT_ALIVE.search(sql)
    assert params["status"] == "open"
    assert params["claimed_by_agent_id"] is None and params["claimed_at"] is None
    notes = params["claim_notes"]
    assert notes[0] == earlier[0]
    assert re.fullmatch(
        rf"released: no live holder found at \d{{4}}-\d\d-\d\dT\d\d:\d\d:\d\dZ, "
        rf"legacy claim by agent {agent}",
        notes[1]["text"],
    )
    for untouched in ("description", "payload", "result", "correlation_key", "title"):
        assert untouched not in params


@pytest.mark.asyncio
async def test_legacy_release_only_touches_items_still_held(monkeypatch):
    """Open, blocked, done and cancelled items are never touched."""
    session = _Session([_Result(rows=[])])
    monkeypatch.setattr("app.core.database.async_session", lambda: session)

    assert await wi_service.release_orphaned_legacy_claims() == 0
    held = [v for v in _params(session.statements[0]).values() if isinstance(v, (list, tuple))]
    assert ["claimed", "in_progress"] in [list(v) for v in held]
    assert "done" not in str(held) and "blocked" not in str(held)


@pytest.mark.asyncio
async def test_legacy_release_counts_only_the_rows_it_wrote(monkeypatch):
    """Updated, completed or claimed between the read and the write: not counted."""
    session = _Session([_Result(rows=[(uuid.uuid4(), uuid.uuid4(), None)]), _Result(rowcount=0)])
    monkeypatch.setattr("app.core.database.async_session", lambda: session)
    assert await wi_service.release_orphaned_legacy_claims() == 0


@pytest.mark.asyncio
async def test_an_open_item_with_a_stale_claim_is_claimed(monkeypatch):
    """Set back to open by hand while still carrying ``claimed_by``."""
    item = _item(status="open", claimed_by_run_id=None)
    run = uuid.uuid4()
    session = _claim_session(item, _Result(one=item.id))
    monkeypatch.setattr("app.core.database.async_session", lambda: session)
    monkeypatch.setattr(wi_service, "get_work_item", _passthrough)

    out = await wi_service.claim_work_item(
        str(item.id), agent_id=str(uuid.uuid4()), user_id=str(item.user_id),
        writer=PERSONAL_AGENT, run_id=str(run),
    )
    assert out["claimed"] is True and out["taken_over"] is False
    flip = _sql(session.statements[1])
    assert "work_items.status =" in flip
    assert "claimed_by_agent_id is null" not in flip


@pytest.mark.asyncio
async def test_setting_an_item_back_to_open_drops_its_claim(monkeypatch):
    item = _item(status="in_progress", claimed_by_run_id=None)
    session = _Session([_Result(one=item)])
    monkeypatch.setattr("app.core.database.async_session", lambda: session)
    monkeypatch.setattr(wi_service, "get_work_item", _passthrough)

    async def _tiers(db, user_id):
        return [], []

    async def _gate(db, writer, item, action):
        return object()

    monkeypatch.setattr(wi_service, "_workspace_tiers", _tiers)
    monkeypatch.setattr(wi_service, "_gate_write", _gate)

    await wi_service.update_work_item(
        str(item.id), user_id=str(item.user_id), status="open", writer=PERSONAL_AGENT
    )
    assert item.status == "open"
    assert item.claimed_by_agent_id is None
    assert item.claimed_by_run_id is None and item.claimed_at is None
    assert item.description == "PROGRESS: step 1 done" and item.result is None


@pytest.mark.asyncio
async def test_moving_a_held_item_along_keeps_its_claim(monkeypatch):
    holder = uuid.uuid4()
    item = _item(status="claimed", claimed_by_agent_id=holder)
    session = _Session([_Result(one=item)])
    monkeypatch.setattr("app.core.database.async_session", lambda: session)
    monkeypatch.setattr(wi_service, "get_work_item", _passthrough)

    async def _tiers(db, user_id):
        return [], []

    async def _gate(db, writer, item, action):
        return object()

    monkeypatch.setattr(wi_service, "_workspace_tiers", _tiers)
    monkeypatch.setattr(wi_service, "_gate_write", _gate)

    await wi_service.update_work_item(
        str(item.id), user_id=str(item.user_id), status="in_progress", writer=PERSONAL_AGENT
    )
    assert item.status == "in_progress" and item.claimed_by_agent_id == holder


def _requeue_world(monkeypatch, item, holder_status):
    session = _Session([_Result(one=item)])
    monkeypatch.setattr("app.core.database.async_session", lambda: session)
    monkeypatch.setattr(wi_service, "get_work_item", _passthrough)

    async def _tiers(db, user_id):
        return [], []

    async def _gate(db, writer, item, action):
        return object()

    async def _holders(db, run_ids):
        return {rid: wi_service.HoldingRun(holder_status, None, "run") for rid in run_ids}

    monkeypatch.setattr(wi_service, "_workspace_tiers", _tiers)
    monkeypatch.setattr(wi_service, "_gate_write", _gate)
    monkeypatch.setattr(wi_service, "holding_runs", _holders)
    return session


@pytest.mark.asyncio
async def test_an_agent_cannot_requeue_an_item_a_live_run_of_another_holds(monkeypatch):
    """The creator setting the item back to open after the inbox gave it to a run."""
    item = _item(status="claimed")
    held_by = item.claimed_by_run_id
    session = _requeue_world(monkeypatch, item, "running")

    with pytest.raises(ValueError, match=f"held by run {held_by}"):
        await wi_service.update_work_item(
            str(item.id), user_id=str(item.user_id), status="open", note="set open",
            writer=PERSONAL_AGENT, writer_run_ids=(uuid.uuid4(),),
        )
    assert item.status == "claimed" and item.claimed_by_run_id == held_by
    assert not session.committed


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("own", "holder_status"),
    [(True, "running"), (False, "completed"), (False, None)],
    ids=["its-own-run", "holder-ended", "holder-purged"],
)
async def test_requeue_goes_through_for_its_own_run_or_an_ended_holder(monkeypatch, own, holder_status):
    item = _item(status="in_progress")
    writer_runs = (item.claimed_by_run_id,) if own else (uuid.uuid4(),)
    _requeue_world(monkeypatch, item, holder_status)

    await wi_service.update_work_item(
        str(item.id), user_id=str(item.user_id), status="open",
        writer=PERSONAL_AGENT, writer_run_ids=writer_runs,
    )
    assert item.status == "open" and item.claimed_by_run_id is None


@pytest.mark.asyncio
async def test_a_write_with_no_run_still_requeues_a_held_item(monkeypatch):
    """A person (no run ids) keeps the hand back to the queue."""
    item = _item(status="in_progress")
    _requeue_world(monkeypatch, item, "running")

    await wi_service.update_work_item(
        str(item.id), user_id=str(item.user_id), status="open", writer=PERSONAL_AGENT
    )
    assert item.status == "open" and item.claimed_by_run_id is None


# -- The run a claim is made in -------------------------------------------------


def test_the_claim_holder_is_the_run_else_the_background_task():
    run, task = uuid.uuid4(), uuid.uuid4()
    assert wi_service.current_claim_holder(None) is None
    with wi_service.claims_held_by_background_task(task):
        assert wi_service.current_claim_holder(None) == task
        assert wi_service.current_claim_holder(str(run)) == run
    assert wi_service.current_claim_holder(None) is None


@pytest.mark.asyncio
async def test_the_claim_tool_passes_its_run(monkeypatch):
    from app.services.builtin_tools import build_work_item_tools
    from app.services.work_item import WorkItemOrigin

    seen = {}

    async def _claim(work_item_id, *, agent_id, user_id, writer, run_id=None):
        seen["run_id"] = run_id
        return {"claimed": True, "taken_over": True, "work_item": {"id": work_item_id}}

    monkeypatch.setattr(wi_service, "claim_work_item", _claim)
    run = uuid.uuid4()
    _, executor = build_work_item_tools(
        SimpleNamespace(), str(uuid.uuid4()), str(uuid.uuid4()),
        origin=WorkItemOrigin(run_id=run),
    )
    out = await executor.execute_tool(
        ToolCall(id=str(uuid.uuid4()), name="claim_work_item", arguments={"work_item_id": "wi-1"})
    )
    assert seen["run_id"] == run
    assert "taken over" in out.content


@pytest.mark.asyncio
async def test_record_claim_run_only_names_a_claim_with_no_run(monkeypatch):
    session = _Session([_Result(rowcount=1)])
    monkeypatch.setattr("app.core.database.async_session", lambda: session)
    item, agent, run = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()

    assert await wi_service.record_claim_run(str(item), agent_id=agent, run_id=str(run))
    sql, params = _sql(session.statements[0]), _params(session.statements[0])
    assert "work_items.claimed_by_run_id is null" in sql
    assert "work_items.claimed_by_agent_id =" in sql
    assert params["claimed_by_run_id"] == run
