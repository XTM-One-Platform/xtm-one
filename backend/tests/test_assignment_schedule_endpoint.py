"""Tests for the read-only ``GET /assignments/schedule`` endpoint.

The endpoint powers the Agentic Flow "Schedule" tab and computes, per visible
enabled assignment: trigger semantics, ``next_run_at`` (via the shared
``next_cron_run`` helper), ``pending_run_at`` (earliest queued orchestration
execution), and ``has_active_run``. These tests pin the per-row field mapping,
the trigger-type branching (schedule / webhook), the soonest-first ordering, and
- importantly - that a *due-now* pending run (an orchestration ``run_at`` in the
past, not yet claimed by a worker) is still surfaced as ``pending_run_at`` rather
than being dropped.

Mirrors the repo's fake-session test convention (sequenced ``execute`` results
driven through a FastAPI ``TestClient``) - the backend suite has no real-DB
harness.
"""

import uuid
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.dialects import postgresql

from app.api import deps
from app.api.deps import require_agentic_ai
from app.api.v1 import assignments as assignments_api
from app.core.database import get_db


class _ScalarsResult:
    """Result whose ``.scalars().all()`` returns the seeded ORM-like rows."""

    def __init__(self, rows):
        self._rows = rows

    def scalars(self):
        return self

    def all(self):
        return list(self._rows)


class _RowsResult:
    """Result whose ``.all()`` yields indexable rows (``row[0]`` == value)."""

    def __init__(self, rows):
        self._rows = rows

    def all(self):
        return list(self._rows)


class _SeqDB:
    """Async session stub returning queued results in ``execute`` order."""

    def __init__(self, results):
        self._results = list(results)
        self.statements = []

    async def execute(self, stmt, *args, **kwargs):
        self.statements.append(stmt)
        return self._results.pop(0)


def _make_client(monkeypatch, results, *, is_admin=True):
    fake_user = SimpleNamespace(id=uuid.uuid4(), is_admin=is_admin, full_name="Admin")
    fake_db = _SeqDB(results)

    async def _groups(_db, _uid):
        return []

    monkeypatch.setattr(assignments_api, "get_user_group_ids", _groups)

    app = FastAPI()
    app.include_router(assignments_api.router, prefix="/api/v1/assignments")

    async def _get_db():
        yield fake_db

    app.dependency_overrides[deps.get_current_user] = lambda: fake_user
    app.dependency_overrides[get_db] = _get_db
    app.dependency_overrides[require_agentic_ai] = lambda: None
    return TestClient(app), fake_db


def test_schedule_endpoint_empty_returns_early(monkeypatch):
    # No visible assignments -> [] without touching the runs/orchestration queries.
    client, fake_db = _make_client(monkeypatch, [_ScalarsResult([])])
    resp = client.get("/api/v1/assignments/schedule")
    assert resp.status_code == 200, resp.text
    assert resp.json() == []
    # Only the assignments query ran; no agent / run / orchestration follow-ups.
    assert len(fake_db.statements) == 1


@pytest.fixture
def seeded(monkeypatch):
    agent = SimpleNamespace(id=uuid.uuid4(), name="Ops", icon="Bot", enabled=True)

    # a1: hourly schedule, already triggered -> deterministic next run from base.
    a1 = SimpleNamespace(
        id=uuid.uuid4(),
        agent_id=agent.id,
        name="Hourly digest",
        trigger_type="schedule",
        trigger_config={"cron": "0 * * * *"},
        enabled=True,
        last_triggered_at=datetime(2026, 1, 1, 9, 0, tzinfo=UTC),
        delay=None,
    )
    # a2: daily schedule, never triggered, with a delayed run due NOW.
    a2 = SimpleNamespace(
        id=uuid.uuid4(),
        agent_id=agent.id,
        name="Delayed sweep",
        trigger_type="schedule",
        trigger_config={"cron": "0 9 * * *"},
        enabled=True,
        last_triggered_at=None,
        delay="10m",
    )
    # a3: webhook trigger -> webhook_url, no next run.
    a3 = SimpleNamespace(
        id=uuid.uuid4(),
        agent_id=agent.id,
        name="Inbound hook",
        trigger_type="webhook",
        trigger_config={},
        enabled=True,
        last_triggered_at=None,
        delay=None,
    )

    run_a1 = uuid.uuid4()
    run_a2 = uuid.uuid4()
    # An orchestration run_at deliberately far in the PAST: a due-now pending run
    # that a worker has not claimed yet. It must still surface as pending_run_at.
    due_now = datetime(2020, 1, 1, 0, 0, tzinfo=UTC)

    results = [
        _ScalarsResult([a1, a2, a3]),  # assignments
        _ScalarsResult([agent]),  # agents
        _RowsResult(  # active runs (a1 running, a2 delayed)
            [
                (run_a1, a1.id, "running"),
                (run_a2, a2.id, "delayed"),
            ]
        ),
        # Queued delivery backlog, grouped count per assignment. Only
        # a1 has deliveries waiting behind its active run; assignments absent
        # from the aggregate report 0.
        _RowsResult([(a1.id, 2)]),
        # Latest finished run per assignment: status and completion
        # time of the same run. Only a1 has one.
        _RowsResult([(a1.id, "failed", datetime(2026, 1, 1, 9, 2, tzinfo=UTC))]),
        _RowsResult([(str(run_a2), due_now)]),  # orchestration executions
    ]
    client, _fake_db = _make_client(monkeypatch, results)
    return SimpleNamespace(
        client=client, agent=agent, a1=a1, a2=a2, a3=a3, due_now=due_now
    )


def test_schedule_endpoint_excludes_disabled_agent(monkeypatch):
    # An assignment whose parent agent is disabled must be dropped: the
    # scheduler / webhooks / assignment engine all skip disabled agents, so its
    # runs never fire. The endpoint returns [] and short-circuits before the
    # runs / orchestration queries.
    agent = SimpleNamespace(id=uuid.uuid4(), name="Off", icon="Bot", enabled=False)
    a1 = SimpleNamespace(
        id=uuid.uuid4(),
        agent_id=agent.id,
        name="Hourly digest",
        trigger_type="schedule",
        trigger_config={"cron": "0 * * * *"},
        enabled=True,
        last_triggered_at=None,
        delay=None,
    )
    results = [
        _ScalarsResult([a1]),  # assignments
        _ScalarsResult([agent]),  # agents (disabled)
    ]
    client, fake_db = _make_client(monkeypatch, results)
    resp = client.get("/api/v1/assignments/schedule")
    assert resp.status_code == 200, resp.text
    assert resp.json() == []
    # Only the assignments + agents queries ran; the disabled-agent filter
    # short-circuits before the runs / orchestration queries.
    assert len(fake_db.statements) == 2


def test_schedule_endpoint_maps_fields_and_orders(seeded):
    resp = seeded.client.get("/api/v1/assignments/schedule")
    assert resp.status_code == 200, resp.text
    items = resp.json()
    assert len(items) == 3

    by_id = {i["assignment_id"]: i for i in items}
    a1 = by_id[str(seeded.a1.id)]
    a2 = by_id[str(seeded.a2.id)]
    a3 = by_id[str(seeded.a3.id)]

    # a1: schedule next-run computed from last_triggered_at base (deterministic).
    assert a1["trigger_type"] == "schedule"
    assert a1["cron"] == "0 * * * *"
    assert a1["next_run_at"] == "2026-01-01T10:00:00+00:00"
    assert a1["agent_name"] == "Ops"
    assert a1["agent_icon"] == "Bot"
    assert a1["delay_seconds"] == 0
    assert a1["has_active_run"] is True
    assert a1["pending_run_at"] is None
    # Queued deliveries waiting behind the active run are reported as a count,
    # and do NOT themselves make the assignment active.
    assert a1["queued_run_count"] == 2
    # The latest finished run's outcome and its own completion time.
    assert a1["last_run_status"] == "failed"
    assert a1["last_run_finished_at"] == "2026-01-01T09:02:00+00:00"

    # a2: delayed run that is due now (past run_at) is still surfaced.
    assert a2["has_active_run"] is True
    assert a2["pending_run_at"] == "2020-01-01T00:00:00+00:00"
    assert a2["queued_run_count"] == 0
    assert a2["last_run_status"] is None
    assert a2["last_run_finished_at"] is None
    assert a2["delay_seconds"] == 600
    assert a2["next_run_at"] is not None  # daily cron projected from now

    # a3: webhook trigger -> url present, no cron / next run.
    assert a3["trigger_type"] == "webhook"
    assert a3["cron"] is None
    assert a3["next_run_at"] is None
    assert a3["has_active_run"] is False
    assert a3["pending_run_at"] is None
    assert a3["queued_run_count"] == 0
    assert a3["webhook_url"].endswith(f"/api/hooks/assignments/{seeded.a3.id}")

    # Soonest-first ordering: a1 (fixed 2026-01-01) < a2 (now-based future) <
    # a3 (no next run sorts last).
    order = [i["assignment_id"] for i in items]
    assert order == [str(seeded.a1.id), str(seeded.a2.id), str(seeded.a3.id)]


def test_schedule_endpoint_reads_the_cron_in_the_configured_timezone(monkeypatch):
    # trigger_config.timezone is honoured: "0 7" in Europe/Paris after a fire
    # on 30 Oct is 31 Oct 07:00 Paris = 06:00Z, shown in both zones. An unknown
    # zone reads as UTC and says so; no zone stays UTC with no warning.
    agent = SimpleNamespace(id=uuid.uuid4(), name="Ops", icon="Bot", enabled=True)
    last = datetime(2026, 10, 30, 6, 0, tzinfo=UTC)

    def _row(name, config):
        return SimpleNamespace(
            id=uuid.uuid4(),
            agent_id=agent.id,
            name=name,
            trigger_type="schedule",
            trigger_config=config,
            enabled=True,
            last_triggered_at=last,
            delay=None,
        )

    paris = _row("Paris", {"cron": "0 7 * * *", "timezone": "Europe/Paris"})
    bad = _row("Bad zone", {"cron": "0 7 * * *", "timezone": "Mars/Olympus"})
    plain = _row("No zone", {"cron": "0 7 * * *"})
    results = [
        _ScalarsResult([paris, bad, plain]),
        _ScalarsResult([agent]),
        _RowsResult([]),
        _RowsResult([]),
        _RowsResult([]),  # latest finished runs (none finished yet)
    ]
    client, _fake_db = _make_client(monkeypatch, results)
    resp = client.get("/api/v1/assignments/schedule")
    assert resp.status_code == 200, resp.text
    by_id = {i["assignment_id"]: i for i in resp.json()}

    p = by_id[str(paris.id)]
    assert p["next_run_at"] == "2026-10-31T06:00:00+00:00"
    assert p["next_run_local"] == "2026-10-31T07:00:00+01:00"
    assert p["timezone"] == "Europe/Paris"
    assert p["timezone_warning"] is None

    # Read as UTC, "0 7" after a 06:00Z base is 07:00Z the same day.
    b = by_id[str(bad.id)]
    assert b["next_run_at"] == "2026-10-30T07:00:00+00:00"
    assert b["timezone"] == "UTC"
    assert b["timezone_warning"] == "timezone 'Mars/Olympus' not recognised, UTC used"

    n = by_id[str(plain.id)]
    assert n["next_run_at"] == "2026-10-30T07:00:00+00:00"
    assert n["next_run_local"] == "2026-10-30T07:00:00+00:00"
    assert n["timezone"] == "UTC"
    assert n["timezone_warning"] is None


def test_schedule_endpoint_tolerates_non_string_trigger_config(monkeypatch):
    # trigger_config is free-form JSON; legacy / misconfigured rows can hold a
    # non-string where a string is expected. The endpoint must degrade
    # gracefully (cron/provider/event -> null, no webhook/poll) instead of
    # 500-ing on Pydantic ``str`` validation or ``TypeError: unhashable type``
    # (a dict ``event`` into ``POLL_INTERVALS.get``, a list ``provider`` into the
    # set membership check). Mirrors the scheduler's defensive isinstance guard.
    agent = SimpleNamespace(id=uuid.uuid4(), name="Ops", icon="Bot", enabled=True)
    # Schedule row with a non-string (list) cron.
    sched = SimpleNamespace(
        id=uuid.uuid4(),
        agent_id=agent.id,
        name="Bad cron",
        trigger_type="schedule",
        trigger_config={"cron": []},
        enabled=True,
        last_triggered_at=None,
        delay=None,
    )
    # integration_event row with an unhashable dict event, list provider, and a
    # numeric integration_id - each of which would crash the unguarded endpoint.
    evt = SimpleNamespace(
        id=uuid.uuid4(),
        agent_id=agent.id,
        name="Bad event",
        trigger_type="integration_event",
        trigger_config={"provider": ["x"], "event": {"k": 1}, "integration_id": 0},
        enabled=True,
        last_triggered_at=None,
        delay=None,
    )
    results = [
        _ScalarsResult([sched, evt]),  # assignments
        _ScalarsResult([agent]),  # agents
        _RowsResult([]),  # active runs (none -> orchestration query skipped)
        _RowsResult([]),  # queued delivery backlog (none)
        _RowsResult([]),  # latest finished runs (none)
    ]
    client, fake_db = _make_client(monkeypatch, results)
    resp = client.get("/api/v1/assignments/schedule")
    assert resp.status_code == 200, resp.text
    by_id = {i["assignment_id"]: i for i in resp.json()}
    assert len(fake_db.statements) == 5
    assert by_id[str(sched.id)]["last_run_status"] is None

    sched_row = by_id[str(sched.id)]
    assert sched_row["cron"] is None
    assert sched_row["next_run_at"] is None

    evt_row = by_id[str(evt.id)]
    assert evt_row["provider"] is None
    assert evt_row["event"] is None
    assert evt_row["poll_interval_seconds"] is None
    assert evt_row["webhook_url"] is None


def test_schedule_last_run_is_scoped_to_the_runs_a_member_can_open(monkeypatch):
    # A group member sees a group-shared assignment but not another member's
    # runs (``run_visible_filter``, own + company-managed): the latest-run query
    # behind the outcome dot carries that filter for a non-admin, and orders by
    # completion so the status and the time come from the same run.
    agent = SimpleNamespace(
        id=uuid.uuid4(),
        name="Ops",
        icon="Bot",
        enabled=True,
        user_id=None,
        is_company_managed=True,
        group_ids=[],
    )
    a1 = SimpleNamespace(
        id=uuid.uuid4(),
        agent_id=agent.id,
        name="Shared digest",
        trigger_type="schedule",
        trigger_config={"cron": "0 * * * *"},
        enabled=True,
        last_triggered_at=datetime(2026, 1, 1, 9, 0, tzinfo=UTC),
        delay=None,
    )
    results = [
        _ScalarsResult([a1]),
        _ScalarsResult([agent]),
        _RowsResult([]),  # active runs
        _RowsResult([]),  # queued backlog
        _RowsResult([]),  # latest finished runs the member can open: none
    ]
    client, fake_db = _make_client(monkeypatch, results, is_admin=False)
    resp = client.get("/api/v1/assignments/schedule")
    assert resp.status_code == 200, resp.text
    row = resp.json()[0]
    assert row["last_run_status"] is None
    assert row["last_run_finished_at"] is None

    last_run_sql = str(fake_db.statements[4].compile(dialect=postgresql.dialect())).lower()
    assert "distinct on" in last_run_sql
    assert "assignment_runs.user_id" in last_run_sql
    assert "coalesce(assignment_runs.completed_at, assignment_runs.created_at)" in last_run_sql


def test_schedule_last_run_ranks_an_early_failure_without_completed_at_first(monkeypatch):
    # A terminal run that exited before setting ``completed_at`` (assignment or
    # agent unavailable at start) is newer than an older completed run: the
    # query ranks runs by COALESCE(completed_at, created_at), never NULLS LAST,
    # so DISTINCT ON keeps the newer failure, and its creation time is returned
    # as its finish time.
    agent = SimpleNamespace(
        id=uuid.uuid4(),
        name="Ops",
        icon="Bot",
        enabled=True,
        user_id=None,
        is_company_managed=True,
        group_ids=[],
    )
    a1 = SimpleNamespace(
        id=uuid.uuid4(),
        agent_id=agent.id,
        name="Digest",
        trigger_type="schedule",
        trigger_config={"cron": "0 * * * *"},
        enabled=True,
        last_triggered_at=datetime(2026, 1, 1, 10, 0, tzinfo=UTC),
        delay=None,
    )
    failed_created_at = datetime(2026, 1, 1, 10, 0, tzinfo=UTC)
    results = [
        _ScalarsResult([a1]),
        _ScalarsResult([agent]),
        _RowsResult([]),  # active runs
        _RowsResult([]),  # queued backlog
        # What Postgres returns for the ranking: the newer failure, its
        # finish time being its creation time.
        _RowsResult([(a1.id, "failed", failed_created_at)]),
    ]
    client, fake_db = _make_client(monkeypatch, results)
    resp = client.get("/api/v1/assignments/schedule")
    assert resp.status_code == 200, resp.text
    row = resp.json()[0]
    assert row["last_run_status"] == "failed"
    assert row["last_run_finished_at"] == "2026-01-01T10:00:00+00:00"

    last_run_sql = str(fake_db.statements[4].compile(dialect=postgresql.dialect())).lower()
    # Ranked by the finish time with the creation-time fallback, never with
    # NULL completion times sent last.
    assert "coalesce(assignment_runs.completed_at, assignment_runs.created_at)" in last_run_sql
    assert "nulls last" not in last_run_sql
