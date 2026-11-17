"""Dismissals and the notification memory of the Security Audit, on real PostgreSQL.

The routes run every check as an admin on a scratch database, so the finding
dismissed is one the audit really reports there; the notifier runs on the same
database, with a Redis stand-in that only holds the lease and, for the upgrade,
the notifier's memory of what it last reported.

``integration``-marked and skips when PostgreSQL is unreachable, like its
neighbours; the temporary database is the shared ``tests/scratch_database.py``.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import async_sessionmaker

import app.models  # noqa: F401 - registers every table on Base.metadata
from app.api import deps
from app.api.v1 import security_audit
from app.core.database import get_db
from app.models.activity import Activity
from app.models.audit_log import AuditLog
from app.models.security_audit_finding_state import SecurityAuditFindingState
from app.models.user import User
from app.services import security_audit_finding_states as finding_states
from app.services import security_audit_notifications as notifications
from tests.scratch_database import ALL_TABLES, scratch_database

pytestmark = pytest.mark.integration

AUDIT = "/api/v1/security-audit"


@pytest.fixture(autouse=True)
def _fresh_legacy_memory_check(monkeypatch):
    """Each test is a new install: the once-per-process legacy check runs again."""
    monkeypatch.setattr(notifications, "_legacy_memory_settled", False)


def _scratch(prefix: str):
    return scratch_database(
        prefix,
        tables=ALL_TABLES,
        extensions=("vector",),
        skip_without_extensions=True,
        refuse_other_databases=True,
    )


@asynccontextmanager
async def _client(session_factory: async_sessionmaker, user: User) -> AsyncIterator[httpx.AsyncClient]:
    api = FastAPI()
    api.include_router(security_audit.router, prefix=AUDIT)

    async def _get_db():
        async with session_factory() as db:
            yield db

    api.dependency_overrides[deps.get_current_user] = lambda: user
    api.dependency_overrides[get_db] = _get_db
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=api), base_url="http://test") as client:
        yield client


async def _admin(session_factory: async_sessionmaker, name: str = "admin") -> User:
    async with session_factory() as db:
        admin = User(email=f"{name}@example.com", hashed_password="x", full_name=name.capitalize(), role="admin")
        db.add(admin)
        await db.commit()
        return admin


def _by_id(report: dict) -> dict[str, dict]:
    findings: dict[str, dict] = {}
    for finding in report["findings"]:
        if finding.get("id"):
            findings.setdefault(finding["id"], finding)
    return findings


def _notifiable(report: dict) -> str:
    """A warning the audit reports on this install: dismissable, notified, re-openable to critical."""
    for finding in report["findings"]:
        if finding.get("id") and finding["severity"] == "warning":
            return finding["id"]
    pytest.fail("the audit reports no warning on a scratch install")


async def _audit_rows(session_factory: async_sessionmaker, finding_id: str) -> list[AuditLog]:
    async with session_factory() as db:
        rows = await db.execute(
            select(AuditLog)
            .where(AuditLog.entity_type == finding_states.AUDIT_ENTITY_TYPE, AuditLog.entity_name == finding_id)
            .order_by(AuditLog.created_at)
        )
        return list(rows.scalars().all())


async def _state(session_factory: async_sessionmaker, finding_id: str) -> SecurityAuditFindingState | None:
    async with session_factory() as db:
        return await db.get(SecurityAuditFindingState, finding_id)


class _Redis:
    def __init__(self, values: dict[str, str] | None = None):
        self.values = dict(values or {})
        self.expiries: dict[str, int] = {}

    async def set(self, key, value, nx=False, ex=None):
        if nx and key in self.values:
            return None
        self.values[key] = value
        return True

    async def get(self, key):
        return self.values.get(key)

    async def expire(self, key, seconds):
        self.expiries[key] = seconds


async def _notified_ids(session_factory: async_sessionmaker) -> list[list[str]]:
    """The finding ids of each Security Audit notification, oldest first (one admin)."""
    async with session_factory() as db:
        rows = await db.execute(
            select(Activity).where(Activity.activity_type == "notification").order_by(Activity.created_at)
        )
        return [
            row.extra_data["finding_ids"]
            for row in rows.scalars().all()
            if (row.extra_data or {}).get("notification_source") == "security_audit"
        ]


# ---------------------------------------------------------------------------
# Dismiss and restore
# ---------------------------------------------------------------------------


async def test_a_dismissal_leaves_the_counts_is_audited_and_is_restored() -> None:
    async with _scratch("security_audit_dismiss") as scratch:
        admin = await _admin(scratch.sessions)
        async with _client(scratch.sessions, admin) as client:
            before = (await client.get(AUDIT)).json()
            finding_id = _notifiable(before)
            dismissed = await client.put(
                f"{AUDIT}/findings/{finding_id}/dismissal", json={"reason": "  Accepted: internal network only  "}
            )
            again = (await client.get(AUDIT)).json()
            restored = await client.delete(f"{AUDIT}/findings/{finding_id}/dismissal")
            restored_twice = await client.delete(f"{AUDIT}/findings/{finding_id}/dismissal")
        rows = await _audit_rows(scratch.sessions, finding_id)
        state = await _state(scratch.sessions, finding_id)

    assert dismissed.status_code == 200, dismissed.text
    report = dismissed.json()
    marker = _by_id(report)[finding_id]["dismissed"]
    assert marker["severity"] == "warning"
    assert marker["reason"] == "Accepted: internal network only"
    assert marker["by_user_id"] == str(admin.id) and marker["by"] == "Admin"
    assert marker["at"]
    assert report["dismissed_count"] == 1
    assert report["summary"]["warning"] == before["summary"]["warning"] - 1
    # Read again, still dismissed.
    assert _by_id(again)[finding_id]["dismissed"]["reason"] == "Accepted: internal network only"

    assert restored.status_code == 200, restored.text
    assert _by_id(restored.json())[finding_id]["dismissed"] is None
    assert restored.json()["summary"] == before["summary"]
    assert restored_twice.status_code == 404

    assert [(row.action, row.user_id) for row in rows] == [("dismiss", admin.id), ("restore", admin.id)]
    assert rows[0].details["reason"] == "Accepted: internal network only"
    assert rows[0].details["severity"] == "warning"
    assert rows[1].details["dismissed_severity"] == "warning"
    # The last dismissal stays on the row as history, with who restored it.
    assert state is not None and state.dismissed_at is None
    assert (state.dismissed_by_user_id, state.dismissed_severity, state.dismissal_reason) == (
        admin.id,
        "warning",
        "Accepted: internal network only",
    )
    assert state.restored_by_user_id == admin.id and state.restored_at is not None


async def test_a_finding_the_audit_does_not_report_is_never_stored() -> None:
    async with _scratch("security_audit_dismiss_unknown") as scratch:
        admin = await _admin(scratch.sessions)
        async with _client(scratch.sessions, admin) as client:
            response = await client.put(f"{AUDIT}/findings/agents.not_a_finding/dismissal", json={})
        state = await _state(scratch.sessions, "agents.not_a_finding")
        rows = await _audit_rows(scratch.sessions, "agents.not_a_finding")

    assert response.status_code == 404
    assert state is None and rows == []


# ---------------------------------------------------------------------------
# Re-opening
# ---------------------------------------------------------------------------


async def test_a_finding_reported_above_its_dismissal_re_opens_once_and_notifies_once(monkeypatch) -> None:
    async with _scratch("security_audit_reopen") as scratch:
        admin = await _admin(scratch.sessions)
        monkeypatch.setattr(notifications, "async_session", scratch.sessions)
        redis = _Redis()
        async with _client(scratch.sessions, admin) as client:
            finding_id = _notifiable((await client.get(AUDIT)).json())
            # Told once, then dismissed.
            assert await notifications.notify_new_findings(redis) >= 1
            assert (
                await client.put(f"{AUDIT}/findings/{finding_id}/dismissal", json={"reason": "fine"})
            ).status_code == 200
            assert await notifications.notify_new_findings(redis) == 0
            # As if it had been dismissed while it was only an info.
            async with scratch.sessions() as db:
                await db.execute(
                    update(SecurityAuditFindingState)
                    .where(SecurityAuditFindingState.finding_id == finding_id)
                    .values(dismissed_severity="info")
                )
                await db.commit()
            reopened = (await client.get(AUDIT)).json()
            read_again = (await client.get(AUDIT)).json()
            # Reading the report writes nothing: the dismissal is still on the row.
            unrecorded = await _state(scratch.sessions, finding_id)
            first = await notifications.notify_new_findings(redis)
            second = await notifications.notify_new_findings(redis)
            recorded = (await client.get(AUDIT)).json()
        notified = await _notified_ids(scratch.sessions)
        rows = await _audit_rows(scratch.sessions, finding_id)
        state = await _state(scratch.sessions, finding_id)

    finding = _by_id(reopened)[finding_id]
    assert finding["dismissed"] is None
    assert finding["reopened"]["at"] is None
    assert finding["reopened"]["severity"] == "warning"
    assert finding["reopened"]["dismissal"]["severity"] == "info"
    assert finding["reopened"]["dismissal"]["reason"] == "fine"
    assert finding["reopened"]["dismissal"]["by"] == "Admin"
    assert reopened["dismissed_count"] == 0
    assert _by_id(read_again)[finding_id] == finding
    assert unrecorded is not None and unrecorded.dismissed_at is not None and unrecorded.reopened_at is None
    # Recorded by the hourly run.
    assert _by_id(recorded)[finding_id]["reopened"]["at"] is not None
    assert _by_id(recorded)[finding_id]["reopened"]["dismissal"]["at"] is None

    # One re-opening, by the platform, anchored on who had dismissed it.
    assert [(row.action, row.user_email) for row in rows] == [
        ("dismiss", "admin@example.com"),
        ("reopen", "system:security_audit"),
    ]
    assert rows[1].user_id == admin.id
    assert rows[1].details["dismissed_severity"] == "info" and rows[1].details["severity"] == "warning"

    # Told at first, then once more for the re-opening, then never again.
    assert (first, second) == (1, 0)
    assert finding_id in notified[0]
    assert notified[1:] == [[finding_id]]
    assert state is not None and state.notified_severity == "warning"
    # Recorded and notified by the same run, at the same instant.
    assert state.reopened_at is not None and state.notified_at == state.reopened_at


# ---------------------------------------------------------------------------
# The notification memory
# ---------------------------------------------------------------------------


async def test_each_finding_is_notified_once_and_losing_redis_changes_nothing(monkeypatch) -> None:
    async with _scratch("security_audit_notify_once") as scratch:
        await _admin(scratch.sessions)
        monkeypatch.setattr(notifications, "async_session", scratch.sessions)
        first = await notifications.notify_new_findings(_Redis())
        second = await notifications.notify_new_findings(_Redis())
        async with scratch.sessions() as db:
            states = await finding_states.load_states(db)
        notified = await _notified_ids(scratch.sessions)

    assert first >= 1 and second == 0
    assert len(notified) == 1 and len(notified[0]) == first
    assert {finding_id for finding_id, state in states.items() if state.notified_at} == set(notified[0])


async def test_a_dismissed_finding_is_never_notified(monkeypatch) -> None:
    async with _scratch("security_audit_notify_dismissed") as scratch:
        admin = await _admin(scratch.sessions)
        monkeypatch.setattr(notifications, "async_session", scratch.sessions)
        async with _client(scratch.sessions, admin) as client:
            finding_id = _notifiable((await client.get(AUDIT)).json())
            assert (await client.put(f"{AUDIT}/findings/{finding_id}/dismissal")).status_code == 200
        await notifications.notify_new_findings(_Redis())
        notified = await _notified_ids(scratch.sessions)
        state = await _state(scratch.sessions, finding_id)

    assert all(finding_id not in ids for ids in notified)
    assert state is not None and state.notified_at is None


async def test_the_upgrade_carries_the_redis_memory_over(monkeypatch) -> None:
    async with _scratch("security_audit_notify_upgrade") as scratch:
        await _admin(scratch.sessions)
        monkeypatch.setattr(notifications, "async_session", scratch.sessions)
        # What the audit reports here, as the notifier remembered it.
        async with scratch.sessions() as db:
            report = await security_audit.build_security_audit_report(db)
        told = {
            finding.id: {"severity": finding.severity, "seen": 1.0}
            for finding in report.findings
            if finding.id and finding.severity in ("critical", "warning")
        }
        held_back = sorted(told)[0]
        del told[held_back]
        redis = _Redis({notifications.LEGACY_STATE_KEY: json.dumps({"run": 1.0, "findings": told})})
        notified_count = await notifications.notify_new_findings(redis)
        notified = await _notified_ids(scratch.sessions)
        async with scratch.sessions() as db:
            states = await finding_states.load_states(db)

    # Only what the old memory did not hold.
    assert notified_count == 1 and notified == [[held_back]]
    assert {finding_id for finding_id, state in states.items() if state.notified_at} == {held_back, *told}
    assert redis.expiries == {notifications.LEGACY_STATE_KEY: notifications.LEGACY_KEY_RETENTION_SECONDS}


async def test_with_no_administrator_nothing_is_recorded(monkeypatch) -> None:
    async with _scratch("security_audit_notify_no_admin") as scratch:
        monkeypatch.setattr(notifications, "async_session", scratch.sessions)
        assert await notifications.notify_new_findings(_Redis()) == 0
        async with scratch.sessions() as db:
            states = await finding_states.load_states(db)

    assert states == {}
