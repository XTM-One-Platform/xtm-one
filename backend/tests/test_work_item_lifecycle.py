"""Tests for the work-item autonomous lifecycle.

Covers the consumer half (dispatch query + scheduler sweep), the claim
release / bounded-retry logic, and the self-cleaning janitor (auto-cancel
abandoned, purge terminal). No DB required: the service functions open their
own sessions, which are monkeypatched with capturing/queued fakes mirroring the
no-DB style of ``test_work_items.py``.
"""

import uuid
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

import app.services.autonomous as autonomous_mod
import app.services.work_item as wi_service
from app.services.company_managed_writes import (
    CompanyManagedWriteRefusedError,
    agent_writer,
)

PERSONAL_AGENT = agent_writer(is_company_managed=False)
COMPANY_AGENT = agent_writer(is_company_managed=True)


def _item(**overrides):
    now = datetime.now(UTC)
    base = dict(
        id=uuid.uuid4(),
        user_id=uuid.uuid4(),
        is_company_managed=False,
        owner_agent_id=None,
        created_by_agent_id=None,
        title="Triage incident",
        description=None,
        payload={},
        status="claimed",
        priority="medium",
        correlation_key=None,
        due_at=None,
        claimed_by_agent_id=uuid.uuid4(),
        claimed_at=now,
        source="agent",
        result=None,
        last_activity_at=now,
        created_at=now,
        updated_at=now,
    )
    base.update(overrides)
    return SimpleNamespace(**base)


# ── Generic fakes ──────────────────────────────────────────────────────────────


class _ScalarsResult:
    def __init__(self, rows=None, rowcount=0, one=None):
        self._rows = rows or []
        self.rowcount = rowcount
        self._one = one

    def scalars(self):
        return self

    def all(self):
        return self._rows

    def scalar_one_or_none(self):
        return self._one


class _QueueSession:
    """Async-context session returning a queued list of results per execute."""

    def __init__(self, results):
        self._results = list(results)
        self.statements: list = []
        self.committed = False

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def get(self, _model, _ident):
        # The workspace tiers look the caller up first; an unknown user has
        # no workspace tier and executes nothing.
        return None

    async def execute(self, statement):
        self.statements.append(statement)
        if not self._results:
            raise AssertionError("unexpected extra execute() call")
        return self._results.pop(0)

    async def commit(self):
        self.committed = True


# ── reap_work_items: self-cleaning janitor ──────────────────────────────────────


@pytest.mark.asyncio
async def test_reap_cancels_abandoned_and_purges_terminal(monkeypatch):
    """The janitor auto-cancels abandoned OPEN items and purges terminal ones,
    returning the real per-phase rowcounts so the scheduler can log them."""
    monkeypatch.setattr(wi_service.settings, "work_item_abandoned_after_days", 14)
    monkeypatch.setattr(wi_service.settings, "work_item_resolved_retention_days", 30)
    results = [
        _ScalarsResult(rows=[uuid.uuid4(), uuid.uuid4()]),  # cancel: select ids
        _ScalarsResult(rowcount=2),                          # cancel: update batch
        _ScalarsResult(rows=[uuid.uuid4()]),                 # purge: select ids
        _ScalarsResult(rowcount=1),                          # purge: delete batch
    ]
    session = _QueueSession(results)
    monkeypatch.setattr("app.core.database.async_session", lambda: session)

    out = await wi_service.reap_work_items()
    assert out == {"cancelled": 2, "purged": 1}
    assert session.committed is True


@pytest.mark.asyncio
async def test_reap_skips_phases_when_disabled(monkeypatch):
    """Each retention window is independently disabled by a <=0 setting; when
    both are off the janitor is a pure no-op (no SELECTs, nothing removed)."""
    monkeypatch.setattr(wi_service.settings, "work_item_abandoned_after_days", 0)
    monkeypatch.setattr(wi_service.settings, "work_item_resolved_retention_days", 0)
    session = _QueueSession([])  # any execute would raise
    monkeypatch.setattr("app.core.database.async_session", lambda: session)

    out = await wi_service.reap_work_items()
    assert out == {"cancelled": 0, "purged": 0}
    assert session.statements == []


@pytest.mark.asyncio
async def test_reap_cancel_update_reapplies_open_guard(monkeypatch):
    """The cancel UPDATE must re-apply ``status == 'open'`` (TOCTOU-safe), not
    just filter the prior id SELECT, so an item claimed between SELECT and
    UPDATE is never wrongly cancelled."""
    from sqlalchemy.dialects import postgresql

    monkeypatch.setattr(wi_service.settings, "work_item_abandoned_after_days", 14)
    monkeypatch.setattr(wi_service.settings, "work_item_resolved_retention_days", 0)
    session = _QueueSession([
        _ScalarsResult(rows=[uuid.uuid4()]),
        _ScalarsResult(rowcount=1),
    ])
    monkeypatch.setattr("app.core.database.async_session", lambda: session)

    await wi_service.reap_work_items()
    update_sql = str(
        session.statements[1].compile(dialect=postgresql.dialect())
    ).lower()
    assert update_sql.startswith("update")
    assert "status" in update_sql  # re-applied open guard + the cancelled set


@pytest.mark.asyncio
async def test_reap_cancel_excludes_future_due_items(monkeypatch):
    """The abandonment sweep must not cancel intentionally-deferred work: an item
    with a future ``due_at`` isn't dispatchable yet, so both the cancel SELECT
    and the cancel UPDATE gate on ``due_at IS NULL OR due_at <= now`` (mirroring
    list_dispatchable_work_items) - never on inactivity alone."""
    from sqlalchemy.dialects import postgresql

    monkeypatch.setattr(wi_service.settings, "work_item_abandoned_after_days", 14)
    monkeypatch.setattr(wi_service.settings, "work_item_resolved_retention_days", 0)
    session = _QueueSession([
        _ScalarsResult(rows=[uuid.uuid4()]),
        _ScalarsResult(rowcount=1),
    ])
    monkeypatch.setattr("app.core.database.async_session", lambda: session)

    await wi_service.reap_work_items()
    select_sql = str(
        session.statements[0].compile(dialect=postgresql.dialect())
    ).lower()
    update_sql = str(
        session.statements[1].compile(dialect=postgresql.dialect())
    ).lower()
    # Both the id SELECT and the TOCTOU-guarded UPDATE exclude future-due items.
    assert "due_at is null" in select_sql and "due_at <=" in select_sql
    assert "due_at is null" in update_sql and "due_at <=" in update_sql


# ── release_work_item_claim: bounded retry ──────────────────────────────────────


def _release_session_for(item):
    return _QueueSession([_ScalarsResult(one=item)])


@pytest.mark.asyncio
async def test_release_failed_increments_then_blocks(monkeypatch):
    """A failed dispatch increments the payload attempt counter; once it reaches
    the cap the item is parked in ``blocked`` (not re-opened) so a permanently
    failing item can't re-dispatch forever."""
    monkeypatch.setattr(wi_service.settings, "work_item_max_dispatch_attempts", 2)
    item = _item(status="claimed", payload={})

    # Each call opens its own session that returns the same (mutated) item, and
    # get_work_item is stubbed to a passthrough so we can assert on `item`.
    monkeypatch.setattr(
        "app.core.database.async_session", lambda: _release_session_for(item)
    )

    async def _passthrough(work_item_id, *, user_id):
        return wi_service.serialize_work_item(item)

    monkeypatch.setattr(wi_service, "get_work_item", _passthrough)

    await wi_service.release_work_item_claim(
        work_item_id=str(item.id),
        user_id=str(item.user_id),
        failed=True,
        error="boom",
        writer=PERSONAL_AGENT,
    )
    assert item.status == "open"
    assert item.payload["_dispatch_attempts"] == 1
    assert item.claimed_by_agent_id is None and item.claimed_at is None

    await wi_service.release_work_item_claim(
        work_item_id=str(item.id),
        user_id=str(item.user_id),
        failed=True,
        error="boom",
        writer=PERSONAL_AGENT,
    )
    assert item.status == "blocked"
    assert item.payload["_dispatch_attempts"] == 2
    assert "boom" in (item.result or "")


@pytest.mark.asyncio
async def test_release_skip_returns_to_open_without_counting(monkeypatch):
    """A skipped dispatch (not a failure) just hands the item back to ``open``
    and never touches the failure attempt counter."""
    item = _item(status="claimed", payload={})
    monkeypatch.setattr(
        "app.core.database.async_session", lambda: _release_session_for(item)
    )

    async def _passthrough(work_item_id, *, user_id):
        return wi_service.serialize_work_item(item)

    monkeypatch.setattr(wi_service, "get_work_item", _passthrough)

    await wi_service.release_work_item_claim(
        work_item_id=str(item.id), user_id=str(item.user_id), writer=PERSONAL_AGENT
    )
    assert item.status == "open"
    assert "_dispatch_attempts" not in item.payload
    assert item.claimed_by_agent_id is None


@pytest.mark.asyncio
@pytest.mark.parametrize("terminal_status", ["done", "cancelled"])
async def test_release_noop_on_terminal_item(monkeypatch, terminal_status):
    """A release that fires after the item was already resolved must be a no-op:
    a terminal (done/cancelled) item is never flipped back to open/blocked and
    its attempt counter is never touched, so best-effort cleanup can't resurrect
    finished work."""
    item = _item(
        status=terminal_status, payload={}, claimed_by_agent_id=None, claimed_at=None
    )
    monkeypatch.setattr(
        "app.core.database.async_session", lambda: _release_session_for(item)
    )

    out = await wi_service.release_work_item_claim(
        work_item_id=str(item.id),
        user_id=str(item.user_id),
        failed=True,
        error="boom",
        writer=PERSONAL_AGENT,
    )
    assert item.status == terminal_status  # unchanged - not reopened
    assert "_dispatch_attempts" not in item.payload
    assert out is not None and out["status"] == terminal_status


@pytest.mark.asyncio
async def test_release_refuses_a_company_managed_item_for_a_personal_agent(monkeypatch):
    """Handing a company-managed item back to the queue is a write: a personal
    agent's release is refused and leaves the claim as it is."""
    holder = uuid.uuid4()
    item = _item(status="claimed", is_company_managed=True, claimed_by_agent_id=holder)
    session = _release_session_for(item)
    monkeypatch.setattr("app.core.database.async_session", lambda: session)

    with pytest.raises(
        CompanyManagedWriteRefusedError,
        match="Only company-managed agents can release company-managed work items",
    ):
        await wi_service.release_work_item_claim(
            work_item_id=str(item.id), user_id=str(item.user_id), writer=PERSONAL_AGENT
        )
    assert item.status == "claimed"
    assert item.claimed_by_agent_id == holder
    assert session.committed is False


# ── list_dispatchable_work_items: eligibility pushed into SQL ────────────────────


class _CapturingSession:
    def __init__(self, rows):
        self._rows = rows
        self.statements: list = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def get(self, _model, _ident):
        # The workspace tiers look the caller up first; an unknown user has
        # no workspace tier and executes nothing.
        return None

    async def execute(self, statement):
        self.statements.append(statement)
        return _ScalarsResult(rows=self._rows)


@pytest.mark.asyncio
async def test_list_dispatchable_filters_in_sql(monkeypatch):
    """Eligibility is compiled into the query: OPEN status, owner match, the
    due-or-undated gate, optional prefix + priority filters, ordered and
    limited."""
    from sqlalchemy.dialects import postgresql

    session = _CapturingSession([])
    monkeypatch.setattr("app.core.database.async_session", lambda: session)

    await wi_service.list_dispatchable_work_items(
        owner_agent_id=str(uuid.uuid4()),
        user_id=str(uuid.uuid4()),
        correlation_prefix="ticket-",
        priority_min="high",
        limit=5,
        writer=COMPANY_AGENT,
    )
    sql = str(session.statements[0].compile(dialect=postgresql.dialect())).lower()
    assert "owner_agent_id =" in sql
    assert "status =" in sql                      # open backlog only
    assert "not_before is null" in sql            # pickup-time gate (#8753)
    assert "due_at <=" not in sql                 # the deadline defers nothing
    assert "correlation_key" in sql and "like" in sql  # prefix filter (ilike)
    assert "priority in" in sql                   # priority_min -> at-or-above set
    assert "order by" in sql and "limit" in sql
    # A company-managed claimer keeps the whole visible backlog.
    assert "is_company_managed is not true" not in sql


@pytest.mark.asyncio
async def test_list_dispatchable_leaves_company_managed_items_out_for_a_personal_claimer(
    monkeypatch,
):
    """A personal agent (or flow) may not take a company-managed item over, so
    the backlog it drains leaves those items out instead of offering claims that
    would be refused and crowd out its own items."""
    from sqlalchemy.dialects import postgresql

    session = _CapturingSession([])
    monkeypatch.setattr("app.core.database.async_session", lambda: session)

    await wi_service.list_dispatchable_work_items(
        owner_agent_id=str(uuid.uuid4()),
        user_id=str(uuid.uuid4()),
        writer=PERSONAL_AGENT,
    )
    sql = str(session.statements[0].compile(dialect=postgresql.dialect())).lower()
    assert "is_company_managed is not true" in sql


@pytest.mark.asyncio
async def test_list_dispatchable_invalid_owner_returns_empty(monkeypatch):
    """A non-UUID owner short-circuits to an empty list without touching the DB."""
    def _boom():
        raise AssertionError("must not open a session for an invalid owner")

    monkeypatch.setattr("app.core.database.async_session", _boom)
    out = await wi_service.list_dispatchable_work_items(
        owner_agent_id="not-a-uuid", user_id=str(uuid.uuid4()), writer=COMPANY_AGENT
    )
    assert out == []


@pytest.mark.asyncio
async def test_list_dispatchable_escapes_like_metacharacters_in_prefix(monkeypatch):
    """correlation_prefix is a LITERAL "starts with" filter: % and _ in the
    configured prefix are escaped (with an explicit ESCAPE char) so they can't
    act as LIKE wildcards and match unrelated keys (e.g. 'order_' vs 'orderX')."""
    from sqlalchemy.dialects import postgresql

    session = _CapturingSession([])
    monkeypatch.setattr("app.core.database.async_session", lambda: session)

    await wi_service.list_dispatchable_work_items(
        owner_agent_id=str(uuid.uuid4()),
        user_id=str(uuid.uuid4()),
        correlation_prefix="order_%",
        writer=COMPANY_AGENT,
    )
    compiled = session.statements[0].compile(dialect=postgresql.dialect())
    assert "escape" in str(compiled).lower()  # ESCAPE clause is emitted
    # The bound prefix parameter has its LIKE metacharacters backslash-escaped.
    assert any(
        isinstance(v, str) and "order\\_\\%" in v for v in compiled.params.values()
    )


# ── _process_work_item_assignments: the autonomous consumer sweep ───────────────


@pytest.mark.asyncio
async def test_consumer_sweep_claims_and_dispatches(monkeypatch):
    """The sweep loads work_item inbox assignments, finds an eligible item,
    atomically claims it, and triggers the assignment with the item in the
    payload - reusing the standard trigger path (a new SOURCE, not a new
    execution path)."""
    agent_id = uuid.uuid4()
    assignment = SimpleNamespace(
        id=uuid.uuid4(),
        agent_id=agent_id,
        user_id=uuid.uuid4(),
        enabled=True,
        name="Inbox worker",
        trigger_type="work_item",
        trigger_config={},
    )
    agent = SimpleNamespace(
        id=agent_id, name="Compliance Agent", enabled=True, is_company_managed=False
    )

    # db.execute is called twice: (1) load assignments, (2) batch-load agents.
    db = _QueueSession([
        _ScalarsResult(rows=[assignment]),
        _ScalarsResult(rows=[agent]),
    ])

    async def _no_active(_db, _assignment_id):
        return False

    captured = {}

    async def _list_dispatchable(**kwargs):
        return [{"id": "wi-1", "correlation_key": "inc-1", "title": "Do it"}]

    async def _claim(work_item_id, *, agent_id, user_id, writer):
        captured["claimed"] = work_item_id
        return {"claimed": True}

    async def _trigger(**kwargs):
        captured["trigger_payload"] = kwargs.get("payload")
        return "run-1"

    monkeypatch.setattr(autonomous_mod, "has_active_run", _no_active)
    monkeypatch.setattr(autonomous_mod, "trigger_assignment", _trigger)
    monkeypatch.setattr(wi_service, "list_dispatchable_work_items", _list_dispatchable)
    monkeypatch.setattr(wi_service, "claim_work_item", _claim)

    count = await autonomous_mod._process_work_item_assignments(db)

    assert count == 1
    assert captured["claimed"] == "wi-1"
    assert captured["trigger_payload"]["trigger_type"] == "work_item"
    assert captured["trigger_payload"]["work_item_id"] == "wi-1"


@pytest.mark.asyncio
@pytest.mark.parametrize("company_managed", [True, False])
async def test_consumer_sweep_lists_and_claims_as_the_assignment_agent(
    monkeypatch, company_managed
):
    """The inbox takes items over as the assignment's agent: a company-managed
    agent may claim company-managed items, a personal one lists and claims
    without them."""
    agent_id = uuid.uuid4()
    assignment = SimpleNamespace(
        id=uuid.uuid4(), agent_id=agent_id, user_id=uuid.uuid4(),
        enabled=True, name="Inbox", trigger_type="work_item", trigger_config={},
    )
    agent = SimpleNamespace(
        id=agent_id, name="Agent", enabled=True, is_company_managed=company_managed
    )
    db = _QueueSession([
        _ScalarsResult(rows=[assignment]),
        _ScalarsResult(rows=[agent]),
    ])
    writers = []

    async def _no_active(_db, _aid):
        return False

    async def _list_dispatchable(**kwargs):
        writers.append(kwargs["writer"])
        return [{"id": "wi-1", "correlation_key": None, "title": "x"}]

    async def _claim(work_item_id, *, agent_id, user_id, writer):
        writers.append(writer)
        return {"claimed": True}

    async def _trigger(**kwargs):
        return "run-1"

    monkeypatch.setattr(autonomous_mod, "has_active_run", _no_active)
    monkeypatch.setattr(autonomous_mod, "trigger_assignment", _trigger)
    monkeypatch.setattr(wi_service, "list_dispatchable_work_items", _list_dispatchable)
    monkeypatch.setattr(wi_service, "claim_work_item", _claim)

    assert await autonomous_mod._process_work_item_assignments(db) == 1
    assert [w.may_write for w in writers] == [company_managed, company_managed]
    # It names its agent, so the creators' coordination access decides what
    # it lists and claims.
    assert [w.agent_id for w in writers] == [agent_id, agent_id]


@pytest.mark.asyncio
async def test_consumer_sweep_releases_claim_when_dispatch_skipped(monkeypatch):
    """If the trigger is skipped (an active run slipped in), the claim is
    released so the item is retried, never stranded ``claimed``."""
    agent_id = uuid.uuid4()
    assignment = SimpleNamespace(
        id=uuid.uuid4(), agent_id=agent_id, user_id=uuid.uuid4(),
        enabled=True, name="Inbox", trigger_type="work_item", trigger_config={},
    )
    agent = SimpleNamespace(
        id=agent_id, name="Agent", enabled=True, is_company_managed=False
    )
    db = _QueueSession([
        _ScalarsResult(rows=[assignment]),
        _ScalarsResult(rows=[agent]),
    ])
    released = {}

    async def _no_active(_db, _aid):
        return False

    async def _list_dispatchable(**kwargs):
        return [{"id": "wi-9", "correlation_key": None, "title": "x"}]

    async def _claim(work_item_id, *, agent_id, user_id, writer):
        return {"claimed": True}

    async def _trigger_skip(**kwargs):
        return None  # active run -> skipped

    async def _release(*, work_item_id, user_id, **kwargs):
        released["id"] = work_item_id

    monkeypatch.setattr(autonomous_mod, "has_active_run", _no_active)
    monkeypatch.setattr(autonomous_mod, "trigger_assignment", _trigger_skip)
    monkeypatch.setattr(wi_service, "list_dispatchable_work_items", _list_dispatchable)
    monkeypatch.setattr(wi_service, "claim_work_item", _claim)
    monkeypatch.setattr(wi_service, "release_work_item_claim", _release)

    count = await autonomous_mod._process_work_item_assignments(db)
    assert count == 0
    assert released["id"] == "wi-9"


# ── load_work_item_peers: roster resolution ─────────────────────────────


@pytest.mark.asyncio
async def test_load_work_item_peers_filters_and_preserves_order(monkeypatch):
    """Resolves configured ids to {id,name,slug}, drops ids that aren't visible
    (stale / now-private), and preserves the configured order regardless of the
    row order the DB returns."""
    from app.services.builtin_tools import load_work_item_peers

    a1, a2, a3 = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    # DB returns a2, a1 (arbitrary order); a3 is NOT returned (invisible/stale).
    rows = [(a2, "Beta", "beta"), (a1, "Alpha", "alpha")]

    class _RowsResult:
        def all(self):
            return rows

    class _Sess:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *e):
            return False

        async def get(self, _model, _ident):
            # The workspace tiers look the caller up first; an unknown user has
            # no workspace tier and executes nothing.
            return None

        async def execute(self, *a, **k):
            return _RowsResult()

    monkeypatch.setattr("app.core.database.async_session", lambda: _Sess())

    async def _groups(_db, _uid):
        return []

    monkeypatch.setattr("app.api.v1.helpers.get_user_group_ids", _groups)

    out = await load_work_item_peers(
        owner_agent_ids=[str(a1), str(a2), str(a3)],
        user_id=str(uuid.uuid4()),
    )
    # a3 dropped (not visible); a1 before a2 (configured order, not DB order).
    assert [p["id"] for p in out] == [str(a1), str(a2)]
    assert out[0]["name"] == "Alpha" and out[0]["slug"] == "alpha"


@pytest.mark.asyncio
async def test_load_work_item_peers_empty_roster_short_circuits(monkeypatch):
    """An empty roster never touches the DB."""
    from app.services.builtin_tools import load_work_item_peers

    def _boom():
        raise AssertionError("must not open a session for an empty roster")

    monkeypatch.setattr("app.core.database.async_session", _boom)
    assert await load_work_item_peers(owner_agent_ids=[], user_id=str(uuid.uuid4())) == []
    assert await load_work_item_peers(owner_agent_ids=None, user_id=None) == []


@pytest.mark.asyncio
async def test_consumer_sweep_releases_claim_when_trigger_raises(monkeypatch):
    """If trigger_assignment RAISES after a successful claim, no run is created
    so no run finalizer will ever release the item - the consumer itself must
    release the claim so the item is retried, never stranded ``claimed``."""
    agent_id = uuid.uuid4()
    assignment = SimpleNamespace(
        id=uuid.uuid4(), agent_id=agent_id, user_id=uuid.uuid4(),
        enabled=True, name="Inbox", trigger_type="work_item", trigger_config={},
    )
    agent = SimpleNamespace(
        id=agent_id, name="Agent", enabled=True, is_company_managed=False
    )
    db = _QueueSession([
        _ScalarsResult(rows=[assignment]),
        _ScalarsResult(rows=[agent]),
    ])
    released = {}

    async def _no_active(_db, _aid):
        return False

    async def _list_dispatchable(**kwargs):
        return [{"id": "wi-5", "correlation_key": None, "title": "x"}]

    async def _claim(work_item_id, *, agent_id, user_id, writer):
        return {"claimed": True}

    async def _trigger_boom(**kwargs):
        raise RuntimeError("dispatch exploded")

    async def _release(*, work_item_id, user_id, **kwargs):
        released["id"] = work_item_id

    monkeypatch.setattr(autonomous_mod, "has_active_run", _no_active)
    monkeypatch.setattr(autonomous_mod, "trigger_assignment", _trigger_boom)
    monkeypatch.setattr(wi_service, "list_dispatchable_work_items", _list_dispatchable)
    monkeypatch.setattr(wi_service, "claim_work_item", _claim)
    monkeypatch.setattr(wi_service, "release_work_item_claim", _release)

    # The sweep isolates each assignment's failure (logs + continues), so a
    # raised dispatch does not abort the tick; it returns 0 dispatched.
    count = await autonomous_mod._process_work_item_assignments(db)
    assert count == 0
    assert released["id"] == "wi-5"


@pytest.mark.asyncio
async def test_consumer_sweep_tries_next_candidate_on_claim_race(monkeypatch):
    """When the first candidate loses its claim race, the sweep falls through to
    the next candidate in the pool and dispatches the first it can claim, so a
    single contended item never wastes the whole tick."""
    agent_id = uuid.uuid4()
    assignment = SimpleNamespace(
        id=uuid.uuid4(), agent_id=agent_id, user_id=uuid.uuid4(),
        enabled=True, name="Inbox", trigger_type="work_item", trigger_config={},
    )
    agent = SimpleNamespace(
        id=agent_id, name="Agent", enabled=True, is_company_managed=False
    )
    db = _QueueSession([
        _ScalarsResult(rows=[assignment]),
        _ScalarsResult(rows=[agent]),
    ])
    claim_attempts = []
    captured = {}

    async def _no_active(_db, _aid):
        return False

    async def _list_dispatchable(**kwargs):
        return [
            {"id": "wi-a", "correlation_key": None, "title": "a"},
            {"id": "wi-b", "correlation_key": None, "title": "b"},
        ]

    async def _claim(work_item_id, *, agent_id, user_id, writer):
        claim_attempts.append(work_item_id)
        return {"claimed": work_item_id == "wi-b"}  # wi-a lost, wi-b won

    async def _trigger(**kwargs):
        captured["payload"] = kwargs.get("payload")
        return "run-7"

    monkeypatch.setattr(autonomous_mod, "has_active_run", _no_active)
    monkeypatch.setattr(autonomous_mod, "trigger_assignment", _trigger)
    monkeypatch.setattr(wi_service, "list_dispatchable_work_items", _list_dispatchable)
    monkeypatch.setattr(wi_service, "claim_work_item", _claim)

    count = await autonomous_mod._process_work_item_assignments(db)
    assert count == 1
    assert claim_attempts == ["wi-a", "wi-b"]  # tried a, fell through to b
    assert captured["payload"]["work_item_id"] == "wi-b"
