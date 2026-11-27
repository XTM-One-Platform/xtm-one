"""Tests for the durable work-item primitive.

Covers the pure staleness derivation + serialization, and the built-in tool
layer (registration, happy-path output, the atomic-claim failure message, and
delegate_to_assignment) by monkeypatching the service functions - mirroring the
fake-based style of ``test_agent_builder_crud_tools.py`` (no DB required).
"""

import uuid
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from sqlalchemy.dialects import postgresql

import app.services.builtin_tools.work_items as wi_tools_mod
import app.services.work_item as wi_service
from app.services import work_item_context
from app.services.builtin_tools import build_work_item_tools
from app.services.company_managed_writes import agent_writer
from app.tools.base import ToolCall


@pytest.fixture(autouse=True)
def _pickup_state_unknown(monkeypatch):
    """The created item's pickup is read from the database: unknown here."""

    async def _unknown(item):
        return None

    monkeypatch.setattr(work_item_context, "new_item_pickup_state", _unknown)


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
        status="open",
        priority="medium",
        correlation_key=None,
        due_at=None,
        claimed_by_agent_id=None,
        claimed_at=None,
        source="agent",
        result=None,
        last_activity_at=now,
        created_at=now,
        updated_at=now,
    )
    base.update(overrides)
    return SimpleNamespace(**base)


# ── Staleness derivation ───────────────────────────────────────────────────────


def test_terminal_items_are_never_stale():
    old = datetime.now(UTC) - timedelta(days=10)
    for status in ("done", "cancelled"):
        assert not wi_service.is_work_item_stale(_item(status=status, last_activity_at=old))


def test_past_due_open_item_is_stale():
    now = datetime.now(UTC)
    assert wi_service.is_work_item_stale(
        _item(status="open", due_at=now - timedelta(hours=1), last_activity_at=now)
    )


def test_quiet_item_is_stale_after_threshold():
    now = datetime.now(UTC)
    stale = _item(status="in_progress", last_activity_at=now - timedelta(hours=30))
    fresh = _item(status="in_progress", last_activity_at=now - timedelta(hours=1))
    assert wi_service.is_work_item_stale(stale)
    assert not wi_service.is_work_item_stale(fresh)


def test_open_backlog_item_is_never_stale_on_inactivity():
    """An OPEN, unclaimed item with no due date is backlog
    waiting for pickup - it must NEVER be flagged stale just because it has gone
    quiet, no matter how old. (This is the exact false alarm the user hit: an
    item owned by an agent that runs weekly looked stale within 24h.)"""
    now = datetime.now(UTC)
    for age_hours in (25, 24 * 7, 24 * 90):
        item = _item(status="open", due_at=None, last_activity_at=now - timedelta(hours=age_hours))
        assert wi_service.is_work_item_stale(item) is False


def test_open_past_due_item_is_stale():
    """An explicit due date is a real, item-specific SLA - a past-due OPEN item
    IS stale (the human set a deadline and nobody picked it up)."""
    now = datetime.now(UTC)
    assert wi_service.is_work_item_stale(
        _item(status="open", due_at=now - timedelta(minutes=1), last_activity_at=now)
    )


def test_claimed_item_quiet_is_stale_but_open_is_not():
    """A claim is a promise to act, so a claimed item that goes quiet is stale;
    an open item with the same inactivity is not (backlog vs dropped ball)."""
    now = datetime.now(UTC)
    old = now - timedelta(hours=48)
    assert wi_service.is_work_item_stale(_item(status="claimed", last_activity_at=old))
    assert wi_service.is_work_item_stale(_item(status="blocked", last_activity_at=old))
    assert not wi_service.is_work_item_stale(_item(status="open", last_activity_at=old))


def test_stale_window_disabled_only_past_due_flags(monkeypatch):
    """With the inactivity window disabled (<=0), only an explicit past-due date
    flags an item - a quiet claimed item is no longer stale."""
    monkeypatch.setattr(wi_service.settings, "work_item_stale_after_hours", 0)
    now = datetime.now(UTC)
    quiet_claimed = _item(status="claimed", last_activity_at=now - timedelta(days=5))
    past_due = _item(status="open", due_at=now - timedelta(hours=1))
    assert wi_service.is_work_item_stale(quiet_claimed) is False
    assert wi_service.is_work_item_stale(past_due) is True


def _holders(run_id, status, kind="run"):
    return {run_id: wi_service.HoldingRun(status=status, ended_at=None, kind=kind)}


def test_live_holder_past_due_is_not_stale():
    """A claimed or in-progress item whose holder run is alive and recently
    active is being worked on: a passed due date alone does not make it stale."""
    now = datetime.now(UTC)
    run_id = uuid.uuid4()
    for status in ("claimed", "in_progress"):
        item = _item(
            status=status,
            due_at=now - timedelta(hours=1),
            last_activity_at=now - timedelta(minutes=7),
            claimed_by_run_id=run_id,
        )
        assert wi_service.is_work_item_stale(item, now, _holders(run_id, "running")) is False
        assert wi_service.serialize_work_item(
            item, now=now, holders=_holders(run_id, "running")
        )["is_stale"] is False


def test_dead_holder_past_due_is_stale():
    now = datetime.now(UTC)
    run_id = uuid.uuid4()
    item = _item(
        status="in_progress",
        due_at=now - timedelta(hours=1),
        last_activity_at=now - timedelta(minutes=7),
        claimed_by_run_id=run_id,
    )
    assert wi_service.is_work_item_stale(item, now, _holders(run_id, "failed")) is True
    # A holder no longer recorded reads as ended.
    gone = {run_id: wi_service.HoldingRun(status=None, ended_at=None, kind=None)}
    assert wi_service.is_work_item_stale(item, now, gone) is True


def test_open_past_due_is_stale_whatever_the_holders():
    now = datetime.now(UTC)
    run_id = uuid.uuid4()
    item = _item(status="open", due_at=now - timedelta(hours=1), claimed_by_run_id=run_id)
    assert wi_service.is_work_item_stale(item, now, _holders(run_id, "running")) is True


def test_blocked_past_due_is_stale_even_with_a_live_run_recorded():
    now = datetime.now(UTC)
    run_id = uuid.uuid4()
    item = _item(
        status="blocked",
        due_at=now - timedelta(hours=1),
        claimed_by_run_id=run_id,
        blocked_on=[],
    )
    assert wi_service.is_work_item_stale(item, now, _holders(run_id, "running")) is True


def test_live_holder_quiet_beyond_the_window_is_stale():
    now = datetime.now(UTC)
    run_id = uuid.uuid4()
    item = _item(
        status="in_progress",
        last_activity_at=now - timedelta(hours=30),
        claimed_by_run_id=run_id,
    )
    assert wi_service.is_work_item_stale(item, now, _holders(run_id, "running")) is True


def test_the_stale_sql_clause_spares_a_past_due_item_held_by_a_live_run():
    sql = str(
        wi_service._stale_sql_clause(datetime.now(UTC)).compile(dialect=postgresql.dialect())
    ).lower()
    assert "claimed_by_run_id is not null" in sql
    assert "assignment_runs" in sql
    assert "not (" in sql


def test_serialize_includes_derived_is_stale():
    now = datetime.now(UTC)
    data = wi_service.serialize_work_item(
        _item(status="open", due_at=now - timedelta(hours=1))
    )
    assert data["is_stale"] is True
    assert data["status"] == "open"
    assert "id" in data and "title" in data


# ── list_work_items: stale_only is filtered in SQL ──────────────


class _CapturingResult:
    def __init__(self, rows):
        self._rows = rows

    def scalars(self):
        return self

    def all(self):
        return self._rows


class _CapturingSession:
    """Async-context session that records the statements it executes and returns a
    fixed row set, so list_work_items' query construction is verifiable with no DB
    (mirrors the no-DB style used across this module)."""

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
        return _CapturingResult(self._rows)


@pytest.mark.asyncio
async def test_list_stale_only_filters_in_sql_not_python(monkeypatch):
    """stale_only must push the staleness predicate + ordering into SQL (the exact
    mirror of count_stale_work_items), NOT post-filter a created_at-DESC window in
    Python. The old heuristic over-fetched limit*3 and dropped non-stale rows in
    Python, so genuinely stale (oldest) items hidden behind a window of newer rows
    silently vanished from the "Show stale" view even while the stale-count alert
    was non-zero. This locks in: (a) the predicate is compiled into the query,
    (b) no limit*3 over-fetch, (c) the service returns exactly what the query
    yields (no Python is_stale re-filter)."""
    from sqlalchemy.dialects import postgresql

    # A row whose Python is_work_item_stale would be False (fresh, open, not past
    # due). The OLD code post-filtered on is_stale and would drop it; the NEW code
    # trusts the SQL predicate and returns whatever the query yielded.
    sentinel = _item(status="open", last_activity_at=datetime.now(UTC))
    session = _CapturingSession([sentinel])
    monkeypatch.setattr("app.core.database.async_session", lambda: session)

    out = await wi_service.list_work_items(
        user_id=str(uuid.uuid4()), stale_only=True, limit=25
    )

    # (c) No Python post-filter: the not-stale sentinel still comes back.
    assert [row["id"] for row in out] == [str(sentinel.id)]

    compiled = session.statements[0].compile(dialect=postgresql.dialect())
    sql = str(compiled).lower()
    # (a) staleness predicate is in SQL: non-terminal status membership + the
    # due-date / inactivity disjunction, ordered by the staleness anchor.
    assert "status in" in sql
    assert "due_at" in sql and "last_activity_at" in sql
    assert "coalesce" in sql
    assert "order by" in sql
    # (b) no limit*3 over-fetch: the requested limit is the bound limit and 75
    # (25 * 3) never appears among the bound parameters. Use a list (the expanded
    # status IN clause binds an unhashable tuple, so a set would raise).
    param_values = list(compiled.params.values())
    assert 25 in param_values
    assert 75 not in param_values


# ── count_work_items: deletable_only swaps to the bulk-delete scope ────────────


class _CountResult:
    def __init__(self, value):
        self._value = value

    def scalar_one(self):
        return self._value


class _CountSession:
    """Async-context session recording the executed statement and returning a
    fixed COUNT(*) scalar, so count_work_items' scope clause is verifiable with
    no DB (mirrors the no-DB style used across this module)."""

    def __init__(self, value):
        self._value = value
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
        return _CountResult(self._value)


@pytest.mark.asyncio
async def test_count_deletable_only_non_admin_excludes_company_managed(monkeypatch):
    """For a non-admin, deletable_only counts ONLY the caller's own private rows -
    the exact scope bulk_delete_work_items removes - so the "select all N
    matching" UI never promises the company-managed rows the user can see but
    not delete, including one carrying their own user id."""
    from sqlalchemy.dialects import postgresql

    session = _CountSession(3)
    monkeypatch.setattr("app.core.database.async_session", lambda: session)

    await wi_service.count_work_items(
        user_id=str(uuid.uuid4()), is_admin=False, deletable_only=True
    )
    sql = str(session.statements[0].compile(dialect=postgresql.dialect())).lower()
    assert "user_id =" in sql
    assert "is_company_managed is not true" in sql
    assert "is_company_managed is true" not in sql


@pytest.mark.asyncio
async def test_count_visible_scope_includes_company_managed(monkeypatch):
    """Without deletable_only the count is the visible scope (own + company-
    managed) that drives the "Showing X-Y of Z" pagination total."""
    from sqlalchemy.dialects import postgresql

    session = _CountSession(9)
    monkeypatch.setattr("app.core.database.async_session", lambda: session)

    await wi_service.count_work_items(user_id=str(uuid.uuid4()))
    sql = str(session.statements[0].compile(dialect=postgresql.dialect())).lower()
    assert "is_company_managed" in sql


# ── bulk_delete_work_items: the DELETE re-applies the deletable scope ──────────


class _BulkDeleteResult:
    def __init__(self, rows=None, rowcount=0):
        self._rows = rows or []
        self.rowcount = rowcount

    def scalars(self):
        return self

    def all(self):
        return self._rows


class _BulkDeleteSession:
    """Async-context session that returns the target-id rows for the first
    (SELECT) execute and a rowcount for each subsequent (DELETE) execute, so the
    re-scoped chunked DELETE is verifiable with no DB (mirrors the no-DB style
    used across this module)."""

    def __init__(self, target_ids):
        self._target_ids = target_ids
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
        if len(self.statements) == 1:
            return _BulkDeleteResult(rows=self._target_ids)
        return _BulkDeleteResult(rowcount=len(self._target_ids))

    async def commit(self):
        self.committed = True


@pytest.mark.asyncio
async def test_bulk_delete_reapplies_deletable_scope_in_delete(monkeypatch):
    """The chunked DELETE must re-apply the deletable scope, not just filter the
    prior SELECT. Re-applying it makes the DB enforce the permission gate, so a
    row that turned company-managed (or changed owner) between the SELECT and the
    DELETE can't be removed by a non-admin - closing the TOCTOU window. The return
    value is the real summed rowcount, so the count reflects what was actually
    removed."""
    from sqlalchemy.dialects import postgresql

    target = [uuid.uuid4(), uuid.uuid4()]
    session = _BulkDeleteSession(target)
    monkeypatch.setattr("app.core.database.async_session", lambda: session)

    deleted = await wi_service.bulk_delete_work_items(
        user_id=str(uuid.uuid4()), is_admin=False, ids=[str(t) for t in target]
    )

    # The real per-batch rowcount is returned (not merely len(target_ids)).
    assert deleted == 2
    assert session.committed is True
    # statements[0] = SELECT id (scope), then per batch the SELECT of the rows the
    # delete audits (#2201) and the DELETE. The DELETE itself must carry the
    # non-admin deletable scope (own private rows only), so the permission gate
    # lives in the delete - company-managed rows are excluded, whoever they
    # record as their user.
    compiled = [str(s.compile(dialect=postgresql.dialect())).lower() for s in session.statements]
    delete_sql = next(sql for sql in compiled if sql.startswith("delete"))
    assert delete_sql.startswith("delete")
    assert "user_id =" in delete_sql
    assert "is_company_managed is not true" in delete_sql


# ── Correlation-key normalization ───────────────────────────────


def test_normalize_correlation_key_strips_bounds_and_blanks():
    from app.core.utils import CORRELATION_KEY_MAX_LENGTH, normalize_correlation_key

    assert normalize_correlation_key("  inc-8948  ") == "inc-8948"
    assert normalize_correlation_key(None) is None
    assert normalize_correlation_key("") is None
    assert normalize_correlation_key("   ") is None
    # Length is bounded to the shared column ceiling so a long key can't raise on
    # commit against the String(200) column.
    bounded = normalize_correlation_key("x" * 500)
    assert bounded is not None and len(bounded) == CORRELATION_KEY_MAX_LENGTH
    # Whitespace variants converge so exact-match filtering stays consistent.
    assert normalize_correlation_key("inc-1") == normalize_correlation_key(" inc-1 ")


# ── Tool layer ─────────────────────────────────────────────────────────────────


async def _call(executor, name, /, **arguments):
    return await executor.execute_tool(
        ToolCall(id=str(uuid.uuid4()), name=name, arguments=arguments)
    )


def test_work_item_tools_registered_and_defined():
    defs, executor = build_work_item_tools(
        SimpleNamespace(), str(uuid.uuid4()), str(uuid.uuid4())
    )
    names = {d.name for d in defs}
    expected = {
        "create_work_item",
        "update_work_item",
        "list_work_items",
        "get_work_item",
        "claim_work_item",
        "complete_work_item",
        "delegate_to_assignment",
    }
    assert expected <= names
    assert expected <= set(executor.tool_names)


@pytest.mark.asyncio
async def test_create_work_item_tool_reports_owner_label(monkeypatch):
    captured = {}

    async def _fake_create(**kwargs):
        captured.update(kwargs)
        return {
            "id": "wi-1",
            "title": kwargs["title"],
            "status": "open",
            "priority": "high",
            "correlation_key": "order-12345",
            "owner_agent_id": kwargs.get("owner_agent_id"),
        }

    monkeypatch.setattr(wi_service, "create_work_item", _fake_create)

    agent_id = str(uuid.uuid4())
    owner_id = str(uuid.uuid4())
    _, executor = build_work_item_tools(
        SimpleNamespace(), str(uuid.uuid4()), agent_id, is_company_managed=True
    )
    result = await _call(
        executor,
        "create_work_item",
        title="Process order 12345",
        owner_agent_id=owner_id,
        priority="high",
        correlation_key="order-12345",
    )
    assert not result.is_error
    assert "Process order 12345" in result.content
    # Owner is a routing label - the tool tells the agent it will be picked up
    # via a sweep / handover, NOT auto-triggered.
    assert "pick it up" in result.content
    assert "auto-trigger" not in result.content
    # Tool forwards creator + visibility + owner to the service; no auto_trigger.
    assert captured["created_by_agent_id"] == agent_id
    assert captured["is_company_managed"] is True
    assert captured["owner_agent_id"] == owner_id
    assert "auto_trigger" not in captured


@pytest.mark.asyncio
async def test_create_work_item_tool_warns_when_unowned(monkeypatch):
    """The #1 misuse: an agent creates a work item with no owner (like a human
    to-do note). With no owner and no handover, nothing ever picks it up. The
    tool result must say so in-loop so the agent sets an owner or hands off,
    instead of silently leaving an orphan (deterministic tool-result grounding,
    not added system-prompt text)."""

    async def _fake_create(**kwargs):
        return {
            "id": "wi-orphan",
            "title": kwargs["title"],
            "status": "open",
            "priority": "medium",
            "owner_agent_id": kwargs.get("owner_agent_id"),  # None -> unowned
        }

    monkeypatch.setattr(wi_service, "create_work_item", _fake_create)
    _, executor = build_work_item_tools(
        SimpleNamespace(), str(uuid.uuid4()), str(uuid.uuid4())
    )
    result = await _call(executor, "create_work_item", title="Follow up later")
    assert not result.is_error
    assert "UNASSIGNED" in result.content
    # It nudges toward the two real pickup paths (owner label / handover).
    assert "owner_agent_id" in result.content
    assert "delegate_to_assignment" in result.content
    # And names the misuse explicitly so the agent stops treating it as a
    # human to-do list.
    assert "not a human to-do list" in result.content
    # The owner-set helper line must NOT appear for an unowned item.
    assert "will pick it up on" not in result.content


@pytest.mark.asyncio
async def test_create_work_item_tool_forwards_payload_json(monkeypatch):
    """payload_json is parsed and forwarded to the service as a structured dict
    so agents can attach machine-readable context (parity with the model/API)."""
    captured = {}

    async def _fake_create(**kwargs):
        captured.update(kwargs)
        return {"id": "wi-1", "title": kwargs["title"], "status": "open", "priority": "medium"}

    monkeypatch.setattr(wi_service, "create_work_item", _fake_create)
    _, executor = build_work_item_tools(SimpleNamespace(), str(uuid.uuid4()), str(uuid.uuid4()))
    result = await _call(
        executor,
        "create_work_item",
        title="Enrich indicator",
        payload_json='{"indicator": "1.2.3.4", "confidence": 80}',
    )
    assert not result.is_error
    assert captured["payload"] == {"indicator": "1.2.3.4", "confidence": 80}


@pytest.mark.asyncio
async def test_create_work_item_tool_rejects_bad_payload_json(monkeypatch):
    """Invalid JSON / a non-object payload_json is rejected with a stable message
    and the service is never called."""

    async def _must_not_create(**kwargs):
        raise AssertionError("service must not be called on invalid payload_json")

    monkeypatch.setattr(wi_service, "create_work_item", _must_not_create)
    _, executor = build_work_item_tools(SimpleNamespace(), str(uuid.uuid4()), str(uuid.uuid4()))

    bad_json = await _call(
        executor, "create_work_item", title="x", payload_json="{not valid}"
    )
    assert "payload_json is not valid JSON" in bad_json.content

    not_object = await _call(
        executor, "create_work_item", title="x", payload_json="[1, 2, 3]"
    )
    assert "payload_json must be a JSON object" in not_object.content


@pytest.mark.asyncio
async def test_create_work_item_rejects_anonymous_private_orphan():
    """An anonymous caller on a non-company-managed agent (user_id=None,
    is_company_managed=False) would create a row invisible to everyone. The
    service rejects it before any DB write."""
    with pytest.raises(ValueError, match="company-managed"):
        await wi_service.create_work_item(
            user_id=None,
            title="orphan",
            is_company_managed=False,
            writer=agent_writer(is_company_managed=False),
        )


@pytest.mark.asyncio
async def test_create_work_item_tool_surfaces_value_error(monkeypatch):
    """A service ValueError (e.g. the anonymous-private orphan guard) is surfaced
    verbatim to the agent, not swallowed by the generic 'failed to create' handler
    (mirrors set_shared_state's ValueError branch)."""

    async def _raise(**kwargs):
        raise ValueError("a work item must belong to a user or be company-managed")

    monkeypatch.setattr(wi_service, "create_work_item", _raise)
    _, executor = build_work_item_tools(
        SimpleNamespace(), str(uuid.uuid4()), str(uuid.uuid4())
    )
    result = await _call(executor, "create_work_item", title="x")
    assert "must belong to a user or be company-managed" in result.content
    assert "failed to create" not in result.content


@pytest.mark.asyncio
async def test_claim_work_item_tool_success_and_conflict(monkeypatch):
    async def _claim_ok(work_item_id, *, agent_id, user_id, writer):
        return {"claimed": True, "work_item": {"id": work_item_id}}

    monkeypatch.setattr(wi_service, "claim_work_item", _claim_ok)
    _, executor = build_work_item_tools(
        SimpleNamespace(), str(uuid.uuid4()), str(uuid.uuid4())
    )
    ok = await _call(executor, "claim_work_item", work_item_id="wi-1")
    assert "Claimed work item" in ok.content

    async def _claim_conflict(work_item_id, *, agent_id, user_id, writer):
        return {
            "claimed": False,
            "reason": "already claimed or not open",
            "work_item": {"claimed_by_agent_id": "agent-x"},
        }

    monkeypatch.setattr(wi_service, "claim_work_item", _claim_conflict)
    conflict = await _call(executor, "claim_work_item", work_item_id="wi-1")
    assert "Could not claim" in conflict.content
    assert "agent-x" in conflict.content


@pytest.mark.asyncio
@pytest.mark.parametrize("status", ["done", "cancelled"])
async def test_a_claim_on_a_finished_item_answers_its_stored_result_at_once(monkeypatch, status):
    """A run spawned on an item another run already finished stops at its first call."""

    async def _claim_finished(work_item_id, *, agent_id, user_id, writer):
        return {
            "claimed": False,
            "reason": "already claimed or not open",
            "work_item": {
                "id": work_item_id,
                "title": "fix the thing",
                "status": status,
                "claimed_by_agent_id": "agent-x",
                "updated_at": "2026-10-30T19:00:00+00:00",
                "result": "PR #12 merged as abc123",
            },
        }

    monkeypatch.setattr(wi_service, "claim_work_item", _claim_finished)
    _, executor = build_work_item_tools(
        SimpleNamespace(), str(uuid.uuid4()), str(uuid.uuid4())
    )
    out = (await _call(executor, "claim_work_item", work_item_id="wi-1")).content
    assert f"is already {status}" in out
    assert "2026-10-30T19:00:00+00:00" in out and "agent-x" in out
    assert "PR #12 merged as abc123" in out
    assert "stop" in out and "pick a different item" not in out


def test_a_finished_item_read_in_full_says_so_first():
    from app.services.builtin_tools.work_items import _full_item_text

    item = {
        "id": "wi-1",
        "title": "t",
        "status": "done",
        "priority": "high",
        "updated_at": "2026-10-30T19:00:00+00:00",
        "result": "shipped",
    }
    text = _full_item_text(item, [], 0, False)
    assert text.splitlines()[1].startswith(
        "  - ALREADY DONE at 2026-10-30T19:00:00+00:00 (last update; completed_at not recorded)"
    )
    open_text = _full_item_text({**item, "status": "open"}, [], 0, False)
    assert "ALREADY" not in open_text


def test_a_finished_item_says_its_completion_time_not_its_last_update():
    """A note added after the completion moves updated_at, never the time shown."""
    from app.services.builtin_tools.work_items import _full_item_text

    item = {
        "id": "wi-1",
        "title": "t",
        "status": "cancelled",
        "priority": "high",
        "completed_at": "2026-11-05T00:31:52.341048+00:00",
        "updated_at": "2026-11-05T00:32:04.433903+00:00",
        "result": "dropped",
    }
    lines = _full_item_text(item, [], 0, False).splitlines()
    assert lines[1].startswith("  - ALREADY CANCELLED at 2026-11-05T00:31:52.341048+00:00:")
    assert "not recorded" not in lines[1]
    timestamps = next(line for line in lines if line.startswith("  - created:"))
    assert timestamps.endswith(", completed: 2026-11-05T00:31:52.341048+00:00")
    assert "updated: 2026-11-05T00:32:04.433903+00:00" in timestamps
    open_text = _full_item_text({**item, "status": "open", "completed_at": None}, [], 0, False)
    assert "completed:" not in open_text


@pytest.mark.parametrize("window", [{}, {"offset": 0}, {"max_chars": 200}])
def test_the_holder_run_and_its_liveness_show_in_every_view(window):
    """A windowed read of a long item still names its holder run, alive or ended."""
    from app.services.builtin_tools.work_items import _full_item_text

    item = {
        "id": "wi-1",
        "title": "t",
        "status": "in_progress",
        "priority": "urgent",
        "description": "note " * 400,
        "claimed_by_run": {"id": "run-1", "alive": False, "status": "cancelled"},
    }
    lines = _full_item_text(item, [], 0, False, (), **window).splitlines()
    assert lines[2] == "  - held by run: run-1, ended (cancelled)"
    alive = {**item, "claimed_by_run": {"id": "run-1", "alive": True, "status": "running"}}
    assert "  - held by run: run-1, alive" in _full_item_text(alive, [], 0, False, (), **window)
    unheld = {**item, "claimed_by_run": None}
    assert "held by run" not in _full_item_text(unheld, [], 0, False, (), **window)


@pytest.mark.parametrize("window", [{}, {"offset": 0}, {"max_chars": 200}])
def test_a_blocked_item_names_what_it_waits_for_in_every_view(window):
    """A windowed read of a long blocked item still shows its blockers and its reason (#11711)."""
    from app.services.builtin_tools.work_items import _full_item_text

    item = {
        "id": "wi-1",
        "title": "t",
        "status": "blocked",
        "priority": "high",
        "description": "note " * 400,
        "blocked_on": ["11111111-2222-3333-4444-555555555555"],
        "blocked_on_refs": [
            {"repo": "xtm-one-platform/xtm-one", "kind": "deployment", "number": 7, "release_on": "deployed"}
        ],
        "blocked_reason": "waits for the retest",
    }
    text = _full_item_text(item, [], 0, False, (), **window)
    assert "11111111-2222-3333-4444-555555555555" in text
    assert "deployment of xtm-one-platform/xtm-one#7" in text
    assert "  - blocked reason: waits for the retest" in text
    assert text.count("  - blocked reason:") == 1


@pytest.mark.asyncio
async def test_update_work_item_tool_hides_service_errors(monkeypatch):
    """A service-layer exception must surface as a stable, user-friendly message
    (mirroring create), never leak the raw exception detail to the agent."""

    async def _boom(*args, **kwargs):
        raise RuntimeError("db exploded: secret connection string")

    monkeypatch.setattr(wi_service, "update_work_item", _boom)
    _, executor = build_work_item_tools(
        SimpleNamespace(), str(uuid.uuid4()), str(uuid.uuid4())
    )
    result = await _call(
        executor, "update_work_item", work_item_id=str(uuid.uuid4()), status="done"
    )
    assert "failed to update the work item" in result.content
    assert "secret connection string" not in result.content


# ── The title is the agent's; the status is a field ────────────────────


@pytest.mark.asyncio
async def test_update_work_item_saves_the_title_and_answers_with_the_items_fields(monkeypatch):
    """The tool reads no word of a title: the status is a field of the tool,
    which declares that the title carries none. The result is the saved item."""
    captured = {}

    async def _fake_update(work_item_id, **kwargs):
        captured.update(kwargs)
        return {
            "id": work_item_id,
            "title": kwargs.get("title") or "X",
            "status": kwargs.get("status") or "open",
            "priority": kwargs.get("priority") or "medium",
        }

    monkeypatch.setattr(wi_service, "update_work_item", _fake_update)
    definitions, executor = build_work_item_tools(
        SimpleNamespace(), str(uuid.uuid4()), str(uuid.uuid4())
    )
    item_id = str(uuid.uuid4())
    result = await _call(executor, "update_work_item", work_item_id=item_id, title="Migrate DB => COMPLETED")

    assert not result.is_error
    assert captured["title"] == "Migrate DB => COMPLETED"
    assert result.content == (
        f"Updated work item **Migrate DB => COMPLETED** (id: `{item_id}`, status: open, priority: medium)."
    )
    update = next(d for d in definitions if d.name == "update_work_item")
    assert "status" in update.input_schema["properties"]
    assert "never encode status" in update.input_schema["properties"]["title"]["description"]


@pytest.mark.asyncio
async def test_list_work_items_tool_flags_stale(monkeypatch):
    async def _fake_list(**kwargs):
        return [
            {
                "id": "wi-1",
                "title": "Stale follow-up",
                "status": "open",
                "priority": "high",
                "owner_agent_id": None,
                "correlation_key": "inc-1",
                "is_stale": True,
                "claimed_by_agent_id": None,
            }
        ]

    monkeypatch.setattr(wi_service, "list_work_items", _fake_list)
    _, executor = build_work_item_tools(
        SimpleNamespace(), str(uuid.uuid4()), str(uuid.uuid4())
    )
    result = await _call(executor, "list_work_items", stale_only=True)
    assert "Stale follow-up" in result.content
    assert "STALE" in result.content


@pytest.mark.asyncio
async def test_delegate_to_assignment_tool(monkeypatch):
    captured = {}

    async def _fake_delegate(**kwargs):
        captured.update(kwargs)
        return "Delegated to assignment 'IR Runbook' - tracked run `run-42` was enqueued."

    monkeypatch.setattr(wi_tools_mod, "_run_delegate_to_assignment", _fake_delegate)
    agent_id = str(uuid.uuid4())
    _, executor = build_work_item_tools(SimpleNamespace(), str(uuid.uuid4()), agent_id)
    result = await _call(
        executor,
        "delegate_to_assignment",
        assignment_id=str(uuid.uuid4()),
        task="Contain the host",
    )
    assert "run-42" in result.content
    assert captured["task"] == "Contain the host"
    assert captured["delegating_agent_id"] == agent_id


# ── delegate_to_assignment: company-managed admin gate ──────────
# Mirrors the REST manual-trigger gate (POST /assignments/{id}/trigger), which
# restricts company-managed assignment triggers to admins.


class _FakeResult:
    def __init__(self, value):
        self._value = value

    def scalar_one_or_none(self):
        return self._value


class _FakeSession:
    """Minimal async-context session returning a queued list of execute results."""

    def __init__(self, results):
        self._results = list(results)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def get(self, _model, _ident):
        # The workspace tiers look the caller up first; an unknown user has
        # no workspace tier and executes nothing.
        return None

    async def execute(self, statement):
        if not self._results:
            raise AssertionError("unexpected extra execute() call")
        return _FakeResult(self._results.pop(0))


def _cm_assignment():
    return SimpleNamespace(
        id=uuid.uuid4(),
        name="Fleet IR Runbook",
        is_company_managed=True,
        enabled=True,
        agent_id=uuid.uuid4(),
    )


@pytest.mark.asyncio
async def test_delegate_company_managed_blocks_anonymous(monkeypatch):
    assignment = _cm_assignment()
    agent = SimpleNamespace(id=assignment.agent_id, enabled=True, hide_runs=True)
    # Anonymous branch: the assignment, then the agent the shared gate reads.
    # Per-user runs do not open it: a caller with no account has no run of
    # its own to start.
    monkeypatch.setattr(
        "app.core.database.async_session", lambda: _FakeSession([assignment, agent])
    )

    result = await wi_tools_mod._run_delegate_to_assignment(
        assignment_id=str(assignment.id),
        task="contain",
        acting_user_id=None,
        delegating_agent_id=str(uuid.uuid4()),
    )
    assert "Only admins can delegate to company-managed assignments" in result


@pytest.mark.asyncio
async def test_delegate_anonymous_lookup_is_the_company_wide_tier(monkeypatch):
    """An anonymous caller resolves the assignment through the one assignment
    visibility: the parent agent, company-managed and not narrowed by
    ``access_restricted``, never the mirrored ``Assignment.is_company_managed``.
    A restricted agent's assignment then answers like an unknown id."""
    statements = []

    class _CapturingSession(_FakeSession):
        async def get(self, _model, _ident):
            # The workspace tiers look the caller up first; an unknown user has
            # no workspace tier and executes nothing.
            return None

        async def execute(self, statement):
            statements.append(statement)
            return await super().execute(statement)

    monkeypatch.setattr(
        "app.core.database.async_session", lambda: _CapturingSession([None])
    )
    assignment_id = str(uuid.uuid4())

    result = await wi_tools_mod._run_delegate_to_assignment(
        assignment_id=assignment_id,
        task="contain",
        acting_user_id=None,
        delegating_agent_id=None,
    )

    assert result == f"Assignment `{assignment_id}` not found or not accessible."
    where = statements[0].whereclause
    sql = str(where.compile(dialect=postgresql.dialect())).lower()
    assert "agents.is_company_managed is true" in sql
    assert "agents.access_restricted is not true" in sql
    assert "assignments.is_company_managed" not in sql


def _stub_delegate_gate(monkeypatch, assignment, *, caller, agent):
    """Authenticated delegation with the shared lookup and overlap guard
    stubbed: the DB reads left are the caller row, then the agent row.
    Returns what the lookup and the engine were called with."""
    seen: dict = {"lookup": [], "trigger": []}

    async def _find(_db, uid, aid, user_group_ids=None, *, is_admin=False, lock=False):
        seen["lookup"].append({"uid": uid, "is_admin": is_admin})
        return assignment

    async def _no_overlap(_db, _assignment, _agent, _uid):
        return None

    async def _trigger(*args, **kwargs):
        seen["trigger"].append(kwargs)
        return "run-77"

    monkeypatch.setattr("app.api.v1.helpers.find_user_assignment", _find)
    monkeypatch.setattr("app.api.v1.helpers.manual_trigger_overlap", _no_overlap)
    monkeypatch.setattr("app.services.assignment_engine.trigger_assignment", _trigger)
    monkeypatch.setattr(
        "app.core.database.async_session", lambda: _FakeSession([caller, agent])
    )
    return seen


async def _delegate_as(uid, assignment):
    return await wi_tools_mod._run_delegate_to_assignment(
        assignment_id=str(assignment.id),
        task="contain",
        acting_user_id=str(uid),
        delegating_agent_id=None,
    )


@pytest.mark.asyncio
async def test_delegate_company_managed_blocks_non_admin(monkeypatch):
    uid = uuid.uuid4()
    assignment = _cm_assignment()
    agent = SimpleNamespace(id=assignment.agent_id, enabled=True, hide_runs=False)
    seen = _stub_delegate_gate(
        monkeypatch,
        assignment,
        caller=SimpleNamespace(id=uid, is_admin=False),
        agent=agent,
    )

    result = await _delegate_as(uid, assignment)

    assert "Only admins can delegate to company-managed assignments" in result
    assert seen["lookup"] == [{"uid": uid, "is_admin": False}]
    assert seen["trigger"] == []


@pytest.mark.asyncio
async def test_delegate_company_managed_allows_admin(monkeypatch):
    uid = uuid.uuid4()
    assignment = _cm_assignment()
    agent = SimpleNamespace(id=assignment.agent_id, enabled=True, hide_runs=False)
    seen = _stub_delegate_gate(
        monkeypatch,
        assignment,
        caller=SimpleNamespace(id=uid, is_admin=True),
        agent=agent,
    )

    result = await _delegate_as(uid, assignment)

    assert "run-77" in result
    assert "Only admins" not in result
    # The lookup takes the admin's role, as the REST trigger route does: an
    # admin's agent reaches every assignment.
    assert seen["lookup"] == [{"uid": uid, "is_admin": True}]
    assert seen["trigger"][0]["skip_if_active"] is True
    assert seen["trigger"][0]["active_run_user_id"] is None


@pytest.mark.asyncio
async def test_delegate_per_user_company_managed_is_open_to_a_non_admin(monkeypatch):
    """REST lets any user who can see it trigger a company-managed assignment
    that runs per user (the run is their own private report); so does the
    delegation, with the engine's atomic guard scoped to the caller's own run
    instead of the whole assignment."""
    uid = uuid.uuid4()
    assignment = _cm_assignment()
    agent = SimpleNamespace(id=assignment.agent_id, enabled=True, hide_runs=True)
    seen = _stub_delegate_gate(
        monkeypatch,
        assignment,
        caller=SimpleNamespace(id=uid, is_admin=False),
        agent=agent,
    )

    result = await _delegate_as(uid, assignment)

    assert "run-77" in result
    assert seen["trigger"][0]["triggered_by_user_id"] == uid
    assert seen["trigger"][0]["skip_if_active"] is True
    assert seen["trigger"][0]["active_run_user_id"] == uid


@pytest.mark.asyncio
async def test_delegate_hands_its_files_to_the_run(monkeypatch):
    """The delegation's file ids reach ``trigger_payload['file_ids']``, the
    list the engine admits as the run's attachments, canonical and once each."""
    uid = uuid.uuid4()
    assignment = _cm_assignment()
    agent = SimpleNamespace(id=assignment.agent_id, enabled=True, hide_runs=False)
    seen = _stub_delegate_gate(
        monkeypatch,
        assignment,
        caller=SimpleNamespace(id=uid, is_admin=True),
        agent=agent,
    )
    file_id = uuid.uuid4()

    result = await wi_tools_mod._run_delegate_to_assignment(
        assignment_id=str(assignment.id),
        task="read the attachment",
        acting_user_id=str(uid),
        delegating_agent_id=None,
        file_ids=[str(file_id).upper(), f" {file_id} "],
    )

    assert "run-77" in result
    assert seen["trigger"][0]["payload"]["file_ids"] == [str(file_id)]


@pytest.mark.asyncio
async def test_delegate_without_files_adds_no_file_ids(monkeypatch):
    uid = uuid.uuid4()
    assignment = _cm_assignment()
    agent = SimpleNamespace(id=assignment.agent_id, enabled=True, hide_runs=False)
    seen = _stub_delegate_gate(
        monkeypatch,
        assignment,
        caller=SimpleNamespace(id=uid, is_admin=True),
        agent=agent,
    )

    await _delegate_as(uid, assignment)

    assert "file_ids" not in seen["trigger"][0]["payload"]


@pytest.mark.asyncio
async def test_delegate_refuses_a_malformed_or_oversized_file_list():
    assignment_id = str(uuid.uuid4())

    bad = await wi_tools_mod._run_delegate_to_assignment(
        assignment_id=assignment_id,
        task="t",
        acting_user_id=None,
        delegating_agent_id=None,
        file_ids=["report.pdf"],
    )
    too_many = await wi_tools_mod._run_delegate_to_assignment(
        assignment_id=assignment_id,
        task="t",
        acting_user_id=None,
        delegating_agent_id=None,
        file_ids=[str(uuid.uuid4()) for _ in range(wi_tools_mod._MAX_DELEGATED_FILES + 1)],
    )

    assert bad == "Error: 'report.pdf' is not a valid file id."
    assert "at most 20" in too_many


@pytest.mark.asyncio
async def test_delegate_anonymous_hears_the_rest_refusal_first(monkeypatch):
    """The no-account restriction comes after the shared gate, so a disabled
    assignment is reported as disabled, like REST would, not as admin-only."""
    assignment = _cm_assignment()
    assignment.enabled = False
    agent = SimpleNamespace(id=assignment.agent_id, enabled=True, hide_runs=True)
    monkeypatch.setattr(
        "app.core.database.async_session", lambda: _FakeSession([assignment, agent])
    )

    result = await wi_tools_mod._run_delegate_to_assignment(
        assignment_id=str(assignment.id),
        task="contain",
        acting_user_id=None,
        delegating_agent_id=None,
    )

    assert result == "Assignment 'Fleet IR Runbook' is disabled."


@pytest.mark.asyncio
async def test_delegate_answers_the_shared_overlap_refusal(monkeypatch):
    uid = uuid.uuid4()
    assignment = _cm_assignment()
    agent = SimpleNamespace(id=assignment.agent_id, enabled=True, hide_runs=True)
    seen = _stub_delegate_gate(
        monkeypatch,
        assignment,
        caller=SimpleNamespace(id=uid, is_admin=False),
        agent=agent,
    )
    from app.api.v1.helpers import ManualTriggerRefusal

    async def _own_run_active(_db, _assignment, _agent, _uid):
        return ManualTriggerRefusal(409, "busy", "own_run_active")

    monkeypatch.setattr("app.api.v1.helpers.manual_trigger_overlap", _own_run_active)

    result = await _delegate_as(uid, assignment)

    assert "You already have a run of assignment 'Fleet IR Runbook'" in result
    assert seen["trigger"] == []


# ---------------------------------------------------------------------------
# Coordination-tools capability prompt (system-prompt nudge)
# ---------------------------------------------------------------------------


def test_coordination_prompt_empty_when_no_tools():
    """No work_items / shared_state wired -> no prompt inflation."""
    assert wi_tools_mod.format_coordination_tools_prompt([]) == ""
    assert wi_tools_mod.format_coordination_tools_prompt(None) == ""
    assert wi_tools_mod.format_coordination_tools_prompt(["web_fetch"]) == ""


# ── Work-item peer roster: owner validation + awareness ────────────────

_PEER_ID = "11111111-1111-1111-1111-111111111111"
_PEERS = [{"id": _PEER_ID, "name": "Triage Bot", "slug": "triage-bot"}]


@pytest.mark.asyncio
async def test_create_work_item_rejects_owner_outside_roster(monkeypatch):
    """With a configured roster, an owner that is not one of the peers is
    rejected (and the service is never called) so the agent can't mint a work
    item for an agent it was never granted - it self-corrects from the named
    list."""

    async def _must_not_create(**kwargs):
        raise AssertionError("service must not be called for an off-roster owner")

    monkeypatch.setattr(wi_service, "create_work_item", _must_not_create)
    _, executor = build_work_item_tools(
        SimpleNamespace(), str(uuid.uuid4()), str(uuid.uuid4()), peer_agents=_PEERS
    )
    result = await _call(
        executor,
        "create_work_item",
        title="x",
        owner_agent_id="99999999-9999-9999-9999-999999999999",
    )
    assert "not one of this agent's work-item peers" in result.content
    assert "Triage Bot" in result.content  # names the valid peers


@pytest.mark.asyncio
async def test_create_work_item_resolves_owner_by_slug_from_roster(monkeypatch):
    """An owner given as a slug resolves to the canonical agent id (like
    handover), so the agent can address a peer by its human-readable slug."""
    captured = {}

    async def _create(**kwargs):
        captured.update(kwargs)
        return {"id": "wi-1", "title": kwargs["title"], "status": "open", "priority": "medium"}

    monkeypatch.setattr(wi_service, "create_work_item", _create)
    _, executor = build_work_item_tools(
        SimpleNamespace(), str(uuid.uuid4()), str(uuid.uuid4()), peer_agents=_PEERS
    )
    result = await _call(
        executor, "create_work_item", title="x", owner_agent_id="triage-bot"
    )
    assert not result.is_error
    assert captured["owner_agent_id"] == _PEER_ID


@pytest.mark.asyncio
async def test_create_work_item_no_roster_accepts_any_owner(monkeypatch):
    """Back-compatible: with no roster configured, the owner passes through
    unchanged (the action-register path and existing agents keep working)."""
    captured = {}

    async def _create(**kwargs):
        captured.update(kwargs)
        return {"id": "wi-1", "title": kwargs["title"], "status": "open", "priority": "medium"}

    monkeypatch.setattr(wi_service, "create_work_item", _create)
    _, executor = build_work_item_tools(
        SimpleNamespace(), str(uuid.uuid4()), str(uuid.uuid4())
    )
    owner = str(uuid.uuid4())
    result = await _call(executor, "create_work_item", title="x", owner_agent_id=owner)
    assert not result.is_error
    assert captured["owner_agent_id"] == owner


def test_create_work_item_description_lists_roster():
    """The roster is surfaced IN the create_work_item description so the model
    is aware of its assignable peers up front (awareness), not only at error
    time."""
    defs, _ = build_work_item_tools(
        SimpleNamespace(), str(uuid.uuid4()), str(uuid.uuid4()), peer_agents=_PEERS
    )
    create_def = next(d for d in defs if d.name == "create_work_item")
    assert "Triage Bot" in create_def.description
    assert "triage-bot" in create_def.description


def test_create_work_item_description_no_roster_is_accurate():
    """With no roster, owner_agent_id is unvalidated (back-compatible), so the
    description must NOT imply a hard "backlog only" restriction - it states the
    field is optional / not validated, matching the permissive _resolve_owner."""
    defs, _ = build_work_item_tools(
        SimpleNamespace(), str(uuid.uuid4()), str(uuid.uuid4())
    )
    create_def = next(d for d in defs if d.name == "create_work_item")
    assert "No peer roster" in create_def.description
    assert "not validated" in create_def.description


def test_coordination_prompt_gates_each_bullet_independently():
    """Each tool contributes only its own bullet when wired."""
    wi_only = wi_tools_mod.format_coordination_tools_prompt(["work_items"])
    assert "Coordination tools" in wi_only
    assert "Work items:" in wi_only
    assert "Shared state:" not in wi_only
    assert "create_work_item" in wi_only
    # The bullet must spell out the orphan precondition: an unowned, un-handed
    # item goes nowhere (the misuse the deterministic warning also catches).
    assert "owner_agent_id" in wi_only
    assert "sits unactioned" in wi_only

    ss_only = wi_tools_mod.format_coordination_tools_prompt(["shared_state"])
    assert "Shared state:" in ss_only
    assert "Work items:" not in ss_only
    assert "get_shared_state" in ss_only

    both = wi_tools_mod.format_coordination_tools_prompt(
        ["work_items", "shared_state", "web_fetch"]
    )
    assert "Work items:" in both
    assert "Shared state:" in both
