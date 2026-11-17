"""Regression tests for the per-user fan-out rollback contract in
``autonomous._fanout_assignment_to_users``.

``trigger_assignment`` is a transaction boundary - it commits each per-user run
itself - so a failure mid-loop can leave the shared scheduler session in a
rolled-back-pending state. Without a rollback in the per-user ``except`` the
poisoned transaction cascades: every subsequent user's ``trigger_assignment``
(and the caller's ``last_triggered_at`` commit) then fails too, silently
dropping the whole cron window. These tests pin that one user's failure is
isolated (rolled back) and the loop continues, while a clean run never rolls
back. The fan-out runs against a fake session (no DB / Redis / network): the
background gate and ``trigger_assignment`` are monkeypatched.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from types import SimpleNamespace

from app.services import autonomous
from tests.agentic_gate_stub import stub_agentic_gate


class _ScalarsResult:
    """Result whose ``.scalars().all()`` returns the seeded rows."""

    def __init__(self, rows):
        self._rows = rows

    def scalars(self):
        return self

    def all(self):
        return list(self._rows)


class _RowsResult:
    """Result whose ``.all()`` yields the seeded (unpackable) rows."""

    def __init__(self, rows):
        self._rows = rows

    def all(self):
        return list(self._rows)


class _FakeDB:
    def __init__(self, results):
        self._results = list(results)
        self.rollbacks = 0
        self.commits = 0

    async def execute(self, _stmt):
        if not self._results:
            raise AssertionError("Unexpected extra execute() call")
        return self._results.pop(0)

    async def commit(self):
        self.commits += 1

    async def rollback(self):
        self.rollbacks += 1


def _patch_fanout(monkeypatch, *, trigger):
    """Patch the background gate (AI may run) and ``trigger_assignment`` (the
    source modules the helper imports lazily)."""
    stub_agentic_gate(monkeypatch)
    monkeypatch.setattr("app.services.assignment_engine.trigger_assignment", trigger)


def _fanout_db(user_ids):
    # The helper makes two execute() calls: (1) active users -> (id, vertical)
    # rows; (2) the recent-run dedup -> scalars().all() (none deduped here).
    return _FakeDB(
        [
            _RowsResult([(uid, "cyber") for uid in user_ids]),
            _ScalarsResult([]),
        ]
    )


async def test_fanout_rolls_back_and_continues_on_per_user_failure(monkeypatch):
    user_a, user_b = uuid.uuid4(), uuid.uuid4()
    triggered_users: list[uuid.UUID] = []

    async def fake_trigger(**kwargs):
        uid = kwargs["triggered_by_user_id"]
        if uid == user_a:
            raise RuntimeError("transient per-user failure")
        triggered_users.append(uid)
        return "run-id"

    _patch_fanout(monkeypatch, trigger=fake_trigger)

    db = _fanout_db([user_a, user_b])
    assignment = SimpleNamespace(id=uuid.uuid4(), name="exposure-briefing")
    agent = SimpleNamespace(name="ctem-agent")

    triggered = await autonomous._fanout_assignment_to_users(
        db, assignment, agent, datetime.now(UTC), "0 9 1 * *"
    )

    # The failed user is rolled back so the poisoned txn can't cascade...
    assert db.rollbacks == 1
    # ...and the loop continues: the second user still fires and is counted.
    assert triggered_users == [user_b]
    assert triggered == 1


async def test_fanout_no_rollback_when_all_users_succeed(monkeypatch):
    user_a, user_b = uuid.uuid4(), uuid.uuid4()
    triggered_users: list[uuid.UUID] = []

    async def fake_trigger(**kwargs):
        triggered_users.append(kwargs["triggered_by_user_id"])
        return "run-id"

    _patch_fanout(monkeypatch, trigger=fake_trigger)

    db = _fanout_db([user_a, user_b])
    assignment = SimpleNamespace(id=uuid.uuid4(), name="exposure-briefing")
    agent = SimpleNamespace(name="ctem-agent")

    triggered = await autonomous._fanout_assignment_to_users(
        db, assignment, agent, datetime.now(UTC), "0 9 1 * *"
    )

    assert db.rollbacks == 0
    assert triggered == 2
    assert triggered_users == [user_a, user_b]
